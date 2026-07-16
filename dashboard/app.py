"""Personal dashboard (plan items 41-45). Single user, password-protected.

Run locally:  python -m dashboard.app   (http://127.0.0.1:5601)
Auth: DASHBOARD_PASSWORD in .env. Moves to the VPS with plan item 34.
"""

import os
from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from flask import (Flask, abort, g, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup

from pipeline import db

app = Flask(__name__)
app.secret_key = os.environ.get("DASHBOARD_SECRET") or os.environ.get("DASHBOARD_PASSWORD", "dev-secret")
# login hardening (plan item 73)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("VERCEL")),
)

STAGE_COLORS = {"emerging": "#4A6FA5", "accelerating": "#0E7C66",
                "peak": "#B77817", "declining": "#9AA7AB"}


def get_conn():
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def _close(exc):
    conn = g.pop("conn", None)
    if conn is not None:
        conn.close()


@app.before_request
def _guard():
    if request.endpoint in ("login", "static") or session.get("ok"):
        return None
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == os.environ.get("DASHBOARD_PASSWORD"):
            session["ok"] = True
            return redirect(url_for("index"))
        import time
        time.sleep(1.5)            # brute-force throttle (plan item 73)
        error = "Wrong password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def sparkline(values: list[float], width=130, height=30, color="currentColor",
              peak_date=None) -> Markup:
    if not values or max(values) <= 0:
        return Markup("")
    top = max(values)
    imax = values.index(top)
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = 2 + i * (width - 4) / max(1, n - 1)
        y = height - 3 - (v / top) * (height - 6)
        pts.append(f"{x:.1f},{y:.1f}")
    lx, ly = pts[-1].split(",")
    px, py = pts[imax].split(",")
    tip = f"peak {top:.0f}" + (f" on {peak_date}" if peak_date else "")
    return Markup(
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">'
        f'<title>{tip}</title>'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="1.6"/>'
        f'<circle cx="{px}" cy="{py}" r="2.6" fill="var(--warn)"/>'
        f'<circle cx="{lx}" cy="{ly}" r="2.4" fill="{color}"/></svg>')


def lifecycle_chart(lifecycle: list[dict], width=680, height=150) -> Markup:
    pts = [(e.get("date"), e.get("strength", 0), e.get("confidence", 0))
           for e in lifecycle if e.get("date")]
    if len(pts) < 2:
        return Markup('<p class="muted">Lifecycle chart appears after two or more days of history.</p>')
    n = len(pts)

    def line(idx, dash=""):
        coords = []
        for i, p in enumerate(pts):
            x = 40 + i * (width - 60) / (n - 1)
            y = height - 22 - (p[idx] / 100) * (height - 40)
            coords.append(f"{x:.1f},{y:.1f}")
        return (f'<polyline points="{" ".join(coords)}" fill="none" '
                f'stroke="var(--accent)" stroke-width="2" {dash}/>')

    labels = (f'<text x="6" y="{height-18}" class="axis">0</text>'
              f'<text x="6" y="16" class="axis">100</text>'
              f'<text x="40" y="{height-6}" class="axis">{pts[0][0]}</text>'
              f'<text x="{width-120}" y="{height-6}" class="axis">{pts[-1][0]}</text>')
    return Markup(
        f'<svg width="100%" viewBox="0 0 {width} {height}" role="img" class="chart">'
        f'<line x1="40" y1="{height-22}" x2="{width-20}" y2="{height-22}" stroke="var(--line)"/>'
        f'{line(1)}{line(2, "stroke-dasharray=\'4 4\' opacity=\'.55\'")}'
        f'{labels}</svg>'
        '<p class="muted small">solid = strength · dashed = confidence</p>')


def snippet(source: str, payload: dict) -> str:
    if source in ("reddit", "_synthetic"):
        return payload.get("title") or ""
    if source == "google_trends":
        rising = payload.get("rising") or []
        return "Rising: " + ", ".join(r.get("query", "") for r in rising[:6])
    if source == "tiktok":
        row = payload.get("row", {})
        return f"{row.get('name')} (rank {row.get('rank')}, {row.get('trend')})"
    if source == "sec_edgar":
        return f"{payload.get('company')} — {payload.get('form')}"
    if source == "gdelt":
        return f"News volume timeline ({len(payload.get('timeline', []))} days)"
    return str(payload)[:120]


