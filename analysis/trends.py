"""Phase 3 — the Claude brain (plan items 24-27).

Clustering: anomalous entities are grouped into named trends. Assignment is
two-stage: (1) deterministic trend memory — an entity already linked to an
active trend updates that trend, no LLM involved (plan item 27: matching by
entity overlap, never by name); (2) the remaining entities go to Claude
Sonnet, together with the list of active trends so it can attach to an
existing trend by id instead of opening a duplicate.

Each trend carries strength (1-100) AND a separate confidence score (plan
item 25), a full lifecycle history, and an evidence trail (trend_evidence)
down to the individual anomaly for drill-down.

Investment mapping (plan item 26): Claude proposes tickers, but only tickers
that verify against the SEC-loaded companies table are stored — Claude never
invents an investable symbol into the DB.
"""

import logging
from datetime import date

from psycopg.types.json import Jsonb

from pipeline import llm

log = logging.getLogger("trends")

STAGES = ["emerging", "accelerating", "peak", "declining"]

CLUSTER_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short trend name, english, lowercase"},
                    "category": {"type": "string"},
                    "stage": {"type": "string", "enum": STAGES},
                    "strength": {"type": "integer", "description": "1-100"},
                    "confidence": {"type": "integer",
                                   "description": "1-100, based on source count, data volume, entity age"},
                    "entities": {"type": "array", "items": {"type": "string"},
                                 "description": "canonical entity names from the input, verbatim"},
                    "existing_trend_id": {"type": ["integer", "null"],
                                          "description": "id of an ACTIVE TREND this belongs to, else null"},
                    "rationale": {"type": "string"},
                },
                "required": ["name", "category", "stage", "strength", "confidence",
                             "entities", "existing_trend_id", "rationale"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["clusters"],
    "additionalProperties": False,
}

CLUSTER_SYSTEM = """You are the trend-clustering brain of a consumer-trend discovery system.
You receive entities that showed anomalous momentum today (with their signals), plus the
list of already-tracked active trends.

Rules:
- Group entities that are the SAME underlying consumer trend (a TikTok hashtag, an Amazon
  product and a Reddit topic about the same thing = one trend).
- If a group belongs to an already-tracked trend, set existing_trend_id — do NOT open a
  duplicate trend under a new name.
- strength = how big the momentum is. confidence = how sure you are it's real: more sources,
  more data, older entities => higher confidence. A one-source one-day blip = low confidence.
- Every input entity must appear in exactly one cluster. Singleton clusters are fine."""

