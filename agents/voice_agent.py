"""
VoiceAgent – Telefonischer First-Responder "Novara".

Streaming-Endpunkt für Vapi Custom LLM.
Gibt OpenAI-kompatible SSE-Chunks zurück.
Keine JSON-Ausgabe — konversationelle, kurze Sätze für TTS geeignet.
"""
import json
import socket as _socket
import time
import uuid
from typing import Any, Iterator, Optional

import anthropic
import httpx
import structlog
from anthropic import DefaultHttpxClient

from core.config import settings
from core.knowledge import load_novara_wissen
from core.security import SecurityLayer

# Force IPv4 DNS for api.anthropic.com — Railway IPv6 egress fails silently.
# httpx.HTTPTransport(local_address="0.0.0.0") was unreliable: httpcore creates
# an AF_INET6 socket when AAAA comes first in DNS, then the IPv4 bind fails
# without falling through to IPv4. Patching getaddrinfo before socket creation
# ensures we only ever attempt IPv4 connections to Anthropic's API.
_orig_getaddrinfo = _socket.getaddrinfo

def _anthropic_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    if host and "anthropic.com" in str(host) and family == 0:
        try:
            results = _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
            if results:
                return results
        except _socket.gaierror:
            pass
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

_socket.getaddrinfo = _anthropic_ipv4_only

log = structlog.get_logger("novara.voice")

_WISSEN = load_novara_wissen()

# EU AI Act Art. 50 – Transparenzpflicht (in Kraft seit 2. August 2026): wer
# mit einem KI-System interagiert, muss das erkennen können, sofern es nicht
# offensichtlich ist — gilt für ein Telefonat genauso wie für eine
# geschriebene Nachricht. Gleiche Konstante/Formulierung wie in
# agents/sdr_agent.py, hier bewusst dupliziert statt importiert: voice_agent.py
# bleibt absichtlich unabhängig von agents/sdr_agent.py, die einzige
# bestehende Kopplung läuft über main.py (Webhook nach Gesprächsende ruft
# sdr.process() auf), nie direkt zwischen den beiden Agenten-Modulen.
#
# Konsens/Opt-out (core/consent.py, Kanal "voice"): dieser Agent nimmt nur
# EINGEHENDE Anrufe entgegen — er initiiert kein Outbound-Telefonat, das vor
# dem Wählen gegen ein Opt-out geprüft werden müsste (ein Anrufer, der selbst
# anruft, hat den Kontakt hergestellt). Die tatsächliche Outreach-Aktion, die
# aus einem Anruf entstehen kann — die Follow-up-E-Mail/LinkedIn-Nachricht,
# die main.py nach Gesprächsende über sdr.process() erzeugen lässt — läuft
# bereits durch SDRGraph.check_consent() (agents/sdr_agent.py). Der Kanal
# "voice" im Ledger ist vorbereitet für den Tag, an dem Novara selbst
# ausgehend anruft (siehe Roadmap) — dann ist HIER der richtige Ort für eine
# is_allowed()-Prüfung vor dem Wählen.
AI_DISCLOSURE_DE = (
    "Hinweis: Diese Nachricht/dieser Anruf wird von einem "
    "KI-System im Auftrag von {client_name} erstellt."
)
_CLIENT_NAME = "Novara Automation"

_SYSTEM_PROMPT = f"""\
Du bist Novara, der freundliche digitale Assistent von Novara Automation Wien
und agierst als erfahrener SDR (Sales Development Representative).
Du nimmst eingehende Anrufe entgegen und sprichst Österreichisch/Deutsch.

PFLICHT-OFFENLEGUNG (EU AI Act Art. 50, in Kraft seit 2. August 2026):
Zu Beginn JEDES Gesprächs musst du sinngemäß offenlegen, dass du ein
KI-System bist, bevor du mit der eigentlichen Qualifizierung beginnst —
zum Beispiel: "{AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)}"
(natürlich in gesprochener, kurzer Form, nicht als vorgelesener Rechtstext).

=== NOVARA WISSENSDATENBANK ===
{_WISSEN}
=== ENDE ===

GESPRÄCHSREGELN (STRIKT EINHALTEN):
1. Antworten KURZ — maximal 2 Sätze. Das ist ein Telefonat, kein Essay.
2. Stelle immer nur EINE Frage auf einmal.
3. Neukunde qualifizieren in dieser Reihenfolge: Name → Firma → Mitarbeiterzahl
   (ODER, falls das natürlicher ins Gespräch passt, der größte aktuelle
   Engpass) → größtes Problem.
4. Bestandskunde mit Problem? Beantworte aus der Wissensdatenbank oder biete Rückruf an.
5. Terminwunsch? Sage: "Ich schicke Ihnen gleich den Buchungslink per SMS."
6. Preise ERST nennen wenn Qualifizierung abgeschlossen (Firma + Mitarbeiterzahl bekannt).
7. KEIN Technik-Jargon: kein "KI", kein "Automatisierungssoftware", kein "LangGraph"
   (Ausnahme: die Pflicht-Offenlegung oben zu Gesprächsbeginn).
8. Ton: freundlich, direkt, kompetent — wie ein Mensch am Telefon.
9. Abschluss: "Vielen Dank für Ihren Anruf. Ich leite alles weiter und Sie hören bald von uns."
10. Dein Endziel in JEDEM qualifizierten Gespräch: zum Buchungslink führen
    (Regel 5) — biete den Termin aktiv an, sobald Firma/Engpass bekannt sind,
    statt nur zu warten, bis der Anrufer selbst danach fragt.

EINWANDBEHANDLUNG (kurz, in maximal 2 Sätzen, wie im restlichen Gespräch):
- Einwand "zu teuer" / Preis zu hoch: Lenke auf ROI, eingesparte Zeit und den
  Charakter als Investition statt Ausgabe — nur Zahlen aus der
  Wissensdatenbank, nichts erfinden.
- Einwand "KI ist zu kompliziert" / keine technischen Kenntnisse: Betone,
  dass Novara "Done-for-you" ist — zu 100% von uns umgesetzt, keinerlei
  IT-Kenntnisse beim Kunden nötig.
- Andere Einwände: kurz ernst nehmen, dann sanft zur nächsten
  Qualifizierungsfrage oder zum Terminvorschlag überleiten.

INTENT-ERKENNUNG:
- Interesse / erstes Mal → Neukunde qualifizieren
- "Ich bin bereits Kunde" / Problem schildern → Support-Modus
- Termin buchen → Buchungslink per SMS ankündigen
- Unklar → Frage: "Sind Sie bereits Kunde bei uns oder rufen Sie zum ersten Mal an?"
"""


