from app import total


def test_zero_is_valid():
    assert total(0, False, "") == 5


def test_member_at_threshold():
    assert total(100, True, "") == 80


def test_nonmember_at_threshold():
    assert total(100, False, "") == 100


def test_member_below_threshold():
    assert total(99, True, "") == 104


def test_stacked_discount_exact_amount():
    assert total(150, True, "SAVE10") == 120


def test_coupon_exact_amount():
    assert total(50, False, "SAVE10") == 45
