"""Core connector implementations.

Connector *identities* live in ``app.plugin_system.connector_registry`` (the
single source of truth since CORESEAM-01); connector *behaviours* for the
``jira`` identity are owned by the Jira plugin. This package keeps only the
Core-native CSV import/export connectors (``csv_connector``) and their shared
``base`` contracts. Import a concrete connector module directly, e.g.
``from app.services.connectors.csv_connector import CsvExportConnector``.
"""
