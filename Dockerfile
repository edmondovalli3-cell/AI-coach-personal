FROM python:3.12-slim

# Pacchetti di sistema necessari per curl_cffi (richiesto da garminconnect)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

# Install Python dependencies
COPY app/requirements.txt /code/app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /code/app/requirements.txt

# Copy application code
COPY app /code/app

# Persistenza: la directory /data sarà montata come volume su Railway
ENV DATA_DIR=/data
RUN mkdir -p /data

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT:-8000}/api/config || exit 1

# Avvio: Railway/Render iniettano PORT, fallback a 8000
CMD uvicorn app.server:app --host 0.0.0.0 --port ${PORT:-8000}
