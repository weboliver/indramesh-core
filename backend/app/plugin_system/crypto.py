"""Field-level Fernet helpers for plugin JSONB config (D-12).

Secret values live inline in the ``plugins.config`` JSONB document, encrypted
per field with the SAME Fernet mechanism and ``v1:`` version prefix
``app.crypto.EncryptedString`` already uses. There is deliberately no second
encryption mechanism here — ``get_fernet()`` is reused directly.

``init_fernet()`` is NOT called here: it is already a process-wide singleton
initialized once in ``main.py``'s lifespan and ``worker.py``'s
``startup_recovery`` (and once per test process by the test fixtures).

``SECRET_SENTINEL`` is the single source of truth for D-11's masking marker;
Plan 282-07's GET/PUT config endpoint reuses it.
"""

import copy

from cryptography.fernet import InvalidToken

from app.crypto import VERSION_PREFIX, get_fernet
from app.plugin_system.validation import secret_field_names

# D-11: the fixed value returned in place of a stored secret, and accepted on
# PUT as "leave this secret unchanged". Chosen to be impossible to collide with
# a legitimate user-entered value.
SECRET_SENTINEL = "__INDRAMESH_SECRET_UNCHANGED__"

INVALID_TOKEN_PLACEHOLDER = "[INVALID_TOKEN]"


def encrypt_secret_field(value: str) -> str:
    """Encrypt one plaintext secret value, ``v1:``-prefixed (D-12).

    A schema that marks a field ``format: password`` without also declaring
    ``type: string`` can let a non-string payload through validation — coerce
    to ``str`` rather than letting ``.encode()`` raise ``AttributeError``.
    """
    if not isinstance(value, str):
        value = str(value)
    ciphertext: bytes = get_fernet().encrypt(value.encode("utf-8"))
    return (VERSION_PREFIX + ciphertext).decode("utf-8")


def decrypt_secret_field(value: str | None) -> str | None:
    """Decrypt one stored secret value.

    Mirrors ``EncryptedString.process_result_value``: a value without the ``v1:``
    prefix passes through unchanged (legacy plaintext), and an ``InvalidToken``
    masks to ``[INVALID_TOKEN]`` rather than raising. A non-string stored value
    (e.g. written before the field was marked secret) also passes through
    unchanged rather than crashing on ``.encode()``.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        return value

    stored = value.encode("utf-8")
    if not stored.startswith(VERSION_PREFIX):
        return value

    ciphertext = stored[len(VERSION_PREFIX):]
    try:
        return get_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken:
        # Corrupted or tampered data — mask instead of crashing a config read.
        return INVALID_TOKEN_PLACEHOLDER


def _split_path(path: str) -> list[str]:
    return path.split(".")


def _get_path(document: dict, path: str) -> tuple[bool, object]:
    """Return ``(present, value)`` for a dotted ``path`` inside ``document``."""
    current: object = document
    for part in _split_path(path):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _set_path(document: dict, path: str, value: object) -> None:
    """Set a dotted ``path`` in ``document``, creating intermediate dicts."""
    parts = _split_path(path)
    current = document
    for part in parts[:-1]:
        nested = current.get(part)
        if not isinstance(nested, dict):
            nested = {}
            current[part] = nested
        current = nested
    current[parts[-1]] = value


def _delete_path(document: dict, path: str) -> None:
    """Delete a dotted ``path`` from ``document``; absent paths are a no-op."""
    parts = _split_path(path)
    current: object = document
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)


def mask_secret_fields(config: dict, config_schema: dict) -> dict:
    """Return a deep copy of ``config`` with secret values sentinel-masked (D-11).

    Nested-aware: each dotted ``secret_field_names()`` path that is present is
    replaced with ``SECRET_SENTINEL``; absent paths stay absent.
    """
    masked = copy.deepcopy(config)
    for path in secret_field_names(config_schema):
        present, value = _get_path(masked, path)
        if present and value is not None:
            _set_path(masked, path, SECRET_SENTINEL)
    return masked


def encrypt_config_secrets(
    new_payload: dict, config_schema: dict, existing_stored: dict
) -> dict:
    """Apply D-11's PUT semantics to a submitted config payload.

    For every ``format:password`` field at any object depth: a submitted
    ``SECRET_SENTINEL``, a missing path, or ``None`` carries over
    ``existing_stored``'s already-encrypted value unchanged; any other submitted
    value is treated as new plaintext and encrypted via ``encrypt_secret_field``.
    Non-secret fields pass through ``new_payload`` as-is. Neither input dict is
    mutated.
    """
    result = copy.deepcopy(new_payload)

    for path in secret_field_names(config_schema):
        present, submitted = _get_path(new_payload, path)
        if not present or submitted is None or submitted == SECRET_SENTINEL:
            stored_present, stored_value = _get_path(existing_stored, path)
            if stored_present:
                _set_path(result, path, stored_value)
            else:
                _delete_path(result, path)
        else:
            _set_path(result, path, encrypt_secret_field(submitted))

    return result


__all__ = [
    "SECRET_SENTINEL",
    "INVALID_TOKEN_PLACEHOLDER",
    "encrypt_secret_field",
    "decrypt_secret_field",
    "mask_secret_fields",
    "encrypt_config_secrets",
]
