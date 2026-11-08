"""Identity is the token issuer (moved from the kernel's
``test_asymmetric_jwt.py``, KB-05c).

The startup role check reads which loaded module issues tokens; the bundled
identity module declares itself the issuer. The kernel's own suite keeps the
asymmetric token, rotation and role-check tests.
"""

from __future__ import annotations


def test_identity_declares_itself_the_token_issuer() -> None:
    import inspect

    import sandloom_identity as identity_pkg

    assert "issues_tokens=True" in inspect.getsource(identity_pkg)
