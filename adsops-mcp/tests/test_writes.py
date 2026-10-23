"""Write specs build real v26 MutateOperations; validation, guardrails and rollbacks per kind."""
import pytest

from gads.client import MutateResult
from safety import engine
from safety.guard import GuardrailError

CID = "1234567890"


@pytest.fixture(autouse=True)
def _specs():
    import tools.writes  # noqa: F401


def _ops(fake):
    return fake.mutates[-1]["ops"]


def _which(m):
    pb = type(m).pb(m)
    return pb.WhichOneof("operation")


def test_negatives_campaign_level_skips_existing_and_rolls_back(fake):
    fake.route(r"FROM campaign WHERE campaign.id IN", [{"campaign.id": 111, "campaign.name": "S", "campaign.advertising_channel_type": "SEARCH"}])
    fake.route(r"FROM campaign_criterion WHERE", [{"campaign.id": 111, "campaign_criterion.keyword.text": "безкоштовно",
                                                   "campaign_criterion.keyword.match_type": "PHRASE"}])
    fake.route(r"FROM search_term_view", [{"search_term_view.search_term": "диван вакансії", "campaign.id": 111,
                                           "metrics.cost_micros": 50_000_000, "metrics.clicks": 5, "metrics.conversions": 0}])
    p = engine.propose("add_negative_keywords", CID, {"level": "campaign", "campaign_ids": ["111"],
                                                      "keywords": ['"безкоштовно"', "вакансії", "[диван бу]"]})
    assert [d["after"] for d in p["diff"]] == ["вакансії [EXACT]", "диван бу [EXACT]"]
    assert p["impact"]["blocked_cost_30d"] == 0  # exact "вакансії" does not match "диван вакансії"
    ops = _ops(fake)
    assert len(ops) == 2 and ops[0].campaign_criterion_operation.create.negative is True
    fake.mutate_result = lambda ops, v: MutateResult(
        resource_names=[] if v else [f"customers/{CID}/campaignCriteria/111~{i}" for i in range(len(ops))], request_id="r")
    out = engine.apply(p["proposal_id"])
    rb = engine._load(out["proposal_id"])["rollback"]
    assert rb["kind"] == "remove_created" and len(rb["params"]["resource_names"]) == 2
    engine.rollback(p["proposal_id"])
    assert _which(_ops(fake)[0]) == "campaign_criterion_operation" and _ops(fake)[0].campaign_criterion_operation.remove


def test_remove_created_refuses_campaigns(fake):
    with pytest.raises(GuardrailError):
        engine.propose("remove_created", CID, {"resource_names": [f"customers/{CID}/campaigns/111"]})


def test_account_level_negatives_three_step(fake):
    fake.route(r"FROM shared_set WHERE", [])
    fake.route(r"FROM customer_negative_criterion", [])
    p = engine.propose("add_negative_keywords", CID, {"level": "account", "keywords": ["torrent", "crack"]})
    kinds = [_which(m) for m in _ops(fake)]
    assert kinds == ["shared_set_operation", "shared_criterion_operation", "shared_criterion_operation",
                     "customer_negative_criterion_operation"]
    assert fake.mutates[-1]["partial_failure"] is False
    assert _ops(fake)[3].customer_negative_criterion_operation.create.negative_keyword_list.shared_set.endswith("/sharedSets/-1")
    assert any("3-step" in w for w in p["warnings"])


def test_add_keywords_dedup_and_warnings(fake):
    from memory.brief import update_brief
    update_brief(CID, {"competitor_terms": ["rozetka"]})
    fake.route(r"FROM ad_group WHERE ad_group.id", [{"ad_group.id": 5, "ad_group.name": "AG", "ad_group.status": "ENABLED",
                                                     "ad_group.cpc_bid_micros": 10_000_000, "campaign.id": 1, "campaign.name": "C",
                                                     "campaign.advertising_channel_type": "SEARCH"}])
    fake.route(r"FROM ad_group_criterion WHERE ad_group.id", [
        {"ad_group_criterion.keyword.text": "диван", "ad_group_criterion.keyword.match_type": "PHRASE", "ad_group_criterion.negative": False},
        {"ad_group_criterion.keyword.text": "бу", "ad_group_criterion.keyword.match_type": "PHRASE", "ad_group_criterion.negative": True}])
    p = engine.propose("add_keywords", CID, {"ad_group_id": "5", "keywords": ["диван", "[диван rozetka]", {"text": "диван бу", "cpc_bid": 50}]})
    assert len(p["diff"]) == 2
    w = " ".join(p["warnings"])
    assert "competitor" in w and "negative" in w and ">3x" in w
    assert _ops(fake)[1].ad_group_criterion_operation.create.cpc_bid_micros == 50_000_000
    with pytest.raises(ValueError):
        engine.propose("add_keywords", CID, {"ad_group_id": "5", "keywords": ["bad!keyword"]})


