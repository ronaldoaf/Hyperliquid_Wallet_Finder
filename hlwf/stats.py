"""Statistics used to decide whether a copy return is real.

- `hac_t`: t-statistic of the mean with Newey-West standard errors (daily
  returns of a position-holding strategy are autocorrelated).
- `block_bootstrap`: stationary bootstrap (Politis-Romano) intervals.
- `psr` / `dsr`: probabilistic and deflated Sharpe ratio (Bailey & Lopez de
  Prado): the chance that the true Sharpe beats a benchmark, given the
  sample's skew and fat tails, and the benchmark a best-of-N search would
  reach by luck.
- `bh_qvalues`: Benjamini-Hochberg false discovery rate across wallets.
- `shift_test`: the wallet's exposures moved in time (circular shifts) as a
  null that keeps its long/short bias and holding times but destroys
  timing; a market that went one way does not count as skill.
- `alpha_beta`: regression on BTC with HAC errors.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
from scipy import stats as st

EULER = 0.5772156649015329


def nw_lags(n: int) -> int:
    return max(1, int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0))))


def hac_se_mean(x: np.ndarray, lags: int | None = None) -> float:
    x = np.asarray(x, float)
    n = len(x)
    if n < 3:
        return float("nan")
    lags = nw_lags(n) if lags is None else lags
    e = x - x.mean()
    s = e @ e / n
    for k in range(1, min(lags, n - 1) + 1):
        w = 1.0 - k / (lags + 1.0)
        s += 2 * w * (e[k:] @ e[:-k]) / n
    return math.sqrt(max(s, 0.0) / n)


def hac_t(x) -> tuple[float, float]:
    """(t, one-sided p-value for mean > 0)."""
    x = np.asarray(x, float)
    se = hac_se_mean(x)
    if not se or not np.isfinite(se) or se == 0:
        return 0.0, 1.0
    t = float(x.mean() / se)
    return t, float(st.t.sf(t, df=max(len(x) - 1, 1)))


def block_bootstrap(x, stat=np.mean, n_boot: int = 2000, mean_block: float = 5.0, seed: int = 0) -> np.ndarray:
    """Stationary bootstrap: blocks of geometric length with mean `mean_block`,
    wrapping around the end. `stat` is applied to each resampled row."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 5:
        return np.array([])
    rng = np.random.default_rng(seed)
    new = rng.random((n_boot, n)) < 1.0 / mean_block
    new[:, 0] = True
    starts = rng.integers(n, size=(n_boot, n))
    k = np.arange(n)
    first = np.maximum.accumulate(np.where(new, k, 0), axis=1)  # where the current block began
    idx = (np.take_along_axis(starts, first, axis=1) + (k - first)) % n
    sample = x[idx]
    if stat is np.mean:
        return sample.mean(axis=1)
    return np.array([stat(row) for row in sample])


def sharpe(x) -> float:
    x = np.asarray(x, float)
    sd = x.std(ddof=1) if len(x) > 1 else 0.0
    return float(x.mean() / sd) if sd > 0 else 0.0


def psr(x, sr0: float = 0.0) -> float:
    """P(true per-period Sharpe > sr0) given skew and kurtosis of x."""
    x = np.asarray(x, float)
    n = len(x)
    if n < 10:
        return float("nan")
    sr = sharpe(x)
    g3 = float(st.skew(x))
    g4 = float(st.kurtosis(x, fisher=False))
    den = 1 - g3 * sr + (g4 - 1) / 4 * sr * sr
    if den <= 0:
        den = 1e-9
    return float(st.norm.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(den)))


def expected_max_sr(n_trials: int, var_sr: float) -> float:
    """Sharpe the best of `n_trials` strategies with zero true Sharpe reaches
    on average (False Strategy Theorem)."""
    if n_trials <= 1:
        return 0.0
    z1 = st.norm.ppf(1 - 1.0 / n_trials)
    z2 = st.norm.ppf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(max(var_sr, 0.0)) * ((1 - EULER) * z1 + EULER * z2)


def dsr(x, n_trials: int, var_sr: float | None = None) -> float:
    x = np.asarray(x, float)
    var_sr = (1.0 / max(len(x) - 1, 1)) if var_sr is None else var_sr
    return psr(x, expected_max_sr(n_trials, var_sr))


def bh_qvalues(p) -> np.ndarray:
    p = np.asarray(p, float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def shift_test(expo: pd.DataFrame, ret: pd.DataFrame, min_shift: int = 168) -> dict:
    """Circular-shift test of timing on hourly data.

    S(tau) = sum over coins and hours of exposure(h - tau) * return(h). The
    actual strategy is tau = 0; shifts of at least `min_shift` hours keep
    every feature of the book except *when* it was on. Returns the one-sided
    p-value and where the actual value sits in the null distribution.
    """
    if expo.empty:
        return {"p": float("nan"), "z": float("nan"), "n": 0}
    E = expo.fillna(0.0).to_numpy(float)
    R = ret.reindex(columns=expo.columns).fillna(0.0).to_numpy(float)
    L = E.shape[0]
    if L < 4 * min_shift:
        return {"p": float("nan"), "z": float("nan"), "n": 0}
    FE = np.fft.rfft(E, axis=0)
    FR = np.fft.rfft(R, axis=0)
    S = np.fft.irfft((np.conj(FE) * FR).sum(axis=1), n=L)
    actual = float(S[0])
    null = S[min_shift:L - min_shift + 1]
    p = (1 + int((null >= actual).sum())) / (1 + len(null))
    sd = null.std()
    return {"p": float(p), "z": float((actual - null.mean()) / sd) if sd > 0 else float("nan"),
            "n": int(len(null)), "actual": actual, "null_mean": float(null.mean())}


def alpha_beta(y, m) -> dict:
    """y = alpha + beta * m + e, HAC t for alpha."""
    y = np.asarray(y, float)
    m = np.asarray(m, float)
    ok = np.isfinite(y) & np.isfinite(m)
    y, m = y[ok], m[ok]
    if len(y) < 20 or m.std() == 0:
        return {"alpha": float("nan"), "beta": float("nan"), "t_alpha": float("nan"), "r2": float("nan")}
    X = np.c_[np.ones(len(m)), m]
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    n, k = X.shape
    lags = nw_lags(n)
    XtX_inv = np.linalg.inv(X.T @ X)
    S = (X * e[:, None]).T @ (X * e[:, None])
    for j in range(1, lags + 1):
        w = 1 - j / (lags + 1)
        G = (X[j:] * e[j:, None]).T @ (X[:-j] * e[:-j, None])
        S += w * (G + G.T)
    V = XtX_inv @ S @ XtX_inv
    se_a = math.sqrt(max(V[0, 0], 1e-30))
    r2 = 1 - (e @ e) / max(((y - y.mean()) @ (y - y.mean())), 1e-30)
    return {"alpha": float(b[0]), "beta": float(b[1]), "t_alpha": float(b[0] / se_a), "r2": float(r2)}


def max_drawdown(ret) -> float:
    """Largest fall of an equity curve compounding `ret` (fractions)."""
    r = np.clip(np.asarray(ret, float), -0.999999, None)
    eq = np.r_[0.0, np.cumsum(np.log1p(r))]
    return float(1 - np.exp((eq - np.maximum.accumulate(eq)).min()))


def concentration(pnl, k: int = 5) -> dict:
    x = np.sort(np.asarray(pnl, float))[::-1]
    tot = x.sum()
    return {"top_k_share": float(x[:k].sum() / tot) if tot > 0 else float("nan"),
            "ex_top_k": float(x[k:].sum())}
