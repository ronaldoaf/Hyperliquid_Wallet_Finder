import numpy as np
import pytest

from hlwf import wallet as wl
from hlwf.api import InfoClient
from hlwf.store import load_gz, save_gz

D = 86_400_000
T0 = 1_780_000_000_000 - (1_780_000_000_000 % D)
NOW = T0 + 100 * D


class FakeAPI(InfoClient):
    """userFillsByTime / userTwapSliceFillsByTime as the exchange serves them:
    the oldest 2000 rows of the window, and only the history from `served_from` on."""

    def __init__(self, fills, served_from=0, twap=(), positions=None, dex_positions=None, funding=()):
        super().__init__(budget=10**9)
        self.funding = list(funding)
        self.fills = sorted(fills, key=lambda f: f["time"])
        self.served_from = served_from
        self.twap = list(twap)
        self.positions = positions or {}
        self.dex_positions = dex_positions or {}
        self.asked = []

    def post(self, body, retries=1):
        self.asked.append(body)
        kind = body["type"]
        if kind == "perpDexs":
            return [None, {"name": "xyz"}]
        if kind == "clearinghouseState":
            pos = self.dex_positions if body.get("dex") else self.positions
            return {"time": NOW, "marginSummary": {"accountValue": "1000"},
                    "assetPositions": [{"position": {"coin": c, "szi": str(s), "positionValue": str(abs(s) * 100)}}
                                       for c, s in pos.items()]}
        a, b = body["startTime"], body.get("endTime", NOW)
        if kind == "userFillsByTime":
            rows = [f for f in self.fills if f["time"] >= self.served_from and a <= f["time"] <= b]
            if body.get("aggregateByTime"):  # one row per order and millisecond
                agg = {}
                for f in rows:
                    k = (f["time"], f["oid"])
                    if k in agg:
                        agg[k] = {**agg[k], "sz": str(float(agg[k]["sz"]) + float(f["sz"]))}
                    else:
                        agg[k] = dict(f)
                rows = list(agg.values())
            return rows[:2000]
        if kind == "userFunding":
            return [r for r in self.funding if a <= r["time"] <= b][:500]
        if kind == "userTwapSliceFillsByTime":
            return [x for x in self.twap if a <= x["fill"]["time"] <= b][:2000]
        raise ValueError(kind)


def fill(t, k, coin="BTC", start=None):
    return {"time": t, "coin": coin, "side": "B", "sz": "1", "px": "100", "startPosition": str(k if start is None else start),
            "tid": k, "oid": k}


def series(n_days=100, per_day=50, first_day=0):
    return [fill(T0 + d * D + i * 60_000, d * per_day + i) for d in range(first_day, n_days) for i in range(per_day)]


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    monkeypatch.setattr(wl.time, "time", lambda: NOW / 1000)


def test_download_walks_every_page(tmp_path):
    # 5,000 fills, 2,000 per page: the walk must not stop after the test window starts
    c = FakeAPI(series())
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), need_from=T0 + 40 * D)
    assert len(rec["fills"]) == 5000
    assert rec["coverage"] == T0 and rec["complete_to"] == NOW and rec["v"] == wl.FORMAT


def test_coverage_starts_where_the_served_history_starts(tmp_path):
    # the API no longer serves the first 10 days: nothing says the wallet was idle then
    c = FakeAPI(series(), served_from=T0 + 10 * D)
    rec = wl.fetch_wallet(c, "0xabc", T0 - 5 * D, str(tmp_path))
    assert rec["coverage"] == T0 + 10 * D


def test_quiet_start_is_covered_when_older_fills_are_served(tmp_path):
    # no fill in the first 20 days of the window, but the API serves older ones
    fills = [fill(T0 - 30 * D, 0)] + series(first_day=20)
    rec = wl.fetch_wallet(FakeAPI(fills), "0xabc", T0, str(tmp_path))
    assert rec["coverage"] == T0


def test_window_that_cannot_be_covered_stops_at_the_first_page(tmp_path):
    c = FakeAPI(series(), served_from=T0 + 50 * D)  # 2,500 fills served, more than one page
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), need_from=T0 + 40 * D)
    pages = [b for b in c.asked if b["type"] == "userFillsByTime" and b["startTime"] > 0]
    assert len(pages) == 1 and rec["coverage"] > T0 + 40 * D and rec["complete_to"] < NOW


def test_extension_keeps_coverage_without_a_hole(tmp_path, monkeypatch):
    c = FakeAPI(series(60))
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path))
    c.fills = series(100)
    monkeypatch.setattr(wl.time, "time", lambda: (NOW + D) / 1000)
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), max_age_h=1)
    assert rec["coverage"] == T0 and len(rec["fills"]) == 5000 and rec["complete_to"] == NOW + D


def test_extension_finds_the_hole_after_the_cache(tmp_path, monkeypatch):
    c = FakeAPI(series(60))
    wl.fetch_wallet(c, "0xabc", T0, str(tmp_path))
    # by the next run the API has forgotten everything before day 70
    c.fills, c.served_from = series(100), T0 + 70 * D
    monkeypatch.setattr(wl.time, "time", lambda: (NOW + D) / 1000)
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), max_age_h=1)
    assert rec["coverage"] == T0 + 70 * D


