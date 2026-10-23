"""Google Ads client factory + thin, quota-aware wrapper.

* search(): SearchStream, rows flattened to {"campaign.name": ..., "metrics.cost_micros": ...}
* mutate(): GoogleAdsService.Mutate (batched, validate_only / partial_failure aware)
* retry with exponential backoff + jitter on RESOURCE_EXHAUSTED and transient errors
* 1h TTL cache for immutable-ish metadata (account info, campaign names, conversion actions)
"""
from __future__ import annotations

import enum
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import grpc

from core.config import Settings, get_settings
from core.fmt import clean_id

log = logging.getLogger("adsops.gads")

RETRYABLE_GRPC = {
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.UNAVAILABLE,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.INTERNAL,
    grpc.StatusCode.ABORTED,
}
RETRYABLE_ERROR_CODES = {
    "quota_error.RESOURCE_EXHAUSTED",
    "quota_error.RESOURCE_TEMPORARILY_EXHAUSTED",
    "internal_error.INTERNAL_ERROR",
    "internal_error.TRANSIENT_ERROR",
    "internal_error.DEADLINE_EXCEEDED",
    "database_error.CONCURRENT_MODIFICATION",
}
MAX_RETRIES = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0
CACHE_TTL = 3600


class GadsError(Exception):
    """Google Ads API error with structured details (safe to return to the model)."""

    def __init__(self, message: str, errors: list[dict[str, Any]] | None = None,
                 request_id: str | None = None, retryable: bool = False):
        super().__init__(message)
        self.errors = errors or []
        self.request_id = request_id
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {"error": str(self), "errors": self.errors, "request_id": self.request_id}


@dataclass
class MutateResult:
    resource_names: list[str | None] = field(default_factory=list)
    partial_failures: list[dict[str, Any]] = field(default_factory=list)
    request_id: str | None = None
    validate_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "resource_names": self.resource_names,
            "partial_failures": self.partial_failures,
            "request_id": self.request_id,
            "validate_only": self.validate_only,
        }


class _RequestIdCapture(grpc.UnaryUnaryClientInterceptor):
    """Captures the `request-id` trailing metadata of the last unary call on this thread."""

    local = threading.local()

    def intercept_unary_unary(self, continuation, client_call_details, request):
        call = continuation(client_call_details, request)
        try:
            for key, value in call.trailing_metadata() or ():
                if key == "request-id":
                    self.local.request_id = value
        except Exception:  # pragma: no cover - metadata not available on errors
            pass
        return call


