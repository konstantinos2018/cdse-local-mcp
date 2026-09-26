"""Runtime configuration, read from ``CDSE_*`` environment variables.

In normal use those variables come from the MCP client's ``env`` block. A ``.env`` file is
also read, but only from the **current working directory** - which is the repository during
development and somewhere else entirely when a client launches the server. So ``.env`` is a
development convenience; nothing may depend on it to work.

Endpoint defaults are the values verified in ``docs/cdse-apis.md``. They are settable so a
test or a future mirror can point elsewhere, not because they are expected to change.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

GIB = 1024**3
DOWNLOAD_FOLDER_NAME = "cdse-local-mcp"


def _user_downloads_dir() -> Path:
    """The user's Downloads folder, as their desktop names it.

    On Linux the folder name is localised - a Greek desktop may call it ``Λήψεις`` - and the
    real name is recorded in ``user-dirs.dirs``. Hardcoding ``~/Downloads`` would create a
    second, unexpected folder there. macOS and Windows keep the on-disk name ``Downloads``
    whatever the display language.
    """
    home = Path.home()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    try:
        lines = (config_home / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return home / "Downloads"

    for line in lines:
        key, _, value = line.strip().partition("=")
        if key != "XDG_DOWNLOAD_DIR" or not value:
            continue
        resolved = Path(value.strip().strip('"').replace("$HOME", str(home)))
        if not resolved.is_absolute():
            resolved = home / resolved
        # Per the XDG spec, a user dir set to $HOME itself means "disabled".
        if resolved != home:
            return resolved
    return home / "Downloads"


def default_download_dir() -> Path:
    """Where downloads land when ``CDSE_DOWNLOAD_DIR`` is unset.

    A visible folder inside Downloads, not a hidden cache. These are files a person asked for
    and will go looking for; the previous default, ``~/.cache/cdse-local-mcp``, hid them.
    """
    return _user_downloads_dir() / DOWNLOAD_FOLDER_NAME


class Settings(BaseSettings):
    """Server configuration.

    Credentials are optional: catalogue discovery works unauthenticated. Downloads need
    ``client_id``/``client_secret``; windowed reads additionally need the S3 keys.
    """

    model_config = SettingsConfigDict(
        env_prefix="CDSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- credentials -------------------------------------------------------------------
    client_id: str | None = None
    client_secret: SecretStr | None = None
    s3_access_key: str | None = None
    s3_secret_key: SecretStr | None = None

    # --- local filesystem and transfer caps --------------------------------------------
    download_dir: Path = Field(default_factory=default_download_dir)
    max_call_bytes: int = 5 * GIB
    max_session_bytes: int = 25 * GIB

    # --- endpoints (see docs/cdse-apis.md) ---------------------------------------------
    stac_url: str = "https://stac.dataspace.copernicus.eu/v1"
    catalogue_odata_url: str = "https://catalogue.dataspace.copernicus.eu/odata/v1"
    download_odata_url: str = "https://download.dataspace.copernicus.eu/odata/v1"
    token_url: str = (
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
    )
    s3_endpoint_url: str = "https://eodata.dataspace.copernicus.eu"
    # CDSE's object store is Ceph-compatible and ignores the region, but botocore insists
    # on one being set.
    s3_region: str = "default"

    # --- http ---------------------------------------------------------------------------
    request_timeout: float = 60.0
    max_search_limit: int = 100

    @property
    def has_oauth(self) -> bool:
        """True when the OAuth client credentials needed for downloads are configured."""
        return bool(self.client_id and self.client_secret)

    @property
    def has_s3(self) -> bool:
        """True when the S3 keys needed for windowed reads are configured."""
        return bool(self.s3_access_key and self.s3_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
