import argparse
import difflib
import json
import os
import random
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")

from engine.factorial import (  # noqa: E402  共享运行层
    build_component_host,
    evaluate_only,
    load_json,
    materialize_candidate,
    misc_dir,
    outputs_dir,
    require_scores,
    resolve_path,
    run_shards,
    sha256_text,
    write_json,
)

PROMPT_TEMPLATE = """You are repairing a ReAct code agent whose harness is built from composable TypeScript modules.
Two independent single-component mutations, A and B, were each an improvement on their own, but the combined
candidate H11 (parent + A + B) regresses on tasks that one single mutation solved. Write a JOINT REPAIR: new
versions of BOTH changed components that keep both improvements and restore the lost tasks.

## Component interface contract (fixed; the exported signatures cannot change)
%%CONTRACT%%

## Parent version and the two changes
%%CHANGES%%

## Failures each change was originally meant to fix (evidence from the parent run)
%%FAILURES%%

## Four-way factorial results (Y = official task success, 0/1)
%%SCORES%%

## Tasks lost by the combination, with execution evidence
%%LOST%%

## Improvements to preserve (do not lose these again)
%%PRESERVE%%
%%PREVIOUS%%
## Output format (strict)
%%OUTPUT%%

Rules: each file stays self-contained (no import/require/dynamic import/eval/Function/process/globalThis/fetch);
keep every exported symbol and its signature; keep the hard requirements of the contract (for W: the Jinja
placeholders, the USER:/ASSISTANT:/SYSTEM: markers at line starts and the trailing `Task: {{ instruction }}` line;
for T: the Python code-fence convention `Output:\\n```\\n...\\n```\\n\\n`; for C: the first instruction message and
the newest observation must survive). Return a block for EVERY changed component listed above, and never hard-code
task-specific instructions or answers.
"""


def code_block(language: str, body: str) -> str:
    return f"```{language}\n{body.rstrip()}\n```"


COMPONENT_BLOCK_RE = re.compile(r'<component\s+name="([A-Za-z]+)"\s*>\n(.*?)\n?</component>', re.DOTALL)


def all_components(components: dict) -> list[str]:
    """A/B 两侧改动组件的并集，顺序固定便于复现。"""
    return sorted(set(components["a"]) | set(components["b"]))


def contract_interface_section() -> str:
    """契约 §2 的组件接口段落，原样嵌进修复提示。"""
    text = (REPO_ROOT / "scratch" / "phase1_contract.md").read_text(encoding="utf-8")
    return text[text.index("## 2.") : text.index("## 3.")].strip()


def normalized_reference(host, component: str, source: str) -> str:
    if component == "S":
        return sha256_text(source.strip())
    return host.normalize(component, source)


def unified_diff(parent_source: str, candidate_source: str, label: str) -> str:
    lines = difflib.unified_diff(
        parent_source.splitlines(keepends=True),
        candidate_source.splitlines(keepends=True),
        fromfile=f"parent/{label}",
        tofile=f"candidate/{label}",
    )
    return "".join(lines) or "(no difference after normalization)"


def build_changes_section(parent: dict, candidate_a: dict, candidate_b: dict, components: dict, indexes: dict) -> str:
    parts = []
    for side, candidate in (("A", candidate_a), ("B", candidate_b)):
        for component in components[side.lower()]:
            parts.append(
                f"### Change {side}: component {component} "
                f"(candidate idx={indexes[side.lower()]}, parent idx={indexes['parent']})\n\n"
                f"Component {component} in the parent version:\n\n"
                + code_block("ts", parent[component])
                + f"\n\nComponent {component} in the {side} version:\n\n"
                + code_block("ts", candidate[component])
                + f"\n\nNormalized difference (parent -> {side}):\n\n"
                + code_block("diff", unified_diff(parent[component], candidate[component], component))
            )
    return "\n\n".join(parts)


def record_json(search_stage_root, candidate_hash_: str, task_id: str, component: str) -> str:
    """复用 gepa_adapter 的轨迹读取（ReflectionRecords.build）：官方逐项失败 + agent_log 执行摘要。

    数据源是搜索的 score_cache（每任务一条，含 misc_dir 与 slim failures），不读
    evaluations/subset.json——它在搜索期间被每批 --evaluate-only 覆盖，只剩最后一批任务。
    """
    from engine.gepa_adapter import ReflectionRecords

    entry = load_json(resolve_path(search_stage_root) / "score_cache" / candidate_hash_ / f"{task_id}.json")
    trajectory = {
        "task_id": task_id,
        "success": bool(entry["success"]),
        "misc_dir": entry["misc_dir"],
        "failures": entry.get("failures", []),
        "steps": entry.get("steps", 0),
        "error": None,
    }
    record = ReflectionRecords().build(component, trajectory, 1.0 if trajectory["success"] else 0.0)
    return json.dumps(record, ensure_ascii=False, indent=1)


