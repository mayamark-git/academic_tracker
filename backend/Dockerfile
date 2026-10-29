FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py store.py ./

RUN useradd --create-home appuser
USER appuser

# Cloud Run provides PORT. Credentials come from the service's runtime identity.
CMD ["sh", "-c", "exec uvicorn main:_app_factory --factory --host 0.0.0.0 --port ${PORT:-8080}"]
