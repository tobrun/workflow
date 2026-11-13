from app import allowed


def test_admin_owner():
    assert allowed("admin", True, False, 300, 100)


def test_editor_nonowner():
    assert not allowed("editor", False, False, 300, 100)


def test_viewer_owner():
    assert not allowed("viewer", True, False, 300, 100)


def test_unknown_role():
    assert not allowed("guest", True, False, 300, 100)


def test_locked_admin():
    assert not allowed("admin", False, True, 300, 100)


def test_expired_admin():
    assert not allowed("admin", False, False, 50, 100)