def record_json_from_experiment(experiment: str, task_id: str, component: str) -> str:
    """单任务实验（factorial/repair 产出）的轨迹读取：它们的 subset.json 只含该任务，是完整的。"""
    from engine.gepa_adapter import ReflectionRecords

    details_path = outputs_dir(experiment) / "evaluations" / "subset.json"
    individual = load_json(details_path)["individual"].get(task_id, {})
    trajectory = {
        "task_id": task_id,
        "success": bool(individual.get("success", False)),
        "misc_dir": str(misc_dir(experiment, task_id)),
        "failures": individual.get("failures", []),
        "steps": 0,
        "error": None,
    }
    record = ReflectionRecords().build(component, trajectory, 1.0 if trajectory["success"] else 0.0)
    return json.dumps(record, ensure_ascii=False, indent=1)


def build_scores_section(report: dict) -> str:
    rows = [f"{'task':<34}{'H00':>6}{'H10':>6}{'H01':>6}{'H11':>6}  lost"]
    for task_id in report["tasks"]:
        cells = {name: report["runs"][name][task_id]["r1"]["success"] for name in ("H00", "H10", "H01", "H11")}
        mark = "yes" if task_id in report["degeneration"]["lost_items"] else ""
        rows.append(
            f"{task_id:<34}{int(cells['H00']):>6}{int(cells['H10']):>6}{int(cells['H01']):>6}{int(cells['H11']):>6}  {mark}"
        )
    rows.append(f"\nInteraction estimate I = {report['interaction']:.4f}")
    return "\n".join(rows)


def parse_component_blocks(raw: str, components: tuple[str, ...]) -> tuple[dict, list[str]]:
    """按 <component name="X"> ... </component> 抽取源码。

    组件源码自身可能含 ``` 围栏（base W 模板就有 45 处），所以不用围栏做分隔。
    """
    found = {}
    for label, body in COMPONENT_BLOCK_RE.findall(raw):
        label = label.strip().upper()
        if label in components and label not in found:
            found[label] = body.rstrip("\n") + "\n"
    return found, [component for component in components if component not in found]


def build_prompt(
    config: dict,
    report: dict,
    preserve_tasks: list[str],
    parent: dict,
    candidate_a: dict,
    candidate_b: dict,
    components: dict,
    indexes: dict,
    experiments: dict,
    previous: str,
) -> str:
    failures = []
    search_stage_root = config["source"]["stage_root"]
    from engine.gepa_adapter import candidate_hash as _ch
    search_hashes = {"parent": _ch(parent), "a": _ch(candidate_a), "b": _ch(candidate_b)}
    for side, key in (("A", "gained_a"), ("B", "gained_b")):
        listed = ",".join(components[side.lower()])
        tasks = report["pair"][key]
        failures.append(f"### Change {side} (components {listed}) fixed these tasks under the parent: {tasks}")
        for task_id in tasks:
            failures.append(
                f"task {task_id} under the PARENT (failed there):\n\n"
                + code_block(
                    "json",
                    record_json(
                        search_stage_root, search_hashes["parent"], task_id, components[side.lower()][0]
                    ),
                )
            )
    lost = []
    for task_id in report["degeneration"]["lost_items"]:
        lost.append(
            f"task {task_id}: H10={int(report['runs']['H10'][task_id]['r1']['success'])} "
            f"H01={int(report['runs']['H01'][task_id]['r1']['success'])} "
            f"H11={int(report['runs']['H11'][task_id]['r1']['success'])}; H11 evidence:\n\n"
            + code_block(
                "json",
                record_json_from_experiment(
                    report["runs"]["H11"][task_id]["r1"]["experiment"],
                    task_id,
                    all_components(components)[0],
                ),
            )
        )
    preserve = [
        f"tasks that A alone solved: {report['pair']['only_a']}",
        f"tasks that B alone solved: {report['pair']['only_b']}",
        f"tasks both single mutations solved (candidate must keep them too): {preserve_tasks}",
    ]
    output_spec = "\n\n".join(
        f'<component name="{component}">\n<full source for component {component}>\n</component>'
        for component in all_components(components)
    )
    replacements = {
        "%%CONTRACT%%": contract_interface_section(),
        "%%CHANGES%%": build_changes_section(parent, candidate_a, candidate_b, components, indexes),
        "%%FAILURES%%": "\n\n".join(failures),
        "%%SCORES%%": build_scores_section(report),
        "%%LOST%%": "\n\n".join(lost) or "(none recorded)",
        "%%PRESERVE%%": "\n".join(preserve),
        "%%PREVIOUS%%": previous,
        "%%OUTPUT%%": "Return one block per changed component, using exactly these XML-style tags (the component "
        "source may itself contain ``` fences, so do NOT wrap it in code fences):\n\n" + output_spec,
    }
    prompt = PROMPT_TEMPLATE
    for token, value in replacements.items():
        prompt = prompt.replace(token, value)
    return prompt


