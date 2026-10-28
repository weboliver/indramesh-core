"""Base utilities for DataLoader batching — key-ordering preservation and batch-load function factory."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "keyed_results",
    "create_batch_load_fn",
]

T = TypeVar("T")
K = TypeVar("K")
M = TypeVar("M")


def keyed_results(
    results: list[T],
    keys: list[K],
    key_attr: str = "id",
) -> list[T | None]:
    """Map a list of ORM results back to the order of the input keys.

    SQLAlchemy's ``WHERE id IN (...)`` returns rows in DB-index order, not
    input-key order.  This helper reconstructs the input order so the
    DataLoader contract (output in the same order as input keys) is satisfied.

    Parameters
    ----------
    results : list[T]
        ORM instances returned by the database query.
    keys : list[K]
        Input key values whose order must be preserved in the output.
    key_attr : str (default ``"id"``)
        Attribute name on ``T`` instances to match against ``keys``.

    Returns
    -------
    list[T | None]
        One entry per key — ``None`` for keys not found in ``results``.
    """
    result_map = {getattr(r, key_attr): r for r in results}
    return [result_map.get(k) for k in keys]


def create_batch_load_fn(
    model_class: type[M],
    key_attr: str = "id",
) -> Callable[[list[Any], AsyncSession], Awaitable[list[M | None]]]:
    """Return a batch ``load_fn`` suitable for ``strawberry.dataloader.DataLoader``.

    The returned closure has the signature::

        async def _load_fn(keys: list, session: AsyncSession, lock: asyncio.Lock) -> list[M | None]:

    The ``session`` and ``lock`` are **explicit parameters** so that consumers
    can bind them with ``functools.partial`` at DataLoader creation time::

        DataLoader(load_fn=partial(create_batch_load_fn(Object), session=ctx.session, lock=ctx.lock))

    ``lock`` serializes access to the shared per-request ``session``: every
    DataLoader instance is bound to the same session, and graphql-core
    resolves sibling/list fields concurrently via asyncio.gather, so two
    different loaders' batch dispatches can otherwise race on one
    AsyncSession (which rejects overlapping operations).

    Parameters
    ----------
    model_class : type[M]
        SQLAlchemy model class (e.g. ``Object``, ``ObjectType``).
    key_attr : str (default ``"id"``)
        Column/attribute to filter on (e.g. ``"id"``, ``"schema_id"``).

    Returns
    -------
    Callable[[list, AsyncSession, asyncio.Lock], Awaitable[list[M | None]]]
    """

    async def _load_fn(
        keys: list[Any],
        session: AsyncSession,
        lock: asyncio.Lock,
    ) -> list[M | None]:
        stmt = select(model_class).where(
            getattr(model_class, key_attr).in_(keys)
        )
        async with lock:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        return keyed_results(rows, keys, key_attr)

    return _load_fn