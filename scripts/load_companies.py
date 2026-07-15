"""Populate the companies table from SEC's official company_tickers.json
(plan item 26). Claude only ever SELECTS from this table — it never invents
tickers.

Usage: python -m scripts.load_companies
"""

import sys

import requests
from dotenv import load_dotenv

from pipeline import db

SEC_URL = "https://www.sec.gov/files/company_tickers.json"


def main() -> int:
    load_dotenv()
    conn = db.connect()
    db.apply_schema(conn)
    resp = requests.get(
        SEC_URL,
        headers={"User-Agent": "trend-engine personal research mkurant@bkonect.com"},
        timeout=60,
    )
    resp.raise_for_status()
    rows = list(resp.json().values())
    # batched executemany in one transaction — row-by-row autocommit over a
    # remote pooler means ~10k network round-trips
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """insert into companies (ticker, name, cik)
               values (%s, %s, %s)
               on conflict (ticker) do update set name = excluded.name, cik = excluded.cik""",
            [(r["ticker"], r["title"], str(r["cik_str"])) for r in rows],
        )
    print(f"loaded/updated {len(rows)} companies from SEC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
