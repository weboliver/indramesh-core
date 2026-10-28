"""initial — the single Core migration baseline (Phase 317, D-10)

The 104-revision Core chain is squashed into this one baseline. Everything
`alembic revision --autogenerate` can see was generated from the Core models
(`app.models`); everything it cannot see is hand-carried below, in this order:

* the three extensions the schema depends on — ``age`` (graph), ``vector``
  (pgvector) and ``pg_trgm`` (trigram hot-path indexes). Autogenerate never
  emits ``CREATE EXTENSION``; ``pg_trgm`` used to be installed by the
  phase-237 index toolkit rather than by a plain statement.
* the ``sources.source_url_revision`` trigger and its plpgsql function
  (formerly revision ``292_source_url_revision``). The trigger — not a Python
  service — is authoritative for the monotonic revision counter, so every
  write path advances it identically.
* the partial unique index ``uq_reference_types_reference_null_schema``
  (``reference_types (reference) WHERE schema_id IS NULL``). A plain UNIQUE
  constraint cannot protect the ``schema_id IS NULL`` ("global") rows, because
  PostgreSQL never treats two NULLs as equal.
* the single raw-SQL seed a fresh install still needs:
  ``system_settings.default_timezone = 'UTC'``. Every other raw-SQL seed in the
  old chain either repaired legacy data or is re-created by
  ``init_service.init_db_on_startup()`` (roles, permissions, canonical
  attribute types, languages, translation types, UI catalog) — those are not
  carried. The one data-migration helper table the old chain left behind
  (``f6931c094d0e_textarea_attribute_type_state``) is dropped on purpose.

Phase 318 (D-22..D-26) re-squashes the six revisions that followed the Phase-317
baseline back into it, so 1.0.0 ships exactly one Core revision:

* ``317_1_attribute_classification`` — the classification catalogue and its four
  per-level assignment tables, with their unique constraints and partial unique
  indexes. The catalogue is seeded at startup, never by a migration, so the
  revision carried no data.
* ``261022_export_creator`` / ``261022_schedule_creator`` — ``creator_id`` on
  ``exports`` and ``schedules`` (nullable UUID, ``ON DELETE SET NULL``) plus the
  per-user list index.
* ``261022_identity_text_index`` — the partial expression index
  ``idx_object_values_attr_text_md5`` the CSV import's identity lookup needs.
* ``261022_import_admin_only`` / ``261022_groups_admin_only`` — dropped, not
  folded. Both were grant-revoke data revisions: the default seed already grants
  nothing they revoke, so they are no-ops on a fresh install.

``old.sql`` / ``new.sql`` equivalence and the per-difference classification are
recorded in ``.planning/phases/317-triage-release-blocker-fixes/317-SCHEMA-DIFF.md``.

Revision ID: initial
Revises:
Create Date: 2026-10-21
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Hand-carried: the extensions the Core schema depends on. Idempotent, so a
# second run against an installation that already carries them is a no-op.
_EXTENSION_UPGRADE_STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS age",
    "CREATE EXTENSION IF NOT EXISTS vector",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
)

# Strict reverse order of the install; dropping an extension removes its own
# catalog schema as well, which is why the guarded drop is the undo path.
_EXTENSION_DOWNGRADE_STATEMENTS = (
    "DROP EXTENSION IF EXISTS pg_trgm",
    "DROP EXTENSION IF EXISTS vector",
    "DROP EXTENSION IF EXISTS age",
)

# Hand-carried from revision 292_source_url_revision: a BEFORE UPDATE trigger
# recomputes OLD.source_url_revision + (0|1), so a client cannot reset or forge
# the counter. INSERT keeps the column default 0 — there is no insert trigger.
_SOURCE_URL_REVISION_FUNCTION = """
CREATE OR REPLACE FUNCTION sources_source_url_revision()
RETURNS trigger AS $$
BEGIN
    NEW.source_url_revision := OLD.source_url_revision
        + CASE
              WHEN NEW.source_url IS DISTINCT FROM OLD.source_url THEN 1
              ELSE 0
          END;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_SOURCE_URL_REVISION_TRIGGER = (
    "CREATE TRIGGER trg_sources_source_url_revision "
    "BEFORE UPDATE ON sources "
    "FOR EACH ROW "
    "EXECUTE FUNCTION sources_source_url_revision()"
)

# Hand-carried from revision 190_reference_type_schema_unique: the only
# surviving raw-SQL index the models cannot express. A plain UNIQUE(schema_id,
# reference) constraint provides zero uniqueness protection among
# `schema_id IS NULL` ("global") rows.
_REFERENCE_TYPE_NULL_SCHEMA_INDEX = (
    "CREATE UNIQUE INDEX uq_reference_types_reference_null_schema "
    "ON reference_types (reference) WHERE schema_id IS NULL"
)

# Hand-carried from revision b57a9a3fd3a8: the one seed a fresh install needs.
# `init_service` seeds roles, permissions, canonical attribute types, languages,
# translation types and the UI catalog at startup — but not system_settings.
_DEFAULT_TIMEZONE_SEED = (
    "INSERT INTO system_settings (key, value) VALUES ('default_timezone', 'UTC')"
)

