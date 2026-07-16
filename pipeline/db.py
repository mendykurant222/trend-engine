"""Database access layer. All writes go through here."""

import hashlib
import json
import logging
import os
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("db")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


class ResilientConnection:
    """Auto-reconnecting wrapper: Neon's pooler drops connections that sit
    idle while a slow collector fetches (found by the first scheduled run).
    execute() retries once on a lost connection; everything else passes
    through. Writes are upserts/bookkeeping, so a rare double-retry is safe.
    """

    def __init__(self, url: str):
        self._url = url
        self._conn = self._open()

    def _open(self) -> psycopg.Connection:
        return psycopg.connect(
            self._url, autocommit=True,
            keepalives=1, keepalives_idle=30,
            keepalives_interval=10, keepalives_count=3,
        )

    def execute(self, *args, **kwargs):
        try:
            return self._conn.execute(*args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            log.warning("db connection lost (%s) — reconnecting", exc)
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = self._open()
            return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def connect() -> "ResilientConnection":
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Put the Neon connection string in .env "
            "(see .env.example)."
        )
    return ResilientConnection(url)


def apply_schema(conn: psycopg.Connection) -> None:
    conn.execute(SCHEMA_PATH.read_text())
    log.info("schema applied")


def content_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def start_run(conn) -> int:
    row = conn.execute("insert into runs default values returning id").fetchone()
    return row[0]


def finish_run(conn, run_id: int, status: str, summary: dict) -> None:
    conn.execute(
        "update runs set finished_at = now(), status = %s, summary = %s where id = %s",
        (status, Jsonb(summary), run_id),
    )


def record_collector_run(conn, run_id: int, collector: str, status: str,
                         items_seen: int = 0, items_stored: int = 0,
                         duration_s: float | None = None, error: str | None = None) -> None:
    conn.execute(
        """insert into run_collectors
           (run_id, collector, status, items_seen, items_stored, duration_s, error)
           values (%s, %s, %s, %s, %s, %s, %s)""",
        (run_id, collector, status, items_seen, items_stored, duration_s, error),
    )


def record_cost(conn, run_id: int | None, provider: str, operation: str,
                units: int = 1, cost_usd: float = 0.0) -> None:
    conn.execute(
        "insert into api_costs (run_id, provider, operation, units, cost_usd) values (%s, %s, %s, %s, %s)",
        (run_id, provider, operation, units, cost_usd),
    )


def store_raw_items(conn, source: str, items: list[dict]) -> int:
    """Insert raw items with hash-based dedupe. Returns number actually stored.

    Each item: {"external_id": str|None, "item_date": date|str|None, "payload": dict}
    """
    stored = 0
    for item in items:
        payload = item["payload"]
        h = content_hash(payload)
        cur = conn.execute(
            """insert into raw_items (source, external_id, content_hash, item_date, payload)
               values (%s, %s, %s, %s, %s)
               on conflict (source, content_hash) do nothing""",
            (source, item.get("external_id"), h, item.get("item_date"), Jsonb(payload)),
        )
        stored += cur.rowcount
    return stored


def watch_entities(conn, limit: int = 20) -> list[tuple[int, str]]:
    """Entities worth querying in entity-driven collectors (EDGAR, GDELT):
    members of active trends, recently anomalous, or newly discovered."""
    rows = conn.execute(
        """select distinct e.id, e.canonical_name from entities e
           where e.status = 'active' and (
             exists (select 1 from trend_cluster_entities tce
                     join trend_clusters t on t.id = tce.cluster_id and t.status = 'active'
                     where tce.entity_id = e.id)
             or exists (select 1 from anomalies a
                        where a.entity_id = e.id and a.signal_date >= current_date - 14)
             or e.first_seen >= current_date - 7)
           order by e.id desc limit %s""", (limit,),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def month_spend(conn) -> list[tuple[str, float]]:
    """Current calendar-month spend per provider, for the budget line in the daily summary."""
    rows = conn.execute(
        """select provider, coalesce(sum(cost_usd), 0)
           from api_costs
           where created_at >= date_trunc('month', now())
           group by provider order by 2 desc"""
    ).fetchall()
    return [(r[0], float(r[1])) for r in rows]
