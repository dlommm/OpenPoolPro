from sqlalchemy.orm import Session

from app import chem_targets, models, water_history


def test_problem_limits():
    assert chem_targets.problem("ph", 7.5, 0.3) == ""
    assert "target" in chem_targets.problem("ph", 9.0, 0.3)
    assert "band" in chem_targets.problem("ph", 7.5, 2.0)        # would switch dosing off
    assert "band" in chem_targets.problem("fc", 4.0, 0.0)        # would dose every visit
    assert "below" in chem_targets.problem("cya", 3.0, 5.0)
    assert chem_targets.problem("ta", float("nan"), 10.0)


def test_current_falls_back_per_pair(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "target_ph", "7.3")
        models.set_setting(s, "band_ph", "5")          # invalid pair -> both defaults
        models.set_setting(s, "target_fc", "3.0")
        models.set_setting(s, "band_fc", "1.0")
        s.commit()
        targets, bands = chem_targets.current(s)
        assert (targets["ph"], bands["ph"]) == (7.5, 0.3)
        assert (targets["fc"], bands["fc"]) == (3.0, 1.0)


def test_history_and_alert_bands_follow_setting(engine):
    from app.db import Base
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        models.set_setting(s, "band_ta", "25")
        s.commit()
        assert water_history.bands(s)["ta"] == 25.0
        ta = next(x for x in water_history.scales(s) if x["key"] == "ta")
        assert (ta["band_lo"], ta["band_hi"]) == (65.0, 115.0)


def test_save_targets_and_bands(client, engine):
    r = client.post("/settings/targets", data={"target_ph": "7.4", "band_ph": "0.2",
                                               "target_fc": "4", "band_fc": "9"},
                    follow_redirects=False)
    assert r.status_code == 303 and "chem_rejected=fc" in r.headers["location"]
    with Session(engine) as s:
        assert models.get_setting(s, "band_ph", "") == "0.2"
        assert models.get_setting(s, "band_fc", "") == ""      # rejected, nothing stored
    page = client.get("/settings?chem_rejected=fc").text
    assert "kept their previous values" in page
    client.post("/settings/targets", data={"reset": "1"})
    with Session(engine) as s:
        targets, bands = chem_targets.current(s)
        assert targets == chem_targets.DEFAULT_TARGETS and bands == chem_targets.DEFAULT_BANDS


def test_dose_fragment_uses_saved_band(client, engine):
    from tests.test_dosing_endpoint import seed
    vid = seed(client, engine)
    assert "Liquid chlorine" in client.get(f"/visit/{vid}/doses", params={"fc": "3.0"}).text
    client.post("/settings/targets", data={"band_fc": "1.5"})
    assert "Liquid chlorine" not in client.get(f"/visit/{vid}/doses", params={"fc": "3.0"}).text
