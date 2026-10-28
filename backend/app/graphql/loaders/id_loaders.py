"""By-ID DataLoaders for core GraphQL entities.

Each factory function takes an ``AsyncSession`` and returns a
``strawberry.dataloader.DataLoader`` configured with the correct batch-load
function.  Consumers call the factory in the context-getter and use the
returned DataLoader in relationship resolvers.
"""

import asyncio
import uuid
from functools import partial

from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import DataLoader

from app.graphql.loaders.base import create_batch_load_fn
from app.models.attribute_type import AttributeType
from app.models.group import Group
from app.models.icon import Icon
from app.models.object import Object
from app.models.object_type import ObjectType
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.reference_type import ReferenceType
from app.models.schema import Schema
from app.models.source import Source
from app.models.user import User

__all__ = [
    "object_by_id_loader",
    "object_type_by_id_loader",
    "schema_by_id_loader",
    "source_by_id_loader",
    "icon_by_id_loader",
    "reference_type_by_id_loader",
    "attribute_type_by_id_loader",
    "object_type_attribute_by_id_loader",
    "user_by_id_loader",
    "group_by_id_loader",
]


def object_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``Object`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(Object)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def object_type_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``ObjectType`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(ObjectType)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def schema_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``Schema`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(Schema)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def source_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``Source`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(Source)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def icon_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``Icon`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(Icon)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def reference_type_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``ReferenceType`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(ReferenceType)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def attribute_type_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``AttributeType`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(AttributeType)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def object_type_attribute_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``ObjectTypeAttribute`` records by primary key."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(ObjectTypeAttribute)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def user_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``User`` records by primary key (D-17:
    ObjectValue.user_name resolution)."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(User)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))


def group_by_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads ``Group`` records by primary key (D-17:
    ObjectValue.group_name resolution)."""
    lock = lock or asyncio.Lock()
    load_fn = create_batch_load_fn(Group)
    return DataLoader(load_fn=partial(load_fn, session=session, lock=lock))