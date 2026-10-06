import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")

from engine.factorial import (  # noqa: E402  共享运行层
    COMPONENTS,
    evaluate_only,
    expected_candidate_ids,
    identity_of,
    load_json,
    misc_dir,
    read_component_calls,
    read_run_meta,
    require_scores,
    resolve_path,
    run_shards,
    write_json,
)

METHOD_NAMES = {"W": ("template", "beforeStep"), "T": ("formatObservation",), "C": ("buildContext",)}


def canonical_observation(raw: str) -> str:
    """base_v2 组件 T 的输出（契约 §4），用于判断 T 是否真的改变了观测形态。"""
    return "Output:\n```\n" + raw + ("" if raw.endswith("\n") else "\n") + "```\n\n"


def read_lm_calls(misc: Path) -> list[dict]:
    return [load_json(path) for path in sorted((Path(misc) / "lm_calls").glob("*.json"))]


def read_agent_log(misc: Path) -> list[dict]:
    path = Path(misc) / "agent_log.jsonl"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def append_evidence(call_messages: list[dict], previous_messages: list[dict]) -> list[dict] | None:
    """历史在 base_v2 的 T/C/W 下只增不改；前缀不一致说明 C 重组过，返回 None。"""
    if call_messages[: len(previous_messages)] != previous_messages:
        return None
    return call_messages[len(previous_messages) :]


def matches_method(record: dict, component: str, method: str, candidate_ids: set[str]) -> bool:
    if record.get("candidate_id") not in candidate_ids:
        return False
    owner = str(record.get("component", "")).upper()
    return owner == component and method in str(record.get("method", ""))


def collect_calls(experiment: str, task_ids: list[str], candidate_ids: set[str]) -> dict[str, dict]:
    """逐任务收集 component_calls 与 lm_calls/agent_log 证据。"""
    evidence = {}
    for task_id in task_ids:
        misc = misc_dir(experiment, task_id)
        calls = read_component_calls(misc)
        evidence[task_id] = {
            "misc": str(misc),
            "component_calls_path": str(misc / "component_calls.jsonl"),
            "component_calls": calls,
            "lm_calls": read_lm_calls(misc),
            "agent_log": read_agent_log(misc),
            "candidate_ids": sorted(
                {record["candidate_id"] for record in calls if record.get("candidate_id") in candidate_ids}
            ),
        }
    return evidence


def loaded_level(component: str, evidence: dict, candidate_ids: set[str]) -> tuple[bool, list[str]]:
    details = []
    ok = False
    for task_id, item in evidence.items():
        matched = [
            record
            for record in item["component_calls"]
            if any(
                matches_method(record, component, method, candidate_ids)
                for method in METHOD_NAMES.get(component, ())
            )
            or (component == "S" and record.get("candidate_id") in candidate_ids)
        ]
        details.append(f"{item['component_calls_path']}: 匹配候选 {item['candidate_ids']} 的调用 {len(matched)} 条")
        ok = ok or bool(matched)
    return ok, details


