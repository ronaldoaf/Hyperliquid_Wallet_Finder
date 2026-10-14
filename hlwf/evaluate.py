"""Stage C: copy one wallet with several delays and test the result.

The test period is out of sample: it starts at the split date, and nothing
after the split was used to pick the wallet. Fills before the split (up to
`insample_days`) are simulated too and reported for context.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import stats
from .api import InfoClient
from .copysim import HOUR_MS, position_events, prepare_fills, simulate
from .market import Market
from .wallet import equity_series, fetch_wallet, open_positions

DAY_MS = 86_400_000
BASE_FUNDING = 0.0000125  # Hyperliquid's hourly interest component (~11% a year, longs pay)


@dataclass
class Settings:
    split_ms: int
    end_ms: int
    insample_days: int = 90
    delays_min: tuple = (30, 60, 120, 240)
    main_delay_min: int = 30
    taker_bps: float = 4.5
    hip3_fee_mult: float = 2.0
    max_leverage: float = 20.0
    min_oos_days: int = 60
    min_opens: int = 10
    active_days: int = 30
    funding_min_share: float = 0.03
    q_max: float = 0.10
    skill_p_max: float = 0.10
    skill_alpha_t: float = 2.0
    robust_delays_min: tuple = (60, 120)
    extra: dict = field(default_factory=dict)


def slice_stats(daily: pd.DataFrame, start_ms: int, end_ms: int) -> dict:
    d = daily[(daily.index >= pd.Timestamp(start_ms, unit="ms").floor("D")) & (daily.index <= pd.Timestamp(end_ms, unit="ms"))]
    r = d["ret"].to_numpy(float)
    n = len(r)
    if n < 2:
        return {"days": n}
    t, p = stats.hac_t(r)
    conc = stats.concentration(d["pnl"].to_numpy(float), 5)
    return {
        "days": n, "pnl": float(d["pnl"].sum()), "price": float(d["price"].sum()), "fees": float(d["fees"].sum()),
        "funding": float(d["funding"].sum()), "ret_sum": float(r.sum()), "ret_ann": float(r.mean() * 365),
        "vol_ann": float(r.std(ddof=1) * np.sqrt(365)), "sharpe_ann": float(stats.sharpe(r) * np.sqrt(365)),
        "t": t, "p": p, "maxdd": stats.max_drawdown(r), "psr": stats.psr(r),
        "top5_share": conc["top_k_share"], "pnl_ex_top5": conc["ex_top_k"],
        "pos_days": float((r > 0).sum() / max((r != 0).sum(), 1)),
        "gross_avg": float(d["gross"].mean()), "lev_avg": float((d["gross"] / d["capital"]).replace([np.inf], np.nan).mean()),
        "lev_max": float((d["gross"] / d["capital"]).replace([np.inf], np.nan).max()),
    }


def style(res, start_ms: int) -> dict:
    """Holding times and long/short mix of the follower's book (hourly)."""
    he = res.hourly_expo
    if he.empty:
        return {}
    he = he[he.index >= start_ms]
    on = he.abs() > 1e-9
    entries = (on & ~on.shift(1, fill_value=False)).sum().sum()
    hours_on = on.sum().sum()
    long_share = float(he.clip(lower=0).sum().sum() / max(he.abs().sum().sum(), 1e-12))
    return {"opens": int(entries), "hold_days": float(hours_on / max(entries, 1) / 24),
            "long_share": long_share, "coins_traded": int(on.any().sum()),
            "time_in_market": float(on.any(axis=1).mean())}


