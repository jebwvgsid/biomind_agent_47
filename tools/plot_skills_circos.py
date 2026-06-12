#!/usr/bin/env python3
"""Draw a BioMind skill-library circos overview.

The plot is built from the local skills directory. It summarizes categories,
per-skill step counts, runtime/script/reference coverage, workflow membership,
and dependency relationships declared in "Related Skills" sections.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

os.environ.setdefault("MPLCONFIGDIR", str(Path(".").resolve() / ".matplotlib-cache"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, PathPatch, Rectangle, Wedge
from matplotlib.path import Path as MplPath
import yaml

ROOT_COLORS = {
    "3d-dna": "#5550a3",
    "crop-breeding": "#4f9a96",
    "databases": "#7b52ab",
    "epigenomics": "#d55f47",
    "genomics": "#f2a74b",
    "open-chromatin": "#1db6bd",
    "scrna": "#69a9ff",
    "sequence": "#9a6b3a",
}


@dataclass
class SkillInfo:
    skill_id: str
    category: str
    name: str
    description: str
    step_count: int
    script_count: int
    reference_count: int
    has_runtime: bool
    workflow_count: int
    input_count: int
    output_count: int
    tag_count: int
    body_words: int
    related_skills: List[str]
    angle0: float = 0.0
    angle1: float = 0.0


def read_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, flags=re.S)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        meta = {}
    return meta, text[match.end() :]


def merged_meta(meta: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(meta)
    nested = meta.get("metadata")
    if isinstance(nested, dict):
        for key, value in nested.items():
            merged.setdefault(key, value)
    return merged


def ensure_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, (tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    text = str(value).strip()
    return [text] if text else []


def workflow_step_count(body: str) -> int:
    numbered = re.findall(r"(?m)^\s*\d+\.\s+\S", body)
    if numbered:
        return len(numbered)
    arrows = re.findall(r"(?m)^\s*(?:->|→|[A-Za-z0-9_./ -]+\s*->)\s*", body)
    if arrows:
        return len(arrows)
    workflow_blocks = re.findall(r"(?is)##\s+(?:Workflow|Planning Workflow|Responsibilities|Agent Decision Protocol)(.*?)(?=\n##\s+|\Z)", body)
    bullet_count = 0
    for block in workflow_blocks:
        bullet_count += len(re.findall(r"(?m)^\s*[-*]\s+\S", block))
    return bullet_count or 1


def related_skill_ids(body: str) -> List[str]:
    related = set()
    for match in re.finditer(r"`((?:skills/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+){1,4})`", body):
        value = match.group(1).strip()
        if value.startswith("skills/"):
            value = value[len("skills/") :]
        if "/" in value:
            related.add(value)
    return sorted(related)


def discover_skills(skills_root: Path, workflows_root: Path) -> List[SkillInfo]:
    workflow_counts = workflow_skill_counts(workflows_root)
    skills: List[SkillInfo] = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        rel_dir = skill_md.parent.relative_to(skills_root).as_posix()
        if any(part.startswith(".") for part in rel_dir.split("/")):
            continue
        text = skill_md.read_text(encoding="utf-8")
        raw_meta, body = read_frontmatter(text)
        meta = merged_meta(raw_meta)
        category = rel_dir.split("/")[0]
        scripts_dir = skill_md.parent / "scripts"
        refs_dir = skill_md.parent / "references"
        scripts = [p for p in scripts_dir.rglob("*") if p.is_file()] if scripts_dir.is_dir() else []
        refs = [p for p in refs_dir.rglob("*") if p.is_file()] if refs_dir.is_dir() else []
        runtime = meta.get("runtime")
        skills.append(
            SkillInfo(
                skill_id=rel_dir,
                category=category,
                name=str(meta.get("name") or skill_md.parent.name),
                description=str(meta.get("description") or "").replace("\n", " ").strip(),
                step_count=workflow_step_count(body),
                script_count=len(scripts),
                reference_count=len(refs),
                has_runtime=isinstance(runtime, dict) and bool(runtime),
                workflow_count=workflow_counts.get(rel_dir, 0),
                input_count=len(ensure_list(meta.get("inputs"))),
                output_count=len(ensure_list(meta.get("outputs"))),
                tag_count=len(ensure_list(meta.get("tags"))),
                body_words=len(re.findall(r"\w+", body)),
                related_skills=related_skill_ids(body),
            )
        )
    return skills


def workflow_skill_counts(workflows_root: Path) -> Counter:
    counts: Counter = Counter()
    if not workflows_root.is_dir():
        return counts
    for path in sorted(workflows_root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        for stage in data.get("stages") or []:
            if not isinstance(stage, dict):
                continue
            skill = str(stage.get("default_skill") or "").strip().strip("/")
            if skill:
                counts[skill] += 1
    return counts


def assign_angles(skills: List[SkillInfo], gap_deg: float = 2.0) -> Dict[str, Tuple[float, float]]:
    grouped: Dict[str, List[SkillInfo]] = defaultdict(list)
    for skill in skills:
        grouped[skill.category].append(skill)
    categories = sorted(grouped)
    total_skills = len(skills)
    total_gap = math.radians(gap_deg * len(categories))
    usable = math.tau - total_gap
    cursor = math.radians(90)
    category_angles: Dict[str, Tuple[float, float]] = {}
    for category in categories:
        group = grouped[category]
        span = usable * len(group) / max(total_skills, 1)
        cat_start = cursor
        slot = span / max(len(group), 1)
        for idx, skill in enumerate(group):
            pad = min(slot * 0.08, math.radians(0.4))
            skill.angle0 = cat_start + idx * slot + pad
            skill.angle1 = cat_start + (idx + 1) * slot - pad
        category_angles[category] = (cat_start, cat_start + span)
        cursor += span + math.radians(gap_deg)
    return category_angles


def pol2xy(radius: float, angle: float) -> Tuple[float, float]:
    return radius * math.cos(angle), radius * math.sin(angle)


def add_wedge(ax, radius_outer: float, radius_inner: float, theta0: float, theta1: float, **kwargs) -> None:
    wedge = Wedge(
        (0, 0),
        radius_outer,
        math.degrees(theta0),
        math.degrees(theta1),
        width=radius_outer - radius_inner,
        **kwargs,
    )
    ax.add_patch(wedge)


def add_arc_label(ax, text: str, theta0: float, theta1: float, radius: float, size: int = 10) -> None:
    angle = (theta0 + theta1) / 2
    x, y = pol2xy(radius, angle)
    rotation = math.degrees(angle)
    if 90 < rotation % 360 < 270:
        rotation += 180
        ha = "right"
    else:
        ha = "left"
    ax.text(x, y, text, rotation=rotation, rotation_mode="anchor", ha=ha, va="center", fontsize=size)


def draw_curve(ax, theta0: float, theta1: float, radius: float, color: str, alpha: float = 0.18, lw: float = 1.0) -> None:
    x0, y0 = pol2xy(radius, theta0)
    x1, y1 = pol2xy(radius, theta1)
    c0 = pol2xy(radius * 0.20, theta0)
    c1 = pol2xy(radius * 0.20, theta1)
    path = MplPath([(x0, y0), c0, c1, (x1, y1)], [MplPath.MOVETO, MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4])
    ax.add_patch(PathPatch(path, facecolor="none", edgecolor=color, lw=lw, alpha=alpha, zorder=0))


def color_for_category(category: str) -> str:
    if category in ROOT_COLORS:
        return ROOT_COLORS[category]
    palette = list(ROOT_COLORS.values())
    return palette[hash(category) % len(palette)]


def draw_plot(skills: List[SkillInfo], output_png: Path, output_svg: Path, title: str) -> None:
    category_angles = assign_angles(skills)
    max_steps = max((skill.step_count for skill in skills), default=1)
    max_scripts = max((skill.script_count for skill in skills), default=1)
    max_refs = max((skill.reference_count for skill in skills), default=1)
    max_io = max((skill.input_count + skill.output_count for skill in skills), default=1)

    fig = plt.figure(figsize=(16.2, 9.8), dpi=180)
    ax = fig.add_axes([0.02, 0.04, 0.62, 0.92])
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_xlim(-1.35, 1.35)
    ax.set_ylim(-1.25, 1.25)

    # Outer category arcs.
    for category, (theta0, theta1) in category_angles.items():
        color = color_for_category(category)
        add_wedge(ax, 1.06, 1.00, theta0, theta1, facecolor=color, edgecolor="white", linewidth=1.2)
        add_arc_label(ax, category, theta0, theta1, 1.14, size=10)

    blues = matplotlib.colormaps["Blues"]
    greens = matplotlib.colormaps["Greens"]
    purples = matplotlib.colormaps["Purples"]
    oranges = matplotlib.colormaps["Oranges"]

    for skill in skills:
        theta0, theta1 = skill.angle0, skill.angle1
        step_color = blues(0.22 + 0.72 * skill.step_count / max_steps)
        script_color = greens(0.18 + 0.72 * min(skill.script_count, max_scripts) / max_scripts) if skill.script_count else "#eeeeee"
        ref_color = purples(0.18 + 0.72 * min(skill.reference_count, max_refs) / max_refs) if skill.reference_count else "#eeeeee"
        runtime_color = "#f05a28" if skill.has_runtime else "#d9d9d9"
        workflow_color = "#111f75" if skill.workflow_count else "#d9d9d9"
        io_color = oranges(0.18 + 0.72 * (skill.input_count + skill.output_count) / max_io) if skill.input_count + skill.output_count else "#eeeeee"
        add_wedge(ax, 0.96, 0.90, theta0, theta1, facecolor=step_color, edgecolor="white", linewidth=0.18)
        add_wedge(ax, 0.88, 0.82, theta0, theta1, facecolor=runtime_color, edgecolor="white", linewidth=0.18)
        add_wedge(ax, 0.80, 0.74, theta0, theta1, facecolor=script_color, edgecolor="white", linewidth=0.18)
        add_wedge(ax, 0.72, 0.66, theta0, theta1, facecolor=ref_color, edgecolor="white", linewidth=0.18)
        add_wedge(ax, 0.64, 0.58, theta0, theta1, facecolor=workflow_color, edgecolor="white", linewidth=0.18)
        add_wedge(ax, 0.56, 0.50, theta0, theta1, facecolor=io_color, edgecolor="white", linewidth=0.18)

    # Category-level inner arcs and cross-category relation curves.
    for category, (theta0, theta1) in category_angles.items():
        color = color_for_category(category)
        add_wedge(ax, 0.40, 0.385, theta0, theta1, facecolor=color, edgecolor="white", linewidth=0.8, alpha=0.95)
        add_arc_label(ax, category.replace("-", "\n"), theta0, theta1, 0.31, size=7)

    id_to_skill = {skill.skill_id: skill for skill in skills}
    seen_pairs = Counter()
    for skill in skills:
        for target in skill.related_skills:
            if target not in id_to_skill:
                continue
            other = id_to_skill[target]
            if other.category == skill.category:
                continue
            pair = tuple(sorted([skill.category, other.category]))
            seen_pairs[pair] += 1
    for (cat_a, cat_b), count in seen_pairs.items():
        a0, a1 = category_angles[cat_a]
        b0, b1 = category_angles[cat_b]
        draw_curve(ax, (a0 + a1) / 2, (b0 + b1) / 2, 0.39, color="#9a9a9a", alpha=min(0.12 + count * 0.03, 0.32), lw=min(0.8 + count * 0.25, 3.0))

    # Center summary.
    ax.add_patch(Circle((0, 0), 0.105, facecolor="white", edgecolor="#444444", lw=1.0, zorder=5))
    ax.text(0, 0.025, "BioMind", ha="center", va="center", fontsize=10, fontweight="bold", zorder=6)
    ax.text(0, -0.035, f"{len(skills)} skills", ha="center", va="center", fontsize=8, zorder=6)

    draw_legend(fig, skills, max_steps, max_scripts, max_refs, max_io, title)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_png, bbox_inches="tight")
    fig.savefig(output_svg, bbox_inches="tight")
    plt.close(fig)


def draw_color_scale(ax, y: float, label: str, cmap_name: str, start: float, vmin: int, vmax: int) -> None:
    cmap = matplotlib.colormaps[cmap_name]
    x0 = 0.035
    bar_y = y - 0.028
    width = 0.23
    height = 0.018
    steps = 12
    ax.text(0.0, y, label, fontsize=8.6, va="center", transform=ax.transAxes)
    for idx in range(steps):
        frac = idx / max(steps - 1, 1)
        color = cmap(start + (0.90 - start) * frac)
        ax.add_patch(
            Rectangle(
                (x0 + width * idx / steps, bar_y),
                width / steps,
                height,
                facecolor=color,
                edgecolor="none",
                transform=ax.transAxes,
            )
        )
    ax.text(x0, bar_y - 0.009, str(vmin), fontsize=7.5, ha="left", va="top", transform=ax.transAxes)
    ax.text(x0 + width, bar_y - 0.009, str(vmax), fontsize=7.5, ha="right", va="top", transform=ax.transAxes)


def draw_legend(fig, skills: List[SkillInfo], max_steps: int, max_scripts: int, max_refs: int, max_io: int, title: str) -> None:
    ax = fig.add_axes([0.67, 0.07, 0.31, 0.87])
    ax.axis("off")
    ax.text(0.0, 0.98, title, fontsize=15, fontweight="bold", va="top")
    ax.text(0.0, 0.925, "Rings from outside to inside:", fontsize=10, va="top")
    y = 0.875
    for label, color in [
        ("Categories", "#4f9a96"),
        ("Runtime contract", "#f05a28"),
        ("Included in workflow", "#111f75"),
        ("Inner links: related skills across categories", "#9a9a9a"),
    ]:
        ax.add_patch(Rectangle((0.0, y - 0.012), 0.025, 0.018, facecolor=color, edgecolor="none", transform=ax.transAxes))
        ax.text(0.035, y, label, fontsize=9, va="center", transform=ax.transAxes)
        y -= 0.045

    ax.text(0.0, y - 0.006, "Quantitative color scales", fontsize=10, fontweight="bold", va="top", transform=ax.transAxes)
    y -= 0.046
    for label, cmap_name, start, vmin, vmax in [
        ("Step count heatmap", "Blues", 0.22, 1, max_steps),
        ("Scripts", "Greens", 0.18, 0, max_scripts),
        ("References", "Purples", 0.18, 0, max_refs),
        ("Inputs + outputs", "Oranges", 0.18, 0, max_io),
    ]:
        draw_color_scale(ax, y, label, cmap_name, start, vmin, vmax)
        y -= 0.068

    counts = Counter(skill.category for skill in skills)
    ax.text(0.0, y - 0.01, "Category counts", fontsize=10, fontweight="bold", va="top", transform=ax.transAxes)
    y -= 0.055
    for category, count in sorted(counts.items()):
        ax.add_patch(Rectangle((0.0, y - 0.012), 0.025, 0.018, facecolor=color_for_category(category), edgecolor="none", transform=ax.transAxes))
        ax.text(0.035, y, f"{category}: {count}", fontsize=8.4, va="center", transform=ax.transAxes)
        y -= 0.034

    runtime_count = sum(1 for skill in skills if skill.has_runtime)
    script_count = sum(1 for skill in skills if skill.script_count)
    ref_count = sum(1 for skill in skills if skill.reference_count)
    workflow_count = sum(1 for skill in skills if skill.workflow_count)
    ax.text(0.0, y - 0.01, "Coverage", fontsize=10, fontweight="bold", va="top", transform=ax.transAxes)
    y -= 0.055
    for label in [
        f"Runtime: {runtime_count} / {len(skills)}",
        f"Scripts: {script_count} / {len(skills)}; References: {ref_count} / {len(skills)}",
        f"Workflow-linked skills: {workflow_count} / {len(skills)}",
    ]:
        ax.text(0.0, y, label, fontsize=8.3, va="center", transform=ax.transAxes)
        y -= 0.032
    y -= 0.018

    top_steps = sorted(skills, key=lambda item: (item.step_count, item.script_count, item.reference_count), reverse=True)[:5]
    ax.text(0.0, y - 0.01, "Largest step counts", fontsize=10, fontweight="bold", va="top", transform=ax.transAxes)
    y -= 0.055
    for skill in top_steps:
        label = textwrap.shorten(skill.skill_id, width=42, placeholder="...")
        ax.text(0.0, y, f"{skill.step_count:>2}  {label}", fontsize=7.2, va="center", transform=ax.transAxes)
        y -= 0.034


def write_summary(skills: Sequence[SkillInfo], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "skill_id",
                "category",
                "name",
                "step_count",
                "script_count",
                "reference_count",
                "has_runtime",
                "workflow_count",
                "input_count",
                "output_count",
                "tag_count",
                "body_words",
            ],
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for skill in skills:
            writer.writerow(
                {
                    "skill_id": skill.skill_id,
                    "category": skill.category,
                    "name": skill.name,
                    "step_count": skill.step_count,
                    "script_count": skill.script_count,
                    "reference_count": skill.reference_count,
                    "has_runtime": int(skill.has_runtime),
                    "workflow_count": skill.workflow_count,
                    "input_count": skill.input_count,
                    "output_count": skill.output_count,
                    "tag_count": skill.tag_count,
                    "body_words": skill.body_words,
                }
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Draw a circos-style overview of the BioMind skills library.")
    parser.add_argument("--skills-root", default="skills", help="Path to BioMind skills root.")
    parser.add_argument("--workflows-root", default="workflows", help="Path to BioMind workflows root.")
    parser.add_argument("--out-prefix", default="docs/biomind_skills_circos", help="Output prefix without extension.")
    parser.add_argument("--title", default="BioMind Skills Library Circos", help="Figure title.")
    args = parser.parse_args()

    skills_root = Path(args.skills_root)
    workflows_root = Path(args.workflows_root)
    out_prefix = Path(args.out_prefix)
    skills = discover_skills(skills_root, workflows_root)
    if not skills:
        raise SystemExit(f"No SKILL.md files found under {skills_root}")
    draw_plot(skills, out_prefix.with_suffix(".png"), out_prefix.with_suffix(".svg"), args.title)
    write_summary(skills, out_prefix.with_name(out_prefix.name + "_summary.tsv"))
    print(f"skills: {len(skills)}")
    print(f"png: {out_prefix.with_suffix('.png')}")
    print(f"svg: {out_prefix.with_suffix('.svg')}")
    print(f"summary: {out_prefix.with_name(out_prefix.name + '_summary.tsv')}")


if __name__ == "__main__":
    main()