def triggered_level(component: str, evidence: dict, candidate_ids: set[str], skills_text: str) -> tuple[bool, list[str]]:
    details = []
    ok = False
    for task_id, item in evidence.items():
        if component == "S":
            calls = item["lm_calls"]
            if not calls:
                continue
            first = calls[0]["request"]["messages"]
            hit = bool(skills_text.strip()) and any(skills_text.strip() in message["content"] for message in first)
            details.append(f"{task_id}: 首次 LM 请求包含技能文本={hit}")
            ok = ok or hit
            continue
        if component == "C":
            hit = False
            for record in item["component_calls"]:
                if record.get("candidate_id") not in candidate_ids:
                    continue
                if not matches_method(record, "C", "buildContext", candidate_ids):
                    continue
                if record.get("in_messages") != record.get("out_messages") or record.get("lm_request"):
                    hit = True
            calls = item["lm_calls"]
            for index in range(1, len(calls)):
                previous = calls[index - 1]["request"]["messages"]
                current = calls[index]["request"]["messages"]
                if current[: len(previous)] != previous:
                    hit = True
            details.append(f"{task_id}: buildContext 消息数变化或发起 lmRequest 或历史前缀被改写={hit}")
            ok = ok or hit
            continue
        environment_outputs = [entry["content"] for entry in item["agent_log"] if entry.get("role") == "environment"]
        calls = item["lm_calls"]
        if component == "W":
            stopped = [
                entry
                for entry in item["agent_log"]
                if entry.get("role") == "termination" and str(entry.get("content", "")).startswith("workflow stop:")
            ]
            injected = False
            for index in range(1, len(calls)):
                appended = append_evidence(calls[index]["request"]["messages"], calls[index - 1]["request"]["messages"])
                if appended is None:
                    continue
                if len([message for message in appended if message["role"] == "user"]) >= 2:
                    injected = True
            hit = bool(stopped) or injected
            details.append(f"{task_id}: beforeStep stop={bool(stopped)} inject={injected}")
            ok = ok or hit
            continue
        # T：比较被追加的观测消息与 base_v2 组件 T 的同输入输出
        hit = False
        for index in range(1, len(calls)):
            appended = append_evidence(calls[index]["request"]["messages"], calls[index - 1]["request"]["messages"])
            if appended is None or index - 1 >= len(environment_outputs):
                continue
            users = [message for message in appended if message["role"] == "user"]
            if not users:
                continue
            raw = environment_outputs[index - 1]
            # 观测消息先于 W 的 inject 追加，故追加段里第一条 user 消息就是 T 的产出
            if users[0]["content"] != canonical_observation(raw):
                hit = True
        details.append(f"{task_id}: formatObservation 输出与 base_v2 形态不同={hit}")
        ok = ok or hit
    return ok, details


