"""Static charts for the markdown report (one axis, thin lines, recessive grid)."""
from __future__ import annotations

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
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


def copy_curves(daily: pd.DataFrame, split, path: str, title: str) -> None:
    """Cumulative copy return (sum of daily returns, %) for each delay."""
    fig, ax = plt.subplots(figsize=(8, 3.6), dpi=130, facecolor=SURFACE)
    _style(ax)
    if "ret_ref" in daily:
        ax.plot(daily.index, daily["ret_ref"].cumsum() * 100, color=REF_COLOR, lw=1.5,
                label="imediata, sem custos (referência)")
    for d, c in DELAY_COLORS.items():
        col = f"ret_{d}"
        if col in daily:
            lab = f"{d} min" if d < 60 else f"{d // 60} h"
            ax.plot(daily.index, daily[col].cumsum() * 100, color=c, lw=2.0 if d == 30 else 1.5,
                    label=f"atraso de {lab}")
    ax.axvline(split, color=INK2, lw=1)
    ax.annotate("início do teste fora da amostra", xy=(split, 1), xycoords=("data", "axes fraction"),
                xytext=(4, -10), textcoords="offset points", fontsize=8, color=INK2, va="top")
    ax.axhline(0, color=INK2, lw=0.8)
    ax.set_ylabel("retorno acumulado da cópia (%)", color=INK2, fontsize=8)
    ax.set_title(title, color=INK, fontsize=10, loc="left")
    leg = ax.legend(fontsize=7.5, frameon=False, loc="upper left", ncol=2)
    for t in leg.get_texts():
        t.set_color(INK2)
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
