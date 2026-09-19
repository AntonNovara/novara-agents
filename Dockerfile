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

# Railway injects $PORT at runtime
ENV PORT=8000

EXPOSE $PORT

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