def test_set_status_and_rollback(fake):
    fake.route(r"FROM campaign WHERE campaign.id IN", [
        {"campaign.id": 1, "campaign.name": "A", "campaign.status": "ENABLED", "campaign_budget.amount_micros": 1},
        {"campaign.id": 2, "campaign.name": "B", "campaign.status": "PAUSED", "campaign_budget.amount_micros": 1}])
    p = engine.propose("set_status", CID, {"entity_type": "campaign", "ids": ["1", "2"], "status": "PAUSED"})
    assert len(p["diff"]) == 1 and p["diff"][0]["before"] == "ENABLED"
    op = _ops(fake)[0].campaign_operation
    assert list(op.update_mask.paths) == ["status"]
    assert engine._load(p["proposal_id"])["rollback"] == {"kind": "set_status", "params": {"entity_type": "campaign", "ids": ["1"], "status": "ENABLED"}}
    with pytest.raises(ValueError):
        engine.propose("set_status", CID, {"entity_type": "campaign", "ids": ["1"], "status": "REMOVED"})


def test_rsa_validation_cyrillic_and_policy(fake):
    fake.route(r"FROM ad_group WHERE ad_group.id", [{"ad_group.id": 5, "ad_group.name": "AG", "campaign.id": 1, "campaign.name": "C",
                                                     "campaign.advertising_channel_type": "SEARCH"}])
    good_h = [f"Диван модель {i}" for i in range(14)] + [{"text": "Магазин Стрий", "pin": 1}]
    good_d = ["Безкоштовна доставка по Україні. Гарантія 2 роки.", "Понад 500 моделей диванів у наявності.",
              "Розстрочка 0% на 10 місяців.", "Виготовлення під замовлення за 14 днів."]
    engine.propose("create_rsa", CID, {"ad_group_id": "5", "headlines": good_h, "descriptions": good_d,
                                           "final_url": "https://example.ua/dyvany", "path1": "дивани", "path2": "кутові"})
    ad = _ops(fake)[0].ad_group_ad_operation.create
    assert ad.status.name == "PAUSED" and len(ad.ad.responsive_search_ad.headlines) == 15
    assert ad.ad.responsive_search_ad.headlines[14].pinned_field.name == "HEADLINE_1"
    with pytest.raises(ValueError) as ex:
        engine.propose("create_rsa", CID, {"ad_group_id": "5", "final_url": "https://x.ua",
                                           "headlines": ["Купуйте найкращі дивани у Львові", "Знижки!", "Знижки!", "{KeyWord:Дивани} Львів"],
                                           "descriptions": ["Опис 🙂", "ok ok"]})
    msg = str(ex.value)
    assert "is 32 chars" in msg and "'!'" in msg and "duplicate" in msg and "emoji" in msg
    assert "{KeyWord" not in msg  # 'Дивани Львів' fits


def test_sitelinks_callouts_snippets(fake):
    fake.route(r"FROM campaign WHERE campaign.id IN", [{"campaign.id": 1, "campaign.name": "C"}])
    engine.propose("create_assets", CID, {"asset_type": "sitelink", "level": "campaign", "campaign_ids": ["1"], "items": [
        {"text": "Кутові дивани", "description1": "Понад 100 моделей", "description2": "Доставка 1-3 дні", "final_url": "https://x.ua/a"},
        {"text": "Акції", "final_url": "https://x.ua/b"}]})
    kinds = [_which(m) for m in _ops(fake)]
    assert kinds == ["asset_operation", "asset_operation", "campaign_asset_operation", "campaign_asset_operation"]
    assert _ops(fake)[2].campaign_asset_operation.create.asset == _ops(fake)[0].asset_operation.create.resource_name
    engine.propose("create_assets", CID, {"asset_type": "callout", "level": "account", "items": ["Доставка 0 грн", "Гарантія 24 міс"]})
    assert _which(_ops(fake)[-1]) == "customer_asset_operation"
    with pytest.raises(ValueError, match="header"):
        engine.propose("create_assets", CID, {"asset_type": "structured_snippet", "level": "account",
                                              "items": [{"header": "Бренди", "values": ["a", "b", "c"]}]})


def _png(path, w, h):
    from PIL import Image
    Image.new("RGB", (w, h), (200, 30, 30)).save(path)
    return str(path)


