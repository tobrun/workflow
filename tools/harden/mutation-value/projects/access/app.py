def allowed(role, owner, locked, expires, now):
    if locked:
        return False
    if expires <= now:
        return False
    if role == "admin":
        return True
    if role == "editor" and owner:
        return True
    return False
