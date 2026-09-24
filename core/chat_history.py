"""Saved assistant conversations, so work can be picked up where it stopped.

Each conversation is one JSON file under ``<data root>/ai_chats``: the
messages the model saw (tool calls and results included, which is what lets
it carry on with the same mesh and simulation ids), the transcript as the
operator saw it, and the ids of every mesh, simulation and project the
conversation touched.

Conversations stay on this machine. They hold what was asked and what the
tools returned; they never hold the API key, which lives in the credential
store and is never part of a message.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.platform_env import data_root
from core.store import RUN_ID_PATTERN

CHAT_DIRECTORY = "ai_chats"

# Ids the run registry hands out, and saved project files.
_RUN_ID = re.compile(rf"\b{RUN_ID_PATTERN}\b")
_PROJECT = re.compile(r"[^\s\"'<>|]+\.atsproj\b")

# Long enough to recognise a conversation in a list.
TITLE_LENGTH = 70


def _now() -> str:
    # Microseconds, so two saves in the same second still sort correctly.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Conversation:
    """One saved chat."""

    chat_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    model: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript_html: str = ""

    def run_ids(self) -> list[str]:
        """Every mesh, simulation and sweep id the conversation mentions, in order."""
        seen: dict[str, None] = {}
        for text in self._texts():
            for match in _RUN_ID.findall(text):
                seen.setdefault(match, None)
        return list(seen)

    def project_paths(self) -> list[str]:
        """Project files saved or opened in the conversation."""
        seen: dict[str, None] = {}
        for text in self._texts():
            for match in _PROJECT.findall(text.replace("\\\\", "\\")):
                seen.setdefault(match, None)
        return list(seen)

    def _texts(self) -> list[str]:
        texts: list[str] = []
        for message in self.messages:
            content = message.get("content")
            if isinstance(content, str):
                texts.append(content)
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                texts.append(str(function.get("arguments", "")))
        return texts

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "model": self.model,
            "messages": self.messages,
            "transcript_html": self.transcript_html,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Conversation":
        return cls(
            chat_id=str(data.get("chat_id") or uuid.uuid4().hex[:12]),
            title=str(data.get("title", "")),
            created_at=str(data.get("created_at", _now())),
            updated_at=str(data.get("updated_at", _now())),
            model=str(data.get("model", "")),
            messages=list(data.get("messages") or []),
            transcript_html=str(data.get("transcript_html", "")),
        )


def title_for(messages: list[dict[str, Any]]) -> str:
    """The first thing the operator asked, shortened."""
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            text = " ".join(message["content"].split())
            return text if len(text) <= TITLE_LENGTH else text[: TITLE_LENGTH - 1] + "…"
    return "New conversation"


class ChatHistory:
    """Saved conversations on disk, newest first."""

    def __init__(self, root: Path | str | None = None) -> None:
        base = Path(root) if root is not None else data_root()
        self.directory = base / CHAT_DIRECTORY

    def _path(self, chat_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{6,32}", chat_id):
            raise ValueError(f"not a conversation id: {chat_id!r}")
        return self.directory / f"{chat_id}.json"

    def save(self, conversation: Conversation) -> Path:
        """Write a conversation, stamping it as updated now."""
        self.directory.mkdir(parents=True, exist_ok=True)
        conversation.updated_at = _now()
        if not conversation.title:
            conversation.title = title_for(conversation.messages)
        path = self._path(conversation.chat_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(conversation.as_dict(), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def load(self, chat_id: str) -> Conversation:
        return Conversation.from_dict(
            json.loads(self._path(chat_id).read_text(encoding="utf-8"))
        )

    def delete(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if path.is_file():
            path.unlink()

    def list(self) -> list[Conversation]:
        """Every readable conversation, most recently updated first."""
        if not self.directory.is_dir():
            return []
        conversations = []
        for path in self.directory.glob("*.json"):
            try:
                conversations.append(
                    Conversation.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, ValueError):
                continue
        return sorted(conversations, key=lambda c: c.updated_at, reverse=True)
