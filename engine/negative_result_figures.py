import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
SCORINGS = ("strict", "frozen", "full_business")
FILL = {"measured": "#dbe7ee", "branch": "#efe4d6", "mapped": "#e4efe4"}


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def table(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(values[0]))
        writer.writeheader()
        writer.writerows(values)


def save_figure(figure, folder, name):
    # 去掉各后端写入的生成时间，使同一份数据重复运行得到逐字节相同的图
    folder.mkdir(parents=True, exist_ok=True)
    for extension, metadata in (("svg", {"Date": None}), ("png", {}),
                                ("pdf", {"CreationDate": None, "ModDate": None})):
        figure.savefig(folder / (name + "." + extension), dpi=450, metadata=metadata)
    plt.close(figure)


def box(axes, x, y, width, height, title, lines, kind="measured"):
    axes.add_patch(plt.Rectangle((x, y), width, height, facecolor=FILL[kind], edgecolor="#3d3d3d", linewidth=0.8))
    axes.text(x + width / 2, y + height - 1.0, title, ha="center", va="top", fontsize=6.9, weight="bold")
    for index, line in enumerate(lines):
        axes.text(x + width / 2, y + height - 3.6 - index * 2.1, line, ha="center", va="top", fontsize=6.3)


def arrow(axes, start, end, color="#3d3d3d", dashed=False, curve=0.0, zorder=3.0):
    axes.annotate("", xy=end, xytext=start,
                  arrowprops={"arrowstyle": "-|>", "color": color, "linewidth": 0.9, "shrinkA": 0, "shrinkB": 0,
                              "linestyle": (0, (3, 2)) if dashed else "solid", "zorder": zorder,
                              "connectionstyle": "arc3,rad=" + str(curve)})


def rail(axes, points):
    for start, end in zip(points[:-2], points[1:-1]):
        axes.plot([start[0], end[0]], [start[1], end[1]], color="#3d3d3d", linewidth=0.9, zorder=2)
    arrow(axes, points[-2], points[-1])


