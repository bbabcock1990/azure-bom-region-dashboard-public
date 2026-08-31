# Azure BOM Region Dashboard — local single-process container.
# Build:  docker compose up --build
# Then open http://localhost:4280/
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOCAL_MODE=true \
    HOST=0.0.0.0 \
    PORT=4280 \
    LOCAL_STORAGE_DIR=/data

WORKDIR /app

# Install dependencies first for better layer caching.
COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

# App source.
COPY . .

# Persist local SQLite + snapshot JSON across container restarts.
VOLUME ["/data"]
EXPOSE 4280

# ALLOWED_ORIGIN defaults to the published port; override if you map a different
# host port. Sign-in via InteractiveBrowserCredential requires the container to
# reach a browser — for headless/container use, run in DEMO_MODE or supply a
# token, otherwise prefer the Windows .exe / native run for interactive Azure.
CMD ["python", "-m", "server"]
