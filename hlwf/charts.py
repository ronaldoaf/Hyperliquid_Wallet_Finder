"""Static charts for the markdown report (one axis, thin lines, recessive grid)."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e4e3df"
# ordinal blue ramp: the 30-min copy is the accent, longer delays step lighter
DELAY_COLORS = {30: "#2a78d6", 60: "#5598e7", 120: "#6da7ec", 240: "#86b6ef"}
REF_COLOR = "#8f8e89"
GOOD = "#2a78d6"
BAD = "#e34948"


def _style(ax):
    ax.set_facecolor(SURFACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.set_axisbelow(True)


def equity_pct(ret: pd.Series) -> pd.Series:
    """Compounded equity of the follower, in % (a day below -100% is a wipe-out)."""
    return (np.exp(np.log1p(ret.clip(lower=-0.9999)).cumsum()) - 1) * 100


def copy_curves(daily: pd.DataFrame, split, path: str, title: str, main: int = 30) -> None:
    """Compounded copy return (%) for each delay over the test period only
    (from the split), so the curve ends where the card's compounded return is."""
    daily = daily[daily.index >= pd.Timestamp(split).floor("D")]
    fig, ax = plt.subplots(figsize=(8, 3.8), dpi=130, facecolor=SURFACE)
    _style(ax)
    if "ret_ref" in daily:
        ax.plot(daily.index, equity_pct(daily["ret_ref"]), color=REF_COLOR, lw=1.5,
                label="imediata, sem custos (referência)")
    found = sorted(int(c[4:]) for c in daily.columns if c.startswith("ret_") and c[4:].isdigit())
    ramp = list(DELAY_COLORS.values())
    for i, d in enumerate(found):  # ordinal blue ramp, shortest delay darkest
        lab = f"{d} min" if d < 60 or d % 60 else f"{d // 60} h"
        ax.plot(daily.index, equity_pct(daily[f"ret_{d}"]), color=ramp[min(i, len(ramp) - 1)],
                lw=2.0 if d == main else 1.5, label=f"atraso de {lab}")
    loc = mdates.AutoDateLocator(minticks=4, maxticks=8)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc, show_offset=False))
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_ylabel("retorno composto da cópia no teste (%)", color=INK2, fontsize=8)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    leg = ax.legend(fontsize=7.5, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=5,
                    handlelength=1.5, columnspacing=1.2)
    for t in leg.get_texts():
        t.set_color(INK2)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
