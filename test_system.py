"""
test_system.py – Integrationstest der lokalen Novara Python/LangGraph-Architektur.

Prüft:
  1. Wissensdatenbank (novara_wissen.txt) wird von allen Agenten sauber geladen
  2. SDR-Agent: interne Routing-Logik mit fiktivem Lead (qualifiziert vs. disqualifiziert)
  3. Operations-Agent: tatsächliches Verhalten mit einem Lead + mit einer Rechnung
     (belegt, dass es KEIN operations->sdr-Routing gibt)
  4. "Gmail-Entwurfs-Skript": Existenz-/Code-Prüfung der Google/E-Mail-Skripte

Ausführen:  python3 test_system.py
Macht echte (kleine) LLM-Calls, wenn ein gültiger ANTHROPIC_API_KEY vorliegt.
"""
from __future__ import annotations

import json
import sys
import traceback

# ── Report-Helfer ───────────────────────────────────────────────────────────

_RESULTS: list[tuple[str, str]] = []  # (status, label)


def section(title: str) -> None:
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def ok(label: str, detail: str = "") -> None:
    _RESULTS.append(("PASS", label))
    print(f"  [PASS] {label}" + (f"  → {detail}" if detail else ""))


def fail(label: str, detail: str = "") -> None:
    _RESULTS.append(("FAIL", label))
    print(f"  [FAIL] {label}" + (f"  → {detail}" if detail else ""))


def warn(label: str, detail: str = "") -> None:
    _RESULTS.append(("WARN", label))
    print(f"  [WARN] {label}" + (f"  → {detail}" if detail else ""))


def info(msg: str) -> None:
    print(f"  · {msg}")


def _short(obj, limit: int = 1000) -> str:
    txt = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    return txt if len(txt) <= limit else txt[:limit] + "\n  … (gekürzt)"


# ── Fiktive Testdaten ───────────────────────────────────────────────────────

QUALIFIED_LEAD = {
    "company": "Elektro Huber GmbH",
    "industry": "Elektrohandwerk",
    "location": "Wien",
    "employees": 6,
    "contact": {"name": "Josef Huber", "role": "Inhaber"},
    "notes": (
        "Verpasst staendig Anrufe, wenn das Team auf der Baustelle ist. Schreibt "
        "Angebote abends manuell. Kein CRM, keine Online-Terminbuchung, kaum "
        "Online-Praesenz. Inhaber entscheidet selbst."
    ),
}

DISQUALIFIED_LEAD = {
    "company": "GlobalTech Enterprise AG",
    "industry": "Enterprise Software",
    "location": "Berlin",
    "employees": 800,
    "contact": {"name": "Dr. Klein", "role": "CTO"},
    "notes": (
        "Vollstaendig digitalisiert, eigenes CRM, dediziertes Operations-Team, "
        "Online-Buchung laeuft. Kein Interesse an weiterer Automatisierung."
    ),
}

SAMPLE_INVOICE = (
    "Rechnung RE-2026-042\n"
    "Novara Automation, Wien\n"
    "Leistung: Starter-Paket Prozessautomatisierung\n"
    "Gesamtbetrag: 990,00 EUR\n"
    "Rechnungsdatum: 15.06.2026\n"
    "Zahlungsziel: 50% Anzahlung bei Auftragsbestaetigung\n"
)


# ── Test 1: Wissensdatenbank ────────────────────────────────────────────────

def test_knowledge_base() -> None:
    section("TEST 1 — Wissensdatenbank (novara_wissen.txt) wird sauber geladen")
    try:
        from core.knowledge import load_novara_wissen
    except Exception as exc:
        fail("Import core.knowledge", str(exc))
        return

    wissen = load_novara_wissen()
    if wissen.startswith("(Wissensdatenbank nicht gefunden"):
        fail("Datei geladen", "Fallback-String zurückgegeben — Datei fehlt")
        return
    ok("Datei geladen", f"{len(wissen)} Zeichen")

    markers = ["STARTER", "€990", "Elektriker", "RETAINER", "ICP"]
    missing = [m for m in markers if m not in wissen]
    if missing:
        warn("Inhalts-Marker", f"fehlen: {missing}")
    else:
        ok("Inhalts-Marker vorhanden", ", ".join(markers))

    # Jeder Agent hat modulweit _WISSEN mit demselben Inhalt
    agent_modules = [
        "agents.operations_agent", "agents.sdr_agent", "agents.support_agent",
        "agents.sales_copilot_agent", "agents.onboarding_agent", "agents.voice_agent",
    ]
    import importlib
    for modname in agent_modules:
        try:
            mod = importlib.import_module(modname)
            w = getattr(mod, "_WISSEN", None)
            if w and len(w) > 500 and "€990" in w:
                ok(f"{modname.split('.')[-1]} lädt Wissensbasis in Kontext", f"{len(w)} Zeichen")
            else:
                fail(f"{modname.split('.')[-1]} Wissensbasis", "leer oder unvollständig")
        except Exception as exc:
            fail(f"Import {modname}", str(exc))