TREND_LIST_SQL = """
    select t.id, t.name, t.category, t.stage, t.strength, t.confidence,
           t.first_detected, t.last_updated, t.watched,
           coalesce(string_agg(distinct c.ticker, ',' order by c.ticker), '') as tickers,
           count(distinct tce.entity_id) as n_entities
    from trend_clusters t
    left join trend_cluster_entities tce on tce.cluster_id = t.id
    left join trend_companies tc on tc.cluster_id = t.id
    left join companies c on c.id = tc.company_id
    where t.status = %(status)s
      and (%(q)s = '' or t.name ilike '%%' || %(q)s || '%%')
      and (%(category)s = '' or t.category = %(category)s)
      and (%(stage)s = '' or t.stage = %(stage)s)
      and t.strength >= %(min_strength)s
      and t.confidence >= %(min_confidence)s
      and (not %(watched_only)s or t.watched)
    group by t.id
    order by t.strength desc, t.last_updated desc
"""


def _list_params():
    return {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", ""),
        "stage": request.args.get("stage", ""),
        "min_strength": int(request.args.get("min_strength") or 0),
        "min_confidence": int(request.args.get("min_confidence") or 0),
        "watched_only": request.args.get("watched") == "1",
    }


def _liveness(conn):
    """Engine liveness for the home page (plan item 66)."""
    from datetime import datetime, timezone
    last = conn.execute(
        """select id, started_at, finished_at, status,
                  (select count(*) from run_collectors rc
                   where rc.run_id = runs.id and rc.status = 'ok')
           from runs order by id desc limit 1""").fetchone()
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=10, minute=30, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    hours = (nxt - now).total_seconds() / 3600
    anomalies_today = conn.execute(
        "select count(*) from anomalies where signal_date = current_date").fetchone()[0]
    return {
        "run_id": last[0] if last else None,
        "ran_at": last[1].strftime("%d.%m %H:%M UTC") if last else "never",
        "status": last[3] if last else "—",
        "ok_steps": last[4] if last else 0,
        "next_in_h": round(hours, 1),
        "anomalies_today": anomalies_today,
    }


SORT_ORDERS = {                     # plan item 71
    "strength": "t.strength desc, t.last_updated desc",
    "newest": "t.first_detected desc, t.strength desc",
    "confidence": "t.confidence desc, t.strength desc",
}


@app.route("/")
def index():
    conn = get_conn()
    params = _list_params() | {"status": "active"}
    sort = request.args.get("sort", "strength")
    sql = TREND_LIST_SQL.replace("order by t.strength desc, t.last_updated desc",
                                 "order by " + SORT_ORDERS.get(sort, SORT_ORDERS["strength"]))
    trends = conn.execute(sql, params).fetchall()
    categories = [r[0] for r in conn.execute(
        "select distinct category from trend_clusters where category is not null order by 1").fetchall()]
    stats = conn.execute("""
        select (select count(*) from trend_clusters where status='active'),
               (select count(*) from entities where status='active'),
               (select count(*) from raw_items),
               (select coalesce(sum(cost_usd),0) from api_costs
                where created_at >= date_trunc('month', now()))""").fetchone()
    return render_template("index.html", trends=trends, categories=categories,
                           stats=stats, args=request.args, live=_liveness(conn),
                           stage_colors=STAGE_COLORS, title="Active trends")


@app.route("/entities")
def entities():
    """Every tracked entity with recent momentum (plan item 67) —
    the thing to watch while trends are still forming."""
    conn = get_conn()
    q = request.args.get("q", "").strip()
    rows = conn.execute("""
        select e.id, e.canonical_name, e.category, e.first_seen,
               coalesce(sum(s.value) filter (where s.signal_date >= current_date - 13), 0) as m14,
               count(distinct s.source) as n_sources,
               coalesce(string_agg(distinct s.source, ',' order by s.source), '') as sources
        from entities e
        left join daily_signals s on s.entity_id = e.id and s.metric = 'mentions'
        where e.status = 'active' and (%(q)s = '' or e.canonical_name ilike '%%' || %(q)s || '%%')
        group by e.id
        order by m14 desc, e.first_seen desc limit 200""", {"q": q}).fetchall()

    since = date.today() - timedelta(days=29)
    days = [since + timedelta(days=i) for i in range(30)]
    ids = [r[0] for r in rows]
    series: dict[int, dict] = {}
    if ids:
        for eid, d, v in conn.execute(
            """select entity_id, signal_date, sum(value) from daily_signals
               where entity_id = any(%s) and metric = 'mentions' and signal_date >= %s
               group by 1, 2""", (ids, since)).fetchall():
            series.setdefault(eid, {})[d] = float(v)
    out = []
    for r in rows:
        vals = [series.get(r[0], {}).get(d, 0) for d in days]
        peak_date = days[vals.index(max(vals))] if max(vals) > 0 else None
        out.append({"id": r[0], "name": r[1], "category": r[2], "first_seen": r[3],
                    "m14": float(r[4]), "n_sources": r[5], "sources": r[6],
                    "spark": sparkline(vals, peak_date=peak_date)})
    return render_template("entities.html", entities=out, q=q,
                           window=f"{days[0].strftime('%d.%m')} – {days[-1].strftime('%d.%m')}",
                           title="Entities")


