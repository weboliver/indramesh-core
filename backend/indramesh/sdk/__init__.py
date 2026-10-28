"""The public, versioned import path for IndraMesh plugin authors.

``indramesh.sdk`` is a thin re-export of the Core facade module
``app/plugin_system/sdk.py``: it exposes exactly that facade's ``__all__`` and
nothing else. New SDK names are added to the facade's ``__all__``, never here.

``indramesh`` is a PEP 420 namespace package. No distribution may ever add an
``indramesh/__init__.py``.

Plugins import nothing from ``app.*``; the AST gate in
``tests/test_plugin_discovery.py`` enforces this.
"""

from app.plugin_system.sdk import *  # noqa: F401,F403
from app.plugin_system.sdk import __all__  # noqa: F401
