"""snacks.json loading, validation, and mtime-based hot reload."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field


class SnacksConfigError(Exception):
    """Raised when a snacks.json file is missing, malformed, or fails validation."""


@dataclass(frozen=True)
class Line:
    id: str
    text: str


@dataclass(frozen=True)
class Snack:
    id: str
    label: str
    slot: int
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)


@dataclass(frozen=True)
class SnacksConfig:
    version: int
    default_snack: str
    snacks: list[Snack]

    def ids(self) -> list[str]:
        return [s.id for s in self.snacks]

    def get(self, snack_id: str) -> Snack | None:
        for s in self.snacks:
            if s.id == snack_id:
                return s
        return None

    def __contains__(self, snack_id: str) -> bool:
        return self.get(snack_id) is not None


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SnacksConfigError(message)


def _parse_line(raw: object, snack_id: str, index: int) -> Line:
    _require(isinstance(raw, dict), f"snack '{snack_id}' lines[{index}] must be an object")
    line_id = raw.get("id")
    text = raw.get("text")
    _require(isinstance(line_id, str) and line_id, f"snack '{snack_id}' lines[{index}].id must be a non-empty string")
    _require(isinstance(text, str) and text, f"snack '{snack_id}' lines[{index}].text must be a non-empty string")
    return Line(id=line_id, text=text)


def _parse_snack(raw: object, index: int) -> Snack:
    _require(isinstance(raw, dict), f"snacks[{index}] must be an object")

    snack_id = raw.get("id")
    _require(isinstance(snack_id, str) and snack_id, f"snacks[{index}].id must be a non-empty string")

    label = raw.get("label")
    _require(isinstance(label, str) and label, f"snack '{snack_id}'.label must be a non-empty string")

    slot = raw.get("slot")
    _require(isinstance(slot, int) and not isinstance(slot, bool), f"snack '{snack_id}'.slot must be an integer")

    aliases = raw.get("aliases", [])
    _require(isinstance(aliases, list) and all(isinstance(a, str) for a in aliases), f"snack '{snack_id}'.aliases must be a list of strings")

    tags = raw.get("tags", [])
    _require(isinstance(tags, list) and all(isinstance(t, str) for t in tags), f"snack '{snack_id}'.tags must be a list of strings")

    lines_raw = raw.get("lines", [])
    _require(isinstance(lines_raw, list) and len(lines_raw) > 0, f"snack '{snack_id}'.lines must be a non-empty list")
    lines = [_parse_line(line, snack_id, i) for i, line in enumerate(lines_raw)]

    return Snack(
        id=snack_id,
        label=label,
        slot=slot,
        aliases=aliases,
        tags=tags,
        lines=lines,
    )


def parse_snacks(data: object) -> SnacksConfig:
    """Validate a decoded snacks.json document and build a SnacksConfig.

    Raises SnacksConfigError with a specific, human-readable message on
    the first validation failure.
    """
    _require(isinstance(data, dict), "snacks.json root must be an object")

    version = data.get("version")
    _require(isinstance(version, int) and not isinstance(version, bool), "'version' must be an integer")

    default_snack = data.get("default_snack")
    _require(isinstance(default_snack, str) and default_snack, "'default_snack' must be a non-empty string")

    snacks_raw = data.get("snacks")
    _require(isinstance(snacks_raw, list) and len(snacks_raw) > 0, "'snacks' must be a non-empty list")

    snacks = [_parse_snack(raw, i) for i, raw in enumerate(snacks_raw)]

    seen_ids: set[str] = set()
    for s in snacks:
        _require(s.id not in seen_ids, f"duplicate snack id '{s.id}'")
        seen_ids.add(s.id)

    _require(default_snack in seen_ids, f"'default_snack' ('{default_snack}') is not a known snack id")

    return SnacksConfig(version=version, default_snack=default_snack, snacks=snacks)


def load_snacks(path: str) -> SnacksConfig:
    """Load and validate a snacks.json file from disk."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as e:
        raise SnacksConfigError(f"could not read snacks config at '{path}': {e}") from e

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SnacksConfigError(f"'{path}' is not valid JSON: {e}") from e

    try:
        return parse_snacks(data)
    except SnacksConfigError as e:
        raise SnacksConfigError(f"'{path}' failed validation: {e}") from e


class SnacksStore:
    """Holds the current SnacksConfig and polls the backing file for changes.

    On an invalid reload, the previous (last-known-good) config is kept
    and the error is logged; the file's mtime is still recorded so the
    same broken file isn't retried every poll until it changes again.
    """

    def __init__(self, path: str, logger: logging.Logger | None = None):
        self._path = path
        self._logger = logger or logging.getLogger(__name__)
        self._current = load_snacks(path)
        self._last_mtime = self._stat_mtime()

    @property
    def path(self) -> str:
        return self._path

    @property
    def current(self) -> SnacksConfig:
        return self._current

    def _stat_mtime(self) -> float | None:
        try:
            return os.stat(self._path).st_mtime
        except OSError:
            return None

    def maybe_reload(self) -> bool:
        """Reload if the file's mtime changed since the last check/attempt.

        Returns True if a new config was successfully loaded and swapped in.
        """
        mtime = self._stat_mtime()
        if mtime == self._last_mtime:
            return False
        self._last_mtime = mtime
        return self._reload()

    def force_reload(self) -> tuple[bool, str]:
        """Reload now regardless of mtime (for a ~/reload service call)."""
        self._last_mtime = self._stat_mtime()
        ok = self._reload()
        if ok:
            return True, "reloaded"
        return False, "reload failed, kept previous config (see logs)"

    def _reload(self) -> bool:
        try:
            new_config = load_snacks(self._path)
        except SnacksConfigError as e:
            self._logger.error(f"snacks.json reload failed, keeping previous config: {e}")
            return False

        old_ids = set(self._current.ids())
        new_ids = set(new_config.ids())
        added = sorted(new_ids - old_ids)
        removed = sorted(old_ids - new_ids)
        if added or removed:
            self._logger.info(f"snacks.json reloaded: added={added} removed={removed}")
        else:
            self._logger.info("snacks.json reloaded: no snack id changes")

        self._current = new_config
        return True
