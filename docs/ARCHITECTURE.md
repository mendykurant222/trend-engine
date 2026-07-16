# Trend Engine — Architecture

Personal trend-discovery system. Primary use case: **spot product/business
opportunities weeks before mainstream**; public-market exposure is a secondary
lens. Single user (Mendy), not a product.

## Pipeline (runs daily, GitHub Actions, 10:30 UTC)

```
collect (7 sources) → resolve ASIN titles → extract entities (Haiku)
→ build daily signals → detect anomalies (statistics) → cluster into trends
(Sonnet) → map SEC-verified tickers → daily Telegram report + alerts
```

Everything is stored raw (`raw_items`, hash-deduped) and reprocessable. A
failing source never stops the run. Every API call's cost lands in
`api_costs`; every Claude prompt/response in `llm_calls`.

## Collectors (`collectors/`)

One class per source behind `BaseCollector` (retry, rate limit, cost hooks).
`needs_watchlist = True` collectors receive `watch_entities` from the
orchestrator (active-trend members ∪ recently anomalous ∪ newly discovered).

| source | mechanism | notes |
|---|---|---|
| reddit | official API (OAuth) | pending Reddit app approval |
| google_trends | SerpApi: per-category rising queries + per-entity 90d interest timelines | interest = separate signal source `google_trends_interest`; pytrends is dead, don't revisit |
| amazon | Keepa best-sellers (6 categories) | ASIN→title via `pipeline/amazon_titles.py`, cached in `asin_titles`; Keepa product param is `asin` (singular) |
| tiktok | Apify actor (top-100 hashtags) | also a calibration benchmark (`analysis/benchmark.py`) |
| sec_edgar | full-text search of filings | EDGAR has NO call transcripts (that's item 76) |
| gdelt | DOC 2.0 volume timelines | ~90d history per call = self-backfilling |
| youtube | Data API v3 search per entity | daily video-upload volume = creator attention |
| research | RSS (ARK, CB Insights, McKinsey) | Gartner blocks bots |

## Analysis

- `pipeline/entities.py` — Haiku batch extraction (structured JSON). Alias
  resolution: exact → pg_trgm fuzzy (≥0.75 auto-merge, 0.55–0.75 Haiku
  verdict) → new entity. Evergreen noise list skips extraction entirely.
- `pipeline/signals.py` — idempotent daily upserts; entity-linked sources
  (gdelt/edgar/youtube/trends-interest) bypass extraction.
- `analysis/anomalies.py` — surge (same-weekday Poisson baseline; normal
  approx above λ=30), acceleration (7d velocity ratio), new-entity
  (multi-source required). Cross-source ×2 within 14 days; sequence bonus
  ×1.25 when leading sources (tiktok/trends/youtube/reddit) fire before
  lagging (amazon/edgar/news/research). Source reliability weights in config.
- `analysis/trends.py` — two-stage: deterministic entity-overlap trend memory
  first (no LLM), then Sonnet clustering with the active-trends list.
  Strength + separate confidence; per-day lifecycle; full evidence trail.
  Investment mapping proposes tickers, stores only SEC-registry matches.

## Reports & alerts (`reports/`)

Daily Telegram (top-5 with dashboard deep links, market lens secondary),
alerts (cross-source ≥90, watchlist ≥60), weekly report with 14/30-day
prediction grading + precision %. `trend_reports` logs every reported trend.

## Ops

- **Dashboard**: Flask on Vercel (`api/index.py` wrapper; deploy with
  `npx vercel --prod --yes`). Password auth (`DASHBOARD_PASSWORD`).
- **Watchdog** (`.github/workflows/watchdog.yml`, every 6h): dead-man alert
  after 30h of silence + 120-day raw-payload retention.
- **CI** (`tests.yml`): compile + smoke_test + anomaly_test on every push.
- **Test suites** (`scripts/`): smoke_test (pipeline), anomaly_test
  (detectors), trends_test (live Sonnet, costs cents), report_test (sends
  Telegram demos), backtest (ground-truth replay over GDELT history),
  dashboard_demo seed|clean.
- **Secrets**: `.env` (gitignored — repo is PUBLIC) mirrored to GitHub
  Actions secrets. Never commit keys.
- **DB**: Neon Postgres; `db/schema.sql` is idempotent and auto-applied.

## Calibration truth

`data/ground_truth.yaml` — 32 labeled historical trends (takeoff/mainstream
dates, real vs hype). `scripts/backtest.py` replays them through the detector
via GDELT history → recall + lead-time. North star: flag real trends after
takeoff, before mainstream.
