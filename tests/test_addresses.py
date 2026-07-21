from app import addresses


def test_compose_full():
    assert addresses.compose_address("12 Palm Ave", "Cocoa Beach", "FL", "32931") == \
        "12 Palm Ave, Cocoa Beach, FL 32931"


def test_compose_skips_empty():
    assert addresses.compose_address("12 Palm Ave", "Cocoa Beach", "", "") == \
        "12 Palm Ave, Cocoa Beach"
    assert addresses.compose_address("", "", "", "") == ""
    assert addresses.compose_address("12 Palm Ave", "", "FL", "") == "12 Palm Ave, FL"


def test_parse_full():
    d = addresses.parse_address("12 Palm Ave, Cocoa Beach, FL 32931")
    assert d == {"street": "12 Palm Ave", "city": "Cocoa Beach", "state": "FL", "zip": "32931"}


def test_parse_no_zip():
    d = addresses.parse_address("12 Palm Ave, Cocoa Beach FL")
    assert d["street"] == "12 Palm Ave" and d["city"] == "Cocoa Beach" and d["state"] == "FL"
    assert d["zip"] == ""


def test_parse_city_state_zip_two_parts():
    d = addresses.parse_address("500 Ocean Blvd, Melbourne FL 32901")
    assert d["street"] == "500 Ocean Blvd" and d["city"] == "Melbourne"
    assert d["state"] == "FL" and d["zip"] == "32901"


def test_parse_unstructured_keeps_street_only():
    d = addresses.parse_address("behind the big blue gate")
    assert d["street"] == "behind the big blue gate"
    assert d["city"] == "" and d["state"] == "" and d["zip"] == ""


def test_parse_empty():
    assert addresses.parse_address("") == {"street": "", "city": "", "state": "", "zip": ""}


from sqlalchemy import select
from sqlalchemy.orm import Session
from app import migrate, models
from app.db import Base


def test_backfill_parses_legacy_address(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        cust = models.Customer(name="Z")
        p = models.Property(customer=cust, address="12 Palm Ave, Cocoa Beach, FL 32931")
        s.add(p); s.commit(); pid = p.id
    migrate.run(engine)
    with Session(engine) as s:
        p = s.get(models.Property, pid)
        assert p.city == "Cocoa Beach" and p.state == "FL" and p.zip == "32931"
        assert p.address == "12 Palm Ave, Cocoa Beach, FL 32931"  # untouched
