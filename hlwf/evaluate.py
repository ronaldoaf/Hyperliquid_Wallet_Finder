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
from .copysim import HOUR_MS, _wallet_pos_at, position_events, position_jumps, prepare_fills, simulate
from .market import Market
from .wallet import all_open_positions, equity_series, fetch_wallet, perp_pnl_series

DAY_MS = 86_400_000
BASE_FUNDING = 0.0000125  # Hyperliquid's hourly interest component (~11% a year, longs pay)


@dataclass
class Settings:
    split_ms: int
    end_ms: int
    insample_days: int = 30
    delays_min: tuple = (30, 60, 120, 240)
    main_delay_min: int = 30
    taker_bps: float = 4.5
    hip3_fee_mult: float = 2.0
    max_leverage: float = 20.0
    min_oos_days: int = 60
    min_opens: int = 10
    min_active_days: int = 40
    inactive_after_days: int = 30
    funding_min_share: float = 0.05
    q_max: float = 0.10
    skill_p_max: float = 0.10
    skill_alpha_t: float = 2.0
    robust_delays_min: tuple = (60, 120)
    insample_screen: bool = True
    max_jump_share: float = 0.02   # position moved without visible fills, over the notional traded
    max_recon_share: float = 0.10  # rebuilt end positions vs the account, over the book's notional
    extra: dict = field(default_factory=dict)


def follower_path(ret) -> np.ndarray:
    """Daily returns of a follower who starts on the first day: a day that
    takes all of its capital (-100%) ends the copy, and the days after it are 0."""
    r = np.maximum(np.asarray(ret, float), -1.0)
    hit = np.flatnonzero(r <= -1.0)
    if len(hit):
        r = r.copy()
        r[hit[0] + 1:] = 0.0
    return r


