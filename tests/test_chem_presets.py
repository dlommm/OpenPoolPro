from app import chem_presets, models


def test_chem_presets_valid_roles():
    assert chem_presets.CHEM_PRESETS
    for c in chem_presets.CHEM_PRESETS:
        assert c["role"] in models.ROLE_VALUES, c["name"]
        assert c["strength_pct"] > 0


def test_add_from_library_creates_chemical(client, engine):
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    r = client.post("/settings/chemicals/add-from-library",
                    data={"preset_name": "Cal-hypo shock"}, follow_redirects=False)
    assert r.status_code == 303
    with Session(engine) as s:
        c = s.scalars(select(models.Chemical).where(
            models.Chemical.name == "Cal-hypo shock")).one()
        assert c.role == "chlorine_granular"
        assert c.cost_per_unit_cents == 0
