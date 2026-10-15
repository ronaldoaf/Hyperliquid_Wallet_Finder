import pandas as pd

from hlwf.screen import score_portfolio, stage_a

D = 86_400_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % D)


def portfolio(perp_av, total_av, pnl, vlm=1e7, month_vlm=0.0):
    t = [T0 + i * 7 * D for i in range(len(pnl))]
    return {"perpAllTime": {"accountValueHistory": list(zip(t, map(str, perp_av))),
                            "pnlHistory": list(zip(t, map(str, pnl))), "vlm": str(vlm)},
            "allTime": {"accountValueHistory": list(zip(t, map(str, total_av))),
                        "pnlHistory": list(zip(t, map(str, pnl))), "vlm": str(vlm)},
            "perpMonth": {"vlm": str(month_vlm)}}


def test_unified_margin_wallet_is_scored_on_total_equity():
    # perp account value ~0 (margin in spot USDC), total equity $1M, +$10k a week
    n = 30
    pf = portfolio([100.0] * n, [1e6] * n, [i * 1e4 for i in range(n)])
    s = score_portfolio(pf, T0 + (n - 1) * 7 * D)
    assert s["ok"] and abs(s["mean_w"] - 0.01) < 1e-3 and s["av_split"] == 1e6


def test_stage_a_ignores_last_month_volume():
    lb = pd.DataFrame({"address": ["a", "b"], "allTime_vlm": [150e6, 150e6], "month_vlm": [10e6, 80e6],
                       "allTime_pnl": [0.0, 0.0], "account_value": [0.0, 0.0]})
    assert list(stage_a(lb, min_volume=100e6)["address"]) == ["a"]
