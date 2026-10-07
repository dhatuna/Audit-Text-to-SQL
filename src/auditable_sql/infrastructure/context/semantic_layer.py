"""Load the semantic layer YAML and derive both the model context and the SQL policy from it."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from auditable_sql.domain.models import SchemaContext
from auditable_sql.domain.policy import SqlPolicy


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    description: str
    exposed: bool


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    description: str
    columns: tuple[Column, ...]

    @property
    def exposed_columns(self) -> tuple[Column, ...]:
        return tuple(c for c in self.columns if c.exposed)


def _context(text: str, version: str) -> SchemaContext:
    return SchemaContext(
        text=text, version=version, sha256=hashlib.sha256(text.encode()).hexdigest()
    )


class SemanticLayer:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.version: str = str(raw["version"])
        self.coverage: dict[str, str] = raw["data_coverage"]
        self.tables: tuple[Table, ...] = tuple(
            Table(
                name=tname,
                description=str(t.get("description", "")).strip(),
                columns=tuple(
                    Column(
                        name=cname,
                        type=str(c["type"]),
                        description=str(c.get("description", "")),
                        exposed=bool(c.get("exposed", True)),
                    )
                    for cname, c in t["columns"].items()
                ),
            )
            for tname, t in raw["tables"].items()
        )
        self.joins: tuple[str, ...] = tuple(raw.get("joins", ()))
        self.definitions: tuple[dict[str, str], ...] = tuple(raw.get("definitions", ()))
        self.not_supported: tuple[str, ...] = tuple(raw.get("not_supported", ()))

    @classmethod
    def load(cls, path: Path) -> SemanticLayer:
        return cls(yaml.safe_load(path.read_text()))

    def policy(self, max_rows: int) -> SqlPolicy:
        return SqlPolicy(
            allowed_columns={
                t.name: frozenset(c.name for c in t.exposed_columns)
                for t in self.tables
                if t.exposed_columns
            },
            max_rows=max_rows,
        )

    def render(self) -> str:
        """Full context: descriptions, join paths, business definitions, explicit gaps."""
        out: list[str] = [
            f"Data covers {self.coverage['first_date']} to {self.coverage['last_date']}. "
            f"{self.coverage['calendar']}",
            "",
            "## Tables (only these columns exist for you)",
        ]
        for t in self.tables:
            if not t.exposed_columns:
                continue
            out.append(f"\n### {t.name}\n{t.description}")
            for c in t.exposed_columns:
                desc = f" -- {c.description}" if c.description else ""
                out.append(f"- {c.name} {c.type}{desc}")
        out += ["", "## Join paths", *(f"- {j}" for j in self.joins)]
        out += ["", "## Business definitions"]
        out += [f"- {d['term']}: {' '.join(d['meaning'].split())}" for d in self.definitions]
        out += ["", "## Not available (decline these)", *(f"- {n}" for n in self.not_supported)]
        return "\n".join(out)

    def render_ddl(self) -> str:
        """Bare DDL of exposed columns: the naive baseline used for before/after evaluation."""
        blocks = []
        for t in self.tables:
            if t.exposed_columns:
                cols = ",\n".join(f"  {c.name} {c.type}" for c in t.exposed_columns)
                blocks.append(f"CREATE TABLE {t.name} (\n{cols}\n);")
        return "\n\n".join(blocks)


class SemanticLayerContext:
    """SchemaContextProvider backed by the semantic layer (descriptions + definitions)."""

    def __init__(self, layer: SemanticLayer) -> None:
        self._ctx = _context(layer.render(), f"semantic:{layer.version}")

    def get(self) -> SchemaContext:
        return self._ctx


class RawDdlContext:
    """SchemaContextProvider that only shows DDL. Exists to measure what the semantic layer buys."""

    def __init__(self, layer: SemanticLayer) -> None:
        self._ctx = _context(layer.render_ddl(), f"ddl:{layer.version}")

    def get(self) -> SchemaContext:
        return self._ctx
