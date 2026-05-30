FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (no .env – secrets come from Railway env vars)
COPY agents/ agents/
COPY core/ core/
COPY tools/ tools/
COPY main.py .
COPY novara_wissen.txt .

# Railway injects $PORT at runtime
ENV PORT=8000

EXPOSE $PORT

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
