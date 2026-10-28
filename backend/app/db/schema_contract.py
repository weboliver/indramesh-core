"""Indexes the Core migration baseline owns, registered on the model metadata.

The Core schema's performance indexes were introduced by explicit Alembic
revisions (phase-104 hot paths, phase-237 structured-Browse set, phase-241
measured FK set) rather than by ``index=True``/``Index(...)`` on the ORM
models. That was deliberate — the project ships an index only after the
normal planner has been proven to select it (D-05) — but it left the model
metadata blind to indexes the database actually carried, so
``alembic check`` reported them as removals on every run.

Since the Phase 317 squash the single ``initial`` baseline creates those
indexes (``backend/alembic/versions/initial.py``); later Core revisions add new
ones. This module registers the same definitions on ``Base.metadata`` so that
metadata and a freshly migrated database agree: ``alembic check`` compares the
real contract instead of proposing to drop indexes the baseline just installed.

Two rules for this module:

* It only *declares* definitions. It never executes DDL; the Core migration
  chain remains the single place an index is created.
* Every entry must match the creating migration's definition exactly (name,
  columns, order, access method, operator class, predicate). A definition that
  drifts from the migration makes ``alembic check`` report a drop/create pair,
  which is the intended alarm, not something to silence.

It is imported at the end of ``app/models/__init__.py`` — the same barrel
import every metadata consumer already performs (``alembic/env.py``,
``plugin_system/database_registry.py``, the test fixtures and
``scripts/security/it314_db.py``).
"""

from sqlalchemy import Index, func, text

from app.db.base import Base


def _index(name: str, table_name: str, columns: tuple[str, ...], **kwargs) -> None:
    table = Base.metadata.tables[table_name]
    Index(name, *(table.c[column] for column in columns), **kwargs)


def _register_indexes() -> None:
    # --- object_refs: reference traversal and the AQL reference join ---------
    _index(
        "idx_object_refs_from_attr_from_obj",
        "object_refs",
        ("from_attribute_name", "from_object_id"),
    )
    _index("idx_object_refs_from_object_id", "object_refs", ("from_object_id",))
    _index("idx_object_refs_to_object_id", "object_refs", ("to_object_id",))
    _index("ix_object_refs_object_type_id", "object_refs", ("object_type_id",))
    _index("ix_object_refs_source_id", "object_refs", ("source_id",))

    # --- object_type_attributes / object_types: hierarchy and attribute joins -
    _index(
        "idx_object_type_attributes_attribute_type_id",
        "object_type_attributes",
        ("attribute_type_id",),
    )
    _index(
        "idx_object_type_attributes_object_type_id",
        "object_type_attributes",
        ("object_type_id",),
    )
    _index(
        "idx_object_type_attributes_reference_type_id",
        "object_type_attributes",
        ("reference_type_id",),
    )
    _index(
        "idx_object_type_attributes_referenced_object_type_id",
        "object_type_attributes",
        ("referenced_object_type_id",),
    )
    _index("idx_object_types_parent_id", "object_types", ("parent_id",))
    _index("idx_object_types_schema_id", "object_types", ("schema_id",))

    # --- object_values: typed Browse ranges, object and attribute lookups -----
    _index(
        "idx_object_values_attr_datetime",
        "object_values",
        ("object_type_attribute_id", "value_datetime"),
    )
    _index(
        "idx_object_values_attr_float",
        "object_values",
        ("object_type_attribute_id", "value_float"),
    )
    _index(
        "idx_object_values_attr_integer",
        "object_values",
        ("object_type_attribute_id", "value_integer"),
    )
    _index("idx_object_values_object_id", "object_values", ("object_id",))
    _index(
        "idx_object_values_object_type_attribute_id",
        "object_values",
        ("object_type_attribute_id",),
    )
    _index(
        "idx_object_values_value_simple_trgm",
        "object_values",
        ("value_simple",),
        postgresql_using="gin",
        postgresql_ops={"value_simple": "gin_trgm_ops"},
    )
    object_values = Base.metadata.tables["object_values"]
    Index(
        "idx_object_values_attr_text_md5",
        object_values.c.object_type_attribute_id,
        func.md5(object_values.c.value_simple),
        postgresql_where=text("value_simple IS NOT NULL"),
    )

    # --- objects: schema scope, the typed list hot path and name search ------
    _index(
        "idx_objects_name_trgm",
        "objects",
        ("name",),
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    _index("idx_objects_schema_id", "objects", ("schema_id",))
    objects = Base.metadata.tables["objects"]
    Index(
        "idx_objects_type_created_desc",
        objects.c.object_type_id,
        objects.c.created_at.desc(),
    )

    # --- remaining FK / list-path indexes from the explicit revisions --------
    _index("idx_schemas_source_id", "schemas", ("source_id",))
    _index("idx_statuses_schema_id", "statuses", ("schema_id",))
    _index("idx_tasks_created_at", "tasks", ("created_at",))
    _index("idx_tasks_status", "tasks", ("current_status",))
    _index(
        "ix_export_definitions_object_type_id",
        "export_definitions",
        ("object_type_id",),
    )
    _index("ix_export_definitions_target_id", "export_definitions", ("target_id",))
    _index(
        "ix_import_definitions_identity_attr_id",
        "import_definitions",
        ("identity_attr_id",),
    )
    _index("ix_index_outbox_object_id", "index_outbox", ("object_id",))
    _index("ix_index_outbox_status", "index_outbox", ("status",))
    _index("ix_permissions_resource", "permissions", ("resource",))
    _index(
        "ix_role_permissions_permission_id",
        "role_permissions",
        ("permission_id",),
    )
    _index("ix_role_permissions_role_id", "role_permissions", ("role_id",))
    _index("ix_task_logs_emitted_at", "task_logs", ("emitted_at",))
    _index(
        "ix_task_logs_task_id_level_sequence",
        "task_logs",
        ("task_id", "level", "sequence"),
    )
    _index("ix_tasks_schedule_id", "tasks", ("schedule_id",))

    # --- reference_types: the global-row uniqueness the ORM cannot express ----
    reference_types = Base.metadata.tables["reference_types"]
    Index(
        "uq_reference_types_reference_null_schema",
        reference_types.c.reference,
        unique=True,
        postgresql_where=text("schema_id IS NULL"),
    )


_register_indexes()
