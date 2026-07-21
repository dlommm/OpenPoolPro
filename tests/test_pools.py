from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


def _prop(client, engine):
    client.post("/customers/new", data={"name": "P", "qb_display_name": "",
                                        "phone": "", "email": "", "notes": ""})
    with Session(engine) as s:
        cid = s.scalars(select(models.Customer)).one().id
    client.post(f"/customers/{cid}/properties/new", data={
        "label": "Home", "address": "", "gate_code": "", "dog_note": "", "notes": "",
        "frequency": "weekly", "weekday": "0", "anchor_date": "", "rate_override_dollars": ""})
    with Session(engine) as s:
        return s.scalars(select(models.Property)).one().id


def test_create_pool_computes_gallons(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "salt", "surface": "pebble", "notes": ""},
        follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        pool = s.scalars(select(models.Pool)).one()
        assert pool.gallons == 16875


def test_invalid_dims_rejected(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "rectangle", "length_ft": "0", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    assert r.status_code == 400 and "dimensions" in r.text


def test_gallons_preview(client, engine):
    _prop(client, engine)
    r = client.get("/pools/gallons-preview",
                   params={"shape": "rectangle", "length_ft": "30",
                           "width_ft": "15", "avg_depth_ft": "5"})
    assert "16,875" in r.text and "$55.00" in r.text
    r = client.get("/pools/gallons-preview",
                   params={"shape": "rectangle", "length_ft": "", "width_ft": "", "avg_depth_ft": ""})
    assert r.status_code == 200  # never 500 while typing


def test_equipment_crud(client, engine):
    pid = _prop(client, engine)
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "round", "length_ft": "20", "width_ft": "",
        "avg_depth_ft": "4", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id
    client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "pump", "brand": "Pentair", "model": "SuperFlo", "serial": "X1",
        "install_date": "", "notes": ""})
    with Session(engine) as s:
        eq = s.scalars(select(models.Equipment)).one()
        assert eq.brand == "Pentair"


def test_add_equipment_stores_warranty_end_and_renders_it(client, engine):
    pid = _prop(client, engine)
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "round", "length_ft": "20", "width_ft": "",
        "avg_depth_ft": "4", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id
    client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "pump", "brand": "Pentair", "model": "SuperFlo", "serial": "X1",
        "install_date": "", "warranty_end": "2027-03-01", "notes": ""})
    with Session(engine) as s:
        eq = s.scalars(select(models.Equipment)).one()
        assert eq.warranty_end == date(2027, 3, 1)
    html = client.get(f"/pools/{pool_id}/edit").text
    assert "2027-03-01" in html


def test_add_equipment_warranty_end_tolerant_blank_and_garbage(client, engine):
    pid = _prop(client, engine)
    client.post(f"/properties/{pid}/pools/new", data={
        "label": "Main", "shape": "round", "length_ft": "20", "width_ft": "",
        "avg_depth_ft": "4", "water_type": "chlorine", "surface": "plaster", "notes": ""})
    with Session(engine) as s:
        pool_id = s.scalars(select(models.Pool)).one().id
    r1 = client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "pump", "brand": "A", "model": "", "serial": "",
        "install_date": "", "warranty_end": "", "notes": ""}, follow_redirects=False)
    r2 = client.post(f"/pools/{pool_id}/equipment/new", data={
        "kind": "pump", "brand": "B", "model": "", "serial": "",
        "install_date": "", "warranty_end": "not-a-date", "notes": ""}, follow_redirects=False)
    assert r1.status_code == 303 and r2.status_code == 303  # tolerant -- never 500
    with Session(engine) as s:
        eqs = s.scalars(select(models.Equipment).order_by(models.Equipment.id)).all()
        assert len(eqs) == 2
        assert all(e.warranty_end is None for e in eqs)


def test_missing_ids_return_404(client, engine):
    assert client.get("/properties/999/pools/new").status_code == 404
    assert client.get("/pools/999/edit").status_code == 404
    assert client.post("/pools/999/deactivate").status_code == 404
    assert client.post("/pools/999/equipment/new", data={
        "kind": "pump", "brand": "", "model": "", "serial": "",
        "install_date": "", "notes": ""}).status_code == 404
    assert client.post("/equipment/999/delete").status_code == 404


def test_missing_form_fields_return_400_not_500(client, engine):
    pid = _prop(client, engine)
    r = client.post(f"/properties/{pid}/pools/new", data={"shape": "rectangle"})
    assert r.status_code == 400


def test_create_spa_pool_sets_kind(client, engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Ann")
        p = models.Property(customer=c)
        s.add(p); s.commit(); pid = p.id
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Hot tub", "shape": "round", "length_ft": "6", "width_ft": "",
        "avg_depth_ft": "3", "water_type": "chlorine", "surface": "plaster",
        "notes": "", "kind": "spa"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        pool = s.scalars(select(models.Pool).where(models.Pool.label == "Hot tub")).one()
        assert pool.kind == "spa"
        assert pool.gallons > 0


def test_new_pool_prefills_from_size_preset(client, engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Bob")
        p = models.Property(customer=c)
        s.add(p); s.commit(); pid = p.id
        sp = s.scalars(select(models.SizePreset)
                       .where(models.SizePreset.label == "16x32 Rectangle")).one()
        preset_id = sp.id
    r = client.get(f"/properties/{pid}/pools/new?preset={preset_id}")
    assert r.status_code == 200
    assert 'value="32"' in r.text and 'value="16"' in r.text  # length/width prefilled


def test_pool_gallons_override_wins(client, engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models
    with Session(engine) as s:
        c = models.Customer(name="Ovr")
        p = models.Property(customer=c)
        s.add(p); s.commit(); pid = p.id
    r = client.post(f"/properties/{pid}/pools/new", data={
        "label": "Odd shape", "shape": "rectangle", "length_ft": "30", "width_ft": "15",
        "avg_depth_ft": "5", "water_type": "chlorine", "surface": "plaster",
        "notes": "", "kind": "pool", "gallons_override": "20000"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        pool = s.scalars(select(models.Pool).where(models.Pool.label == "Odd shape")).one()
        assert pool.gallons_override == 20000
        assert pool.gallons == 20000  # override wins over the computed 16875


def test_size_presets_seeded(engine):
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from app import models, seeds
    from app.db import Base
    Base.metadata.create_all(engine)
    seeds.run(engine)
    with Session(engine) as s:
        pool_sizes = s.scalars(select(models.SizePreset).where(models.SizePreset.kind == "pool")).all()
        spa_sizes = s.scalars(select(models.SizePreset).where(models.SizePreset.kind == "spa")).all()
        assert len(pool_sizes) >= 10 and len(spa_sizes) >= 1


def test_rate_cards_offer_common_sizes(client):
    # Pricing tab quick-add: pool rate card lists a common size from the seeded library
    html = client.get("/settings").text
    assert "Add common size" in html
    assert "16x32 Rectangle" in html
