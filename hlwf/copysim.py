"""Simulate a follower who copies a wallet's perp positions with a delay.

The follower holds, at every moment, the position the wallet held `delay`
earlier (scaled to the follower's capital). Every change in the wallet's
position becomes a market order of the follower, sent `delay` after the
wallet's fill and executed at the open of the first price bar that starts
after that moment, paying taker fee plus spread/impact. Fills that land in
the same bar are netted, as a polling copier would do. The follower also
pays or receives hourly funding on what it holds.

Nothing here uses the wallet's own fill prices: a wallet that trades at
prices nobody else can get (off-market prints, illiquid books) gets no
credit for them.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
EPS = 1e-12


def market_kind(coin: str) -> str:
    if coin.startswith("@") or "/" in coin:
        return "spot"
    if coin.startswith("#"):
        return "outcome"
    if ":" in coin:
        return "hip3"
    return "perp"


def prepare_fills(rows: list[dict]) -> pd.DataFrame:
    """API fill dicts -> perp/HIP-3 fills with signed size, in API order."""
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["time", "coin", "q", "startPosition", "px", "tid", "dir"])
    df["kind"] = df["coin"].map(market_kind)
    df = df[df["kind"].isin(("perp", "hip3"))].copy()
    for c in ("px", "sz", "startPosition", "closedPnl", "fee"):
        if c in df:
            df[c] = df[c].astype(float)
    df["q"] = np.where(df["side"] == "B", 1.0, -1.0) * df["sz"]
    df["time"] = df["time"].astype(np.int64)
    return df.sort_values("time", kind="mergesort").reset_index(drop=True)


def fill_groups(g: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fills of one coin taken together per timestamp: (time, position before,
    position after, price of the first fill) of each group.

    The group starts at the one `startPosition` that is not another fill's
    end (the API order inside a millisecond is not guaranteed) and ends at
    that plus the group's size."""
    t = g["time"].to_numpy(np.int64)
    s = g["startPosition"].to_numpy(float)
    q = g["q"].to_numpy(float)
    px = g["px"].to_numpy(float)
    cut = np.r_[0, np.flatnonzero(t[1:] != t[:-1]) + 1, len(t)]
    times = t[cut[:-1]]
    start, end = np.empty(len(times)), np.empty(len(times))
    for k in range(len(times)):
        a, b = cut[k], cut[k + 1]
        if b - a == 1:
            start[k] = s[a]
        else:
            ends = set(np.round(s[a:b] + q[a:b], 9))
            cands = [x for x in s[a:b] if round(x, 9) not in ends]
            start[k] = cands[0] if cands else s[a]
        end[k] = start[k] + q[a:b].sum()
    return times, start, end, px[cut[:-1]]


def position_events(fills: pd.DataFrame) -> dict:
    """Per coin: (times, position after each time, position before the first).

    If a group of fills starts from a size different from where the previous
    one ended (a TWAP slice or any fill the API no longer serves), the jump
    is booked at that group's time: the follower learns about it late. See
    `position_jumps` for how much of the book moves that way.
    """
    out = {}
    for coin, g in fills.groupby("coin", sort=False):
        times, start, end, _ = fill_groups(g)  # after a gap the exchange's startPosition wins
        pos = np.where(np.abs(end) < 1e-10, 0.0, end)
        out[coin] = (times, pos, float(start[0]))
    return out


def position_jumps(fills: pd.DataFrame, rel_tol: float = 1e-7) -> pd.DataFrame:
    """Position changes with no visible fill: where a group of fills starts
    from a size different from where the previous group of the coin ended.
    Columns: coin, time, size (signed jump), px (price of the group)."""
    rows = []
    for coin, g in fills.groupby("coin", sort=False):
        times, start, end, px = fill_groups(g)
        jump = start[1:] - end[:-1]
        big = np.abs(jump) > rel_tol * np.maximum(1.0, np.abs(start[1:]))
        for t, j, p in zip(times[1:][big], jump[big], px[1:][big]):
            rows.append({"coin": coin, "time": int(t), "size": float(j), "px": float(p)})
    return pd.DataFrame(rows, columns=["coin", "time", "size", "px"])