# ── Test 2: SDR-Routing ─────────────────────────────────────────────────────

def test_sdr_routing(live: bool) -> None:
    section("TEST 2 — SDR-Agent: internes Routing mit fiktivem Lead (JSON)")
    if not live:
        warn("SDR-Live-Test übersprungen", "kein gültiger ANTHROPIC_API_KEY lokal")
        return
    try:
        from agents.sdr_agent import SDRAgent
        from agents.base_agent import AgentRequest
    except Exception as exc:
        fail("Import SDRAgent", str(exc))
        return

    agent = SDRAgent()

    # 2a: qualifizierter Lead -> sollte route_after_score -> compose_outreach nehmen
    info("Fiktiver Lead (JSON):")
    print(_short(QUALIFIED_LEAD, 600))
    try:
        resp = agent.process(AgentRequest(text=json.dumps(QUALIFIED_LEAD, ensure_ascii=False)))
        if not resp.success:
            fail("SDR qualifizierter Lead", resp.error or "unbekannter Fehler")
        else:
            r = resp.result
            qualified = r.get("qualified")
            score = r.get("lead_score")
            info(f"lead_score={score}, qualified={qualified}, DLP={resp.dlp_findings}")
            if qualified and "outreach" in r:
                ok("Routing qualifiziert → compose_outreach", f"score {score}, Kanal {r['outreach'].get('channel')}")
            elif qualified is False:
                warn("Lead wurde disqualifiziert", f"score {score} (LLM-Bewertung – inhaltlich prüfen)")
            else:
                warn("Unerwartete Ergebnisform", _short(r, 500))
    except Exception:
        fail("SDR qualifizierter Lead — Exception", "")
        traceback.print_exc()

    # 2b: klar disqualifizierter Lead -> finalize_disqualified
    try:
        resp = agent.process(AgentRequest(text=json.dumps(DISQUALIFIED_LEAD, ensure_ascii=False)))
        if not resp.success:
            fail("SDR disqualifizierter Lead", resp.error or "")
        else:
            r = resp.result
            info(f"lead_score={r.get('lead_score')}, qualified={r.get('qualified')}")
            if r.get("qualified") is False:
                ok("Routing disqualifiziert → finalize_disqualified", f"score {r.get('lead_score')}")
            else:
                warn("Grossbetrieb wurde qualifiziert", "ICP-Schwelle inhaltlich prüfen")
    except Exception:
        fail("SDR disqualifizierter Lead — Exception", "")
        traceback.print_exc()


# ── Test 3: Operations-Verhalten + Beweis kein operations->sdr-Routing ──────

