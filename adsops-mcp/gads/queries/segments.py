"""GAQL builders for segment-level waste analysis (placements, devices, locations, hours)."""
from __future__ import annotations

from core.dates import DateRange

M = ("metrics.cost_micros, metrics.impressions, metrics.clicks, "
     "metrics.conversions, metrics.conversions_value")

GEO_SEGMENT = {
    "country": "segments.geo_target_country",
    "region": "segments.geo_target_region",
    "city": "segments.geo_target_city",
}


def placements(dr: DateRange) -> str:
    return (
        "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, detail_placement_view.display_name, "
        "detail_placement_view.placement, detail_placement_view.placement_type, detail_placement_view.target_url, "
        f"{M} FROM detail_placement_view WHERE {dr.gaql()} AND metrics.cost_micros > 0"
    )


def devices(dr: DateRange) -> str:
    return (f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type, segments.device, {M} "
            f"FROM campaign WHERE {dr.gaql()} AND campaign.status != 'REMOVED' AND metrics.cost_micros > 0")


def hours(dr: DateRange) -> str:
    return (f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type, segments.hour, {M} "
            f"FROM campaign WHERE {dr.gaql()} AND campaign.status != 'REMOVED' AND metrics.cost_micros > 0")


def locations(dr: DateRange, level: str = "region") -> str:
    seg = GEO_SEGMENT[level]
    return (f"SELECT campaign.id, campaign.name, campaign.advertising_channel_type, geographic_view.location_type, "
            f"{seg}, {M} FROM geographic_view WHERE {dr.gaql()} AND metrics.cost_micros > 0")


def geo_names(resource_names: list[str]) -> str:
    inner = ", ".join(f"'{rn}'" for rn in resource_names)
    return ("SELECT geo_target_constant.resource_name, geo_target_constant.name, geo_target_constant.canonical_name, "
            f"geo_target_constant.target_type FROM geo_target_constant WHERE geo_target_constant.resource_name IN ({inner})")