@dataclass
class CopyResult:
    daily: pd.DataFrame                       # per UTC day: pnl, price, fees, funding, gross, capital, ret
    by_coin: pd.DataFrame                     # per coin totals
    trades: pd.DataFrame                      # follower orders
    hourly_expo: pd.DataFrame = field(default_factory=pd.DataFrame)  # hours x coins, exposure / capital
    hourly_ret: pd.DataFrame = field(default_factory=pd.DataFrame)   # hours x coins, return of the hour
    coin_daily: pd.DataFrame = field(default_factory=pd.DataFrame)   # days x coins, PnL after costs


def _wallet_pos_at(times: np.ndarray, pos: np.ndarray, pos0: float, T: np.ndarray) -> np.ndarray:
    if len(times) == 0:
        return np.full(len(T), pos0)
    i = np.searchsorted(times, T, side="right") - 1
    return np.where(i >= 0, pos[np.maximum(i, 0)], pos0)


def follower_orders(ev: tuple, oracle, delay_ms: int, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Orders of the follower in one coin: one per execution bar, netted.

    At `start_ms` the follower takes whatever position the wallet held
    `delay` earlier (a new follower copies open positions too)."""
    times, pos, pos0 = ev
    sel = (times + delay_ms > start_ms) & (times + delay_ms <= end_ms)
    T = np.r_[start_ms, times[sel] + delay_ms]
    target = np.r_[_wallet_pos_at(times, pos, pos0, np.array([start_ms - delay_ms]))[0], pos[sel]]
    px, tx, src = oracle.exec_price(T)
    ok = ~np.isnan(px) & (tx <= end_ms)
    T, target, px, tx, src = T[ok], target[ok], px[ok], tx[ok], src[ok]
    if len(T) == 0:
        return pd.DataFrame(columns=["t_signal", "t_exec", "target", "q", "px", "src"])
    # bars from different layers can come out of order: never trade back in time
    mono = np.maximum.accumulate(tx)
    fix = mono > tx
    if fix.any():
        p2, t2, s2 = oracle.exec_price(mono[fix])
        px[fix], tx[fix], src[fix] = p2, t2, s2
        tx = np.maximum.accumulate(tx)
    df = pd.DataFrame({"t_signal": T - delay_ms, "t_exec": tx, "target": target, "px": px, "src": src})
    df = df.groupby("t_exec", sort=True).agg(t_signal=("t_signal", "first"), target=("target", "last"),
                                             px=("px", "first"), src=("src", "first")).reset_index()
    df["q"] = df["target"].diff().fillna(df["target"])
    return df[df["q"].abs() > EPS].reset_index(drop=True)


def equity_on(equity: pd.Series | None, T: np.ndarray) -> np.ndarray:
    """Wallet account value at times T (ms): the last reading at or before T,
    as a follower would know it (no interpolation toward a later reading).
    Before the first reading, the first one."""
    if equity is None or equity.empty:
        return np.full(len(T), np.nan)
    e = equity.sort_index()
    i = np.searchsorted(e.index.to_numpy(np.int64), np.asarray(T, dtype=np.int64), side="right") - 1
    return e.to_numpy(float)[np.maximum(i, 0)]


def liquidation_days(he: pd.DataFrame, hr: pd.DataFrame, days: np.ndarray, maint: dict,
                     default_mm: float) -> np.ndarray:
    """Days on which the follower would be liquidated inside the day: from the
    day's opening capital, the hourly marked equity (exposure x hourly return,
    summed over coins) falls below the maintenance margin of the book (cross
    margin: sum of |exposure| x each coin's maintenance fraction). The daily
    close alone would miss a crash that recovers before midnight."""
    out = np.zeros(len(days), dtype=bool)
    if he.empty:
        return out
    E = he.fillna(0.0)
    R = hr.reindex(columns=E.columns).fillna(0.0)
    port = (E.to_numpy(float) * R.to_numpy(float)).sum(axis=1)
    mm = np.array([maint.get(c, default_mm) for c in E.columns])
    need = (np.abs(E.to_numpy(float)) * mm).sum(axis=1)
    di = np.searchsorted(days, E.index.to_numpy(np.int64), side="right") - 1
    for k in np.unique(di[di >= 0]):
        m = di == k
        equity = 1.0 + np.cumsum(port[m])  # after each hour, over the day's opening capital
        # the margin needed is that of the book held during the hour
        if (equity <= need[m]).any():
            out[k] = True
    return out


def simulate(events: dict, oracles: dict, delay_ms: int, start_ms: int, end_ms: int,
             cost_bps: dict | None = None, funding: dict | None = None, equity: pd.Series | None = None,
             max_leverage: float = 20.0, hourly: bool = True, maint: dict | None = None) -> CopyResult:
    """Copy every coin in `events` and mark the follower to market daily.

    Returns are PnL over the wallet's account value at the start of the day
    (a follower who sizes in proportion to the wallet's equity), with the
    capital floored at the day's peak gross exposure / `max_leverage` and at
    the initial margin each coin requires at its maximum leverage on the
    exchange (from `maint`), so that a stale or tiny equity reading cannot
    blow up a return, and a follower never runs more leverage than it may. The peak is taken at the
    start and end of the day and around every order of the follower, coin by
    coin (their sum bounds the true peak from above, so trades opened and
    closed inside one day count too). A follower cannot lose more than its
    capital: a day's return is at least -100%.
    """
    cost_bps = cost_bps or {}
    funding = funding or {}
    day0 = (start_ms // DAY_MS) * DAY_MS
    days = np.arange(day0, end_ms, DAY_MS, dtype=np.int64)  # days that start before the end
    hours = np.arange(((start_ms + HOUR_MS - 1) // HOUR_MS) * HOUR_MS, end_ms, HOUR_MS, dtype=np.int64)
    cols = {k: np.zeros(len(days)) for k in ("price", "fees", "funding", "gross", "gross_peak", "margin_peak")}
    per_coin, all_trades, H_expo, H_ret, H_pos, coin_pnl = [], [], {}, {}, {}, {}
    for coin, ev in events.items():
        o = oracles.get(coin)
        if o is None:
            continue
        tr = follower_orders(ev, o, delay_ms, start_ms, end_ms)
        close = o.daily_close()
        C = (np.array(close.reindex(days).ffill().bfill(), dtype=float) if len(close)  # writable copy
             else np.full(len(days), np.nan))
        if np.isnan(C).all():
            continue
        # the last day is cut at end_ms: mark it at the price then, not at the
        # daily candle's close (which may be later, when the candle was fetched)
        asof = getattr(o, "price_asof", None)
        if asof is not None and len(days) and end_ms - days[-1] < DAY_MS:
            p_end = asof(int(end_ms))
            if np.isfinite(p_end):
                C[-1] = p_end
        # positions at the end of each day and trades inside each day
        di = np.minimum(np.searchsorted(days, tr["t_exec"].to_numpy(np.int64), side="right") - 1, len(days) - 1)
        q = tr["q"].to_numpy(float)
        px = tr["px"].to_numpy(float)
        dq = np.bincount(di, weights=q, minlength=len(days))
        eod = np.cumsum(dq)
        sod = np.r_[0.0, eod[:-1]]
        prev_C = np.r_[C[0], C[:-1]]
        carry = sod * (C - prev_C)
        trade = np.bincount(di, weights=q * (C[di] - px), minlength=len(days))
        c_bps = cost_bps.get(coin, 0.0)
        fee = np.bincount(di, weights=np.abs(q) * px * c_bps / 1e4, minlength=len(days))
        price_pnl = carry + trade
        fund = np.zeros(len(days))
        if len(hours):
            hp, _, _ = o.exec_price(hours)
            hp = pd.Series(hp).ffill().bfill().to_numpy(float)
            ti = np.searchsorted(tr["t_exec"].to_numpy(np.int64), hours, side="right") - 1
            hpos = np.where(ti >= 0, np.cumsum(q)[np.maximum(ti, 0)], 0.0) if len(q) else np.zeros(len(hours))
            fr = funding.get(coin)
            if fr is not None and len(fr):
                rate = fr.copy()
                rate.index = (rate.index.to_numpy(np.int64) // HOUR_MS) * HOUR_MS
                rate = rate.groupby(level=0).last().reindex(hours).fillna(0.0).to_numpy(float)
                fpay = -hpos * hp * rate
                hd = np.searchsorted(days, hours, side="right") - 1
                fund = np.bincount(hd, weights=fpay, minlength=len(days))
            if hourly:
                H_pos[coin] = hpos * hp
                nxt = np.r_[hp[1:], np.nan]
                H_ret[coin] = nxt / hp - 1.0
        cols["price"] += price_pnl
        cols["fees"] -= fee
        cols["funding"] += fund
        coin_pnl[coin] = price_pnl - fee + fund
        cols["gross"] += np.abs(eod) * C
        if len(q):
            held = np.cumsum(q)
            peak = np.zeros(len(days))
            np.maximum.at(peak, di, np.maximum(np.abs(held), np.abs(held - q)) * px)
            cols["gross_peak"] += peak
            # initial margin at the coin's maximum leverage (2 x its maintenance fraction)
            cols["margin_peak"] += peak * 2.0 * (maint or {}).get(coin, 1.0 / (2.0 * max_leverage))
        n_open = int(((np.abs(np.r_[0.0, np.cumsum(q)[:-1]]) < EPS) & (np.abs(np.cumsum(q)) > EPS)).sum()) if len(q) else 0
        per_coin.append({"coin": coin, "price": float(price_pnl.sum()), "fees": float(-fee.sum()),
                         "funding": float(fund.sum()), "orders": len(tr), "opens": n_open,
                         "volume": float((np.abs(q) * px).sum()), "cost_bps": c_bps})
        if len(tr):
            tr = tr.assign(coin=coin)
            all_trades.append(tr)
    daily = pd.DataFrame(cols, index=pd.Index(days, name="day"))
    daily["pnl"] = daily["price"] + daily["fees"] + daily["funding"]
    eq = equity_on(equity, days.astype(float))
    g = daily["gross"].to_numpy()
    gross_prev = np.r_[g[0], g[:-1]]
    # never more leverage than the follower's cap, nor than each coin allows on the exchange
    cap = np.fmax(eq, np.maximum(np.maximum.reduce([gross_prev, g, daily["gross_peak"].to_numpy()]) / max_leverage,
                                 daily["margin_peak"].to_numpy()))
    cap = np.where(np.isnan(cap) | (cap < 1.0), np.nan, cap)
    daily["equity"] = eq
    daily["capital"] = cap
    ret = np.where(np.isnan(cap), 0.0, daily["pnl"] / np.where(np.isnan(cap), 1.0, cap))
    daily["ret"] = np.maximum(ret, -1.0)
    daily.index = pd.to_datetime(daily.index, unit="ms")
    by_coin = pd.DataFrame(per_coin)
    if len(by_coin):
        by_coin["pnl"] = by_coin["price"] + by_coin["fees"] + by_coin["funding"]
        by_coin = by_coin.sort_values("pnl", ascending=False).reset_index(drop=True)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()
    he = hr = pd.DataFrame()
    if hourly and H_pos:
        capd = pd.Series(cap, index=days).ffill().bfill()
        hcap = capd.reindex(hours, method="ffill").bfill().to_numpy(float)
        he = pd.DataFrame(H_pos, index=hours).div(np.where(np.isnan(hcap), np.inf, hcap), axis=0)
        hr = pd.DataFrame(H_ret, index=hours)
        liq = liquidation_days(he, hr, days, maint or {}, 1.0 / (2.0 * max_leverage))
        if liq.any():
            daily["liquidated"] = liq
            daily.loc[liq, "ret"] = -1.0  # the follower's capital is gone that day
    cd = pd.DataFrame(coin_pnl, index=pd.to_datetime(days, unit="ms")) if coin_pnl else pd.DataFrame()
    return CopyResult(daily=daily, by_coin=by_coin, trades=trades, hourly_expo=he, hourly_ret=hr, coin_daily=cd)