def evaluate_wallet(addr: str, cfg: Settings, market: Market, client: InfoClient, data_dir: str = "data",
                    verbose: bool = False) -> tuple[dict, dict]:
    addr = addr.lower()
    want_from = cfg.split_ms - cfg.insample_days * DAY_MS
    rec = fetch_wallet(client, addr, want_from, data_dir)
    fills = prepare_fills(rec["fills"])
    events = position_events(fills)
    # a position held through the whole window has no fill in it: take it from the open positions
    for coin, szi in open_positions(rec.get("state")).items():
        if coin not in events:
            events[coin] = (np.array([], dtype=np.int64), np.array([]), float(szi))
    cov = max(int(rec["coverage"]), int(rec["asked_from"]))
    eval_start = cov
    oos_start = max(cfg.split_ms, eval_start)
    last_fill = int(fills["time"].max()) if len(fills) else None
    out = {"address": addr, "coverage_start": cov, "oos_start": oos_start, "n_fills": int(len(fills)),
           "last_fill": last_fill, "coins": sorted(events)}
    art: dict = {}
    oos_days = (cfg.end_ms - oos_start) / DAY_MS
    if not events:
        out.update(status="reprovada", reason="sem operações em perps no período")
        return out, art
    eq = equity_series(data_dir, addr)
    oracles, costs = {}, {}
    for coin in events:
        try:
            oracles[coin] = market.oracle(coin, eval_start - DAY_MS)
            costs[coin] = market.cost_bps(coin, cfg.taker_bps, cfg.hip3_fee_mult)
        except Exception as e:  # a coin without any candles cannot be copied or marked
            if verbose:
                print(f"  {coin}: no prices ({e})", flush=True)
    main = cfg.main_delay_min * 60_000
    # Real hourly funding (11 API calls per coin and 120 days) only where it can
    # matter: coins with a real share of the book in the test period. Elsewhere,
    # and before the test period, the flat base rate.
    probe = simulate(events, oracles, main, eval_start, cfg.end_ms, costs, None, eq, cfg.max_leverage)
    he = probe.hourly_expo
    share = he[he.index >= oos_start].abs().sum() if len(he) else pd.Series(dtype=float)
    share = share / max(share.sum(), 1e-12)
    funding = {}
    hours = np.arange((eval_start // HOUR_MS) * HOUR_MS, cfg.end_ms + HOUR_MS, HOUR_MS, dtype=np.int64)
    f0 = max(eval_start, oos_start - 7 * DAY_MS)
    for coin in oracles:
        base = pd.Series(BASE_FUNDING, index=hours)
        if share.get(coin, 0.0) >= cfg.funding_min_share:
            real = market.funding(coin, f0 - HOUR_MS, cfg.end_ms)
            real = real[real.index >= f0 - HOUR_MS]
            real.index = (real.index.to_numpy(np.int64) // HOUR_MS) * HOUR_MS
            real = real.groupby(level=0).last()
            base.loc[base.index >= f0] = real.reindex(base.index[base.index >= f0]).fillna(0.0).to_numpy()
        funding[coin] = base
    results = {}
    results["ref"] = simulate(events, oracles, 0, eval_start, cfg.end_ms, {}, funding, eq, cfg.max_leverage)
    for d in sorted(set(cfg.delays_min) | {cfg.main_delay_min}):
        results[d] = simulate(events, oracles, d * 60_000, eval_start, cfg.end_ms, costs, funding, eq,
                              cfg.max_leverage)
    for k, res in results.items():
        out[f"oos_{k}"] = slice_stats(res.daily, oos_start, cfg.end_ms)
        if oos_start - eval_start >= 14 * DAY_MS:
            out[f"is_{k}"] = slice_stats(res.daily, eval_start, oos_start - 1)
    r_main = results[cfg.main_delay_min]
    d = r_main.daily[r_main.daily.index >= pd.Timestamp(oos_start, unit="ms").floor("D")]
    # skill: timing (shift test) and alpha over BTC
    he = r_main.hourly_expo[r_main.hourly_expo.index >= oos_start]
    hr = r_main.hourly_ret[r_main.hourly_ret.index >= oos_start]
    out["shift"] = stats.shift_test(he, hr)
    btc = market.candles("BTC", "1d", eval_start - 3 * DAY_MS).set_index("t")["close"]
    btc_ret = btc / btc.shift(1) - 1
    btc_ret.index = pd.to_datetime(btc_ret.index, unit="ms")
    out["alpha"] = stats.alpha_beta(d["ret"].to_numpy(float), btc_ret.reindex(d.index).to_numpy(float))
    boot = stats.block_bootstrap(d["ret"].to_numpy(float), n_boot=2000, mean_block=5)
    if len(boot):
        lo, hi = np.quantile(boot * 365, [0.05, 0.95])
        out["ret_ann_ci90"] = [float(lo), float(hi)]
    out["style"] = style(r_main, oos_start)
    out["by_coin"] = r_main.by_coin.to_dict("records") if len(r_main.by_coin) else []
    pos_now = open_positions(rec.get("state"))
    acct = float(((rec.get("state") or {}).get("marginSummary") or {}).get("accountValue") or 0.0)
    out["account_value_now"] = acct
    out["open_positions"] = pos_now
    out["oos_days"] = float(oos_days)
    out["eff_delay_min"] = effective_delay(r_main, oos_start)
    art["daily"] = {k: v.daily for k, v in results.items()}
    art["by_coin"] = r_main.by_coin
    return out, art


def effective_delay(res, start_ms: int) -> float:
    tr = res.trades
    if tr is None or tr.empty:
        return float("nan")
    tr = tr[tr["t_signal"] >= start_ms]
    return float(((tr["t_exec"] - tr["t_signal"]) / 60_000).mean()) if len(tr) else float("nan")


def classify(s: dict, cfg: Settings, q: float | None) -> tuple[str, list[str]]:
    """Verdict and the list of criteria that failed (in Portuguese, for the report)."""
    main = s.get(f"oos_{cfg.main_delay_min}") or {}
    fails, soft = [], []
    if s.get("status") == "reprovada":
        return "reprovada", [s.get("reason", "")]
    if s.get("oos_days", 0) < cfg.min_oos_days or main.get("days", 0) < cfg.min_oos_days:
        return "dados insuficientes", [f"período fora da amostra curto ({s.get('oos_days', 0):.0f} dias; fills só desde "
                                       f"{pd.Timestamp(s['coverage_start'], unit='ms'):%d/%m/%Y})"]
    opens = (s.get("style") or {}).get("opens", 0)
    if opens < cfg.min_opens:
        return "dados insuficientes", [f"poucas entradas fora da amostra ({opens})"]
    if not main.get("ret_ann", 0) > 0:
        fails.append(f"cópia com {cfg.main_delay_min} min perde dinheiro fora da amostra")
    for d in cfg.robust_delays_min:
        x = s.get(f"oos_{d}") or {}
        if not x.get("ret_ann", 0) > 0:
            fails.append(f"com {d} min de atraso perde dinheiro")
    if q is None or not q <= cfg.q_max:
        fails.append(f"não significativo após correção para múltiplos testes (q = {q:.2f})" if q is not None
                     else "sem valor-q")
    sh = (s.get("shift") or {}).get("p")
    ta = (s.get("alpha") or {}).get("t_alpha")
    skill = (sh is not None and np.isfinite(sh) and sh <= cfg.skill_p_max) or (ta is not None and np.isfinite(ta) and ta >= cfg.skill_alpha_t)
    if not skill:
        fails.append("lucro explicado pela direção do mercado (sem timing nem alfa sobre o BTC)")
    if not main.get("pnl_ex_top5", 0) > 0:
        fails.append("lucro depende dos 5 melhores dias")
    last = s.get("last_fill")
    if last is None or (cfg.end_ms - last) > cfg.active_days * DAY_MS:
        soft.append("inativa nos últimos 30 dias")
    if not fails:
        return ("aprovada" if not soft else "aprovada (inativa)"), soft
    if main.get("p", 1) <= 0.05 and main.get("ret_ann", 0) > 0 and (s.get("oos_60") or {}).get("ret_ann", 0) > 0:
        return "promissora", fails + soft
    return "reprovada", fails + soft


def settings_dict(cfg: Settings) -> dict:
    return asdict(cfg)
