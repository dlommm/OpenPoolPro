from app import tax


def test_parse_percent_ppm():
    assert tax.parse_percent("6.625") == 66250
    assert tax.parse_percent("0") == 0
    assert tax.parse_percent("7") == 70000
    assert tax.parse_percent("") == 0
    assert tax.parse_percent("  6.5 ") == 65000
    assert tax.parse_percent("garbage") == 0
    assert tax.parse_percent("-3") == 0          # no negative rates


def test_format_ppm_trims():
    assert tax.format_ppm(66250) == "6.625"
    assert tax.format_ppm(70000) == "7"
    assert tax.format_ppm(0) == "0"
    assert tax.format_ppm(65000) == "6.5"


def test_format_parse_roundtrip():
    for s in ["0", "4", "6.5", "6.625", "8.375", "10.25"]:
        assert tax.format_ppm(tax.parse_percent(s)) == s


def test_tax_cents_rounds():
    assert tax.tax_cents(22200, 66250) == 1471      # $222.00 @ 6.625% = $14.71
    assert tax.tax_cents(0, 66250) == 0
    assert tax.tax_cents(18000, 0) == 0
    assert tax.tax_cents(5500, 60000) == 330        # $55 @ 6% = $3.30
