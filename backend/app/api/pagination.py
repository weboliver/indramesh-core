"""
Per-entity pagination defaults (API-04).

Centralized constants so testing and future tuning only changes one file.
See .planning/phases/106-api-response-optimization/106-CONTEXT.md D-18 through D-21.
"""

# Heavy payload entities (nested values/refs) — smaller default reduces wire size
# Per D-19: Objects default limit=50 (was 100)
OBJECTS_DEFAULT_LIMIT: int = 50

# Lightweight reference entities — higher default is fine for admin list views
# Per D-20: sources, schemas, attribute types, statuses, reference types, object-types,
# object-values, object-refs default limit=200 (was 100)
REFERENCE_DEFAULT_LIMIT: int = 200

# Maximum cap stays unchanged across all endpoints
# Per D-21: max le=1000 (no change)
MAX_LIMIT: int = 1000