def test_create_search_campaign_atomic_paused(fake):
    fake.route(r"campaign.name = ", [])
    rsa = {"headlines": [f"Заголовок {i}" for i in range(3)], "descriptions": ["Опис один.", "Опис два."], "final_url": "https://x.ua"}
    engine.propose("create_search_campaign", CID, {
        "name": "UA | Дивани | Search", "daily_budget": 500, "bidding": {"strategy": "TARGET_CPA", "target_cpa": 300},
        "geo": ["UA"], "languages": ["uk", "ru"], "negative_keywords": ["безкоштовно"],
        "ad_groups": [{"name": "Кутові", "keywords": ["кутовий диван", "[кутові дивани]"], "rsa": rsa}]})
    ops = _ops(fake)
    assert fake.mutates[-1]["partial_failure"] is False
    camp = ops[1].campaign_operation.create
    assert camp.status.name == "PAUSED" and camp.contains_eu_political_advertising.name == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert camp.network_settings.target_content_network is False and camp.geo_target_type_setting.positive_geo_target_type.name == "PRESENCE"
    assert camp.maximize_conversions.target_cpa_micros == 300_000_000
    assert camp.campaign_budget == ops[0].campaign_budget_operation.create.resource_name
    kinds = [_which(m) for m in ops]
    assert kinds.count("campaign_criterion_operation") == 4 and kinds.count("ad_group_criterion_operation") == 2
    assert kinds[-1] == "ad_group_ad_operation"
    from core.config import get_settings  # max_daily_budget guard
    import os, yaml
    with open(os.environ["ADSOPS_CONFIG"], "w") as f:
        yaml.safe_dump({"defaults": {"max_daily_budget": 400}}, f)
    get_settings.cache_clear()
    with pytest.raises(GuardrailError):
        engine.propose("create_search_campaign", CID, {"name": "X", "daily_budget": 500, "ad_groups": [{"name": "a", "keywords": ["x"]}]})


def test_create_pmax_with_images_and_feed(fake, tmp_path):
    fake.route(r"campaign.name = ", [])
    ag = {"name": "AG1", "final_url": "https://x.ua", "business_name": "Stryi Mebli",
          "headlines": ["Дивани", "Дивани зі знижкою", "Меблі Стрий"], "long_headlines": ["Дивани від виробника з доставкою по Україні"],
          "descriptions": ["Безкоштовна доставка.", "Гарантія 2 роки на всі дивани та крісла."],
          "marketing_images": [_png(tmp_path / "m.png", 1200, 628)], "square_images": [_png(tmp_path / "s.png", 1200, 1200)],
          "logos": [_png(tmp_path / "l.png", 512, 512)], "youtube_video_ids": ["dQw4w9WgXcQ"]}
    p = engine.propose("create_pmax_campaign", CID, {"name": "PMax | Всі товари", "daily_budget": 800, "merchant_center_id": "123456",
                                                     "bidding": {"strategy": "TARGET_ROAS", "target_roas": 5},
                                                     "asset_group": ag, "audience_signal": {"search_themes": ["купити диван"]},
                                                     "asset_automation_opt_out": ["TEXT_ASSET_AUTOMATION"]})
    assert p["status"] == "validated", p
    ops = _ops(fake)
    kinds = [_which(m) for m in ops]
    camp = ops[1].campaign_operation.create
    assert camp.advertising_channel_type.name == "PERFORMANCE_MAX" and camp.shopping_setting.merchant_id == 123456
    assert camp.maximize_conversion_value.target_roas == 5.0 and camp.asset_automation_settings[0].asset_automation_status.name == "OPTED_OUT"
    assert "asset_group_listing_group_filter_operation" in kinds and "asset_group_signal_operation" in kinds
    assert kinds.count("asset_group_asset_operation") == 3 + 1 + 2 + 1 + 3 + 1  # text + business name + images + video
    assert kinds.index("asset_group_operation") > max(i for i, k in enumerate(kinds) if k == "asset_operation")
    with pytest.raises(ValueError, match="aspect"):
        engine.propose("create_pmax_campaign", CID, {"name": "Bad", "daily_budget": 100,
                                                     "asset_group": {**ag, "marketing_images": [_png(tmp_path / "b.png", 1000, 1000)]}})


def test_create_demand_gen(fake, tmp_path):
    fake.route(r"campaign.name = ", [])
    ad = {"headlines": ["Нова колекція"], "descriptions": ["Дивани для дому."], "business_name": "Stryi",
          "final_url": "https://x.ua", "marketing_images": [_png(tmp_path / "m.png", 1200, 628)], "logos": [_png(tmp_path / "l.png", 300, 300)]}
    engine.propose("create_demand_gen_campaign", CID, {"name": "DG | Ремаркетинг", "daily_budget": 300, "ad": ad})
    ops = _ops(fake)
    assert ops[1].campaign_operation.create.advertising_channel_type.name == "DEMAND_GEN"
    dg = ops[-1].ad_group_ad_operation.create.ad.demand_gen_multi_asset_ad
    assert len(dg.marketing_images) == 1 and len(dg.logo_images) == 1 and dg.business_name == "Stryi"


