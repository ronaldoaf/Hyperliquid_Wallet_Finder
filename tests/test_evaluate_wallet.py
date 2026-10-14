"""evaluate_wallet end to end, with a fake market and a fake fills record."""
import numpy as np
import pandas as pd
import pytest

from hlwf import evaluate as ev
from hlwf.evaluate import Settings, evaluate_wallet

H = 3_600_000
D = 86_400_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)
SPLIT = T0 + 40 * D
END = T0 + 110 * D
BAR = 300_000


class WaveOracle:
    """Price oscillating around 100 on 5m bars."""

    def __init__(self):
        self.t = np.arange(T0 - 60 * D, END + 2 * D, BAR, dtype=np.int64)

    def price(self, t):
        return 100.0 + 10.0 * np.sin(np.asarray(t, float) / (3 * D))

    def exec_price(self, T):
        T = np.asarray(T, dtype=np.int64)
        i = np.searchsorted(self.t, T, side="left")
        ok = i < len(self.t)
        i = np.minimum(i, len(self.t) - 1)
        return np.where(ok, self.price(self.t[i]), np.nan), np.where(ok, self.t[i], -1), np.zeros(len(T), dtype=np.int64)

    def daily_close(self):
        days = np.arange(T0 - 60 * D, END + D, D, dtype=np.int64)
        return pd.Series(self.price(days + D), index=days)


class FakeMarket:
    def oracle(self, coin, start_ms, end_ms=None):
        return WaveOracle()

    def cost_bps(self, coin, taker_bps=4.5, hip3_fee_mult=2.0):
        return 5.0

    def funding(self, coin, start_ms, end_ms=None):
        return pd.Series(dtype=float)

    def candles(self, coin, interval, start_ms=None):
        days = np.arange(T0 - 60 * D, END + D, D, dtype=np.int64)
        return pd.DataFrame({"t": days, "open": 100.0, "close": WaveOracle().price(days + D), "volume": 1.0})


def trades(start, end, coin="BTC", jump_every=None):
    """Buy 1 and sell it two days later, every three days; optionally a
    position change with no fill before every `jump_every`-th trade."""
    out, pos, k, n = [], 0.0, 0, 0
    for t in range(start + 2 * H, end - 3 * D, 3 * D):
        for side, dt in (("B", 0), ("A", 2 * D)):
            n += 1
            if jump_every and n % jump_every == 0:
                pos += 1.0  # moved without a visible fill
            q = 1.0 if side == "B" else -1.0
            out.append({"time": t + dt, "coin": coin, "side": side, "sz": "1", "px": "100", "startPosition": str(pos),
                        "tid": k, "oid": k, "closedPnl": "0", "fee": "0"})
            pos += q
            k += 1
    return out, pos


def record(fills, coverage, complete_to=END + H, positions=None, asked_from=None):
    state = {"time": END + H, "marginSummary": {"accountValue": "10000"},
             "assetPositions": [{"position": {"coin": c, "szi": str(s), "positionValue": str(abs(s) * 100)}}
                                for c, s in (positions or {}).items()]}
    return {"v": 2, "fills": fills, "coverage": coverage, "complete_to": complete_to,
            "asked_from": asked_from or coverage, "fetched_at": END + H, "state": state, "dex_states": {},
            "dexes_checked": []}


def run(monkeypatch, tmp_path, rec, **kw):
    monkeypatch.setattr(ev, "fetch_wallet", lambda *a, **k: rec)
    cfg = Settings(split_ms=SPLIT, end_ms=END, **kw)
    return evaluate_wallet("0xabc", cfg, FakeMarket(), None, str(tmp_path))[0]


def test_insample_window_follows_the_settings_not_the_cache(monkeypatch, tmp_path):
    # the cache was downloaded for a 90-day in-sample run; this run asks for 30
    fills, pos = trades(SPLIT - 90 * D, END)
    s = run(monkeypatch, tmp_path, record(fills, SPLIT - 90 * D, positions={"BTC": pos}))
    assert s["eval_start"] == SPLIT - 30 * D
    assert s["is_30"]["days"] == 30


def test_fills_short_of_the_end_are_insufficient(monkeypatch, tmp_path):
    fills, pos = trades(SPLIT - 30 * D, END)
    s = run(monkeypatch, tmp_path, record(fills, SPLIT - 30 * D, complete_to=END - 5 * D, positions={"BTC": pos}))
    assert s["status"] == "insuficiente" and "fills incompletos" in s["reason"]


def test_position_moving_without_fills_is_insufficient(monkeypatch, tmp_path):
    fills, pos = trades(SPLIT - 30 * D, END, jump_every=4)
    s = run(monkeypatch, tmp_path, record(fills, SPLIT - 30 * D, positions={"BTC": pos}))
    assert s["status"] == "insuficiente" and "sem fills visíveis" in s["reason"]
    assert s["jump_share"] > 0.02


def test_position_never_traded_is_held_from_the_start(monkeypatch, tmp_path):
    fills, pos = trades(SPLIT - 30 * D, END)
    s = run(monkeypatch, tmp_path, record(fills, SPLIT - 30 * D, positions={"BTC": pos, "ETH": -4.0}))
    assert "ETH" in s["coins"] and s.get("status") is None
    assert s["recon"]["share"] == pytest.approx(0.0, abs=1e-9)
    assert any(x["coin"] == "ETH" for x in s["by_coin"])


def test_book_that_does_not_end_where_the_account_is_is_insufficient(monkeypatch, tmp_path):
    fills, pos = trades(SPLIT - 30 * D, END)
    # the fills end flat in BTC, the account says long 5
    s = run(monkeypatch, tmp_path, record(fills, SPLIT - 30 * D, positions={"BTC": pos + 5.0}))
    assert s["status"] == "insuficiente" and "não batem" in s["reason"]
    assert s["recon"]["coins"][0]["coin"] == "BTC"


def test_fills_before_a_hole_are_ignored(monkeypatch, tmp_path):
    # fills before the coverage start (separated from it by a hole) must not set the opening position
    early = [{"time": SPLIT - 60 * D, "coin": "BTC", "side": "B", "sz": "7", "px": "100", "startPosition": "0",
              "tid": -1, "oid": -1, "closedPnl": "0", "fee": "0"}]
    fills, pos = trades(SPLIT - 30 * D, END)
    s = run(monkeypatch, tmp_path, record(early + fills, SPLIT - 30 * D, positions={"BTC": pos}))
    assert s["n_fills"] == len(fills) and s.get("status") is None
