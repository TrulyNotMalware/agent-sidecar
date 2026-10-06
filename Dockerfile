FROM python:3.14-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Pinned on purpose: the runners parse these CLIs' output and depend on their flags.
# Bump deliberately and re-run the test suite (tests/integration fakes the CLIs, so
# also smoke-test a real turn: scripts/smoke.py).
ARG NODE_MAJOR=24
ARG CODEX_VERSION=0.159.0

# tini for PID 1 / signal handling. Node.js (for the codex CLI) comes from the
# NodeSource apt repository: apt verifies packages against its signing key, which is
# fetched over TLS here (trust on first use) — no `curl | bash` of a setup script.
# (nodejs pulls in Debian's python3 as a dependency; /usr/local/bin/python stays first.)
# The claude CLI is not installed: claude-agent-sdk ships its own bundled binary,
# which is what it runs (and what /readyz checks).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates curl gnupg \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# constraints.txt pins the tested runtime dependency set (see its header). Installed
# from a scratch copy that is then removed, so only site-packages holds the code.
COPY pyproject.toml constraints.txt /tmp/src/
COPY sidecar /tmp/src/sidecar
RUN pip install -c /tmp/src/constraints.txt /tmp/src && rm -rf /tmp/src

# An empty default MCP config: a mounted ConfigMap at /etc/sidecar replaces it, and a
# mistyped MCP_CONFIG_PATH still fails loudly instead of being silently ignored.
RUN mkdir -p /etc/sidecar && echo '{"mcpServers": {}}' > /etc/sidecar/mcp.json

# Non-root. All mutable state lives under /var/lib/claude-sidecar — mount a volume
# there: session workspaces, claude transcripts/config (CLAUDE_CONFIG_DIR) and codex
# auth/rollouts (CODEX_HOME). HOME holds only caches.
RUN useradd --uid 10001 --user-group --create-home --home-dir /home/sidecar sidecar \
 && install -d -o sidecar -g sidecar \
      /var/lib/claude-sidecar/sessions /var/lib/claude-sidecar/claude /var/lib/claude-sidecar/codex

ENV HOME=/home/sidecar \
    PROVIDER=claude \
    BIND=0.0.0.0 \
    PORT=7300 \
    WORKSPACE_ROOT=/var/lib/claude-sidecar/sessions \
    CLAUDE_CONFIG_DIR=/var/lib/claude-sidecar/claude \
    CODEX_HOME=/var/lib/claude-sidecar/codex \
    MCP_CONFIG_PATH=/etc/sidecar/mcp.json \
    CLAUDE_MD_PATH=/workspace/CLAUDE.md

USER 10001:10001
WORKDIR /home/sidecar
EXPOSE 7300

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "sidecar"]
