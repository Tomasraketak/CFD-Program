"""API key storage.

The properties worth guarding are the security ones: a key must never reach a
project file, the fallback file must not be world-readable, and the interface
must be told honestly which backend accepted the key.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from core import credentials
from core.credentials import (
    CredentialError,
    credentials_path,
    delete_api_key,
    describe_storage,
    has_api_key,
    load_api_key,
    mask_api_key,
    normalise_provider,
    save_api_key,
)

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def no_keyring(monkeypatch):
    """Exercise the file fallback unless a test asks for a keystore."""
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


class FakeKeyring:
    """Minimal stand-in for the ``keyring`` module."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, user: str) -> str | None:
        return self.store.get((service, user))

    def set_password(self, service: str, user: str, password: str) -> None:
        self.store[(service, user)] = password

    def delete_password(self, service: str, user: str) -> None:
        del self.store[(service, user)]


# -- provider names ---------------------------------------------------------


def test_unknown_provider_is_rejected():
    """A typo must not silently create a second credential namespace."""
    with pytest.raises(CredentialError):
        normalise_provider("openrouterr")


def test_provider_name_is_case_insensitive():
    assert normalise_provider("  OpenRouter ") == "openrouter"


# -- the file fallback ------------------------------------------------------


def test_round_trip_through_the_file(isolated_data_root):
    report = save_api_key(KEY, root=isolated_data_root)
    assert report.backend == "file"
    # The fallback is not secure storage and must not claim to be.
    assert report.secure is False
    assert load_api_key(root=isolated_data_root) == KEY


def test_surrounding_whitespace_is_stripped(isolated_data_root):
    """Keys pasted from a browser routinely carry a newline."""
    save_api_key(f"  {KEY}\n", root=isolated_data_root)
    assert load_api_key(root=isolated_data_root) == KEY


def test_empty_key_is_refused(isolated_data_root):
    with pytest.raises(CredentialError):
        save_api_key("   ", root=isolated_data_root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_fallback_file_is_owner_only(isolated_data_root):
    save_api_key(KEY, root=isolated_data_root)
    mode = credentials_path(isolated_data_root).stat().st_mode
    assert not mode & stat.S_IRGRP
    assert not mode & stat.S_IROTH


def test_corrupt_file_does_not_raise(isolated_data_root):
    """A damaged credentials file must not stop the application starting."""
    path = credentials_path(isolated_data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert load_api_key(root=isolated_data_root) is None


def test_deleting_removes_the_key(isolated_data_root):
    save_api_key(KEY, root=isolated_data_root)
    assert delete_api_key(root=isolated_data_root) is True
    assert load_api_key(root=isolated_data_root) is None
    assert delete_api_key(root=isolated_data_root) is False


def test_other_providers_survive_a_delete(isolated_data_root):
    """Removing one key must not wipe the whole file."""
    path = credentials_path(isolated_data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"openrouter": KEY, "other": "keep-me"}), encoding="utf-8"
    )
    delete_api_key(root=isolated_data_root)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {"other": "keep-me"}


# -- precedence -------------------------------------------------------------


def test_environment_variable_wins(isolated_data_root, monkeypatch):
    """A key supplied for one session overrides stored state."""
    save_api_key(KEY, root=isolated_data_root)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-environment")
    assert load_api_key(root=isolated_data_root) == "sk-or-from-environment"
    assert describe_storage(root=isolated_data_root).backend == "environment"


def test_keystore_is_preferred_over_the_file(isolated_data_root, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)

    report = save_api_key(KEY, root=isolated_data_root)
    assert report.backend == "keyring"
    assert report.secure is True
    # Nothing should have been written in plain text.
    assert not credentials_path(isolated_data_root).exists()
    assert load_api_key(root=isolated_data_root) == KEY
    assert describe_storage(root=isolated_data_root).backend == "keyring"


def test_keystore_delete_clears_both_backends(isolated_data_root, monkeypatch):
    fake = FakeKeyring()
    save_api_key(KEY, root=isolated_data_root)  # file, no keyring yet
    monkeypatch.setattr(credentials, "_keyring", lambda: fake)
    save_api_key(KEY, root=isolated_data_root)  # keyring

    assert delete_api_key(root=isolated_data_root) is True
    assert load_api_key(root=isolated_data_root) is None


def test_describe_storage_when_nothing_is_stored(isolated_data_root):
    report = describe_storage(root=isolated_data_root)
    assert report.backend == "none"
    assert report.secure is False
    assert has_api_key(root=isolated_data_root) is False


def test_a_failing_keyring_backend_is_ignored(monkeypatch):
    """Importing keyring succeeds on a headless box even when it cannot work."""
    keyring = pytest.importorskip("keyring")
    from keyring.backends.fail import Keyring as FailKeyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: FailKeyring())
    assert credentials._keyring() is None


# -- display ----------------------------------------------------------------


def test_masking_never_reveals_the_key():
    masked = mask_api_key(KEY)
    assert KEY not in masked
    assert masked.startswith("sk-or-")
    assert masked.endswith(KEY[-4:])


def test_masking_a_short_key_reveals_nothing():
    assert mask_api_key("short") == "*****"


def test_masking_no_key():
    assert mask_api_key(None) == "(none)"


# -- the rule that matters most ---------------------------------------------


def test_a_saved_project_contains_no_api_key(isolated_data_root, tmp_path):
    """Projects are committed to version control; a key there would leak."""
    from core.project import default_project

    save_api_key(KEY, root=isolated_data_root)

    target = default_project().save(tmp_path / "study.atsproj")
    text = target.read_text(encoding="utf-8")

    assert KEY not in text
    assert "api_key" not in text.lower()
    assert "openrouter" not in text.lower()


def test_settings_carry_no_api_key(isolated_data_root):
    """Settings are plain JSON too, and are not a credential store."""
    from core.settings import AppSettings

    save_api_key(KEY, root=isolated_data_root)
    path = AppSettings().save(isolated_data_root)
    assert KEY not in path.read_text(encoding="utf-8")
