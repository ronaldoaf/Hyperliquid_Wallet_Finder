"""Command line entry point: `python -m hlwf <command>`."""
from __future__ import annotations

import argparse
import time

from .api import InfoClient


def cmd_portfolios(a):
    from .screen import fetch_portfolios, load_leaderboard, stage_a
    lb = load_leaderboard(a.data_dir)
    cand = stage_a(lb, min_volume=a.min_volume, leaky=a.leaky)
    if a.limit:
        cand = cand.head(a.limit)
    print(f"stage A: {len(cand)} of {len(lb)} wallets (volume >= {a.min_volume:,.0f})", flush=True)
    c = InfoClient(budget=a.budget)
    n = fetch_portfolios(c, cand["address"], a.data_dir, max_age_h=a.max_age_h)
    print(f"portfolios fetched: {n}", flush=True)


def _cfg_kw(a) -> dict:
    kw = {}
    for k in ("main_delay_min", "taker_bps", "max_leverage", "min_oos_days", "insample_days"):
        v = getattr(a, k, None)
        if v is not None:
            kw[k] = v
    return kw


def cmd_scan(a):
    from .pipeline import run_scan
    path = run_scan(a.data_dir, a.out, split_days=a.split_days, top=a.top, min_volume=a.min_volume,
                    leaky=a.leaky, budget=a.budget, fetch_missing=a.fetch_missing, cfg_kw=_cfg_kw(a))
    print(f"report: {path}")


def cmd_wallet(a):
    import pandas as pd

    from .pipeline import run_wallets
    from .report import write_wallet_report
    table, results, cfg = run_wallets(a.addresses, a.data_dir, a.out, split_days=a.split_days, budget=a.budget,
                                      cfg_kw=_cfg_kw(a))
    tag = pd.Timestamp(cfg.split_ms, unit="ms").strftime("avulsas_%Y-%m-%d")
    path = write_wallet_report(a.out, a.data_dir, tag, cfg, table, results, "Carteiras avulsas")
    cols = ["address", "verdict", "oos_days", "ret_ann_30", "t_30", "shift_p", "alpha_t", "why"]
    with pd.option_context("display.width", 200, "display.max_colwidth", 90):
        print(table[cols].to_string(index=False))
    print(f"report: {path}")


def cmd_track(a):
    import pandas as pd

    from .pipeline import load_selection, run_wallets
    from .report import write_wallet_report
    sel = load_selection(a.selection)
    addrs = [w["address"] for w in sel["wallets"]]
    if not addrs:
        print("selection is empty")
        return
    start = int(sel["selected_at"])
    tag = pd.Timestamp(start, unit="ms").strftime("acompanhamento_%Y-%m-%d")
    kw = _cfg_kw(a) | {"min_oos_days": 1}
    table, results, cfg = run_wallets(addrs, a.data_dir, a.out, budget=a.budget, cfg_kw=kw, split_ms=start, tag=tag)
    path = write_wallet_report(a.out, a.data_dir, tag, cfg, table, results,
                               f"Acompanhamento da seleção de {pd.Timestamp(start, unit='ms'):%d/%m/%Y}")
    print(f"report: {path}")


def cmd_positions(a):
    """What the selected wallets hold now and what they did in the last hours."""
    import json

    import pandas as pd

    addrs = []
    for x in a.targets:
        if x.endswith(".json"):
            with open(x) as f:
                addrs += [w["address"] for w in json.load(f)["wallets"]]
        else:
            addrs.append(x.lower())
    c = InfoClient(budget=a.budget)
    now = int(time.time() * 1000)
    for addr in addrs:
        st = c.post({"type": "clearinghouseState", "user": addr})
        av = float(st["marginSummary"]["accountValue"])
        print(f"\n## {addr}  (patrimônio em perps US$ {av:,.0f})")
        rows = []
        for p in st.get("assetPositions", []):
            x = p["position"]
            szi = float(x["szi"])
            ntl = float(x["positionValue"])
            rows.append({"moeda": x["coin"], "lado": "long" if szi > 0 else "short", "tamanho": abs(szi),
                         "entrada": float(x["entryPx"]), "nocional": ntl, "alav": ntl / av if av else float("nan"),
                         "PnL aberto": float(x["unrealizedPnl"]), "liquidação": x.get("liquidationPx")})
        print(pd.DataFrame(rows).to_string(index=False) if rows else "sem posições abertas")
        fills = c.post({"type": "userFillsByTime", "user": addr, "startTime": now - a.hours * 3_600_000,
                        "aggregateByTime": True})
        fills = [f for f in fills if not f["coin"].startswith("@") and "/" not in f["coin"]]
        if fills:
            df = pd.DataFrame(fills)
            df["hora"] = pd.to_datetime(df["time"], unit="ms").dt.strftime("%d/%m %H:%M")
            print(f"fills nas últimas {a.hours}h:")
            print(df[["hora", "coin", "dir", "sz", "px"]].tail(15).to_string(index=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hlwf", description="Hyperliquid wallet finder")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--budget", type=int, default=1100, help="API weight per minute (limit 1200)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("portfolios", help="stage A + download weekly history (stage B input)")
    p.add_argument("--min-volume", type=float, default=50e6)
    p.add_argument("--leaky", action="store_true")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--max-age-h", type=float, default=72.0)
    p.set_defaults(func=cmd_portfolios)

    def common(p):
        p.add_argument("--out", default="reports")
        p.add_argument("--split-days", type=int, default=120, help="length of the out-of-sample test")
        p.add_argument("--main-delay-min", type=int, default=None)
        p.add_argument("--taker-bps", type=float, default=None)
        p.add_argument("--max-leverage", type=float, default=None)
        p.add_argument("--min-oos-days", type=int, default=None)
        p.add_argument("--insample-days", type=int, default=None)

    p = sub.add_parser("scan", help="full search: stages A, B and C plus the report")
    common(p)
    p.add_argument("--top", type=int, default=150, help="wallets sent to stage C")
    p.add_argument("--min-volume", type=float, default=50e6)
    p.add_argument("--leaky", action="store_true")
    p.add_argument("--fetch-missing", action="store_true", help="download missing weekly histories first")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("wallet", help="test specific wallets")
    common(p)
    p.add_argument("addresses", nargs="+")
    p.set_defaults(func=cmd_wallet)

    p = sub.add_parser("track", help="forward test of a past selection")
    common(p)
    p.add_argument("selection", help="reports/varredura_.../selecao.json")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("positions", help="open positions and recent fills of selected wallets")
    p.add_argument("targets", nargs="+", help="addresses or a selecao.json")
    p.add_argument("--hours", type=int, default=24)
    p.set_defaults(func=cmd_positions)

    a = ap.parse_args(argv)
    t0 = time.time()
    a.func(a)
    print(f"done in {(time.time() - t0) / 60:.1f} min", flush=True)
