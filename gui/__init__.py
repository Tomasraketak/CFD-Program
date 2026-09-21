"""Desktop GUI package.

Importing the parameter core before Qt is deliberate and load-bearing.
PySide6 installs an import hook (``shibokensupport.feature_imported``) that
runs ``inspect.getsource`` on every newly imported module. If Qt is imported
first and pydantic is imported afterwards, that hook fires part-way through
pydantic's own import, calls ``hasattr(module, '__wrapped__')``, triggers
pydantic's deprecated-attribute shim and fails with a circular ImportError.

Importing ``core.models`` here means pydantic is always fully loaded before
Qt, whichever GUI module a caller reaches for first.
"""

from __future__ import annotations

import core.models as _models  # noqa: F401  (imported for its side effect)
