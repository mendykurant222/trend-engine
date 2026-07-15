# Trend Engine

Personal trend-discovery system: collectors pull raw signals from consumer
platforms, an anomaly detector finds unusual momentum, Claude clusters
anomalies into named trends and maps them to investable companies.

```
[Collectors] → [Raw DB] → [Signals] → [Anomaly Detector]
→ [Claude Analysis] → [Daily report + personal dashboard]
```

**Principles**
- Every source is an independent Collector behind one interface (`collectors/base.py`).
- Plain code collects; Claude analyzes.
- Everything is stored raw (`raw_items`) so it can always be reprocessed.
- Full evidence behind every trend is kept for drill-down.
- Every API call's cost is recorded (`api_costs`); budget tracked in the daily summary.

## Layout

| Path | Purpose |
|---|---|
| `collectors/` | one module per source: reddit, google_trends (SerpApi), amazon (Keepa), tiktok (Apify) |
| `pipeline/` | orchestrator, db layer; signals builder + entity resolution land in Phase 1 |
| `analysis/` | anomaly detection (Phase 2), Claude clustering (Phase 3) |
| `db/schema.sql` | full schema — entities/aliases/merges, raw_items, signals, anomalies, trends, companies, runs, api_costs, llm_calls |
| `reports/` | daily/weekly reports; run summary email + Telegram |
| `dashboard/` | personal web dashboard (Phase 6) |
| `data/ground_truth.yaml` | 20 labeled historical trends for calibration + backtesting |
| `scripts/` | backfill, SEC companies loader |
| `config/config.yaml` | sources, thresholds, categories, budget |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in keys
python -m pipeline.orchestrator --check     # validate config, no DB needed
python -m pipeline.orchestrator --init-db   # apply schema to Supabase
python -m scripts.load_companies            # SEC tickers → companies table
python -m pipeline.orchestrator             # full run
```

### Accounts needed (owner action)

| Service | For | Cost |
|---|---|---|
| Supabase (paid tier) | Postgres | $25/mo |
| Reddit app (script type) | Reddit API | free |
| SerpApi or DataForSEO | Google Trends (primary path — pytrends is dead) | from ~$50/mo |
| Keepa | Amazon best sellers + history | ~€49/mo |
| Apify | TikTok Creative Center | ~$5-40/mo |
| Resend | summary email | free tier |
| Telegram bot (@BotFather) | daily report channel | free |
| Anthropic API | Claude analysis (Phase 3) | target <$100/mo |

Budget ceiling for the whole system: **$300/mo** (`config.yaml → budget`).

## Scheduling

GitHub Actions (`.github/workflows/daily.yml`) runs daily at 10:30 UTC.
Actions is an API orchestrator only — never scrape directly from its IPs.
Repo secrets must mirror `.env`. Move to a VPS in Phase 5 when runs get heavy.

## North star

Flag real trends **after takeoff but before mainstream**, measured against
`data/ground_truth.yaml`. Calibration (Phase 2) and backtesting (Phase 7)
both score against that file.
