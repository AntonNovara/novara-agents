"""
Novara Agent Factory – API Gateway (FastAPI)

Alle Agenten werden über /api/v1/agents/{agent_type}/process angesprochen.
API-Key-Authentifizierung über X-API-Key Header (aus .env).
Strukturiertes JSON-Logging für alle Requests (DSGVO-Audit-Trail).
"""
import json
import logging
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Security, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from fastapi.responses import JSONResponse, Response, StreamingResponse

from agents.base_agent import AgentRequest, AgentResponse
from agents.field_worker_agent import FieldWorkerAgent
from agents.guardian_agent import GuardianAgent
from agents.onboarding_agent import OnboardingAgent
from agents.operations_agent import OperationsAgent
from agents.sales_copilot_agent import SalesCopilotAgent
from agents.sdr_agent import SDRAgent
from agents.support_agent import SupportAgent
from agents.voice_agent import VoiceAgent
from core import consent, lead_capture
from core.config import settings
from core.security import OutputBlockedError, SecurityLayer
from tools import lead_notifier, sequence_scheduler
from tools.calendar_integration import GoogleCalendarTool
from tools.document_parser import DocumentParser
from tools.reply_classifier import ReplyClassifier
from utils.pdf_generator import generate_regiebericht, suggested_filename

# ── Logging Setup ─────────────────────────────────────────────────────────────

def _configure_logging() -> None:
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer() if settings.log_format == "json"
        else structlog.dev.ConsoleRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
    )
    logging.basicConfig(stream=sys.stdout, level=settings.log_level)


_configure_logging()
log = structlog.get_logger("novara.gateway")

# ── Agent Registry ────────────────────────────────────────────────────────────
# Agents sind Singletons – einmalige LLM/Graph-Initialisierung beim Start.

_AGENT_REGISTRY: dict = {}

_VOICE_AGENT: Optional[VoiceAgent] = None
_CALENDAR_TOOL: Optional[GoogleCalendarTool] = None
# GuardianAgent (agents/guardian_agent.py) ist wie VoiceAgent NICHT Teil von
# _AGENT_REGISTRY -- andere Antwortform (strukturierter Health-Audit statt
# AgentRequest/AgentResponse), passt nicht ins generische BaseAgent-Interface.
_GUARDIAN_AGENT: Optional[GuardianAgent] = None


def _build_registry() -> dict:
    return {
        "field-worker": FieldWorkerAgent(),
        "onboarding":   OnboardingAgent(),
        "operations":   OperationsAgent(),
        "sales-copilot": SalesCopilotAgent(),
        "sdr":          SDRAgent(),
        "support":      SupportAgent(),
    }


# ── Application Lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _AGENT_REGISTRY, _VOICE_AGENT, _CALENDAR_TOOL, _GUARDIAN_AGENT
    log.info("Novara Agent Factory starting", environment=settings.environment)

    # Aufgeloeste Modell-ID beim Start loggen, damit ein falscher ANTHROPIC_MODEL-
    # Env-Override in Railway sofort sichtbar ist (Ursache fuer model_not_found).
    log.info("LLM-Modell konfiguriert", anthropic_model=settings.anthropic_model)

    # ANTHROPIC_API_KEY beim Start verifizieren – nicht erst beim ersten LLM-Call.
    if settings.anthropic_key_configured:
        log.info("ANTHROPIC_API_KEY geladen", source="environment")
    elif settings.is_production:
        log.error("ANTHROPIC_API_KEY fehlt oder ist Platzhalter – Abbruch in Production")
        raise RuntimeError(
            "ANTHROPIC_API_KEY ist nicht gesetzt. Bitte als Environment Variable "
            "in Railway hinterlegen (Settings → Variables)."
        )
    else:
        log.warning(
            "ANTHROPIC_API_KEY fehlt oder ist Platzhalter – LLM-Calls werden fehlschlagen",
            environment=settings.environment,
        )

    _AGENT_REGISTRY = _build_registry()
    _VOICE_AGENT = VoiceAgent()
    _CALENDAR_TOOL = GoogleCalendarTool()
    # GuardianAgent wiederverwendet _VOICE_AGENT.check_connectivity() für den
    # Anthropic-Teil seines Audits, daher erst NACH VoiceAgent() konstruiert.
    _GUARDIAN_AGENT = GuardianAgent(voice_agent=_VOICE_AGENT)
    log.info("Agent registry initialised", agents=list(_AGENT_REGISTRY.keys()))

    # Egress/Auth gegen die Anthropic-API beim Start verifizieren, damit ein
    # APIConnectionError (z. B. blockierter Egress auf Railway) sofort in den
    # Logs steht statt erst beim ersten Anruf. Nicht fatal – nur Diagnose.
    if settings.anthropic_key_configured:
        check = _VOICE_AGENT.check_connectivity()
        if check.get("ok"):
            log.info("Anthropic-API erreichbar", **check)
        else:
            log.error("Anthropic-API NICHT erreichbar – Egress/Auth pruefen", **check)

    yield
    log.info("Novara Agent Factory shutting down")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Novara Agent Factory",
    description="Modulares Multi-Agenten-System für B2B-Automatisierung",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,  # Swagger nur in Dev
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # Vapi und ngrok müssen immer durchkommen
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# static/chat_widget.js wird von hier ausgeliefert, damit eine Landing Page
# ihn mit EINER Zeile einbinden kann, ohne die Datei selbst zu hosten:
#   <script src="https://<dieses-deployment>/static/chat_widget.js"></script>
# Das Widget leitet seine API-Basis-URL standardmäßig vom eigenen Script-
# Origin ab (siehe static/chat_widget.js) -- funktioniert dadurch automatisch,
# solange es von genau diesem Deployment geladen wird.
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

# ── Authentication ────────────────────────────────────────────────────────────

_api_key_scheme = APIKeyHeader(name=settings.api_key_header, auto_error=False)


async def require_api_key(api_key: Optional[str] = Security(_api_key_scheme)) -> str:
    expected = settings.api_secret_key.get_secret_value()
    # In development mode, skip auth when key is the default placeholder
    if settings.environment == "development" and expected == "dev-secret":
        return "dev-bypass"
    if not api_key or api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": settings.api_key_header},
        )
    return api_key


