"""Customer sync planning: what changes, what needs approval, what never happens.

The guarantee under test is that QuickBooks is downstream and protected --
nothing is deleted, nothing is overwritten without explicit approval, and no
duplicate customer is created in either direction.
"""
import pytest
from sqlalchemy.orm import Session

from app import models, qbo_api, qbo_sync


def setup_function(_):
    qbo_api._cache.clear()


SCOPE = "sandbox:9130000"


def _connected(engine):
    models.Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "qbo_client_id", "client-abc")
        models.set_setting(s, "qbo_client_secret", "app-secret")
        models.set_setting(s, "qbo_environment", "sandbox")
        models.set_setting(s, "qbo_refresh_token", "rt-1")
        models.set_setting(s, "qbo_realm_id", "9130000")
        s.commit()


def _remote(qbo_id, name, email="", phone=""):
    raw = {"Id": str(qbo_id), "DisplayName": name}
    if email:
        raw["PrimaryEmailAddr"] = {"Address": email}
    if phone:
        raw["PrimaryPhone"] = {"FreeFormNumber": phone}
    return raw


def _roster(monkeypatch, rows):
    monkeypatch.setattr(qbo_api, "query_customers", lambda s, **k: list(rows))


def _add(engine, **kwargs):
    with Session(engine) as s:
        customer = models.Customer(active=True, **kwargs)
        s.add(customer)
        s.commit()
        return customer.id


# ---------- push planning ----------

