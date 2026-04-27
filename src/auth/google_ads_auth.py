"""Google Ads API authentication handling."""

import os
import logging
from pathlib import Path
from typing import Optional

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


class GoogleAdsAuthenticator:
    """Handles authentication with the Google Ads API.

    Supports two methods:
    1. YAML config file (google-ads.yaml)
    2. Environment variables
    """

    def __init__(
        self,
        yaml_path: Optional[str] = None,
        use_env: bool = False,
    ):
        self.yaml_path = yaml_path or os.getenv(
            "GOOGLE_ADS_YAML_PATH", "config/google-ads.yaml"
        )
        self.use_env = use_env
        self._client: Optional[GoogleAdsClient] = None

    def get_client(self) -> GoogleAdsClient:
        """Return an authenticated GoogleAdsClient instance."""
        if self._client is not None:
            return self._client

        if self.use_env or not Path(self.yaml_path).exists():
            self._client = self._build_client_from_env()
        else:
            self._client = self._build_client_from_yaml()

        return self._client

    def _build_client_from_yaml(self) -> GoogleAdsClient:
        """Build client from YAML configuration file."""
        yaml_path = Path(self.yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"Google Ads YAML config not found at '{self.yaml_path}'. "
                f"Copy config/google-ads.yaml.example to config/google-ads.yaml "
                f"and fill in your credentials."
            )

        logger.info("Building Google Ads client from YAML: %s", self.yaml_path)
        try:
            client = GoogleAdsClient.load_from_storage(
                path=str(yaml_path), version="v17"
            )
            return client
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Google Ads client from YAML: {exc}"
            ) from exc

    def _build_client_from_env(self) -> GoogleAdsClient:
        """Build client from environment variables."""
        required = {
            "developer_token": "GOOGLE_ADS_DEVELOPER_TOKEN",
            "client_id": "GOOGLE_ADS_CLIENT_ID",
            "client_secret": "GOOGLE_ADS_CLIENT_SECRET",
            "refresh_token": "GOOGLE_ADS_REFRESH_TOKEN",
            "login_customer_id": "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        }

        credentials: dict = {"use_proto_plus": True}
        missing = []
        for field, env_var in required.items():
            value = os.getenv(env_var)
            if not value:
                missing.append(env_var)
            else:
                credentials[field] = value

        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}. "
                f"Copy .env.example to .env and fill in your credentials."
            )

        logger.info("Building Google Ads client from environment variables.")
        try:
            client = GoogleAdsClient.load_from_dict(credentials, version="v17")
            return client
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Google Ads client from env: {exc}"
            ) from exc

    @property
    def login_customer_id(self) -> str:
        """Return the MCC customer ID from config or env."""
        return os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")

    def test_connection(self) -> bool:
        """Test connectivity by fetching the MCC account info."""
        try:
            client = self.get_client()
            customer_service = client.get_service("CustomerService")
            accessible = customer_service.list_accessible_customers()
            logger.info(
                "Connection successful. Accessible accounts: %d",
                len(accessible.resource_names),
            )
            return True
        except GoogleAdsException as exc:
            for error in exc.failure.errors:
                logger.error("Google Ads API error: %s", error.message)
            return False
        except Exception as exc:
            logger.error("Connection test failed: %s", exc)
            return False