def _is_first_turn(anthropic_messages: list[dict]) -> bool:
    """True, wenn im bisherigen Gesprächsverlauf noch keine Assistant-Antwort vorkam."""
    return not any(m["role"] == "assistant" for m in anthropic_messages)


def _with_disclosure_prefix(content: str) -> str:
    """
    Stellt die Pflicht-Offenlegung (Art. 50) dem ersten Satz des Gesprächs
    deterministisch voran — nicht nur per Prompt-Instruktion. Gleiche
    Philosophie wie der Credential-Hard-Block in core/security.py: eine
    System-Prompt-Regel ist eine Empfehlung ans LLM, kein Garant. Wird nur
    beim ERSTEN Turn eines Gesprächs aufgerufen (siehe _is_first_turn).
    """
    disclosure = AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)
    return f"{disclosure} {content}" if content else disclosure


# DLP für den Live-Gesprächspfad (schließt den in CLAUDE.md dokumentierten
# kritischen Gap "Der Live-Gesprächspfad hat KEINE DLP-Schicht"). Vapi
# übernimmt Speech-to-Text selbst -- was hier als `content` einer
# User-Message ankommt, ist bereits Text, kein Audio. "Audio-zu-Text durch
# die DLP-Filter schicken" heißt daher konkret: JEDE transkribierte
# User-Äußerung läuft durch SecurityLayer.check_and_redact(), BEVOR sie ins
# Anthropic-Prompt einfließt -- exakt dieselbe Prüfung, die BaseAgent.process()
# für alle textbasierten Agenten schon immer vor jedem LLM-Aufruf durchführt.
_VOICE_BLOCKED_FALLBACK_DE = (
    "Entschuldigung, das kann ich am Telefon nicht besprechen. "
    "Wie kann ich Ihnen sonst weiterhelfen?"
)


def _sanitize_conversation(anthropic_messages: list[dict]) -> tuple[list[dict], Optional[str]]:
    """
    Wendet SecurityLayer.check_and_redact() auf jede User-Nachricht an.

    Kontaktdaten (E-Mail/Telefon) bleiben wie überall im System lesbar,
    sensible Daten (IBAN, Steuernummer, ...) werden redigiert -- der Text,
    den das LLM sieht, ist also NIE der unveränderte Rohtext des Anrufers.

    Ein Hard-Block-Treffer (Credential-Leak oder Rollenumdefinitions-Versuch
    mit KI-Identitäts-/Verneinungs-Cue, siehe core/security.py) stoppt den
    LLM-Aufruf für diesen Turn KOMPLETT -- die zweite Rückgabe ist dann der
    `blocked_reason`, und der Aufrufer (complete()/stream()) darf die
    Original-Nachricht nie ans Modell weiterreichen, sondern muss stattdessen
    _VOICE_BLOCKED_FALLBACK_DE ausgeben. Läuft über die gesamte bisherige
    Historie, nicht nur die neueste Nachricht -- billige Regex-Prüfung,
    Verteidigung in der Tiefe falls eine frühere Nachricht aus irgendeinem
    Grund ungefiltert im Verlauf gelandet wäre.
    """
    sanitized: list[dict] = []
    for m in anthropic_messages:
        if m["role"] != "user":
            sanitized.append(m)
            continue
        dlp = SecurityLayer.check_and_redact(m["content"])
        if not dlp.approved:
            log.warning(
                "Voice-Turn durch DLP blockiert",
                blocked_reason=dlp.blocked_reason,
            )
            return sanitized, dlp.blocked_reason
        if dlp.findings:
            log.info("Voice-Turn: PII redigiert", findings=dlp.findings)
        sanitized.append({"role": m["role"], "content": dlp.redacted_text})
    return sanitized, None