# Hand-carried from revision 317_1_attribute_classification: the classification
# catalogue. "Not classified" is the absence of an assignment row, so existing
# attributes and objects need no backfill; the catalogue rows themselves are
# seeded at startup, so the folded revision carries no data.
CLASSIFICATION_TABLES = (
    'classification_families',
    'classification_labels',
    'schema_classification_labels',
    'object_type_classification_labels',
    'object_type_attribute_classification_labels',
    'object_classification_labels',
)

# Labels are localised through the polymorphic ``translations`` table, which has
# no FK to its entity — the downgrade therefore removes the rows and the type by
# hand instead of relying on a cascade.
_TRANSLATION_TYPE_CODE = 'classification_label'
_DELETE_LABEL_TRANSLATIONS = (
    "DELETE FROM translations WHERE translation_type_id IN "
    "(SELECT id FROM translation_types WHERE code = '" + _TRANSLATION_TYPE_CODE + "')"
)
_DELETE_TRANSLATION_TYPE = (
    "DELETE FROM translation_types WHERE code = '" + _TRANSLATION_TYPE_CODE + "'"
)

_CLASSIFICATION_ASSIGNMENT_TABLES = (
    'schema_classification_labels',
    'object_type_classification_labels',
    'object_type_attribute_classification_labels',
    'object_classification_labels',
)

# Hand-carried from revision 261022_identity_text_index: the CSV import resolves
# an Object by ``(object_type_attribute_id, identity value)``, and TEXT,
# TEXTAREA, STATUS and JSON identities live in ``object_values.value_simple``.
# A plain B-tree on that column is unusable (TEXTAREA values exceed the B-tree
# key limit), so the index keys ``md5(value_simple)`` instead and is partial on
# ``value_simple IS NOT NULL``. Autogenerate cannot emit a functional/partial
# index; ``app/db/schema_contract.py`` registers the same definition on the model
# metadata so ``alembic check`` still compares it.
_IDENTITY_TEXT_INDEX_NAME = "idx_object_values_attr_text_md5"


