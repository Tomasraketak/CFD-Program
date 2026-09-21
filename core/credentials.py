"""Storage of API keys for external services.

An API key is a bearer credential: anyone holding it can spend the owner's
money. It therefore gets deliberately different treatment from every other
setting in this application.

Three rules shape this module:

* **Never in a project file.** Projects are plain JSON that operators commit
  to version control alongside their CAD. A key written there would be pushed
  to a repository. :mod:`core.project` has no credential field, and a test
  asserts that saved projects contain no key.
* **The operating system's keystore first.** On Windows that is Credential
  Manager, reached through the optional ``keyring`` package. That is real
  storage, protected by the user's login.
* **Be honest about the fallback.** With no keystore available the key goes
  into a file with owner-only permissions. That protects it from other users
  on the machine and from nothing else. :func:`describe_storage` reports which
  backend is in use so the interface can say so plainly rather than implying a
  security it does not have.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core.platform_env import data_root

# Service name under which keys are filed in the OS keystore.
KEYRING_SERVICE = "AeroThermalStudio"

# Fallback file, used only when no OS keystore is available.
CREDENTIALS_FILENAME = "credentials.json"

# Environment variables checked before any stored key, so a CI run or a power
# user can override without touching stored state.
ENVIRONMENT_VARIABLES: dict[str, str] = {
    "openrouter": "OPENROUTER_API_KEY",
}

# Providers this application knows how to talk to.
KNOWN_PROVIDERS = ("openrouter",)


class CredentialError(RuntimeError):
    """Raised when a credential cannot be stored or retrieved."""


@dataclass(frozen=True)
class StorageReport:
    """Where a provider's key lives, and how well protected it is.

    Attributes
    ----------
    backend:
        ``environment``, ``keyring``, ``file`` or ``none``.
    secure:
        True only for the OS keystore. A file is not secure storage and this
        flag exists so the interface can say so.
    detail:
        One sentence suitable for showing to the operator.
    """

    backend: str
    secure: bool
    detail: str

    def as_dict(self) -> dict[str, object]:
        """JSON-serialisable form."""
        return {"backend": self.backend, "secure": self.secure, "detail": self.detail}


def _keyring():
    """Return the ``keyring`` module, or None when it is unusable.

    Importing is not enough: on a headless Linux box the import succeeds but
    every operation raises, so a backend that actually works is required.
    """
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception:
        return None

    try:
        backend = keyring.get_keyring()
    except Exception:  # pragma: no cover - depends on the host
        return None

    if backend is None or isinstance(backend, FailKeyring):
        return None
    return keyring


def credentials_path(root: Path | str | None = None) -> Path:
    """Path of the fallback credentials file."""
    base = Path(root) if root is not None else data_root()
    return base / CREDENTIALS_FILENAME


def _read_file(root: Path | str | None = None) -> dict[str, str]:
    """Read the fallback file, tolerating absence and corruption."""
    path = credentials_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in payload.items()
        if isinstance(value, str)
    }


def _write_file(payload: dict[str, str], root: Path | str | None = None) -> Path:
    """Write the fallback file with owner-only permissions."""
    path = credentials_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Restrict before the file is in place, so it is never briefly readable
        # by other users under its final name.
        _restrict_permissions(temporary)
        temporary.replace(path)
        _restrict_permissions(path)
    except OSError as error:
        raise CredentialError(
            f"could not write the credentials file at {path}: {error}"
        ) from error
    return path


def _restrict_permissions(path: Path) -> None:
    """Make a file readable and writable only by its owner.

    A no-op where the platform does not support it; Windows relies on the
    user profile's own ACL instead.
    """
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pass


def normalise_provider(provider: str) -> str:
    """Validate and canonicalise a provider name."""
    key = provider.strip().lower()
    if key not in KNOWN_PROVIDERS:
        raise CredentialError(
            f"unknown provider '{provider}'; known providers: "
            f"{', '.join(KNOWN_PROVIDERS)}"
        )
    return key


def load_api_key(
    provider: str = "openrouter", root: Path | str | None = None
) -> str | None:
    """Retrieve a stored API key.

    Looked up in order: environment variable, OS keystore, fallback file. The
    environment wins so a key can be supplied for one session without being
    written anywhere.

    Returns
    -------
    str or None
        The key, or None when none is stored.
    """
    provider = normalise_provider(provider)

    variable = ENVIRONMENT_VARIABLES.get(provider)
    if variable:
        from_environment = os.environ.get(variable, "").strip()
        if from_environment:
            return from_environment

    keyring = _keyring()
    if keyring is not None:
        try:
            stored = keyring.get_password(KEYRING_SERVICE, provider)
            if stored:
                return stored
        except Exception:  # pragma: no cover - backend dependent
            pass

    return _read_file(root).get(provider) or None


def save_api_key(
    api_key: str, provider: str = "openrouter", root: Path | str | None = None
) -> StorageReport:
    """Store an API key, preferring the operating system's keystore.

    Parameters
    ----------
    api_key:
        The key to store. Surrounding whitespace is stripped, because keys
        pasted from a browser routinely carry it.
    provider:
        Which service the key belongs to.

    Returns
    -------
    StorageReport
        Which backend accepted the key and whether it is real secure storage.

    Raises
    ------
    CredentialError
        If the key is empty or cannot be stored anywhere.
    """
    provider = normalise_provider(provider)
    api_key = (api_key or "").strip()
    if not api_key:
        raise CredentialError("the API key is empty")

    keyring = _keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, provider, api_key)
            return StorageReport(
                backend="keyring",
                secure=True,
                detail=(
                    "Stored in the operating system's credential manager, "
                    "protected by your login."
                ),
            )
        except Exception:  # pragma: no cover - backend dependent
            pass

    payload = _read_file(root)
    payload[provider] = api_key
    path = _write_file(payload, root)
    return StorageReport(
        backend="file",
        secure=False,
        detail=(
            f"No OS credential store was available, so the key is in a "
            f"plain-text file readable only by your account:\n{path}\n"
            "Install the 'keyring' package for proper storage."
        ),
    )


def delete_api_key(
    provider: str = "openrouter", root: Path | str | None = None
) -> bool:
    """Remove a stored key from every backend.

    Returns
    -------
    bool
        True when something was actually removed.
    """
    provider = normalise_provider(provider)
    removed = False

    keyring = _keyring()
    if keyring is not None:
        try:
            if keyring.get_password(KEYRING_SERVICE, provider):
                keyring.delete_password(KEYRING_SERVICE, provider)
                removed = True
        except Exception:  # pragma: no cover - backend dependent
            pass

    payload = _read_file(root)
    if provider in payload:
        del payload[provider]
        _write_file(payload, root)
        removed = True

    return removed


def has_api_key(
    provider: str = "openrouter", root: Path | str | None = None
) -> bool:
    """True when a key is available from any source."""
    return bool(load_api_key(provider, root))


def describe_storage(
    provider: str = "openrouter", root: Path | str | None = None
) -> StorageReport:
    """Report where this provider's key currently comes from."""
    provider = normalise_provider(provider)

    variable = ENVIRONMENT_VARIABLES.get(provider)
    if variable and os.environ.get(variable, "").strip():
        return StorageReport(
            backend="environment",
            secure=True,
            detail=f"Supplied by the {variable} environment variable.",
        )

    keyring = _keyring()
    if keyring is not None:
        try:
            if keyring.get_password(KEYRING_SERVICE, provider):
                return StorageReport(
                    backend="keyring",
                    secure=True,
                    detail=(
                        "Stored in the operating system's credential manager."
                    ),
                )
        except Exception:  # pragma: no cover - backend dependent
            pass

    if provider in _read_file(root):
        return StorageReport(
            backend="file",
            secure=False,
            detail=(
                f"Stored in a plain-text file readable only by your account: "
                f"{credentials_path(root)}"
            ),
        )

    return StorageReport(
        backend="none", secure=False, detail="No API key is stored."
    )


def mask_api_key(api_key: str | None) -> str:
    """Render a key for display without revealing it.

    Shows only enough to tell two keys apart. Never log or display the key
    itself.
    """
    if not api_key:
        return "(none)"
    cleaned = api_key.strip()
    if len(cleaned) <= 10:
        return "*" * len(cleaned)
    return f"{cleaned[:6]}...{cleaned[-4:]}"
