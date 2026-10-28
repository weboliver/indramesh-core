"""Package entry for the directory-scanned example plugin (CORESEAM-06).

The presence of this file is what makes the loader import ``plugins/example``
as a real Python package (``indramesh_plugin_example``): ``module.py``'s
relative imports (``from .database import Base``) resolve through that one
canonical identity, and the manifest's ``module`` field still names the entry
submodule that defines the plugin class.
"""