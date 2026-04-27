import os

import pytest

os.environ.setdefault("BEARER_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", "/tmp/claude-sidecar-test-sessions")
os.environ.setdefault("CANCEL_GRACE_SEC", "0")

from sidecar.app import create_app  # noqa: E402
from sidecar.config import get_settings  # noqa: E402


@pytest.fixture
def settings():
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c
