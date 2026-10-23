import pytest

from core import dates
from core.fmt import compact, from_micros, to_micros, pct_delta
from gads.client import flatten_row, select_fields


def test_micros_roundtrip():
    assert to_micros(12.345) == 12_350_000
    assert from_micros(12_350_000) == 12.35
    assert to_micros(None) is None


def test_compact_totals_over_all_rows():
    rows = [{"n": i, "cost": float(i)} for i in range(10)]
    out = compact(rows, limit=3, sort_by="cost", totals_fields=["cost"])
    assert out["truncated"] is True and out["returned"] == 3
    assert out["rows"][0]["cost"] == 9.0
    assert out["totals"]["cost"] == 45.0


def test_pct_delta():
    assert pct_delta(150, 100) == 50.0
    assert pct_delta(1, 0) is None


def test_date_presets_and_previous():
    dr = dates.resolve("LAST_7_DAYS", "Europe/Kyiv")
    assert dr.days == 7
    prev = dr.previous()
    assert prev.days == 7 and (dr.start - prev.end).days == 1
    c = dates.resolve("2026-01-01..2026-01-31")
    assert c.days == 31 and "BETWEEN '2026-01-01' AND '2026-01-31'" in c.gaql()
    lm = dates.resolve("LAST_MONTH")
    assert lm.start.day == 1
    with pytest.raises(ValueError):
        dates.resolve("NEXT_CENTURY")


def test_flatten_real_proto_row():
    from google.ads.googleads.client import GoogleAdsClient
    c = GoogleAdsClient(credentials=None, developer_token="x", version="v26")
    row = c.get_type("GoogleAdsRow")
    row.campaign.id = 42
    row.campaign.name = "Бренд | Search"
    row.campaign.status = c.enums.CampaignStatusEnum.PAUSED
    row.metrics.cost_micros = 1_500_000
    row.ad_group_ad.ad.type_ = c.enums.AdTypeEnum.RESPONSIVE_SEARCH_AD
    q = "SELECT campaign.id, campaign.name, campaign.status, metrics.cost_micros, ad_group_ad.ad.type FROM ad_group_ad"
    flat = flatten_row(row, select_fields(q))
    assert flat == {"campaign.id": 42, "campaign.name": "Бренд | Search", "campaign.status": "PAUSED",
                    "metrics.cost_micros": 1_500_000, "ad_group_ad.ad.type": "RESPONSIVE_SEARCH_AD"}


def test_run_gaql_validation_and_limit(fake):
    from tools.gaql import run_gaql, validate_gaql
    assert validate_gaql("SELECT campaign.id FROM campaign", 100).endswith("LIMIT 100")
    assert "LIMIT 5" in validate_gaql("SELECT campaign.id FROM campaign LIMIT 5", 100)
    q = validate_gaql("SELECT campaign.id FROM campaign PARAMETERS include_drafts=true", 10)
    assert q.index("LIMIT 10") < q.index("PARAMETERS")
    with pytest.raises(ValueError):
        validate_gaql("DELETE campaign", 10)
    with pytest.raises(ValueError):
        validate_gaql("SELECT campaign.id FROM campaign; SELECT x FROM y", 10)
    fake.route("FROM campaign", [{"campaign.id": 1, "metrics.cost_micros": 2_000_000}])
    out = run_gaql("1234567890", "SELECT campaign.id, metrics.cost_micros FROM campaign")
    assert out["rows"][0]["metrics.cost"] == 2.0 and out["currency"] == "UAH"


def test_list_accounts_and_alias(fake):
    from tools.accounts import list_accounts, set_account_alias
    fake.route("FROM customer_client", [
        {"customer_client.id": 9999999999, "customer_client.descriptive_name": "MCC", "customer_client.manager": True,
         "customer_client.status": "ENABLED", "customer_client.hidden": False, "customer_client.level": 0,
         "customer_client.currency_code": "UAH", "customer_client.time_zone": "Europe/Kyiv", "customer_client.test_account": False},
        {"customer_client.id": 1234567890, "customer_client.descriptive_name": "Stryi", "customer_client.manager": False,
         "customer_client.status": "ENABLED", "customer_client.hidden": False, "customer_client.level": 1,
         "customer_client.currency_code": "UAH", "customer_client.time_zone": "Europe/Kyiv", "customer_client.test_account": False},
        {"customer_client.id": 1111111111, "customer_client.descriptive_name": "Old", "customer_client.manager": False,
         "customer_client.status": "CANCELED", "customer_client.hidden": False, "customer_client.level": 1,
         "customer_client.currency_code": "USD", "customer_client.time_zone": "America/New_York", "customer_client.test_account": False},
    ])
    set_account_alias("123-456-7890", "stryi")
    out = list_accounts()
    assert out["count"] == 1 and out["accounts"][0]["alias"] == "stryi"
    from core.accounts import resolve
    assert resolve("STRYI") == "1234567890"
    assert list_accounts(include_inactive=True, include_managers=True)["count"] == 3
