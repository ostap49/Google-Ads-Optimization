import monitor


def test_monitor_dedup_and_never_mutates(auto, monkeypatch, capsys):
    auto.route("FROM customer_client", [{"customer_client.id": 1234567890, "customer_client.descriptive_name": "Shop",
                                         "customer_client.manager": False, "customer_client.status": "ENABLED",
                                         "customer_client.hidden": False, "customer_client.level": 1, "customer_client.currency_code": "UAH",
                                         "customer_client.time_zone": "Europe/Kyiv", "customer_client.test_account": False}])
    monkeypatch.setattr(monitor, "check_anomalies", lambda cid: [{"severity": "high", "key": "a", "text": "spend up 80%"}])
    sent = []
    monkeypatch.setattr(monitor, "send_telegram", lambda text: sent.append(text))
    out = monitor.run(None, ["anomalies", "pacing", "conversions"], dry=False, force=False)
    assert out["alerts"] >= 1 and "spend up 80%" in sent[0]
    again = monitor.run(None, ["anomalies"], dry=False, force=False)
    assert again["alerts"] == 0  # de-duplicated for today
    assert auto.mutates == []
