import anyio

EXPECTED = {"weekly_review", "wasted_spend_cleanup", "new_search_campaign", "new_pmax_campaign", "pmax_troubleshooting",
            "conversion_tracking_audit", "budget_pacing_check", "search_term_mining", "client_report"}


def test_all_playbooks_present_and_reference_real_tools():
    import re
    import server
    server.build_server()
    from skills.prompts import playbooks, render
    from tools._registry import TOOLS
    books = playbooks()
    assert set(books) == EXPECTED
    for name in books:
        text = render(name, "1234567890", "en", "sofas")
        assert "1234567890" in text and "English" in text and "{customer_id}" not in text
        for called in set(re.findall(r"`([a-z_]+)\(", text)):
            assert called in TOOLS, f"{name} references unknown tool {called}"


def test_prompts_registered():
    import server
    m = server.build_server()

    async def go():
        names = {p.name for p in await m.list_prompts()}
        assert EXPECTED <= names
        res = await m.get_prompt("weekly_review", {"customer_id": "stryi", "lang": "uk"})
        assert "Ukrainian" in res.messages[0].content.text
    anyio.run(go)
