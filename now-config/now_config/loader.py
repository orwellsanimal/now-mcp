"""Lazy JSON loader for the instance-config snapshot.

The snapshot root is resolved in this order:
1. `INSTANCE_CONFIG_ROOT` env var (absolute path)
2. First ancestor of cwd that contains an `instance-config/schema/tables.json`

Each file is loaded once on first access and held in memory for the server's lifetime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class SnapshotNotFound(RuntimeError):
    """Raised when the instance-config snapshot can't be located."""


def _resolve_root() -> Path:
    env = os.environ.get("INSTANCE_CONFIG_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not (root / "schema" / "tables.json").is_file():
            raise SnapshotNotFound(
                f"INSTANCE_CONFIG_ROOT={root} does not contain schema/tables.json"
            )
        return root

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        probe = candidate / "instance-config" / "schema" / "tables.json"
        if probe.is_file():
            return candidate / "instance-config"

    raise SnapshotNotFound(
        "Could not locate an instance-config/ snapshot from cwd or any ancestor. "
        "Set INSTANCE_CONFIG_ROOT to point at it explicitly."
    )


class Snapshot:
    """Lazy cache over the instance-config JSON files."""

    def __init__(self, root: Path | None = None) -> None:
        self.root: Path = root if root is not None else _resolve_root()
        self._cache: dict[str, dict[str, Any]] = {}
        self._sys_id_to_table: dict[str, str] | None = None

    def _load(self, rel: str, top_key: str) -> dict[str, Any]:
        if rel in self._cache:
            return self._cache[rel]
        path = self.root / rel
        if not path.is_file():
            raise SnapshotNotFound(f"Missing snapshot file: {path}")
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
        if top_key not in doc:
            raise SnapshotNotFound(f"{path} is missing the {top_key!r} key")
        self._cache[rel] = doc[top_key]
        return self._cache[rel]

    def tables(self) -> dict[str, Any]:
        return self._load("schema/tables.json", "tables")

    def columns(self) -> dict[str, list[dict[str, Any]]]:
        return self._load("schema/columns.json", "columns")

    def choices(self) -> dict[str, list[dict[str, Any]]]:
        return self._load("schema/choices.json", "choices")

    def properties(self) -> dict[str, Any]:
        return self._load("platform/properties.json", "properties")

    def scopes(self) -> dict[str, Any]:
        return self._load("platform/scopes.json", "scopes")

    def roles(self) -> dict[str, Any]:
        return self._load("security/roles.json", "roles")

    def catalog_items(self) -> dict[str, Any]:
        return self._load("catalog/items.json", "items")

    def parent_table(self, table: str) -> str | None:
        """Return the immediate super_class table name, or None if `table` is a root.

        The snapshot stores `super_class` as the parent's sys_id; this method
        resolves it back to the parent's table name via a lazy reverse index.
        """
        rec = self.tables().get(table)
        if rec is None:
            return None
        parent_sys_id = rec.get("super_class", "") or ""
        if not parent_sys_id:
            return None
        if self._sys_id_to_table is None:
            self._sys_id_to_table = {
                r.get("sys_id", ""): name
                for name, r in self.tables().items()
                if r.get("sys_id")
            }
        return self._sys_id_to_table.get(parent_sys_id)

    def ancestor_tables(self, table: str) -> list[str]:
        """Return the inheritance chain from `table` (exclusive) up to the root.

        Example: ancestor_tables("incident") -> ["task"].
        """
        chain: list[str] = []
        current = self.parent_table(table)
        seen: set[str] = set()
        while current and current not in seen:
            chain.append(current)
            seen.add(current)
            current = self.parent_table(current)
        return chain
