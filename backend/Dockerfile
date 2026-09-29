# ==============================================================================
# backend/Dockerfile — Production FastAPI + SQLModel + SQLite (WAL Mode) Image
# ==============================================================================
FROM python:3.12-slim AS base

# Prevent Python from writing .pyc bytecode files and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SQLITE_DB_PATH=/app/data/medical_scheduler.db

WORKDIR /app

# Install minimal system packages required for healthchecks and timezone accuracy
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create a deterministic non-root system user/group (UID 10001 / GID 10001)
# Using an explicit numeric UID/GID makes host bind-mount ownership matching reliable.
RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin appuser

# Install Python dependencies first to leverage Docker layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r /app/requirements.txt

# Copy FastAPI application code into /app
COPY . /app

# Prepare /app/data directory for SQLite (.db, .db-wal, and .db-shm files)
# CRITICAL FOR SQLITE WAL MODE:
# SQLite must have write + execute permissions on the PARENT directory (/app/data)
# so it can dynamically create, lock, and unlink the `-wal` and `-shm` shared-memory files.
RUN mkdir -p /app/data && \
    chown -R 10001:10001 /app /app/data && \
    chmod 770 /app/data

# Drop root privileges
USER 10001:10001

# Declare persistent volume mount point for SQLite WAL files
VOLUME ["/app/data"]

EXPOSE 8000

# Run single-process async Uvicorn worker (optimal for SQLite WAL concurrency via aiosqlite)
ENTRYPOINT ["uvicorn", "main:app"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
