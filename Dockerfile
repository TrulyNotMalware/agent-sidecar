FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# tini for PID 1 / signal handling; node+npm to install claude and codex CLIs.
# Both CLIs are installed unconditionally — PROVIDER env var selects which is used at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tini ca-certificates curl gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code @openai/codex \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY sidecar ./sidecar
RUN pip install -e .

ENV PROVIDER=claude \
    BIND=0.0.0.0 \
    PORT=7300 \
    WORKSPACE_ROOT=/var/lib/claude-sidecar/sessions \
    MCP_CONFIG_PATH=/etc/sidecar/mcp.json \
    CLAUDE_MD_PATH=/workspace/CLAUDE.md
RUN mkdir -p ${WORKSPACE_ROOT}

EXPOSE 7300

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "sidecar"]
