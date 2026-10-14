from hlwf.evaluate import Settings, classify
from hlwf.pipeline import finalize

D = 86_400_000
NOW = 1_790_000_000_000


def summary(ret=0.5, p=0.001, ret60=0.4, ret120=0.3, shift_p=0.01, alpha_t=1.0, ex_top5=1.0, opens=30,
            days=120, last_fill=NOW - D):
    s = {"address": "0xabc", "coverage_start": NOW - 300 * D, "oos_start": NOW - days * D,
         "oos_days": days, "last_fill": last_fill, "style": {"opens": opens},
         "shift": {"p": shift_p}, "alpha": {"t_alpha": alpha_t}}
    s["oos_30"] = {"days": days, "ret_ann": ret, "p": p, "pnl_ex_top5": ex_top5, "ret_comp": ret / 3, "pnl": 1000.0}
    s["oos_60"] = {"days": days, "ret_ann": ret60}
    s["oos_120"] = {"days": days, "ret_ann": ret120}
    return s


CFG = Settings(split_ms=NOW - 120 * D, end_ms=NOW)


def test_all_criteria_pass():
    assert classify(summary(), CFG, q=0.02)[0] == "aprovada"


def test_market_beta_only_is_not_approved():
    v, why = classify(summary(shift_p=0.6, alpha_t=0.5), CFG, q=0.02)
    assert v == "promissora" and any("mercado" in w for w in why)


def test_alpha_can_replace_timing():
    assert classify(summary(shift_p=0.6, alpha_t=2.5), CFG, q=0.02)[0] == "aprovada"


def test_must_survive_longer_delays():
    v, why = classify(summary(ret120=-0.1), CFG, q=0.02)
    assert v != "aprovada" and any("120 min" in w for w in why)


def test_short_sample_is_insufficient():
    assert classify(summary(days=30), CFG, q=0.02)[0] == "dados insuficientes"


def test_inactive_wallet_is_flagged():
    v, why = classify(summary(last_fill=NOW - 60 * D), CFG, q=0.02)
    assert v == "aprovada (inativa)" and any("sem ordens" in w for w in why)


def test_finalize_counts_untestable_wallets_as_p1():
    good = summary(p=0.001)
    short = summary(days=20, p=0.0001)
    short["address"] = "0xdef"
    table = finalize([good, short], CFG)
    row = table.set_index("address")
    assert row.loc["0xdef", "p_30"] == 1.0
    # two tests: q of the good one is p * 2 / 1
    assert row.loc["0xabc", "q"] == 0.002


def test_insample_loser_leaves_the_family():
    good = summary(p=0.001)
    bad = summary(p=0.0001)
    bad["address"] = "0xdef"
    bad["is_30"] = {"days": 30, "active_days": 30, "ret_ann": -0.2}
    table = finalize([good, bad], CFG).set_index("address")
    assert not table.loc["0xdef", "in_family"] and table.loc["0xdef", "verdict"] == "reprovada"
    # the family has one test, so q equals p
    assert table.loc["0xabc", "q"] == 0.001


def test_wallet_idle_before_split_stays_in_family():
    idle = summary(p=0.001)
    idle["is_30"] = {"days": 30, "active_days": 0, "ret_ann": 0.0}
    table = finalize([idle], CFG).set_index("address")
    assert table.loc["0xabc", "in_family"]


def test_mean_up_but_follower_down_is_not_approved():
    s = summary()
    s["oos_30"]["ret_comp"] = -0.05  # positive mean of daily returns, but the compounded copy lost
    v, why = classify(s, CFG, q=0.02)
    assert v == "reprovada" and any("composto" in w for w in why)


def test_ruined_copy_is_not_approved():
    s = summary()
    s["oos_30"]["ruined"] = True
    v, why = classify(s, CFG, q=0.02)
    assert v == "reprovada" and any("todo o capital" in w for w in why)


def test_follower_path_stops_at_ruin():
    from hlwf.evaluate import follower_path
    assert follower_path([0.1, -3.0, 0.5, 0.2]).tolist() == [0.1, -1.0, 0.0, 0.0]
    assert follower_path([0.1, -0.5]).tolist() == [0.1, -0.5]


def test_stamp_changes_with_settings_and_end():
    from hlwf.pipeline import run_stamp
    a = run_stamp(CFG)
    assert a == run_stamp(Settings(split_ms=NOW - 120 * D, end_ms=NOW))
    assert a["settings_hash"] != run_stamp(Settings(split_ms=NOW - 120 * D, end_ms=NOW + 1))["settings_hash"]
    assert a["settings_hash"] != run_stamp(Settings(split_ms=NOW - 120 * D, end_ms=NOW, insample_days=90))["settings_hash"]
