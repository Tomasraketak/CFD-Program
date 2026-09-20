"""Shared pytest fixtures.

Redirects the application data root into a temporary directory so no test
touches the operator's real ``%LOCALAPPDATA%`` tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable when pytest is invoked from elsewhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.store import RunStore, reset_default_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path, monkeypatch):
    """Point the data root at a per-test temporary directory."""
    root = tmp_path / "ats-data"
    monkeypatch.setenv("ATS_DATA_ROOT", str(root))
    reset_default_store()
    yield root
    reset_default_store()


@pytest.fixture
def store(isolated_data_root) -> RunStore:
    """A RunStore rooted in the isolated data root."""
    return RunStore()