def figure1(out, status, stages, summary, transfer, costs, components):
    exposure = stages["exposure_chain"]
    counts = {row["stage"]: row["requirements"] for row in exposure["stages"]}
    retention = stages["retention_chain"]
    slots = status["generation"]["slot_status"]
    gate = sum(block["edit_triggered"] for block in status["blocks"])
    applied = status["protocol"]["ancestor_version"]
    paid = components["paid_deployment"]
    guard = paid["units"]["ace_op_vp_guard"]
    reviews = read(out / "business/execution_reviews.json")
    business_successes = sum(row["full_business_success"] for row in reviews)
    migration_runs = sum(row["runs"] for row in transfer["migration_layer"]["by_arm"])
    column = {"left": 42, "middle": 94, "right": 146}
    width = 42

    figure, axes = plt.subplots(figsize=(15.4, 8.8))
    axes.set_xlim(-6, 194)
    axes.set_ylim(-9, 88)
    axes.axis("off")
    axes.set_title("Figure 1  Life-constructing relations and the measured Agent event cycle", fontsize=11, pad=4)
    axes.text(18, 84.5, "life keeps maintaining functional organisation", ha="center", fontsize=7.2, style="italic")

    for index, (title, lines) in enumerate([
            ("L1  take up usable resources", ["task outcomes and business", "feedback reach the editor"]),
            ("L2  store and change organisation", ["proposals, persistent versions,", "executable components"]),
            ("L3  express structure as function", ["experience enters real requests;", "components fire on real paths"]),
            ("L4  existing function starts later activity", ["later same-task performance,", "retention and migration"])]):
        box(axes, 2, 62 - index * 13, 32, 11, title, lines, kind="mapped")
    for index in range(3):
        arrow(axes, (18, 62 - index * 13), (18, 60 - index * 13), color="#6b8f6b")
    arrow(axes, (4, 27), (4, 73), color="#6b8f6b", dashed=True, curve=-0.22)

    nodes = [
        (column["left"], 62, width, 11, "A1  task obligations and feedback  M1",
         [f"{counts['feedback_exposure']} requirement exposures; {summary['feedback']['edits']} edits"], "measured"),
        (column["left"], 49, width, 11, "A2  does the evaluation start an edit  M2",
         [f"{gate} of 6 blocks enter editing;", "a strict pass deploys the ancestor"], "measured"),
        (column["left"], 36, width, 11, "A3  edit request and requirement selection  M3",
         [f"{counts['selected_in_edit_reason']} of {counts['feedback_exposure']} exposures selected",
          f"selection rate {exposure['conversion']['selected_per_exposure']:.3f}"], "measured"),
        (column["left"], 24, width, 10, "A4  is a legal candidate generated  M3",
         [f"24 slots: {slots['edited']} legal, {slots['invalid_edit']} generation failures,",
          f"{slots['untriggered_source_passed']} untriggered"], "measured"),
        (column["left"], 12, width, 10, "A5  persistent version and actual change  M3",
         [f"{counts['candidate_expression']} requirements fully written",
          f"{exposure['expression_verification']['unit_references']} unit references inside the candidate"], "measured"),
        (column["middle"], 62, width, 11, "A6  actual request and component events  M4",
         [f"462 executions load {paid['component_invocations']} component invocations",
          "injected content checked in each real request"], "measured"),
        (column["middle"], 49, width, 11, "A7  business actions and end state  M4",
         [f"{business_successes}/462 confirmed passes; {sum(r['full_business_state'] == 'unknown' for r in reviews)} unknown",
          f"ace_op_vp_guard {guard['hooks']['before_call']} before_call invocations; zero effect"], "measured"),
        (column["middle"], 36, width, 11, "A8  source retest and isolated migration  M5",
         [f"{transfer['source_layer']['summary']['retests']} source retests",
          f"{migration_runs} isolated migration runs"], "measured"),
        (column["middle"], 24, width, 10, "A9  retention in later opportunities  M5",
         [f"{retention['stages'][2]['sequences']} of {retention['stages'][1]['sequences']} followed assertion sequences retained",
          f"{retention['stages'][0]['sequences'] - retention['stages'][1]['sequences']} of {retention['stages'][0]['sequences']} verified sequences lack followup"], "measured"),
        (column["right"], 49, width, 11, "A0  keep the existing version",
         ["untriggered blocks and generation", f"failures run one version {applied[:8]}"], "branch"),
        (column["right"], 24, width, 10, "A10  version selected by the evaluation rule  M2",
         ["the gate checks format and conflicts only",
          "no migration assertion enters an edit request"], "branch"),
    ]
    for x, y, box_width, height, title, lines, kind in nodes:
        box(axes, x, y, box_width, height, title, lines, kind=kind)
    box(axes, 2, -6, 186, 9, "C  every settled cost  M6",
        [f"task execution {costs['groups']['task_execution']['billed_cny']:.3f} CNY; "
         f"editing and organisation {costs['groups']['editing']['billed_cny'] + costs['groups']['organization']['billed_cny']:.3f} CNY; "
         f"ledger total {costs['total_billed_cny']:.3f} CNY"], kind="measured")

    for start, end in [((63, 62), (63, 60)), ((63, 49), (63, 47)),
                       ((63, 36), (63, 34)), ((63, 24), (63, 22)), ((84, 17), (94, 66)),
                       ((146, 58), (136, 58)), ((115, 62), (115, 60)), ((115, 49), (115, 47)),
                       ((115, 36), (115, 34)), ((146, 25), (84, 15))]:
        arrow(axes, start, end)
    rail(axes, [(84, 57), (88, 57), (88, 79), (167, 79), (167, 60)])
    rail(axes, [(136, 26), (192, 26), (192, 82), (63, 82), (63, 73)])
    for column_x, top in ((63, 12), (115, 24), (167, 24)):
        arrow(axes, (column_x, 3), (column_x, top), dashed=True, color="#8a5a3b")
    for start, end in [((34, 67), (42, 67)), ((34, 55), (42, 17)), ((34, 42), (94, 57)), ((34, 29), (94, 28))]:
        # 生物学映射线画在方框之下，交叉处不遮挡文字
        arrow(axes, start, end, color="#6b8f6b", dashed=True, curve=0.1, zorder=0.4)
    axes.text(-6, -8.2, "Solid: recorded Agent event relations. Dashed: biology mapping, generation failure and cost. "
                        "Values read from feedback_stages.json, candidate_status.json, transfer.json, "
                        "component_business.json, costs.json and business/execution_reviews.json.", fontsize=6.2)
    figure.tight_layout()
    save_figure(figure, out / "figures", "figure1_construction_cycle")


