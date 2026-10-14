"""The full search: leaderboard -> weekly history -> delayed copy, tested
out of sample, with the false discovery rate controlled across wallets."""
from __future__ import annotations

import json
import os
import time
import traceback

import numpy as np
import pandas as pd

from . import stats
from .api import InfoClient
from .evaluate import Settings, classify, evaluate_wallet, settings_dict
from .market import Market
from .screen import fetch_portfolios, load_leaderboard, portfolio_path, stage_a, stage_b
from .store import load_json, save_json

DAY_MS = 86_400_000


def run_dir(data_dir: str, tag: str) -> str:
    return os.path.join(data_dir, "results", tag)


def save_daily(path: str, art: dict) -> None:
    daily = art.get("daily") or {}
    cols = {}
    for k, d in daily.items():
        cols[f"ret_{k}"] = d["ret"]
        cols[f"pnl_{k}"] = d["pnl"]
    if cols:
        df = pd.DataFrame(cols)
        main = daily.get(30)
        if main is not None:
            df["gross_30"] = main["gross"]
            df["capital_30"] = main["capital"]
        df.to_csv(path, compression="gzip")


def load_daily(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    return pd.read_csv(path, index_col=0, parse_dates=True)


def evaluate_many(addrs, cfg: Settings, data_dir: str, client: InfoClient, tag: str, verbose: bool = True,
                  reuse: bool = True) -> list[dict]:
    d = run_dir(data_dir, tag)
    os.makedirs(d, exist_ok=True)
    market = Market(client, data_dir, verbose=False)
    out = []
    t0 = time.time()
    for i, a in enumerate(addrs):
        jp = os.path.join(d, f"{a}.json")
        if reuse and os.path.exists(jp):
            out.append(load_json(jp))
            continue
        try:
            s, art = evaluate_wallet(a, cfg, market, client, data_dir)
            save_daily(os.path.join(d, f"{a}.csv.gz"), art)
        except Exception as e:  # one broken wallet must not stop the scan
            s = {"address": a, "status": "erro", "reason": f"{type(e).__name__}: {e}",
                 "trace": traceback.format_exc()[-2000:]}
        save_json(jp, s)
        out.append(s)
        if verbose:
            m = s.get(f"oos_{cfg.main_delay_min}") or {}
            print(f"  [{i + 1}/{len(addrs)} {(time.time() - t0) / 60:.0f}min] {a} "
                  f"days={m.get('days', 0)} ret_ann={m.get('ret_ann', float('nan')):+.2f} "
                  f"t={m.get('t', float('nan')):.2f} {s.get('status', '')}", flush=True)
    return out


def finalize(results: list[dict], cfg: Settings) -> pd.DataFrame:
    """Benjamini-Hochberg across every wallet sent to stage C (a wallet that
    could not be tested counts as p = 1), then the verdict of each."""
    k = cfg.main_delay_min
    p = []
    for s in results:
        m = s.get(f"oos_{k}") or {}
        enough = s.get("oos_days", 0) >= cfg.min_oos_days and m.get("days", 0) >= cfg.min_oos_days
        p.append(m.get("p", 1.0) if enough and np.isfinite(m.get("p", np.nan)) else 1.0)
    q = stats.bh_qvalues(p) if p else np.array([])
    rows = []
    for s, pi, qi in zip(results, p, q):
        if s.get("status") == "erro":
            verdict, why = "erro", [s.get("reason", "")]
        else:
            verdict, why = classify(s, cfg, float(qi))
        s["p_used"], s["q"], s["verdict"], s["why"] = float(pi), float(qi), verdict, why
        rows.append(flat_row(s, cfg))
    return pd.DataFrame(rows)


def annotate_roles(client: InfoClient, results: list[dict], verdicts=("aprovada", "aprovada (inativa)", "promissora")):
    """Vault or sub-account? (A vault can be joined directly, with no copy delay.)"""
    for s in results:
        if s.get("verdict") in verdicts and "role" not in s:
            try:
                r = client.post({"type": "userRole", "user": s["address"]})
                s["role"] = r.get("role")
                if isinstance(r.get("data"), dict) and r["data"].get("master"):
                    s["master"] = r["data"]["master"]
            except Exception:
                pass


def flat_row(s: dict, cfg: Settings) -> dict:
    k = cfg.main_delay_min
    m = s.get(f"oos_{k}") or {}
    st = s.get("style") or {}
    bc = s.get("by_coin") or []
    row = {
        "address": s["address"], "verdict": s.get("verdict"), "why": "; ".join(s.get("why") or []),
        "oos_days": round(s.get("oos_days", 0), 1), "ret_ann_30": m.get("ret_ann"), "sharpe_30": m.get("sharpe_ann"),
        "t_30": m.get("t"), "p_30": s.get("p_used"), "q": s.get("q"), "maxdd_30": m.get("maxdd"),
        "pnl_30": m.get("pnl"), "fees_30": m.get("fees"), "funding_30": m.get("funding"),
        "lev_avg": m.get("lev_avg"), "pnl_ex_top5": m.get("pnl_ex_top5"),
        "shift_p": (s.get("shift") or {}).get("p"), "alpha_t": (s.get("alpha") or {}).get("t_alpha"),
        "beta_btc": (s.get("alpha") or {}).get("beta"),
        "opens": st.get("opens"), "hold_days": st.get("hold_days"), "long_share": st.get("long_share"),
        "coins": st.get("coins_traded"), "top_coins": ", ".join(x["coin"] for x in bc[:4]),
        "account_value_now": s.get("account_value_now"),
        "last_fill": pd.Timestamp(s["last_fill"], unit="ms").strftime("%Y-%m-%d") if s.get("last_fill") else None,
        "eff_delay_min": s.get("eff_delay_min"),
    }
    for d in ("ref", 60, 120, 240):
        row[f"ret_ann_{d}"] = (s.get(f"oos_{d}") or {}).get("ret_ann")
    ins = s.get(f"is_{k}") or {}
    row["is_ret_ann_30"] = ins.get("ret_ann")
    row["is_t_30"] = ins.get("t")
    return row


def run_scan(data_dir: str = "data", out_root: str = "reports", split_days: int = 120, top: int = 150,
             min_volume: float = 50e6, leaky: bool = False, budget: int = 1100, fetch_missing: bool = False,
             cfg_kw: dict | None = None, verbose: bool = True) -> str:
    from .report import write_scan_report

    now = int(time.time() * 1000)
    end = now
    split = ((now - split_days * DAY_MS) // DAY_MS) * DAY_MS  # a UTC midnight
    tag = pd.Timestamp(split, unit="ms").strftime("corte_%Y-%m-%d")
    client = InfoClient(budget=budget)
    lb = load_leaderboard(data_dir)
    a = stage_a(lb, min_volume=min_volume, leaky=leaky)
    if fetch_missing:
        fetch_portfolios(client, a["address"], data_dir, max_age_h=24 * 365)
    universe = [x for x in a["address"] if os.path.exists(portfolio_path(data_dir, x))]
    if verbose:
        print(f"stage A: {len(a)} wallets; with weekly history: {len(universe)}", flush=True)
    b = stage_b(universe, split, data_dir)
    ok = b[b["ok"] & (b["t_w"] > 0)] if len(b) else b
    sel = ok.head(top)
    if verbose:
        print(f"stage B: {int(b['ok'].sum())} scored, {len(ok)} with positive in-sample t; testing {len(sel)}",
              flush=True)
    cfg = Settings(split_ms=split, end_ms=end, **(cfg_kw or {}))
    results = evaluate_many(list(sel["address"]), cfg, data_dir, client, tag, verbose=verbose)
    table = finalize(results, cfg)
    annotate_roles(client, results)
    funnel = {"leaderboard": len(lb), "stage_a": len(a), "stage_b_universe": len(universe),
              "stage_b_scored": int(b["ok"].sum()) if len(b) else 0, "stage_b_positive": len(ok),
              "stage_c": len(sel), "min_volume": min_volume, "leaky": leaky, "top": top}
    path = write_scan_report(out_root, data_dir, tag, cfg, funnel, b, table, results)
    save_json(os.path.join(run_dir(data_dir, tag), "_meta.json"),
              {"settings": settings_dict(cfg), "funnel": funnel, "generated_at": now})
    return path


def run_wallets(addrs, data_dir: str = "data", out_root: str = "reports", split_days: int = 120,
                budget: int = 1100, cfg_kw: dict | None = None, split_ms: int | None = None,
                tag: str | None = None) -> tuple[pd.DataFrame, list[dict], Settings]:
    """Evaluate specific wallets (no selection step: each is a single test)."""
    now = int(time.time() * 1000)
    split = split_ms or ((now - split_days * DAY_MS) // DAY_MS) * DAY_MS
    client = InfoClient(budget=budget)
    addrs = [x.lower() for x in addrs]
    fetch_portfolios(client, addrs, data_dir, max_age_h=6)
    cfg = Settings(split_ms=split, end_ms=now, **(cfg_kw or {}))
    tag = tag or pd.Timestamp(split, unit="ms").strftime("avulsas_%Y-%m-%d")
    results = evaluate_many(addrs, cfg, data_dir, client, tag, verbose=True, reuse=False)
    table = finalize(results, cfg)
    annotate_roles(client, results)
    return table, results, cfg


def load_selection(path: str) -> dict:
    with open(path) as f:
        return json.load(f)
