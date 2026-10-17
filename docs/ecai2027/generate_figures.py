import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np


FOLDER = Path(__file__).resolve().parent
ROOT = FOLDER.parents[1]
DATA = ROOT / "experiments/negative_result_offline"
PREVIEW = FOLDER / ".build/figures"
COLUMN = 86 / 25.4
FULL = 178 / 25.4
BLUE = "#008dff"
GREEN = "#38bd84"
ORANGE = "#ff681e"
INK = "#10213e"
GRAY = "#929aa7"
MUTED = "#536579"
GRID = "#e7edf3"

plt.rcParams.update({
    "font.family": "Arial",
    "font.size": 8.5,
    "text.color": INK,
    "axes.labelcolor": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.edgecolor": "#c8d4df",
    "axes.linewidth": 0.6,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "path",
    "savefig.facecolor": "white",
})


def read(relative):
    return json.loads((DATA / relative).read_text(encoding="utf-8"))


def save(figure, name):
    PREVIEW.mkdir(parents=True, exist_ok=True)
    figure.savefig(FOLDER / f"{name}.pdf",
                   metadata={"CreationDate": None, "ModDate": None})
    figure.savefig(FOLDER / f"{name}.svg", metadata={"Date": None})
    figure.savefig(PREVIEW / f"{name}.png", dpi=450)
    plt.close(figure)
    print(name, flush=True)


def figure_2_requirements_flow():
    # 来源为正文采用的完成判读，事件身份保存在原始 JSON 中。
    flow = read("completion/requirement_flow.json")
    counts = [row["requirements"] for row in flow["stages"]]
    figure = plt.figure(figsize=(COLUMN, 2.38))
    axes = figure.add_axes([0, 0, 1, 1])
    axes.set(xlim=(0, 100), ylim=(0, 100))
    axes.axis("off")
    axes.text(2, 96, "From feedback to completed function", weight="bold",
              fontsize=9.2, va="top")
    axes.text(98, 85.5, "Exposures", ha="right", fontsize=8, color=MUTED)
    labels = ["Exposed", "Selected in rationale", "Fully incorporated",
              "Outcome observed", "Obligation completed"]
    positions = [76, 62.5, 49, 35.5, 22]
    for index, (label, count, y) in enumerate(zip(labels, counts, positions)):
        color = GREEN if index == 4 else BLUE
        axes.text(2, y, label, va="center", fontsize=8.5,
                  weight="bold" if index == 4 else "normal")
        axes.barh(y, 34, left=49, height=5.3, color=GRID, linewidth=0)
        axes.barh(y, 34 * count / counts[0], left=49, height=5.3,
                  color=color, linewidth=0)
        axes.text(98, y, str(count), ha="right", va="center",
                  weight="bold", fontsize=10.5, color=color)
    axes.plot([2, 98], [12.5, 12.5], color=GRID, lw=0.8)
    axes.text(2, 6, f"Observed completion: {counts[-1] / counts[0]:.2%}",
              fontsize=8.5, weight="bold", color="#16865b", va="center")
    save(figure, "fig2_requirements_flow")


def figure_3_contrast_blocks():
    # 各块的完整业务成功数直接读取 transfer.json，与补充材料 Table S4 一致。
    transfer = read("transfer.json")
    candidates = {(row["domain"], row["arm"]): row
                  for row in transfer["migration_layer"]["candidates"]}
    domains = ["finance", "sales", "support", "hr", "marketing", "operations"]
    labels = ["Finance", "Sales", "Support", "HR", "Marketing", "Operations"]
    positions = [6, 5, 4, 2, 1, 0]
    figure = plt.figure(figsize=(COLUMN, 3.00))
    axes = figure.add_axes([0.28, 0.165, 0.54, 0.66])
    axes.set(xlim=(-0.4, 12.4), ylim=(-0.6, 7.1))
    axes.set_xticks([0, 4, 8, 12])
    axes.set_yticks([])
    axes.tick_params(axis="x", labelsize=8, length=3, width=0.6)
    axes.set_xlabel("Confirmed successes / 12", fontsize=8.3, labelpad=5)
    for spine in ("top", "left", "right"):
        axes.spines[spine].set_visible(False)
    axes.set_axisbelow(True)
    axes.grid(axis="x", color=GRID, lw=0.7)
    figure.text(0.02, 0.965, "Organization effects by source block", fontsize=9.2,
                weight="bold", va="top")
    handles = [Line2D([], [], color=GRAY, marker="o", mfc="white", ms=5,
                      lw=0, label="F0R0"),
               Line2D([], [], color=BLUE, marker="o", ms=5, lw=0, label="F1R0")]
    figure.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.01, 0.913),
                  frameon=False, ncol=2, fontsize=8, handletextpad=0.3,
                  columnspacing=1.1, borderaxespad=0)
    axes.text(14.9, 7.4, "Δ count", ha="right", fontsize=8,
              color=MUTED, clip_on=False)
    group_means = []
    for group in (domains[:3], domains[3:]):
        differences = [candidates[(domain, "F1R0")]["business_successes"]
                       - candidates[(domain, "F0R0")]["business_successes"]
                       for domain in group]
        group_means.append(np.mean(differences) / 12 * 100)
    for y, title, effect in zip([6.8, 2.8],
                                ["Editing triggered", "Ancestor retained"], group_means):
        axes.text(-6.5, y, title, fontsize=8.1, weight="bold", color=MUTED,
                  va="center", clip_on=False)
        axes.text(14.9, y, f"{effect:+.2f} pp", fontsize=8.1, ha="right",
                  va="center", color=INK, clip_on=False)
    for domain, label, y in zip(domains, labels, positions):
        f0 = candidates[(domain, "F0R0")]["business_successes"]
        f1 = candidates[(domain, "F1R0")]["business_successes"]
        axes.text(-0.9, y, label, ha="right", va="center", fontsize=8.5,
                  clip_on=False)
        axes.plot([f0, f1], [y, y], color="#b4c7d5", lw=1.7, zorder=2)
        axes.scatter([f0], [y], s=35, facecolor="white", edgecolor=GRAY,
                     linewidth=1.1, zorder=3)
        axes.scatter([f1], [y], s=15 if f0 == f1 else 27,
                     facecolor=BLUE, edgecolor="white", linewidth=0.5, zorder=4)
        difference = f1 - f0
        color = GREEN if difference > 0 else ORANGE if difference < 0 else MUTED
        axes.text(14.9, y, f"{difference:+d}" if difference else "0",
                  fontsize=9, weight="bold", color=color, ha="right",
                  va="center", clip_on=False)
    save(figure, "fig3_contrast_blocks")


