import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidecar.app import create_app
from sidecar.config import Settings, get_settings

os.environ.setdefault("BEARER_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", "/tmp/claude-sidecar-test-sessions")


@pytest.fixture
def settings() -> Settings:
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def app() -> FastAPI:
    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
