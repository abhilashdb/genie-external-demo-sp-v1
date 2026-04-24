"""Load environment variables and expose a typed Settings object.

Fails fast at import time if required vars are missing. GENIE_SPACE_ID is
allowed to be blank (Agent A populates it after the Genie space is created)
and will emit a warning instead.
"""

from __future__ import annotations

import os
import sys
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Find the .env file at the project root (one level up from backend/).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


class ConfigError(RuntimeError):
    """Raised when a required env var is missing."""


def _require(name: str) -> str:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        raise ConfigError(
            f"Missing required environment variable: {name} (expected in {_ENV_PATH})"
        )
    return val.strip()


def _optional(name: str, default: str = "") -> str:
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else default


@dataclass(frozen=True)
class Settings:
    # Databricks workspace
    dbx_host: str
    dbx_warehouse_id: str
    dbx_catalog: str
    dbx_schema: str

    # Genie space (may be blank during initial setup)
    genie_space_id: str

    # Service principals
    sp_northstar_client_id: str
    sp_northstar_secret: str
    sp_northstar_dealership: str
    sp_sunrise_client_id: str
    sp_sunrise_secret: str
    sp_sunrise_dealership: str

    # Optional SP app ids (not in .env yet — used only in sp_mapping return)
    sp_northstar_app_id: str = ""
    sp_sunrise_app_id: str = ""

    # Session / server
    app_session_secret: str = ""
    backend_host: str = "127.0.0.1"
    backend_port: int = 8000
    frontend_port: int = 5173

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    def token_url(self) -> str:
        """Databricks OAuth token endpoint for M2M client_credentials."""
        return f"{self.dbx_host.rstrip('/')}/oidc/v1/token"

    def api_base(self) -> str:
        return self.dbx_host.rstrip("/")


def load_settings() -> Settings:
    # Required (fail fast)
    dbx_host = _require("DBX_HOST")
    dbx_warehouse_id = _require("DBX_WAREHOUSE_ID")
    dbx_catalog = _require("DBX_CATALOG")
    dbx_schema = _require("DBX_SCHEMA")

    sp_northstar_client_id = _require("SP_NORTHSTAR_CLIENT_ID")
    sp_northstar_secret = _require("SP_NORTHSTAR_SECRET")
    sp_northstar_dealership = _require("SP_NORTHSTAR_DEALERSHIP")

    sp_sunrise_client_id = _require("SP_SUNRISE_CLIENT_ID")
    sp_sunrise_secret = _require("SP_SUNRISE_SECRET")
    sp_sunrise_dealership = _require("SP_SUNRISE_DEALERSHIP")

    app_session_secret = _require("APP_SESSION_SECRET")

    # Optional (warn only)
    genie_space_id = _optional("GENIE_SPACE_ID")
    if not genie_space_id:
        warnings.warn(
            "GENIE_SPACE_ID is not set; chat calls will fail until Agent A "
            "populates it in .env.",
            stacklevel=2,
        )

    backend_host = _optional("BACKEND_HOST", "127.0.0.1")
    try:
        backend_port = int(_optional("BACKEND_PORT", "8000"))
    except ValueError:
        backend_port = 8000
    try:
        frontend_port = int(_optional("FRONTEND_PORT", "5173"))
    except ValueError:
        frontend_port = 5173

    return Settings(
        dbx_host=dbx_host,
        dbx_warehouse_id=dbx_warehouse_id,
        dbx_catalog=dbx_catalog,
        dbx_schema=dbx_schema,
        genie_space_id=genie_space_id,
        sp_northstar_client_id=sp_northstar_client_id,
        sp_northstar_secret=sp_northstar_secret,
        sp_northstar_dealership=sp_northstar_dealership,
        sp_sunrise_client_id=sp_sunrise_client_id,
        sp_sunrise_secret=sp_sunrise_secret,
        sp_sunrise_dealership=sp_sunrise_dealership,
        sp_northstar_app_id=_optional("SP_NORTHSTAR_APP_ID"),
        sp_sunrise_app_id=_optional("SP_SUNRISE_APP_ID"),
        app_session_secret=app_session_secret,
        backend_host=backend_host,
        backend_port=backend_port,
        frontend_port=frontend_port,
    )


# Import-time load — raises if required vars are missing.
settings: Settings = load_settings()
