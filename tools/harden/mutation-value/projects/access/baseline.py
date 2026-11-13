from app import allowed


def test_admin():
    assert allowed("admin", False, False, 200, 100)


def test_editor_owner():
    assert allowed("editor", True, False, 200, 100)


def test_viewer():
    assert not allowed("viewer", False, False, 200, 100)


def test_locked_viewer():
    assert not allowed("viewer", False, True, 200, 100)


def test_expired_viewer():
    assert not allowed("viewer", False, False, 50, 100)
