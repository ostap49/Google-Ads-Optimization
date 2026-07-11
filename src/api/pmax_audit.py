"""PMax asset automation audit (read-only).

Checks the five asset automation settings Google enables by default on
Performance Max campaigns. The compliance standard is all five OPTED_OUT
(manual asset control). Based on the pmax-asset-automation skill.
"""

import logging
from typing import Any, Dict, List

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException

logger = logging.getLogger(__name__)

# The five settings, in display order. Google's default for each is OPTED_IN.
AUTOMATION_TYPES = {
    "TEXT_ASSET_AUTOMATION": "Auto text (headlines/descriptions)",
    "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION": "Final URL expansion",
    "GENERATE_ENHANCED_YOUTUBE_VIDEOS": "Auto videos",
    "GENERATE_IMAGE_ENHANCEMENT": "Image enhancement (crop)",
    "GENERATE_IMAGE_EXTRACTION": "Image extraction from URLs",
}

PMAX_QUERY = """
    SELECT
        campaign.id,
        campaign.name,
        campaign.asset_automation_settings
    FROM campaign
    WHERE
        campaign.advertising_channel_type = 'PERFORMANCE_MAX'
        AND campaign.status = 'ENABLED'
"""


def audit_account(client: GoogleAdsClient, customer_id: str) -> List[Dict[str, Any]]:
    """Audit all enabled PMax campaigns in one account.

    Returns one dict per campaign with each setting's status and a list of
    flagged (not OPTED_OUT) settings. Raises GoogleAdsException on API errors.
    """
    service = client.get_service("GoogleAdsService")
    response = service.search_stream(
        customer_id=customer_id.replace("-", ""), query=PMAX_QUERY
    )

    campaigns = []
    for batch in response:
        for row in batch.results:
            # Google's default is OPTED_IN; settings absent from the response
            # are still at that default.
            settings = {t: "OPTED_IN" for t in AUTOMATION_TYPES}
            for s in row.campaign.asset_automation_settings:
                # Enum values newer than the client library arrive as raw
                # ints without .name — skip them.
                if not hasattr(s.asset_automation_type, "name") or not hasattr(
                    s.asset_automation_status, "name"
                ):
                    continue
                t = s.asset_automation_type.name
                if t in settings:
                    settings[t] = s.asset_automation_status.name

            flagged = [t for t, st in settings.items() if st != "OPTED_OUT"]
            campaigns.append(
                {
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "settings": settings,
                    "flagged": flagged,
                    "compliant": not flagged,
                }
            )

    logger.info(
        "PMax audit: %d campaign(s) in account %s", len(campaigns), customer_id
    )
    return campaigns
