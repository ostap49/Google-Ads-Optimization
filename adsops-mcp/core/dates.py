"""Date range resolution in the account's own time zone.

Every range is resolved to explicit dates so GAQL always uses BETWEEN and the
previous period of equal length can be computed for period-over-period deltas.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PRESETS = (
    "TODAY", "YESTERDAY", "LAST_7_DAYS", "LAST_14_DAYS", "LAST_30_DAYS", "LAST_90_DAYS",
    "THIS_MONTH", "LAST_MONTH", "THIS_WEEK_MON_TODAY", "LAST_WEEK_MON_SUN", "THIS_YEAR",
)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def gaql(self, field: str = "segments.date") -> str:
        return f"{field} BETWEEN '{self.start.isoformat()}' AND '{self.end.isoformat()}'"

    def previous(self) -> "DateRange":
        """Equal-length window immediately before this one. LAST_MONTH/THIS_MONTH compare to the prior month span."""
        if self.label == "LAST_MONTH":
            end = self.start - timedelta(days=1)
            return DateRange(end.replace(day=1), end, "PREVIOUS_MONTH")
        end = self.start - timedelta(days=1)
        return DateRange(end - timedelta(days=self.days - 1), end, f"PREVIOUS_{self.days}_DAYS")

    def as_dict(self) -> dict[str, str | int]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "days": self.days, "label": self.label}


def today_in(tz: str | None) -> date:
    try:
        return datetime.now(ZoneInfo(tz)).date() if tz else date.today()
    except Exception:
        return date.today()


def resolve(date_range: str | None = "LAST_30_DAYS", tz: str | None = None,
            start: str | None = None, end: str | None = None) -> DateRange:
    """Resolve a preset or custom range.

    Accepts: a preset name, "YYYY-MM-DD..YYYY-MM-DD", "YYYY-MM-DD,YYYY-MM-DD", or explicit start/end.
    """
    t = today_in(tz)
    if start and end:
        return _custom(start, end)
    spec = (date_range or "LAST_30_DAYS").strip()
    for sep in ("..", ","):
        if sep in spec:
            a, b = spec.split(sep, 1)
            return _custom(a.strip(), b.strip())
    p = spec.upper()
    y = t - timedelta(days=1)
    if p == "TODAY":
        return DateRange(t, t, p)
    if p == "YESTERDAY":
        return DateRange(y, y, p)
    if p.startswith("LAST_") and p.endswith("_DAYS"):
        n = int(p[5:-5])
        return DateRange(y - timedelta(days=n - 1), y, p)
    if p == "THIS_MONTH":
        return DateRange(t.replace(day=1), t, p)
    if p == "LAST_MONTH":
        last = t.replace(day=1) - timedelta(days=1)
        return DateRange(last.replace(day=1), last, p)
    if p == "THIS_WEEK_MON_TODAY":
        return DateRange(t - timedelta(days=t.weekday()), t, p)
    if p == "LAST_WEEK_MON_SUN":
        mon = t - timedelta(days=t.weekday() + 7)
        return DateRange(mon, mon + timedelta(days=6), p)
    if p == "THIS_YEAR":
        return DateRange(t.replace(month=1, day=1), t, p)
    raise ValueError(f"Unknown date range '{date_range}'. Use one of {PRESETS} or 'YYYY-MM-DD..YYYY-MM-DD'.")


def _custom(a: str, b: str) -> DateRange:
    s, e = date.fromisoformat(a), date.fromisoformat(b)
    if e < s:
        s, e = e, s
    return DateRange(s, e, "CUSTOM")
