"""Maintenance-mode cut-over utilities.

The supported deploy order is quiesce -> clear queue -> migrate -> purge cache
-> staged start. ``app.maintenance.cutover`` implements the bounded, namespace
scoped queue-clear and cache-purge steps; ``app.maintenance.__main__`` exposes
them as a fail-closed CLI. Nothing in this package opens a Redis connection or
imports an application ORM module at import time.
"""
