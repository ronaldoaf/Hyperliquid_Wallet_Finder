"""Stages A and B of the search: cheap filters over the whole leaderboard.

Stage A (leaderboard, one download): keep wallets with enough all-time
volume. By default nothing else is filtered here: the leaderboard's PnL,
ROI and account value are *current* numbers, so filtering on them would leak
the out-of-sample period into the selection (a wallet that blew up after
the split would be dropped before it could fail the test).

Stage B (one `portfolio` call per wallet, ~60 per minute): the weekly
perp PnL and account value history. Only the part *before* the split date
is used to score the wallet (t-statistic of weekly returns), so the
selection never sees the period in which the copy is tested.
"""
from __future__ import annotations

import os
import time

import numpy as np
import pandas as pd

from .api import InfoClient, fetch_leaderboard
from .store import load_gz, save_gz

DAY_MS = 86_400_000


# ------------------------------------------------------------------ stage A
def leaderboard_frame(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        x = {"address": r["ethAddress"].lower(), "account_value": float(r["accountValue"]),
             "display_name": r.get("displayName")}
        for k, v in r["windowPerformances"]:
            for m in ("pnl", "roi", "vlm"):
                x[f"{k}_{m}"] = float(v[m])
        out.append(x)
    return pd.DataFrame(out)


def load_leaderboard(data_dir: str = "data", max_age_h: float = 24.0, verbose: bool = True) -> pd.DataFrame:
    path = os.path.join(data_dir, "leaderboard.json.gz")
    cached = load_gz(path)
    if cached and time.time() - cached["fetched_at"] / 1000 < max_age_h * 3600:
        rows = cached["rows"]
    else:
        if verbose:
            print("downloading leaderboard ...", flush=True)
        rows = fetch_leaderboard()
        save_gz(path, {"fetched_at": int(time.time() * 1000), "rows": rows})
    return leaderboard_frame(rows)


def stage_a(lb: pd.DataFrame, min_volume: float = 50e6, leaky: bool = False,
            min_account_value: float = 10_000) -> pd.DataFrame:
    """Activity filter. `leaky=True` also requires positive all-time PnL and a
    current account value, which is faster but biases the out-of-sample test.

    The volume is today's all-time volume minus the last month's, an upper
    bound of the volume before a split at least a month back: every wallet
    that met the threshold at the split is kept, and the wallets that only
    crossed it in the last month (by trading after the split) are not let in.
    Those that crossed it earlier in the test period still are: a residual
    survivorship bias, stated in the report."""
    vol = lb["allTime_vlm"] - lb["month_vlm"].fillna(0.0).clip(lower=0.0) if "month_vlm" in lb else lb["allTime_vlm"]
    m = vol >= min_volume
    if leaky:
        m &= (lb["allTime_pnl"] > 0) & (lb["account_value"] >= min_account_value)
    return lb[m].sort_values("allTime_vlm", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------------ stage B
def portfolio_path(data_dir: str, addr: str) -> str:
    return os.path.join(data_dir, "portfolio", f"{addr.lower()}.json.gz")


def fetch_portfolios(c: InfoClient, addrs, data_dir: str = "data", max_age_h: float = 24.0,
                     verbose: bool = True) -> int:
    """Download (or refresh) the `portfolio` of every address; cached per wallet."""
    n_new = 0
    t0 = time.time()
    addrs = list(addrs)
    for i, a in enumerate(addrs):
        p = portfolio_path(data_dir, a)
        old = load_gz(p)
        if old and time.time() - old["fetched_at"] / 1000 < max_age_h * 3600:
            continue
        try:
            pf = dict(c.post({"type": "portfolio", "user": a}))
        except Exception as e:  # keep going: one bad wallet should not stop the scan
            if verbose:
                print(f"  {a}: {e}", flush=True)
            continue
        save_gz(p, {"fetched_at": int(time.time() * 1000), "portfolio": pf})
        n_new += 1
        if verbose and n_new % 100 == 0:
            rate = n_new / max(time.time() - t0, 1e-9) * 60
            print(f"  portfolio {i + 1}/{len(addrs)} ({rate:.0f}/min)", flush=True)
    return n_new


def history(pf: dict, key: str) -> pd.DataFrame:
    w = pf.get(key) or {}
    av = pd.DataFrame(w.get("accountValueHistory") or [], columns=["time", "account_value"])
    pn = pd.DataFrame(w.get("pnlHistory") or [], columns=["time", "pnl"])
    if av.empty or pn.empty:
        return pd.DataFrame(columns=["time", "account_value", "pnl"])
    df = av.merge(pn, on="time", how="inner").sort_values("time")
    df[["account_value", "pnl"]] = df[["account_value", "pnl"]].astype(float)
    return df.reset_index(drop=True)


def weekly_returns(h: pd.DataFrame, min_gap_days: float = 2.0, min_capital: float = 1_000.0) -> pd.DataFrame:
    """Returns between consecutive history points (about a week apart).

    The denominator is the capital at risk in the interval: the larger of the
    starting account value and the ending value net of the interval's PnL
    (that is, start plus deposits), so a deposit does not show up as a return.
    """
    if len(h) < 2:
        return pd.DataFrame(columns=["time", "ret", "dpnl", "capital"])
    keep, last = [], None
    for i, t in enumerate(h["time"].values):
        if last is None or t - last >= min_gap_days * DAY_MS or i == len(h) - 1:
            keep.append(i)
            last = t
    h = h.iloc[keep].reset_index(drop=True)
    dp = h["pnl"].diff()
    av0 = h["account_value"].shift(1)
    cap = np.maximum.reduce([av0.values, (h["account_value"] - dp).values, np.full(len(h), min_capital)])
    out = pd.DataFrame({"time": h["time"], "ret": dp / cap, "dpnl": dp, "capital": cap})
    # PnL with no capital behind it is an artifact (the first point of the API
    # history folds everything before May/2024 into one jump): drop it
    bogus = (av0 <= min_capital) & ((h["account_value"] - dp) <= min_capital) & (dp.abs() > min_capital)
    return out[~bogus].iloc[1:].reset_index(drop=True)


def with_total_equity(h: pd.DataFrame, pf: dict) -> pd.DataFrame:
    """Perp PnL history with the capital stage C uses: the larger of perp and
    total account value (as of each point). With unified margin the perp
    account value alone can be near zero while the trader has millions."""
    tot = history(pf, "allTime")
    if h.empty or tot.empty:
        return h
    t = tot.set_index("time")["account_value"].sort_index()
    i = np.searchsorted(t.index.to_numpy(np.int64), h["time"].to_numpy(np.int64), side="right") - 1
    asof = np.where(i >= 0, t.to_numpy(float)[np.maximum(i, 0)], np.nan)
    return h.assign(account_value=np.fmax(h["account_value"].to_numpy(float), asof))


def score_portfolio(pf: dict, split_ms: int, min_weeks: int = 12, min_active: int = 8, recent_weeks: int = 8,
                    min_account_value: float = 10_000, max_turnover: float = 20_000) -> dict:
    """In-sample score from the weekly perp history up to the split date."""
    h = history(pf, "perpAllTime")
    if h.empty:
        h = history(pf, "allTime")
    else:
        h = with_total_equity(h, pf)
    out = {"ok": False, "reason": ""}
    if h.empty:
        out["reason"] = "sem histórico"
        return out
    ins = h[h["time"] <= split_ms]
    if ins.empty:
        out["reason"] = "sem histórico antes do corte"
        return out
    # Volume over average equity, per year: market makers and HFT run at tens
    # of thousands of times their equity a year, swing traders at hundreds.
    # Equity and years come from before the split; the volume is all-time
    # minus the last month (the API gives no volume history), so it still
    # holds part of the test period: a partial leak, stated in the report.
    w_all = pf.get("perpAllTime") or pf.get("allTime") or {}
    w_month = pf.get("perpMonth") or pf.get("month") or {}
    vlm = max(float(w_all.get("vlm") or 0.0) - float(w_month.get("vlm") or 0.0), 0.0)
    mean_av = float(ins["account_value"].clip(lower=0).mean())
    years = max((ins["time"].iloc[-1] - ins["time"].iloc[0]) / DAY_MS / 365.0, 30 / 365.0)
    out["turnover"] = vlm / max(mean_av, 1.0) / years
    r = weekly_returns(ins)
    active = r["dpnl"].abs() > 1e-6
    if active.any():
        r = r.loc[active.idxmax():]  # from the first active week on
    n = len(r)
    n_active = int((r["dpnl"].abs() > 1e-6).sum())
    out.update({"n_weeks": n, "n_active": n_active, "first": int(ins["time"].iloc[0]) if len(ins) else None})
    if n < min_weeks or n_active < min_active:
        out["reason"] = f"histórico curto antes do corte ({n_active} semanas com PnL)"
        return out
    if out["turnover"] > max_turnover:
        out["reason"] = f"giro alto ({out['turnover']:,.0f}x o patrimônio por ano): perfil de bot de alta frequência"
        return out
    recent = r[r["time"] > split_ms - recent_weeks * 7 * DAY_MS]
    if not (recent["dpnl"].abs() > 1e-6).any():
        out["reason"] = "parado nas semanas antes do corte"
        return out
    av_split = float(ins["account_value"].iloc[-4:].max())
    out["av_split"] = av_split
    if av_split < min_account_value:
        out["reason"] = f"patrimônio pequeno no corte (US$ {av_split:,.0f})"
        return out
    x = r["ret"].clip(-1.0, 5.0).values  # a wiped-out week counts as -100%
    sd = x.std(ddof=1)
    out.update({
        "ok": True,
        "mean_w": float(x.mean()), "sd_w": float(sd),
        "t_w": float(x.mean() / sd * np.sqrt(n)) if sd > 0 else 0.0,
        "pos_frac": float((x > 0).mean()),
        "pnl_in": float(r["dpnl"].sum()),
        "maxdd_w": float(max_drawdown(np.cumsum(np.log1p(np.clip(x, -0.99, None))))),
    })
    return out


def max_drawdown(log_equity: np.ndarray) -> float:
    """Largest peak-to-trough fall, as a fraction, of an equity curve in logs."""
    if len(log_equity) == 0:
        return 0.0
    eq = np.r_[0.0, log_equity]
    return float(1 - np.exp((eq - np.maximum.accumulate(eq)).min()))


def stage_b(addrs, split_ms: int, data_dir: str = "data", **kw) -> pd.DataFrame:
    rows = []
    for a in addrs:
        d = load_gz(portfolio_path(data_dir, a))
        if not d:
            continue
        s = score_portfolio(d["portfolio"], split_ms, **kw)
        rows.append({"address": a, **s})
    df = pd.DataFrame(rows)
    if len(df) and "t_w" in df:
        df = df.sort_values("t_w", ascending=False, na_position="last")
    return df.reset_index(drop=True)
