"""System-admin allowlist — the SINGLE backend home for the emails
allowed to see /system-health and receive the daily health email.

This is deliberately narrower than role='admin': regular admins run
stores; these three run the PLATFORM. The frontend mirrors the list in
src/lib/systemAdmins.ts purely to hide the menu item — the API guard
here is the real enforcement.
"""
from __future__ import annotations

SYSTEM_ADMIN_EMAILS: frozenset[str] = frozenset({
    "itsupport@vivofashiongroup.com",
    "peter@vivofashiongroup.com",
    "stephen@vivofashiongroup.com",
})


def is_system_admin(user_or_email) -> bool:
    """True only when the user's email is in SYSTEM_ADMIN_EMAILS.
    Accepts a User ORM row (reads .email) or a bare email string."""
    email = getattr(user_or_email, "email", user_or_email)
    if not isinstance(email, str):
        return False
    return email.strip().lower() in SYSTEM_ADMIN_EMAILS