def slice_stats(daily: pd.DataFrame, start_ms: int, end_ms: int) -> dict:
    d = daily[(daily.index >= pd.Timestamp(start_ms, unit="ms").floor("D")) & (daily.index <= pd.Timestamp(end_ms, unit="ms"))]
    r = follower_path(d["ret"].to_numpy(float))
    n = len(r)
    if n < 2:
        return {"days": n}
    t, p = stats.hac_t(r)
    conc = stats.concentration(d["pnl"].to_numpy(float), 5)
    active = int(((d["gross"] > 0) | (d["pnl"].abs() > 0)).sum())
    return {
        "days": n, "active_days": active, "pnl": float(d["pnl"].sum()), "price": float(d["price"].sum()), "fees": float(d["fees"].sum()),
        "funding": float(d["funding"].sum()), "ret_sum": float(r.sum()), "ret_ann": float(r.mean() * 365),
        "ret_comp": float(np.prod(1.0 + r) - 1.0), "ruined": bool((r <= -1.0).any()),
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


def jump_share(fills: pd.DataFrame, start_ms: int, end_ms: int) -> float:
    """Notional of the position changes with no visible fill, over the
    notional of the fills, inside [start_ms, end_ms]."""
    f = fills[(fills["time"] >= start_ms) & (fills["time"] <= end_ms)]
    j = position_jumps(fills)
    j = j[(j["time"] >= start_ms) & (j["time"] <= end_ms)]
    jumped = float((j["size"].abs() * j["px"]).sum())
    return jumped / max(float((f["q"].abs() * f["px"]).sum()), 1e-9) if jumped else 0.0


def reconcile(events: dict, rec: dict, fills: pd.DataFrame) -> dict:
    """Positions rebuilt from the fills against the account state of each dex,
    at the time of that state. Coins of a dex whose state is unknown are skipped."""
    main = rec.get("state") or {}
    states = {"": main} if main else {}
    states.update(rec.get("dex_states") or {})
    checked = set(rec.get("dexes_checked") or []) | ({""} if main else set())
    marks, actual = {}, {}
    for st in states.values():
        for p in st.get("assetPositions", []):
            x = p.get("position") or {}
            szi = float(x.get("szi") or 0)
            if szi:
                actual[x["coin"]] = szi
                marks[x["coin"]] = abs(float(x.get("positionValue") or 0)) / abs(szi)
    last_px = fills.groupby("coin")["px"].last().to_dict() if len(fills) else {}
    gap, book, worst = 0.0, 0.0, []
    for coin in set(events) | set(actual):
        dex = coin.split(":")[0] if ":" in coin else ""
        if dex not in checked:
            continue
        t_state = int((states.get(dex) or main).get("time") or rec.get("fetched_at") or 0)
        times, pos, pos0 = events.get(coin, (np.array([], dtype=np.int64), np.array([]), 0.0))
        mine = float(_wallet_pos_at(times, pos, pos0, np.array([t_state]))[0])
        theirs = actual.get(coin, 0.0)
        px = marks.get(coin) or float(last_px.get(coin, 0.0))
        gap += abs(mine - theirs) * px
        book += max(abs(mine), abs(theirs)) * px
        if abs(mine - theirs) * px > 1.0:
            worst.append({"coin": coin, "rebuilt": mine, "account": theirs, "gap_usd": abs(mine - theirs) * px})
    worst.sort(key=lambda x: -x["gap_usd"])
    return {"gap_usd": gap, "book_usd": book, "coins": worst[:5]}


def pnl_check(ref_stats: dict | None, real: pd.Series, start_ms: int, end_ms: int) -> dict | None:
    """Immediate copy (no costs) against the wallet's own perp PnL in the same
    window: a diagnostic of the rebuilt book, not a test."""
    if not ref_stats or real.empty or ref_stats.get("days", 0) < 2:
        return None
    t = np.array([pd.Timestamp(start_ms, unit="ms").floor("D").value // 1_000_000, end_ms], float)
    a, b = np.interp(t, real.index.to_numpy(float), real.to_numpy(float))
    sim, act = float(ref_stats["pnl"]), float(b - a)
    return {"sim": sim, "real": act, "rel": abs(sim - act) / max(abs(sim), abs(act), 1.0)}


def evaluate_wallet(addr: str, cfg: Settings, market: Market, client: InfoClient, data_dir: str = "data",
                    verbose: bool = False) -> tuple[dict, dict]:
    addr = addr.lower()
    want_from = cfg.split_ms - cfg.insample_days * DAY_MS
    # fills must start before the split plus room for `min_oos_days` of test
    need_from = cfg.end_ms - cfg.min_oos_days * DAY_MS
    rec = fetch_wallet(client, addr, want_from, data_dir, need_from=need_from, need_to=cfg.end_ms)
    cov = int(rec["coverage"])
    # a fill older than the coverage may sit before a hole: the position at
    # the coverage start comes from the startPosition of the first fill after it
    fills = prepare_fills([f for f in rec["fills"] if f["time"] >= cov])
    events = position_events(fills)
    pos_now = all_open_positions(rec)
    eval_start = max(cov, want_from)
    oos_start = max(cfg.split_ms, eval_start)
    seen = fills[fills["time"] <= cfg.end_ms]
    last_fill = int(seen["time"].max()) if len(seen) else None
    out = {"address": addr, "coverage_start": cov, "complete_to": int(rec["complete_to"]), "eval_start": eval_start,
           "oos_start": oos_start, "n_fills": int(len(fills)),
           "n_twap": int(fills["twapId"].notna().sum()) if "twapId" in fills else 0,
           "last_fill": last_fill, "coins": sorted(set(events) | set(pos_now))}
    art: dict = {}
    oos_days = (cfg.end_ms - oos_start) / DAY_MS
    if not events and not pos_now:
        out.update(status="reprovada", reason="sem operações em perps no período")
        return out, art
    if oos_days < cfg.min_oos_days:
        # the API no longer serves the fills of the test period: stop before
        # spending requests on prices
        out.update(status="insuficiente", oos_days=float(oos_days),
                   reason=f"período fora da amostra curto ({oos_days:.0f} dias, fills só desde "
                          f"{pd.Timestamp(cov, unit='ms'):%d/%m/%Y})")
        return out, art
    if rec["complete_to"] < cfg.end_ms:
        out.update(status="insuficiente", oos_days=float(oos_days),
                   reason=f"fills incompletos (completos só até {pd.Timestamp(rec['complete_to'], unit='ms'):%d/%m/%Y})")
        return out, art
    out["jump_share"] = jump_share(fills, eval_start, cfg.end_ms)
    if out["jump_share"] > cfg.max_jump_share:
        out.update(status="insuficiente", oos_days=float(oos_days),
                   reason=f"posição muda sem fills visíveis ({out['jump_share'] * 100:.0f}% do volume)")
        return out, art
    # with the fills complete, a position held now and never traded since the
    # coverage start was open the whole window
    for coin, szi in pos_now.items():
        if coin not in events:
            events[coin] = (np.array([], dtype=np.int64), np.array([]), float(szi))
    out["recon"] = reconcile(events, rec, fills)
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
    perp_pnl = perp_pnl_series(data_dir, addr)
    out["pnl_check_is"] = pnl_check(out.get("is_ref"), perp_pnl, eval_start, oos_start - 1)
    out["pnl_check_oos"] = pnl_check(out.get("oos_ref"), perp_pnl, oos_start, cfg.end_ms)
    r_main = results[cfg.main_delay_min]
    d = r_main.daily[r_main.daily.index >= pd.Timestamp(oos_start, unit="ms").floor("D")]
    r_oos = follower_path(d["ret"].to_numpy(float))
    # skill: timing (shift test) and alpha over BTC
    he = r_main.hourly_expo[r_main.hourly_expo.index >= oos_start]
    hr = r_main.hourly_ret[r_main.hourly_ret.index >= oos_start]
    out["shift"] = stats.shift_test(he, hr)
    btc = market.candles("BTC", "1d", eval_start - 3 * DAY_MS).set_index("t")["close"]
    btc_ret = btc / btc.shift(1) - 1
    btc_ret.index = pd.to_datetime(btc_ret.index, unit="ms")
    out["alpha"] = stats.alpha_beta(r_oos, btc_ret.reindex(d.index).to_numpy(float))
    boot = stats.block_bootstrap(r_oos, n_boot=2000, mean_block=5)
    if len(boot):
        lo, hi = np.quantile(boot * 365, [0.05, 0.95])
        out["ret_ann_ci90"] = [float(lo), float(hi)]
    out["style"] = style(r_main, oos_start)
    out["by_coin"] = r_main.by_coin.to_dict("records") if len(r_main.by_coin) else []
    acct = float(((rec.get("state") or {}).get("marginSummary") or {}).get("accountValue") or 0.0)
    out["account_value_now"] = acct
    out["open_positions"] = pos_now
    out["oos_days"] = float(oos_days)
    out["eff_delay_min"] = effective_delay(r_main, oos_start)
    # the rebuilt book has to end where the account is
    ref = results["ref"].daily
    ref = ref[ref.index >= pd.Timestamp(eval_start, unit="ms").floor("D")]
    rc = out["recon"]
    rc["share"] = rc["gap_usd"] / max(rc["book_usd"], float(ref["gross"].mean()) if len(ref) else 0.0, 1.0)
    if rc["share"] > cfg.max_recon_share:
        out.update(status="insuficiente",
                   reason=f"posições reconstruídas não batem com a conta ({rc['share'] * 100:.0f}% do nocional)")
    art["daily"] = {k: v.daily for k, v in results.items()}
    art["by_coin"] = r_main.by_coin
    return out, art


def effective_delay(res, start_ms: int) -> float:
    tr = res.trades
    if tr is None or tr.empty:
        return float("nan")
    tr = tr[tr["t_signal"] >= start_ms]
    return float(((tr["t_exec"] - tr["t_signal"]) / 60_000).mean()) if len(tr) else float("nan")


def sample_problem(s: dict, cfg: Settings) -> str | None:
    """Why the wallet cannot be tested at all (None if it can)."""
    main = s.get(f"oos_{cfg.main_delay_min}") or {}
    if s.get("status") in ("reprovada", "erro", "insuficiente"):
        return s.get("reason", "")
    if s.get("oos_days", 0) < cfg.min_oos_days or main.get("days", 0) < cfg.min_oos_days:
        return (f"período fora da amostra curto ({s.get('oos_days', 0):.0f} dias, fills só desde "
                f"{pd.Timestamp(s['coverage_start'], unit='ms'):%d/%m/%Y})")
    if main.get("active_days", cfg.min_active_days) < cfg.min_active_days:
        return f"posicionada em poucos dias do teste ({main.get('active_days')})"
    opens = (s.get("style") or {}).get("opens", 0)
    if opens < cfg.min_opens:
        return f"poucas entradas fora da amostra ({opens})"
    return None


def classify(s: dict, cfg: Settings, q: float | None) -> tuple[str, list[str]]:
    """Verdict and the list of criteria that failed (in Portuguese, for the report)."""
    main = s.get(f"oos_{cfg.main_delay_min}") or {}
    fails, soft = [], []
    if s.get("status") == "reprovada":
        return "reprovada", [s.get("reason", "")]
    why = sample_problem(s, cfg)
    if why:
        return "dados insuficientes", [why]
    if not main.get("ret_ann", 0) > 0:
        fails.append(f"cópia com {cfg.main_delay_min} min perde dinheiro fora da amostra")
    elif main.get("ruined"):
        fails.append(f"cópia com {cfg.main_delay_min} min perderia todo o capital num dia")
    elif not (main.get("ret_comp", 0) > 0 and main.get("pnl", 0) > 0):
        # a positive mean of daily returns, but the follower ends with less money
        fails.append(f"cópia com {cfg.main_delay_min} min perde dinheiro no retorno composto ou em US$")
    for d in cfg.robust_delays_min:
        x = s.get(f"oos_{d}") or {}
        if not x.get("ret_ann", 0) > 0:
            fails.append(f"com {d} min de atraso perde dinheiro")
    if q is None or not q <= cfg.q_max:
        fails.append(f"não significativo após correção para múltiplos testes (q = {q:.2f})".replace(".", ",") if q is not None
                     else "sem valor-q")
    sh = (s.get("shift") or {}).get("p")
    ta = (s.get("alpha") or {}).get("t_alpha")
    skill = (sh is not None and np.isfinite(sh) and sh <= cfg.skill_p_max) or (ta is not None and np.isfinite(ta) and ta >= cfg.skill_alpha_t)
    if not skill:
        fails.append("lucro explicado pela direção do mercado (sem timing nem alfa sobre o BTC)")
    if not main.get("pnl_ex_top5", 0) > 0:
        fails.append("lucro depende dos 5 melhores dias")
    last = s.get("last_fill")
    if last is None or (cfg.end_ms - last) > cfg.inactive_after_days * DAY_MS:
        soft.append(f"sem ordens nos últimos {cfg.inactive_after_days} dias"
                    + (" (segue com posições abertas)" if s.get("open_positions") else ""))
    if not fails:
        return ("aprovada" if not soft else "aprovada (inativa)"), soft
    if (main.get("p", 1) <= 0.05 and main.get("ret_ann", 0) > 0 and (s.get("oos_60") or {}).get("ret_ann", 0) > 0
            and main.get("ret_comp", 0) > 0 and main.get("pnl", 0) > 0 and not main.get("ruined")):
        return "promissora", fails + soft
    return "reprovada", fails + soft


def settings_dict(cfg: Settings) -> dict:
    return asdict(cfg)
