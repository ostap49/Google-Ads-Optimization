"""gads.client against a mocked GoogleAdsService (real v26 proto messages, no network)."""
import grpc
import pytest
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

import gads.client as C
from google.ads.googleads.v26.errors.types import criterion_error, query_error, quota_error


class _RpcError(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        return "boom"


def _client():
    return GoogleAdsClient(credentials=None, developer_token="x", version="v26", use_proto_plus=True,
                           login_customer_id="9999999999")


def _quota_exception(client):
    failure = client.get_type("GoogleAdsFailure")
    err = client.get_type("GoogleAdsError")
    err.error_code.quota_error = quota_error.QuotaErrorEnum.QuotaError.RESOURCE_EXHAUSTED
    err.message = "Too many requests"
    failure.errors.append(err)
    return GoogleAdsException(_RpcError(grpc.StatusCode.RESOURCE_EXHAUSTED), None, failure, "req-q")


class FakeService:
    def __init__(self, client, batches, fail_times=0):
        self.client, self.batches, self.fail_times, self.calls = client, batches, fail_times, 0

    def search_stream(self, customer_id, query):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise _quota_exception(self.client)
        return iter(self.batches)


def _batch(client, rows):
    resp = client.get_type("SearchGoogleAdsStreamResponse")
    for cid, name, cost in rows:
        row = client.get_type("GoogleAdsRow")
        row.campaign.id = cid
        row.campaign.name = name
        row.campaign.status = client.enums.CampaignStatusEnum.ENABLED
        row.metrics.cost_micros = cost
        resp.results.append(row)
    return resp


def test_search_stream_flattens_batches(monkeypatch):
    client = _client()
    svc = FakeService(client, [_batch(client, [(1, "A", 1_000_000)]), _batch(client, [(2, "Б", 2_000_000)])])
    g = C.Gads(client=client)
    monkeypatch.setattr(g, "service", lambda name: svc)
    rows = g.search("123-456-7890", "SELECT campaign.id, campaign.name, campaign.status, metrics.cost_micros FROM campaign")
    assert rows == [
        {"campaign.id": 1, "campaign.name": "A", "campaign.status": "ENABLED", "metrics.cost_micros": 1_000_000},
        {"campaign.id": 2, "campaign.name": "Б", "campaign.status": "ENABLED", "metrics.cost_micros": 2_000_000},
    ]
    assert g.login_customer_id == "9999999999"


def test_retry_on_resource_exhausted(monkeypatch):
    client = _client()
    svc = FakeService(client, [_batch(client, [(1, "A", 0)])], fail_times=2)
    g = C.Gads(client=client)
    monkeypatch.setattr(g, "service", lambda name: svc)
    sleeps = []
    monkeypatch.setattr(C.time, "sleep", lambda s: sleeps.append(s))
    assert len(g.search("1234567890", "SELECT campaign.id FROM campaign")) == 1
    assert svc.calls == 3 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_non_retryable_error_is_structured(monkeypatch):
    client = _client()
    g = C.Gads(client=client)

    class Bad:
        def search_stream(self, **kw):
            failure = client.get_type("GoogleAdsFailure")
            err = client.get_type("GoogleAdsError")
            err.error_code.query_error = query_error.QueryErrorEnum.QueryError.UNRECOGNIZED_FIELD
            err.message = "Unrecognized field"
            failure.errors.append(err)
            raise GoogleAdsException(_RpcError(grpc.StatusCode.INVALID_ARGUMENT), None, failure, "req-1")

    monkeypatch.setattr(g, "service", lambda name: Bad())
    with pytest.raises(C.GadsError) as ex:
        g.search("1234567890", "SELECT campaign.bogus FROM campaign")
    d = ex.value.as_dict()
    assert d["errors"][0]["code"] == "query_error.UNRECOGNIZED_FIELD" and d["request_id"] == "req-1"


def test_mutate_partial_failure_parsing(monkeypatch):
    client = _client()
    g = C.Gads(client=client)

    class Svc:
        def mutate(self, request):
            assert request.validate_only is False and request.partial_failure is True
            resp = client.get_type("MutateGoogleAdsResponse")
            ok = client.get_type("MutateOperationResponse")
            ok.campaign_criterion_result.resource_name = "customers/1/campaignCriteria/1~2"
            resp.mutate_operation_responses.append(ok)
            resp.mutate_operation_responses.append(client.get_type("MutateOperationResponse"))
            failure = client.get_type("GoogleAdsFailure")
            err = client.get_type("GoogleAdsError")
            err.error_code.criterion_error = criterion_error.CriterionErrorEnum.CriterionError.KEYWORD_HAS_INVALID_CHARS
            err.message = "bad chars"
            el = client.get_type("ErrorLocation").FieldPathElement()
            el.field_name = "mutate_operations"
            el.index = 1
            err.location.field_path_elements.append(el)
            failure.errors.append(err)
            resp.partial_failure_error.code = 3
            any_ = resp.partial_failure_error.details.add()
            any_.Pack(type(failure).pb(failure))
            return resp

    monkeypatch.setattr(g, "service", lambda name: Svc())
    op = client.get_type("MutateOperation")
    res = g.mutate("1", [op, op], validate_only=False, partial_failure=True)
    assert res.resource_names == ["customers/1/campaignCriteria/1~2", None]
    assert res.partial_failures[0]["operation_index"] == 1
    assert res.partial_failures[0]["code"] == "criterion_error.KEYWORD_HAS_INVALID_CHARS"


def test_where_fields_auto_selected():
    q = C.ensure_where_fields_selected(
        "SELECT ad_group.id FROM ad_group WHERE ad_group.status = 'ENABLED' AND campaign.status != 'REMOVED' "
        "AND segments.date DURING LAST_7_DAYS AND metrics.clicks > 0")
    assert C.select_fields(q) == ["ad_group.id", "ad_group.status", "campaign.status"]


def test_change_history_from_proto_rows(fake, monkeypatch):
    from tools.history import get_change_history
    client = fake.client
    row = client.get_type("GoogleAdsRow")
    ev = row.change_event
    ev.change_date_time = "2026-10-20 10:00:00"
    ev.user_email = "me@example.com"
    ev.client_type = client.enums.ChangeClientTypeEnum.GOOGLE_ADS_WEB_CLIENT
    ev.change_resource_type = client.enums.ChangeEventResourceTypeEnum.CAMPAIGN_BUDGET
    ev.resource_change_operation = client.enums.ResourceChangeOperationEnum.UPDATE
    ev.change_resource_name = "customers/1234567890/campaignBudgets/55"
    ev.old_resource.campaign_budget.amount_micros = 100_000_000
    ev.new_resource.campaign_budget.amount_micros = 150_000_000
    ev.changed_fields.paths.append("amount_micros")
    fake.search_raw = lambda cid, q: [row]
    out = get_change_history("1234567890", days=7)
    ch = out["rows"][0]
    assert ch["resource_type"] == "CAMPAIGN_BUDGET" and ch["client"] == "GOOGLE_ADS_WEB_CLIENT"
    assert ch["changed"]["amount_micros"] == {"old": 100.0, "new": 150.0}


def test_keyword_research_merges_languages(fake, monkeypatch):
    from tools.planning import keyword_research
    client = fake.client

    def idea(text, vol, concept):
        r = client.get_type("GenerateKeywordIdeaResult")
        r.text = text
        r.keyword_idea_metrics.avg_monthly_searches = vol
        r.keyword_idea_metrics.competition = client.enums.KeywordPlanCompetitionLevelEnum.HIGH
        r.keyword_idea_metrics.low_top_of_page_bid_micros = 5_000_000
        r.keyword_idea_metrics.high_top_of_page_bid_micros = 15_000_000
        c = client.get_type("KeywordConcept")
        c.name = concept
        r.keyword_annotations.concepts.append(c)
        return r

    calls = []

    class Svc:
        def generate_keyword_ideas(self, request):
            calls.append(request.language)
            if request.language.endswith("1036"):
                return [idea("диван кутовий", 1000, "кутовий"), idea("диван розкладний", 500, "розкладний"),
                        idea("кутовий диван ціна", 300, "кутовий")]
            return [idea("диван кутовий", 1300, "кутовий"), idea("диван угловой", 2000, "угловой")]

    fake.service = lambda name: Svc()
    fake._retry = lambda fn, what: fn()
    out = keyword_research("1234567890", seed_keywords=["диван"])
    assert calls == ["languageConstants/1036", "languageConstants/1031"]
    kw = {r["keyword"]: r for r in out["rows"]}
    assert kw["диван кутовий"]["avg_monthly_searches"] == 1300 and kw["диван кутовий"]["top_of_page_bid_low"] == 5.0
    assert "кутовий" in out["themes"]
