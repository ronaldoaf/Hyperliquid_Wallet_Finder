"""Per-wallet data for stage C: fills, open positions and equity history.

The API serves only the 10,000 most recent fills of a wallet. Fills are
cached and merged, so the local history grows with every run; `coverage`
says from when the fills are known to be complete.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

from .api import InfoClient
from .screen import portfolio_path
from .store import load_gz, save_gz

DAY_MS = 86_400_000
API_FILL_CAP = 10_000


def fill_key(f: dict):
    return (f["tid"], f["oid"], f["time"], f["sz"], f["px"])


def wallet_dir(data_dir: str, addr: str) -> str:
    return os.path.join(data_dir, "wallets", addr.lower())


def fetch_wallet(c: InfoClient, addr: str, start_ms: int, data_dir: str = "data", max_age_h: float = 6.0) -> dict:
    """Fills from `start_ms` (or the oldest the API still has) to now, plus
    the current clearinghouse state. Returns the cached record, where
    `coverage` is the time from which the fills are complete."""
    addr = addr.lower()
    path = os.path.join(wallet_dir(data_dir, addr), "fills.json.gz")
    rec = load_gz(path)
    now = int(time.time() * 1000)
    fresh = rec and now - rec["fetched_at"] < max_age_h * 3600_000
    if rec and fresh and rec["asked_from"] <= start_ms:
        return rec
    body = {"type": "userFillsByTime", "user": addr, "aggregateByTime": False}
    if rec and rec["asked_from"] <= start_ms:
        # the cache already reaches back far enough: only extend it forward
        q0 = rec["fills"][-1]["time"] if rec["fills"] else rec["fetched_at"]
        new = c.paginate_time(body, q0, now, key=fill_key, page_limit=2000)
        cov = rec["coverage"]
        if len(new) >= API_FILL_CAP - 500 and new and new[0]["time"] > q0 + DAY_MS:
            cov = new[0]["time"]  # more new fills than the API keeps: a hole after the cache
        asked = rec["asked_from"]
        old = rec["fills"]
    else:
        new = c.paginate_time(body, start_ms, now, key=fill_key, page_limit=2000)
        cov = start_ms if len(new) < API_FILL_CAP - 500 else (new[0]["time"] if new else now)
        asked = start_ms
        old = rec["fills"] if rec else []
        if rec and new and rec["fetched_at"] >= cov:
            cov = min(cov, rec["coverage"])  # the old cache joins the new download without a hole
    merged = {fill_key(f): f for f in old + new}
    fills = sorted(merged.values(), key=lambda f: f["time"])
    try:
        state = c.post({"type": "clearinghouseState", "user": addr})
    except Exception:
        state = rec.get("state") if rec else None
    rec = {"fills": fills, "coverage": int(cov), "asked_from": int(asked), "fetched_at": now, "state": state}
    save_gz(path, rec)
    return rec


def open_positions(state: dict | None) -> dict:
    out = {}
    for p in (state or {}).get("assetPositions", []):
        pos = p.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if szi:
            out[pos["coin"]] = szi
    return out


def equity_series(data_dir: str, addr: str) -> pd.Series:
    """Account value over time: the larger of total and perp equity, from the
    weekly all-time history refined by the month/week/day windows of the
    `portfolio` endpoint.

    Total equity is the right base for a follower who sizes in proportion
    to the trader (with unified margin the perps are backed by spot USDC and
    the perp account value alone can be near zero). A trader who also holds
    spot tokens gets a larger base and smaller copy returns: conservative.
    """
    d = load_gz(portfolio_path(data_dir, addr))
    if not d:
        return pd.Series(dtype=float)
    pf = d["portfolio"]

    def collect(keys):
        pts = {}
        for key in keys:
            for t, v in (pf.get(key) or {}).get("accountValueHistory") or []:
                pts[int(t)] = float(v)
        return pd.Series(pts, dtype=float).sort_index()

    tot = collect(("allTime", "month", "week", "day"))
    perp = collect(("perpAllTime", "perpMonth", "perpWeek", "perpDay"))
    if tot.empty:
        return perp
    if perp.empty:
        return tot
    idx = tot.index.union(perp.index)
    x = idx.to_numpy(float)
    a = np.interp(x, tot.index.to_numpy(float), tot.to_numpy(float))
    b = np.interp(x, perp.index.to_numpy(float), perp.to_numpy(float))
    return pd.Series(np.maximum(a, b), index=idx)
