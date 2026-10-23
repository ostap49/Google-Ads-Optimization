"""matplotlib charts for client reports. One measure per chart (no dual axes), recessive grid, thin marks."""
from __future__ import annotations

from pathlib import Path
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
SERIES = "#2a78d6"     # categorical slot 1
TARGET = "#7a7975"     # neutral reference line


def _axes(title: str, figsize=(8, 3.2)):
    fig, ax = plt.subplots(figsize=figsize, dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold", pad=10)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    return fig, ax


def _save(fig, path: Path) -> str:
    fig.tight_layout()
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return str(path)


def daily_bars(days: list[str], values: list[float], title: str, path: Path) -> str:
    fig, ax = _axes(title)
    xs = list(range(len(days)))
    ax.bar(xs, values, width=0.78, color=SERIES, linewidth=0)
    step = max(1, len(days) // 8)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([d[5:] for d in days[::step]])
    ax.set_xlim(-0.6, len(days) - 0.4)
    return _save(fig, path)


def daily_line(days: list[str], values: list[float | None], title: str, path: Path,
               target: float | None = None, target_label: str = "") -> str:
    fig, ax = _axes(title)
    xs = list(range(len(days)))
    ax.plot(xs, values, color=SERIES, linewidth=2, solid_capstyle="round")
    pts = [(x, v) for x, v in zip(xs, values) if v is not None]
    if pts:
        ax.plot([pts[-1][0]], [pts[-1][1]], "o", color=SERIES, markersize=5, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(f"{pts[-1][1]:,.2f}".replace(",", " "), pts[-1], textcoords="offset points", xytext=(6, 4),
                    color=INK, fontsize=8)
    if target:
        ax.axhline(target, color=TARGET, linewidth=1, linestyle=(0, (4, 3)))
        ax.annotate(target_label, (0, target), textcoords="offset points", xytext=(2, 4), color=INK_2, fontsize=8)
    step = max(1, len(days) // 8)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([d[5:] for d in days[::step]])
    ax.set_ylim(bottom=0)
    return _save(fig, path)


def campaign_bars(names: list[str], costs: list[float], labels: list[str], title: str, path: Path) -> str:
    n = len(names)
    fig, ax = _axes(title, figsize=(8, 0.45 * n + 1.2))
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.grid(axis="y", visible=False)
    ys = list(range(n))[::-1]
    ax.barh(ys, costs, height=0.72, color=SERIES, linewidth=0)
    ax.set_yticks(ys)
    ax.set_yticklabels([x if len(x) <= 38 else x[:36] + "…" for x in names], color=INK)
    top = max(costs) if costs else 1
    for y, c, lab in zip(ys, costs, labels):
        ax.text(c + top * 0.01, y, lab, va="center", fontsize=8, color=INK_2)
    ax.set_xlim(0, top * 1.28)
    return _save(fig, path)
