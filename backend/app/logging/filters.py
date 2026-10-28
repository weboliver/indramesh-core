"""Handler filter that stamps category and correlation fields on each record."""

import logging

from app.logging.categories import category_for
from app.logging.context import get_log_context
from app.logging.fields import CONTEXT_FIELDS


class LogContextFilter(logging.Filter):
    """Stamp the category and the current context fields on a record.

    The category is always derived from the logger name (never from ``extra``,
    D-16). For Core loggers context values only fill attributes the record does
    not already carry with a non-None value, so an explicit Core ``extra`` wins.
    For ``plugin.*`` records every correlation field comes from Core context
    only: a same-named ``extra`` supplied by plugin code is discarded (D-04).
    The filter never suppresses a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        category = category_for(record.name)
        record.category = category
        context = get_log_context()

        if category.startswith("plugin."):
            for key in CONTEXT_FIELDS:
                if key in context:
                    setattr(record, key, context[key])
                elif hasattr(record, key):
                    delattr(record, key)
            return True

        for key, value in context.items():
            if value is None:
                continue
            if getattr(record, key, None) is None:
                setattr(record, key, value)
        return True
