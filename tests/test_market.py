import numpy as np
import pandas as pd

from hlwf.market import PriceOracle, binance_symbol, validate_against

D = 86_400_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)


def daily(prices):
    return pd.DataFrame({"t": T0 + np.arange(len(prices)) * D, "open": prices, "close": prices, "volume": 1.0})


def five_min(prices, scale=1.0):
    rows = []
    for i, p in enumerate(prices):
        t = T0 + i * D + np.arange(288) * 300_000
        rows.append(pd.DataFrame({"t": t, "open": p / scale, "close": p / scale}))
    return pd.concat(rows, ignore_index=True)


def test_binance_symbol_mapping():
    assert binance_symbol("BTC") == "BTCUSDT"
    assert binance_symbol("kPEPE") == "1000PEPEUSDT"
    assert binance_symbol("xyz:TSLA") is None


def test_validate_accepts_matching_prices():
    p = np.linspace(100, 120, 30)
    assert validate_against(five_min(p), daily(p)) is not None


def test_validate_fixes_thousand_unit():
    p = np.linspace(0.01, 0.012, 30)
    out = validate_against(five_min(p, scale=1000.0), daily(p))
    assert out is not None and np.isclose(out["close"].iloc[-1], p[-1])


def test_validate_rejects_ticker_collision():
    p = np.linspace(100, 120, 30)
    assert validate_against(five_min(p * 3.7), daily(p)) is None


def test_oracle_prefers_finest_layer_and_never_looks_back():
    fine = pd.DataFrame({"t": T0 + 10 * D + np.arange(0, 12) * 300_000, "open": 2.0, "close": 2.0})
    coarse = pd.DataFrame({"t": T0 + np.arange(0, 20) * D, "open": 1.0, "close": 1.0})
    o = PriceOracle("X", [("fine", 300_000, fine), ("coarse", D, coarse)], coarse)
    px, tx, src = o.exec_price([T0 + 10 * D + 1, T0 + 5 * D + 1])
    assert px.tolist() == [2.0, 1.0]
    assert tx[0] == T0 + 10 * D + 300_000 and tx[1] == T0 + 6 * D  # first bar starting at or after T
