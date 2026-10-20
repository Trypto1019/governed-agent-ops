"""Tiny database adapter: SQLite for the local demo, Postgres in production.

Every module talks to the database through this wrapper so the SQL stays
portable. Write SQL with `?` placeholders; the Postgres path rewrites them to
`%s`. Timestamps are passed in from Python (`common.clock.now_iso()`) rather
than using `now()`, so both engines and the demo clock agree.

    DATABASE_URL=sqlite:///var/demo.sqlite          (default)
    DATABASE_URL=postgresql://<user>:<password>@host/dbname (needs `psycopg`)
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable

DEFAULT_URL = "sqlite:///var/demo.sqlite"
ROOT = Path(__file__).resolve().parent.parent


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


class DB:
    """A connection plus a few helpers. Rows come back as dicts."""

    def __init__(self, url: str | None = None):
        self.url = url or database_url()
        if self.url.startswith("sqlite:///"):
            self.kind = "sqlite"
            path = self.url[len("sqlite:///"):]
            if path != ":memory:":
                # Relative paths resolve against the repo root, so the
                # dispatcher's child processes all open the same file.
                p = Path(path)
                path = str(p if p.is_absolute() else ROOT / p)
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path, timeout=30)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
        elif self.url.startswith(("postgres://", "postgresql://")):
            self.kind = "postgres"
            try:
                import psycopg  # type: ignore
                from psycopg.rows import dict_row  # type: ignore
            except ImportError as e:  # pragma: no cover - optional dependency
                raise RuntimeError("Postgres DATABASE_URL needs `pip install psycopg`") from e
            self.conn = psycopg.connect(self.url, row_factory=dict_row)
        else:
            raise ValueError("DATABASE_URL must start with sqlite:/// or postgresql://")

    # ------------------------------------------------------------ core
    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.kind == "postgres" else sql

    def execute(self, sql: str, params: Iterable[Any] = ()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.execute(sql, params).fetchone()
        if row is None:
            return None
        return row[0] if self.kind == "sqlite" else next(iter(dict(row).values()))

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run an INSERT ... RETURNING id and return the id."""
        return int(self.scalar(sql, params))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "DB":
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.commit()
        self.close()

    # ------------------------------------------------------------ schema
    def init_schema(self) -> None:
        """Create tables. SQLite uses schema/sqlite.sql; Postgres uses schema/ddl.sql."""
        name = "sqlite.sql" if self.kind == "sqlite" else "ddl.sql"
        ddl = (ROOT / "schema" / name).read_text()
        if self.kind == "sqlite":
            self.conn.executescript(ddl)
        else:  # pragma: no cover - exercised only against a real Postgres
            self.conn.execute(ddl)
        self.commit()


def connect(url: str | None = None) -> DB:
    return DB(url)


def as_json(value: Any) -> Any:
    """JSON columns are TEXT in SQLite and JSONB in Postgres; normalise to Python."""
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def dumps(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True)
