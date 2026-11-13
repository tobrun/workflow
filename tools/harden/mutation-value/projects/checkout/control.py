from app import total


def test_large_regular():
    assert total(200, False, "") == 200


def test_large_member():
    assert total(200, True, "") == 180


def test_large_coupon():
    assert total(200, False, "SAVE10") == 190


def test_combined_discount():
    assert total(200, True, "SAVE10") == 170


def test_small_member():
    assert total(40, True, "") == 45


def test_small_coupon():
    assert total(40, False, "SAVE10") == 35
