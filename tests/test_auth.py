"""Regression tests for the page gate (can_see_page least-privilege). Hashing,
session tokens and the login limits are home_core's and tested there; DB-bound
paths (login_token redemption, page_allowed_ids) are in test_permissions.py.

Run inside the api image (dida_api installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_auth.py"
"""
from dida_api.auth import AuthUser, can_see_page


def U(role="user", pages=None):
    return AuthUser(id=1, username="u", role=role, token_version=0, allowed_pages=pages, can_control=True)


def test_can_see_page():
    assert can_see_page(U("admin", ["entry"]), "cameras") is True, "admin sees any page"
    assert can_see_page(U("user", None), "cameras") is True, "unrestricted (pages=None) sees any page"
    assert can_see_page(U("user", ["entry"]), "entry") is True, "scoped user sees its own page"
    assert can_see_page(U("user", ["entry"]), "cameras") is False, "scoped user blocked from a foreign page"
    assert can_see_page(U("user", ["media", "cameras"]), "cameras") is True, "any-of match passes"
    assert can_see_page(U("user", ["media"]), "cameras", "floorplan") is False, "any-of: none present → blocked"
    assert can_see_page(U("user", []), "entry") is False, "empty allow-list blocks everything"
