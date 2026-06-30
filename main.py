"""
Novara Agent Factory – API Gateway (FastAPI)

Alle Agenten werden über /api/v1/agents/{agent_type}/process angesprochen.
API-Key-Authentifizierung über X-API-Key Header (aus .env).
Strukturiertes JSON-Logging für alle Requests (DSGVO-Audit-Trail).
"""
import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import structlog
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Security, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader

from fastapi.responses import JSONResponse, StreamingResponse

from agents.base_agent import AgentRequest, AgentResponse
from agents.onboarding_agent import OnboardingAgent
from agents.operations_agent import OperationsAgent
from agents.sales_copilot_agent import SalesCopilotAgent
from agents.sdr_agent import SDRAgent
from agents.support_agent import SupportAgent
from agents.voice_agent import VoiceAgent
from core.config import settings
from tools.calendar_integration import GoogleCalendarTool
from tools.document_parser import DocumentParser

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


def _build_registry() -> dict:
    return {
        "onboarding":   OnboardingAgent(),
        "operations":   OperationsAgent(),
        "sales-copilot": SalesCopilotAgent(),
        "sdr":          SDRAgent(),
        "support":      SupportAgent(),
    }


# ── Application Lifespan ──────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _AGENT_REGISTRY, _VOICE_AGENT, _CALENDAR_TOOL
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


# ── Health Endpoints ──────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "healthy",
        "environment": settings.environment,
        "agents": list(_AGENT_REGISTRY.keys()),
    }


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
                        await loop.run_in_executor(None, sdr.process, req)
                        log.info("Voice call → SDR Agent abgeschlossen", session=session_id)
                    except Exception as sdr_exc:
                        log.error("SDR-Hintergrundtask fehlgeschlagen", error=str(sdr_exc))

                asyncio.create_task(_run_sdr_bg())
                log.info("Voice call → SDR Agent gestartet (async)", session=session_id)

        return {"received": True}

    except Exception as exc:
        # Letzte Absicherung: niemals HTTP 500 an Vapi zurückgeben
        log.error("Vapi webhook: unbehandelter Fehler", error=str(exc), exc_info=True)
        return {"received": False, "error": "internal server error"}


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
