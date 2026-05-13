"""График веса в PNG (matplotlib без GUI)."""
from __future__ import annotations

import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402


def weight_chart(history: list[tuple[str, float]], target: float | None = None) -> bytes:
    if not history:
        return b""
    dates = [datetime.fromisoformat(ts) for ts, _ in history]
    weights = [w for _, w in history]

    fig, ax = plt.subplots(figsize=(8, 4.5), dpi=120)
    ax.plot(dates, weights, marker="o", color="#3b82f6", linewidth=2, markersize=5)
    ax.fill_between(dates, weights, min(weights) - 1, color="#3b82f6", alpha=0.08)

    if target:
        ax.axhline(target, linestyle="--", color="#10b981", linewidth=1.2, label=f"цель: {target} кг")
        ax.legend(loc="best")

    ax.set_title("Динамика веса", fontsize=14, weight="bold")
    ax.set_ylabel("кг")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()
