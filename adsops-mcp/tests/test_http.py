from starlette.testclient import TestClient


def test_bearer_auth_blocks_and_allows():
    import server
    mcp = server.build_server()
    app = server.BearerAuth(mcp.streamable_http_app(streamable_http_path="/mcp"), "s3cret")
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
        r = c.post("/mcp", json=init, headers={"Authorization": "Bearer s3cret",
                                               "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200 and "adsops-mcp" in r.text
