"""Market data for the copy simulation: execution prices, daily marks,
funding rates and a per-coin trading cost.

Hyperliquid's `candleSnapshot` serves only the latest 5000 candles of each
interval (5m ~ 17 days, 15m ~ 52 days, 1h ~ 7 months, 4h ~ 2.3 years), so
older intraday prices come from Binance's public archive of USDT-margined
futures (5m klines), checked against Hyperliquid's own daily closes before
use. Every download is cached under data/market/ and merged with what is
already there, so reruns extend the history instead of replacing it.
"""
from __future__ import annotations

import io
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests

from .api import InfoClient
from .store import load_gz, save_gz

INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
BINANCE = "https://data.binance.vision/data/futures/um"


def safe(coin: str) -> str:
    return coin.replace(":", "_").replace("/", "_")


def binance_symbol(coin: str) -> str | None:
    if ":" in coin or coin.startswith("@") or "/" in coin:
        return None
    if coin.startswith("k") and len(coin) > 1 and coin[1:].isupper():
        return f"1000{coin[1:]}USDT"  # kPEPE -> 1000PEPEUSDT
    return f"{coin}USDT"


class Market:
    def __init__(self, client: InfoClient | None = None, data_dir: str = "data", use_binance: bool = True,
                 verbose: bool = True):
        self.c = client or InfoClient()
        self.dir = os.path.join(data_dir, "market")
        self.use_binance = use_binance
        self.verbose = verbose
        self._ctx: dict | None = None
        self._oracles: dict = {}
        self._candles: dict = {}   # refreshed once per process
        self._funding: dict = {}
        self.http = requests.Session()

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    # ------------------------------------------------------------ asset info
    def contexts(self) -> dict:
        """Current 24h volume and impact spread per perp (main dex + HIP-3 dexes)."""
        if self._ctx is not None:
            return self._ctx
        path = os.path.join(self.dir, "contexts.json.gz")
        cached = load_gz(path)
        if cached and time.time() - cached["fetched_at"] / 1000 < 24 * 3600:
            self._ctx = cached["ctx"]
            return self._ctx
        ctx = {}
        dexes = [None]
        try:
            dexes += [d["name"] for d in self.c.post({"type": "perpDexs"}) if d]
        except Exception:
            pass
        for dex in dexes:
            body = {"type": "metaAndAssetCtxs"} | ({"dex": dex} if dex else {})
            try:
                meta, ctxs = self.c.post(body)
            except Exception:
                continue
            for u, x in zip(meta["universe"], ctxs):
                mid = float(x.get("midPx") or x.get("markPx") or 0) or None
                imp = x.get("impactPxs")
                spread = (float(imp[1]) - float(imp[0])) / mid if (imp and mid) else None
                ctx[u["name"]] = {"vlm": float(x.get("dayNtlVlm") or 0), "spread": spread,
                                  "delisted": bool(u.get("isDelisted")), "hip3": dex is not None}
        save_gz(path, {"fetched_at": int(time.time() * 1000), "ctx": ctx})
        self._ctx = ctx
        return ctx

    def cost_bps(self, coin: str, taker_bps: float = 4.5, hip3_fee_mult: float = 2.0) -> float:
        """One-way cost of a copier's market order: taker fee + half spread + impact.

        The spread/impact part comes from today's 24h volume (a liquidity tier)
        or today's impact spread, whichever is larger; delisted or unknown
        coins get the worst tier.
        """
        x = self.contexts().get(coin)
        fee = taker_bps * (hip3_fee_mult if (":" in coin) else 1.0)
        if not x or x["delisted"]:
            return fee + 25.0
        v = x["vlm"]
        tier = 1.0 if v >= 1e9 else 2.0 if v >= 1e8 else 5.0 if v >= 2e7 else 10.0 if v >= 2e6 else 25.0
        half = (x["spread"] or 0.0) / 2 * 1e4
        return fee + max(tier, half)

    # ---------------------------------------------------------------- candles
    def candles(self, coin: str, interval: str, start_ms: int | None = None) -> pd.DataFrame:
        """Candles from `start_ms` (default: as far back as the API serves, 5000
        bars) to now. Only the missing part is downloaded: candle requests weigh
        one unit per 60 bars, so asking for 5000 bars when 200 are needed is
        the difference between 10 and 50 coins a minute."""
        step = INTERVAL_MS[interval]
        now = int(time.time() * 1000)
        lo = max(start_ms if start_ms is not None else 0, now - 5000 * step)
        memo = self._candles.get((coin, interval))
        if memo is not None and memo[0] <= lo:
            return memo[1]
        path = os.path.join(self.dir, "candles", f"{safe(coin)}_{interval}.json.gz")
        rows = {r[0]: r for r in (load_gz(path) or [])}
        spans = []
        if not rows:
            spans.append(lo)
        else:
            if min(rows) > lo + step:
                spans.append(lo)  # older part missing (refetching the rest is cheap)
            elif now - max(rows) > max(step, 30 * 60_000):
                spans.append(max(rows) - step)
        for a in spans:
            try:
                page = self.c.post({"type": "candleSnapshot",
                                    "req": {"coin": coin, "interval": interval, "startTime": int(a), "endTime": now}})
            except Exception as e:
                self.log(f"  candles {coin} {interval}: {e}")
                page = []
            for r in page or []:
                rows[int(r["t"])] = [int(r["t"]), float(r["o"]), float(r["c"]), float(r.get("v") or 0)]
        if spans:
            save_gz(path, [rows[k] for k in sorted(rows)])
        df = pd.DataFrame([rows[k] for k in sorted(rows)], columns=["t", "open", "close", "volume"])
        self._candles[(coin, interval)] = (lo, df)
        return df

    # ---------------------------------------------------------------- binance
    def _binance_file(self, url: str, path: str) -> pd.DataFrame | None:
        if os.path.exists(path):
            return pd.read_csv(path)
        if os.path.exists(path + ".missing"):
            return None
        try:
            r = self.http.get(url, timeout=120)
        except requests.RequestException:
            return None
        if r.status_code != 200:
            if r.status_code == 404:
                open(path + ".missing", "w").close()
            return None
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            raw = z.read(z.namelist()[0]).decode()
        first = raw.split("\n", 1)[0]
        df = pd.read_csv(io.StringIO(raw), header=0 if first.startswith("open_time") else None)
        df = df.iloc[:, [0, 1, 4]]
        df.columns = ["t", "open", "close"]
        df.to_csv(path, index=False, compression="gzip")
        return df

    def binance_5m(self, coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """5m klines of the matching Binance USDT-M perp, monthly files for past
        months and daily files for the current one (up to yesterday)."""
        empty = pd.DataFrame(columns=["t", "open", "close"])
        sym = binance_symbol(coin)
        if not sym or not self.use_binance:
            return empty
        d = os.path.join(self.dir, "binance5m", sym)
        os.makedirs(d, exist_ok=True)
        now = pd.Timestamp.now("UTC").tz_localize(None)
        yday = (now - pd.Timedelta(days=1)).floor("D")
        marker = os.path.join(d, "NOSYMBOL")
        if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < 7 * 86400:
            return empty
        # listed today? (yesterday's daily file exists)
        probe = self._binance_file(f"{BINANCE}/daily/klines/{sym}/5m/{sym}-5m-{yday:%Y-%m-%d}.zip",
                                   os.path.join(d, f"{yday:%Y-%m-%d}.csv.gz"))
        if probe is None:
            open(marker, "w").close()
            return empty
        start = pd.Timestamp(start_ms, unit="ms")
        end = min(pd.Timestamp(end_ms, unit="ms"), yday)
        cur = now.to_period("M")
        months = [m for m in pd.period_range(start.to_period("M"), end.to_period("M"), freq="M") if m < cur]
        with ThreadPoolExecutor(8) as ex:  # the archive is a CDN: no API weight, latency bound
            got = list(ex.map(lambda m: self._binance_file(
                f"{BINANCE}/monthly/klines/{sym}/5m/{sym}-5m-{m}.zip", os.path.join(d, f"{m}.csv.gz")), months))
        frames = [x for x in got if x is not None]
        # current month, or last month's file not published yet: daily files
        daily_months = [cur] + [m for m, x in zip(months, got) if x is None and m == cur - 1]
        days = [day for m in daily_months
                for day in pd.date_range(max(m.start_time, start.floor("D")), min(m.end_time, end), freq="D")]
        with ThreadPoolExecutor(8) as ex:
            got = list(ex.map(lambda day: self._binance_file(
                f"{BINANCE}/daily/klines/{sym}/5m/{sym}-5m-{day:%Y-%m-%d}.zip",
                os.path.join(d, f"{day:%Y-%m-%d}.csv.gz")), days))
        frames += [x for x in got if x is not None]
        if not frames:
            return empty
        df = pd.concat(frames).drop_duplicates("t").sort_values("t")
        return df[(df["t"] >= start_ms - DAY_MS) & (df["t"] <= end_ms + DAY_MS)].reset_index(drop=True)

    # ---------------------------------------------------------------- funding
    def funding(self, coin: str, start_ms: int, end_ms: int | None = None) -> pd.Series:
        """Hourly funding rate (positive: longs pay shorts), indexed by ms.
        The cache remembers which span it has covered, so a coin listed after
        `start_ms` is not asked again for the hours before its listing."""
        mem = self._funding.get(coin)
        if mem is not None and mem[0] <= start_ms and mem[1] >= (end_ms or 0):
            return mem[2]
        path = os.path.join(self.dir, "funding", f"{safe(coin)}.json.gz")
        cached = load_gz(path) or {"lo": None, "hi": None, "rows": []}
        rows = {r[0]: r[1] for r in cached["rows"]}
        lo, hi = cached["lo"], cached["hi"]
        end_ms = end_ms or int(time.time() * 1000)
        spans = []
        if lo is None:
            spans.append((start_ms, end_ms))
        else:
            if start_ms < lo:
                spans.append((start_ms, lo))
            if end_ms > hi + HOUR_MS:
                spans.append((hi, end_ms))
        for a, b in spans:
            try:
                new = self.c.paginate_time({"type": "fundingHistory", "coin": coin}, a, b,
                                           key=lambda x: x["time"], page_limit=500)
            except Exception as e:
                self.log(f"  funding {coin}: {e}")
                continue
            for r in new:
                rows[int(r["time"])] = float(r["fundingRate"])
            lo = a if lo is None else min(lo, a)
            hi = b if hi is None else max(hi, b)
        if spans:
            save_gz(path, {"lo": lo, "hi": hi, "rows": [[k, rows[k]] for k in sorted(rows)]})
        out = pd.Series(rows, dtype=float).sort_index()
        if lo is not None:
            self._funding[coin] = (lo, hi, out)
        return out

    # ---------------------------------------------------------------- oracle
    def oracle(self, coin: str, start_ms: int, end_ms: int | None = None) -> "PriceOracle":
        """Price layers for `coin` from `start_ms`, finest first: Hyperliquid
        5m (recent), Binance 5m (validated), then Hyperliquid 15m/1h/4h only if
        the 5m layers leave the start uncovered, and daily closes for marks."""
        key = (coin, start_ms // DAY_MS)
        if key in self._oracles:
            return self._oracles[key]
        end_ms = end_ms or int(time.time() * 1000)
        daily = self.candles(coin, "1d", start_ms - 3 * DAY_MS)
        layers = []
        bn = None
        if self.use_binance:
            bn = validate_against(self.binance_5m(coin, start_ms - DAY_MS, end_ms), daily)
        if bn is not None and len(bn):
            # Binance covers up to yesterday: Hyperliquid 5m only for the last hours
            hl5 = self.candles(coin, "5m", int(bn["t"].iloc[-1]) - HOUR_MS)
            if len(hl5):
                layers.append(("hl5m", INTERVAL_MS["5m"], hl5))
            layers.append(("binance5m", INTERVAL_MS["5m"], bn))
        # No Binance (HIP-3, Hyperliquid-only coins): 15m for the last 52 days and
        # 1h before that. The follower then trades up to one bar later than the
        # nominal delay, which only makes the test harder to pass.
        firsts = [int(x["t"].iloc[0]) for _, _, x in layers if len(x)]
        covered = min(firsts) if firsts else end_ms
        for iv in ("15m", "1h", "4h"):
            if covered <= start_ms + INTERVAL_MS[iv]:
                break
            df = self.candles(coin, iv, start_ms)
            if len(df):
                layers.append((f"hl{iv}", INTERVAL_MS[iv], df))
                covered = min(covered, int(df["t"].iloc[0]))
        if len(daily):
            layers.append(("hl1d", INTERVAL_MS["1d"], daily))
        o = PriceOracle(coin, layers, daily)
        self._oracles[key] = o
        return o


def validate_against(bn: pd.DataFrame, daily: pd.DataFrame, tol: float = 0.02) -> pd.DataFrame | None:
    """Accept Binance prices only if their daily closes match Hyperliquid's
    (fixing a 1000x unit difference); a ticker collision is rejected."""
    if bn is None or bn.empty or daily.empty:
        return None
    b = bn.copy()
    b["day"] = (b["t"] // DAY_MS) * DAY_MS
    bclose = b.groupby("day")["close"].last()
    h = daily.set_index("t")["close"]
    j = pd.concat([h, bclose], axis=1, keys=["hl", "bn"]).dropna()
    j = j.iloc[:-1] if len(j) > 1 else j  # today's candle is partial
    if len(j) < 5:
        return None
    ratio = float((j["hl"] / j["bn"]).median())
    for k in (1.0, 1000.0, 0.001):
        if abs(ratio / k - 1) < tol:
            if k != 1.0:
                bn = bn.copy()
                bn[["open", "close"]] *= k
            dev = (j["hl"] / (j["bn"] * k) - 1).abs()
            if float(dev.quantile(0.9)) > 0.05:
                return None
            return bn[["t", "open", "close"]]
    return None


class PriceOracle:
    """Price at which a copier who acts at time T trades: the open of the
    first bar that starts at or after T, from the finest series that has one
    (so the effective delay is the nominal one plus up to one bar)."""

    def __init__(self, coin: str, layers: list, daily: pd.DataFrame):
        self.coin = coin
        self.layers = [(name, step, d["t"].to_numpy(np.int64), d["open"].to_numpy(float)) for name, step, d in layers]
        self.daily = daily

    def exec_price(self, T) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        T = np.asarray(T, dtype=np.int64)
        px = np.full(len(T), np.nan)
        tx = np.full(len(T), -1, dtype=np.int64)
        src = np.full(len(T), -1, dtype=np.int64)
        todo = np.ones(len(T), dtype=bool)
        for li, (_, step, t, o) in enumerate(self.layers):
            if not todo.any() or len(t) == 0:
                continue
            idx = np.searchsorted(t, T[todo], side="left")
            ok = idx < len(t)
            idx_c = np.minimum(idx, len(t) - 1)
            ok &= (t[idx_c] - T[todo]) < step  # the series covers T (no gap)
            sel = np.where(todo)[0][ok]
            px[sel] = o[idx_c[ok]]
            tx[sel] = t[idx_c[ok]]
            src[sel] = li
            todo[sel] = False
        return px, tx, src

    def daily_close(self) -> pd.Series:
        d = self.daily
        return pd.Series(d["close"].to_numpy(float), index=d["t"].to_numpy(np.int64))