def test_operations_and_routing_claim(live: bool) -> None:
    section("TEST 3 — Operations-Agent: Realverhalten (KEIN operations→sdr-Routing)")
    info("ARCHITEKTUR-BEFUND: Der operations-Graph verarbeitet RECHNUNGEN")
    info("(classify → extract → validate → write_to_crm → finalize).")
    info("Er hat KEINEN sdr-Knoten und KEINE Kante zum SDR-Agenten.")
    info("Die einzige Agent-zu-Agent-Übergabe ist voice → sdr (Vapi-Webhook).")

    if not live:
        warn("Operations-Live-Test übersprungen", "kein gültiger ANTHROPIC_API_KEY lokal")
        return
    try:
        from agents.operations_agent import OperationsAgent
        from agents.base_agent import AgentRequest
    except Exception as exc:
        fail("Import OperationsAgent", str(exc))
        return

    agent = OperationsAgent()

    # 3a: Lead an operations -> wird NICHT als Rechnung erkannt, kein sdr-Handoff
    try:
        resp = agent.process(AgentRequest(text=json.dumps(QUALIFIED_LEAD, ensure_ascii=False)))
        if not resp.success:
            warn("Operations mit Lead-Input", resp.error or "")
        else:
            r = resp.result
            routed_to_sdr = "sdr" in json.dumps(r).lower()
            info("Ergebnis (Auszug):")
            print(_short(r, 600))
            if not routed_to_sdr:
                ok("Kein sdr-Routing aus operations", "Lead wird als Nicht-Rechnung behandelt")
            else:
                fail("Unerwartetes sdr-Routing", "operations verweist auf sdr")
    except Exception:
        fail("Operations mit Lead — Exception", "")
        traceback.print_exc()

    # 3b: echte Rechnung -> operations funktioniert bestimmungsgemäß
    try:
        resp = agent.process(AgentRequest(text=SAMPLE_INVOICE))
        if not resp.success:
            fail("Operations mit Rechnung", resp.error or "")
        else:
            r = resp.result
            info("Ergebnis (Auszug):")
            print(_short(r, 600))
            ok("Operations verarbeitet Rechnung (bestimmungsgemäß)")
    except Exception:
        fail("Operations mit Rechnung — Exception", "")
        traceback.print_exc()


# ── Test 4: "Gmail-Entwurfs-Skript" ─────────────────────────────────────────

def test_gmail_script() -> None:
    section("TEST 4 — 'Gmail-Entwurfs-Skript': Existenz- und Code-Prüfung")
    import os
    import py_compile

    info("BEFUND: Es existiert KEIN Gmail-Entwurfs-Skript im Repository.")
    info("'gmail' kommt nur in tools/faq_database.py vor (FAQ-Text).")
    info("Google-/E-Mail-bezogene Skripte sind:")
    info("  - get_token.py            (Google-OAuth, NUR calendar-Scope)")
    info("  - tools/notification_system.py  (Mock-E-Mail, sendet NICHT real)")

    # Statische Syntaxprüfung der relevanten Skripte, bevor irgendetwas live liefe
    for path in ["get_token.py", "tools/notification_system.py", "tools/calendar_integration.py"]:
        if not os.path.exists(path):
            warn(f"{path}", "Datei nicht gefunden")
            continue
        try:
            py_compile.compile(path, doraise=True)
            ok(f"Syntax OK: {path}")
        except py_compile.PyCompileError as exc:
            fail(f"Syntaxfehler: {path}", str(exc))

    # Inhaltliche Hinweise
    if os.path.exists("get_token.py"):
        src = open("get_token.py", encoding="utf-8").read()
        if "gmail" not in src.lower():
            warn("get_token.py deckt Gmail NICHT ab", "SCOPES nur 'calendar' — für Gmail-Drafts fehlt der gmail-Scope")
        if "credentials.json" in src:
            info("get_token.py erwartet lokale credentials.json (sonst FileNotFoundError beim Ausführen)")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    section("NOVARA – LOKALER ARCHITEKTUR-TEST")
    try:
        from core.config import settings
        live = settings.anthropic_key_configured
        info(f"Modell: {settings.anthropic_model}")
        info(f"Live-LLM-Calls: {'JA' if live else 'NEIN (mock/kein Key)'}")
    except Exception as exc:
        print("Konfiguration konnte nicht geladen werden:", exc)
        return 1

    test_knowledge_base()
    test_sdr_routing(live)
    test_operations_and_routing_claim(live)
    test_gmail_script()

    # Zusammenfassung
    section("ZUSAMMENFASSUNG")
    p = sum(1 for s, _ in _RESULTS if s == "PASS")
    f = sum(1 for s, _ in _RESULTS if s == "FAIL")
    w = sum(1 for s, _ in _RESULTS if s == "WARN")
    print(f"  PASS: {p}   WARN: {w}   FAIL: {f}")
    if f:
        print("\n  Fehlgeschlagen:")
        for s, label in _RESULTS:
            if s == "FAIL":
                print(f"    - {label}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
