"""BaseCollector — the uniform interface every data source implements (plan item 5).

Subclasses implement fetch() and yield raw items:
    {"external_id": str|None, "item_date": "YYYY-MM-DD"|None, "payload": dict}

The orchestrator handles storage, run bookkeeping, and cost recording.
"""

import abc
import logging
import time

import requests


class CollectorError(Exception):
    pass


class BaseCollector(abc.ABC):
    name: str = "base"
    #: minimum seconds between HTTP requests (rate limiting)
    min_interval_s: float = 1.0
    max_retries: int = 3

    def __init__(self, config: dict):
        self.config = config.get("sources", {}).get(self.name, {})
        self.log = logging.getLogger(f"collector.{self.name}")
        self._last_request_ts = 0.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "trend-engine/0.1 (personal research)"
        #: costs incurred during this fetch, appended as (operation, units, cost_usd)
        self.costs: list[tuple[str, int, float]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    def ready(self) -> str | None:
        """Return a reason string if the collector can't run (e.g. missing API key), else None."""
        return None

    @abc.abstractmethod
    def fetch(self) -> list[dict]:
        """Collect and return raw items. Raise CollectorError on unrecoverable failure."""

    def add_cost(self, operation: str, units: int = 1, cost_usd: float = 0.0) -> None:
        self.costs.append((operation, units, cost_usd))

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """HTTP with rate limiting + exponential-backoff retry."""
        kwargs.setdefault("timeout", 30)
        for attempt in range(1, self.max_retries + 1):
            wait = self.min_interval_s - (time.monotonic() - self._last_request_ts)
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()
            try:
                resp = self._session.request(method, url, **kwargs)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise CollectorError(f"HTTP {resp.status_code} from {url}")
                resp.raise_for_status()
                return resp
            except (requests.RequestException, CollectorError) as exc:
                if attempt == self.max_retries:
                    raise CollectorError(f"{self.name}: giving up after {attempt} attempts: {exc}") from exc
                backoff = 2 ** attempt
                self.log.warning("attempt %d failed (%s); retrying in %ds", attempt, exc, backoff)
                time.sleep(backoff)
        raise CollectorError("unreachable")
