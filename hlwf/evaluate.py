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
from .wallet import all_open_positions, equity_series, fetch_wallet, funding_rows, perp_pnl_series

DAY_MS = 86_400_000
EPS_POS = 1e-12
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
    funding_cover: float = 0.90     # real funding until this share of the test-period exposure
    funding_max_coins: int = 40
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
    # an entry is a coin going from flat to held after the start: a position
    # carried in from before the window is not one
    entries = (on & ~on.shift(1, fill_value=True)).sum().sum()
    carried = on & (~on).cumsum().eq(0)  # each coin's first run, if it was already on at the start
    hours_on = (on & ~carried).sum().sum()
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


def start_check(events: dict, rows: list[dict], fills: pd.DataFrame, d0: int) -> dict:
    """Positions rebuilt from the fills against the sizes the wallet paid
    funding on, on the UTC day `d0` (the first whole day of the window). A
    position built by TWAP slices or fills the API no longer serves would
    otherwise be copied from the start of the window, before the wallet held
    it.

    Hourly rows: the first funding hour of the day. Daily rows (older history):
    the day's average position (szi x nSamples / 24) against the rebuilt
    position averaged over the day's hours. A day with no row means the wallet
    held nothing, and is compared as such (on every dex: userFunding reports
    HIP-3 dexes too)."""
    hourly = all(r.get("nSamples") in (None, "") for r in rows)
    if rows and hourly:
        h0 = (min(r["time"] for r in rows) // HOUR_MS) * HOUR_MS
        theirs = {r["coin"]: float(r["szi"]) for r in rows if (r["time"] // HOUR_MS) * HOUR_MS == h0}
        hours = np.array([h0 + 1], dtype=np.int64)
    elif rows:
        theirs = {r["coin"]: float(r["szi"]) * float(r.get("nSamples") or 0) / 24.0 for r in rows}
        hours = d0 + HOUR_MS * np.arange(24, dtype=np.int64)
    else:
        theirs = {}
        hours = np.array([d0 + 1], dtype=np.int64)
    px_fill = fills.groupby("coin")["px"].first().to_dict() if len(fills) else {}
    # usdc is the day's total over nSamples payments, szi and the rate are averages
    px_fund = {r["coin"]: abs(float(r["usdc"])) / (abs(float(r["szi"]) * float(r["fundingRate"]))
                                                   * float(r.get("nSamples") or 1))
               for r in rows if float(r.get("szi") or 0) and float(r.get("fundingRate") or 0)}
    gap, book, worst = 0.0, 0.0, []
    for coin in set(events) | set(theirs):
        times, pos, pos0 = events.get(coin, (np.array([], dtype=np.int64), np.array([]), 0.0))
        mine = float(np.mean(_wallet_pos_at(times, pos, pos0, hours)))
        other = theirs.get(coin, 0.0)
        px = px_fill.get(coin) or px_fund.get(coin) or 0.0
        gap += abs(mine - other) * px
        book += max(abs(mine), abs(other)) * px
        if abs(mine - other) * px > 1.0:
            worst.append({"coin": coin, "rebuilt": mine, "funding": other, "gap_usd": abs(mine - other) * px})
    worst.sort(key=lambda x: -x["gap_usd"])
    return {"gap_usd": gap, "book_usd": book, "coins": worst[:5], "checked": True, "day": int(d0)}


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
    # and it has to start where the wallet was: the positions it paid funding on
    try:
        d0 = -(-eval_start // DAY_MS) * DAY_MS  # the first whole UTC day inside the window
        out["recon_start"] = start_check(events, funding_rows(client, addr, d0, data_dir), fills, d0)
    except Exception as e:  # the check needs one request; without it, say so
        out["recon_start"] = {"checked": False, "error": str(e)[:200]}
    eq_raw = equity_series(data_dir, addr)
    oracles, costs, maint, unpriced = {}, {}, {}, []
    for coin in events:
        try:
            o = market.oracle(coin, eval_start - DAY_MS)
        except Exception as e:
            if verbose:
                print(f"  {coin}: no prices ({e})", flush=True)
            unpriced.append(coin)
            continue
        if not o.layers or o.daily.empty:  # nothing to trade or mark at
            unpriced.append(coin)
            continue
        oracles[coin] = o
        costs[coin] = market.cost_bps(coin, cfg.taker_bps, cfg.hip3_fee_mult,
                                      window_ntl(o.daily, eval_start, cfg.end_ms))
        maint[coin] = market.maintenance(coin, cfg.max_leverage)
    # a coin that cannot be priced is left out of the copy: fine if it is a
    # crumb of the book, not if the result would depend on what is missing.
    # A failed download is not a fact about the wallet: the result is not
    # saved as valid (the wallet is retried on resume and counts as p = 1).
    missing = sorted(unpriced)
    if missing:
        if set(missing) & market.errors:
            raise RuntimeError(f"price download failed for {', '.join(sorted(set(missing) & market.errors)[:5])}")
        w = fills[(fills["time"] >= eval_start) & (fills["time"] <= cfg.end_ms)]
        ntl = (w["q"].abs() * w["px"]).groupby(w["coin"]).sum()
        share_missing = float(ntl.reindex(missing).fillna(0.0).sum() / max(float(ntl.sum()), 1e-9))
        out["missing_prices"] = {"coins": missing, "share": share_missing}
        if share_missing > cfg.max_jump_share or (set(pos_now) & set(missing)):
            out.update(status="insuficiente", oos_days=float(oos_days),
                       reason=f"sem preços para {', '.join(missing[:3])} ({share_missing * 100:.0f}% do volume)")
            return out, art
    main = cfg.main_delay_min * 60_000
    # The equity a follower sizes against is the wallet's live account value.
    # The portfolio readings are days apart, so carry each one forward with
    # the wallet's own PnL since then (an immediate copy without costs).
    own = simulate(events, oracles, 0, eval_start, cfg.end_ms, {}, None, eq_raw, cfg.max_leverage, hourly=False)
    eq = carried_equity(eq_raw, own.daily["pnl"])
    # Real hourly funding (7 API calls per coin and 120 days) for the coins
    # that carry the book in the test period: by exposure, until 90% of it is
    # covered (at most 40 coins, plus any coin with 5% or more), and only
    # inside the test period. Elsewhere, and before the split (where a choice
    # made with test-period exposure must not reach the in-sample screen), a
    # flat rate: the main dex's base rate, and zero on HIP-3 dexes (their base
    # rates are lower, and a flat rate would credit their shorts with income).
    probe = simulate(events, oracles, main, eval_start, cfg.end_ms, costs, None, eq, cfg.max_leverage)
    he = probe.hourly_expo
    share = he[he.index >= oos_start].abs().sum() if len(he) else pd.Series(dtype=float)
    share = (share / max(share.sum(), 1e-12)).sort_values(ascending=False)
    chosen, covered = set(), 0.0
    for coin, sh in share.items():
        if sh <= 0 or (sh < cfg.funding_min_share and (covered >= cfg.funding_cover
                                                       or len(chosen) >= cfg.funding_max_coins)):
            continue
        chosen.add(coin)
        covered += sh
    out["funding_real_share"] = float(covered)
    funding = {}
    hours = np.arange((eval_start // HOUR_MS) * HOUR_MS, cfg.end_ms + HOUR_MS, HOUR_MS, dtype=np.int64)
    f0 = oos_start
    for coin in oracles:
        flat = 0.0 if ":" in coin else BASE_FUNDING
        base = pd.Series(flat, index=hours)
        if coin in chosen:
            real = market.funding(coin, f0 - HOUR_MS, cfg.end_ms)
            real = real[real.index >= f0 - HOUR_MS]
            real.index = (real.index.to_numpy(np.int64) // HOUR_MS) * HOUR_MS
            real = real.groupby(level=0).last()
            # an hour the download did not bring keeps the flat rate (never zero for longs on the main dex)
            base.loc[base.index >= f0] = real.reindex(base.index[base.index >= f0]).fillna(flat).to_numpy()
        funding[coin] = base
    results = {}
    results["ref"] = simulate(events, oracles, 0, eval_start, cfg.end_ms, {}, funding, eq, cfg.max_leverage,
                              maint=maint)
    for d in sorted(set(cfg.delays_min) | {cfg.main_delay_min}):
        results[d] = simulate(events, oracles, d * 60_000, eval_start, cfg.end_ms, costs, funding, eq,
                              cfg.max_leverage, maint=maint)
    for k, res in results.items():
        # a follower who starts at the split pays to enter the book it inherits
        # and to leave the one still open at the end (not the cost-free reference)
        daily = res.daily if k == "ref" else with_edge_costs(res, oracles, costs, oos_start, cfg.end_ms)
        out[f"oos_{k}"] = slice_stats(daily, oos_start, cfg.end_ms)
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
    out["by_coin"] = by_coin_oos(r_main, oos_start)
    # perp equity now, over every dex (HIP-3 margin sits in its own clearinghouse)
    states = [rec.get("state") or {}] + list((rec.get("dex_states") or {}).values())
    out["account_value_now"] = float(sum(float((st.get("marginSummary") or {}).get("accountValue") or 0.0)
                                         for st in states))
    out["open_positions"] = pos_now
    out["oos_days"] = float(oos_days)
    out["eff_delay_min"] = effective_delay(r_main, oos_start)
    # the rebuilt book has to end where the account is
    ref = results["ref"].daily
    ref = ref[ref.index >= pd.Timestamp(eval_start, unit="ms").floor("D")]
    rc = out["recon"]
    gross_avg = float(ref["gross"].mean()) if len(ref) else 0.0
    rc["share"] = rc["gap_usd"] / max(rc["book_usd"], gross_avg, 1.0)
    rs = out["recon_start"]
    rs["share"] = rs.get("gap_usd", 0.0) / max(rs.get("book_usd", 0.0), gross_avg, 1.0)
    if rc["share"] > cfg.max_recon_share:
        out.update(status="insuficiente",
                   reason=f"posições reconstruídas não batem com a conta ({rc['share'] * 100:.0f}% do nocional)")
    elif rs["share"] > cfg.max_recon_share:
        out.update(status="insuficiente",
                   reason=f"posições no início da janela não batem com o histórico de funding "
                          f"({rs['share'] * 100:.0f}% do nocional, TWAP ou fills que a API não guarda mais)")
    art["daily"] = {k: v.daily for k, v in results.items()}
    art["by_coin"] = r_main.by_coin
    art["main_delay"] = cfg.main_delay_min
    return out, art


def window_ntl(daily: pd.DataFrame, start_ms: int, end_ms: int) -> float | None:
    """Median daily traded notional of a coin over the window (1d candles)."""
    if daily is None or daily.empty or "volume" not in daily:
        return None
    d = daily[(daily["t"] >= start_ms - DAY_MS) & (daily["t"] <= end_ms)]
    if d.empty:
        return None
    return float((d["volume"] * d["close"]).median())


def carried_equity(readings: pd.Series, pnl: pd.Series) -> pd.Series:
    """Account value at each day start: the last reading at or before it plus
    the wallet's PnL accrued since that reading (deposits and withdrawals
    show up at the next reading). Nothing after the day start is used."""
    if readings is None or readings.empty or pnl.empty:
        return readings
    days = pnl.index.to_numpy("datetime64[ms]").astype(np.int64)
    cum = np.r_[0.0, np.cumsum(pnl.to_numpy(float))]          # PnL before each day start
    edges = np.r_[days, days[-1] + DAY_MS]
    r_t = readings.index.to_numpy(np.int64)
    r_v = readings.to_numpy(float)
    i = np.searchsorted(r_t, days, side="right") - 1
    ok = i >= 0
    idx = np.maximum(i, 0)
    at_reading = np.interp(r_t[idx].astype(float), edges.astype(float), cum)  # PnL up to the reading
    eq = np.where(ok, r_v[idx] + cum[:-1] - at_reading, r_v[0])
    return pd.Series(np.maximum(eq, 0.0), index=days)


def with_edge_costs(res, oracles: dict, costs: dict, start_ms: int, end_ms: int) -> pd.DataFrame:
    """The daily frame with the cost of opening, at `start_ms`, the positions
    the follower already held then, and of closing, at `end_ms`, those still
    open, charged on the first and the last day."""
    tr, d = res.trades, res.daily.copy()
    if tr is None or tr.empty or d.empty:
        return d
    entry = exit_ = 0.0
    for coin, g in tr.groupby("coin"):
        o, c = oracles.get(coin), costs.get(coin, 0.0)
        if o is None:
            continue
        t = g["t_exec"].to_numpy(np.int64)
        q = g["q"].to_numpy(float)
        p0 = float(q[t <= start_ms].sum())
        p1 = float(q[t <= end_ms].sum())
        if abs(p0) > EPS_POS:
            entry += abs(p0) * o.price_asof(int(start_ms)) * c / 1e4
        if abs(p1) > EPS_POS:
            exit_ += abs(p1) * o.price_asof(int(end_ms)) * c / 1e4
    first = d.index[d.index >= pd.Timestamp(start_ms, unit="ms").floor("D")]
    for day, cost in ((first[0] if len(first) else None, entry), (d.index[-1], exit_)):
        if day is None or not np.isfinite(cost) or cost == 0:
            continue
        d.loc[day, "fees"] -= cost
        d.loc[day, "pnl"] -= cost
        cap = d.loc[day, "capital"]
        if np.isfinite(cap) and cap > 0:
            d.loc[day, "ret"] = max(float(d.loc[day, "ret"]) - cost / cap, -1.0)
    return d


def by_coin_oos(res, start_ms: int) -> list[dict]:
    """PnL per coin over the test period (the card shows test-period numbers
    only), with the whole-window totals kept alongside."""
    bc = res.by_coin
    if bc is None or bc.empty:
        return []
    cd = res.coin_daily
    oos = cd[cd.index >= pd.Timestamp(start_ms, unit="ms").floor("D")].sum() if len(cd) else pd.Series(dtype=float)
    out = bc.rename(columns={"pnl": "pnl_window"}).assign(pnl=lambda x: x["coin"].map(oos).fillna(0.0))
    return out.sort_values("pnl", ascending=False).to_dict("records")


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


def follower_gains(x: dict) -> bool:
    """The follower ends with more money: positive mean daily return, positive
    compounded return and dollar PnL, and no day that takes all the capital."""
    return (x.get("ret_ann", 0) > 0 and x.get("ret_comp", 0) > 0 and x.get("pnl", 0) > 0
            and not x.get("ruined"))


def classify(s: dict, cfg: Settings, q: float | None) -> tuple[str, list[str]]:
    """Verdict and the list of criteria that failed (in Portuguese, for the report)."""
    main = s.get(f"oos_{cfg.main_delay_min}") or {}
    fails, soft = [], []
    if s.get("status") == "reprovada":
        return "reprovada", [s.get("reason", "")]
    why = sample_problem(s, cfg)
    if why:
        return "dados insuficientes", [why]
    if not follower_gains(main):
        k = cfg.main_delay_min
        if main.get("ruined"):
            fails.append(f"cópia com {k} min perderia todo o capital num dia")
        elif main.get("ret_ann", 0) > 0:  # a positive mean of daily returns, but less money at the end
            fails.append(f"cópia com {k} min perde dinheiro no retorno composto ou em US$")
        else:
            fails.append(f"cópia com {k} min perde dinheiro fora da amostra")
    for d in cfg.robust_delays_min:
        x = s.get(f"oos_{d}") or {}
        if not follower_gains(x):
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
    first_robust = s.get(f"oos_{cfg.robust_delays_min[0]}") if cfg.robust_delays_min else main
    if main.get("p", 1) <= 0.05 and follower_gains(main) and follower_gains(first_robust or {}):
        return "promissora", fails + soft
    return "reprovada", fails + soft


def settings_dict(cfg: Settings) -> dict:
    return asdict(cfg)
