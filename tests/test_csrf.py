"""CSRF Origin/Referer guard (app.main.csrf_origin_guard)."""


def test_cross_origin_post_is_blocked(client):
    r = client.post("/logout", headers={"Origin": "http://evil.example"},
                    follow_redirects=False)
    assert r.status_code == 403


def test_same_origin_post_is_allowed(client):
    # TestClient's Host is "testserver"; a matching Origin passes through.
    r = client.post("/logout", headers={"Origin": "http://testserver"},
                    follow_redirects=False)
    assert r.status_code == 303  # logout redirect, not blocked


def test_missing_origin_falls_through(client):
    # Non-browser clients send no Origin/Referer — allowed (SameSite=Lax backstop).
    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303


def test_forwarded_host_match_is_allowed(client):
    # A reverse proxy that rewrites Host but sets X-Forwarded-Host must not
    # 403 a legitimate same-site post.
    r = client.post("/logout",
                    headers={"Origin": "https://pool.example.com",
                             "X-Forwarded-Host": "pool.example.com",
                             "Host": "internal-backend:8000"},
                    follow_redirects=False)
    assert r.status_code == 303


def test_cross_origin_referer_only_is_blocked(client):
    r = client.post("/logout", headers={"Referer": "http://evil.example/x"},
                    follow_redirects=False)
    assert r.status_code == 403
