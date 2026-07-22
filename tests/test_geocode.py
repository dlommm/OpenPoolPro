from app import geocode


def _fake_fetch(payload):
    return lambda url: payload


def test_no_key_returns_none():
    assert geocode.geocode("x", "") is None


def test_parses_lat_lon():
    payload = {"features": [{"properties": {"lat": 28.32, "lon": -80.61}}]}
    assert geocode.geocode("12 Palm Ave", "KEY", fetch=_fake_fetch(payload)) == (28.32, -80.61)


def test_empty_features_returns_none():
    assert geocode.geocode("x", "KEY", fetch=_fake_fetch({"features": []})) is None


def test_fetch_error_returns_none():
    def boom(url):
        raise OSError("network down")
    assert geocode.geocode("x", "KEY", fetch=boom) is None
