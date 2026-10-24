"""First-run setup: no default owner, the setup code, the wizard steps, the
upgraded owner's profile step, and the owner reset link."""
import pyotp
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.config as config
from app import models, setup_flow
from app.auth_core import hash_password, verify_password
from app.main import create_app
from tests.conftest import make_owner

PW = "a-long-owner-pass"


def fresh(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "TIMEZONE", config.TIMEZONE)
    app = create_app(engine=engine)
    return app, TestClient(app)


def start(engine, monkeypatch, tmp_path):
    """A fresh install with the owner created through the wizard."""
    app, c = fresh(engine, monkeypatch, tmp_path)
    c.post("/setup/code", data={"code": app.state.setup_code})
    r = c.post("/setup/account", data={"name": "Alex Morgan", "username": "", "email": "alex@example.com",
                                       "password": PW, "password_confirm": PW}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup/security"
    return app, c


def test_fresh_install_sends_everything_to_setup(engine, monkeypatch, tmp_path):
    app, c = fresh(engine, monkeypatch, tmp_path)
    assert app.state.setup_code and len(app.state.setup_code) == 14
    for path in ("/", "/login", "/dashboard", "/customers"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    assert "Setup code" in c.get("/setup").text
    assert c.get("/health").status_code == 200


def test_setup_code_gate(engine, monkeypatch, tmp_path):
    app, c = fresh(engine, monkeypatch, tmp_path)
    assert c.get("/setup/account", follow_redirects=False).headers["location"] == "/setup"
    r = c.post("/setup/code", data={"code": "AAAA-BBBB-CCCC"})
    assert "match the one in the server log" in r.text
    r = c.post("/setup/code", data={"code": app.state.setup_code.lower().replace("-", " ")},
               follow_redirects=False)
    assert r.headers["location"] == "/setup/welcome"
    assert "Let's get your pool business set up" in c.get("/setup/welcome").text


def test_account_validation(engine, monkeypatch, tmp_path):
    app, c = fresh(engine, monkeypatch, tmp_path)
    c.post("/setup/code", data={"code": app.state.setup_code})
    base = {"name": "Alex Morgan", "username": "", "email": "", "password": PW, "password_confirm": PW}
    for patch, msg in (({"name": ""}, "full name"), ({"username": "A!"}, "lowercase"),
                       ({"password": "short", "password_confirm": "short"}, "password of 12"),
                       ({"password_confirm": PW + "x"}, "two passwords")):
        r = c.post("/setup/account", data={**base, **patch})
        assert r.status_code == 400 and msg in r.text, patch
    with Session(engine) as s:
        assert s.scalars(select(models.User)).all() == []


def test_creating_the_owner_signs_in_and_retires_the_code(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    with Session(engine) as s:
        u = s.scalars(select(models.User)).one()
        assert (u.name, u.username, u.role) == ("Alex Morgan", "alex.morgan", "admin")
        assert verify_password(PW, u.password_hash)
        assert models.get_setting(s, "setup_status", "") == "in_progress"
        assert models.get_setting(s, "owner_email", "") == "alex@example.com"
    assert app.state.needs_owner is False and app.state.setup_code is None
    assert "Lock the front door" in c.get("/setup/security").text
    # The public setup pages are closed now.
    anon = TestClient(app)
    assert anon.get("/setup/welcome", follow_redirects=False).headers["location"] == "/login"
    assert anon.post("/setup/code", data={"code": "x"}, follow_redirects=False).headers["location"] == "/login"


def test_skipping_two_step_warns_first(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    page = c.get("/setup/security?confirm_skip=1").text
    assert "Are you sure?" in page and "Skip anyway" in page
    r = c.post("/setup/security/skip", follow_redirects=False)
    assert r.headers["location"] == "/setup/business"
    with Session(engine) as s:
        assert "security" in setup_flow.skipped(s)


def test_two_step_enrolment_in_setup(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    c.post("/setup/security/start")   # fresh account: no password re-entry
    page = c.get("/setup/security").text
    secret = page.split('class="mono setup-key">')[1].split("<")[0]
    r = c.post("/setup/security/confirm", data={"code": pyotp.TOTP(secret).now()})
    assert "Two-step sign-in is on" in r.text and "setup-codes" in r.text
    with Session(engine) as s:
        u = s.scalars(select(models.User)).one()
        assert u.totp_enabled and len(s.scalars(select(models.BackupCode)).all()) > 0
    assert c.get("/setup/business").status_code == 200   # still signed in after the epoch bump


def test_business_prices_look_and_water_save(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    c.post("/setup/business", data={"business_name": "Coastline Pool Co.", "company_phone": "555-0100",
                                    "timezone": "America/Chicago", "default_state": "tx"})
    with Session(engine) as s:
        tiers = s.scalars(select(models.RateTier)).all()
        chem = s.scalars(select(models.Chemical)).first()
    c.post("/setup/prices", data={f"tier_{tiers[0].id}": "49.50", "labour": "110", "markup": "25",
                                  "net": "30", f"tier_{tiers[1].id}": "garbage"})
    c.post("/setup/water", data={"chem": [str(chem.id)]})
    c.post("/setup/look", data={"palette": "coral", "nav_layout": "side"})
    with Session(engine) as s:
        assert models.get_setting(s, "business_name", "") == "Coastline Pool Co."
        assert models.get_setting(s, "timezone", "") == "America/Chicago"
        assert models.get_setting(s, "default_state", "") == "TX"
        assert s.get(models.RateTier, tiers[0].id).rate_cents == 4950
        assert s.get(models.RateTier, tiers[1].id).rate_cents == tiers[1].rate_cents   # kept
        assert models.get_setting(s, "labor_rate_cents", "") == "11000"
        assert models.get_setting(s, "invoice_net_days", "") == "30"
        assert [c_.active for c_ in s.scalars(select(models.Chemical)).all()].count(True) == 1
        assert models.get_setting(s, "palette", "") == "coral"
        assert models.get_setting(s, "nav_layout", "") == "side"
    assert config.TIMEZONE == "America/Chicago"
    c.post("/setup/business", data={"timezone": "Mars/Olympus"})
    with Session(engine) as s:
        assert models.get_setting(s, "timezone", "") == "America/Chicago"


def test_crew_gets_temporary_passwords_once(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    r = c.post("/setup/crew", data={"name": ["Jordan Reyes", "", "Sam Okafor"],
                                    "username": ["", "", "sam"], "role": ["tech", "tech", "office"]})
    assert "shown once" in r.text and "jordan.reyes" in r.text
    temp = r.text.split("jordan.reyes</td><td class=\"mono\">")[1].split("<")[0]
    with Session(engine) as s:
        sam = s.scalars(select(models.User).where(models.User.username == "sam")).one()
        assert sam.must_change_password and sam.manage_customers and sam.role == "tech"
    assert temp not in c.get("/setup/crew").text
    tech = TestClient(app)
    tech.post("/login", data={"username": "jordan.reyes", "password": temp})
    assert tech.get("/", follow_redirects=False).headers["location"] == "/account?change=1"
    tech.post("/account/password", data={"current_password": temp, "new_password": "my-own-pass-1",
                                         "new_password_confirm": "my-own-pass-1"})
    assert tech.get("/", follow_redirects=False).status_code == 200


def test_finish_and_getting_started(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    assert "Continue setup" in c.get("/dashboard").text
    r = c.post("/setup/review", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    page = c.get("/dashboard").text
    assert "Getting started" in page and "Turn on two-step sign-in" in page
    assert "Continue setup" not in page
    c.post("/dashboard/getting-started/hide")
    assert "Getting started" not in c.get("/dashboard").text


def test_sign_in_resumes_setup(engine, monkeypatch, tmp_path):
    app, c = start(engine, monkeypatch, tmp_path)
    again = TestClient(app)
    r = again.post("/login", data={"username": "alex.morgan", "password": PW}, follow_redirects=False)
    assert r.headers["location"] == "/setup"
    assert again.get("/setup", follow_redirects=False).headers["location"] == "/setup/security"


def test_login_page_shows_business_name(client, engine):
    with Session(engine) as s:
        models.set_setting(s, "business_name", "Coastline Pool Co.")
        s.commit()
    anon = TestClient(client.app)
    assert "Coastline Pool Co." in anon.get("/login").text


def test_upgraded_owner_finishes_profile(engine, monkeypatch, tmp_path):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(models.User(name="Owner", username="owner", role="admin", active=True,
                          password_hash=hash_password("old-env-pass")))
        s.commit()
    app, c = fresh(engine, monkeypatch, tmp_path)
    assert app.state.setup_legacy
    c.post("/login", data={"username": "owner", "password": "old-env-pass"})
    assert c.get("/customers", follow_redirects=False).headers["location"] == "/setup/profile"
    bad = c.post("/setup/profile", data={"name": "Alex Morgan", "username": "owner",
                                         "current_password": "old-env-pass", "password": PW,
                                         "password_confirm": PW})
    assert bad.status_code == 400
    r = c.post("/setup/profile", data={"name": "Alex Morgan", "username": "",
                                       "current_password": "old-env-pass", "password": PW,
                                       "password_confirm": PW}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    assert c.get("/customers").status_code == 200
    with Session(engine) as s:
        u = s.scalars(select(models.User)).one()
        assert (u.name, u.username) == ("Alex Morgan", "alex.morgan")
        assert verify_password(PW, u.password_hash)


def test_owner_reset_link(engine, monkeypatch, tmp_path):
    make_owner(engine, password="forgotten-pass")
    app, c = fresh(engine, monkeypatch, tmp_path)
    with Session(engine) as s:
        owner = s.scalars(select(models.User)).one()
        owner.totp_enabled, owner.totp_secret = True, pyotp.random_base32()
        token = setup_flow.issue_reset(s, owner.id, clear_2fa=True)
        s.commit()
    assert c.get("/setup/reset/not-the-token").status_code == 404
    assert "Choose a new password" in c.get(f"/setup/reset/{token}").text
    r = c.post(f"/setup/reset/{token}", data={"password": PW, "password_confirm": PW},
               follow_redirects=False)
    assert r.headers["location"] == "/login?reset=1"
    with Session(engine) as s:
        u = s.scalars(select(models.User)).one()
        assert verify_password(PW, u.password_hash) and not u.totp_enabled
    assert c.get(f"/setup/reset/{token}").status_code == 404   # single use


def test_manage_reset_owner_prints_link(engine, monkeypatch, tmp_path, capsys):
    from app import manage
    from app.db import Base, make_engine
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    eng = make_engine()
    Base.metadata.create_all(eng)
    make_owner(eng, username="alex.morgan", password=PW)
    assert manage.main(["reset-owner", "--base-url", "https://pools.example.com"]) == 0
    out = capsys.readouterr().out
    assert "https://pools.example.com/setup/reset/" in out
    assert manage.main(["reset-owner", "--username", "nobody"]) == 1


def test_owner_created_elsewhere_is_picked_up(engine, monkeypatch, tmp_path):
    """A seed script or another worker creating the owner ends setup mode in
    this process too, instead of leaving /setup open."""
    app, c = fresh(engine, monkeypatch, tmp_path)
    make_owner(engine, username="alex.morgan", password=PW)
    r = c.get("/login", follow_redirects=False)
    assert r.status_code == 200 and app.state.needs_owner is False
    assert c.get("/setup/welcome", follow_redirects=False).headers["location"] == "/login"
