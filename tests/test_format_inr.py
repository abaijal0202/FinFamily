"""Indian digit-grouping currency filter (12,34,567 not 1,234,567)."""
from app import format_inr


def test_small_numbers_no_grouping():
    assert format_inr(0) == "0"
    assert format_inr(42) == "42"
    assert format_inr(999) == "999"


def test_thousands_single_group():
    assert format_inr(1000) == "1,000"
    assert format_inr(12345) == "12,345"


def test_lakhs_and_crores():
    assert format_inr(100000) == "1,00,000"          # 1 lakh
    assert format_inr(1234567) == "12,34,567"
    assert format_inr(90780049.51, 2) == "9,07,80,049.51"  # ~9 crore, matches CAS sub total


def test_decimals():
    assert format_inr(1375.2, 2) == "1,375.20"
    assert format_inr(756360.0, 2) == "7,56,360.00"


def test_negative_numbers():
    assert format_inr(-50000) == "-50,000"
    assert format_inr(-1234567.89, 2) == "-12,34,567.89"


def test_none_and_falsy_treated_as_zero():
    assert format_inr(None) == "0"
    assert format_inr(0.0) == "0"