# ── Request Middleware (Correlation ID + Timing) ──────────────────────────────

@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

    start = time.perf_counter()
    response = await call_next(request)
    elapsed = round((time.perf_counter() - start) * 1000, 2)

    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Processing-Time-Ms"] = str(elapsed)

    log.info(
        "HTTP request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        ms=elapsed,
    )
    return response


# ── Root ──────────────────────────────────────────────────────────────────────
# Einfacher, abhängigkeitsloser 200er auf GET / -- manche Plattform-/Netzwerk-
# Diagnosen (und einfache Uptime-Checks) fragen die Root-Route ab statt eines
# konfigurierten Healthcheck-Pfads. Bewusst ohne jeden Zugriff auf
# _AGENT_REGISTRY o. ä., damit hier nichts werfen kann.

@app.get("/", tags=["System"])
async def root():
    return {"status": "ok", "service": "novara-agent-factory"}


# ── Health Endpoints ──────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "healthy",
        "environment": settings.environment,
        "agents": list(_AGENT_REGISTRY.keys()),
    }


@app.get("/health/models", tags=["System"])
async def health_models():
    """Listet alle auf diesem Anthropic-Account verfügbaren Modelle auf."""
    if _VOICE_AGENT is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "voice agent not initialised"})
    import asyncio
    loop = asyncio.get_running_loop()

    def _list_models():
        try:
            models = _VOICE_AGENT._client.models.list()
            return {"ok": True, "models": [m.id for m in models.data], "configured": settings.anthropic_model}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "configured": settings.anthropic_model}

    result = await loop.run_in_executor(None, _list_models)
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


@app.get("/health/egress", tags=["System"])
async def health_egress():
    """Diagnosiert DNS-Auflösung und Raw-TCP-Verbindung zu api.anthropic.com.
    Zeigt genau, ob Railway DNS, IPv4-TCP oder IPv6-TCP blockiert."""
    import asyncio
    import socket

    host = "api.anthropic.com"
    port = 443
    loop = asyncio.get_running_loop()

    results: dict = {}

    # DNS — alle Records
    try:
        infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port))
        results["dns"] = {"ok": True, "addresses": [i[4][0] for i in infos[:6]]}
    except Exception as e:
        results["dns"] = {"ok": False, "error": str(e)}

    def _tcp(family: int, label: str) -> dict:
        try:
            infos = socket.getaddrinfo(host, port, family)
            if not infos:
                return {"ok": None, "note": f"no {label} address"}
            addr = infos[0][4]
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect(addr)
            sock.close()
            return {"ok": True, "addr": addr[0]}
        except socket.gaierror as e:
            return {"ok": None, "note": f"no {label} address", "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    results["tcp_ipv4"] = await loop.run_in_executor(None, _tcp, socket.AF_INET, "ipv4")
    results["tcp_ipv6"] = await loop.run_in_executor(None, _tcp, socket.AF_INET6, "ipv6")
    return results


@app.get("/health/llm", tags=["System"])
async def health_llm():
    """On-Demand-Egress-Test gegen die Anthropic-API – jederzeit per curl abrufbar,
    ohne neu zu deployen. Unterscheidet APIConnectionError (Egress) von Auth-Fehlern."""
    if _VOICE_AGENT is None:
        return JSONResponse(status_code=503, content={"ok": False, "error": "voice agent not initialised"})
    import asyncio

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _VOICE_AGENT.check_connectivity)
    return JSONResponse(status_code=200 if result.get("ok") else 502, content=result)


@app.get("/api/v1/health/audit", tags=["System"])
async def health_audit():
    """
    Infrastruktur-Audit (agents/guardian_agent.py, GuardianAgent.run_audit()):
    Anthropic-API-Erreichbarkeit, Netlify-Frontend-Erreichbarkeit, Railway-
    Umgebung/-Egress und strukturelle Validität des Multi-Agenten-Graphen
    (alle 5 Factory-Agenten + die 4 InboundChatGraph-Nodes) in einem Aufruf.

    BEWUSST UNAUTHENTIFIZIERT trotz /api/v1/-Präfix (anders als die übrigen
    /api/v1/agents/*-Endpunkte) -- gleiche Begründung wie die bestehenden
    /health/*-Endpunkte oben (/health/egress exponiert bereits vergleichbar
    detaillierte Diagnose-Infos unauthentifiziert): ein Health-Audit muss
    von externen Monitoring-/Uptime-Tools ohne Secret abrufbar sein. Läuft
    unter /api/v1/health/audit statt /health/audit, weil main.py bisher
    ALLE reinen Diagnose-Endpunkte unter /health/* führt UND alle
    Business-Endpunkte unter /api/v1/* -- dieser Audit ist explizit als
    strukturierter API-Contract gedacht (feste JSON-Form, siehe
    GuardianAgent.run_audit()), nicht als Ad-hoc-Diagnose wie /health/egress.

    Statuscode folgt dem aggregierten "status"-Feld: 200 bei "healthy" oder
    "degraded" (der Service selbst antwortet weiterhin normal, auch wenn
    z. B. Netlify gerade down ist), 503 NUR bei "unhealthy" (der
    Agenten-Graph selbst ist strukturell beschädigt -- das einzige
    Szenario, in dem ein Monitoring-Tool wirklich alarmieren sollte).
    """
    if _GUARDIAN_AGENT is None:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "error": "guardian agent not initialised"})
    import asyncio

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _GUARDIAN_AGENT.run_audit, _AGENT_REGISTRY)
    return JSONResponse(status_code=200 if result["status"] != "unhealthy" else 503, content=result)


@app.get("/api/v1/agents", tags=["Agents"], dependencies=[Depends(require_api_key)])
async def list_agents():
    return {
        "agents": [
            {"type": agent_type, "status": "ready"} for agent_type in _AGENT_REGISTRY
        ]
    }


# ── Core Agent Endpoint ───────────────────────────────────────────────────────

