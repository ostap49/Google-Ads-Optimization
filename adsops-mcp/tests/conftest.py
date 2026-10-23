import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Fresh DB + config per test; never touch the real account."""
    monkeypatch.setenv("ADSOPS_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("ADSOPS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("ADSOPS_REPORTS_DIR", str(tmp_path / "reports"))
    monkeypatch.setenv("ADSOPS_DRY_RUN", "false")
    from core.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
    from gads.client import set_gads
    set_gads(None)


class FakeGads:
    """Stand-in for gads.client.Gads: canned rows per GAQL pattern, records mutates."""

    def __init__(self, routes=None, info=None):
        from gads.client import _TTLCache
        from google.ads.googleads.client import GoogleAdsClient
        self.routes = list(routes or [])
        self.queries = []
        self.mutates = []
        self.info = {"customer_id": "1234567890", "name": "Test Shop", "currency": "UAH",
                     "time_zone": "Europe/Kyiv", "manager": False, "test_account": True,
                     "status": "ENABLED", "auto_tagging_enabled": True, **(info or {})}
        self.cache = _TTLCache()
        self.login_customer_id = "9999999999"
        # real proto types so operation building is validated against v26
        self.client = GoogleAdsClient(credentials=None, developer_token="x", version="v26", use_proto_plus=True)
        self.mutate_result = None

    def route(self, pattern, rows):
        self.routes.insert(0, (pattern, rows))

    def search(self, customer_id, query):
        self.queries.append(query)
        for pattern, rows in self.routes:
            if re.search(pattern, query, re.S | re.I):
                return [dict(r) for r in (rows(query) if callable(rows) else rows)]
        return []

    def cached_search(self, customer_id, query, ttl=3600):
        return self.search(customer_id, query)

    def account_info(self, customer_id):
        return dict(self.info, customer_id=str(customer_id))

    def type(self, name):
        return self.client.get_type(name)

    def enum(self, name):
        return getattr(self.client.enums, name)

    def mutate(self, customer_id, operations, *, validate_only, partial_failure=False):
        from gads.client import MutateResult
        self.mutates.append({"customer_id": customer_id, "ops": operations,
                             "validate_only": validate_only, "partial_failure": partial_failure})
        if self.mutate_result:
            return self.mutate_result(operations, validate_only)
        names = [] if validate_only else [f"customers/{customer_id}/fake/{i}" for i in range(len(operations))]
        return MutateResult(resource_names=names, request_id="req-1", validate_only=validate_only)

    def apply_recommendation(self, customer_id, resource_names, partial_failure=True):
        from gads.client import MutateResult
        self.mutates.append({"customer_id": customer_id, "recommendations": resource_names})
        return MutateResult(resource_names=list(resource_names), request_id="req-rec")


@pytest.fixture
def fake():
    from gads.client import set_gads
    g = FakeGads()
    set_gads(g)
    return g