@app.route("/anomalies")
def anomalies():
    """The last 7 days of anomalies — watch the engine think (plan item 68)."""
    conn = get_conn()
    rows = conn.execute("""
        select a.signal_date, e.canonical_name, a.source, a.kind, a.score, a.details
        from anomalies a join entities e on e.id = a.entity_id
        where a.signal_date >= current_date - 7
        order by a.signal_date desc, a.score desc limit 200""").fetchall()
    return render_template("anomalies.html", rows=rows, title="Anomalies")


@app.route("/graveyard")
def graveyard():
    conn = get_conn()
    params = _list_params() | {"status": "dead"}
    trends = conn.execute(TREND_LIST_SQL, params).fetchall()
    return render_template("graveyard.html", trends=trends,
                           stage_colors=STAGE_COLORS, title="Graveyard")


@app.post("/trend/<int:tid>/watch")
def toggle_watch(tid):
    conn = get_conn()
    conn.execute("update trend_clusters set watched = not watched where id = %s", (tid,))
    return redirect(request.referrer or url_for("trend", tid=tid))


@app.route("/trend/<int:tid>")
def trend(tid):
    conn = get_conn()
    row = conn.execute(
        """select id, name, category, stage, strength, confidence, status,
                  first_detected, last_updated, watched, lifecycle
           from trend_clusters where id = %s""", (tid,)).fetchone()
    if not row:
        abort(404)
    t = dict(zip(["id", "name", "category", "stage", "strength", "confidence",
                  "status", "first_detected", "last_updated", "watched", "lifecycle"], row))

    since = date.today() - timedelta(days=60)
    entities = []
    for eid, name, first_seen in conn.execute(
        """select e.id, e.canonical_name, e.first_seen
           from trend_cluster_entities tce join entities e on e.id = tce.entity_id
           where tce.cluster_id = %s order by e.first_seen""", (tid,)).fetchall():
        by_source = {}
        for source, d, v in conn.execute(
            """select source, signal_date, value from daily_signals
               where entity_id = %s and metric = 'mentions' and signal_date >= %s
               order by signal_date""", (eid, since)).fetchall():
            by_source.setdefault(source, {})[d] = float(v)
        days = [since + timedelta(days=i) for i in range((date.today() - since).days + 1)]
        sources = [{"source": s, "total": sum(vals.values()),
                    "spark": sparkline([vals.get(d, 0) for d in days])}
                   for s, vals in sorted(by_source.items())]
        entities.append({"id": eid, "name": name, "first_seen": first_seen, "sources": sources})

    companies = conn.execute(
        """select c.ticker, c.name, tc.exposure, tc.direction, tc.confidence, tc.material
           from trend_companies tc join companies c on c.id = tc.company_id
           where tc.cluster_id = %s
           order by tc.material desc, tc.confidence desc""", (tid,)).fetchall()

    evidence = conn.execute(
        """select a.signal_date, e.canonical_name, a.source, a.kind, a.score, a.details
           from trend_evidence te
           join anomalies a on a.id = te.anomaly_id
           join entities e on e.id = a.entity_id
           where te.cluster_id = %s
           order by a.signal_date desc, a.score desc limit 40""", (tid,)).fetchall()

    raw = conn.execute(
        """select ri.source, ri.item_date, ri.payload
           from raw_item_entities rie
           join raw_items ri on ri.id = rie.raw_item_id
           where rie.entity_id in (select entity_id from trend_cluster_entities where cluster_id = %s)
           order by ri.collected_at desc limit 30""", (tid,)).fetchall()
    raw_items = [{"source": s, "date": d, "text": snippet(s, p),
                  "url": ("https://reddit.com" + p.get("permalink", "")) if s == "reddit" and p.get("permalink") else None}
                 for s, d, p in raw]

    return render_template("trend.html", t=t, entities=entities, companies=companies,
                           evidence=evidence, raw_items=raw_items,
                           chart=lifecycle_chart(t["lifecycle"] or []),
                           stage_colors=STAGE_COLORS, title=t["name"])


if __name__ == "__main__":
    if not os.environ.get("DASHBOARD_PASSWORD"):
        raise SystemExit("Set DASHBOARD_PASSWORD in .env first")
    app.run(host="127.0.0.1", port=5601, debug=False)
