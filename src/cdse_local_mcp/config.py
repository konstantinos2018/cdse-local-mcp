"""Runtime configuration, read from ``CDSE_*`` environment variables.

Endpoint defaults are the values verified in ``docs/cdse-apis.md``. They are settable so a
test or a future mirror can point elsewhere, not because they are expected to change.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

GIB = 1024**3


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
    download_dir: Path = Path.home() / ".cache" / "cdse-local-mcp"
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