INVEST_SCHEMA = {
    "type": "object",
    "properties": {
        "companies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "US stock ticker, uppercase"},
                    "name": {"type": "string"},
                    "exposure": {"type": "string", "enum": ["manufacturer", "retailer", "supplier"]},
                    "direction": {"type": "string", "enum": ["positive", "negative"]},
                    "confidence": {"type": "integer", "description": "1-100"},
                    "material": {"type": "boolean",
                                 "description": "true only if the trend could move this company's revenue meaningfully"},
                },
                "required": ["ticker", "name", "exposure", "direction", "confidence", "material"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["companies"],
    "additionalProperties": False,
}

INVEST_SYSTEM = """You map consumer trends to publicly traded US companies (SEC-listed).
Given a trend, propose companies exposed to it: manufacturers, retailers, suppliers —
positive or negative exposure (e.g. GLP-1 drugs are negative for snack makers).
Only real US tickers. material=true ONLY when the trend is large enough relative to the
company's revenue to matter — a single viral product rarely moves a mega-cap. Prefer
pure-plays. If no listed company is meaningfully exposed, return an empty list."""


def _anomalous_entities(conn, target_date: date) -> dict[int, dict]:
    """Entities with anomalies today, with their evidence."""
    rows = conn.execute(
        """select e.id, e.canonical_name, e.category, e.first_seen,
                  a.id, a.source, a.kind, a.score, a.details
           from anomalies a join entities e on e.id = a.entity_id
           where a.signal_date = %s and e.status = 'active'
           order by a.score desc""",
        (target_date,),
    ).fetchall()
    out: dict[int, dict] = {}
    for eid, name, category, first_seen, aid, source, kind, score, details in rows:
        ent = out.setdefault(eid, {"name": name, "category": category,
                                   "first_seen": first_seen, "anomalies": []})
        ent["anomalies"].append({"id": aid, "source": source, "kind": kind,
                                 "score": float(score), "details": details})
    return out


def _active_trends(conn) -> dict[int, dict]:
    trends: dict[int, dict] = {}
    for tid, name, stage, strength, confidence in conn.execute(
        "select id, name, stage, strength, confidence from trend_clusters where status = 'active'"
    ).fetchall():
        trends[tid] = {"name": name, "stage": stage, "strength": strength,
                       "confidence": confidence, "entity_ids": set()}
    for tid, eid in conn.execute(
        """select tce.cluster_id, tce.entity_id from trend_cluster_entities tce
           join trend_clusters t on t.id = tce.cluster_id where t.status = 'active'"""
    ).fetchall():
        if tid in trends:
            trends[tid]["entity_ids"].add(eid)
    return trends


def _append_lifecycle(conn, cluster_id: int, target_date: date,
                      stage: str, strength: int, confidence: int) -> None:
    """One lifecycle entry per day per trend (re-runs replace, not duplicate)."""
    entry = {"date": str(target_date), "stage": stage,
             "strength": strength, "confidence": confidence}
    conn.execute(
        """update trend_clusters set
             lifecycle = (select coalesce(jsonb_agg(e), '[]'::jsonb)
                          from jsonb_array_elements(lifecycle) e
                          where e->>'date' != %s) || %s::jsonb,
             stage = %s, strength = %s, confidence = %s, last_updated = %s
           where id = %s""",
        (str(target_date), Jsonb(entry), stage, strength, confidence, target_date, cluster_id),
    )


def _add_evidence(conn, cluster_id: int, anomaly_ids: list[int]) -> None:
    for aid in anomaly_ids:
        conn.execute(
            "insert into trend_evidence (cluster_id, anomaly_id) values (%s, %s) on conflict do nothing",
            (cluster_id, aid),
        )


def _link_entities(conn, cluster_id: int, entity_ids: list[int]) -> None:
    for eid in entity_ids:
        conn.execute(
            "insert into trend_cluster_entities (cluster_id, entity_id) values (%s, %s) on conflict do nothing",
            (cluster_id, eid),
        )


def cluster_anomalies(conn, config: dict, run_id: int | None,
                      target_date: date | None = None) -> dict:
    target_date = target_date or date.today()
    entities = _anomalous_entities(conn, target_date)
    if not entities:
        return {"new": 0, "updated": 0, "entities": 0}
    trends = _active_trends(conn)

    # ---- stage 1: trend memory — deterministic entity-overlap assignment ----
    updated_ids = set()
    unassigned: dict[int, dict] = {}
    for eid, ent in entities.items():
        home = next((tid for tid, t in trends.items() if eid in t["entity_ids"]), None)
        if home is not None:
            _add_evidence(conn, home, [a["id"] for a in ent["anomalies"]])
            t = trends[home]
            _append_lifecycle(conn, home, target_date, t["stage"], t["strength"], t["confidence"])
            updated_ids.add(home)
        else:
            unassigned[eid] = ent

    new_ids = []
    if unassigned:
        # ---- stage 2: Claude clusters the rest ----
        by_name = {ent["name"]: eid for eid, ent in unassigned.items()}
        entity_lines = []
        for ent in unassigned.values():
            evidence = "; ".join(
                f"{a['source']}/{a['kind']} score {a['score']:.0f}" for a in ent["anomalies"])
            entity_lines.append(
                f"- {ent['name']} (category: {ent['category']}, first seen {ent['first_seen']}): {evidence}")
        trend_lines = [
            f"- id {tid}: {t['name']} (stage {t['stage']})" for tid, t in trends.items()
        ] or ["(none yet)"]
        user_text = (
            f"Date: {target_date}\n\nAnomalous entities today:\n" + "\n".join(entity_lines)
            + "\n\nActive trends already tracked:\n" + "\n".join(trend_lines)
        )
        result = llm.structured_call(conn, run_id, "trend_clustering",
                                     llm.MODEL_ANALYSIS, CLUSTER_SYSTEM, user_text,
                                     CLUSTER_SCHEMA)

        for cluster in result.get("clusters", []):
            eids = [by_name[n] for n in cluster.get("entities", []) if n in by_name]
            if not eids:
                continue
            anomaly_ids = [a["id"] for eid in eids for a in unassigned[eid]["anomalies"]]
            stage = cluster["stage"] if cluster["stage"] in STAGES else "emerging"
            strength = max(1, min(100, int(cluster["strength"])))
            confidence = max(1, min(100, int(cluster["confidence"])))

            existing = cluster.get("existing_trend_id")
            if existing in trends:
                cid = existing
                updated_ids.add(cid)
            else:
                cid = conn.execute(
                    """insert into trend_clusters
                       (name, category, stage, strength, confidence, first_detected, last_updated)
                       values (%s, %s, %s, %s, %s, %s, %s) returning id""",
                    (cluster["name"].strip().lower(), cluster["category"], stage,
                     strength, confidence, target_date, target_date),
                ).fetchone()[0]
                new_ids.append(cid)
            _link_entities(conn, cid, eids)
            _add_evidence(conn, cid, anomaly_ids)
            _append_lifecycle(conn, cid, target_date, stage, strength, confidence)

    return {"new": len(new_ids), "updated": len(updated_ids),
            "entities": len(entities), "new_ids": new_ids}


def map_investments(conn, run_id: int | None, cluster_id: int) -> int:
    """Claude proposes tickers; only SEC-verified ones are stored (plan item 26)."""
    row = conn.execute(
        "select name, category from trend_clusters where id = %s", (cluster_id,)
    ).fetchone()
    if not row:
        return 0
    name, category = row
    entity_names = [r[0] for r in conn.execute(
        """select e.canonical_name from trend_cluster_entities tce
           join entities e on e.id = tce.entity_id where tce.cluster_id = %s""",
        (cluster_id,),
    ).fetchall()]

    user_text = (f"Trend: {name}\nCategory: {category}\n"
                 f"Entities involved: {', '.join(entity_names)}")
    result = llm.structured_call(conn, run_id, "investment_mapping",
                                 llm.MODEL_ANALYSIS, INVEST_SYSTEM, user_text,
                                 INVEST_SCHEMA, max_tokens=2000)
    stored = 0
    for c in result.get("companies", []):
        ticker = c["ticker"].strip().upper()
        verified = conn.execute(
            "select id from companies where ticker = %s", (ticker,)).fetchone()
        if not verified:
            log.info("dropping unverified ticker %s for trend %s", ticker, name)
            continue
        conn.execute(
            """insert into trend_companies
               (cluster_id, company_id, exposure, direction, confidence, material)
               values (%s, %s, %s, %s, %s, %s)
               on conflict (cluster_id, company_id) do update
               set exposure = excluded.exposure, direction = excluded.direction,
                   confidence = excluded.confidence, material = excluded.material""",
            (cluster_id, verified[0], c["exposure"], c["direction"],
             max(1, min(100, int(c["confidence"]))), c["material"]),
        )
        stored += 1
    return stored


def trend_report_lines(conn, top_n: int = 5) -> list[str]:
    """Top active trends for the daily summary (feeds plan item 30)."""
    rows = conn.execute(
        """select t.id, t.name, t.stage, t.strength, t.confidence,
                  coalesce(string_agg(distinct c.ticker, ',' order by c.ticker), '')
           from trend_clusters t
           left join trend_companies tc on tc.cluster_id = t.id
           left join companies c on c.id = tc.company_id
           where t.status = 'active'
           group by t.id order by t.strength desc, t.last_updated desc limit %s""",
        (top_n,),
    ).fetchall()
    if not rows:
        return []
    lines = ["Top trends:"]
    for _, name, stage, strength, confidence, tickers in rows:
        t = f" [{tickers}]" if tickers else ""
        lines.append(f"  📈 {name} — {stage}, strength {strength}, confidence {confidence}{t}")
    return lines


def run_trend_analysis(conn, config: dict, run_id: int | None,
                       target_date: date | None = None) -> tuple[dict, list[str]]:
    """Cluster today's anomalies, map investments for new trends, report."""
    stats = cluster_anomalies(conn, config, run_id, target_date)
    mapped = 0
    for cid in stats.get("new_ids", []):
        mapped += map_investments(conn, run_id, cid)
    stats["companies_mapped"] = mapped
    return stats, trend_report_lines(conn)
