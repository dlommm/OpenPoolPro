def test_manifest_and_sw_served(client):
    assert client.get("/manifest.json").status_code == 200
    assert client.get("/sw.js").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200