def run_attempt(
    args,
    config: dict,
    report: dict,
    preserve_tasks: list[str],
    attempt_index: int,
    parent: dict,
    candidate_a: dict,
    candidate_b: dict,
    components: dict,
    indexes: dict,
    experiments: dict,
    host,
    reflection_lm,
    previous: str,
) -> dict:
    from engine.gepa_adapter import candidate_hash

    attempt = {"attempt": attempt_index, "outcome": "unrepaired"}
    reflection_before = reflection_lm.total_cost
    prompt = build_prompt(
        config, report, preserve_tasks, parent, candidate_a, candidate_b, components, indexes, experiments, previous
    )
    raw = reflection_lm(prompt)
    attempt["reflection_cost_usd"] = round(reflection_lm.total_cost - reflection_before, 4)
    attempt["raw_output_excerpt"] = raw[:4000]
    sources, missing = parse_component_blocks(raw, tuple(all_components(components)))
    if missing:
        attempt["outcome"] = "parse_failed"
        attempt["missing_blocks"] = missing
        attempt["cost"] = attempt["reflection_cost_usd"]
        return attempt
    candidate = dict(parent)
    for component in all_components(components):
        candidate[component] = sources[component]
    validation = host.validate({name: candidate[name] for name in ("W", "T", "C")})
    attempt["validation"] = validation
    if not validation.get("ok"):
        attempt["outcome"] = "validate_failed"
        attempt["cost"] = attempt["reflection_cost_usd"]
        return attempt

    pair_dir = resolve_path(config["stage_root"]) / "factorial" / f"pair{args.pair_index}"
    attempt_dir = pair_dir / f"repair_attempt{attempt_index}"
    materialize_candidate(
        attempt_dir,
        candidate,
        host,
        {
            "kind": "joint_repair",
            "pair_index": args.pair_index,
            "attempt": attempt_index,
            "components": components,
            "indexes": indexes,
            "replaced": {
                **{component: f"A#{indexes['a']}" for component in components["a"]},
                **{component: f"B#{indexes['b']}" for component in components["b"]},
            },
        },
    )
    attempt["sources_hash"] = candidate_hash(candidate)
    attempt["candidate_dir"] = str(attempt_dir)

    tasks = sorted(
        set(report["degeneration"]["lost_items"]) | set(report["pair"]["only_a"]) | set(report["pair"]["only_b"])
    )
    run_tasks = tasks + [task_id for task_id in preserve_tasks if task_id not in tasks]
    experiment = f"{config['experiment_prefix']}_repair_p{args.pair_index}_a{attempt_index}"
    attempt["experiment"] = experiment
    attempt["task_ids"] = run_tasks
    run_shards(attempt_dir, experiment, run_tasks, args.config, int(config["num_processes"]))
    if evaluate_only(attempt_dir, experiment, run_tasks, args.config) != 0:
        raise RuntimeError(f"{experiment} 的 --evaluate-only 非零退出")
    records = require_scores(args.config, experiment, run_tasks)
    success = {task_id: bool(records[task_id]["success"]) for task_id in run_tasks}
    attempt["scores"] = success
    attempt["task_cost_usd"] = round(
        sum(float(records[task_id]["usage"].get("cost", 0.0)) for task_id in run_tasks), 4
    )
    attempt["reverted_to_parent"] = {
        side: all(
            normalized_reference(host, component, candidate[component])
            == normalized_reference(host, component, parent[component])
            for component in components[side]
        )
        for side in ("a", "b")
    }
    attempt["selection_branch"] = attempt["reverted_to_parent"]["a"] != attempt["reverted_to_parent"]["b"]
    attempt["restored_lost"] = all(success.get(task_id, False) for task_id in report["degeneration"]["lost_items"])
    attempt["preserved_a"] = all(success.get(task_id, False) for task_id in report["pair"]["gained_a"])
    attempt["preserved_b"] = all(success.get(task_id, False) for task_id in report["pair"]["gained_b"])
    attempt["preserved_sample"] = all(success.get(task_id, False) for task_id in preserve_tasks)
    if attempt["restored_lost"] and attempt["preserved_a"] and attempt["preserved_b"]:
        attempt["outcome"] = "repaired"
    elif attempt["selection_branch"]:
        attempt["outcome"] = "selection_branch"
    attempt["cost"] = round(attempt["reflection_cost_usd"] + attempt["task_cost_usd"], 4)
    return attempt