def figure2(out, stages, summary, transfer):
    exposure = stages["exposure_chain"]
    chain = read(out / "completion/requirement_flow.json")["stages"]
    values = [row["requirements"] for row in chain]
    retention = stages["retention_chain"]
    unobserved = exposure["without_observation"]
    groups = ("first_failed", "first_passed", "all")
    situations = transfer["carry_layer"]["situations"]
    pairs = [situations[name]["pairs"] for name in groups]
    frozen = [situations[name]["frozen"] for name in groups]
    business = [situations[name]["business"] for name in groups]
    fresh = [row["fresh_successes"] for row in frozen]
    carried = [row["carry_successes"] for row in frozen]

    figure, axes = plt.subplots(1, 3, figsize=(14.6, 4.9), gridspec_kw={"width_ratios": [1.45, 1, 1]})
    position = np.arange(len(values))
    axes[0].barh(position, values, color="#256b87")
    axes[0].invert_yaxis()
    axes[0].set_yticks(position, [row["stage"].replace("_", " ") for row in chain], fontsize=7.0)
    for index, row in enumerate(chain):
        suffix = "" if row["rate_from_previous"] is None else f"   ({row['rate_from_previous']:.3f} of previous)"
        axes[0].text(row["requirements"] + 5, index, f"{row['requirements']}{suffix}", va="center", fontsize=7.0)
    axes[0].set_xlim(0, 340)
    axes[0].set_xlabel("requirement items", fontsize=8)
    axes[0].set_title("Figure 2  Requirement flow and its unobserved cases\n"
                      f"{values[0]} annotated requirement items in {summary['feedback']['edits']} edits",
                      fontsize=8.4)

    labels = ["selected+written without later run", "retention sequences without later run",
              "retention sequences without later business run", "written full without selection",
              "repeated groups never selected"]
    numbers = [unobserved["selected_written_full_without_later_execution"],
               unobserved["retention_sequences_without_later_historical_observation"],
               unobserved["retention_sequences_without_later_business_observation"],
               exposure["superset_relations"]["written_full_without_selection"],
               exposure["repeated_requirement_identity"]["groups_never_selected"]]
    position = np.arange(len(numbers))
    axes[1].barh(position, numbers, color=["#ce8b45", "#ce8b45", "#b4472f", "#94b7ca", "#94b7ca"])
    axes[1].invert_yaxis()
    axes[1].set_yticks(position, labels, fontsize=6.5)
    for index, value in enumerate(numbers):
        axes[1].text(value + 1, index, str(value), va="center", fontsize=7.0)
    axes[1].set_xlim(0, max(numbers) * 1.35)
    axes[1].set_xlabel("items / sequences / groups", fontsize=8)
    axes[1].set_title("Same chain: what was never selected\nand never observed", fontsize=8.4)

    index = np.arange(len(groups))
    axes[2].bar(index - 0.2, [value / count for value, count in zip(fresh, pairs)], width=0.38,
                color="#94b7ca", label="fresh rerun")
    axes[2].bar(index + 0.2, [value / count for value, count in zip(carried, pairs)], width=0.38,
                color="#256b87", label="carried trace")
    for order, name in enumerate(groups):
        axes[2].text(order - 0.2, fresh[order] / pairs[order] + 0.02, str(fresh[order]), ha="center", fontsize=7.0)
        axes[2].text(order + 0.2, carried[order] / pairs[order] + 0.02, str(carried[order]), ha="center", fontsize=7.0)
    axes[2].set_xticks(index, ["first failed", "first passed", "all pairs"], fontsize=7.5)
    axes[2].set_ylim(0, 1.18)
    axes[2].set_ylabel("frozen requirement completion", fontsize=8)
    axes[2].legend(frameon=False, fontsize=7)
    axes[2].set_title(f"Carried trajectory and history retention\n"
                      f"{carried[2] - fresh[2]} extra frozen successes on {pairs[2]} pairs\n"
                      f"{business[2]['extra_successes']} extra business successes\n"
                      f"Retention: {retention['stages'][2]['sequences']}/{retention['stages'][1]['sequences']} followed sequences",
                      fontsize=8.0)
    for axis in axes:
        axis.spines["top"].set_visible(False)
        axis.spines["right"].set_visible(False)
    figure.tight_layout()
    save_figure(figure, out / "figures", "figure2_requirement_flow")


