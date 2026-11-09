"""Per-account login throttle (SESS-03 / D-04).

Once the same account has failed ``THROTTLE_THRESHOLD`` times, every further
attempt is delayed by an exponentially growing amount, capped at
``THROTTLE_CAP_SECONDS``; the counter decays after ``THROTTLE_DECAY_SECONDS``.
Nothing is ever locked: a hard lockout would itself be a denial-of-service
vector against a known account.

The submitted username is stored only as a SHA-256 digest, and the delay is
applied for every submitted name — including one that does not exist — so the
throttle can never become the account-existence oracle that the bcrypt
equalisation in ``auth_service._authenticate`` just removed.

Asymmetry (deliberate — do not "unify" the two): this throttle fails **open**,
an unreachable Valkey means no delay and never a failed login, because it is a
mitigation. The revocation blocklist in ``auth_service`` fails **closed**
because it is an authentication control.
"""

import asyncio
import hashlib

from redis.exceptions import RedisError

from app.db.redis import get_redis

THROTTLE_THRESHOLD = 3
THROTTLE_BASE_SECONDS = 0.5
THROTTLE_CAP_SECONDS = 8.0
THROTTLE_DECAY_SECONDS = 900
THROTTLE_KEY_PREFIX = "login_fail:"


async def _sleep(seconds: float) -> None:
    """Sleep seam: the delay schedule is assertable without waiting for it."""
    await asyncio.sleep(seconds)


def delay_seconds(failures: int) -> float:
    """Delay before an attempt, given how many failures are already recorded.

    Pure and total: 0 below the threshold, then the delay doubles per further
    failure and stops at the cap. The attempt after the third recorded failure
    is the first slowed one (D-04).
    """
    if failures < THROTTLE_THRESHOLD:
        return 0.0
    return min(
        THROTTLE_CAP_SECONDS,
        THROTTLE_BASE_SECONDS * 2 ** (failures - THROTTLE_THRESHOLD),
    )


def _key(username: str) -> str:
    """Key on a digest, so the submitted username is never stored or logged."""
    digest = hashlib.sha256(username.strip().lower().encode()).hexdigest()
    return f"{THROTTLE_KEY_PREFIX}{digest}"


async def failure_count(username: str) -> int:
    """Recorded failures for this account; 0 when the store is unavailable."""
    try:
        client = await get_redis()
        raw = await client.get(_key(username))
    except (RuntimeError, RedisError):
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


async def apply_delay(username: str) -> None:
    """Delay this attempt; fail open so an outage never blocks a login."""
    await _sleep(delay_seconds(await failure_count(username)))


async def record_failure(username: str) -> None:
    """Count one failed attempt and refresh its decay window."""
    try:
        client = await get_redis()
        key = _key(username)
        await client.incr(key)
        await client.expire(key, THROTTLE_DECAY_SECONDS)
    except (RuntimeError, RedisError):
        return


async def clear_failures(username: str) -> None:
    """Reset the counter after a successful login, so nobody stays slowed."""
    try:
        client = await get_redis()
        await client.delete(_key(username))
    except (RuntimeError, RedisError):
        return
