"""Per-wallet data for stage C: fills, open positions and equity history.

The API serves only the most recent fills of a wallet. The docs speak of
10,000, but what it keeps varies from a few hours to years of history, so
nothing here counts fills: the cached record says over which span the fills
are known to be complete, from `coverage` to `complete_to`, and the cache
grows with every run. TWAP slices come from their own endpoint and are
merged into the fills.
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
HOUR_MS = 3_600_000
FORMAT = 2  # version of the cached record; older ones are downloaded again
PAGE = 2000  # fills per API response
PROBE_GAP_MS = DAY_MS  # first fill this close to the start: no need to ask whether older ones exist
FILLS = {"type": "userFillsByTime", "aggregateByTime": False}
TWAP = {"type": "userTwapSliceFillsByTime"}


def fill_key(f: dict):
    return (f["tid"], f["oid"], f["time"], f["sz"], f["px"])


def wallet_dir(data_dir: str, addr: str) -> str:
    return os.path.join(data_dir, "wallets", addr.lower())


def twap_fill(x: dict) -> dict:
    """A TWAP slice as an ordinary fill (the endpoint wraps each one)."""
    return {**x["fill"], "twapId": x.get("twapId")}


def served_before(c: InfoClient, addr: str, t: int) -> bool:
    """Does the API still serve any fill older than `t`? What it serves is the
    most recent part of the history, so if it does, every fill from `t` on is
    served too."""
    return bool(c.post({**FILLS, "user": addr, "startTime": 0, "endTime": t - 1}))


def coverage_start(c: InfoClient, addr: str, start_ms: int, first_ms: int | None) -> int:
    """From when the fills are complete, given the oldest fill the API serves
    after `start_ms` (None: it serves none)."""
    if first_ms is not None and first_ms - start_ms <= PROBE_GAP_MS:
        return first_ms
    if served_before(c, addr, start_ms):
        return start_ms
    return first_ms if first_ms is not None else int(time.time() * 1000)


def download_twap(c: InfoClient, addr: str, start_ms: int, end_ms: int) -> tuple[list[dict], int]:
    st: dict = {}
    rows = c.paginate_time({**TWAP, "user": addr}, start_ms, end_ms, key=lambda x: fill_key(x["fill"]),
                           time_of=lambda x: int(x["fill"]["time"]), page_limit=PAGE, status=st)
    return [twap_fill(x) for x in rows], st["complete_to"]


def download(c: InfoClient, addr: str, start_ms: int, now: int, need_from: int | None, old: dict | None) -> dict:
    """Fills from `start_ms` to now. If the coverage starts after `need_from`,
    the test window cannot be covered: stop at the first page."""
    info: dict = {}

    def hopeless(page):
        info["cov"] = coverage_start(c, addr, start_ms, int(page[0]["time"]))
        return need_from is not None and info["cov"] > need_from

    st: dict = {}
    new = c.paginate_time({**FILLS, "user": addr}, start_ms, now, key=fill_key, page_limit=PAGE,
                          first_page_stop=hopeless, status=st)
    cov = info["cov"] if "cov" in info else coverage_start(c, addr, start_ms, int(new[0]["time"]) if new else None)
    asked = start_ms
    fills = list(new)
    if old and old.get("fills"):
        of = old["fills"]
        if old.get("v") == FORMAT:
            o_from, o_to = int(old["coverage"]), int(old["complete_to"])
        else:  # earlier format: its end could be cut short and its start overstated
            o_from, o_to = max(int(old["coverage"]), int(of[0]["time"])), int(of[-1]["time"]) - 1
        if new and o_from < cov and int(new[0]["time"]) <= o_to and fill_key(new[0]) in {fill_key(f) for f in of}:
            cov = o_from  # the old cache reaches further back and joins the new download without a hole
            asked = min(asked, int(old["asked_from"]))
        fills = of + fills
    rec = {"v": FORMAT, "fills": fills, "coverage": int(cov), "complete_to": int(st["complete_to"]),
           "asked_from": int(asked), "fetched_at": now, "twap_to": int(start_ms)}
    if st["complete"]:
        tw, rec["twap_to"] = download_twap(c, addr, start_ms, now)
        rec["fills"] = fills + tw
    return rec


def extend(c: InfoClient, addr: str, rec: dict, now: int) -> dict:
    """Fetch what came after the cache. The first page must hold the last
    cached fill again; if the API no longer serves it, the fills between it
    and the first one served are lost and the coverage restarts there."""
    fills = [f for f in rec["fills"] if f.get("twapId") is None]
    if not fills:
        return download(c, addr, int(rec["asked_from"]), now, None, None)
    last = max(fills, key=lambda f: f["time"])
    st: dict = {}
    new = c.paginate_time({**FILLS, "user": addr}, int(last["time"]), now, key=fill_key, page_limit=PAGE, status=st)
    cov = int(rec["coverage"])
    if fill_key(last) not in {fill_key(f) for f in new}:
        cov = int(new[0]["time"]) if new else now
    tw, twap_to = download_twap(c, addr, max(int(rec.get("twap_to", cov)) - HOUR_MS, cov), now)
    return {**rec, "fills": rec["fills"] + new + tw, "coverage": cov, "complete_to": int(st["complete_to"]),
            "fetched_at": now, "twap_to": int(twap_to)}


def account_states(c: InfoClient, addr: str, old: dict | None) -> tuple[dict | None, dict, list[str]]:
    """Account state on the main dex and on every HIP-3 dex where the wallet
    holds something. A state that fails to download keeps the previous one
    (each state carries its own `time`)."""
    old = old or {}
    try:
        main = c.post({"type": "clearinghouseState", "user": addr})
    except Exception:
        main = old.get("state")
    dex_states, checked = {}, []
    try:
        dexes = c.perp_dexes()
    except Exception:
        dexes = []
    for dex in dexes:
        try:
            st = c.post({"type": "clearinghouseState", "user": addr, "dex": dex})
        except Exception:
            st = (old.get("dex_states") or {}).get(dex)
            if st is None and dex not in (old.get("dexes_checked") or []):
                continue
        checked.append(dex)
        if st and st.get("assetPositions"):
            dex_states[dex] = st
    return main, dex_states, checked


def fetch_wallet(c: InfoClient, addr: str, start_ms: int, data_dir: str = "data", max_age_h: float = 6.0,
                 need_from: int | None = None, need_to: int | None = None) -> dict:
    """Fills from `start_ms` (or the oldest the API still has) to now, TWAP
    slices included, plus the current account state on every perp dex.

    The fills are complete from `coverage` to `complete_to`. If `need_from`
    is given and the coverage starts after it, the download stops early (the
    test window cannot be covered). A cached record is reused while younger
    than `max_age_h` and complete up to `need_to`."""
    addr = addr.lower()
    path = os.path.join(wallet_dir(data_dir, addr), "fills.json.gz")
    rec = load_gz(path)
    now = int(time.time() * 1000)
    if rec and rec.get("v") == FORMAT and rec["asked_from"] <= start_ms:
        fresh = now - rec["fetched_at"] < max_age_h * 3600_000 and rec["complete_to"] >= (need_to or 0)
        if fresh or (need_from is not None and rec["coverage"] > need_from):  # coverage only moves forward
            return rec
        new = extend(c, addr, rec, now)
    else:
        new = download(c, addr, start_ms, now, need_from, rec)
    merged = {fill_key(f): f for f in new["fills"]}
    new["fills"] = sorted(merged.values(), key=lambda f: f["time"])
    new["state"], new["dex_states"], new["dexes_checked"] = account_states(c, addr, rec)
    save_gz(path, new)
    return new


def open_positions(state: dict | None) -> dict:
    out = {}
    for p in (state or {}).get("assetPositions", []):
        pos = p.get("position") or {}
        szi = float(pos.get("szi") or 0)
        if szi:
            out[pos["coin"]] = szi
    return out


def all_open_positions(rec: dict | None) -> dict:
    """Open positions on the main dex and on the HIP-3 dexes."""
    out = open_positions((rec or {}).get("state"))
    for st in ((rec or {}).get("dex_states") or {}).values():
        out.update(open_positions(st))
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


def perp_pnl_series(data_dir: str, addr: str) -> pd.Series:
    """Cumulative perp PnL of the wallet as the exchange reports it (the
    weekly points of `perpAllTime`; the shorter windows restart at zero, so
    they cannot be spliced in)."""
    d = load_gz(portfolio_path(data_dir, addr))
    rows = ((d or {}).get("portfolio", {}).get("perpAllTime") or {}).get("pnlHistory") or []
    return pd.Series({int(t): float(v) for t, v in rows}, dtype=float).sort_index()
