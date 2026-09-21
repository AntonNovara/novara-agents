"""
Basisüberwachung für GET /health (Railway-Deployment novara-agents).

Läuft NICHT im FastAPI-Container selbst -- ein abgestürzter Prozess kann sich
nicht selbst melden. Stattdessen: .github/workflows/uptime-monitor.yml führt
dieses Skript alle 5 Minuten in einem eigenen GitHub-Actions-Runner aus.
Bewusst standalone (kein Import aus core/config.py o. ä.) -- der Runner hat
keine der übrigen Projekt-Abhängigkeiten installiert, nur Python-Stdlib.

Bei Fehlschlag (kein HTTP 200, Timeout, DNS-/Verbindungsfehler) wird eine
E-Mail über dieselbe Gmail-SMTP-Route wie tools/lead_notifier.py verschickt
(SMTP_EMAIL/SMTP_PASSWORD -- hier als GitHub-Actions-Secrets hinterlegt,
nicht aus .env gelesen). Kein Zustand zwischen Läufen (jeder Runner startet
frisch) -- ein anhaltender Ausfall alarmiert dadurch bei JEDEM 5-Minuten-Lauf
erneut, das ist für einen "Basis"-Monitor bewusst in Kauf genommen statt
einer Dedup-/Cooldown-Logik, die einen persistenten Store bräuchte.
"""
from __future__ import annotations

import os
import smtplib
import sys
import urllib.error
import urllib.request
from email.mime.text import MIMEText

HEALTH_URL = os.environ.get("HEALTH_URL", "https://novara-agents-production.up.railway.app/health")
TIMEOUT_SECONDS = 10
RAILWAY_PROJECT_URL = "https://railway.com/project/3ce5eaba-7972-4bfe-876a-95ce591315db"


def check() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read(500).decode("utf-8", errors="replace")
            if resp.status == 200:
                return True, body
            return False, f"HTTP {resp.status}: {body}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}"
    except Exception as exc:  # DNS, timeout, connection refused, ...
        return False, f"{type(exc).__name__}: {exc}"


def send_alert(detail: str) -> None:
    smtp_email = os.environ["SMTP_EMAIL"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    alert_to = os.environ.get("ALERT_TO", smtp_email)

    msg = MIMEText(
        "El healthcheck de novara-agents en Railway no responde correctamente.\n\n"
        f"URL: {HEALTH_URL}\n"
        f"Detalle: {detail}\n\n"
        f"Panel de Railway: {RAILWAY_PROJECT_URL}\n\n"
        "Este correo se genera automáticamente cada 5 minutos mientras el "
        "healthcheck siga fallando (sin agrupar reintentos)."
    )
    msg["Subject"] = "🔴 novara-agents (Railway) no responde"
    msg["From"] = smtp_email
    msg["To"] = alert_to

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=TIMEOUT_SECONDS) as server:
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.send_message(msg)


def main() -> int:
    ok, detail = check()
    if ok:
        print(f"OK: {detail}")
        return 0

    print(f"FAIL: {detail}", file=sys.stderr)
    try:
        send_alert(detail)
        print("Alert email sent.")
    except Exception as exc:
        # Der Healthcheck-Fehlschlag selbst ist der wichtigere Signalpunkt --
        # ein zusätzlicher SMTP-Fehler soll den ohnehin fehlgeschlagenen
        # Workflow-Run nicht verschleiern, nur zusätzlich sichtbar sein.
        print(f"ALSO FAILED to send alert email: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