@app.post(
    "/api/v1/agents/{agent_type}/process",
    response_model=AgentResponse,
    tags=["Agents"],
    summary="Trigger an agent with a text payload",
    responses={
        200: {"description": "Agent processed successfully"},
        401: {"description": "Unauthorized – missing or invalid API key"},
        404: {"description": "Agent type not found"},
        422: {"description": "Validation error in request body"},
    },
)
async def process_agent(
    agent_type: str,
    request: AgentRequest,
    _: str = Depends(require_api_key),
) -> AgentResponse:
    agent = _AGENT_REGISTRY.get(agent_type)
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_type}' not found. Available: {list(_AGENT_REGISTRY.keys())}",
        )

    log.info("Dispatching to agent", agent=agent_type, session=request.session_id)
    return agent.process(request)


# ── PDF / File Upload Endpoint ────────────────────────────────────────────────

_PARSER = DocumentParser()
_ALLOWED_MIME = {"application/pdf", "text/plain"}


@app.post(
    "/api/v1/agents/operations/process-file",
    response_model=AgentResponse,
    tags=["Agents"],
    summary="Process a PDF or plain-text invoice file",
    responses={
        200: {"description": "File processed successfully"},
        401: {"description": "Unauthorized"},
        422: {"description": "Unsupported file type or empty content"},
    },
)
async def process_invoice_file(
    file: UploadFile = File(..., description="PDF or plain-text invoice"),
    session_id: str = Form(default=""),
    _: str = Depends(require_api_key),
) -> AgentResponse:
    content_type = (file.content_type or "").split(";")[0].strip()
    filename = (file.filename or "").lower()

    is_pdf = filename.endswith(".pdf") or content_type == "application/pdf"
    is_text = filename.endswith(".txt") or content_type == "text/plain"

    if not (is_pdf or is_text):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported file type '{content_type}'. Supported: PDF, plain text.",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty file.")

    text = _PARSER.extract_text_from_pdf(raw) if is_pdf else raw.decode("utf-8", errors="replace")

    if not text.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not extract any text from the file.",
        )

    sid = session_id or str(uuid.uuid4())
    request = AgentRequest(text=text[:32_000], session_id=sid)

    agent = _AGENT_REGISTRY.get("operations")
    log.info("Processing invoice file", filename=file.filename, session=sid)
    return agent.process(request)


# ── Inbound Reply Webhook (SDR-Sequenz) ──────────────────────────────────────

_REPLY_CLASSIFIER = ReplyClassifier()
_REPLY_CHANNELS = ("email", "linkedin", "voice")


class InboundReplyRequest(BaseModel):
    """Eingehende Antwort auf einen SDR-Outreach (E-Mail-Reply, LinkedIn-Nachricht, ...)."""

    identifier: str = Field(..., min_length=1, description="E-Mail-Adresse oder LinkedIn-URL des Absenders")
    channel: str = Field(..., description="'email' | 'linkedin' | 'voice' — Kanal, über den die Antwort kam")
    text: str = Field(..., min_length=1, max_length=8_000)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))


class InboundReplyResponse(BaseModel):
    success: bool
    session_id: str
    intent: str = ""
    confidence: float = 0.0
    rationale: str = ""
    dlp_findings: list[str] = Field(default_factory=list)
    sequence_id: Optional[str] = None
    sequence_status: Optional[str] = None
    consent_recorded: bool = False
    error: Optional[str] = None


@app.post(
    "/api/v1/webhooks/inbound-reply",
    response_model=InboundReplyResponse,
    tags=["SDR"],
    summary="Klassifiziert eine eingehende Antwort auf SDR-Outreach (interested/objection/opt_out)",
    responses={
        200: {"description": "Antwort klassifiziert und verarbeitet"},
        401: {"description": "Unauthorized"},
        422: {"description": "Unbekannter Kanal"},
    },
)
async def inbound_reply_webhook(
    payload: InboundReplyRequest,
    _: str = Depends(require_api_key),
) -> InboundReplyResponse:
    if payload.channel not in _REPLY_CHANNELS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unbekannter Kanal '{payload.channel}'. Erlaubt: {_REPLY_CHANNELS}",
        )

    # Input-DLP -- derselbe Schritt, den BaseAgent.process() vor jedem
    # LLM-Aufruf durchführt (der Text geht ggf. in den ReplyClassifier-
    # LLM-Fallback ein und muss davor abgesichert sein).
    input_dlp = SecurityLayer.check_and_redact(payload.text)
    if not input_dlp.approved:
        log.warning("Inbound reply blocked by DLP", session=payload.session_id, reason=input_dlp.blocked_reason)
        return InboundReplyResponse(
            success=False,
            session_id=payload.session_id,
            error=f"Input blocked by DLP: {input_dlp.blocked_reason}",
        )

    result = _REPLY_CLASSIFIER.classify(input_dlp.redacted_text)
    log.info(
        "Inbound reply classified",
        session=payload.session_id,
        channel=payload.channel,
        intent=result.intent,
        confidence=result.confidence,
    )

    consent_recorded = False
    if result.intent == "opt_out":
        consent.record_opt_out(
            payload.identifier,
            payload.channel,
            reason=f"Antwort klassifiziert als Opt-out ({result.matched_pattern or result.rationale})",
        )
        consent_recorded = True

    sequence_id: Optional[str] = None
    sequence_status: Optional[str] = None
    if result.intent in ("opt_out", "interested"):
        seq = sequence_scheduler.find_by_identifier(payload.identifier)
        if seq is not None:
            stop_reason = "opt_out" if result.intent == "opt_out" else "interested — Mensch übernimmt"
            seq = sequence_scheduler.stop(seq.sequence_id, reason=stop_reason)
            sequence_id, sequence_status = seq.sequence_id, seq.status

    return InboundReplyResponse(
        success=True,
        session_id=payload.session_id,
        intent=result.intent,
        confidence=result.confidence,
        rationale=result.rationale,
        dlp_findings=input_dlp.findings,
        sequence_id=sequence_id,
        sequence_status=sequence_status,
        consent_recorded=consent_recorded,
    )


# ── Landing-Page-Chat-Widget (Inbound-SDR) ────────────────────────────────────
# Gegenstück zum Outbound-SDR-Flow oben: static/chat_widget.js bettet sich
# per einer einzigen <script>-Zeile in JEDE Landing Page ein und spricht
# ausschließlich diesen Endpoint an.

