"""Client for the public Hyperliquid Info API and the stats leaderboard.

Read-only, no keys. The Info API is rate limited per IP by request *weight*
(1200 per minute): most calls weigh 20, a few weigh 2 or 60, and the
list endpoints add 1 per 20 items returned (1 per 60 for candles). The
client keeps a one-minute window of the weight it spent and waits before a
call that would go over the budget.
"""
from __future__ import annotations

import collections
import time
from typing import Any, Callable

import requests

API_URL = "https://api.hyperliquid.xyz/info"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

LIGHT = {"l2Book", "allMids", "clearinghouseState", "orderStatus", "spotClearinghouseState", "exchangeStatus"}
HEAVY = {"userRole": 60}
PER_20 = {"userFills", "userFillsByTime", "fundingHistory", "userFunding", "historicalOrders",
          "userTwapSliceFills", "userTwapSliceFillsByTime", "userNonFundingLedgerUpdates", "recentTrades"}
PER_60 = {"candleSnapshot"}


def base_weight(kind: str) -> int:
    if kind in LIGHT:
        return 2
    return HEAVY.get(kind, 20)


def extra_weight(kind: str, n_items: int) -> int:
    if kind in PER_20:
        return n_items // 20
    if kind in PER_60:
        return n_items // 60
    return 0


class InfoClient:
    def __init__(self, url: str = API_URL, budget: int = 1000, timeout: float = 60.0, verbose: bool = False):
        self.url = url
        self.budget = budget  # weight per rolling minute (the hard limit is 1200)
        self.timeout = timeout
        self.verbose = verbose
        self.spent: collections.deque = collections.deque()  # (time, weight)
        self.session = requests.Session()
        self.calls = 0

    def _window(self) -> int:
        now = time.monotonic()
        while self.spent and now - self.spent[0][0] > 60.0:
            self.spent.popleft()
        return sum(w for _, w in self.spent)

    def _wait_for(self, w: int) -> None:
        while self._window() + w > self.budget:
            oldest = self.spent[0][0]
            time.sleep(max(0.05, 60.0 - (time.monotonic() - oldest) + 0.05))

    def post(self, body: dict, retries: int = 8) -> Any:
        kind = body.get("type", "")
        w0 = base_weight(kind)
        delay = 5.0
        for attempt in range(retries):
            self._wait_for(w0)
            self.spent.append((time.monotonic(), w0))
            try:
                r = self.session.post(self.url, json=body, timeout=self.timeout)
                if r.status_code == 429:
                    # someone else on this IP, or the estimate was short: fill the window
                    self.spent.append((time.monotonic(), self.budget))
                if r.status_code == 429 or r.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                out = r.json()
                self.calls += 1
                n = len(out) if isinstance(out, list) else 0
                ew = extra_weight(kind, n)
                if ew:
                    self.spent.append((time.monotonic(), ew))
                return out
            except (requests.RequestException, ValueError):
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

    def paginate_time(self, body: dict, start_ms: int, end_ms: int | None = None,
                      key: Callable[[dict], Any] = lambda x: (x.get("time"), x.get("hash"), str(x)),
                      time_of: Callable[[dict], int] = lambda x: int(x["time"]),
                      page_limit: int = 500, max_pages: int | None = None) -> list[dict]:
        """Walk forward over an endpoint that takes startTime/endTime and returns
        the oldest `page_limit` rows of the window. The next page starts at the
        last timestamp seen (rows sharing it across pages are deduplicated)."""
        end_ms = end_ms or int(time.time() * 1000)
        seen: set = set()
        out: list[dict] = []
        cur, pages = start_ms, 0
        while cur <= end_ms:
            page = self.post({**body, "startTime": cur, "endTime": end_ms})
            pages += 1
            if not page:
                break
            new = 0
            for item in page:
                k = key(item)
                if k not in seen:
                    seen.add(k)
                    out.append(item)
                    new += 1
            last_t = max(time_of(x) for x in page)
            if len(page) < page_limit or (max_pages and pages >= max_pages):
                break
            cur = last_t + 1 if new == 0 else last_t
        out.sort(key=time_of)
        return out


def fetch_leaderboard(timeout: float = 300.0) -> list[dict]:
    r = requests.get(LEADERBOARD_URL, timeout=timeout)
    r.raise_for_status()
    return r.json()["leaderboardRows"]
