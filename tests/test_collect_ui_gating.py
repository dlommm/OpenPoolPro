"""The Amount-to-collect panel must not offer controls the server ignores.

`view_money` gates the labor override server-side (app/routers/visits.py,
app/routers/jobs.py). The form used to render the editable box for everyone and
live-update the total as the tech typed, so a tech without the permission could
"apply" a discount, watch the Collect figure drop, collect that amount in cash,
and leave the visit billing full rate — a silent cash/billing mismatch the owner
only finds at reconciliation.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models

FIELD = 'name="labor_override_dollars"'


def _property(engine, **kw):
    with Session(engine) as s:
        c = models.Customer(name="Acme", active=True)
        s.add(c)
        s.flush()
        p = models.Property(customer_id=c.id, label="Main", address="1 St", active=True,
                            weekday=0, frequency="weekly", route_order=1,
                            rate_override_cents=12000, **kw)
        s.add(p)
        s.commit()
        return p.id


def test_visit_form_hides_the_override_box_without_view_money(client, tech_client, engine):
    pid = _property(engine)
    tech = tech_client(name="nobody")
    body = tech.get(f"/visit/{pid}/2026-07-13").text
    assert FIELD not in body, "editable override rendered for a tech the server ignores"
    assert "$120.00" in body, "the service figure should still be shown, read-only"


def test_visit_form_shows_the_override_box_with_view_money(client, tech_client, engine):
    pid = _property(engine)
    tech = tech_client(name="money", view_money=True)
    assert FIELD in tech.get(f"/visit/{pid}/2026-07-13").text


def test_job_detail_hides_the_override_box_without_view_money(client, tech_client, engine):
    pid = _property(engine)
    r = client.post("/jobs/new", data={"property_id": str(pid), "title": "Repair",
                                       "hours": "2", "rate_dollars": "100"},
                    follow_redirects=False)
    jid = int(r.headers["location"].rsplit("/", 1)[-1])

    tech = tech_client(name="nobody")
    assert FIELD not in tech.get(f"/jobs/{jid}").text

    money = tech_client(name="money", view_money=True)
    assert FIELD in money.get(f"/jobs/{jid}").text


def test_applying_doses_also_refreshes_the_collect_total(client, engine):
    """Regression pin for the stale-total bug.

    `recompute()` is bound to the "input" event, and assigning `.value` in JS
    fires no such event — so the apply-doses handler MUST dispatch one, or the
    tech reads a Collect figure that predates the doses they just applied and
    asks the customer for the wrong money. Asserted against the template source
    because the behaviour is client-side.
    """
    import pathlib
    src = pathlib.Path("app/templates/visits/form.html").read_text()
    apply_block = src.split('e.target.id !== "apply-doses"')[1].split("});")[0]
    assert "input.value = el.dataset.amount" in apply_block
    assert 'dispatchEvent(new Event("input"' in apply_block, (
        "apply-doses sets .value without dispatching an input event, so the "
        "Amount-to-collect total will not recompute")


def test_collect_panel_stays_live_for_a_non_money_tech(client, tech_client, engine):
    """Removing the service input must not freeze the rest of the panel: the
    recompute has to fall back to the server figure rather than bailing out."""
    import pathlib
    src = pathlib.Path("app/templates/visits/form.html").read_text()
    recompute = src.split("function recompute()")[1].split("\n  }")[0]
    assert "if (!serviceEl) return;" not in recompute, (
        "recompute bails when there is no editable service box, which freezes "
        "chemicals/tax/total for every tech without view_money")
    assert "collect.service_cents" in recompute
