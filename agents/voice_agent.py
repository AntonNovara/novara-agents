"""
VoiceAgent – Telefonischer First-Responder "Novara".

Streaming-Endpunkt für Vapi Custom LLM.
Gibt OpenAI-kompatible SSE-Chunks zurück.
Keine JSON-Ausgabe — konversationelle, kurze Sätze für TTS geeignet.
"""
import json
import time
import uuid
from typing import Iterator, Any

import anthropic
import httpx
import structlog
from anthropic import DefaultHttpxClient

from core.config import settings
from core.knowledge import load_novara_wissen

log = structlog.get_logger("novara.voice")

_WISSEN = load_novara_wissen()

_SYSTEM_PROMPT = f"""\
Du bist Novara, der freundliche digitale Assistent von Novara Automation Wien.
Du nimmst eingehende Anrufe entgegen und sprichst Österreichisch/Deutsch.

=== NOVARA WISSENSDATENBANK ===
{_WISSEN}
=== ENDE ===

GESPRÄCHSREGELN (STRIKT EINHALTEN):
1. Antworten KURZ — maximal 2 Sätze. Das ist ein Telefonat, kein Essay.
2. Stelle immer nur EINE Frage auf einmal.
3. Neukunde qualifizieren in dieser Reihenfolge: Name → Firma → Mitarbeiterzahl → größtes Problem.
4. Bestandskunde mit Problem? Beantworte aus der Wissensdatenbank oder biete Rückruf an.
5. Terminwunsch? Sage: "Ich schicke Ihnen gleich den Buchungslink per SMS."
6. Preise ERST nennen wenn Qualifizierung abgeschlossen (Firma + Mitarbeiterzahl bekannt).
7. KEIN Technik-Jargon: kein "KI", kein "Automatisierungssoftware", kein "LangGraph".
8. Ton: freundlich, direkt, kompetent — wie ein Mensch am Telefon.
9. Abschluss: "Vielen Dank für Ihren Anruf. Ich leite alles weiter und Sie hören bald von uns."

INTENT-ERKENNUNG:
- Interesse / erstes Mal → Neukunde qualifizieren
- "Ich bin bereits Kunde" / Problem schildern → Support-Modus
- Termin buchen → Buchungslink per SMS ankündigen
- Unklar → Frage: "Sind Sie bereits Kunde bei uns oder rufen Sie zum ersten Mal an?"
"""


class VoiceAgent:
    """Streaming-fähiger Konversationsagent für Telefongespräche via Vapi."""

    def __init__(self) -> None:
        # IPv4 erzwingen: local_address="0.0.0.0" bindet den lokalen Socket an
        # eine IPv4-Adresse, sodass die Verbindung NICHT ueber IPv6 laeuft.
        # Das behebt den APIConnectionError auf Railway, wo der Container einen
        # AAAA-Record aufloest und der IPv6-Egress ins Leere laeuft.
        # http2=False erzwingt zugleich HTTP/1.1 (verhindert Stream-Saettigung),
        # retries=2 faengt transiente Verbindungsabbrueche ab. Der Transport
        # traegt http2/IPv4; das 30s-Timeout sitzt auf dem Anthropic-Client.
        # Synchroner Client -> httpx.HTTPTransport/Client, NICHT AsyncClient.
        transport = httpx.HTTPTransport(
            local_address="0.0.0.0",
            http2=False,
            retries=2,
        )
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=30.0,
            http_client=DefaultHttpxClient(transport=transport),
        )

    def check_connectivity(self) -> dict[str, Any]:
        """Leichter Egress-/Auth-Test gegen die Anthropic-API (kein Token-Verbrauch).

        models.list prueft DNS, TLS, Egress und API-Key in einem einzigen GET.
        Faengt alle Fehler ab und gibt error_type/error strukturiert zurueck,
        damit ein APIConnectionError (Egress) von einem AuthenticationError klar
        unterscheidbar ist.
        """
        t0 = time.perf_counter()
        try:
            models = self._client.models.list(limit=1)
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "model_sample": models.data[0].id if models.data else None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }

    def complete(self, messages: list[dict]) -> dict[str, Any]:
        """Nicht-streamende Antwort im OpenAI chat.completion Format (für stream=false)."""
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = settings.anthropic_model

        anthropic_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        try:
            response = self._client.messages.create(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=anthropic_messages,
                max_tokens=300,
            )
            content = response.content[0].text if response.content else ""
        except Exception as exc:
            log.error(
                "Anthropic complete() fehlgeschlagen – Fallback ausgegeben",
                model=model,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            content = "Entschuldigung, da ist kurz etwas schiefgelaufen. Können Sie das bitte wiederholen?"

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def stream(self, messages: list[dict]) -> Iterator[str]:
        """
        Gibt OpenAI-kompatible SSE-Chunks zurück.
        Vapi erwartet exakt dieses Format beim Custom LLM Endpunkt.
        """
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())
        model = settings.anthropic_model

        anthropic_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        # Erstes Chunk: role
        yield _sse_chunk(completion_id, created, model, {"role": "assistant"}, None)

        try:
            with self._client.messages.stream(
                model=model,
                system=_SYSTEM_PROMPT,
                messages=anthropic_messages,
                max_tokens=300,
            ) as stream:
                for text in stream.text_stream:
                    yield _sse_chunk(completion_id, created, model, {"content": text}, None)
        except Exception as exc:
            log.error(
                "Anthropic stream() fehlgeschlagen – Fallback ausgegeben",
                model=model,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            fallback = "Entschuldigung, da ist kurz etwas schiefgelaufen. Können Sie das bitte wiederholen?"
            yield _sse_chunk(completion_id, created, model, {"content": fallback}, None)

        # Letzter Chunk: finish_reason
        yield _sse_chunk(completion_id, created, model, {}, "stop")
        yield "data: [DONE]\n\n"


def _sse_chunk(
    completion_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason,
) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"