def run_mechanism_check(args, config: dict) -> tuple[dict, int]:
    component = args.component
    base_dir = resolve_path(args.base_dir)
    out_dir = resolve_path(args.out if args.out else f"experiments/mechanism/{component}")
    candidate_dir = out_dir / "candidate"
    report = {
        "component": component,
        "base_dir": str(base_dir),
        "tasks": args.tasks,
        "source_experiment": args.source_experiment,
        "config": args.config,
        "out_dir": str(out_dir),
        "generated": False,
        "loaded": False,
        "triggered": False,
        "benefit": False,
        "levels": {},
    }

    command = [
        sys.executable,
        "engine/propose.py",
        "--candidate-dir",
        str(base_dir),
        "--component",
        component,
        "--tasks",
        ",".join(args.tasks),
        "--source-experiment",
        args.source_experiment,
        "--evaluation-label",
        args.evaluation_label,
        "--config",
        str(resolve_path(args.config)),
        "--out",
        str(candidate_dir),
    ]
    print(f"[mechanism] 提议 {' '.join(command)}", flush=True)
    proposal_code = subprocess.call(command, cwd=str(REPO_ROOT))
    meta_path = candidate_dir / "proposal_meta.json"
    proposal_meta = load_json(meta_path) if meta_path.exists() else None
    report["proposal"] = {
        "exit_code": proposal_code,
        "proposal_meta_path": str(meta_path),
        "proposal_meta": proposal_meta,
    }
    outcome = (proposal_meta or {}).get("outcome")
    report["generated"] = outcome == "proposed"
    report["levels"]["generated"] = {
        "ok": report["generated"],
        "evidence": [str(meta_path)],
        "note": f"proposal outcome={outcome!r}",
    }
    if outcome != "proposed":
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return report, 2

    from engine.agent_runtime import load_candidate

    candidate = load_candidate(str(candidate_dir))
    candidate_identity = identity_of(candidate)
    experiment = f"mech_{component}_{candidate_identity['hash']}"
    candidate_ids = expected_candidate_ids(candidate)
    report["candidate"] = candidate_identity
    report["experiment"] = experiment
    report["targets"] = {
        "experiment_summary_path": str(
            resolve_path(args.config).parent / f"{experiment}_summary.json"
        ),
        "component_calls_glob": str(misc_dir(experiment, "<task>") / "component_calls.jsonl"),
    }

    num_processes = args.num_processes or int(config["gepa"]["num_processes"])
    run_shards(candidate_dir, experiment, args.tasks, args.config, num_processes)
    if evaluate_only(candidate_dir, experiment, args.tasks, args.config) != 0:
        raise RuntimeError(f"{experiment} 的 --evaluate-only 非零退出")
    records = require_scores(args.config, experiment, args.tasks)
    report["run_meta"] = read_run_meta(experiment, 0)
    evidence = collect_calls(experiment, args.tasks, candidate_ids)
    loaded, loaded_details = loaded_level(component, evidence, candidate_ids)
    triggered, triggered_details = triggered_level(component, evidence, candidate_ids, candidate.skills_text)
    report["loaded"] = loaded
    report["levels"]["loaded"] = {"ok": loaded, "evidence": loaded_details}
    report["triggered"] = triggered
    report["levels"]["triggered"] = {"ok": triggered, "evidence": triggered_details}

    candidate_success = {task_id: bool(records[task_id]["success"]) for task_id in args.tasks}
    scores = {
        "candidate_successes": sum(candidate_success.values()),
        "num_tasks": len(args.tasks),
        "candidate_cost_usd": round(
            sum(float(records[task_id]["usage"].get("cost", 0.0)) for task_id in args.tasks), 4
        ),
    }
    if args.base_summary:
        base_summary = load_json(resolve_path(args.base_summary))
        base_records = {record["task_id"]: record for record in base_summary["records"]}
        base_success = {task_id: bool(base_records[task_id]["success"]) for task_id in args.tasks}
        gained = sorted(task_id for task_id in args.tasks if candidate_success[task_id] and not base_success[task_id])
        lost = sorted(task_id for task_id in args.tasks if base_success[task_id] and not candidate_success[task_id])
        scores.update({"base_summary_path": str(resolve_path(args.base_summary)), "base_successes": sum(base_success.values()), "gained": gained, "lost": lost})
        benefit = bool(gained) and not lost
        benefit_note = f"gained={gained} lost={lost}"
    else:
        scores.update({"base_summary_path": None, "base_successes": None, "gained": [], "lost": []})
        benefit = False
        benefit_note = "未提供 --base-summary，只报候选自身成功数，benefit 记为 false"
    report["benefit"] = benefit
    report["levels"]["benefit"] = {"ok": benefit, "evidence": [scores["base_summary_path"]] if scores["base_summary_path"] else [], "note": benefit_note}
    report["scores"] = scores
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return report, 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="机制验收：真实反射提议 → 装载运行 → 四层取证（generated/loaded/triggered/benefit）")
    parser.add_argument("--component", required=True, choices=list(COMPONENTS))
    parser.add_argument("--base-dir", default="candidates/base_v2")
    parser.add_argument("--tasks", required=True, help="逗号分隔的 task_id")
    parser.add_argument("--source-experiment", required=True)
    parser.add_argument("--evaluation-label", default="subset")
    parser.add_argument("--config", default="experiments/stage3_v2/config.json")
    parser.add_argument("--out", default=None)
    parser.add_argument("--base-summary", default=None)
    parser.add_argument("--num-processes", type=int, default=None)
    args = parser.parse_args(argv)
    args.tasks = [task_id.strip() for task_id in args.tasks.split(",") if task_id.strip()]
    if not args.tasks:
        parser.error("--tasks 为空")
    return args


def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))
    report, code = run_mechanism_check(args, config)
    out_dir = resolve_path(args.out if args.out else f"experiments/mechanism/{args.component}")
    write_json(out_dir / "mechanism_report.json", report)
    print(
        f"[mechanism] generated={report['generated']} loaded={report['loaded']} "
        f"triggered={report['triggered']} benefit={report['benefit']} "
        f"报告写入 {out_dir / 'mechanism_report.json'}",
        flush=True,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
