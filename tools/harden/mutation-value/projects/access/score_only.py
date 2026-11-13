"""Diagnostic ablation: address only the two literal surviving mutations."""
from app import allowed


def test_admin_at_expiry():
    assert not allowed("admin", False, False, 100, 100)


def test_editor_without_ownership():
    assert not allowed("editor", False, False, 200, 100)
