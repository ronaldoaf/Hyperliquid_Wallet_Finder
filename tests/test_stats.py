import numpy as np
import pandas as pd
import pytest

from hlwf import stats


def test_hac_t_detects_mean():
    rng = np.random.default_rng(1)
    t, p = stats.hac_t(rng.normal(0.5, 1.0, 400))
    assert t > 5 and p < 1e-6
    t, p = stats.hac_t(rng.normal(0.0, 1.0, 400))
    assert abs(t) < 4


def test_hac_widens_for_autocorrelated_series():
    rng = np.random.default_rng(2)
    e = rng.normal(size=2000)
    ar = np.zeros_like(e)
    for i in range(1, len(e)):
        ar[i] = 0.8 * ar[i - 1] + e[i]
    naive = ar.std(ddof=1) / np.sqrt(len(ar))
    assert stats.hac_se_mean(ar) > 1.5 * naive


def test_bh_qvalues_known_case():
    q = stats.bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.0533333, 0.0533333, 0.5], rel=1e-4)


def test_psr_and_dsr_ordering():
    rng = np.random.default_rng(3)
    x = rng.normal(0.2, 1.0, 250)
    assert stats.psr(x) > 0.99
    assert stats.dsr(x, n_trials=1000) < stats.psr(x)


def test_expected_max_sr_grows_with_trials():
    assert stats.expected_max_sr(10, 0.01) < stats.expected_max_sr(1000, 0.01)


def test_bootstrap_matches_standard_error_for_iid():
    rng = np.random.default_rng(4)
    x = rng.normal(1.0, 1.0, 300)
    b = stats.block_bootstrap(x, n_boot=2000)
    se = x.std(ddof=1) / np.sqrt(len(x))
    assert b.mean() == pytest.approx(x.mean(), abs=0.2 * se)
    assert b.std() == pytest.approx(se, rel=0.2)


def test_shift_test_timing_vs_bias():
    rng = np.random.default_rng(5)
    n = 24 * 120
    r = rng.normal(0, 0.01, n)
    ret = pd.DataFrame({"X": r})
    # perfect timing: long before up hours
    good = pd.DataFrame({"X": np.sign(r)})
    assert stats.shift_test(good, ret)["p"] < 0.01
    # constant long in a drifting market: profitable, but no timing
    drift = pd.DataFrame({"X": r + 0.001})
    const = pd.DataFrame({"X": np.ones(n)})
    assert stats.shift_test(const, drift)["p"] > 0.2


def test_alpha_beta_recovers_parameters():
    rng = np.random.default_rng(6)
    m = rng.normal(0, 0.03, 500)
    y = 0.002 + 1.5 * m + rng.normal(0, 0.005, 500)
    ab = stats.alpha_beta(y, m)
    assert ab["beta"] == pytest.approx(1.5, abs=0.05)
    assert ab["t_alpha"] > 4


def test_max_drawdown():
    assert stats.max_drawdown([0.1, -0.5, 0.2]) == pytest.approx(0.5)
