from typing import Any
from sqlalchemy import String
from sqlalchemy import TypeDecorator
from sqlalchemy.engine.interfaces import Dialect
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
import os

# Always a MultiFernet — with one key unless a rotation is in progress, so
# encrypt/decrypt behave exactly as a bare Fernet does for existing installs.
_fernet: MultiFernet | None = None

VERSION_PREFIX = b"v1:"


def init_fernet() -> MultiFernet:
    """Initialize the global MultiFernet instance from the FERNET_KEY env vars.

    Called from lifespan startup. Raises RuntimeError if FERNET_KEY is missing
    or invalid (not a valid 32-byte urlsafe-base64-encoded key).

    ``FERNET_KEY_OLD`` is optional and exists only for a key rotation: when set,
    it is appended as the second key, so ciphertext written under the old key
    stays readable while every new write uses ``FERNET_KEY``. Unset or empty
    leaves a single-key MultiFernet, which is the normal state. An invalid
    ``FERNET_KEY_OLD`` raises instead of being dropped — a silently ignored old
    key would degrade every value it protects to ``[INVALID_TOKEN]``.
    """
    global _fernet
    key = os.environ.get("FERNET_KEY")
    if not key:
        raise RuntimeError(
            "FERNET_KEY environment variable is required but not set. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    try:
        keys = [Fernet(key.encode() if isinstance(key, str) else key)]
    except Exception as e:
        raise RuntimeError(
            f"FERNET_KEY is invalid: {e}. "
            "Generate a valid key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    old_key = os.environ.get("FERNET_KEY_OLD")
    if old_key:
        try:
            keys.append(Fernet(old_key.encode() if isinstance(old_key, str) else old_key))
        except Exception as e:
            raise RuntimeError(
                f"FERNET_KEY_OLD is invalid: {e}. It is only set during a key rotation — "
                "either supply the previous key in the same format as FERNET_KEY or unset it."
            )
    _fernet = MultiFernet(keys)
    return _fernet


def get_fernet() -> MultiFernet:
    """Return the global MultiFernet instance. Raises RuntimeError if not initialized."""
    if _fernet is None:
        raise RuntimeError("Fernet not initialized — call init_fernet() during startup")
    return _fernet


class EncryptedString(TypeDecorator):
    """SQLAlchemy TypeDecorator for Fernet-encrypted string columns.

    Encrypts values on write (process_bind_param), decrypts on read
    (process_result_value). Prepends the "v1:" version prefix; the global
    MultiFernet carries the previous key while a rotation is in progress, so
    old ciphertext keeps decrypting while new writes use the current key.

    Usage:
        source_bearer_token: Mapped[str] = mapped_column(EncryptedString(800))
    """

    impl = String
    cache_ok = True

    def __init__(self, length: int = 800, **kwargs: Any) -> None:
        super().__init__(length=length, **kwargs)

    @property
    def impl_cache_key(self) -> Any:
        """Include length in cache key to prevent collisions."""
        return self.impl.length

    def process_bind_param(
        self, value: str | None, dialect: Dialect
    ) -> str | None:
        """Encrypt plaintext value before writing to database."""
        if value is None:
            return None
        fernet = get_fernet()
        ciphertext: bytes = fernet.encrypt(value.encode("utf-8"))
        stored: bytes = VERSION_PREFIX + ciphertext
        return stored.decode("utf-8")

    def process_result_value(
        self, value: str | None, dialect: Dialect
    ) -> str | None:
        """Decrypt ciphertext value after reading from database."""
        if value is None:
            return None
        stored = value.encode("utf-8")
        if not stored.startswith(VERSION_PREFIX):
            # Handle legacy unencrypted data (migration in progress)
            return value
        ciphertext = stored[len(VERSION_PREFIX):]
        fernet = get_fernet()
        try:
            return fernet.decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            # Corrupted or tampered data — return masked instead of crashing
            return "[INVALID_TOKEN]"
