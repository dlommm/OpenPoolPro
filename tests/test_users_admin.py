from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.auth_core import verify_password


def test_create_tech_with_flags(client, engine):
    r = client.post("/users/new", data={
        "name": "Sam Rivera", "username": "Sam", "password": "field-pass",
        "role": "tech", "view_money": "on", "can_repairs": "on"},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        u = s.scalars(select(models.User).where(models.User.username == "sam")).one()
        assert u.role == "tech" and u.view_money and u.can_repairs
        assert not u.manage_customers and not u.view_all_routes
        assert verify_password("field-pass", u.password_hash)
    assert "Sam Rivera" in client.get("/users").text


def test_duplicate_username_inline_error(client, engine):
    data = {"name": "A", "username": "dupe", "password": "x1234567", "role": "tech"}
    client.post("/users/new", data=data)
    r = client.post("/users/new", data=data)
    assert r.status_code == 200 and "already taken" in r.text


def test_edit_flags_and_reset_password(client, engine):
    client.post("/users/new", data={"name": "T", "username": "t1",
                                    "password": "old-pass-1", "role": "tech"})
    with Session(engine) as s:
        uid = s.scalars(select(models.User).where(models.User.username == "t1")).one().id
    client.post(f"/users/{uid}/edit", data={"name": "T", "username": "t1",
                                            "role": "tech", "manage_customers": "on"})
    client.post(f"/users/{uid}/password", data={"password": "new-pass-9"})
    with Session(engine) as s:
        u = s.get(models.User, uid)
        assert u.manage_customers and verify_password("new-pass-9", u.password_hash)


def test_disable_enable_and_self_guard(client, engine):
    client.post("/users/new", data={"name": "T", "username": "t2",
                                    "password": "pass-1234", "role": "tech"})
    with Session(engine) as s:
        uid = s.scalars(select(models.User).where(models.User.username == "t2")).one().id
        owner_id = s.scalars(select(models.User).where(models.User.username == "owner")).one().id
    client.post(f"/users/{uid}/disable")
    with Session(engine) as s:
        assert s.get(models.User, uid).active is False
    client.post(f"/users/{uid}/enable")
    with Session(engine) as s:
        assert s.get(models.User, uid).active is True
    r = client.post(f"/users/{owner_id}/disable")
    assert "yourself" in r.text or r.status_code == 400
    with Session(engine) as s:
        assert s.get(models.User, owner_id).active is True


def test_users_admin_only(client, tech_client):
    t = tech_client(name="peon")
    assert t.get("/users", follow_redirects=False).status_code == 403
    assert t.post("/users/new", data={"name": "X", "username": "x",
                                      "password": "12345678", "role": "admin"},
                  follow_redirects=False).status_code == 403


def test_role_demotion_guards(client, engine):
    with Session(engine) as s:
        owner_id = s.scalars(select(models.User).where(
            models.User.username == "owner")).one().id
    # sole admin demoting self: blocked
    r = client.post(f"/users/{owner_id}/edit", data={
        "name": "Owner", "username": "owner", "role": "tech"})
    assert "yourself" in r.text or "last active admin" in r.text
    with Session(engine) as s:
        assert s.get(models.User, owner_id).role == "admin"
    # add a second admin; demoting THAT one is fine while owner remains
    client.post("/users/new", data={"name": "Second", "username": "second",
                                    "password": "adminpass1", "role": "admin"})
    with Session(engine) as s:
        sid = s.scalars(select(models.User).where(
            models.User.username == "second")).one().id
    client.post(f"/users/{sid}/edit", data={"name": "Second", "username": "second",
                                            "role": "tech"})
    with Session(engine) as s:
        assert s.get(models.User, sid).role == "tech"


def test_short_password_inline_error(client):
    r = client.post("/users/new", data={"name": "X", "username": "shorty",
                                        "password": "short", "role": "tech"})
    assert r.status_code == 200 and "8+ characters" in r.text
