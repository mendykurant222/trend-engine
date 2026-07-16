"""Anomaly detector test with synthetic signal histories — no API keys needed.

Builds 5 weeks of deterministic daily_signals for four scenarios, runs
detect_anomalies for today, checks the verdicts, and cleans up.

  steady-widget    stable ~3/day, today 3            -> must NOT flag
  spiking-gadget   ~1.5/day, today 14, in 2 sources  -> surge + cross-source ×2
  ramping-lamp     0.5/day -> 2.5/day this week      -> acceleration
  fresh-thing      first seen 3 days ago, 10 mentions
                   across 2 sources                  -> new_entity

Usage: python -m scripts.anomaly_test
"""

import sys
from datetime import date, timedelta

from dotenv import load_dotenv

from analysis.anomalies import debug_report, detect_anomalies
from pipeline import db
from pipeline.orchestrator import load_config

SRC_A, SRC_B = "_synthetic", "_synthetic2"
TODAY = date.today()


def seed(conn):
    entities = {}

    def mk_entity(name, first_seen_days_ago):
        row = conn.execute(
            "insert into entities (canonical_name, category, first_seen) values (%s, 'consumer-products', %s) returning id",
            (name, TODAY - timedelta(days=first_seen_days_ago)),
        ).fetchone()
        entities[name] = row[0]

    def sig(name, days_ago, value, source=SRC_A):
        conn.execute(
            """insert into daily_signals (entity_id, source, signal_date, metric, value)
               values (%s, %s, %s, 'mentions', %s)
               on conflict (entity_id, source, signal_date, metric) do update set value = excluded.value""",
            (entities[name], source, TODAY - timedelta(days=days_ago), value),
        )

    # steady-widget: 3/day forever, today 3 (clears min_mentions, no surge)
    mk_entity("steady-widget", 40)
    for i in range(0, 36):
        sig("steady-widget", i, 3)

    # spiking-gadget: 1-2/day baseline, today 14 in BOTH sources
    mk_entity("spiking-gadget", 40)
    for i in range(1, 36):
        sig("spiking-gadget", i, 1 + (i % 2))            # alternating 1,2
        sig("spiking-gadget", i, 1, source=SRC_B)
    sig("spiking-gadget", 0, 14)
    sig("spiking-gadget", 0, 9, source=SRC_B)

    # ramping-lamp: 0-1/day for weeks, then 2-3/day this week (velocity 0.5 -> 2.5)
    mk_entity("ramping-lamp", 40)
    for i in range(7, 36):
        sig("ramping-lamp", i, i % 2)                    # ~0.5/day
    for i in range(0, 7):
        sig("ramping-lamp", i, 2 + (i % 2))              # ~2.5/day
    sig("ramping-lamp", 0, 3)

    # fresh-thing: born 3 days ago, 10 mentions across 2 sources
    mk_entity("fresh-thing", 3)
    for i in range(0, 3):
        sig("fresh-thing", i, 2)
        sig("fresh-thing", i, 2 - (i % 2), source=SRC_B)

    # nike: same new-entity profile, but matches "NIKE, Inc." in the SEC
    # companies table -> established-brand damping must suppress it (item 23)
    mk_entity("nike", 3)
    for i in range(0, 3):
        sig("nike", i, 2)
        sig("nike", i, 2, source=SRC_B)

    return entities


def main() -> int:
    load_dotenv()
    conn = db.connect()
    db.apply_schema(conn)
    config = load_config()
    failures = []
    entities = {}

    try:
        entities = seed(conn)
        # pre-existing LEADING-source anomaly 5 days ago -> today's lagging
        # (_synthetic2) anomalies for spiking-gadget must earn the sequence bonus
        conn.execute(
            """insert into anomalies (entity_id, source, signal_date, kind, score, details)
               values (%s, '_synthetic', %s, 'surge', 70, '{"today": 8, "baseline_mean": 1.5, "p": 1e-6}')""",
            (entities["spiking-gadget"], TODAY - timedelta(days=5)))
        n = detect_anomalies(conn, config, TODAY)
        print(f"{n} anomalies found\n")
        for line in debug_report(conn, TODAY):
            print(line)

        rows = conn.execute(
            """select e.canonical_name, a.source, a.kind, a.score, a.details
               from anomalies a join entities e on e.id = a.entity_id
               where a.signal_date = %s and e.id = any(%s)""",
            (TODAY, list(entities.values())),
        ).fetchall()
        kinds = {(name, kind) for name, _, kind, _, _ in rows}
        by_name_kind = {(name, kind): (score, details) for name, _, kind, score, details in rows}

        if any(name == "steady-widget" for name, _ in kinds):
            failures.append("steady-widget was flagged (false positive)")
        if ("spiking-gadget", "surge") not in kinds:
            failures.append("spiking-gadget surge NOT detected")
        else:
            _, details = by_name_kind[("spiking-gadget", "surge")]
            if not details.get("cross_source"):
                failures.append("spiking-gadget missing cross-source bonus")
        if ("ramping-lamp", "acceleration") not in kinds:
            failures.append("ramping-lamp acceleration NOT detected")
        if ("fresh-thing", "new_entity") not in kinds:
            failures.append("fresh-thing new_entity NOT detected")
        if any(name == "nike" for name, _ in kinds):
            failures.append("nike flagged despite established-brand damping")

        # sequence bonus (item 61): leading fired 5d before today's lagging source
        seq = conn.execute(
            """select 1 from anomalies where signal_date = %s and entity_id = %s
               and source = '_synthetic2' and details @> '{"sequence": true}' limit 1""",
            (TODAY, entities["spiking-gadget"])).fetchone()
        if not seq:
            failures.append("sequence bonus NOT applied to spiking-gadget lagging anomaly")

    finally:
        eids = list(entities.values())
        if eids:
            conn.execute("delete from anomalies where entity_id = any(%s)", (eids,))
            conn.execute("delete from daily_signals where entity_id = any(%s)", (eids,))
            conn.execute("delete from entity_aliases where entity_id = any(%s)", (eids,))
            conn.execute("delete from entities where id = any(%s)", (eids,))
        print("\ncleanup done")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("ANOMALY TEST PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
