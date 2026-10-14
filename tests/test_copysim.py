import numpy as np
import pandas as pd
import pytest

from hlwf.copysim import position_events, prepare_fills, simulate

H = 3_600_000
D = 86_400_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)  # a UTC midnight
BAR = 300_000  # 5m


class LinearOracle:
    """Price = 100 + slope per hour, sampled on 5m bars."""

    def __init__(self, slope=1.0, start=T0 - 10 * D, end=T0 + 30 * D):
        self.slope = slope
        self.t = np.arange(start, end, BAR, dtype=np.int64)
        self.layers = [("fake", BAR, self.t, self.price(self.t))]

    def price(self, t):
        return 100.0 + self.slope * (np.asarray(t, float) - T0) / H

    def exec_price(self, T):
        T = np.asarray(T, dtype=np.int64)
        i = np.searchsorted(self.t, T, side="left")
        ok = i < len(self.t)
        i = np.minimum(i, len(self.t) - 1)
        px = np.where(ok, self.price(self.t[i]), np.nan)
        return px, np.where(ok, self.t[i], -1), np.zeros(len(T), dtype=np.int64)

    def daily_close(self):
        days = np.arange(T0 - 10 * D, T0 + 30 * D, D, dtype=np.int64)
        return pd.Series(self.price(days + D), index=days)


def fill(t, coin, side, sz, start):
    return {"time": t, "coin": coin, "side": side, "sz": str(sz), "px": "0", "startPosition": str(start),
            "closedPnl": "0", "fee": "0", "tid": t}


def run(fills, delay_ms, start=T0, end=T0 + 10 * D, cost=0.0, funding=None, equity=None, oracle=None):
    ev = position_events(prepare_fills(fills))
    o = oracle or LinearOracle()
    return simulate(ev, {c: o for c in ev}, delay_ms, start, end, cost_bps={c: cost for c in ev},
                    funding=funding, equity=equity)