class LandingVisitorInfo(BaseModel):
    """
    Optionale Formulardaten aus dem Chat-Widget (z. B. ein vorgelagertes
    Namens-/E-Mail-Feld). Bewusst ein eigenes, eng begrenztes Schema statt
    eines freien dict -- dieser Endpoint ist öffentlich/unauthentifiziert
    (siehe landing_chat()-Docstring), ein beliebiges dict wäre eine
    unnötig große Angriffsfläche (Prompt-Injection über exotische Keys,
    unbegrenzte Payload-Größe).
    """
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=320)
    company: str = Field(default="", max_length=200)
    phone: str = Field(default="", max_length=50)


class LandingAttachment(BaseModel):
    """
    Optionaler Anhang (PDF-Angebot, Planungs-Tabelle, Foto einer Baustelle),
    den ein Besucher im Chat mitschickt — verarbeitet von
    agents/sdr_agent.py InboundChatGraph.document_node() (Node 2/4, siehe
    CLAUDE.md, Abschnitt "4-Node-Refactor"). content_base64 ist auf ca.
    11 MB begrenzt (Base64 inflationiert ~33 % gegenüber den Rohbytes) --
    document_node() prüft die dekodierte Größe zusätzlich hart gegen
    _MAX_ATTACHMENT_BYTES (8 MB); die Feldgrenze hier ist nur die erste,
    billige Abwehr gegen offensichtlich überdimensionierte Payloads auf
    diesem öffentlichen/unauthentifizierten Endpoint (siehe
    landing_chat()-Docstring).
    """
    filename: str = Field(default="Anhang", max_length=255)
    mime_type: str = Field(default="", max_length=100)
    content_base64: str = Field(..., min_length=1, max_length=11_500_000)


class LandingChatRequest(BaseModel):
    session_id: str = Field(
        ..., min_length=1, max_length=100,
        description="Vom Widget generiert und über die gesamte Konversation hinweg gleich gehalten (z. B. localStorage)",
    )
    message: str = Field(..., min_length=1, max_length=2_000)
    visitor_info: LandingVisitorInfo = Field(default_factory=LandingVisitorInfo)
    attachment: Optional[LandingAttachment] = None


class LandingChatResponse(BaseModel):
    success: bool
    session_id: str
    reply: str = ""
    should_book_demo: bool = False
    booking_url: Optional[str] = None
    icp_score: int = 0
    dlp_findings: list[str] = Field(default_factory=list)
    error: Optional[str] = None


@app.post(
    "/api/v1/chat/landing",
    response_model=LandingChatResponse,
    tags=["Chat"],
    summary="Inbound-Chat-Widget der Landing Page (SDR-Agent, Inbound-Modus)",
    responses={
        200: {"description": "Antwort generiert (auch bei DLP-Block: success=false, kein 4xx/5xx)"},
        422: {"description": "Validierungsfehler im Request-Body"},
        503: {"description": "SDR-Agent noch nicht initialisiert (Server startet gerade)"},
    },
)
async def landing_chat(payload: LandingChatRequest) -> LandingChatResponse:
    """
    ÖFFENTLICH, KEIN API-Key (anders als /api/v1/agents/*) -- static/
    chat_widget.js läuft im Browser jedes anonymen Landing-Page-Besuchers,
    ein Secret könnte dort nie verborgen bleiben. Gleiches Muster wie die
    ebenfalls unauthentifizierten Voice-Endpunkte oben (Vapi kann auch
    keinen X-API-Key mitschicken); CORS ist bereits global offen (siehe
    CORSMiddleware oben). Die Sicherheitsgrenze ist hier NICHT der API-Key,
    sondern: (a) Input-DLP-Check auf jede Nachricht, (b) ein striktes
    visitor_info-Schema statt eines freien dict, (c) eine Obergrenze im
    Session-Store (agents/sdr_agent.py, _MAX_INBOUND_SESSIONS). Echtes
    Rate-Limiting fehlt noch -- siehe CLAUDE.md, "Bekannte Einschränkungen".
    """
    sdr = _AGENT_REGISTRY.get("sdr")
    if sdr is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SDR agent not initialised")

    # Input-DLP -- exakt derselbe Schritt, den BaseAgent.process() vor jedem
    # LLM-Aufruf durchführt (agents/base_agent.py). Hier manuell, weil dieser
    # Endpoint bewusst nicht über AgentRequest/BaseAgent.process() läuft --
    # siehe SDRAgent.process_inbound_chat()-Docstring (mehrstufiges Chat-
    # State statt eines einzelnen zustandslosen Text-Requests).
    input_dlp = SecurityLayer.check_and_redact(payload.message)
    if not input_dlp.approved:
        log.warning("Landing chat blocked by DLP", session=payload.session_id, reason=input_dlp.blocked_reason)
        return LandingChatResponse(
            success=False,
            session_id=payload.session_id,
            error=f"Input blocked by DLP: {input_dlp.blocked_reason}",
        )

    try:
        result = sdr.process_inbound_chat(
            session_id=payload.session_id,
            message=input_dlp.redacted_text,
            visitor_info=payload.visitor_info.model_dump(),
            attachment=payload.attachment.model_dump() if payload.attachment else None,
        )
    except Exception as exc:
        log.exception("Landing chat failed", session=payload.session_id)
        return LandingChatResponse(success=False, session_id=payload.session_id, error=str(exc))

    # Output-DLP -- die generierte Antwort selbst absichern, bevor sie den
    # Prozess verlässt. Gleiche Philosophie wie SecurityLayer.sanitize_dict()
    # in agents/base_agent.py: ein Hard-Block-Treffer im LLM-Output darf nie
    # unbehandelt durchgehen, gerade nicht auf einem öffentlichen Endpoint.
    try:
        sanitized = SecurityLayer.sanitize_dict(result)
    except OutputBlockedError as exc:
        log.warning("Landing chat output blocked by DLP", session=payload.session_id)
        return LandingChatResponse(
            success=False,
            session_id=payload.session_id,
            error=f"Output blocked by DLP: {exc.blocked_reason}",
            dlp_findings=input_dlp.findings,
        )

    return LandingChatResponse(
        success=True,
        session_id=payload.session_id,
        reply=sanitized.get("reply", ""),
        should_book_demo=sanitized.get("should_book_demo", False),
        booking_url=sanitized.get("booking_url"),
        icp_score=(sanitized.get("icp") or {}).get("score", 0),
        dlp_findings=input_dlp.findings,
    )


