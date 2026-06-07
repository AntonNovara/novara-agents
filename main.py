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

from fastapi.responses import StreamingResponse

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
    _AGENT_REGISTRY = _build_registry()
    _VOICE_AGENT = VoiceAgent()
    _CALENDAR_TOOL = GoogleCalendarTool()
    log.info("Agent registry initialised", agents=list(_AGENT_REGISTRY.keys()))
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
    allow_origins=["*"] if not settings.is_production else [],
    allow_methods=["POST", "GET"],
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
    summary="Vapi Custom LLM – OpenAI-kompatibler Streaming-Endpunkt",
)
async def voice_chat_completions(request: Request):
    body = await request.json()
    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=422, detail="messages array is required")

    return StreamingResponse(
        _VOICE_AGENT.stream(messages),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/v1/voice/webhook",
    tags=["Voice"],
    summary="Vapi Server Webhook (end-of-call-report, function-call, tool-calls)",
)
async def voice_webhook(request: Request):
    body = await request.json()
    msg = body.get("message", {})
    event_type = msg.get("type", "")

    # ── Appointment booking — new Vapi format: tool-calls ─────────────────────
    # Vapi sends this for server-side tools; response must be {"results": [...]}
    if event_type == "tool-calls":
        tool_call_list = msg.get("toolCallList", [])
        results = []
        for tc in tool_call_list:
            call_id = tc.get("id", "")
            fn_name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            params = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

            if fn_name == "book_appointment" and _CALENDAR_TOOL is not None:
                result = _CALENDAR_TOOL.book(
                    date=params.get("date", ""),
                    time=params.get("time", ""),
                    name=params.get("name", ""),
                    company=params.get("company", ""),
                    phone=params.get("phone", ""),
                    topic=params.get("topic", ""),
                )
                if result.success:
                    log.info("Appointment booked", event_id=result.event_id, start=result.start)
                    result_text = (
                        f"Termin erfolgreich eingetragen: {result.summary} am {result.start}."
                    )
                else:
                    log.error("Calendar booking failed", error=result.error)
                    result_text = (
                        "Der Termin konnte leider nicht eingetragen werden. "
                        "Bitte versuchen Sie es später erneut."
                    )
            else:
                result_text = f"Unbekanntes Tool: {fn_name}"

            results.append({"toolCallId": call_id, "result": result_text})

        return {"results": results}

    # ── Appointment booking — legacy Vapi format: function-call ───────────────
    # Older Vapi setups; response must be {"result": "..."}
    if event_type == "function-call":
        fn = msg.get("functionCall", {})
        if fn.get("name") == "book_appointment" and _CALENDAR_TOOL is not None:
            params = fn.get("parameters", {})
            result = _CALENDAR_TOOL.book(
                date=params.get("date", ""),
                time=params.get("time", ""),
                name=params.get("name", ""),
                company=params.get("company", ""),
                phone=params.get("phone", ""),
                topic=params.get("topic", ""),
            )
            if result.success:
                log.info("Appointment booked", event_id=result.event_id, start=result.start)
                return {
                    "result": (
                        f"Termin erfolgreich eingetragen: {result.summary} "
                        f"am {result.start}."
                    )
                }
            else:
                log.error("Calendar booking failed", error=result.error)
                return {
                    "result": (
                        "Der Termin konnte leider nicht eingetragen werden. "
                        "Bitte versuchen Sie es später erneut."
                    )
                }

    # ── Post-call SDR handoff ─────────────────────────────────────────────────
    if event_type == "end-of-call-report":
        transcript = msg.get("transcript", "")
        session_id = msg.get("call", {}).get("id", str(uuid.uuid4()))
        if transcript and _AGENT_REGISTRY.get("sdr"):
            sdr = _AGENT_REGISTRY["sdr"]
            sdr.process(AgentRequest(text=transcript, session_id=session_id))
            log.info("Voice call → SDR Agent triggered", session=session_id)

    return {"received": True}


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
