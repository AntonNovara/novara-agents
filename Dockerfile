FROM python:3.11-slim

WORKDIR /app

# ffmpeg: pydub (Baustellen-Voice-Assistant, utils/pdf_generator.py-Nachbar
# agents/field_worker_agent.py) braucht es zur Laufzeit, um WhatsApp-
# Sprachnachrichten (i. d. R. OGG/Opus) zu dekodieren/konvertieren -- pydub
# selbst ist reines Python und ruft ffmpeg als externes Binary auf, das pip
# nicht mitliefert.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (no .env – secrets come from Railway env vars)
COPY agents/ agents/
COPY core/ core/
COPY tools/ tools/
COPY utils/ utils/
COPY static/ static/
COPY main.py .
COPY novara_wissen.txt .
# clients/*.json (Pilotkunden-Betriebsprofile, core/client_profiles.py) --
# FEHLTE hier bisher komplett: main.py::_build_client_agents() und
# agents/quote_agent.py::load_client_profile() lasen also in Produktion
# NIE ein echtes Profil, sondern immer nur "Datei nicht gefunden" -- der
# Docker-Build-Kontext hatte den ganzen Ordner nie gesehen. Gefunden beim
# ersten echten End-to-End-Test von POST /api/v1/tools/quote-draft gegen
# Produktion (404 "Kein Betriebsprofil", obwohl die Datei im Repo lag und
# gepusht war). "clients=[] in boot logs" aus einer früheren Session war
# also NIE ein Beleg dafür, dass das Pilotprogramm korrekt mit null echten
# Profilen lief -- das Verhalten wäre mit und ohne diesen Fix identisch
# gewesen, solange kein echtes Profil existierte.
COPY clients/ clients/

# Railway injects $PORT at runtime
ENV PORT=8000

EXPOSE $PORT

# --proxy-headers/--forwarded-allow-ips=*: Railway terminiert TLS an einem
# vorgeschalteten Proxy -- ohne das trüge request.client.host überall (u. a.
# im neuen Rate-Limiting auf POST /api/v1/chat/landing, main.py) die interne
# Proxy-IP statt der echten Besucher-IP. Railway ist der einzige Hop vor
# diesem Container, daher ist ein pauschales "*" hier vertretbar (kein
# öffentlich erreichbarer Multi-Tenant-Proxy dazwischen).
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'"]