def _classification_assignment_columns(target_column: str) -> tuple:
    """The columns every assignment table shares; the target column differs per level."""
    return (
        sa.Column(target_column, sa.UUID(), nullable=False),
        sa.Column('label_id', sa.UUID(), nullable=False),
        sa.Column('family_id', sa.UUID(), nullable=False),
        sa.Column('family_multi_valued', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def upgrade() -> None:
    # Extensions first: the schema's trigram indexes need pg_trgm, and the
    # application expects all three catalogs before it starts. Everything else
    # hand-carried below needs the tables, so it runs after the generated DDL.
    for statement in _EXTENSION_UPGRADE_STATEMENTS:
        op.execute(statement)

    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('attribute_types',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('data_type', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('audit_log_config',
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('audit_log_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_by', sa.UUID(), nullable=True),
    sa.PrimaryKeyConstraint('entity_type')
    )
    op.create_table('groups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('index_outbox',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=True),
    sa.Column('schema_id', sa.UUID(), nullable=True),
    sa.Column('operation', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('origin_kind', sa.String(length=20), nullable=True),
    sa.Column('origin_name', sa.String(length=255), nullable=True),
    sa.Column('origin_task_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('processed_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('languages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=10), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('permissions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('resource', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('owner_plugin', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_permissions_name'), 'permissions', ['name'], unique=True)
    # Folded from 317_1_attribute_classification. The catalogue has no schema_id:
    # one global family list per instance. ``is_multi_valued`` is copied onto
    # labels and assignments so a composite FK can prove the copies.
    op.create_table('classification_families',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('is_multi_valued', sa.Boolean(), nullable=False),
    sa.Column('is_ranked', sa.Boolean(), nullable=False),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.Column('allows_operator_labels', sa.Boolean(), nullable=False),
    sa.Column('sort', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key'),
    sa.UniqueConstraint('id', 'is_multi_valued', name='uq_classification_families_id_multi')
    )
    op.create_table('classification_labels',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('family_id', sa.UUID(), nullable=False),
    sa.Column('family_multi_valued', sa.Boolean(), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('is_system', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.ForeignKeyConstraint(['family_id', 'family_multi_valued'], ['classification_families.id', 'classification_families.is_multi_valued'], name='fk_classification_labels_family', onupdate='CASCADE', ondelete='RESTRICT'),
    sa.UniqueConstraint('family_id', 'key', name='uq_classification_labels_family_key'),
    sa.UniqueConstraint('id', 'family_id', 'family_multi_valued', name='uq_classification_labels_id_family')
    )
    op.create_table('plugins',
    sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('version', sa.String(length=50), nullable=False),
    sa.Column('state', sa.String(length=20), nullable=False, server_default=sa.text("'loaded'::character varying")),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('installed_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('worker_loaded_at', sa.DateTime(), nullable=True),
    sa.Column('worker_loaded_version', sa.String(length=50), nullable=True),
    sa.Column('database_migrations_dir', sa.String(length=255), nullable=True),
    sa.Column('database_version_table', sa.String(length=63), nullable=True),
    sa.Column('database_error', sa.Text(), nullable=True),
    sa.Column('database_error_revision', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('roles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_roles_name'), 'roles', ['name'], unique=True)
    op.create_table('schema_templates',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('category', sa.String(length=100), nullable=True),
    sa.Column('template_data', sa.JSON(), nullable=False),
    sa.Column('usage_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('sources',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('source_url', sa.String(length=500), nullable=True),
    sa.Column('source_url_revision', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('import_folder', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('import_folder')
    )
    op.create_table('system_settings',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('translation_types',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('code', sa.String(length=50), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('ui_strings',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('default_text', sa.Text(), nullable=False),
    sa.Column('namespace', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ui_strings_key'), 'ui_strings', ['key'], unique=True)
    op.create_table('users',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    sa.Column('is_superuser', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('is_service_account', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('token_version', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_users_email'), 'users', ['email'], unique=True)
    op.create_index(op.f('ix_users_username'), 'users', ['username'], unique=True)
    op.create_table('api_clients',
    sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('token_prefix', sa.String(length=16), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('service_user_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['service_user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_clients_service_user_id'), 'api_clients', ['service_user_id'], unique=False)
    op.create_index(op.f('ix_api_clients_token_hash'), 'api_clients', ['token_hash'], unique=True)
    op.create_table('audit_logs',
    sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
    sa.Column('entity_type', sa.String(length=50), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=True),
    sa.Column('object_type_attribute_id', sa.UUID(), nullable=True),
    sa.Column('action', sa.String(length=10), nullable=False),
    sa.Column('actor_id', sa.UUID(), nullable=True),
    sa.Column('changes', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['actor_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_actor_id'), 'audit_logs', ['actor_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_created_at'), 'audit_logs', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_logs_entity_id'), 'audit_logs', ['entity_id'], unique=False)
    op.create_index(op.f('ix_audit_logs_entity_type'), 'audit_logs', ['entity_type'], unique=False)
    op.create_index(op.f('ix_audit_logs_object_id'), 'audit_logs', ['object_id'], unique=False)
    # ix_audit_logs_object_type_attribute_id is deliberately absent: the old
    # chain dropped it as a provably never-selected index (phase 241 DEAD-01),
    # and `app/models/audit_log.py` no longer declares it either.
    op.create_table('groups_roles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_id', 'role_id', name='uq_group_role')
    )
    op.create_table('record_permissions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('record_entity_type', sa.String(length=50), nullable=False),
    sa.Column('record_id', sa.UUID(), nullable=False),
    sa.Column('resource', sa.String(length=100), nullable=False),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('is_deny', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'record_entity_type', 'record_id', 'action', name='uq_record_permission')
    )
    op.create_index('ix_record_permissions_entity', 'record_permissions', ['record_entity_type', 'record_id'], unique=False)
    op.create_table('role_permissions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('permission_id', sa.UUID(), nullable=False),
    sa.Column('is_deny', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['permission_id'], ['permissions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('role_id', 'permission_id', name='uq_role_permission')
    )
    op.create_table('saved_queries',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('query_text', sa.Text(), nullable=False),
    sa.Column('variables', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_by_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('schemas',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=100), nullable=True),
    sa.Column('keygen', sa.String(length=100), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1000), nullable=True),
    sa.Column('primary_color', sa.String(length=7), nullable=False, server_default=sa.text("'#2563EB'::character varying")),
    sa.Column('header_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#1E293B'::character varying")),
    sa.Column('sidebar_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#F1F5F9'::character varying")),
    sa.Column('section_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#F8FAFC'::character varying")),
    sa.Column('surface_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#FFFFFF'::character varying")),
    sa.Column('dark_primary_color', sa.String(length=7), nullable=False, server_default=sa.text("'#3B82F6'::character varying")),
    sa.Column('dark_header_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#0F172A'::character varying")),
    sa.Column('dark_sidebar_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#1E293B'::character varying")),
    sa.Column('dark_section_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#0F172A'::character varying")),
    sa.Column('dark_surface_bg_color', sa.String(length=7), nullable=False, server_default=sa.text("'#1E293B'::character varying")),
    sa.Column('status_filter_config', sa.JSON(), nullable=True),
    sa.Column('default_language_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['default_language_id'], ['languages.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_schemas_key'), 'schemas', ['key'], unique=False)
    op.create_table('translations',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('translation_type_id', sa.UUID(), nullable=False),
    sa.Column('entity_id', sa.UUID(), nullable=False),
    sa.Column('field', sa.String(length=50), nullable=False),
    sa.Column('language_id', sa.UUID(), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['language_id'], ['languages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['translation_type_id'], ['translation_types.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('translation_type_id', 'entity_id', 'field', 'language_id', name='uq_translations_natural_key')
    )
    op.create_index('ix_translations_type_entity', 'translations', ['translation_type_id', 'entity_id'], unique=False)
    op.create_table('user_profiles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('first_name', sa.String(length=100), nullable=True),
    sa.Column('last_name', sa.String(length=100), nullable=True),
    sa.Column('avatar_url', sa.String(length=500), nullable=True),
    sa.Column('ui_language', sa.String(length=10), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', name='uq_user_profile_user_id')
    )
    op.create_table('user_roles',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('role_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['role_id'], ['roles.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'role_id', name='uq_user_role')
    )
    op.create_table('users_groups',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('group_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['group_id'], ['groups.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'group_id', name='uq_user_group')
    )
    op.create_table('exports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('creator_id', sa.UUID(), nullable=True),
    sa.Column('object_key', sa.String(length=255), nullable=True),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('total_objects', sa.Integer(), nullable=False),
    sa.Column('processed_objects', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], name='fk_exports_creator_id_users', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_exports_creator_id', 'exports', ['creator_id'], unique=False)
    op.create_table('icons',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('local_name', sa.String(length=100), nullable=True),
    sa.Column('url', sa.String(length=1000), nullable=False),
    sa.Column('type', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('imports',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('object_key', sa.String(length=255), nullable=True),
    sa.Column('error_code', sa.String(length=50), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('total_objects', sa.Integer(), nullable=False),
    sa.Column('processed_objects', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('reference_types',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('reference', sa.String(length=100), nullable=False),
    sa.Column('description', sa.String(length=500), nullable=True),
    sa.Column('color', sa.String(length=50), nullable=True),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('schema_id', 'reference', name='uq_reference_types_schema_reference')
    )
    op.create_table('schema_languages',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('language_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['language_id'], ['languages.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('schema_id', 'language_id', name='uq_schema_languages_pair')
    )
    op.create_table('statuses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('color', sa.String(length=7), nullable=True),
    sa.Column('order', sa.Integer(), nullable=False),
    sa.Column('status_category', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('targets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('target_url', sa.String(length=500), nullable=False),
    sa.Column('config', sa.JSON(), nullable=True),
    sa.Column('export_folder', sa.String(length=255), nullable=True),
    sa.Column('connector_type', sa.String(length=50), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('export_folder')
    )
    op.create_table('export_statuses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('export_id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('is_running', sa.Boolean(), nullable=False),
    sa.Column('is_done', sa.Boolean(), nullable=False),
    sa.Column('current_id', sa.UUID(), nullable=True),
    sa.Column('has_error', sa.Boolean(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('progress_percentage', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_id'], ['export_statuses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('import_statuses',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('import_id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('is_running', sa.Boolean(), nullable=False),
    sa.Column('is_done', sa.Boolean(), nullable=False),
    sa.Column('current_id', sa.UUID(), nullable=True),
    sa.Column('has_error', sa.Boolean(), nullable=False),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('progress_percentage', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['import_id'], ['imports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_id'], ['import_statuses.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('service_accounts',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('username', sa.String(length=255), nullable=True),
    # `app.crypto.EncryptedString` is a TypeDecorator over VARCHAR(800); the
    # baseline spells the column type it renders instead of importing
    # application encryption code into a migration (autogenerate wrote the
    # decorator without its import, which does not even load).
    sa.Column('source_bearer_token', sa.String(length=800), nullable=True),
    sa.Column('target_bearer_token', sa.String(length=800), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('object_type_attributes',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1000), nullable=True),
    sa.Column('sort', sa.Integer(), nullable=False),
    sa.Column('attribute_type_id', sa.UUID(), nullable=False),
    sa.Column('is_required', sa.Boolean(), nullable=False),
    sa.Column('multilingual', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('minimum_cardinality', sa.Integer(), nullable=False, server_default=sa.text('0')),
    sa.Column('maximum_cardinality', sa.Integer(), nullable=False, server_default=sa.text("'-1'::integer")),
    sa.Column('is_unique', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('is_hidden', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('is_system', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('default_value', sa.Text(), nullable=True),
    sa.Column('structure', sa.JSON(), nullable=True),
    sa.Column('referenced_object_type_id', sa.UUID(), nullable=True),
    sa.Column('reference_type_id', sa.UUID(), nullable=True),
    sa.Column('managed_by', sa.String(length=64), nullable=True),
    sa.Column('externally_readonly', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['attribute_type_id'], ['attribute_types.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['reference_type_id'], ['reference_types.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('object_types',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('parent_id', sa.UUID(), nullable=True),
    sa.Column('inherits_attributes', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('is_abstract', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('description', sa.String(length=1000), nullable=True),
    sa.Column('sort', sa.Integer(), nullable=False),
    sa.Column('label_attribute_id', sa.UUID(), nullable=True),
    sa.Column('multilingual', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('icon_id', sa.UUID(), nullable=True),
    sa.Column('managed_by', sa.String(length=64), nullable=True),
    sa.Column('externally_readonly', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['icon_id'], ['icons.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['label_attribute_id'], ['object_type_attributes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['parent_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('import_definitions',
    sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('field_mappings', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    sa.Column('identity_attr_id', sa.UUID(), nullable=True),
    sa.Column('value_separator', sa.String(length=10), nullable=False, server_default=sa.text("';'::character varying")),
    sa.Column('date_format', sa.String(length=50), nullable=True),
    sa.Column('encoding', sa.String(length=20), nullable=False, server_default=sa.text("'utf-8'::character varying")),
    sa.Column('is_permanent', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['identity_attr_id'], ['object_type_attributes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', name='uq_import_definition_name')
    )
    op.create_table('objects',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('schema_id', sa.UUID(), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=True),
    sa.Column('name', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('schema_id', 'key', name='uq_objects_schema_key')
    )
    op.create_index(op.f('ix_objects_key'), 'objects', ['key'], unique=False)
    # Folded from 317_1_attribute_classification: one assignment table per level,
    # each with a real FK to its target (ON DELETE CASCADE) and a composite FK to
    # the label (ON UPDATE CASCADE, ON DELETE RESTRICT). They follow the level
    # tables above because every one of them references schemas, object_types,
    # object_type_attributes or objects. The partial unique index enforces at
    # most one label per single-valued family and target.
    specifications = (
        ('schema_classification_labels', 'schema_id', 'schemas.id'),
        ('object_type_classification_labels', 'object_type_id', 'object_types.id'),
        ('object_type_attribute_classification_labels', 'object_type_attribute_id', 'object_type_attributes.id'),
        ('object_classification_labels', 'object_id', 'objects.id'),
    )
    for table_name, target_column, target_table in specifications:
        op.create_table(
            table_name,
            *_classification_assignment_columns(target_column),
            sa.PrimaryKeyConstraint(target_column, 'label_id'),
            sa.ForeignKeyConstraint([target_column], [target_table], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['label_id', 'family_id', 'family_multi_valued'], ['classification_labels.id', 'classification_labels.family_id', 'classification_labels.family_multi_valued'], name=f'fk_{table_name}_label', onupdate='CASCADE', ondelete='RESTRICT')
        )
        op.create_index(
            f'uq_{table_name}_single',
            table_name,
            [target_column, 'family_id'],
            unique=True,
            postgresql_where=sa.text('NOT family_multi_valued'),
        )
        op.create_index(f'ix_{table_name}_label_id', table_name, ['label_id'], unique=False)
    op.create_table('export_definitions',
    sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('gen_random_uuid()')),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=False),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('field_mappings', sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
    sa.Column('is_permanent', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', name='uq_export_definition_name')
    )
    op.create_table('schedules',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('job_function', sa.String(length=100), nullable=True),
    sa.Column('cron_expression', sa.String(length=50), nullable=False),
    sa.Column('args', postgresql.JSON(astext_type=sa.Text()), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('schema_id', sa.UUID(), nullable=True),
    sa.Column('object_type_id', sa.UUID(), nullable=True),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('template_id', sa.UUID(), nullable=True),
    sa.Column('template_type', sa.String(length=20), nullable=True),
    sa.Column('creator_id', sa.UUID(), nullable=True),
    sa.Column('timezone', sa.String(length=50), nullable=False, server_default=sa.text("'UTC'::character varying")),
    sa.Column('tasktype', sa.String(length=255), nullable=False, server_default=sa.text("'IMPORT'::character varying")),
    sa.Column('preset_type', sa.String(length=20), nullable=True),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.Column('next_run_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], name='fk_schedules_creator_id_users', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_schedules_creator_id', 'schedules', ['creator_id'], unique=False)
    op.create_table('object_comments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('author_id', sa.UUID(), nullable=True),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('external_author_name', sa.String(length=255), nullable=True),
    sa.Column('managed_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_object_comments_object_id'), 'object_comments', ['object_id'], unique=False)
    op.create_table('object_files',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('storage_key', sa.String(length=64), nullable=False),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('content_type', sa.String(length=255), nullable=True),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('uploader_id', sa.UUID(), nullable=True),
    sa.Column('managed_by', sa.String(length=64), nullable=True),
    sa.Column('external_author_name', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['uploader_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_object_files_object_id'), 'object_files', ['object_id'], unique=False)
    op.create_table('object_refs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('from_object_id', sa.UUID(), nullable=False),
    sa.Column('to_object_id', sa.UUID(), nullable=False),
    sa.Column('object_type_id', sa.UUID(), nullable=False),
    sa.Column('source_id', sa.UUID(), nullable=False),
    sa.Column('from_attribute_name', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['from_object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['to_object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('object_values',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('object_type_attribute_id', sa.UUID(), nullable=False),
    sa.Column('value_float', sa.Float(), nullable=True),
    sa.Column('value_integer', sa.Integer(), nullable=True),
    sa.Column('value_boolean', sa.Boolean(), nullable=True),
    sa.Column('value_simple', sa.Text(), nullable=True),
    sa.Column('value_datetime', sa.DateTime(), nullable=True),
    sa.Column('value_struct', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['object_type_attribute_id'], ['object_type_attributes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('object_write_protection_releases',
    sa.Column('object_id', sa.UUID(), nullable=False),
    sa.Column('managed_by', sa.String(length=100), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['object_id'], ['objects.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('object_id', 'managed_by')
    )
    op.create_table('tasks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('parent_task_id', sa.UUID(), nullable=True),
    sa.Column('import_id', sa.UUID(), nullable=True),
    sa.Column('export_id', sa.UUID(), nullable=True),
    sa.Column('schedule_id', sa.UUID(), nullable=True),
    sa.Column('source_id', sa.UUID(), nullable=True),
    sa.Column('schema_id', sa.UUID(), nullable=True),
    sa.Column('object_type_id', sa.UUID(), nullable=True),
    sa.Column('target_id', sa.UUID(), nullable=True),
    sa.Column('export_definition_id', sa.UUID(), nullable=True),
    sa.Column('import_definition_id', sa.UUID(), nullable=True),
    sa.Column('service_account_id', sa.UUID(), nullable=True),
    sa.Column('creator_id', sa.UUID(), nullable=True),
    sa.Column('current_step', sa.String(length=255), nullable=True),
    sa.Column('current_status', sa.String(length=20), nullable=False),
    sa.Column('sort', sa.Integer(), nullable=False),
    sa.Column('tasktype', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
    sa.Column('error_message', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('cancel_requested_at', sa.DateTime(), nullable=True),
    sa.Column('is_permanent', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('processed_objects', sa.Integer(), nullable=True),
    sa.Column('total_objects', sa.Integer(), nullable=True),
    sa.Column('plugin_parameters', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('now()')),
    sa.ForeignKeyConstraint(['creator_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['export_definition_id'], ['export_definitions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['import_definition_id'], ['import_definitions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['import_id'], ['imports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['object_type_id'], ['object_types.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['parent_task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schedule_id'], ['schedules.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['schema_id'], ['schemas.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_account_id'], ['service_accounts.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['source_id'], ['sources.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['target_id'], ['targets.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_log_state',
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('next_sequence', sa.BigInteger(), nullable=False, server_default=sa.text('0')),
    sa.Column('dropped_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    sa.Column('pruning_revision', sa.Integer(), nullable=False, server_default=sa.text('0')),
    sa.Column('expired', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    sa.Column('max_lines', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('task_logs',
    sa.Column('task_id', sa.UUID(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('emitted_at', sa.DateTime(), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('segment', sa.Integer(), nullable=False),
    sa.Column('step', sa.String(length=255), nullable=True),
    sa.Column('level', sa.SmallInteger(), nullable=False),
    sa.Column('category', sa.String(length=255), nullable=False),
    sa.Column('logger', sa.String(length=255), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('task_id', 'sequence')
    )

    # Deferred: object_type_attributes <-> object_types is a foreign-key cycle, so
    # autogenerate cannot order the two create_table calls.
    op.create_foreign_key('object_type_attributes_object_type_id_fkey', 'object_type_attributes', 'object_types', ['object_type_id'], ['id'], ondelete='CASCADE')
    # Deferred: object_type_attributes <-> object_types is a foreign-key cycle, so
    # autogenerate cannot order the two create_table calls.
    op.create_foreign_key('object_type_attributes_referenced_object_type_id_fkey', 'object_type_attributes', 'object_types', ['referenced_object_type_id'], ['id'], ondelete='CASCADE')
    # ### end Alembic commands ###

    # Hand-carried indexes after the generated DDL. Autogenerate only emits the
    # indexes the ORM models declare; these are the durable performance indexes
    # the old chain shipped from explicit revisions (the phase-104 hot paths,
    # the phase-237 structured-Browse set and the phase-241 measured FK set).
    # They are created here so a fresh install has the same access paths as the
    # chain this baseline replaces; `app/db/schema_contract.py` registers the
    # same definitions on the model metadata so `alembic check` keeps agreeing
    # with the database.
    op.create_index('idx_object_refs_from_attr_from_obj', 'object_refs', ['from_attribute_name', 'from_object_id'], unique=False)
    op.create_index('idx_object_refs_from_object_id', 'object_refs', ['from_object_id'], unique=False)
    op.create_index('idx_object_refs_to_object_id', 'object_refs', ['to_object_id'], unique=False)
    op.create_index('idx_object_type_attributes_attribute_type_id', 'object_type_attributes', ['attribute_type_id'], unique=False)
    op.create_index('idx_object_type_attributes_object_type_id', 'object_type_attributes', ['object_type_id'], unique=False)
    op.create_index('idx_object_type_attributes_reference_type_id', 'object_type_attributes', ['reference_type_id'], unique=False)
    op.create_index('idx_object_type_attributes_referenced_object_type_id', 'object_type_attributes', ['referenced_object_type_id'], unique=False)
    op.create_index('idx_object_types_parent_id', 'object_types', ['parent_id'], unique=False)
    op.create_index('idx_object_types_schema_id', 'object_types', ['schema_id'], unique=False)
    op.create_index('idx_object_values_attr_datetime', 'object_values', ['object_type_attribute_id', 'value_datetime'], unique=False)
    op.create_index('idx_object_values_attr_float', 'object_values', ['object_type_attribute_id', 'value_float'], unique=False)
    op.create_index('idx_object_values_attr_integer', 'object_values', ['object_type_attribute_id', 'value_integer'], unique=False)
    op.create_index('idx_object_values_object_id', 'object_values', ['object_id'], unique=False)
    op.create_index('idx_object_values_object_type_attribute_id', 'object_values', ['object_type_attribute_id'], unique=False)
    op.create_index('idx_object_values_value_simple_trgm', 'object_values', ['value_simple'], unique=False, postgresql_using='gin', postgresql_ops={'value_simple': 'gin_trgm_ops'})
    op.create_index('idx_objects_name_trgm', 'objects', ['name'], unique=False, postgresql_using='gin', postgresql_ops={'name': 'gin_trgm_ops'})
    op.create_index('idx_objects_schema_id', 'objects', ['schema_id'], unique=False)
    op.create_index('idx_objects_type_created_desc', 'objects', ['object_type_id', sa.text('created_at DESC')], unique=False)
    op.create_index('idx_schemas_source_id', 'schemas', ['source_id'], unique=False)
    op.create_index('idx_statuses_schema_id', 'statuses', ['schema_id'], unique=False)
    op.create_index('idx_tasks_created_at', 'tasks', ['created_at'], unique=False)
    op.create_index('idx_tasks_status', 'tasks', ['current_status'], unique=False)
    op.create_index('ix_export_definitions_object_type_id', 'export_definitions', ['object_type_id'], unique=False)
    op.create_index('ix_export_definitions_target_id', 'export_definitions', ['target_id'], unique=False)
    op.create_index('ix_import_definitions_identity_attr_id', 'import_definitions', ['identity_attr_id'], unique=False)
    op.create_index('ix_index_outbox_object_id', 'index_outbox', ['object_id'], unique=False)
    op.create_index('ix_index_outbox_status', 'index_outbox', ['status'], unique=False)
    op.create_index('ix_object_refs_object_type_id', 'object_refs', ['object_type_id'], unique=False)
    op.create_index('ix_object_refs_source_id', 'object_refs', ['source_id'], unique=False)
    op.create_index('ix_permissions_resource', 'permissions', ['resource'], unique=False)
    op.create_index('ix_role_permissions_permission_id', 'role_permissions', ['permission_id'], unique=False)
    op.create_index('ix_role_permissions_role_id', 'role_permissions', ['role_id'], unique=False)
    op.create_index('ix_task_logs_emitted_at', 'task_logs', ['emitted_at'], unique=False)
    op.create_index('ix_task_logs_task_id_level_sequence', 'task_logs', ['task_id', 'level', 'sequence'], unique=False)
    op.create_index('ix_tasks_schedule_id', 'tasks', ['schedule_id'], unique=False)
    op.create_index(
        _IDENTITY_TEXT_INDEX_NAME,
        'object_values',
        [sa.text('object_type_attribute_id'), sa.text('md5(value_simple)')],
        unique=False,
        postgresql_where=sa.text('value_simple IS NOT NULL'),
    )

    # Hand-carried raw SQL after the generated DDL: the revision trigger, the
    # partial unique index and the one seed a fresh install needs.
    op.execute(_SOURCE_URL_REVISION_FUNCTION)
    op.execute(_SOURCE_URL_REVISION_TRIGGER)
    op.execute(_REFERENCE_TYPE_NULL_SCHEMA_INDEX)
    op.execute(_DEFAULT_TIMEZONE_SEED)


def downgrade() -> None:
    # Folded classification teardown first, preserving 317_1's order: the
    # polymorphic translation rows have no FK to their entity, so they are
    # deleted explicitly before their type row.
    op.execute(_DELETE_LABEL_TRANSLATIONS)
    op.execute(_DELETE_TRANSLATION_TYPE)

    # Hand-carried indexes first, in reverse install order. The expression index
    # was installed last, so it is dropped first; the table outlives all of them.
    op.drop_index(_IDENTITY_TEXT_INDEX_NAME, table_name='object_values')
    op.drop_index('ix_tasks_schedule_id', table_name='tasks')
    op.drop_index('ix_task_logs_task_id_level_sequence', table_name='task_logs')
    op.drop_index('ix_task_logs_emitted_at', table_name='task_logs')
    op.drop_index('ix_role_permissions_role_id', table_name='role_permissions')
    op.drop_index('ix_role_permissions_permission_id', table_name='role_permissions')
    op.drop_index('ix_permissions_resource', table_name='permissions')
    op.drop_index('ix_object_refs_source_id', table_name='object_refs')
    op.drop_index('ix_object_refs_object_type_id', table_name='object_refs')
    op.drop_index('ix_index_outbox_status', table_name='index_outbox')
    op.drop_index('ix_index_outbox_object_id', table_name='index_outbox')
    op.drop_index('ix_import_definitions_identity_attr_id', table_name='import_definitions')
    op.drop_index('ix_export_definitions_target_id', table_name='export_definitions')
    op.drop_index('ix_export_definitions_object_type_id', table_name='export_definitions')
    op.drop_index('idx_tasks_status', table_name='tasks')
    op.drop_index('idx_tasks_created_at', table_name='tasks')
    op.drop_index('idx_statuses_schema_id', table_name='statuses')
    op.drop_index('idx_schemas_source_id', table_name='schemas')
    op.drop_index('idx_objects_type_created_desc', table_name='objects')
    op.drop_index('idx_objects_schema_id', table_name='objects')
    op.drop_index('idx_objects_name_trgm', table_name='objects')
    op.drop_index('idx_object_values_value_simple_trgm', table_name='object_values')
    op.drop_index('idx_object_values_object_type_attribute_id', table_name='object_values')
    op.drop_index('idx_object_values_object_id', table_name='object_values')
    op.drop_index('idx_object_values_attr_integer', table_name='object_values')
    op.drop_index('idx_object_values_attr_float', table_name='object_values')
    op.drop_index('idx_object_values_attr_datetime', table_name='object_values')
    op.drop_index('idx_object_types_schema_id', table_name='object_types')
    op.drop_index('idx_object_types_parent_id', table_name='object_types')
    op.drop_index('idx_object_type_attributes_referenced_object_type_id', table_name='object_type_attributes')
    op.drop_index('idx_object_type_attributes_reference_type_id', table_name='object_type_attributes')
    op.drop_index('idx_object_type_attributes_object_type_id', table_name='object_type_attributes')
    op.drop_index('idx_object_type_attributes_attribute_type_id', table_name='object_type_attributes')
    op.drop_index('idx_object_refs_to_object_id', table_name='object_refs')
    op.drop_index('idx_object_refs_from_object_id', table_name='object_refs')
    op.drop_index('idx_object_refs_from_attr_from_obj', table_name='object_refs')

    # Hand-carried teardown of the statements installed after the generated DDL,
    # in reverse install order. It must run BEFORE the generated drop_table calls
    # below: the trigger lives on `sources`, the seed row on `system_settings` and
    # the partial index on `reference_types`, so tearing them down afterwards
    # would address tables that no longer exist (CR-01).
    op.execute("DROP TRIGGER IF EXISTS trg_sources_source_url_revision ON sources")
    op.execute("DROP FUNCTION IF EXISTS sources_source_url_revision()")
    op.execute("DELETE FROM system_settings WHERE key = 'default_timezone'")
    op.execute("DROP INDEX IF EXISTS uq_reference_types_reference_null_schema")

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint('object_type_attributes_referenced_object_type_id_fkey', 'object_type_attributes', type_='foreignkey')
    op.drop_constraint('object_type_attributes_object_type_id_fkey', 'object_type_attributes', type_='foreignkey')
    op.drop_table('task_logs')
    op.drop_table('task_log_state')
    op.drop_table('tasks')
    op.drop_table('object_write_protection_releases')
    op.drop_table('object_values')
    op.drop_table('object_refs')
    op.drop_index(op.f('ix_object_files_object_id'), table_name='object_files')
    op.drop_table('object_files')
    op.drop_index(op.f('ix_object_comments_object_id'), table_name='object_comments')
    op.drop_table('object_comments')
    op.drop_index('ix_schedules_creator_id', table_name='schedules')
    op.drop_table('schedules')
    op.drop_table('export_definitions')
    op.drop_index(op.f('ix_objects_key'), table_name='objects')
    # Folded classification assignment tables, in reverse creation order. They
    # reference the level tables dropped below, so they go before `objects`.
    for table_name in reversed(_CLASSIFICATION_ASSIGNMENT_TABLES):
        op.drop_index(f'ix_{table_name}_label_id', table_name=table_name)
        op.drop_index(f'uq_{table_name}_single', table_name=table_name)
        op.drop_table(table_name)
    op.drop_table('objects')
    op.drop_table('import_definitions')
    op.drop_table('object_types')
    op.drop_table('object_type_attributes')
    op.drop_table('service_accounts')
    op.drop_table('import_statuses')
    op.drop_table('export_statuses')
    op.drop_table('targets')
    op.drop_table('statuses')
    op.drop_table('schema_languages')
    op.drop_table('reference_types')
    op.drop_table('imports')
    op.drop_table('icons')
    op.drop_index('ix_exports_creator_id', table_name='exports')
    op.drop_table('exports')
    op.drop_table('users_groups')
    op.drop_table('user_roles')
    op.drop_table('user_profiles')
    op.drop_index('ix_translations_type_entity', table_name='translations')
    op.drop_table('translations')
    op.drop_index(op.f('ix_schemas_key'), table_name='schemas')
    op.drop_table('schemas')
    op.drop_table('saved_queries')
    op.drop_table('role_permissions')
    op.drop_index('ix_record_permissions_entity', table_name='record_permissions')
    op.drop_table('record_permissions')
    op.drop_table('groups_roles')

    op.drop_index(op.f('ix_audit_logs_object_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_entity_type'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_entity_id'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_created_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_actor_id'), table_name='audit_logs')
    op.drop_table('audit_logs')
    op.drop_index(op.f('ix_api_clients_token_hash'), table_name='api_clients')
    op.drop_index(op.f('ix_api_clients_service_user_id'), table_name='api_clients')
    op.drop_table('api_clients')
    op.drop_index(op.f('ix_users_username'), table_name='users')
    op.drop_index(op.f('ix_users_email'), table_name='users')
    op.drop_table('users')
    op.drop_index(op.f('ix_ui_strings_key'), table_name='ui_strings')
    op.drop_table('ui_strings')
    op.drop_table('translation_types')
    op.drop_table('system_settings')
    op.drop_table('sources')
    op.drop_table('schema_templates')
    op.drop_index(op.f('ix_roles_name'), table_name='roles')
    op.drop_table('roles')
    op.drop_table('plugins')
    op.drop_index(op.f('ix_permissions_name'), table_name='permissions')
    op.drop_table('permissions')
    op.drop_table('languages')
    op.drop_table('index_outbox')
    op.drop_table('groups')
    op.drop_table('audit_log_config')
    # The classification catalogue goes last: every assignment table above
    # references its labels.
    op.drop_table('classification_labels')
    op.drop_table('classification_families')
    op.drop_table('attribute_types')
    # ### end Alembic commands ###

    # PostGIS/vector extensions last: every dependent table and index is gone.
    for statement in _EXTENSION_DOWNGRADE_STATEMENTS:
        op.execute(statement)