def test_record_younger_than_max_age_but_short_of_need_to_is_extended(tmp_path, monkeypatch):
    c = FakeAPI(series(60))
    wl.fetch_wallet(c, "0xabc", T0, str(tmp_path))
    n = len(c.asked)
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), need_to=NOW)
    assert len(c.asked) == n  # complete up to now: reused
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path), need_to=NOW + 1)
    assert len(c.asked) > n


def test_record_in_the_old_format_is_downloaded_again(tmp_path):
    # the old cache stopped early (the bug) and claimed complete coverage
    path = tmp_path / "wallets" / "0xabc" / "fills.json.gz"
    save_gz(str(path), {"fills": series(45), "coverage": T0, "asked_from": T0, "fetched_at": NOW, "state": None})
    rec = wl.fetch_wallet(FakeAPI(series()), "0xabc", T0, str(tmp_path))
    assert len(rec["fills"]) == 5000 and rec["complete_to"] == NOW and rec["v"] == wl.FORMAT


def test_old_cache_joins_a_shorter_served_history(tmp_path):
    # the cache has days 0-59; the API now serves only from day 30: together they cover day 0 on
    path = tmp_path / "wallets" / "0xabc" / "fills.json.gz"
    old = {"v": wl.FORMAT, "fills": series(60), "coverage": T0, "complete_to": T0 + 60 * D, "asked_from": T0 + D,
           "fetched_at": T0 + 60 * D, "twap_to": T0 + 60 * D, "state": None}
    save_gz(str(path), old)
    rec = wl.fetch_wallet(FakeAPI(series(), served_from=T0 + 30 * D), "0xabc", T0, str(tmp_path))
    assert rec["coverage"] == T0 and len(rec["fills"]) == 5000


def test_twap_slices_are_merged(tmp_path):
    slices = [{"fill": {**fill(T0 + 5 * D + 7, 10**6, start=999), "hash": "0x0"}, "twapId": 42}]
    rec = wl.fetch_wallet(FakeAPI(series(), twap=slices), "0xabc", T0, str(tmp_path))
    tw = [f for f in rec["fills"] if f.get("twapId") == 42]
    assert len(tw) == 1 and len(rec["fills"]) == 5001


def test_positions_of_every_dex_are_read(tmp_path):
    c = FakeAPI(series(10), positions={"BTC": 2.0}, dex_positions={"xyz:TSLA": -3.0})
    rec = wl.fetch_wallet(c, "0xabc", T0, str(tmp_path))
    assert wl.all_open_positions(rec) == {"BTC": 2.0, "xyz:TSLA": -3.0}
    assert rec["dexes_checked"] == ["xyz"]
    assert load_gz(str(tmp_path / "wallets" / "0xabc" / "fills.json.gz"))["dex_states"]["xyz"]


def test_first_page_stop_is_checked_on_the_first_page_only():
    c = FakeAPI(series())
    calls = []

    def stop(page):
        calls.append(page[0]["time"])
        return False

    st = {}
    rows = c.paginate_time({"type": "userFillsByTime", "user": "0xabc"}, T0, NOW, key=wl.fill_key,
                           page_limit=2000, first_page_stop=stop, status=st)
    assert len(rows) == 5000 and len(calls) == 1 and st == {"complete": True, "complete_to": NOW}


def test_max_pages_reports_where_the_walk_stopped():
    st = {}
    rows = FakeAPI(series()).paginate_time({"type": "userFillsByTime", "user": "0xabc"}, T0, NOW, key=wl.fill_key,
                                           page_limit=2000, max_pages=1, status=st)
    assert not st["complete"] and st["complete_to"] == max(f["time"] for f in rows) - 1
    assert np.all(np.diff([f["time"] for f in rows]) >= 0)


def test_sweep_of_one_millisecond_is_not_truncated(tmp_path):
    # one market order fills against 2,500 makers in the same millisecond
    sweep = [{**fill(T0 + 5 * D, 1000 + i, start=100 + i), "oid": 999} for i in range(2500)]
    fills = series(n_days=5, per_day=20) + sweep + [fill(T0 + 6 * D, 5000, start=2600)]
    rec = wl.fetch_wallet(FakeAPI(fills), "0xabc", T0, str(tmp_path))
    assert rec["complete_to"] == NOW
    at = [f for f in rec["fills"] if f["time"] == T0 + 5 * D]
    assert len(at) == 1 and float(at[0]["sz"]) == 2500.0  # the whole sweep, aggregated by order


def test_extend_starts_where_the_cache_is_complete(tmp_path):
    # a record complete only up to day 54 also holds fills of days 60-70 (joined from an older
    # cache); extending must fetch days 55-59 instead of resuming after day 70
    all_fills = series(n_days=100, per_day=10)
    head = [f for f in all_fills if f["time"] < T0 + 55 * D]
    tail = [f for f in all_fills if T0 + 60 * D <= f["time"] < T0 + 70 * D]
    rec = {"v": wl.FORMAT, "fills": head + tail, "coverage": T0, "complete_to": T0 + 55 * D - 1,
           "asked_from": T0, "fetched_at": NOW - 10 * D, "twap_to": T0}
    new = wl.extend(FakeAPI(all_fills), "0xabc", rec, NOW)
    got = {f["tid"] for f in new["fills"]}
    assert {f["tid"] for f in all_fills} <= got and new["coverage"] == T0