def test_round_trip_pnl_uses_delayed_bar_prices():
    t_buy, t_sell = T0 + 2 * H + 60_000, T0 + 30 * H + 7_000
    res = run([fill(t_buy, "BTC", "B", 2, 0), fill(t_sell, "BTC", "A", 2, 2)], delay_ms=30 * 60_000)
    o = LinearOracle()
    buy_t = ((t_buy + 30 * 60_000 + BAR - 1) // BAR) * BAR
    sell_t = ((t_sell + 30 * 60_000 + BAR - 1) // BAR) * BAR
    expected = 2 * (o.price(sell_t) - o.price(buy_t))
    assert res.daily["pnl"].sum() == pytest.approx(expected, rel=1e-9)
    assert list(res.trades["t_exec"]) == [buy_t, sell_t]


def test_costs_are_charged_on_both_legs():
    t_buy, t_sell = T0 + 2 * H, T0 + 5 * H
    free = run([fill(t_buy, "ETH", "B", 1, 0), fill(t_sell, "ETH", "A", 1, 1)], 0)
    paid = run([fill(t_buy, "ETH", "B", 1, 0), fill(t_sell, "ETH", "A", 1, 1)], 0, cost=10.0)
    o = LinearOracle()
    notional = o.price(t_buy) + o.price(t_sell)
    assert free.daily["pnl"].sum() - paid.daily["pnl"].sum() == pytest.approx(notional * 10 / 1e4)


def test_fills_inside_one_bar_are_netted():
    t = T0 + 3 * H + 1_000  # all three inside the bar that opens at T0+3h05
    fills = [fill(t, "SOL", "B", 1, 0), fill(t + 10_000, "SOL", "B", 1, 1), fill(t + 20_000, "SOL", "A", 0.5, 2)]
    res = run(fills, 0)
    assert len(res.trades) == 1
    assert res.trades["q"].iloc[0] == pytest.approx(1.5)


def test_position_gap_is_booked_at_next_fill():
    # second fill starts at 3, not at 1: two units were added without a visible fill
    t1, t2 = T0 + H, T0 + 10 * H
    ev = position_events(prepare_fills([fill(t1, "BTC", "B", 1, 0), fill(t2, "BTC", "B", 1, 3)]))
    times, pos, pos0 = ev["BTC"]
    assert list(pos) == [1.0, 4.0] and pos0 == 0.0


def test_same_millisecond_fills_in_any_order():
    t = T0 + H
    # API order inside the millisecond is reversed: 2->3 listed before 0->2
    ev = position_events(prepare_fills([fill(t, "BTC", "B", 1, 2), fill(t, "BTC", "B", 2, 0)]))
    times, pos, pos0 = ev["BTC"]
    assert list(pos) == [3.0] and pos0 == 0.0


def test_open_position_at_start_is_copied():
    # wallet was already long 5 before the window and closes inside it
    res = run([fill(T0 + 5 * H, "BTC", "A", 5, 5)], 0, start=T0 + H)
    q = res.trades["q"].to_numpy()
    assert q[0] == pytest.approx(5) and q[-1] == pytest.approx(-5)
    o = LinearOracle()
    assert res.daily["pnl"].sum() == pytest.approx(5 * (o.price(T0 + 5 * H) - o.price(T0 + H)))


def test_funding_paid_by_long():
    t_buy, t_sell = T0 + H, T0 + 11 * H
    flat = LinearOracle(slope=0.0)
    rate = pd.Series(0.0001, index=np.arange(T0, T0 + 2 * D, H, dtype=np.int64))
    res = run([fill(t_buy, "BTC", "B", 1, 0), fill(t_sell, "BTC", "A", 1, 1)], 0,
              funding={"BTC": rate}, oracle=flat)
    # held through the hours T0+1h .. T0+10h inclusive: 10 payments of 1 * 100 * 1bp
    assert res.daily["funding"].sum() == pytest.approx(-10 * 100 * 0.0001)


def test_events_after_end_are_not_traded():
    res = run([fill(T0 + 9 * D + 23 * H + 50 * 60_000, "BTC", "B", 1, 0)], 30 * 60_000)
    assert res.trades.empty


def test_returns_use_equity():
    t_buy, t_sell = T0 + 2 * H, T0 + D + 2 * H
    eq = pd.Series([1000.0, 1000.0], index=[T0 - D, T0 + 20 * D])
    res = run([fill(t_buy, "BTC", "B", 1, 0), fill(t_sell, "BTC", "A", 1, 1)], 0, equity=eq)
    assert res.daily["ret"].sum() == pytest.approx(res.daily["pnl"].sum() / 1000.0)


def test_shift_hourly_matrix_shapes():
    res = run([fill(T0 + 2 * H, "BTC", "B", 1, 0), fill(T0 + 3 * D, "ETH", "A", 1, 0)], 0,
              equity=pd.Series([1e4], index=[T0]))
    assert set(res.hourly_expo.columns) == {"BTC", "ETH"}
    assert res.hourly_expo.shape == res.hourly_ret.shape


def test_position_without_fills_is_held_all_window():
    # an open position from the account state, never traded in the window
    ev = {"BTC": (np.array([], dtype=np.int64), np.array([]), 2.0)}
    o = LinearOracle()
    res = simulate(ev, {"BTC": o}, 30 * 60_000, T0 + H, T0 + 3 * D)
    assert res.trades["q"].tolist() == [2.0]
    entry = ((T0 + H + BAR - 1) // BAR) * BAR
    last_close = o.daily_close().reindex([T0 + 2 * D]).iloc[0]
    assert res.daily["pnl"].sum() == pytest.approx(2 * (last_close - o.price(entry)))


def test_daily_return_is_at_least_minus_100pct():
    # tiny equity and a losing round trip inside one day
    t = T0 + 2 * H
    res = run([fill(t, "BTC", "B", 10, 0), fill(t + 6 * H, "BTC", "A", 10, 10)], 0, end=T0 + 3 * D,
              equity=pd.Series([20.0], index=[T0]), oracle=LinearOracle(slope=-1.0))
    assert res.daily["ret"].min() >= -1.0


def test_round_trip_inside_a_day_counts_in_the_capital_floor():
    # end-of-day gross is zero, but the follower held ~200 of notional for six hours
    t = T0 + 2 * H
    res = run([fill(t, "BTC", "B", 2, 0), fill(t + 6 * H, "BTC", "A", 2, 2)], 0, equity=pd.Series([1.0], index=[T0]))
    day0 = res.daily.iloc[0]
    assert day0["gross"] == 0.0 and day0["capital"] >= 2 * 100 / 20


def test_position_jumps_are_listed():
    from hlwf.copysim import position_jumps
    # the second fill starts at 3: two units moved without a visible fill
    j = position_jumps(prepare_fills([fill(T0 + H, "BTC", "B", 1, 0), fill(T0 + 10 * H, "BTC", "B", 1, 3)]))
    assert len(j) == 1 and j["size"].iloc[0] == pytest.approx(2.0) and j["time"].iloc[0] == T0 + 10 * H
    assert position_jumps(prepare_fills([fill(T0 + H, "BTC", "B", 1, 0), fill(T0 + 2 * H, "BTC", "B", 1, 1)])).empty
