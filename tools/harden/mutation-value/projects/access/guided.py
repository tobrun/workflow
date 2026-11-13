from app import allowed


def test_admin_at_expiry():
    assert not allowed("admin", False, False, 100, 100)


def test_admin_just_before_expiry():
    assert allowed("admin", False, False, 101, 100)


def test_editor_without_ownership():
    assert not allowed("editor", False, False, 200, 100)


def test_viewer_with_ownership():
    assert not allowed("viewer", True, False, 200, 100)


def test_lock_denies_privileged_role():
    assert not allowed("admin", False, True, 200, 100)


def test_expiry_denies_privileged_role():
    assert not allowed("editor", True, False, 50, 100)
