"""AeroThermalStudio core: parameter models, units, storage and projects."""

from __future__ import annotations

__version__ = "0.1.0"

# Bumped only when the on-disk project format changes incompatibly, so an
# older application can refuse a newer file instead of misreading it.
PROJECT_FORMAT_VERSION = 1
