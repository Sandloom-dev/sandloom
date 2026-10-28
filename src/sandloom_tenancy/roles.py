"""Organization role constants and hierarchy (ORM-free, safe for API layer)."""

ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"

ORG_ROLES = (ROLE_MEMBER, ROLE_ADMIN, ROLE_OWNER)
_ROLE_RANK = {role: rank for rank, role in enumerate(ORG_ROLES)}


def role_rank(role: str) -> int:
    """Rank of a held role; an unknown role ranks below every real role."""
    return _ROLE_RANK.get(role, -1)


def role_at_least(role: str, minimum: str) -> bool:
    """Return True if `role` grants at least `minimum` privileges.

    Fails closed: an unknown ``minimum`` is a programming/configuration error
    and raises ``ValueError`` instead of silently degrading to the lowest rank
    (which would turn e.g. a typo'd ``"admn"`` gate into a member gate).
    """
    if minimum not in _ROLE_RANK:
        raise ValueError(
            f"Unknown organization role {minimum!r}; expected one of {ORG_ROLES}"
        )
    return role_rank(role) >= _ROLE_RANK[minimum]
