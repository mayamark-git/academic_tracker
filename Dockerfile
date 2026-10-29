# Cloud Run backend image, built from the repository root.
# The Streamlit app, local database, PDFs and .env are intentionally not copied.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/main.py backend/store.py ./

RUN useradd --create-home appuser
USER appuser

# Cloud Run provides PORT. Credentials come from the service's runtime identity.
CMD ["sh", "-c", "exec uvicorn main:_app_factory --factory --host 0.0.0.0 --port ${PORT:-8080}"]
