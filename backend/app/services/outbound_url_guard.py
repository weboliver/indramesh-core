"""The single outbound-URL predicate for connector endpoints (SESS-07, D-06).

One predicate, applied at three moments:

**Save time** (Core: ``source_service`` / ``target_service``)
    :func:`check_resolved_target` runs before the row is written and checks BOTH
    the literal host and the host's **resolved** answer set. DNS resolution is
    part of the save-time decision: the earlier "literal only at save,
    resolution only at connect" split is superseded by D-06. The lookup is
    bounded by :data:`RESOLVE_TIMEOUT_SECONDS` and goes through the module-level
    :func:`resolve_host_addresses` seam, so the whole backend suite can answer
    every lookup offline and no save test reaches a real resolver.

**Connect time** (plugin: every pull/push — 321-05)
    The same function is called again against a FRESH answer, because the
    save-time answer can be stale, and with ``fail_closed=True``.

**The per-endpoint flag**
    ``config["allow_internal_target"] is True`` short-circuits both moments: a
    flagged endpoint is that operator's explicit declaration that this URL is
    internal, and it costs no lookup at all. The flag is read from the endpoint
    row's own config and nowhere else; there is deliberately no process-wide
    switch that disables the guard.

Save-time failure mode, deliberately chosen rather than a gap: a resolution
that answers nothing at all (NXDOMAIN, timeout, resolver error) yields **no
save-time verdict**, so the save proceeds and the connect-time check decides.
A host that answers is checked at save; a host that answers nothing cannot
point anywhere yet. ``fail_closed`` is the switch that turns that no-answer
case into a refusal, and the connect-time caller is the one that passes it —
the layer about to dial a socket must never dial a host it could not vet.
"""

from __future__ import annotations

import asyncio
import ipaddress
from urllib.parse import urlsplit

#: The one config key an endpoint row may carry to waive the address predicate
#: for itself. Only the literal boolean ``True`` is accepted.
ALLOW_INTERNAL_TARGET_KEY = "allow_internal_target"

#: The one bound on a save-time lookup. 321-05 re-exports this to plugins as
#: ``OUTBOUND_RESOLVE_TIMEOUT_SECONDS``, so no plugin seam carries a second
#: literal.
RESOLVE_TIMEOUT_SECONDS = 3.0

_ALLOWED_SCHEMES = frozenset({"http", "https"})

_REFUSAL_MESSAGES = {
    "scheme": "outbound URL refused: the URL scheme must be http or https",
    "host": "outbound URL refused: the URL host is not allowed",
    "address": "outbound URL refused: the URL targets a non-public address",
}


class OutboundUrlRefused(ValueError):
    """An endpoint URL is not allowed to be dialled by Core or a connector.

    Subclasses ``ValueError`` so the Source/Target routes' existing
    ``ValueError`` -> 422 mapping applies without a second translation path.
    The message names only the reason (``scheme``, ``host`` or ``address``) and
    never echoes the full submitted URL.
    """


def _refuse(reason: str) -> None:
    raise OutboundUrlRefused(_REFUSAL_MESSAGES[reason])


def read_allow_internal_flag(config: dict | None) -> bool:
    """Whether ``config`` carries ``allow_internal_target`` as the boolean ``True``.

    Identity, never truthiness: the Jira connector persists submitted config
    unnormalized, so ``"true"``, ``1`` and ``"yes"`` are all refused (T-321-15).
    """
    return isinstance(config, dict) and config.get(ALLOW_INTERNAL_TARGET_KEY) is True


def is_public_address(host: str) -> bool:
    """Whether ``host`` is an IP literal that is globally routable.

    ``False`` for every non-global family (loopback, link-local including the
    metadata address, private, CGNAT, ULA, reserved) and for anything that is not
    an IP literal at all.
    """
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


async def resolve_host_addresses(host: str) -> list[str]:
    """The default resolver seam: every unique address ``host`` answers with.

    Module-level and looked up at call time, so a test (or the backend suite's
    autouse conftest double) can replace it with a fake that never touches the
    network. Raises ``OSError`` (including ``socket.gaierror``) on failure.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None)
    addresses: list[str] = []
    seen: set[str] = set()
    for info in infos:
        address = info[4][0]
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return addresses


def check_url_literal(url: str | None) -> None:
    """Refuse an outbound URL by its literal form only — this performs no DNS.

    Refuses a scheme outside ``{"http", "https"}``, a missing hostname, the
    hostname ``localhost`` or any name ending in ``.localhost``, and an IP
    literal that is not globally routable. An absent/blank URL is not a target
    and is a no-op (a Jira Source may legitimately carry no URL; only its Target
    requires one).
    """
    if url is None or not str(url).strip():
        return
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname
    except ValueError:
        _refuse("host")
        return
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        _refuse("scheme")
    if not host:
        _refuse("host")
    host = host.lower()
    if host == "localhost" or host.endswith(".localhost"):
        _refuse("host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        _refuse("address")


async def check_resolved_target(
    url: str | None,
    *,
    allow_internal: bool = False,
    fail_closed: bool = False,
    resolver=None,
    timeout: float = RESOLVE_TIMEOUT_SECONDS,
) -> None:
    """Refuse an outbound URL against its literal host AND its resolved answers.

    The single entry point Core and the plugin share. Returns immediately when
    ``allow_internal`` is true, so a flagged endpoint costs no lookup at all.

    ``fail_closed`` selects what a no-answer resolution means: the default
    ``False`` (save time) produces no verdict — the connect-time check decides —
    while ``True`` (connect time, 321-05) raises, because the layer about to
    dial must not dial a host it could not vet.
    """
    if allow_internal:
        return
    check_url_literal(url)
    if url is None:
        return
    try:
        host = urlsplit(str(url).strip()).hostname
    except ValueError:
        _refuse("host")
        return
    if not host:
        _refuse("host")
    host = host.lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # check_url_literal already refused a non-global literal; nothing to
        # resolve for an address that is dialled directly.
        return

    try:
        answers = await asyncio.wait_for(
            (resolver or resolve_host_addresses)(host), timeout
        )
    except (OSError, asyncio.TimeoutError):
        if fail_closed:
            _refuse("address")
        return
    if not answers:
        if fail_closed:
            _refuse("address")
        return
    for address in answers:
        if not is_public_address(address):
            # One bad answer is enough; never "all must be internal".
            _refuse("address")
