"""Run the real sidecar (uvicorn + claude-agent-sdk) with the SDK pointed at a fake CLI.

The SDK prefers its bundled CLI over PATH, so the lookup is patched rather than
shadowed. FAKE_CLI must name an executable that runs tests/integration/fake_claude.py.
"""

import os

from claude_agent_sdk._internal.transport import subprocess_cli

subprocess_cli.SubprocessCLITransport._find_cli = lambda self: os.environ["FAKE_CLI"]
os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")

from sidecar.__main__ import main  # noqa: E402

main()