def table1(out, audit):
    reviews = read(out / "business/execution_reviews.json")
    migration = [row for row in reviews if row["scope"] == "migration"]
    ranking = {scoring: {arm: index for index, arm in enumerate(
        audit["ranking_change"]["orders"]["frozen_requirements" if scoring == "frozen" else scoring])}
        for scoring in SCORINGS}
    rows = []
    for arm in (*ARMS, "ancestor", "carry"):
        values = [row for row in migration if row["arm"] == arm]
        entry = {"scope": "migration", "arm": arm, "denominator": len(values)}
        for scoring in SCORINGS:
            successes = sum(bool(row[scoring + "_success"]) for row in values)
            entry[scoring + "_successes"] = successes
            entry[scoring + "_rate"] = round(successes / len(values), 4)
            entry["rank_" + scoring] = ranking[scoring][arm]
        entry["strict_false_passes"] = sum(bool(row["strict_success"]) and row["full_business_state"] == "failure" for row in values)
        entry["business_unknowns"] = sum(row["full_business_state"] == "unknown" for row in values)
        entry["executable_business_successes"] = sum(row["executable_business_success"] for row in values)
        entry["strict_false_rejections"] = sum(not row["strict_success"] and row["full_business_success"] for row in values)
        for left, right in (("strict", "frozen"), ("strict", "full_business"), ("frozen", "full_business")):
            entry[f"{left}_vs_{right}_flips"] = sum(bool(row[left + "_success"]) != bool(row[right + "_success"])
                                                    for row in values)
        rows.append(entry)
    totals = {"scope": "migration_all", "arm": "all", "denominator": len(migration)}
    for scoring in SCORINGS:
        successes = sum(entry[scoring + "_successes"] for entry in rows)
        totals[scoring + "_successes"] = successes
        totals[scoring + "_rate"] = round(successes / len(migration), 4)
    totals["strict_false_passes"] = sum(entry["strict_false_passes"] for entry in rows)
    totals["strict_false_rejections"] = sum(entry["strict_false_rejections"] for entry in rows)
    totals["business_unknowns"] = sum(entry["business_unknowns"] for entry in rows)
    totals["executable_business_successes"] = sum(entry["executable_business_successes"] for entry in rows)
    for left, right in (("strict", "frozen"), ("strict", "full_business"), ("frozen", "full_business")):
        totals[f"{left}_vs_{right}_flips"] = sum(entry[f"{left}_vs_{right}_flips"] for entry in rows)
    rows.append(totals)
    for arm in ("source", "F0R0_source", "F1R0_source", "F0R1_source", "F1R1_source"):
        values = [row for row in reviews if row["scope"] == "source" and row["arm"] == arm]
        rows.append({"scope": "source", "arm": arm, "denominator": len(values),
                     "strict_successes": sum(bool(row["strict_success"]) for row in values),
                     "strict_rate": round(sum(bool(row["strict_success"]) for row in values) / len(values), 4),
                     "full_business_successes": sum(row["full_business_success"] for row in values),
                     "full_business_rate": round(sum(row["full_business_success"] for row in values) / len(values), 4),
                     "strict_vs_full_business_flips": sum(bool(row["strict_success"]) != bool(row["full_business_success"])
                                                         for row in values)})
    table(out / "tables/table1_scoring_systems.csv", rows)
    return rows


