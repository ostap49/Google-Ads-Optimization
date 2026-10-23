"""Exclusions: placements, locations, ad schedule, device bid adjustments."""
from __future__ import annotations

import re
from typing import Any

from safety.specs import ChangeSpec, Prepared, enum, mop, register, rn_campaign

DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")
DEVICES = ("MOBILE", "DESKTOP", "TABLET")
NO_DEVICE_MODIFIERS = {"PERFORMANCE_MAX", "DEMAND_GEN", "SMART", "VIDEO"}


def parse_placement(value: str) -> dict[str, str]:
    v = value.strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", v) or re.fullmatch(r"(UC[\w-]{22})", v) \
        or re.fullmatch(r"youtube_channel::(.+)", v)
    if m:
        return {"type": "youtube_channel", "id": m.group(1)}
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})", v) or re.fullmatch(r"youtube_video::(.+)", v)
    if m:
        return {"type": "youtube_video", "id": m.group(1)}
    m = re.fullmatch(r"mobileapp::(.+)", v) or re.fullmatch(r"([12]-[\w.]+)", v)
    if m:
        return {"type": "mobile_application", "id": m.group(1)}
    url = re.sub(r"^https?://", "", v).rstrip("/")
    if not url or " " in url:
        raise ValueError(f"Cannot parse placement '{value}'")
    return {"type": "placement", "id": url}


def schedule_blocks(hours: list[int], mode: str, bid_modifier: float | None, days: list[str]) -> list[dict[str, Any]]:
    """Full-week schedule that excludes (or bid-adjusts) the given hours on `days`. Max 6 blocks per day."""
    hs = {int(h) for h in hours}
    if not hs or not hs <= set(range(24)):
        raise ValueError("hours must be integers 0..23")
    if mode not in ("exclude", "bid_modifier"):
        raise ValueError("mode must be 'exclude' or 'bid_modifier'")
    if mode == "bid_modifier" and bid_modifier is None:
        raise ValueError("mode='bid_modifier' needs bid_modifier (0.1..10)")

    def val(h: int) -> Any:
        if h in hs:
            return "X" if mode == "exclude" else float(bid_modifier)
        return 1.0

    out = []
    for day in DAYS:
        if day not in days:
            out.append({"day": day, "start_hour": 0, "end_hour": 24, "bid_modifier": None})
            continue
        blocks, start = [], 0
        for h in range(1, 25):
            if h == 24 or val(h) != val(start):
                v = val(start)
                if v != "X":
                    blocks.append({"day": day, "start_hour": start, "end_hour": h, "bid_modifier": None if v == 1.0 else v})
                start = h
        if len(blocks) > 6:
            raise ValueError(f"{day}: {len(blocks)} schedule blocks needed, Google allows 6 per day — group hours into fewer ranges")
        out += blocks
    return out


