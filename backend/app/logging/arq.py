"""arq custom log dictionary for ``arq --custom-log-dict`` (D-18).

Incremental so arq's CLI ``dictConfig`` never closes the handler
``configure_logging`` installed (research Pitfall 3 / Code Example B). arq keeps
propagating to the root handler, where its records render as category ``task``.
"""

LOG_DICT = {"version": 1, "incremental": True, "loggers": {"arq": {"level": "INFO"}}}
