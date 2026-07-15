-- Trend Engine schema (plan item 4)
-- Idempotent: safe to re-run.

create table if not exists entities (
    id              bigserial primary key,
    canonical_name  text not null unique,
    category        text,
    first_seen      date not null default current_date,
    status          text not null default 'active',   -- active | merged | ignored
    created_at      timestamptz not null default now()
);

create table if not exists entity_aliases (
    id          bigserial primary key,
    entity_id   bigint not null references entities(id),
    alias       text not null,
    source      text,                                  -- which collector/process produced it
    created_at  timestamptz not null default now(),
    unique (alias)
);

-- Merge/split history so a wrong merge can be undone retroactively (plan item 4)
create table if not exists entity_merges (
    id              bigserial primary key,
    from_entity_id  bigint not null references entities(id),
    into_entity_id  bigint not null references entities(id),
    reason          text,
    decided_by      text,                              -- 'claude' | 'manual'
    merged_at       timestamptz not null default now(),
    undone          boolean not null default false,
    undone_at       timestamptz
);

create table if not exists raw_items (
    id            bigserial primary key,
    source        text not null,                       -- collector name
    external_id   text,                                -- source-native id if any
    content_hash  text not null,                       -- sha256 for dedupe
    item_date     date,                                -- the date the item refers to
    collected_at  timestamptz not null default now(),
    payload       jsonb not null,
    unique (source, content_hash)
);
create index if not exists raw_items_source_date on raw_items (source, item_date);
create index if not exists raw_items_collected on raw_items (collected_at);

-- entity-extraction bookkeeping (Phase 1)
alter table raw_items add column if not exists entities_extracted_at timestamptz;

-- which entities appear in which raw items — the basis for mention counts
create table if not exists raw_item_entities (
    raw_item_id bigint not null references raw_items(id),
    entity_id   bigint not null references entities(id),
    unique (raw_item_id, entity_id)
);

create table if not exists daily_signals (
    id          bigserial primary key,
    entity_id   bigint not null references entities(id),
    source      text not null,
    signal_date date not null,
    metric      text not null,                         -- mentions | interest | rank | views | score_sum
    value       numeric not null,
    unique (entity_id, source, signal_date, metric)
);
create index if not exists daily_signals_date on daily_signals (signal_date);

create table if not exists anomalies (
    id          bigserial primary key,
    entity_id   bigint not null references entities(id),
    source      text not null,
    signal_date date not null,
    kind        text not null,                         -- surge | acceleration | new_entity
    score       numeric not null,
    details     jsonb not null default '{}'::jsonb,    -- baseline, window, raw numbers
    created_at  timestamptz not null default now()
);
create index if not exists anomalies_date on anomalies (signal_date);

create table if not exists trend_clusters (
    id             bigserial primary key,
    name           text not null,
    category       text,
    stage          text,                               -- emerging | accelerating | peak | declining
    strength       int,                                -- 1-100
    confidence     int,                                -- 1-100, separate from strength (plan item 25)
    status         text not null default 'active',     -- active | dead (graveyard)
    first_detected date not null,
    last_updated   date not null,
    lifecycle      jsonb not null default '[]'::jsonb, -- [{date, stage, strength, confidence}]
    created_at     timestamptz not null default now()
);

-- personal watchlist flag (plan item 43) — watched trends get boosted alerts
alter table trend_clusters add column if not exists watched boolean not null default false;

create table if not exists trend_cluster_entities (
    cluster_id bigint not null references trend_clusters(id),
    entity_id  bigint not null references entities(id),
    unique (cluster_id, entity_id)
);

-- Full evidence trail behind every trend, for drill-down (plan item 25)
create table if not exists trend_evidence (
    id         bigserial primary key,
    cluster_id bigint not null references trend_clusters(id),
    anomaly_id bigint not null references anomalies(id),
    added_at   timestamptz not null default now(),
    unique (cluster_id, anomaly_id)
);

-- Populated from SEC company_tickers.json — Claude only selects from this list (plan item 26)
create table if not exists companies (
    id       bigserial primary key,
    ticker   text not null unique,
    name     text not null,
    cik      text,
    exchange text
);

create table if not exists trend_companies (
    cluster_id  bigint not null references trend_clusters(id),
    company_id  bigint not null references companies(id),
    exposure    text,                                  -- manufacturer | retailer | supplier
    direction   text,                                  -- positive | negative
    confidence  int,
    material    boolean not null default false,        -- is the exposure material to the company
    created_at  timestamptz not null default now(),
    unique (cluster_id, company_id)
);

-- which trends were reported when — feeds the prediction-performance table
-- in the weekly report (plan item 32) and the full feedback loop (item 47)
create table if not exists trend_reports (
    id            bigserial primary key,
    cluster_id    bigint not null references trend_clusters(id),
    reported_date date not null,
    stage         text,
    strength      int,
    confidence    int,
    unique (cluster_id, reported_date)
);

create table if not exists runs (
    id          bigserial primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    status      text not null default 'running',       -- running | ok | partial | failed
    summary     jsonb not null default '{}'::jsonb
);

create table if not exists run_collectors (
    id              bigserial primary key,
    run_id          bigint not null references runs(id),
    collector       text not null,
    status          text not null,                     -- ok | failed | skipped
    items_seen      int not null default 0,
    items_stored    int not null default 0,
    duration_s      numeric,
    error           text
);

-- Cost tracking per provider per run (plan items 4, 6, 11, 28)
create table if not exists api_costs (
    id         bigserial primary key,
    run_id     bigint references runs(id),
    provider   text not null,                          -- serpapi | keepa | apify | anthropic | ...
    operation  text not null,
    units      int not null default 1,
    cost_usd   numeric not null default 0,
    created_at timestamptz not null default now()
);

-- Every Claude prompt+response saved for weekly quality review (plan item 29)
create table if not exists llm_calls (
    id            bigserial primary key,
    run_id        bigint references runs(id),
    model         text not null,
    purpose       text not null,                       -- entity_extraction | clustering | investment_mapping | ...
    prompt        jsonb not null,
    response      jsonb not null,
    input_tokens  int,
    output_tokens int,
    cost_usd      numeric,
    created_at    timestamptz not null default now()
);