def table2(out, status, transfer, costs, stages):
    reviews = read(out / "business/execution_reviews.json")
    migration = [row for row in reviews if row["scope"] == "migration"]
    slots = {(slot["domain"], slot["arm"]): slot for slot in status["slots"]}
    candidates = {(row["domain"], row["arm"]): row for row in transfer["migration_layer"]["candidates"]}
    rows = []
    for block in status["blocks"]:
        for arm in ("F0R0", "F1R0"):
            slot, candidate = slots[(block["domain"], arm)], candidates[(block["domain"], arm)]
            values = [row for row in migration if row["domain"] == block["domain"] and row["arm"] == arm]
            rows.append({"group": "block_arm", "unit": block["domain"], "arm": arm,
                         "slot_status": slot["status"], "candidate_deployed": not slot["deployed_ancestor"],
                         "denominator": len(values),
                         "strict_successes": sum(bool(row["strict_success"]) for row in values),
                         "frozen_successes": sum(bool(row["frozen_success"]) for row in values),
                         "full_business_successes": sum(row["full_business_success"] for row in values),
                         "ancestor_full_business_successes": candidate["ancestor_business_successes"],
                         "migration_gains": candidate["business_gains"], "migration_losses": candidate["business_losses"],
                         "generation_cny": round(slot["generation_cost_cny"], 6),
                         "source_retest_cny": round(candidate["source_retest_cny"], 6),
                         "migration_cny": round(candidate["migration_cny"], 6),
                         "candidate_total_cny": round(candidate["candidate_total_cny"], 6)})
    sets = [("edited_blocks", [block["domain"] for block in status["blocks"] if block["edit_triggered"]]),
            ("untriggered_blocks", [block["domain"] for block in status["blocks"] if not block["edit_triggered"]]),
            ("all_six_blocks", [block["domain"] for block in status["blocks"]])]
    for name, domains in sets:
        values = [row for row in migration if row["domain"] in domains and row["arm"] in ARMS]
        rows.append({"group": name, "unit": "|".join(domains), "arm": "+".join(ARMS),
                     "denominator": len(values),
                     "strict_successes": sum(bool(row["strict_success"]) for row in values),
                     "frozen_successes": sum(bool(row["frozen_success"]) for row in values),
                     "full_business_successes": sum(row["full_business_success"] for row in values),
                     "ancestor_full_business_successes": sum(candidates[(domain, arm)]["ancestor_business_successes"]
                                                             for domain in domains for arm in ARMS),
                     "migration_gains": sum(candidates[(domain, arm)]["business_gains"] for domain in domains for arm in ARMS),
                     "migration_losses": sum(candidates[(domain, arm)]["business_losses"] for domain in domains for arm in ARMS),
                     "generation_cny": round(sum(slots[(domain, arm)]["generation_cost_cny"] for domain in domains for arm in ARMS), 6),
                     "source_retest_cny": round(sum(candidates[(domain, arm)]["source_retest_cny"] for domain in domains for arm in ARMS), 6),
                     "migration_cny": round(sum(candidates[(domain, arm)]["migration_cny"] for domain in domains for arm in ARMS), 6)})
    for name in ("all", "first_failed", "first_passed"):
        situation = transfer["carry_layer"]["situations"][name]
        rows.append({"group": "carry_same_task", "unit": name, "arm": "carried vs fresh rerun",
                     "denominator": situation["pairs"],
                     "frozen_successes": situation["frozen"]["carry_successes"],
                     "full_business_successes": situation["business"]["carry_successes"],
                     "ancestor_full_business_successes": situation["business"]["fresh_successes"],
                     "migration_gains": situation["business"]["extra_successes"],
                     "migration_losses": max(0, situation["business"]["fresh_successes"] - situation["business"]["carry_successes"]),
                     "candidate_total_cny": round(situation["business"]["carry_total_cny"], 6),
                     "carry_incremental_cny": round(situation["business"]["incremental_total_cny"], 6)})
    exposure, retention = stages["exposure_chain"], stages["retention_chain"]
    requirement_flow = read(out / "completion/requirement_flow.json")
    rows.append({"group": "history_requirement_chain", "unit": "65_edits_263_exposures", "arm": "exposure chain",
                 "chain_start": requirement_flow["stages"][0]["requirements"],
                 "chain_end": requirement_flow["stages"][-1]["requirements"],
                 "chain_rate": round(requirement_flow["stages"][-1]["rate_from_exposure"], 4)})
    rows.append({"group": "history_retention", "unit": "verified_assertion_sequences", "arm": "retention chain",
                 "chain_start": retention["stages"][1]["sequences"],
                 "chain_end": retention["stages"][2]["sequences"],
                 "chain_rate": round(retention["stages"][2]["sequences"] / retention["stages"][1]["sequences"], 4)})
    rows.append({"group": "settled_cost_ledger", "arm": "billed",
                 "unit": "editing=%.6f|organization=%.6f|task_execution=%.6f" % (
                     costs["groups"]["editing"]["billed_cny"], costs["groups"]["organization"]["billed_cny"],
                     costs["groups"]["task_execution"]["billed_cny"]),
                 "candidate_total_cny": round(costs["total_billed_cny"], 6)})
    fields = ["group", "unit", "arm", "slot_status", "candidate_deployed", "denominator", "strict_successes",
              "frozen_successes", "full_business_successes", "ancestor_full_business_successes", "migration_gains",
              "migration_losses", "chain_start", "chain_end", "chain_rate",
              "generation_cny", "source_retest_cny", "migration_cny", "carry_incremental_cny", "candidate_total_cny"]
    table(out / "tables/table2_flow_success_retention_cost.csv",
          [{field: row.get(field, "") for field in fields} for row in rows])
    return rows


