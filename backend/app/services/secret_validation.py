"""Validation of the JWT signing key (SESS-04).

One rule, used by the startup lifespan in ``app.main`` and by
``auth_service.get_secret_key``, so the two paths cannot drift: the key must be
present and at least ``JWT_SECRET_KEY_MIN_LENGTH`` characters long.

The length is Python ``len()`` over the value read from the environment —
characters, not bytes — and the comparison is an integer ``<`` against the
documented minimum with no rounding, so exactly 32 passes and exactly 31 fails.

A short key is not a formatting nit: HS256 is a symmetric algorithm, so a key of
a few bytes is brute-forceable offline from a single known token. The value is
never echoed and never logged; only its length is reported.
"""

JWT_SECRET_KEY_MIN_LENGTH = 32

_GENERATE_HINT = (
    'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(32))"'
)


def validate_jwt_secret_key(value: str | None) -> None:
    """Refuse an absent, empty or too-short JWT signing key, naming the variable.

    Takes a plain value rather than reading the environment itself, so it is
    testable without booting the app and so both call sites pass exactly what
    they read.
    """
    if not value:
        raise RuntimeError(
            "JWT_SECRET_KEY environment variable is required but not set. "
            + _GENERATE_HINT
        )
    if len(value) < JWT_SECRET_KEY_MIN_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET_KEY must be at least {JWT_SECRET_KEY_MIN_LENGTH} characters "
            f"(got {len(value)}). {_GENERATE_HINT}"
        )
