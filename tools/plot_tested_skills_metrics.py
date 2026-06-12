#!/usr/bin/env python3
"""Plot a compact dashboard for BioMind tested skill runs.

The input is a TSV table with one row per tested task. Blank numeric cells are
treated as missing values, not zero, so partially recorded sessions can still be
shown without inventing measurements.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

os.environ.setdefault("MPLCONFIGDIR", str(Path(".").resolve() / ".matplotlib-cache"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch


INPUT_ROLE_COLUMNS = ["consultant", "execute", "generate", "plan_review", "planner", "supervisor", "triage"]
PLOTTED_ROLE_COLUMNS = ["consultant", "execute", "generate", "planner", "supervisor", "triage"]
ROLE_COLORS = {
    "consultant": "#4f79a7",
    "execute": "#59a14f",
    "generate": "#f28e2b",
    "planner": "#edc948",
    "supervisor": "#e15759",
    "triage": "#76b7b2",
}


@dataclass
class TestRun:
    date: str
    session_id: str
    test_content: str
    total_agent_seconds: float | None
    role_seconds: Dict[str, float | None]
    script_seconds: float | None
    error_count: float | None
    human_confirm_count: float | None

    @property
    def label(self) -> str:
        return self.test_content

    @property
    def measured_role_seconds(self) -> float:
        return sum(self.role_seconds[role] for role in PLOTTED_ROLE_COLUMNS if self.role_seconds[role] is not None)


def parse_float(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text.rstrip("s"))
    except ValueError as exc:
        raise ValueError(f"Cannot parse numeric value: {value!r}") from exc


def read_runs(path: Path) -> List[TestRun]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"date", "session_id", "test_content", "script_seconds", "error_count", "human_confirm_count", *INPUT_ROLE_COLUMNS}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise SystemExit(f"Missing required TSV columns: {', '.join(missing)}")
        runs = []
        for row_index, row in enumerate(reader, start=2):
            if row.get(None):
                raise SystemExit(f"TSV row {row_index} has extra columns; check tab alignment.")
            runs.append(
                TestRun(
                    date=row["date"].strip(),
                    session_id=row["session_id"].strip(),
                    test_content=row["test_content"].strip(),
                    total_agent_seconds=parse_float(row.get("total_agent_seconds", "")),
                    role_seconds={column: parse_float(row.get(column, "")) for column in INPUT_ROLE_COLUMNS},
                    script_seconds=parse_float(row.get("script_seconds", "")),
                    error_count=parse_float(row.get("error_count", "")),
                    human_confirm_count=parse_float(row.get("human_confirm_count", "")),
                )
            )
    return runs


def non_missing(values: Iterable[float | None]) -> List[float]:
    return [value for value in values if value is not None and math.isfinite(value)]


def max_or_one(values: Iterable[float | None]) -> float:
    valid = non_missing(values)
    return max(valid) if valid else 1.0


def short_label(text: str, limit: int = 34) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def plot_dashboard(runs: Sequence[TestRun], output_png: Path, output_svg: Path, title: str) -> None:
    measured_runs = [run for run in runs if run.measured_role_seconds or run.script_seconds is not None]
    if not measured_runs:
        raise SystemExit("No measured rows found in input TSV.")

    labels = [short_label(run.label) for run in measured_runs]
    y_positions = list(range(len(measured_runs)))

    fig = plt.figure(figsize=(15.8, 9.4), dpi=180)
    grid = fig.add_gridspec(
        nrows=2,
        ncols=3,
        width_ratios=[2.35, 1.05, 1.15],
        height_ratios=[1.45, 1.0],
        left=0.08,
        right=0.98,
        bottom=0.12,
        top=0.84,
        wspace=0.28,
        hspace=0.33,
    )
    ax_stack = fig.add_subplot(grid[:, 0])
    ax_quality = fig.add_subplot(grid[0, 1])
    ax_script = fig.add_subplot(grid[1, 1])
    ax_heatmap = fig.add_subplot(grid[:, 2])

    fig.suptitle(title, fontsize=17, fontweight="bold", x=0.08, y=0.975, ha="left")
    fig.legend(
        handles=[Patch(facecolor=ROLE_COLORS[role], label=role) for role in PLOTTED_ROLE_COLUMNS],
        ncols=6,
        fontsize=7.8,
        frameon=False,
        loc="upper left",
        bbox_to_anchor=(0.08, 0.942),
    )

    left = [0.0] * len(measured_runs)
    for role in PLOTTED_ROLE_COLUMNS:
        values = [run.role_seconds[role] or 0.0 for run in measured_runs]
        ax_stack.barh(y_positions, values, left=left, height=0.72, color=ROLE_COLORS[role], edgecolor="white", linewidth=0.7)
        left = [base + value for base, value in zip(left, values)]
    ax_stack.set_yticks(y_positions)
    ax_stack.set_yticklabels(labels, fontsize=8.4)
    ax_stack.invert_yaxis()
    ax_stack.set_xlabel("Agent role runtime (seconds)")
    ax_stack.set_title("Runtime decomposition by agent role", loc="left", fontsize=11, fontweight="bold")
    ax_stack.grid(axis="x", color="#dddddd", linewidth=0.6)

    errors = [run.error_count for run in measured_runs]
    confirms = [run.human_confirm_count for run in measured_runs]
    max_marker = max_or_one([*(value for value in errors), *(value for value in confirms)])
    ax_quality.scatter([run.error_count or 0 for run in measured_runs], y_positions, s=70, color="#d95f02", label="correction errors", alpha=0.85)
    ax_quality.scatter([run.human_confirm_count or 0 for run in measured_runs], y_positions, s=70, color="#1b9e77", label="human confirmations", alpha=0.85)
    ax_quality.set_yticks(y_positions)
    ax_quality.set_yticklabels([])
    ax_quality.invert_yaxis()
    ax_quality.set_xlim(0, max_marker * 1.15 + 0.5)
    ax_quality.set_xlabel("Count")
    ax_quality.set_title("Review burden", loc="left", fontsize=11, fontweight="bold")
    ax_quality.grid(axis="x", color="#dddddd", linewidth=0.6)
    ax_quality.legend(fontsize=7.8, frameon=False, loc="lower right")

    script_values = [run.script_seconds or 0 for run in measured_runs]
    ax_script.barh(y_positions, script_values, height=0.72, color="#6b6ecf", edgecolor="white", linewidth=0.7)
    ax_script.set_yticks(y_positions)
    ax_script.set_yticklabels([])
    ax_script.invert_yaxis()
    ax_script.set_xlabel("Seconds")
    ax_script.set_title("Script runtime", loc="left", fontsize=11, fontweight="bold")
    ax_script.grid(axis="x", color="#dddddd", linewidth=0.6)

    matrix = []
    for run in measured_runs:
        matrix.append([run.role_seconds[role] if run.role_seconds[role] is not None else math.nan for role in PLOTTED_ROLE_COLUMNS])
    image = ax_heatmap.imshow(matrix, aspect="auto", cmap="YlGnBu")
    ax_heatmap.set_xticks(range(len(PLOTTED_ROLE_COLUMNS)))
    ax_heatmap.set_xticklabels(PLOTTED_ROLE_COLUMNS, rotation=45, ha="right", fontsize=8)
    ax_heatmap.set_yticks(y_positions)
    ax_heatmap.set_yticklabels([])
    ax_heatmap.set_title("Role runtime heatmap", loc="left", fontsize=11, fontweight="bold")
    for row_index, run in enumerate(measured_runs):
        for col_index, role in enumerate(PLOTTED_ROLE_COLUMNS):
            value = run.role_seconds[role]
            if value is not None:
                ax_heatmap.text(col_index, row_index, f"{value:.0f}", ha="center", va="center", fontsize=6.5, color="#222222")
    cbar = fig.colorbar(image, ax=ax_heatmap, fraction=0.055, pad=0.03)
    cbar.set_label("Seconds", fontsize=8)
    cbar.ax.tick_params(labelsize=7)

    missing_count = len(runs) - len(measured_runs)
    summary = (
        f"Measured rows: {len(measured_runs)} / {len(runs)}"
        f"    Missing-metric rows: {missing_count}"
        f"    Total correction errors: {sum(value for value in non_missing(errors)):.0f}"
        f"    Total human confirmations: {sum(value for value in non_missing(confirms)):.0f}"
    )
    fig.text(0.08, 0.045, summary, fontsize=9, color="#333333")

    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, bbox_inches="tight")
    fig.savefig(output_svg, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot BioMind tested skill metrics from a TSV table.")
    parser.add_argument("--input", default="docs/biomind_tested_skills_metrics.tsv", help="Input TSV with tested skill metrics.")
    parser.add_argument("--out-prefix", default="docs/biomind_tested_skills_dashboard", help="Output prefix without extension.")
    parser.add_argument("--title", default="BioMind Tested Skill Runs", help="Figure title.")
    args = parser.parse_args()

    runs = read_runs(Path(args.input))
    out_prefix = Path(args.out_prefix)
    plot_dashboard(runs, out_prefix.with_suffix(".png"), out_prefix.with_suffix(".svg"), args.title)
    print(f"rows: {len(runs)}")
    print(f"png: {out_prefix.with_suffix('.png')}")
    print(f"svg: {out_prefix.with_suffix('.svg')}")


if __name__ == "__main__":
    main()
