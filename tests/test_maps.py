from app import maps


def test_none_without_key_or_coords():
    assert maps.static_map_url(28.3, -80.6, "") is None
    assert maps.static_map_url(None, -80.6, "KEY") is None
    assert maps.static_map_url(28.3, None, "KEY") is None


def test_builds_url():
    url = maps.static_map_url(28.3, -80.6, "KEY")
    assert url.startswith("https://maps.geoapify.com/v1/staticmap")
    assert "lonlat:-80.6,28.3" in url and "apiKey=KEY" in url