def figure_4_transfer_vs_carry():
    # 业务计数使用全量复核。费用取真实两次尝试配对，包含首次尝试。
    summary = read("business/summary.json")
    counts = {arm: sum(row["full_business_successes"] for row in summary
                       if row["scope"] == "migration" and row["arm"] == arm)
              for arm in ("F0R0", "F1R0", "ancestor", "carry")}
    carry = read("carry.json")["summary"]["all"]
    fresh_total, carry_total = carry["fresh_total_cny"], carry["carry_total_cny"]
    figure = plt.figure(figsize=(FULL, 2.25))
    axes = figure.add_axes([0.16, 0.20, 0.335, 0.58])
    axes.set(xlim=(0, 100), ylim=(0.3, 6.1))
    axes.set_xticks([0, 50, 100], labels=["0", "50", "100%"])
    axes.tick_params(axis="x", labelsize=8, length=3, width=0.6)
    axes.set_yticks([])
    for spine in ("top", "left", "right"):
        axes.spines[spine].set_visible(False)
    axes.grid(axis="x", color=GRID, lw=0.7)
    axes.set_axisbelow(True)
    figure.text(0.015, 0.96, "(a) Complete business success", fontsize=9.2,
                weight="bold", va="top")
    axes.text(-43, 5.95, "Persistent transfer", fontsize=8.1, weight="bold",
              color=MUTED, clip_on=False, va="center")
    axes.text(-43, 2.95, "Same task", fontsize=8.1, weight="bold",
              color=MUTED, clip_on=False, va="center")
    for arm, label, y, color in zip(
            ["F0R0", "F1R0", "ancestor", "carry"],
            ["F0R0", "F1R0", "Fresh retry", "Carry"], [5, 4, 2, 1],
            ["#74bdf0", BLUE, GRAY, GREEN]):
        axes.text(-5, y, label, ha="right", va="center", fontsize=8.5,
                  clip_on=False)
        axes.barh(y, counts[arm] / 72 * 100, height=0.43, color=color, linewidth=0)
        axes.text(counts[arm] / 72 * 100 + 3, y, f"{counts[arm]}/72",
                  va="center", fontsize=8.5, weight="bold", color=INK)
    figure.text(0.16, 0.045, "24 tasks · 3 runs per condition", fontsize=8,
                color=MUTED)

    figure.text(0.59, 0.96, "(b) Cost of direct evidence reuse", fontsize=9.2,
                weight="bold", va="top")
    figure.text(0.59, 0.845, "Two attempts per pair; 72 pairs", fontsize=8,
                color=MUTED)
    figure.text(0.805, 0.73, "Fresh retry", fontsize=8.5, ha="center", color=MUTED)
    figure.text(0.945, 0.73, "Carry", fontsize=8.5, ha="center", color="#16865b")
    figure.add_artist(Line2D([0.59, 0.99], [0.685, 0.685], transform=figure.transFigure,
                            color=GRID, lw=0.8))
    figure.text(0.59, 0.565, "Total cost\n(CNY)", fontsize=8.3, va="center")
    figure.text(0.805, 0.565, f"{fresh_total:.3f}", fontsize=11,
                ha="center", va="center", weight="bold", color=MUTED)
    figure.text(0.945, 0.565, f"{carry_total:.3f}", fontsize=11,
                ha="center", va="center", weight="bold", color=INK)
    figure.text(0.59, 0.35, "Cost / success\n(CNY)", fontsize=8.3, va="center")
    figure.text(0.805, 0.35, f"{fresh_total / counts['ancestor']:.4f}", fontsize=11,
                ha="center", va="center", weight="bold", color=MUTED)
    figure.text(0.945, 0.35, f"{carry_total / counts['carry']:.4f}", fontsize=11,
                ha="center", va="center", weight="bold", color="#16865b")
    figure.text(0.59, 0.125,
                f"+{counts['carry'] - counts['ancestor']} confirmed successes with Carry",
                fontsize=9, weight="bold", color="#16865b")
    save(figure, "fig4_transfer_vs_carry")


if __name__ == "__main__":
    figure_2_requirements_flow()
    figure_3_contrast_blocks()
    figure_4_transfer_vs_carry()
