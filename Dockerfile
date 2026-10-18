# Fulcrum Service Recovery — container image.
# Build/run from the project root (the folder containing this file):
#   docker build -t fulcrum-service-recovery .
#   docker run --rm -p 8000:8000 fulcrum-service-recovery
# Add provider credentials at run time, never at build time:
#   docker run --rm -p 8000:8000 -e AMD_API_KEY=... fulcrum-service-recovery
FROM python:3.12-slim

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Application code: backend + contracts + fixtures + static frontend.
COPY backend/ backend/
COPY contracts/ contracts/
COPY fixtures/ fixtures/
COPY frontend/ frontend/

# Cases/audit trail persist inside the container unless a volume is mounted;
# mount one to keep them:  -v fulcrum-data:/app/data
ENV FULCRUM_STORE_PATH=/app/data/fulcrum_cases.sqlite

# Run unprivileged: the app needs write access only to its data directory.
RUN useradd --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]