def test_unlinked_customer_with_no_remote_match_is_a_create(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Someone Else")])
    _add(engine, name="Ada Rivera", email="ada@example.com")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert [r.name for r in plan["creates"]] == ["Ada Rivera"]
    assert not plan["links"] and not plan["review"]


def test_exact_name_match_links_instead_of_creating_a_duplicate(engine, monkeypatch):
    """QuickBooks enforces unique display names, so creating here would fail --
    and a failure count teaches nothing about the customer already there."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "ada@example.com")])
    _add(engine, name="Ada Rivera")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"]
    assert [(r.name, r.qbo_id) for r in plan["links"]] == [("Ada Rivera", "42")]


def test_name_matching_ignores_case_and_spacing(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "ada   rivera")])
    _add(engine, name="Ada Rivera")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert [r.qbo_id for r in plan["links"]] == ["42"]


def test_near_name_match_is_reviewed_never_linked_automatically(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Robert Smithe")])
    _add(engine, name="Robert Smith")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"] and not plan["links"]
    assert [r.remote_name for r in plan["review"]] == ["Robert Smithe"]
    assert plan["review"][0].ratio >= qbo_sync.NEAR_MATCH_RATIO


def test_shared_email_is_a_match_even_when_names_differ(engine, monkeypatch):
    """'E Vance' and 'Ed Vance' are not a name match, but one address is strong
    evidence of one person -- a business-level duplicate QuickBooks' own
    uniqueness rule would happily allow."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "E Vance", "e@example.com")])
    _add(engine, name="Ed Vance", email="e@example.com")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"]
    assert [r.remote_name for r in plan["review"]] == ["E Vance"]


def test_empty_remote_field_is_additive_and_needs_no_approval(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera")])
    _add(engine, name="Ada Rivera", email="ada@example.com", phone="555",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["approval"]
    changes = {c.label: c for c in plan["additive"][0].changes}
    assert changes["email"].additive and changes["phone"].additive
    assert changes["email"].old == ""


def test_differing_remote_value_needs_approval(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    _add(engine, name="Ada Rivera", email="new@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["additive"]
    change = plan["approval"][0].overwriting[0]
    assert (change.label, change.old, change.new) == ("email", "old@example.com", "new@example.com")


def test_a_name_change_always_needs_approval_even_from_empty(engine, monkeypatch):
    """DisplayName is QuickBooks' unique key for the record, so renaming it is
    never additive."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "")])
    _add(engine, name="Ada Rivera", qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["additive"]
    assert plan["approval"][0].overwriting[0].field == "DisplayName"


def test_a_blank_local_value_is_never_pushed(engine, monkeypatch):
    """Not knowing a phone number is not a claim that QuickBooks' is wrong."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "ada@example.com", "555")])
    _add(engine, name="Ada Rivera", email="", phone="",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["additive"] and not plan["approval"]
    assert plan["unchanged"] == 1


def test_a_customer_linked_to_another_company_is_never_pushed(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [])
    _add(engine, name="Ada Rivera", qbo_customer_id="42", qbo_customer_scope="production:999")
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"] and not plan["links"]
    assert "different QuickBooks company" in plan["review"][0].reason


def test_a_linked_id_missing_from_the_company_is_reviewed_not_recreated(engine, monkeypatch):
    """Re-creating under a fresh id is exactly how duplicates appear."""
    _connected(engine)
    _roster(monkeypatch, [])
    _add(engine, name="Ada Rivera", qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"]
    assert plan["stale"] == 1 and len(plan["review"]) == 1


def test_inactive_local_customers_are_left_out_of_the_push(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [])
    with Session(engine) as s:
        s.add(models.Customer(name="Retired", active=False))
        s.commit()
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
    assert not plan["creates"]


# ---------- push apply ----------

def _apply(engine, approved=(), digest=None):
    with Session(engine) as s:
        plan = qbo_sync.plan_push(s)
        return qbo_sync.apply_push(s, digest or plan["digest"], set(approved))


def test_apply_writes_additive_fields_without_approval(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera")])
    _add(engine, name="Ada Rivera", email="ada@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    sent = []
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda s, c, fields: sent.append(fields) or {"Id": "42"})
    result = _apply(engine)
    assert result["updated"] == 1 and result["skipped"] == 0
    assert sent == [{"PrimaryEmailAddr": "ada@example.com"}]


def test_apply_holds_back_unapproved_overwrites(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    _add(engine, name="Ada Rivera", email="new@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda *a, **k: pytest.fail("must not write an unapproved overwrite"))
    result = _apply(engine)
    assert result["updated"] == 0 and result["skipped"] == 1


def test_apply_writes_an_approved_overwrite(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    cid = _add(engine, name="Ada Rivera", email="new@example.com",
               qbo_customer_id="42", qbo_customer_scope=SCOPE)
    sent = []
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda s, c, fields: sent.append(fields) or {"Id": "42"})
    result = _apply(engine, approved=[f"cust:{cid}:PrimaryEmailAddr"])
    assert result["updated"] == 1 and result["skipped"] == 0
    assert sent == [{"PrimaryEmailAddr": "new@example.com"}]


def test_approving_one_field_does_not_carry_another(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com", "111")])
    cid = _add(engine, name="Ada Rivera", email="new@example.com", phone="999",
               qbo_customer_id="42", qbo_customer_scope=SCOPE)
    sent = []
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda s, c, fields: sent.append(fields) or {"Id": "42"})
    _apply(engine, approved=[f"cust:{cid}:PrimaryEmailAddr"])
    assert sent == [{"PrimaryEmailAddr": "new@example.com"}]  # phone withheld


def test_apply_links_an_exact_match_without_any_remote_write(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera")])
    cid = _add(engine, name="Ada Rivera")
    monkeypatch.setattr(qbo_api, "create_customer",
                        lambda *a, **k: pytest.fail("must link, not create"))
    result = _apply(engine)
    assert result["linked"] == 1 and result["created"] == 0
    with Session(engine) as s:
        customer = s.get(models.Customer, cid)
        assert customer.qbo_customer_id == "42" and customer.qbo_customer_scope == SCOPE


def test_apply_refuses_a_stale_plan(engine, monkeypatch):
    """Approval binds to the QuickBooks state that was reviewed."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    _add(engine, name="Ada Rivera", email="new@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda *a, **k: pytest.fail("must not write against a stale plan"))
    with Session(engine) as s:
        with pytest.raises(qbo_sync.SyncError, match="since this review"):
            qbo_sync.apply_push(s, "not-the-digest", set())


def test_the_digest_tracks_which_customer_gets_which_value(engine, monkeypatch):
    """Counts alone would let two different plans share a digest."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    _add(engine, name="Ada Rivera", email="a@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        first = qbo_sync.plan_push(s)["digest"]
        s.query(models.Customer).update({"email": "b@example.com"})
        s.commit()
        second = qbo_sync.plan_push(s)["digest"]
    assert first != second


# ---------- import planning ----------

def test_import_creates_customers_that_have_no_local_match(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Ada Rivera", "ada@example.com", "555")])
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    assert [r.name for r in plan["creates"]] == ["Ada Rivera"]
    assert plan["creates"][0].email == "ada@example.com"


def test_import_auto_links_an_exact_local_name_match(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Ada Rivera")])
    cid = _add(engine, name="Ada Rivera")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    assert not plan["creates"]
    assert [r.customer_id for r in plan["auto_link"]] == [cid]


def test_import_reviews_a_near_local_match(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Robert Smithe")])
    _add(engine, name="Robert Smith")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    assert not plan["creates"] and not plan["auto_link"]
    assert [r.name for r in plan["review"]] == ["Robert Smithe"]


def test_import_skips_customers_already_linked(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera")])
    _add(engine, name="Ada Rivera", qbo_customer_id="42", qbo_customer_scope=SCOPE)
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    assert plan["already"] == 1 and not plan["creates"] and not plan["auto_link"]


def test_import_never_claims_one_local_customer_twice(engine, monkeypatch):
    """Two QuickBooks records must not both link to the same local customer."""
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Ada Rivera"), _remote(2, "Ada Rivera ")])
    _add(engine, name="Ada Rivera")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    claimed = [r.customer_id for r in plan["auto_link"] + plan["review"] if r.customer_id]
    assert len(claimed) == len(set(claimed)) == 1


def test_import_into_an_empty_openpoolpro_creates_everything(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(i, f"Customer {i}") for i in range(1, 6)])
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
    assert len(plan["creates"]) == 5 and not plan["review"]


# ---------- import apply ----------

def test_apply_import_creates_and_links_local_records(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Ada Rivera", "ada@example.com", "555"),
                          _remote(2, "Bob Smith")])
    cid = _add(engine, name="Bob Smith")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
        result = qbo_sync.apply_import(s, plan["digest"], {})
    assert result == {"created": 1, "linked": 1, "skipped": 0}
    with Session(engine) as s:
        assert s.get(models.Customer, cid).qbo_customer_id == "2"
        ada = s.query(models.Customer).filter_by(qbo_customer_id="1").one()
        assert (ada.name, ada.email, ada.phone) == ("Ada Rivera", "ada@example.com", "555")
        assert ada.qbo_customer_scope == SCOPE


def test_apply_import_honours_a_review_decision(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Robert Smithe")])
    _add(engine, name="Robert Smith")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
        key = plan["review"][0].key
        result = qbo_sync.apply_import(s, plan["digest"], {key: "create"})
    assert result["created"] == 1 and result["linked"] == 0


def test_apply_import_skips_a_review_row_by_default(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Robert Smithe")])
    _add(engine, name="Robert Smith")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
        result = qbo_sync.apply_import(s, plan["digest"], {})
    assert result == {"created": 0, "linked": 0, "skipped": 1}


def test_apply_import_refuses_a_stale_plan(engine, monkeypatch):
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Ada Rivera")])
    with Session(engine) as s:
        with pytest.raises(qbo_sync.SyncError, match="since this review"):
            qbo_sync.apply_import(s, "not-the-digest", {})
    with Session(engine) as s:
        assert s.query(models.Customer).count() == 0


def test_import_keeps_the_quickbooks_display_name_so_a_push_plans_no_rename(engine, monkeypatch):
    """A linked local name that differs would otherwise show up as a rename
    needing approval on the very next push."""
    _connected(engine)
    _roster(monkeypatch, [_remote(1, "Rivera, Ada")])
    _add(engine, name="Rivera, Ada")
    with Session(engine) as s:
        plan = qbo_sync.plan_import(s)
        qbo_sync.apply_import(s, plan["digest"], {})
    with Session(engine) as s:
        push = qbo_sync.plan_push(s)
    assert not push["approval"] and not push["additive"]


# ---------- the standing guarantee ----------

def test_nothing_in_a_sync_can_delete_or_deactivate_a_quickbooks_record(engine, monkeypatch):
    """Every write a sync makes must survive the transport's non-destructive
    guard -- the guarantee holds by construction, not by convention."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com"), _remote(43, "Zed")])
    cid = _add(engine, name="Ada Rivera", email="new@example.com",
               qbo_customer_id="42", qbo_customer_scope=SCOPE)
    _add(engine, name="Brand New")
    seen = []

    def recording_api(s, method, path, body=None, **kwargs):
        qbo_api._assert_non_destructive(method, path, body)
        seen.append((method, path, body))
        return {"Customer": {"Id": "99", "SyncToken": "0"}}

    monkeypatch.setattr(qbo_api, "_api", recording_api)
    _apply(engine, approved=[f"cust:{cid}:PrimaryEmailAddr"])
    assert seen, "expected the sync to make writes"
    for method, path, body in seen:
        assert method in ("GET", "POST")
        assert "operation=" not in path
        assert not isinstance(body, dict) or body.get("Active") is not False


# ---------- the review page ----------

def test_review_page_renders_every_bucket(client, engine, monkeypatch):
    """Exercises the template against one of each row type at once."""
    _connected(engine)
    _roster(monkeypatch, [
        _remote(42, "Ada Rivera", "old@example.com"),   # overwrite -> approval
        _remote(43, "Cy Nguyen"),                        # empty -> additive
        _remote(44, "Bob Smith"),                        # exact -> link
        _remote(45, "Robert Smithe"),                    # near -> review
        _remote(46, "Only In QuickBooks", "q@example.com"),  # import create
    ])
    _add(engine, name="Ada Rivera", email="new@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    _add(engine, name="Cy Nguyen", phone="555",
         qbo_customer_id="43", qbo_customer_scope=SCOPE)
    _add(engine, name="Bob Smith")
    _add(engine, name="Robert Smith")
    _add(engine, name="Brand New")

    r = client.get("/settings/qbo/customers")
    assert r.status_code == 200
    body = r.text
    assert "Will create in QuickBooks" in body
    assert "Will link to existing QuickBooks customers" in body
    assert "Will fill empty QuickBooks fields" in body
    assert "Needs your approval" in body
    assert "old@example.com" in body and "new@example.com" in body  # the diff is shown
    assert "Only In QuickBooks" in body                              # import side
    assert "never overwrites a QuickBooks value without your approval" in body


def test_review_page_makes_no_writes(client, engine, monkeypatch):
    """A render must never mutate QuickBooks -- the same rule the items list
    follows, since a crafted link can make an admin's browser load this page."""
    _connected(engine)
    _roster(monkeypatch, [_remote(42, "Ada Rivera", "old@example.com")])
    _add(engine, name="Ada Rivera", email="new@example.com",
         qbo_customer_id="42", qbo_customer_scope=SCOPE)
    monkeypatch.setattr(qbo_api, "update_customer_fields",
                        lambda *a, **k: pytest.fail("render must not write"))
    monkeypatch.setattr(qbo_api, "create_customer",
                        lambda *a, **k: pytest.fail("render must not write"))
    assert client.get("/settings/qbo/customers").status_code == 200
    with Session(engine) as s:
        assert s.query(models.Customer).one().email == "new@example.com"