class VoiceAgent:
    """Streaming-fähiger Konversationsagent für Telefongespräche via Vapi."""

    def __init__(self) -> None:
        # Sicherheits-Gate, KEINE optionale Warnung: dieser Agent hat während
        # des laufenden Live-Gesprächs keine DLP-Schicht (kein PII-Schutz,
        # kein Prompt-Injection-Schutz -- siehe CLAUDE.md, Abschnitt "Voice
        # Agent"). Eine reine Log-Warnung würde in einem Railway-Logstream
        # untergehen; eine dokumentierte Notiz in CLAUDE.md hilft nichts, wenn
        # niemand sie vor dem Redeploy liest. Deshalb hart am Start verweigern,
        # bis das bewusst bestätigt wurde -- eine versehentliche oder
        # kontextlose Reaktivierung des Voice-Service (z. B. durch simples
        # Wieder-Anschalten auf Railway) darf nicht stillschweigend wieder live
        # gehen.
        if not settings.voice_agent_dlp_reviewed:
            raise RuntimeError(
                "VoiceAgent-Start verweigert: VOICE_AGENT_DLP_REVIEWED ist nicht "
                "gesetzt (oder nicht 'true').\n"
                "Dieser Voice-Agent hat WÄHREND des laufenden Live-Telefongesprächs "
                "KEINE DLP-Schicht -- kein PII-Schutz, kein Prompt-Injection-Schutz. "
                "Siehe CLAUDE.md, Abschnitt 'Voice Agent (agents/voice_agent.py)' "
                "für die vollständige Erklärung dieser Lücke, bevor dieser Service "
                "(erneut) live geschaltet wird.\n"
                "Nach bewusster Prüfung/Entscheidung explizit freischalten: "
                "Environment-Variable VOICE_AGENT_DLP_REVIEWED=true setzen."
            )

        # IPv4 erzwingen: local_address="0.0.0.0" bindet den lokalen Socket an
        # eine IPv4-Adresse, sodass die Verbindung NICHT ueber IPv6 laeuft.
        # Das behebt den APIConnectionError auf Railway, wo der Container einen
        # AAAA-Record aufloest und der IPv6-Egress ins Leere laeuft.
        # http2=False erzwingt zugleich HTTP/1.1 (verhindert Stream-Saettigung),
        # retries=2 faengt transiente Verbindungsabbrueche ab. Der Transport
        # traegt http2/IPv4; das 30s-Timeout sitzt auf dem Anthropic-Client.
        # Synchroner Client -> httpx.HTTPTransport/Client, NICHT AsyncClient.
        transport = httpx.HTTPTransport(
            http2=False,  # HTTP/1.1 prevents stream saturation on PaaS
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
        Gibt die vollstaendige Exception-Kette zurueck, damit der Root-Cause
        (z. B. socket.gaierror vs. ConnectionRefusedError) sichtbar ist.
        """
        t0 = time.perf_counter()
        try:
            models = self._client.models.list(limit=1)
            return {
                "ok": True,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "model_sample": models.data[0].id if models.data else None,
                "base_url": str(self._client.base_url),
            }
        except Exception as exc:
            cause = exc.__cause__
            cause2 = getattr(cause, "__cause__", None)
            return {
                "ok": False,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "error_type": type(exc).__name__,
                "error": str(exc)[:300],
                "cause_type": type(cause).__name__ if cause else None,
                "cause": str(cause)[:300] if cause else None,
                "cause2_type": type(cause2).__name__ if cause2 else None,
                "cause2": str(cause2)[:300] if cause2 else None,
                "base_url": str(self._client.base_url),
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
        first_turn = _is_first_turn(anthropic_messages)
        anthropic_messages, blocked_reason = _sanitize_conversation(anthropic_messages)

        if blocked_reason:
            content = _VOICE_BLOCKED_FALLBACK_DE
        else:
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

        if first_turn:
            content = _with_disclosure_prefix(content)

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
        first_turn = _is_first_turn(anthropic_messages)
        anthropic_messages, blocked_reason = _sanitize_conversation(anthropic_messages)

        # Erstes Chunk: role
        yield _sse_chunk(completion_id, created, model, {"role": "assistant"}, None)

        # Pflicht-Offenlegung deterministisch VOR dem ersten LLM-Token dieses
        # Anrufs ausgeben — siehe _with_disclosure_prefix()-Docstring.
        if first_turn:
            disclosure = AI_DISCLOSURE_DE.format(client_name=_CLIENT_NAME)
            yield _sse_chunk(completion_id, created, model, {"content": f"{disclosure} "}, None)

        if blocked_reason:
            # Kein LLM-Aufruf mit dem blockierten Rohtext -- deterministische
            # Ausweich-Antwort statt dass der Versuch je das Modell erreicht.
            yield _sse_chunk(
                completion_id, created, model, {"content": _VOICE_BLOCKED_FALLBACK_DE}, None
            )
        else:
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
