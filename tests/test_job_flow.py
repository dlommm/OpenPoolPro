from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def seed_two_customers(engine):
    """One customer with an active property, one without any property."""
    with Session(engine) as s:
        alice = models.Customer(name="Alice Anderson", phone="555-111-2222",
                                email="alice@example.com")
        prop = models.Property(customer=alice, label="Home", address="1 Ocean Ave",
                               city="Seaside")
        bob = models.Customer(name="Bob Baker", phone="555-333-4444",
                              email="bob@example.com")
        s.add_all([alice, prop, bob])
        s.commit()
        return alice.id, prop.id, bob.id


def test_search_blank_q_shows_prompt_and_no_customers(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search")
    assert r.status_code == 200
    assert "Alice Anderson" not in r.text
    assert "Bob Baker" not in r.text


def test_search_matches_name_and_excludes_other_customer(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search", params={"q": "alice"})
    assert r.status_code == 200
    assert "Alice Anderson" in r.text
    assert "Bob Baker" not in r.text


def test_search_matches_phone(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search", params={"q": "555-333"})
    assert "Bob Baker" in r.text
    assert "Alice Anderson" not in r.text


def test_search_matches_email(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search", params={"q": "alice@example.com"})
    assert "Alice Anderson" in r.text
    assert "Bob Baker" not in r.text


def test_search_excludes_inactive_customers(client, engine):
    with Session(engine) as s:
        s.add(models.Customer(name="Inactive Ida", active=False))
        s.commit()
    r = client.get("/jobs/new/search", params={"q": "ida"})
    assert "Inactive Ida" not in r.text


def test_search_shows_primary_property_city(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search", params={"q": "alice"})
    assert "Seaside" in r.text


def test_search_no_results_state(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/search", params={"q": "zzznomatch"})
    assert "Alice Anderson" not in r.text
    assert "Bob Baker" not in r.text


def test_search_result_count_is_capped(client, engine):
    with Session(engine) as s:
        for i in range(20):
            s.add(models.Customer(name=f"Capco Customer {i}", phone="", email=""))
        s.commit()
    r = client.get("/jobs/new/search", params={"q": "capco"})
    assert r.text.count("hx-get=\"/jobs/new/properties?customer_id=") <= 15


def test_properties_lists_active_property_label_and_address(client, engine):
    alice_id, prop_id, _bob_id = seed_two_customers(engine)
    r = client.get("/jobs/new/properties", params={"customer_id": alice_id})
    assert r.status_code == 200
    assert "Alice Anderson" in r.text
    assert "Home" in r.text
    assert "1 Ocean Ave" in r.text
    assert f'value="{prop_id}"' in r.text


def test_properties_excludes_inactive_property(client, engine):
    with Session(engine) as s:
        c = models.Customer(name="Carla Customer")
        models.Property(customer=c, label="Active Place", address="2 Bay St", active=True)
        models.Property(customer=c, label="Old Place", address="3 Gone St", active=False)
        s.add(c)
        s.commit()
        cid = c.id
    r = client.get("/jobs/new/properties", params={"customer_id": cid})
    assert "Active Place" in r.text
    assert "Old Place" not in r.text


def test_properties_preselects_when_only_one(client, engine):
    alice_id, prop_id, _bob_id = seed_two_customers(engine)
    r = client.get("/jobs/new/properties", params={"customer_id": alice_id})
    assert "checked" in r.text


def test_properties_empty_state_when_none_active(client, engine):
    _alice_id, _prop_id, bob_id = seed_two_customers(engine)
    r = client.get("/jobs/new/properties", params={"customer_id": bob_id})
    assert r.status_code == 200
    assert "Bob Baker" in r.text
    assert "no active" in r.text.lower() or "no propert" in r.text.lower()


def test_properties_404_for_missing_customer(client, engine):
    seed_two_customers(engine)
    r = client.get("/jobs/new/properties", params={"customer_id": 999999})
    assert r.status_code == 404


# --- Task 5: New/Existing job-create flow ------------------------------------

def test_create_job_existing_mode_search_then_hourly_labor(client, engine):
    alice_id, prop_id, _bob_id = seed_two_customers(engine)
    with Session(engine) as s:
        t = models.JobTemplate(title="Green clean", job_type="service", est_hours=1.5)
        s.add(t)
        s.commit()
        tid = t.id
    # Exercise the Task-4 typeahead search that feeds the property picker.
    r = client.get("/jobs/new/search", params={"q": "alice"})
    assert "Alice Anderson" in r.text
    r2 = client.get("/jobs/new/properties", params={"customer_id": alice_id})
    assert f'value="{prop_id}"' in r2.text

    r3 = client.post("/jobs/new", data={
        "mode": "existing", "property_id": str(prop_id), "title": "Green clean",
        "template_id": str(tid), "job_type": "service",
        "rate_dollars": "50", "hours": "2"}, follow_redirects=False)
    assert r3.status_code == 303
    with Session(engine) as s:
        j = s.scalars(select(models.Job).where(models.Job.title == "Green clean")).one()
        assert j.property_id == prop_id
        assert j.hours == 2.0 and j.labor_cents == 10000  # $50/hr x 2h


def test_create_job_new_mode_creates_customer_property_pool_and_job(client, engine):
    r = client.post("/jobs/new", data={
        "mode": "new", "title": "First service",
        "customer_name": "New Customer", "customer_phone": "555-000-1111",
        "customer_email": "newcust@example.com",
        "new_label": "Home", "new_street": "10 Lagoon Dr", "new_city": "Coral Bay",
        "new_state": "FL", "new_zip": "33000", "new_gate_code": "1234",
        "new_frequency": "weekly", "new_weekday": "2",
        "label": "Main pool", "kind": "pool", "shape": "rectangle",
        "length_ft": "30", "width_ft": "15", "avg_depth_ft": "5",
        "water_type": "chlorine", "surface": "plaster",
        "job_type": "service", "rate_dollars": "60", "hours": "3"},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.scalars(select(models.Customer).where(models.Customer.name == "New Customer")).one()
        assert c.phone == "555-000-1111" and c.email == "newcust@example.com"
        p = s.scalars(select(models.Property).where(models.Property.customer_id == c.id)).one()
        assert p.street == "10 Lagoon Dr" and p.city == "Coral Bay" and p.gate_code == "1234"
        pool = s.scalars(select(models.Pool).where(models.Pool.property_id == p.id)).one()
        assert pool.kind == "pool" and pool.gallons > 0
        j = s.scalars(select(models.Job).where(models.Job.title == "First service")).one()
        assert j.property_id == p.id
        assert j.hours == 3.0 and j.labor_cents == 18000  # $60/hr x 3h


def test_create_job_new_mode_blocked_without_manage_customers(client, engine, tech_client):
    tech = tech_client(name="fieldtech")  # no manage_customers flag
    r = tech.post("/jobs/new", data={
        "mode": "new", "title": "Sneaky job",
        "customer_name": "Should Not Exist"}, follow_redirects=False)
    assert r.status_code == 400  # falls back to existing-mode with no property_id
    with Session(engine) as s:
        assert s.scalars(select(models.Customer).where(
            models.Customer.name == "Should Not Exist")).first() is None
