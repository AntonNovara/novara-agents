"""
GuardianAgent — Health & Infrastructure Audit + Self-Healing-Middleware.

Zwei unabhängige, aber thematisch zusammengehörige Verantwortlichkeiten in
einer Datei (beide dienen der Systemstabilität, beide werden von genau
zwei Aufrufstellen importiert):

1. **Health & Infrastructure Audit** (`GuardianAgent.run_audit()`) —
   main.py exponiert das Ergebnis über `GET /api/v1/health/audit`. Prüft in
   einem einzigen Aufruf: Anthropic-API-Erreichbarkeit (wrappt die
   bestehende `VoiceAgent.check_connectivity()`-Logik statt sie zu
   duplizieren), Netlify-Frontend-Erreichbarkeit (echter HTTP-Call gegen
   die Live-Website), Railway-Umgebung + -Egress, und die strukturelle
   Validität des Multi-Agenten-Graphen (alle 5 Factory-Agenten registriert
   + `InboundChatGraph` exponiert exakt die erwarteten 4 Nodes aus dem
   17.09.2026-Refactor).

   NICHT Teil von `_AGENT_REGISTRY` (main.py) — wie `VoiceAgent` hat auch
   `GuardianAgent` eine andere Antwortform (strukturierter Audit-Report
   statt `AgentRequest`/`AgentResponse`) und passt nicht ins generische
   `BaseAgent`-Interface. Eigener Modul-Singleton in main.py
   (`_GUARDIAN_AGENT`), analog zu `_VOICE_AGENT`/`_CALENDAR_TOOL`.

2. **Self-Healing Middleware** (`resilient_node()`-Decorator) — angewendet
   auf die 4 Nodes von `agents/sdr_agent.py` `InboundChatGraph`
   (`receptionist_node`, `document_node`, `appointment_node`,
   `supervisor_node`). Zusätzliche Verteidigungsschicht ÜBER den bereits
   bestehenden, node-internen try/except-Blöcken (die bleiben unverändert
   — siehe deren eigene Docstrings/Kommentare, insbesondere
   `receptionist_node`s zweistufiges try/except für LLM-Aufruf vs.
   JSON-Parsing). `receptionist_node`/`document_node` fangen praktisch
   jede erwartbare Fehlerart (LLM-Timeout, ungültiges JSON, kaputtes
   Base64, ...) bereits selbst ab und geben IMMER einen gültigen State
   zurück — dieser Decorator greift dort nur im unwahrscheinlichen Fall
   eines Bugs außerhalb der bekannten Fehlerpfade. Bei `appointment_node`/
   `supervisor_node` ist er dagegen eine ECHTE zusätzliche Absicherung:
   deren Session-Persistenz/Lead-Capture/`customer_state`-Aufrufe sind
   heute NICHT einzeln try/except-abgesichert (nur der Lead-Notification-
   Call selbst) — ein unerwarteter Pydantic-`ValidationError` o. ä. dort
   würde ohne diesen Decorator unbehandelt bis zu `main.py landing_chat()`
   durchschlagen.

   Reversucht bei einer Exception mit kurzem, festem Backoff (keiner der
   4 Nodes rechtfertigt eine aufwändigere Exponential-Backoff-Strategie —
   dies läuft synchron im User-Antwortpfad, siehe main.py landing_chat()).
   Schlagen alle Versuche fehl, wird NIE die Exception propagiert, sondern
   ein `fallback_builder` aufgerufen, der einen sicheren, garantiert
   gültigen State liefert — LangGraph.invoke() darf niemals mitten im
   Graphen mit einer unbehandelten Exception abbrechen, und der Besucher
   darf niemals eine leere oder kaputte Antwort bekommen, selbst wenn EIN
   Node komplett ausfällt (siehe agents/sdr_agent.py, `_inbound_chat_fallback()`).
"""
from __future__ import annotations

import functools
import logging
import os
import socket
import time
from typing import Any, Callable, Optional, TypeVar

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

# ── 1. Health & Infrastructure Audit ────────────────────────────────────────