class _TTLCache:
    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get_or_set(self, key: Any, ttl: float, fn: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (now + ttl, value)
        return value

    def invalidate(self, prefix: tuple | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._data.clear()
            else:
                for k in [k for k in self._data if isinstance(k, tuple) and k[: len(prefix)] == prefix]:
                    del self._data[k]


# ----------------------------------------------------------------------------- helpers

_SELECT_RE = re.compile(r"^\s*SELECT\s+(.*?)\s+FROM\s+", re.I | re.S)


def select_fields(query: str) -> list[str]:
    m = _SELECT_RE.match(query)
    if not m:
        raise GadsError("Query must start with SELECT ... FROM ...")
    return [f.strip() for f in m.group(1).split(",") if f.strip()]


_CLAUSE_RE = re.compile(r"\bWHERE\b(.*?)(?=\bORDER\s+BY\b|\bLIMIT\b|\bPARAMETERS\b|$)", re.I | re.S)
_FIELD_RE = re.compile(r"\b([a-z_]+(?:\.[a-z_0-9]+)+)\b")


# Fields that are filter-only (or whose resource requires them as a filter) — never auto-selected.
FILTER_ONLY = {
    "campaign_search_term_insight.campaign_id",
    "customer_search_term_insight.id",
}


def ensure_where_fields_selected(query: str) -> str:
    """API v24+ rejects filters on attribute fields that are not selected (EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE).

    Appends such resource-attribute fields to SELECT. Segments/metrics are left alone: adding a segment
    would change row granularity, and metrics filters are accepted as-is.
    """
    m = _CLAUSE_RE.search(query)
    if not m:
        return query
    selected = set(select_fields(query))
    where = re.sub(r"'(?:[^'\\]|\\.)*'", "''", m.group(1))
    missing = []
    for f in _FIELD_RE.findall(where):
        if f.split(".")[0] in {"segments", "metrics"} or f in selected or f in missing or f in FILTER_ONLY:
            continue
        missing.append(f)
    if not missing:
        return query
    sel = _SELECT_RE.match(query)
    insert_at = sel.end(1)
    return query[:insert_at] + ", " + ", ".join(missing) + query[insert_at:]


def _attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name)
    except AttributeError:
        return getattr(obj, name + "_")  # proto-plus renames reserved words: type -> type_


def to_py(value: Any) -> Any:
    """proto-plus value -> plain JSON-able python."""
    if isinstance(value, enum.Enum):
        return value.name
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(type(value), "to_dict") and hasattr(type(value), "pb"):
        return type(value).to_dict(value, preserving_proto_field_name=True, use_integers_for_enums=False)
    if hasattr(value, "DESCRIPTOR") and hasattr(value, "ListFields"):
        from google.protobuf.json_format import MessageToDict
        return MessageToDict(value, preserving_proto_field_name=True)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if hasattr(value, "__iter__") and not isinstance(value, dict):
        return [to_py(v) for v in value]
    return value


def _descriptor(msg: Any) -> Any:
    if hasattr(msg, "DESCRIPTOR"):
        return msg.DESCRIPTOR
    pb = getattr(type(msg), "pb", None)
    return pb(msg).DESCRIPTOR if pb else None


def _enum_name(parent: Any, field_name: str, value: Any) -> Any:
    """Map raw enum ints to names using the field descriptor (works for proto-plus and raw protobuf)."""
    desc = _descriptor(parent)
    fd = desc.fields_by_name.get(field_name) or desc.fields_by_name.get(field_name + "_") if desc else None
    if fd is None or fd.enum_type is None:
        return None

    def name(v: Any) -> Any:
        ev = fd.enum_type.values_by_number.get(int(v))
        return ev.name if ev else int(v)

    if isinstance(value, (int, enum.Enum)):
        return name(value)
    if hasattr(value, "__iter__"):
        return [name(v) for v in value]
    return None


def flatten_row(row: Any, fields: Iterable[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for path in fields:
        parent, obj = None, row
        parts = path.split(".")
        try:
            for part in parts:
                parent, obj = obj, _attr(obj, part)
            named = _enum_name(parent, parts[-1], obj) if parent is not None else None
            out[path] = named if named is not None else to_py(obj)
        except AttributeError:
            out[path] = None
    return out


def error_code_str(error_code: Any) -> str:
    """GoogleAdsError.error_code (oneof) -> 'quota_error.RESOURCE_EXHAUSTED'."""
    pb = type(error_code).pb(error_code)
    which = pb.WhichOneof("error_code")
    if not which:
        return "unknown"
    enum_value = getattr(pb, which)
    enum_name = pb.DESCRIPTOR.fields_by_name[which].enum_type.values_by_number[enum_value].name
    return f"{which}.{enum_name}"


def describe_failure(failure: Any) -> list[dict[str, Any]]:
    out = []
    for err in getattr(failure, "errors", []) or []:
        item: dict[str, Any] = {"code": error_code_str(err.error_code), "message": err.message}
        path = [
            f"{el.field_name}[{el.index}]" if "index" in el and el.index is not None else el.field_name
            for el in err.location.field_path_elements
        ] if err.location else []
        if path:
            item["location"] = ".".join(path)
            for el in err.location.field_path_elements:
                if el.field_name == "mutate_operations" and "index" in el:
                    item["operation_index"] = el.index
        if err.trigger and err.trigger.string_value:
            item["trigger"] = err.trigger.string_value
        try:
            delay = err.details.quota_error_details.retry_delay
            if delay and delay.seconds:
                item["retry_after_s"] = delay.seconds
        except Exception:
            pass
        out.append(item)
    return out


# ----------------------------------------------------------------------------- client

class Gads:
    def __init__(self, settings: Settings | None = None, client: Any = None):
        self.settings = settings or get_settings()
        self._client = client
        self._lock = threading.Lock()
        self.cache = _TTLCache()
        self._interceptor = _RequestIdCapture()

    # -- construction ---------------------------------------------------------
    @property
    def client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    from google.ads.googleads.client import GoogleAdsClient

                    path = self.settings.google_ads_yaml
                    if path.exists():
                        self._client = GoogleAdsClient.load_from_storage(str(path), version=self.settings.api_version)
                    else:
                        self._client = GoogleAdsClient.load_from_env(version=self.settings.api_version)
        return self._client

    @property
    def login_customer_id(self) -> str | None:
        lcid = getattr(self.client, "login_customer_id", None)
        return clean_id(lcid) if lcid else None

    def service(self, name: str) -> Any:
        return self.client.get_service(name, interceptors=[self._interceptor])

    def enum(self, name: str) -> Any:
        return getattr(self.client.enums, name)

    def type(self, name: str) -> Any:
        return self.client.get_type(name)

    # -- retry ----------------------------------------------------------------
    def _retry(self, fn: Callable[[], Any], what: str) -> Any:
        from google.ads.googleads.errors import GoogleAdsException

        attempt = 0
        while True:
            try:
                return fn()
            except GoogleAdsException as ex:
                errors = describe_failure(ex.failure)
                codes = {e["code"] for e in errors}
                retryable = bool(codes & RETRYABLE_ERROR_CODES) or ex.error.code() in RETRYABLE_GRPC
                if not retryable or attempt >= MAX_RETRIES:
                    msg = "; ".join(f"{e['code']}: {e['message']}" for e in errors) or str(ex.error)
                    raise GadsError(f"{what} failed: {msg}", errors, ex.request_id, retryable) from None
                hinted = max((e.get("retry_after_s", 0) for e in errors), default=0)
                self._sleep(attempt, hinted, what, codes)
            except grpc.RpcError as ex:
                code = ex.code() if hasattr(ex, "code") else None
                if code not in RETRYABLE_GRPC or attempt >= MAX_RETRIES:
                    raise GadsError(f"{what} failed: {code}: {getattr(ex, 'details', lambda: ex)()}") from None
                self._sleep(attempt, 0, what, {str(code)})
            attempt += 1

    @staticmethod
    def _sleep(attempt: int, hinted: float, what: str, codes: set[str]) -> None:
        delay = min(MAX_DELAY, max(hinted, BASE_DELAY * (2 ** attempt)) + random.uniform(0, 0.5))
        log.warning("%s: retryable %s, sleeping %.1fs (attempt %d)", what, sorted(codes), delay, attempt + 1)
        time.sleep(delay)

    # -- reads ----------------------------------------------------------------
    def search_raw(self, customer_id: str, query: str) -> list[Any]:
        cid = clean_id(customer_id)
        query = ensure_where_fields_selected(query)

        def run() -> list[Any]:
            svc = self.service("GoogleAdsService")
            rows: list[Any] = []
            stream = svc.search_stream(customer_id=cid, query=query)
            for batch in stream:  # keep `svc` referenced while the stream is consumed
                rows.extend(batch.results)
            return rows

        return self._retry(run, f"search({cid})")

    def search(self, customer_id: str, query: str) -> list[dict[str, Any]]:
        fields = select_fields(ensure_where_fields_selected(query))
        return [flatten_row(r, fields) for r in self.search_raw(customer_id, query)]

    def cached_search(self, customer_id: str, query: str, ttl: float = CACHE_TTL) -> list[dict[str, Any]]:
        return self.cache.get_or_set(("q", clean_id(customer_id), query), ttl, lambda: self.search(customer_id, query))

    def account_info(self, customer_id: str) -> dict[str, Any]:
        """name, currency, time zone, manager flag — cached 1h."""
        q = ("SELECT customer.id, customer.descriptive_name, customer.currency_code, customer.time_zone, "
             "customer.manager, customer.test_account, customer.status, customer.auto_tagging_enabled FROM customer LIMIT 1")
        rows = self.cached_search(customer_id, q)
        if not rows:
            raise GadsError(f"Customer {customer_id} not found or not accessible")
        r = rows[0]
        return {
            "customer_id": str(r["customer.id"]),
            "name": r["customer.descriptive_name"],
            "currency": r["customer.currency_code"],
            "time_zone": r["customer.time_zone"],
            "manager": r["customer.manager"],
            "test_account": r["customer.test_account"],
            "status": r["customer.status"],
            "auto_tagging_enabled": r["customer.auto_tagging_enabled"],
        }

    # -- writes ---------------------------------------------------------------
    def mutate(self, customer_id: str, operations: list[Any], *, validate_only: bool,
               partial_failure: bool = False) -> MutateResult:
        """GoogleAdsService.Mutate. Operations are MutateOperation messages. Batches of up to 5,000 ops.

        Atomic (partial_failure=False) requests are never split: temp resource names must resolve in one call.
        """
        cid = clean_id(customer_id)
        if not operations:
            return MutateResult(validate_only=validate_only)
        chunks = [operations] if not partial_failure else [operations[i:i + 5000] for i in range(0, len(operations), 5000)]
        result = MutateResult(validate_only=validate_only)
        offset = 0
        for chunk in chunks:
            def run(chunk=chunk) -> Any:
                svc = self.service("GoogleAdsService")
                req = self.type("MutateGoogleAdsRequest")
                req.customer_id = cid
                req.mutate_operations.extend(chunk)
                req.validate_only = validate_only
                req.partial_failure = partial_failure
                return svc.mutate(request=req)

            _RequestIdCapture.local.request_id = None
            resp = self._retry(run, f"mutate({cid})")
            result.request_id = getattr(_RequestIdCapture.local, "request_id", None) or result.request_id
            for r in resp.mutate_operation_responses:
                result.resource_names.append(_response_resource_name(r))
            if partial_failure and resp.partial_failure_error and resp.partial_failure_error.code:
                failure_type = type(self.type("GoogleAdsFailure"))
                for detail in resp.partial_failure_error.details:
                    failure = failure_type.deserialize(detail.value)
                    for e in describe_failure(failure):
                        if "operation_index" in e:
                            e["operation_index"] += offset
                        result.partial_failures.append(e)
            offset += len(chunk)
        return result

    def apply_recommendation(self, customer_id: str, resource_names: list[str], partial_failure: bool = True) -> MutateResult:
        cid = clean_id(customer_id)

        def run() -> Any:
            svc = self.service("RecommendationService")
            ops = []
            for rn in resource_names:
                op = self.type("ApplyRecommendationOperation")
                op.resource_name = rn
                ops.append(op)
            return svc.apply_recommendation(customer_id=cid, operations=ops, partial_failure=partial_failure)

        _RequestIdCapture.local.request_id = None
        resp = self._retry(run, f"apply_recommendation({cid})")
        out = MutateResult(resource_names=[r.resource_name for r in resp.results],
                           request_id=getattr(_RequestIdCapture.local, "request_id", None))
        if resp.partial_failure_error and resp.partial_failure_error.code:
            failure_type = type(self.type("GoogleAdsFailure"))
            for detail in resp.partial_failure_error.details:
                out.partial_failures.extend(describe_failure(failure_type.deserialize(detail.value)))
        return out


def _response_resource_name(resp: Any) -> str | None:
    pb = type(resp).pb(resp)
    which = pb.WhichOneof("response")
    if not which:
        return None
    return getattr(getattr(resp, which), "resource_name", None) or None


_default: Gads | None = None


def get_gads() -> Gads:
    global _default
    if _default is None:
        _default = Gads()
    return _default


def set_gads(gads: Gads | None) -> None:
    """Swap the process-wide client (tests)."""
    global _default
    _default = gads