def test_exclusions(fake):
    fake.route(r"FROM campaign WHERE campaign.id IN", [{"campaign.id": 1, "campaign.name": "Display", "campaign.advertising_channel_type": "DISPLAY"},
                                                       {"campaign.id": 2, "campaign.name": "Search", "campaign.advertising_channel_type": "SEARCH"}])
    engine.propose("add_exclusions", CID, {"exclusion_type": "placement", "level": "account",
                                           "values": ["https://games.example.com/", "UCabcdefghijklmnopqrstuv", "mobileapp::1-com.game.x"]})
    crits = [m.customer_negative_criterion_operation.create for m in _ops(fake)]
    assert crits[0].placement.url == "games.example.com" and crits[1].youtube_channel.channel_id == "UCabcdefghijklmnopqrstuv"
    assert crits[2].mobile_application.app_id == "1-com.game.x"
    with pytest.raises(ValueError, match="not supported"):
        engine.propose("add_exclusions", CID, {"exclusion_type": "placement", "campaign_ids": ["2"], "values": ["x.com"]})

    fake.route(r"campaign_criterion.type = 'DEVICE'", [{"campaign.id": 2, "campaign_criterion.criterion_id": 30001,
                                                       "campaign_criterion.device.type": "MOBILE", "campaign_criterion.bid_modifier": 1.0}])
    with pytest.raises(GuardrailError):
        engine.propose("add_exclusions", CID, {"exclusion_type": "device", "campaign_ids": ["2"], "values": ["MOBILE"], "bid_modifier": 0.5})
    p = engine.propose("add_exclusions", CID, {"exclusion_type": "device", "campaign_ids": ["2"], "values": ["MOBILE", "TABLET"], "bid_modifier": 0})
    ops = _ops(fake)
    assert ops[0].campaign_criterion_operation.update.resource_name.endswith("2~30001")
    assert ops[1].campaign_criterion_operation.create.device.type_.name == "TABLET"
    assert engine._load(p["proposal_id"])["rollback"]["params"]["device_modifiers"] == {"2": {"MOBILE": 1.0, "TABLET": 1.0}}

    fake.route(r"campaign_criterion.type = 'AD_SCHEDULE'", [])
    p = engine.propose("add_exclusions", CID, {"exclusion_type": "ad_schedule", "campaign_ids": ["2"], "hours": [0, 1, 2, 3, 4, 5]})
    assert len(_ops(fake)) == 7 and _ops(fake)[0].campaign_criterion_operation.create.ad_schedule.start_hour == 6
    fake.route(r"campaign_criterion.type = 'AD_SCHEDULE'", [{"campaign.id": 2, "campaign_criterion.criterion_id": 9,
        "campaign_criterion.ad_schedule.day_of_week": "MONDAY", "campaign_criterion.ad_schedule.start_hour": 8,
        "campaign_criterion.ad_schedule.end_hour": 20, "campaign_criterion.bid_modifier": None}])
    with pytest.raises(ValueError, match="replace_existing"):
        engine.propose("add_exclusions", CID, {"exclusion_type": "ad_schedule", "campaign_ids": ["2"], "hours": [23]})
    p = engine.propose("add_exclusions", CID, {"exclusion_type": "ad_schedule", "campaign_ids": ["2"], "hours": [23], "replace_existing": True})
    assert _which(_ops(fake)[0]) == "campaign_criterion_operation" and _ops(fake)[0].campaign_criterion_operation.remove.endswith("2~9")
    assert engine._load(p["proposal_id"])["rollback"]["params"]["entries_by_campaign"]["2"][0]["start_hour"] == 8


def test_apply_recommendation_by_id(fake):
    rn = f"customers/{CID}/recommendations/abc"
    fake.route(r"FROM recommendation WHERE recommendation.resource_name IN", [
        {"recommendation.resource_name": rn, "recommendation.type": "SITELINK_ASSET", "recommendation.campaign": f"customers/{CID}/campaigns/1",
         "recommendation.dismissed": False}])
    p = engine.propose("apply_recommendation", CID, {"recommendation_ids": ["abc"]})
    assert p["status"] == "validated" and p["reversible"] is False
    out = engine.apply(p["proposal_id"])
    assert out["status"] == "applied" and fake.mutates[-1]["recommendations"] == [rn]
