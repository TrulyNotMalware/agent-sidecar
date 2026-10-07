import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sidecar.app import create_app
from sidecar.config import Settings, get_settings
from sidecar.observability.logging import configure_logging

os.environ.setdefault("BEARER_SECRET", "test-secret")
os.environ.setdefault("WORKSPACE_ROOT", "/tmp/claude-sidecar-test-sessions")


@pytest.fixture(scope="session", autouse=True)
def _logging_configured() -> None:
    # Importing sidecar.app configures nothing (it is a factory); the tests that pin
    # what configure_logging installs on plain `logging` must not depend on an earlier
    # test having built an app.
    configure_logging()


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