def run_repair(args, config: dict) -> tuple[dict, int]:
    from engine.gepa_adapter import candidate_hash
    from engine.gnosis_lm import GnosisLM

    pair_dir = resolve_path(config["stage_root"]) / "factorial" / f"pair{args.pair_index}"
    report_path = pair_dir / "factorial_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"缺少四组组合干预报告：{report_path}")
    report = load_json(report_path)
    if not report["degeneration"]["repeatable_loss"]:
        payload = {
            "pair_index": args.pair_index,
            "outcome": "no_degeneration",
            "lost_items": report["degeneration"]["lost_items"],
            "source_report": str(report_path),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        write_json(pair_dir / "repair_report.json", payload)
        print(f"[repair] pair {args.pair_index} 没有可重复的组合退化，不触发修复", flush=True)
        return payload, 2

    search_result = load_json(resolve_path(args.search_result or config["source"]["search_result"]))
    candidates = [json.loads(text) for text in search_result["candidates"]]
    indexes = report["indexes"]
    components = report["components"]
    parent = candidates[indexes["parent"]]
    candidate_a = candidates[indexes["a"]]
    candidate_b = candidates[indexes["b"]]
    prefix = config["source"]["experiment_prefix"]
    experiments = {
        "parent": f"{prefix}_{candidate_hash(parent)}",
        "a": f"{prefix}_{candidate_hash(candidate_a)}",
        "b": f"{prefix}_{candidate_hash(candidate_b)}",
    }
    reflection_lm = GnosisLM(
        rate_card=config["rate_card"],
        max_tokens=config.get("reflection", {}).get(
            "max_tokens", config["sampling"]["max_tokens"]
        ),
        temperature=config["sampling"]["temperature"],
        seed=config["sampling"]["seed"],
        max_retries=config["sampling"]["max_retries"],
        retry_after_n_seconds=config["sampling"]["retry_after_n_seconds"],
    )
    affected = (
        set(report["degeneration"]["lost_items"]) | set(report["pair"]["only_a"]) | set(report["pair"]["only_b"])
    )
    both_solved = [
        task_id
        for task_id in report["tasks"]
        if task_id not in affected
        and report["runs"]["H10"][task_id]["r1"]["success"]
        and report["runs"]["H01"][task_id]["r1"]["success"]
    ]
    limit = int(config["factorial"]["tasks_per_pair"])
    preserve_tasks = sorted(
        random.Random(int(config["sampling"]["seed"]) + args.pair_index).sample(
            both_solved, min(limit, len(both_solved))
        )
    )

    host = build_component_host(config)
    attempts = []
    outcome = "unrepaired"
    try:
        for attempt_index in range(1, int(config["repair"]["max_attempts"]) + 1):
            previous = ""
            if attempts:
                previous = (
                    "## Previous repair attempt (did not satisfy the criteria)\n\n"
                    + code_block("json", json.dumps(attempts[-1], ensure_ascii=False, indent=1)[:6000])
                    + "\n\n"
                )
            attempt = run_attempt(
                args, config, report, preserve_tasks, attempt_index, parent, candidate_a, candidate_b,
                components, indexes, experiments, host, reflection_lm, previous,
            )
            attempts.append(attempt)
            print(f"[repair] attempt {attempt_index}: {attempt['outcome']}", flush=True)
            if attempt["outcome"] == "repaired":
                outcome = "repaired"
                break
            if attempt["outcome"] == "selection_branch":
                outcome = "selection_branch"
    finally:
        host.close()

    payload = {
        "pair_index": args.pair_index,
        "outcome": outcome,
        "components": components,
        "indexes": indexes,
        "preserve_tasks": preserve_tasks,
        "source_report": str(report_path),
        "experiments": experiments,
        "attempts": attempts,
        "total_cost": round(
            reflection_lm.total_cost + sum(attempt.get("task_cost_usd", 0.0) for attempt in attempts), 4
        ),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    write_json(pair_dir / "repair_report.json", payload)
    print(f"[repair] pair {args.pair_index} outcome={outcome} 费用=${payload['total_cost']}", flush=True)
    return payload, 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="联合修复算子：按四组干预证据提议 H11'，重跑判定是否恢复损失并保留双方收益")
    parser.add_argument("--pair-index", type=int, required=True)
    parser.add_argument("--search-result", default=None,
                        help="候选池文件；缺省读 config 的 source.search_result")
    parser.add_argument("--config", default="experiments/stage4/config.json")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))
    _, code = run_repair(args, config)
    sys.exit(code)


if __name__ == "__main__":
    main()