def validate(out, status, stages, transfer, carry, costs, audit, components):
    reviews = read(out / "business/execution_reviews.json")
    checks = {"business_reviews": len(reviews) == 462,
              "migration_reviews": len([row for row in reviews if row["scope"] == "migration"]) == 432,
              "slots": len(status["slots"]) == 24,
              "blocks": len(status["blocks"]) == 6,
              "migration_runs": sum(row["runs"] for row in transfer["migration_layer"]["by_arm"]) == 288,
              "ancestor_and_carry_runs": len([row for row in reviews
                                              if row["scope"] == "migration" and row["arm"] in ("ancestor", "carry")]) == 144,
              "component_effect_events": components["totals"]["effect_events"] == 196,
              "component_executions": components["totals"]["executions_with_component_unit"] == 447,
              "carry_pairs": carry["summary"]["all"]["pairs"] == 72,
              "audit_reviews": audit["execution_level"]["executions"] == 462,
              "stage_chain_monotone": all(
                  later["requirements"] <= earlier["requirements"]
                  for earlier, later in zip(stages["exposure_chain"]["stages"],
                                            stages["exposure_chain"]["stages"][1:])),
              "costs_positive": costs["total_billed_cny"] > 0}
    failed = sorted(name for name, value in checks.items() if not value)
    if failed:
        raise ValueError("图表输入未通过核对: " + ", ".join(failed))
    return checks


def run(root, output):
    out = Path(output)
    (out / "figures").mkdir(parents=True, exist_ok=True)
    status = read(out / "history/candidate_status.json")
    stages = read(out / "history/feedback_stages.json")
    summary = read(out / "history/summary.json")
    transfer = read(out / "transfer.json")
    carry = read(out / "carry.json")
    costs = read(out / "costs.json")
    audit = read(out / "evaluation_audit.json")
    components = read(out / "history/component_business.json")
    checks = validate(out, status, stages, transfer, carry, costs, audit, components)
    plt.rcParams.update({"font.size": 8, "svg.fonttype": "none",
                         "svg.hashsalt": "gnosis-negative-result"})
    figure1(out, status, stages, summary, transfer, costs, components)
    figure2(out, stages, summary, transfer)
    first = table1(out, audit)
    second = table2(out, status, transfer, costs, stages)
    print(json.dumps({"input_checks": checks,
                      "table1": {"rows": len(first),
                                 "migration_totals": next(row for row in first if row["scope"] == "migration_all")},
                      "table2": {"rows": len(second),
                                 "all_six_blocks": next(row for row in second if row["group"] == "all_six_blocks")},
                      "tables": sorted(path.name for path in (out / "tables").iterdir()),
                      "figures": sorted(path.name for path in (out / "figures").iterdir())},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    arguments = parser.parse_args()
    run(arguments.root, arguments.output)