@register
class AddExclusions(ChangeSpec):
    kind = "add_exclusions"

    def prepare(self, g, cid, params, guard):
        et = params.get("exclusion_type")
        level = params.get("level", "campaign")
        camp_ids = [str(int(c)) for c in params.get("campaign_ids") or ([params["campaign_id"]] if params.get("campaign_id") else [])]
        camps = {}
        if camp_ids:
            camps = {str(x["campaign.id"]): x for x in g.search(cid, (
                "SELECT campaign.id, campaign.name, campaign.advertising_channel_type FROM campaign "
                f"WHERE campaign.id IN ({', '.join(camp_ids)})"))}
            missing = [c for c in camp_ids if c not in camps]
            if missing:
                raise ValueError(f"Campaigns not found: {missing}")
        elif level != "account":
            raise ValueError("campaign_ids required (or level='account' for placements)")
        label = lambda c: f"campaign {c} ({camps[c]['campaign.name']})"  # noqa: E731
        diff: list[dict[str, Any]] = []
        p: dict[str, Any] = {"exclusion_type": et, "level": level, "campaign_ids": camp_ids}
        rollback = None

        if et == "placement":
            items = [parse_placement(v) for v in params.get("values") or []]
            if not items:
                raise ValueError("values (placements) required")
            if level == "campaign":
                bad = [c for c in camp_ids if camps[c]["campaign.advertising_channel_type"] in ("PERFORMANCE_MAX", "SEARCH")]
                if bad:
                    raise ValueError(f"Campaign-level placement exclusions are not supported for {bad} (PMax/Search) — use level='account'.")
            p["items"] = items
            targets = [label(c) for c in camp_ids] if level == "campaign" else ["account (all campaigns incl. PMax)"]
            diff = [{"resource": t, "field": "excluded_placement", "before": None, "after": f"{i['type']}: {i['id']}"}
                    for t in targets for i in items]
        elif et == "location":
            from tools.planning import geo_resources
            geos = geo_resources(g, params.get("values") or [])
            p["geos"] = geos
            diff = [{"resource": label(c), "field": "excluded_location", "before": None, "after": geo} for c in camp_ids for geo in geos]
        elif et == "device":
            mods = params.get("device_modifiers")
            if not mods:
                bm = params.get("bid_modifier")
                if bm is None:
                    raise ValueError("device needs bid_modifier (0 = exclude, 0.5 = -50%, 1.2 = +20%)")
                devs = [str(v).upper() for v in params.get("values") or []]
                if not devs or any(d not in DEVICES for d in devs):
                    raise ValueError(f"values must be devices from {DEVICES}")
                mods = {c: {d: float(bm) for d in devs} for c in camp_ids}
            for c in camp_ids:
                if camps[c]["campaign.advertising_channel_type"] in NO_DEVICE_MODIFIERS:
                    raise ValueError(f"{label(c)}: {camps[c]['campaign.advertising_channel_type']} does not support device bid adjustments")
            cur = g.search(cid, ("SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.device.type, "
                                 "campaign_criterion.bid_modifier, campaign_criterion.type FROM campaign_criterion "
                                 f"WHERE campaign_criterion.type = 'DEVICE' AND campaign.id IN ({', '.join(camp_ids)})"))
            existing = {(str(x["campaign.id"]), x["campaign_criterion.device.type"]): x for x in cur}
            changes, back = [], {}
            for c, dm in mods.items():
                for dev, new in dm.items():
                    x = existing.get((c, dev))
                    before = float(x["campaign_criterion.bid_modifier"]) if x and x.get("campaign_criterion.bid_modifier") else 1.0
                    if abs(before - float(new)) < 1e-9:
                        continue
                    if float(new) == 0:
                        guard.warn(f"{label(c)}: {dev} will be fully excluded (-100%).")
                    elif not 0.1 <= float(new) <= 10:
                        raise ValueError("bid_modifier must be 0 (exclude) or between 0.1 and 10")
                    else:
                        guard.check_change_pct(f"{label(c)} {dev} bid modifier", before, float(new), "max_bid_change_pct")
                    changes.append({"campaign_id": c, "device": dev, "bid_modifier": float(new),
                                    "criterion_rn": f"customers/{cid}/campaignCriteria/{c}~{x['campaign_criterion.criterion_id']}" if x else None})
                    back.setdefault(c, {})[dev] = before
                    diff.append({"resource": label(c), "field": f"device_bid_modifier.{dev}", "before": before, "after": float(new)})
            p["changes"] = changes
            rollback = {"kind": self.kind, "params": {"exclusion_type": "device", "campaign_ids": camp_ids, "device_modifiers": back}} if back else None
        elif et == "ad_schedule":
            entries = params.get("entries")
            if entries is None:
                entries = schedule_blocks(params.get("hours") or [], params.get("mode", "exclude"), params.get("bid_modifier"),
                                          [d.upper() for d in params.get("days") or DAYS])
            if params.get("mode", "exclude") != "exclude" and params.get("bid_modifier") is not None:
                if not 0.1 <= float(params["bid_modifier"]) <= 10:
                    raise ValueError("ad schedule bid_modifier must be between 0.1 (-90%) and 10")
            cur = g.search(cid, ("SELECT campaign.id, campaign_criterion.criterion_id, campaign_criterion.ad_schedule.day_of_week, "
                                 "campaign_criterion.ad_schedule.start_hour, campaign_criterion.ad_schedule.end_hour, "
                                 "campaign_criterion.ad_schedule.start_minute, campaign_criterion.ad_schedule.end_minute, "
                                 "campaign_criterion.bid_modifier, campaign_criterion.type FROM campaign_criterion "
                                 f"WHERE campaign_criterion.type = 'AD_SCHEDULE' AND campaign.id IN ({', '.join(camp_ids)})"))
            old: dict[str, list[dict[str, Any]]] = {}
            remove = []
            for x in cur:
                c = str(x["campaign.id"])
                old.setdefault(c, []).append({"day": x["campaign_criterion.ad_schedule.day_of_week"],
                                              "start_hour": x["campaign_criterion.ad_schedule.start_hour"],
                                              "end_hour": x["campaign_criterion.ad_schedule.end_hour"],
                                              "bid_modifier": x.get("campaign_criterion.bid_modifier") or None})
                remove.append(f"customers/{cid}/campaignCriteria/{c}~{x['campaign_criterion.criterion_id']}")
            if old and not params.get("replace_existing"):
                raise ValueError(f"Campaigns {sorted(old)} already have an ad schedule; pass replace_existing=True to replace it "
                                 f"(current: {old}).")
            per = params.get("entries_by_campaign") or {c: entries for c in camp_ids}
            p.update({"entries_by_campaign": per, "remove": remove})
            for c in camp_ids:
                served = sum((e["end_hour"] - e["start_hour"]) for e in per.get(c, []))
                diff.append({"resource": label(c), "field": "ad_schedule", "before": old.get(c) or "all hours",
                             "after": f"{served}h/week scheduled; " + "; ".join(
                                 f"{e['day'][:3]} {e['start_hour']:02d}-{e['end_hour']:02d}" + (f" x{e['bid_modifier']}" if e.get('bid_modifier') else "")
                                 for e in per.get(c, []) if e["start_hour"] != 0 or e["end_hour"] != 24 or e.get("bid_modifier"))[:600]})
            rollback = {"kind": self.kind, "params": {"exclusion_type": "ad_schedule", "campaign_ids": camp_ids,
                                                      "entries_by_campaign": old, "replace_existing": True}} if old else None
            p["restore_all_hours_on_rollback"] = not old
        else:
            raise ValueError("exclusion_type must be placement | location | device | ad_schedule")
        return Prepared(params=p, diff=diff, rollback=rollback, campaign_ids=camp_ids,
                        partial_failure=et in ("placement", "location"),
                        summary=f"{et} changes on {len(camp_ids) or 'account'} {'campaign(s)' if camp_ids else 'level'}")

    def build(self, g, cid, params):
        et, ops = params["exclusion_type"], []
        if et == "placement":
            for item in params["items"]:
                targets = params["campaign_ids"] if params["level"] == "campaign" else [None]
                for c in targets:
                    m = mop(g)
                    if c:
                        crit = m.campaign_criterion_operation.create
                        crit.campaign = rn_campaign(cid, c)
                        crit.negative = True
                    else:
                        crit = m.customer_negative_criterion_operation.create
                    if item["type"] == "placement":
                        crit.placement.url = item["id"]
                    elif item["type"] == "youtube_channel":
                        crit.youtube_channel.channel_id = item["id"]
                    elif item["type"] == "youtube_video":
                        crit.youtube_video.video_id = item["id"]
                    else:
                        crit.mobile_application.app_id = item["id"]
                    ops.append(m)
        elif et == "location":
            for c in params["campaign_ids"]:
                for geo in params["geos"]:
                    m = mop(g)
                    crit = m.campaign_criterion_operation.create
                    crit.campaign = rn_campaign(cid, c)
                    crit.negative = True
                    crit.location.geo_target_constant = geo
                    ops.append(m)
        elif et == "device":
            for ch in params["changes"]:
                m = mop(g)
                if ch["criterion_rn"]:
                    op = m.campaign_criterion_operation
                    op.update.resource_name = ch["criterion_rn"]
                    op.update.bid_modifier = ch["bid_modifier"]
                    op.update_mask.paths.append("bid_modifier")
                else:
                    crit = m.campaign_criterion_operation.create
                    crit.campaign = rn_campaign(cid, ch["campaign_id"])
                    crit.device.type_ = enum(g, "DeviceEnum", ch["device"])
                    crit.bid_modifier = ch["bid_modifier"]
                ops.append(m)
        elif et == "ad_schedule":
            for rn in params.get("remove") or []:
                m = mop(g)
                m.campaign_criterion_operation.remove = rn
                ops.append(m)
            for c, entries in params["entries_by_campaign"].items():
                for e in entries:
                    m = mop(g)
                    crit = m.campaign_criterion_operation.create
                    crit.campaign = rn_campaign(cid, c)
                    s = crit.ad_schedule
                    s.day_of_week = enum(g, "DayOfWeekEnum", e["day"])
                    s.start_hour = int(e["start_hour"])
                    s.end_hour = int(e["end_hour"])
                    s.start_minute = enum(g, "MinuteOfHourEnum", "ZERO")
                    s.end_minute = enum(g, "MinuteOfHourEnum", "ZERO")
                    if e.get("bid_modifier"):
                        crit.bid_modifier = float(e["bid_modifier"])
                    ops.append(m)
        return ops

    def rollback_from_result(self, cid, params, result):
        if params["exclusion_type"] in ("placement", "location"):
            names = [rn for rn in result.resource_names if rn]
            return {"kind": "remove_created", "params": {"resource_names": names}} if names else None
        if params["exclusion_type"] == "ad_schedule" and params.get("restore_all_hours_on_rollback"):
            created = [rn for rn in result.resource_names if rn and rn not in (params.get("remove") or [])]
            return {"kind": "remove_created", "params": {"resource_names": created}} if created else None
        return None
