"""FastAPI dependencies shared by the routes."""

from typing import Annotated

from fastapi import Depends

from .config import Settings, get_settings

# Settings reach handlers through the dependency system, so a test can replace them
# with `app.dependency_overrides[get_settings]` instead of editing the environment.
SettingsDep = Annotated[Settings, Depends(get_settings)]
