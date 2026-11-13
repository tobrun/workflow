import pytest
from app import total


def test_regular():
    assert total(50, False, "") == 55


def test_discounted():
    assert total(150, True, "SAVE10") < 150


def test_negative():
    with pytest.raises(ValueError):
        total(-10, False, "")


def test_unknown_coupon():
    with pytest.raises(ValueError):
        total(50, False, "BAD")


def test_floor():
    assert total(5, False, "SAVE10") == 5