# ── Voice / Vapi Endpunkte ────────────────────────────────────────────────────
#
# Vapi hängt an die konfigurierte Custom-LLM-URL automatisch /chat/completions an.
# Wenn du in Vapi als URL https://<host>/api/v1/voice/chat einträgst,
# ruft Vapi tatsächlich POST /api/v1/voice/chat/chat/completions auf.

@app.post(
    "/api/v1/voice/chat/chat/completions",
    tags=["Voice"],
    summary="Vapi Custom LLM – OpenAI-kompatibler Endpunkt (streaming + non-streaming)",
)
async def voice_chat_completions(request: Request):
    import asyncio

    # JSON-Parse absichern — Vapi schickt manchmal kaputte Bodies
    try:
        body = await request.json()
    except Exception as parse_exc:
        log.warning("Custom LLM: ungültiger JSON-Body", error=str(parse_exc))
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
        )

    messages = body.get("messages", [])
    if not messages:
        return JSONResponse(
            status_code=422,
            content={"error": {"message": "messages array is required", "type": "invalid_request_error"}},
        )

    # Vapi schickt sein Dashboard-Modell im Body mit — wir ignorieren es komplett
    # und erzwingen immer settings.anthropic_model, damit veraltete/ungültige
    # Modell-Namen aus dem Vapi-Dashboard keinen 404 auf Anthropic-Seite auslösen.
    vapi_model = body.get("model", "")
    if vapi_model and vapi_model != settings.anthropic_model:
        log.warning(
            "Vapi-Modell überschrieben – verwende eigenes Modell",
            vapi_model=vapi_model,
            using=settings.anthropic_model,
        )

    # Vapi sendet manchmal stream=false — dann reguläres JSON zurückgeben
    stream_requested = body.get("stream", True)

    if not stream_requested:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _VOICE_AGENT.complete, messages)
        return JSONResponse(content=result)

    return StreamingResponse(
        _VOICE_AGENT.stream(messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _save_call_markdown(session_id: str, transcript: str, sdr_response, call_msg: dict) -> None:
    """Speichert Transkript + SDR-Ergebnis als Markdown-Datei für Claude Cowork.

    Zielordner: settings.calls_export_dir (Standard ~/novara-calls).
    Dateiname:  YYYY-MM-DD_HH-MM_<Firma>.md
    Auf Railway ist der Ordner flüchtig; für Persistenz Railway-Volume unter
    /novara-calls einbinden und CALLS_EXPORT_DIR=/novara-calls setzen.
    """
    try:
        export_dir = Path(settings.calls_export_dir).expanduser()
        export_dir.mkdir(parents=True, exist_ok=True)

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d_%H-%M")

        # Firmenname aus SDR-Ergebnis, sonst Session-ID als Fallback
        company = ""
        sdr_block = ""
        if sdr_response and sdr_response.success:
            r = sdr_response.result
            company = r.get("company_name", "")
            qualified = r.get("qualified", False)
            score = r.get("lead_score", "–")
            outreach = r.get("outreach") or {}
            next_steps = r.get("next_steps") or []

            sdr_block = f"""
## SDR-Analyse

| | |
|---|---|
| **Lead-Score** | {score}/100 |
| **Qualifiziert** | {"✅ Ja" if qualified else "❌ Nein"} |
| **Firma** | {r.get("company_name", "–")} |
| **Kontakt** | {r.get("contact_name", "–")} ({r.get("contact_title", "–")}) |
| **Kanal** | {outreach.get("channel", "–")} |

### Outreach-Entwurf
**Betreff:** {outreach.get("subject", "–")}

{outreach.get("body", "– kein Entwurf –")}

### Empfohlene Next Steps
{"".join(f"- {ns}{chr(10)}" for ns in next_steps) or "– keine –"}
"""

        safe_name = re.sub(r"[^\w\-]", "-", company)[:40] if company else session_id[:8]
        filepath = export_dir / f"{date_str}_{safe_name}.md"

        call_duration = call_msg.get("call", {}).get("endedAt", "")
        md = f"""# Novara Anruf-Protokoll

| | |
|---|---|
| **Datum** | {now.strftime("%d.%m.%Y %H:%M")} UTC |
| **Session** | `{session_id}` |
| **Ende** | {call_duration or "–"} |
{sdr_block}
---

## Gesprächstranskript

{transcript.strip() or "– kein Transkript übermittelt –"}
"""
        filepath.write_text(md, encoding="utf-8")
        log.info("Post-Call Markdown gespeichert", path=str(filepath), session=session_id)

    except Exception as exc:
        log.error("Post-Call Markdown konnte nicht gespeichert werden", error=str(exc), session=session_id)


def _parse_vapi_params(raw) -> dict:
    """Normalise Vapi parameters/arguments — may arrive as dict or JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _run_booking(params: dict) -> str:
    """Execute book_appointment and return a human-readable result string."""
    if _CALENDAR_TOOL is None:
        return "Kalenderdienst nicht initialisiert."
    result = _CALENDAR_TOOL.book(
        date=params.get("date", ""),
        time=params.get("time", ""),
        name=params.get("name", ""),
        company=params.get("company", ""),
        phone=params.get("phone", ""),
        # accept both "topic" (our name) and "summary" (alternative Vapi mapping)
        topic=params.get("topic") or params.get("summary", ""),
    )
    if result.success:
        log.info("Appointment booked", event_id=result.event_id, start=result.start)
        return f"Termin erfolgreich eingetragen: {result.summary} am {result.start}."
    log.error("Calendar booking failed", error=result.error)
    return (
        "Der Termin konnte leider nicht eingetragen werden. "
        "Bitte versuchen Sie es später erneut."
    )


@app.post(
    "/api/v1/voice/webhook",
    tags=["Voice"],
    summary="Vapi Server Webhook (end-of-call-report, function-call, tool-calls)",
)
async def voice_webhook(request: Request):
    # ── JSON-Parse (Vapi schickt manchmal kaputte Bodies) ─────────────────────
    try:
        body = await request.json()
    except Exception as parse_exc:
        log.warning("Vapi webhook: ungültiger JSON-Body", error=str(parse_exc))
        return {"received": False, "error": "invalid JSON body"}

    try:
        msg = body.get("message", {})
        event_type = msg.get("type", "")

        # Full payload logged at INFO so Railway shows exact keys Vapi sends
        log.info("Vapi webhook received", event_type=event_type, payload=body)

        # ── New Vapi format: tool-calls ───────────────────────────────────────
        # Response must be {"results": [{"toolCallId": "...", "result": "..."}]}
        if event_type == "tool-calls":
            tool_call_list = msg.get("toolCallList", [])
            results = []
            for tc in tool_call_list:
                call_id = tc.get("id", "")
                fn = tc.get("function", {})
                fn_name = fn.get("name", "")
                params = _parse_vapi_params(fn.get("arguments", {}))

                try:
                    if fn_name == "book_appointment":
                        result_text = _run_booking(params)
                    else:
                        result_text = f"Unbekanntes Tool: {fn_name}"
                except Exception as tool_exc:
                    log.error("Tool-Ausführung fehlgeschlagen", tool=fn_name, error=str(tool_exc))
                    result_text = "Ein interner Fehler ist aufgetreten. Bitte versuchen Sie es erneut."

                results.append({"toolCallId": call_id, "result": result_text})

            return {"results": results}

        # ── Legacy Vapi format: function-call ─────────────────────────────────
        # Response must be {"result": "..."}
        if event_type == "function-call":
            fn = msg.get("functionCall", {})
            fn_name = fn.get("name", "")
            # parameters may be a dict or a JSON string depending on Vapi version
            params = _parse_vapi_params(fn.get("parameters", {}))

            try:
                if fn_name == "book_appointment":
                    return {"result": _run_booking(params)}
            except Exception as fn_exc:
                log.error("Function-Call fehlgeschlagen", function=fn_name, error=str(fn_exc))
                return {"result": "Ein interner Fehler ist aufgetreten. Bitte versuchen Sie es erneut."}

        # ── Post-call SDR handoff (fire & forget im Thread-Pool) ─────────────
        if event_type == "end-of-call-report":
            transcript = msg.get("transcript", "")
            session_id = msg.get("call", {}).get("id", str(uuid.uuid4()))
            if transcript and _AGENT_REGISTRY.get("sdr"):
                import asyncio

                sdr = _AGENT_REGISTRY["sdr"]
                req = AgentRequest(text=transcript, session_id=session_id)

                async def _run_sdr_bg():
                    try:
                        loop = asyncio.get_running_loop()
                        sdr_response = await loop.run_in_executor(None, sdr.process, req)
                        log.info("Voice call → SDR Agent abgeschlossen", session=session_id)
                        # Post-Call: Markdown für Claude Cowork speichern
                        await loop.run_in_executor(
                            None, _save_call_markdown, session_id, transcript, sdr_response, msg
                        )
                    except Exception as sdr_exc:
                        log.error("SDR-Hintergrundtask fehlgeschlagen", error=str(sdr_exc))

                    # ── Lead-Capture + Benachrichtigung (core/lead_capture.py) ──
                    # Läuft NACH dem Gespräch auf dem vollen Transkript (anders
                    # als der Landing-Chat, der pro Turn erfasst) — der
                    # Live-Gesprächspfad liefert keine strukturierte JSON-
                    # Antwort pro Turn (siehe agents/voice_agent.py), Regex auf
                    # das vollständige Transkript ist hier die zuverlässigere
                    # Stelle für E-Mail/Telefon-Erkennung. Eigener try/except,
                    # unabhängig vom SDR-Hintergrundtask oben, damit ein Fehler
                    # hier niemals den bereits abgeschlossenen SDR-Handoff
                    # rückwirkend als fehlgeschlagen erscheinen lässt.
                    #
                    # notify_lead_async() (statt send_lead_notification()
                    # direkt): dieser gesamte Block läuft als asyncio.Task auf
                    # main.py's Event Loop -- ein synchroner, blockierender
                    # SMTP-Call HIER würde den Loop für alle anderen
                    # gleichzeitigen Requests (auch den Landing-Chat!) bis zu
                    # _SMTP_TIMEOUT_SECONDS lang einfrieren. notify_lead_async()
                    # verschickt stattdessen in einem separaten Thread und
                    # kehrt sofort zurück -- der end-of-call-report-Handler
                    # antwortet Vapi damit unabhängig vom SMTP-Ausgang.
                    try:
                        contact_fields = lead_capture.extract_contact_fields(transcript)
                        new_lead = lead_capture.capture(
                            source="voice",
                            session_id=session_id,
                            message=transcript,
                            name=contact_fields["name"],
                            email=contact_fields["email"],
                            phone=contact_fields["phone"],
                            company=contact_fields["company"],
                        )
                        if new_lead is not None:
                            lead_notifier.notify_lead_async(new_lead)
                    except Exception as lead_exc:
                        log.error("Voice-Lead-Capture fehlgeschlagen", error=str(lead_exc))

                asyncio.create_task(_run_sdr_bg())
                log.info("Voice call → SDR Agent gestartet (async)", session=session_id)

        return {"received": True}

    except Exception as exc:
        # Letzte Absicherung: niemals HTTP 500 an Vapi zurückgeben
        log.error("Vapi webhook: unbehandelter Fehler", error=str(exc), exc_info=True)
        return {"received": False, "error": "internal server error"}


# ── Baustellen-Voice-Assistant: WhatsApp-Webhook (Twilio) ─────────────────────
# ÖFFENTLICH, KEIN X-API-Key -- gleiches Muster wie der Vapi-Webhook oben und
# main.py landing_chat() (main.py docstrings dort): Twilio kann wie Vapi keinen
# benutzerdefinierten Header mitschicken. Die Sicherheitsgrenze ist hier NICHT
# der API-Key, sondern Twilios eigene Request-Signatur (X-Twilio-Signature,
# HMAC-SHA1 über die vollständige aufgerufene URL + alle POST-Parameter,
# geprüft in _verify_twilio_signature()) -- eine ECHTE kryptografische Prüfung
# pro Request, kein Shared Secret im Header. Reports landen unter
# static/reports/ (bereits über app.mount("/static", ...) oben ausgeliefert)
# -- auf Railways ephemerem Dateisystem nur bis zum nächsten Deploy/Neustart
# verfügbar, was für den Zweck (Twilio lädt die PDF kurz nach der
# Webhook-Antwort herunter) ausreicht; siehe CLAUDE.md, "Bekannte
# Einschränkungen" für die vollständige Begründung.

_REPORTS_DIR = Path(__file__).parent / "static" / "reports"


def _verify_twilio_signature(request: Request, form_params: dict) -> bool:
    """
    Ohne TWILIO_AUTH_TOKEN (core/config.py) wird NICHT geprüft (Warn-Log) --
    dieselbe Fail-Safe-für-lokale-Entwicklung-Philosophie wie
    ANTHROPIC_API_KEY/Demo-Modus (core/llm.py): kein Twilio-Zugang lokal soll
    den Endpoint nicht komplett unbenutzbar machen. In Produktion MUSS
    TWILIO_AUTH_TOKEN gesetzt sein, sonst nimmt der Endpoint unauthentifizierte
    Requests an, die echte LLM-Calls und PDF-Erzeugung auslösen.

    URL wird bewusst NICHT aus request.url allein gebaut: Railway terminiert
    TLS an einem vorgeschalteten Proxy, request.url.scheme/netloc können
    dadurch "http"/die interne Adresse statt der von Twilio tatsächlich
    aufgerufenen "https://...novaraautomation..."-URL melden -- ohne die
    X-Forwarded-*-Header (von Railways Proxy gesetzt) würde JEDE Signatur
    fälschlich als ungültig gelten.
    """
    auth_token = settings.twilio_auth_token.get_secret_value()
    if not auth_token:
        log.warning("WhatsApp-Webhook: TWILIO_AUTH_TOKEN nicht gesetzt -- Signaturprüfung übersprungen")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    netloc = request.headers.get("X-Forwarded-Host", request.url.netloc)
    url = f"{proto}://{netloc}{request.url.path}"
    if request.url.query:
        url += f"?{request.url.query}"

    return RequestValidator(auth_token).validate(url, form_params, signature)


def _twiml_response(message: str, media_url: Optional[str] = None) -> Response:
    """Baut eine TwiML-Antwort (twilio.twiml.messaging_response.MessagingResponse
    escaped den Nachrichtentext selbst korrekt -- kein manuelles XML-String-Building,
    das bei Sonderzeichen im Techniker-Text zu ungültigem XML führen könnte)."""
    twiml = MessagingResponse()
    msg = twiml.message(message)
    if media_url:
        msg.media(media_url)
    return Response(content=str(twiml), media_type="application/xml")


def _download_and_normalize_audio(media_url: str, content_type: str) -> bytes:
    """
    Synchron (Netzwerk-Download UND ffmpeg-Subprozess über pydub sind beide
    blockierend) -- der Aufrufer MUSS dies über loop.run_in_executor()
    aufrufen, sonst friert der Event Loop für alle gleichzeitigen Requests ein
    (gleiche Regel wie überall sonst in main.py, z. B. /health/llm).

    Twilio-Media-URLs verlangen HTTP-Basic-Auth mit Account-SID+Auth-Token,
    sonst liefert Twilio einen 401 statt der Audiodatei. Ergebnis ist auf WAV
    normalisiert -- WhatsApp-Sprachnachrichten kommen i. d. R. als
    "audio/ogg; codecs=opus", ein für die meisten STT-Provider unhandliches
    Format; WAV ist der kleinste gemeinsame Nenner für eine künftige
    Transkriptions-Anbindung (siehe _transcribe_audio()).
    """
    account_sid = settings.twilio_account_sid.get_secret_value()
    auth_token = settings.twilio_auth_token.get_secret_value()
    resp = httpx.get(media_url, auth=(account_sid, auth_token), timeout=15)
    resp.raise_for_status()

    import io

    from pydub import AudioSegment

    fmt = "ogg" if "ogg" in content_type.lower() else None
    segment = AudioSegment.from_file(io.BytesIO(resp.content), format=fmt)
    buffer = io.BytesIO()
    segment.export(buffer, format="wav")
    return buffer.getvalue()


def _transcribe_audio(wav_bytes: bytes) -> Optional[str]:
    """
    BEKANNTE LÜCKE, bewusst nicht stillschweigend vorgetäuscht: dieses Repo
    hat noch KEINEN Speech-to-Text-Provider angebunden. Claude (Anthropic
    Messages API) transkribiert kein Audio; Vapi (agents/voice_agent.py)
    umgeht das Problem, indem es STT komplett selbst/extern übernimmt und
    novara-agents nur bereits transkribierten Text sieht -- für WhatsApp gibt
    es keine solche vorgelagerte Instanz.

    Gibt bewusst None zurück statt zu raten/zu halluzinieren -- der Aufrufer
    (whatsapp_webhook()) antwortet dem Techniker dann ehrlich, dass
    automatische Transkription noch nicht aktiv ist, statt eine erfundene
    oder leere Zusammenfassung als Regiebericht auszugeben. Einziger,
    bewusst isolierter Anknüpfungspunkt für eine künftige Anbindung (z. B.
    Whisper API) -- Audio-Download+Normalisierung (_download_and_normalize_audio)
    ist bereits vollständig implementiert und liefert hier fertiges WAV an.
    """
    logger.debug("Audio-Transkription angefordert (%d Bytes) -- noch kein STT-Provider konfiguriert", len(wav_bytes))
    return None


@app.post(
    "/api/v1/webhook/whatsapp",
    tags=["WhatsApp"],
    summary="Twilio-WhatsApp-Webhook (Baustellen-Voice-Assistant)",
    responses={
        200: {"description": "TwiML-Antwort (auch bei internen Fehlern -- Twilio bekommt nie einen 5xx)"},
        401: {"description": "Ungültige oder fehlende Twilio-Signatur"},
    },
)
async def whatsapp_webhook(request: Request):
    """
    Nimmt eine WhatsApp-Text- oder Sprachnachricht eines Technikers entgegen
    (Twilio WhatsApp Business API), extrahiert über field_worker_agent.py
    strukturierte Regiebericht-Daten, erzeugt via utils/pdf_generator.py ein
    PDF und schickt eine TwiML-Bestätigung (inkl. PDF-Link) zurück.

    Twilio sendet `application/x-www-form-urlencoded`, NICHT JSON -- anders
    als jeder andere Endpoint in main.py. Wichtige Felder: `From` (Absender-
    Nummer), `Body` (Text), `NumMedia`/`MediaUrl0`/`MediaContentType0`
    (Anhang, z. B. eine Sprachnachricht).
    """
    try:
        form = await request.form()
        params = {k: str(v) for k, v in form.items()}
    except Exception as exc:
        log.warning("WhatsApp-Webhook: Form-Parsing fehlgeschlagen", error=str(exc))
        return _twiml_response("Entschuldigung, deine Nachricht konnte nicht gelesen werden.")

    if not _verify_twilio_signature(request, params):
        log.warning("WhatsApp-Webhook: ungültige Twilio-Signatur", frm=params.get("From", ""))
        raise HTTPException(status_code=401, detail="Invalid Twilio signature")

    sender = params.get("From", "unknown")
    body_text = (params.get("Body") or "").strip()
    try:
        num_media = int(params.get("NumMedia", "0") or "0")
    except ValueError:
        num_media = 0

    log.info("WhatsApp-Webhook empfangen", frm=sender, has_media=num_media > 0, body_chars=len(body_text))

    import asyncio
    loop = asyncio.get_running_loop()

    transcript = body_text
    audio_note = ""

    if num_media > 0:
        media_url = params.get("MediaUrl0", "")
        media_type = params.get("MediaContentType0", "")
        if media_type.startswith("audio/"):
            try:
                wav_bytes = await loop.run_in_executor(
                    None, _download_and_normalize_audio, media_url, media_type
                )
                transcribed = _transcribe_audio(wav_bytes)
                if transcribed:
                    transcript = f"{transcript}\n{transcribed}".strip()
                else:
                    audio_note = (
                        "\n\n\U0001f399️ Sprachnachricht erhalten, aber automatische Transkription "
                        "ist noch nicht aktiv -- bitte schick deinen Bericht zusätzlich als Text."
                    )
            except Exception as exc:
                log.warning("WhatsApp-Webhook: Audio-Verarbeitung fehlgeschlagen", error=str(exc))
                audio_note = (
                    "\n\n⚠️ Deine Sprachnachricht konnte nicht verarbeitet werden. "
                    "Bitte schick deinen Bericht als Text."
                )
        else:
            audio_note = (
                f"\n\n\U0001f4ce Anhang ({media_type or 'unbekannter Typ'}) erhalten, aber nicht "
                "verarbeitet -- bitte schick Text oder eine Sprachnachricht."
            )

    if not transcript:
        return _twiml_response(
            "Hallo! Schick mir kurz, was du heute gemacht hast (Kunde, Stunden, Material) -- "
            "ich erstelle daraus automatisch deinen Regiebericht." + audio_note
        )

    field_worker = _AGENT_REGISTRY.get("field-worker")
    if field_worker is None:
        return _twiml_response(
            "Der Baustellen-Assistent ist gerade nicht verfügbar. Bitte versuch es in ein paar Minuten erneut."
        )

    agent_response = await loop.run_in_executor(
        None,
        field_worker.process,
        AgentRequest(text=transcript, session_id=f"whatsapp-{sender}-{uuid.uuid4().hex[:8]}"),
    )

    if not agent_response.success:
        log.warning("WhatsApp-Webhook: field-worker-Agent fehlgeschlagen", error=agent_response.error)
        return _twiml_response(
            "Entschuldigung, da ist etwas schiefgelaufen. Bitte versuch es nochmal oder melde dich direkt bei Anton."
            + audio_note
        )

    data = agent_response.result

    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        pdf_path = await loop.run_in_executor(
            None, generate_regiebericht, data, _REPORTS_DIR / suggested_filename(data)
        )
    except Exception as exc:
        log.error("WhatsApp-Webhook: PDF-Erstellung fehlgeschlagen", error=str(exc))
        return _twiml_response(
            "Ich habe deine Angaben erhalten, konnte aber den Regiebericht als PDF nicht erstellen. "
            "Anton wurde informiert." + audio_note
        )

    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    netloc = request.headers.get("X-Forwarded-Host", request.url.netloc)
    pdf_url = f"{proto}://{netloc}/static/reports/{pdf_path.name}"

    stunden_text = data.get("stunden") if data.get("stunden") is not None else "?"
    confirmation = (
        f"✅ Regiebericht erstellt für {data.get('kunde') or 'unbekannten Kunden'} "
        f"({stunden_text} Std.).\n{pdf_url}"
    )
    if data.get("confidence_notes"):
        confirmation += f"\n\nHinweis: {data['confidence_notes']}"
    confirmation += audio_note

    return _twiml_response(confirmation, media_url=pdf_url)


# ── Post-Call Protokolle ──────────────────────────────────────────────────────

@app.get("/api/v1/calls", tags=["Calls"], dependencies=[Depends(require_api_key)])
async def list_calls():
    """Listet alle gespeicherten Anruf-Protokolle (für Claude Cowork / Railway-Zugriff)."""
    export_dir = Path(settings.calls_export_dir).expanduser()
    if not export_dir.exists():
        return {"calls": [], "export_dir": str(export_dir), "count": 0}
    files = sorted(export_dir.glob("*.md"), reverse=True)
    return {
        "calls": [{"filename": f.name, "bytes": f.stat().st_size} for f in files[:100]],
        "export_dir": str(export_dir),
        "count": len(files),
    }


@app.get("/api/v1/calls/{filename}", tags=["Calls"], dependencies=[Depends(require_api_key)])
async def get_call(filename: str):
    """Gibt den Markdown-Inhalt eines Anruf-Protokolls zurück."""
    if not re.match(r"^[\w\-]+\.md$", filename):
        raise HTTPException(status_code=400, detail="Ungültiger Dateiname")
    export_dir = Path(settings.calls_export_dir).expanduser()
    filepath = export_dir / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Protokoll nicht gefunden")
    return {"filename": filename, "content": filepath.read_text(encoding="utf-8")}


# ── Dev Runner ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=not settings.is_production,
        log_level=settings.log_level.lower(),
    )
