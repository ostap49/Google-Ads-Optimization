"""Merchant Center (Content API for Shopping v2.1) client.

Uses the same OAuth credentials as Google Ads (env vars), but the refresh
token must carry BOTH scopes:
    https://www.googleapis.com/auth/adwords
    https://www.googleapis.com/auth/content

Implemented with stdlib urllib only — no extra dependencies.
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE_URL = "https://shoppingcontent.googleapis.com/content/v2.1"


class MerchantError(Exception):
    pass


class MerchantClient:
    """Minimal read-only Content API client."""

    def __init__(self, refresh_token: Optional[str] = None):
        self.client_id = os.getenv("GOOGLE_ADS_CLIENT_ID", "")
        self.client_secret = os.getenv("GOOGLE_ADS_CLIENT_SECRET", "")
        self.refresh_token = refresh_token or os.getenv("GOOGLE_ADS_REFRESH_TOKEN", "")
        self._token: Optional[str] = None
        self._token_exp: float = 0.0
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise MerchantError(
                "Google OAuth credentials are not configured (env vars missing)."
            )

    # ── auth ──────────────────────────────────────────────────────

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        data = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise MerchantError(f"OAuth token refresh failed: {body}") from exc
        self._token = payload["access_token"]
        self._token_exp = time.time() + payload.get("expires_in", 3600)
        return self._token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict:
        url = f"{BASE_URL}/{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._access_token()}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            if exc.code == 403:
                raise MerchantError(
                    "403 from Merchant Center. Most likely the refresh token was "
                    "generated without the 'content' scope — regenerate it in the "
                    "OAuth Playground with BOTH scopes: "
                    "https://www.googleapis.com/auth/adwords and "
                    "https://www.googleapis.com/auth/content. Details: " + body[:300]
                ) from exc
            raise MerchantError(f"Merchant API error {exc.code}: {body[:300]}") from exc

    # ── endpoints ─────────────────────────────────────────────────

    def authinfo(self) -> List[Dict[str, Any]]:
        """Merchant accounts accessible with this token."""
        data = self._get("accounts/authinfo")
        return [
            {
                "merchant_id": str(ident.get("merchantId") or ident.get("aggregatorId")),
                "is_mca": "aggregatorId" in ident,
            }
            for ident in data.get("accountIdentifiers", [])
        ]

    def list_products(
        self, merchant_id: str, max_pages: int = 40
    ) -> List[Dict[str, Any]]:
        """Full product feed (offerId, title, price, availability...)."""
        products = []
        page_token = None
        for _ in range(max_pages):
            data = self._get(
                f"{merchant_id}/products",
                {"maxResults": 250, "pageToken": page_token},
            )
            for p in data.get("resources", []):
                price = p.get("price", {})
                products.append(
                    {
                        "offer_id": p.get("offerId", ""),
                        "title": p.get("title", ""),
                        "brand": p.get("brand", ""),
                        "product_type": p.get("productTypes", [""])[0]
                        if p.get("productTypes")
                        else "",
                        "price": float(price.get("value", 0) or 0),
                        "currency": price.get("currency", ""),
                        "availability": p.get("availability", ""),
                        "custom_labels": {
                            i: p.get(f"customLabel{i}", "") for i in range(5)
                        },
                    }
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        logger.info("Merchant %s: %d products in feed", merchant_id, len(products))
        return products

    def list_statuses(
        self, merchant_id: str, max_pages: int = 40
    ) -> Dict[str, Dict[str, Any]]:
        """Product approval statuses + item-level issues, keyed by offerId."""
        statuses: Dict[str, Dict[str, Any]] = {}
        page_token = None
        for _ in range(max_pages):
            data = self._get(
                f"{merchant_id}/productstatuses",
                {"maxResults": 250, "pageToken": page_token},
            )
            for s in data.get("resources", []):
                # productId format: online:uk:UA:OFFERID — offerId is the tail
                offer_id = s.get("productId", "").split(":")[-1]
                dests = s.get("destinationStatuses", [])
                shopping = next(
                    (d for d in dests if d.get("destination") == "Shopping"),
                    dests[0] if dests else {},
                )
                statuses[offer_id.lower()] = {
                    "status": shopping.get("status", "unknown"),
                    "issues": [
                        {
                            "code": i.get("code", ""),
                            "description": i.get("description", ""),
                            "servability": i.get("servability", ""),
                        }
                        for i in s.get("itemLevelIssues", [])
                    ],
                }
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return statuses
