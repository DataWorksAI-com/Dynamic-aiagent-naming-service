# ── agentns Dockerfile ────────────────────────────────────────────────────────
# Multi-stage build — final image is ~120 MB (python:3.12-slim base)
#
# Build:
#   docker build -t agentns:latest .
#
# Run (in-memory, no MongoDB):
#   docker run -p 8200:8200 agentns:latest
#
# Run (with MongoDB persistence):
#   docker run -p 8200:8200 \
#     -e MONGODB_URI="mongodb+srv://user:pass@cluster.mongodb.net/" \
#     -e AGENTNS_NAMESPACE="mycompany.sales" \
#     agentns:latest

FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools
RUN pip install --no-cache-dir build

# Copy project files
COPY pyproject.toml .
COPY agentns/ agentns/

# Build wheel
RUN python -m build --wheel --outdir /dist


# ── final image ────────────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="agentns"
LABEL org.opencontainers.image.description="Agent Name Service sidecar for multi-agent systems"
LABEL org.opencontainers.image.version="1.0.0"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/DataWorksAI-com/Dynamic-ans"

WORKDIR /app

# Copy wheel from builder and install (with optional mongo extras)
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/agentns-*.whl "agentns[mongo] @ /tmp/agentns-1.0.0-py3-none-any.whl" 2>/dev/null || \
    pip install --no-cache-dir /tmp/agentns-*.whl motor>=3.4.0

# Create non-root user
RUN useradd -r -s /bin/false agentns
USER agentns

# ── environment defaults ───────────────────────────────────────────────────────
ENV AGENTNS_PORT=8200
ENV AGENTNS_NAMESPACE=agents.local
ENV AGENTNS_TLD=agentns.local
ENV AGENTNS_HEALTH_INTERVAL=30
ENV AGENTNS_GEOCODING=on
# MONGODB_URI — set at runtime if persistence is needed

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

CMD ["agentns-server"]
