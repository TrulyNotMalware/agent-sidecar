"""Run the real sidecar (uvicorn + claude-agent-sdk), with the SDK pointed at a fake CLI.

The SDK prefers its bundled CLI over PATH, so the lookup is patched rather than
shadowed. FAKE_CLI names an executable that runs tests/integration/fake_claude.py;
without it (test_real_cli.py) the SDK runs its bundled CLI against a fake model API.
"""

import os
import sys

from claude_agent_sdk._internal.transport import subprocess_cli

# Fail loudly if the private hook moved: patching a missing attribute would succeed
# silently and let the real CLI run with whatever credentials it can find.
if not callable(getattr(subprocess_cli.SubprocessCLITransport, "_find_cli", None)):
    sys.exit(
        "claude-agent-sdk no longer has SubprocessCLITransport._find_cli; "
        "update tests/integration/_server.py before running the integration tests"
    )
if os.environ.get("FAKE_CLI"):
    subprocess_cli.SubprocessCLITransport._find_cli = lambda self: os.environ["FAKE_CLI"]
os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")

from sidecar.__main__ import main  # noqa: E402

main()