class GuardianAgent:
    """Führt den Infrastruktur-Audit aus (GET /api/v1/health/audit, main.py)."""

    # Die 5 BaseAgent-Agenten, die main.py._build_registry() erzeugt —
    # gleiche Liste wie main.py health_check(), hier zusätzlich als
    # Soll-Zustand für den Audit (main.py._AGENT_REGISTRY.keys() ist der
    # Ist-Zustand, siehe check_agent_graph()).
    EXPECTED_AGENTS = frozenset({"onboarding", "operations", "sales-copilot", "sdr", "support"})

    # Die 4 Nodes aus dem 17.09.2026-Refactor (agents/sdr_agent.py,
    # InboundChatGraph) — siehe CLAUDE.md, Abschnitt "4-Node-Refactor".
    EXPECTED_INBOUND_NODES = frozenset(
        {"receptionist_node", "document_node", "appointment_node", "supervisor_node"}
    )

    def __init__(self, voice_agent: Any, netlify_url: Optional[str] = None) -> None:
        # voice_agent wird NUR für dessen bereits vorhandene
        # check_connectivity()-Methode wiederverwendet (kein Duplikat der
        # Anthropic-Egress/Auth-Prüfung) — GuardianAgent hat keine eigene
        # Anthropic-Anbindung.
        self._voice_agent = voice_agent
        self._netlify_url = netlify_url or settings.netlify_site_url

    # ── Einzelprüfungen ───────────────────────────────────────────────────

    def check_anthropic_api(self) -> dict[str, Any]:
        """Wrappt VoiceAgent.check_connectivity() (agents/voice_agent.py) — dieselbe
        DNS/TLS/Auth-Prüfung wie beim Server-Start (main.py lifespan()), hier on-demand."""
        if self._voice_agent is None:
            return {"ok": False, "error": "voice agent not initialised (Startphase noch nicht abgeschlossen)"}
        try:
            return self._voice_agent.check_connectivity()
        except Exception as exc:
            # check_connectivity() selbst fängt bereits alles ab -- dieses
            # try/except ist eine zusätzliche Absicherung gegen einen Fehler
            # IN check_connectivity() selbst (z. B. falls VOICE_AGENT_DLP_REVIEWED
            # den Voice-Agent-Start verweigert hat und self._voice_agent daher
            # in einem unerwarteten Zustand ist).
            return {"ok": False, "error_type": type(exc).__name__, "error": str(exc)[:300]}

    def check_netlify_frontend(self) -> dict[str, Any]:
        """Echter HTTP-GET gegen die Live-Netlify-Website — kein reiner DNS/TCP-Ping,
        weil ein erreichbarer Server trotzdem einen 5xx (Build-Fehler, o. ä.) liefern
        könnte, den ein reiner Verbindungstest nicht erkennen würde."""
        t0 = time.perf_counter()
        try:
            with httpx.Client(timeout=8, follow_redirects=True) as client:
                resp = client.get(self._netlify_url)
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            return {
                "ok": resp.status_code < 500,
                "url": self._netlify_url,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
            }
        except Exception as exc:
            return {
                "ok": False,
                "url": self._netlify_url,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
            }

    def check_railway(self) -> dict[str, Any]:
        """
        "Railway-Konnektivität" heißt hier zweierlei, da wir SELBST innerhalb
        von Railway laufen und keinen Railway-Platform-API-Token besitzen, um
        Railways eigenen Status abzufragen:
        (a) Umgebungs-Erkennung — laufen wir überhaupt auf Railway? (Railway
            setzt automatisch RAILWAY_ENVIRONMENT_NAME/RAILWAY_PROJECT_ID/
            RAILWAY_SERVICE_ID; deren Fehlen ist in lokaler Entwicklung normal,
            kein Fehlerzustand — siehe "ok" unten).
        (b) Egress-Diagnose — DNS + TCP-Connect gegen railway.app selbst,
            dieselbe DNS/TCP-Methodik wie main.py GET /health/egress (dort
            gegen api.anthropic.com), hier gegen Railways eigene Domain, um
            einen ALLGEMEINEN Egress-Ausfall von einem Anthropic-spezifischen
            zu unterscheiden.
        """
        env_present = any(
            os.environ.get(key)
            for key in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
        )
        result: dict[str, Any] = {
            "running_on_railway": env_present,
            "railway_environment": os.environ.get("RAILWAY_ENVIRONMENT_NAME"),
        }

        host = "railway.app"
        t0 = time.perf_counter()
        try:
            infos = socket.getaddrinfo(host, 443)
            addr = infos[0][4]
            sock = socket.socket(infos[0][0], socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect(addr)
            sock.close()
            result["egress_ok"] = True
            result["egress_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as exc:
            result["egress_ok"] = False
            result["egress_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            result["egress_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

        # "ok" hängt bewusst NUR an der Egress-Diagnose, nicht an
        # running_on_railway -- ein lokaler Dev-Lauf ohne Railway-Env-Vars
        # ist kein Infrastruktur-Fehler, ein fehlgeschlagener Egress-Check
        # schon (egal ob lokal oder auf Railway).
        result["ok"] = result["egress_ok"]
        return result

    def check_agent_graph(self, agent_registry: dict[str, Any]) -> dict[str, Any]:
        """
        Strukturelle Validität des Multi-Agenten-Graphen:
        (a) alle 5 Factory-Agenten sind in main.py._AGENT_REGISTRY registriert
            (agent_registry-Parameter, per Dependency-Injection statt Import
            von main.py -- main.py importiert guardian_agent.py, nicht
            umgekehrt, sonst zirkulärer Import).
        (b) InboundChatGraph (agents/sdr_agent.py, Teil des SDR-Agenten)
            exponiert exakt die 4 erwarteten Nodes aus dem
            17.09.2026-Refactor -- über LangGraphs eigene
            CompiledStateGraph.get_graph().nodes-Introspektion, kein
            Duplikat der Graph-Definition.
        """
        registered = set(agent_registry.keys())
        missing_agents = sorted(self.EXPECTED_AGENTS - registered)
        unexpected_agents = sorted(registered - self.EXPECTED_AGENTS)

        inbound_nodes: list[str] = []
        missing_inbound_nodes: list[str] = []
        inbound_error: Optional[str] = None
        try:
            sdr_agent = agent_registry.get("sdr")
            compiled_graph = sdr_agent._inbound._graph  # noqa: SLF001 -- bewusster Introspektions-Zugriff, siehe Docstring
            node_names = set(compiled_graph.get_graph().nodes.keys())
            inbound_nodes = sorted(node_names)
            missing_inbound_nodes = sorted(self.EXPECTED_INBOUND_NODES - node_names)
        except Exception as exc:
            inbound_error = f"{type(exc).__name__}: {str(exc)[:200]}"

        ok = not missing_agents and not missing_inbound_nodes and inbound_error is None
        result: dict[str, Any] = {
            "ok": ok,
            "registered_agents": sorted(registered),
            "missing_agents": missing_agents,
            "inbound_chat_nodes": inbound_nodes,
            "missing_inbound_nodes": missing_inbound_nodes,
        }
        if unexpected_agents:
            # Kein Fehlerzustand (z. B. ein künftiger 6. Agent), aber
            # erwähnenswert -- EXPECTED_AGENTS oben müsste dann nachgezogen werden.
            result["unexpected_agents"] = unexpected_agents
        if inbound_error:
            result["inbound_chat_error"] = inbound_error
        return result

    # ── Gesamt-Audit ──────────────────────────────────────────────────────

    def run_audit(self, agent_registry: dict[str, Any]) -> dict[str, Any]:
        """
        Führt alle vier Prüfungen aus und aggregiert einen Gesamtstatus:
        - "healthy"  -- alle Prüfungen ok.
        - "degraded" -- der Agenten-Graph selbst ist strukturell intakt
                        (die eigentliche Business-Logik funktioniert), aber
                        mindestens eine externe Abhängigkeit (Anthropic,
                        Netlify, Railway-Egress) ist gerade nicht erreichbar.
        - "unhealthy" -- der Agenten-Graph selbst ist beschädigt (fehlender
                        Agent oder fehlender Node) -- das schwerwiegendste
                        Szenario, unabhängig vom Zustand externer Dienste.
        """
        checks = {
            "anthropic_api": self.check_anthropic_api(),
            "netlify_frontend": self.check_netlify_frontend(),
            "railway": self.check_railway(),
            "agent_graph": self.check_agent_graph(agent_registry),
        }

        graph_ok = checks["agent_graph"]["ok"]
        all_ok = all(c.get("ok") for c in checks.values())
        if not graph_ok:
            overall = "unhealthy"
        elif all_ok:
            overall = "healthy"
        else:
            overall = "degraded"

        return {
            "status": overall,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checks": checks,
        }


# ── 2. Self-Healing Middleware ──────────────────────────────────────────────

F = TypeVar("F", bound=Callable[..., dict])

_DEFAULT_MAX_ATTEMPTS = 2   # 1 initialer Versuch + 1 Retry
_DEFAULT_BACKOFF_SECONDS = 0.4  # kurz und fest -- läuft synchron im User-Antwortpfad


def resilient_node(
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
    fallback_builder: Optional[Callable[[dict], dict]] = None,
) -> Callable[[F], F]:
    """
    Decorator-Factory für LangGraph-Node-Methoden mit Signatur
    `(self, state: dict) -> dict`.

    Reversucht bei JEDER Exception bis zu `max_attempts` Mal mit einem
    festen, kurzen Backoff. Schlagen alle Versuche fehl, wird NIE die
    Exception propagiert: `fallback_builder(state)` (falls übergeben)
    liefert einen garantiert gültigen Ersatz-State; ohne `fallback_builder`
    wird der UNVERÄNDERTE Input-State zurückgegeben (reiner Passthrough,
    der betroffene Node wird faktisch zum No-op für diesen Turn).

    Der `fallback_builder` bekommt bewusst den ORIGINALEN Input-State (nicht
    einen etwaigen Teilfortschritt aus einem fehlgeschlagenen Versuch, den
    es bei einer Exception ohnehin nie gibt) -- er soll daraus einen
    sicheren Ersatz bauen, z. B. ein Mindest-final_result, falls der Node,
    der final_result normalerweise befüllt, ausgefallen ist (siehe
    agents/sdr_agent.py, `_inbound_chat_fallback()`).
    """
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(self, state, *args, **kwargs):
            node_name = getattr(func, "__name__", "unknown_node")
            session_id = state.get("session_id", "unknown") if isinstance(state, dict) else "unknown"
            last_exc: Optional[Exception] = None

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(self, state, *args, **kwargs)
                except Exception as exc:
                    last_exc = exc
                    logger.warning(
                        "GuardianAgent: Node '%s' fehlgeschlagen (Versuch %d/%d): %s",
                        node_name, attempt, max_attempts, exc,
                        extra={"session": session_id, "node": node_name, "attempt": attempt},
                    )
                    if attempt < max_attempts:
                        time.sleep(backoff_seconds)

            logger.error(
                "GuardianAgent: Node '%s' nach %d Versuchen weiterhin fehlgeschlagen -- "
                "Self-Healing-Fallback greift, kein unbehandelter Fehler an den Aufrufer",
                node_name, max_attempts,
                extra={"session": session_id, "node": node_name, "error": str(last_exc)},
            )

            if fallback_builder is None:
                return state
            try:
                return fallback_builder(state)
            except Exception as fallback_exc:
                # Letzte Verteidigungslinie: selbst ein kaputter
                # fallback_builder darf niemals eine Exception nach oben
                # durchreichen -- reiner State-Passthrough statt Absturz.
                logger.error(
                    "GuardianAgent: fallback_builder für Node '%s' selbst fehlgeschlagen: %s -- "
                    "reiner State-Passthrough",
                    node_name, fallback_exc, extra={"session": session_id, "node": node_name},
                )
                return state

        return wrapper
    return decorator
