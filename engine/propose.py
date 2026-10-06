import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")
sys.path.insert(0, str(REPO_ROOT))

from gepa.strategies.instruction_proposal import InstructionProposalSignature

from engine.agent_runtime import load_candidate, normalize_component_text, write_manifest
from engine.component_host import ComponentHost
from engine.gepa_adapter import (
    COMPONENTS,
    INTERFACE_VERSION,
    ReflectionRecords,
    build_reflection_lm,
    candidate_hash,
    extract_proposed_source,
)

PROPOSAL_RAW_MAX_CHARS = 20000


def load_trajectory(misc_dir: Path, task_id: str, evaluation: dict) -> dict:
    """从已有实验输出拼出与 GEPA evaluate 同形态的轨迹，供反思记录复用。"""
    detail = evaluation.get(task_id)
    if detail is None:
        raise KeyError(f"评分文件里没有任务 {task_id}：{misc_dir.parent.parent}")
    steps = 0
    log_path = misc_dir / "agent_log.jsonl"
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            if '"role": "agent"' in line:
                steps += 1
    return {
        "task_id": task_id,
        "success": bool(detail.get("success", False)),
        "misc_dir": str(misc_dir),
        "failures": detail.get("failures") or [],
        "steps": steps,
        "error": None,
    }


def write_candidate(root: Path, sources: dict, host: ComponentHost) -> dict:
    for component in COMPONENTS:
        (root / component).mkdir(parents=True, exist_ok=True)
    (root / "W" / "workflow.ts").write_text(sources["W"], encoding="utf-8")
    (root / "T" / "wrapper.ts").write_text(sources["T"], encoding="utf-8")
    (root / "C" / "context.ts").write_text(sources["C"], encoding="utf-8")
    skills_path = root / "S" / "skills.md"
    if sources["S"].strip():
        skills_path.write_text(sources["S"], encoding="utf-8")
    elif skills_path.exists():
        # S 变空时必须删旧文件：load_candidate 按目录扫描 .md，不看 manifest
        skills_path.unlink()
    # 上一轮的 meta 不属于候选内容，重写 manifest 前去干净
    (root / "proposal_meta.json").unlink(missing_ok=True)
    normalized = {
        "W": host.normalize("W", sources["W"]),
        "T": host.normalize("T", sources["T"]),
        "C": host.normalize("C", sources["C"]),
        "S": normalize_component_text("S", sources["S"]),
    }
    (root / "normalized.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    write_manifest(str(root), INTERFACE_VERSION)
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description="定向提议单组件并校验，验证提议→校验→物化通路")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--component", required=True, choices=COMPONENTS)
    parser.add_argument("--tasks", required=True, help="逗号分隔的 task_id 列表")
    parser.add_argument("--config", default="experiments/stage3_v2/config.json")
    parser.add_argument("--source-experiment", required=True)
    parser.add_argument("--evaluation-label", default="subset")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    config = json.loads((REPO_ROOT / args.config).read_text(encoding="utf-8"))
    candidate = load_candidate(str(REPO_ROOT / args.candidate_dir))
    if candidate.interface_version != INTERFACE_VERSION:
        raise ValueError(
            f"propose 只支持 interface_version={INTERFACE_VERSION} 的候选：{candidate.root}"
        )
    template = config["gepa"]["reflection_prompt_templates"][args.component]
    InstructionProposalSignature.validate_prompt_template(template)

    sources = candidate.component_sources
    outputs_dir = (
        Path(os.environ["APPWORLD_ROOT"])
        / "experiments"
        / "outputs"
        / args.source_experiment
    )
    evaluation = json.loads(
        (outputs_dir / "evaluations" / f"{args.evaluation_label}.json").read_text(
            encoding="utf-8"
        )
    )["individual"]
    task_ids = [task_id for task_id in args.tasks.split(",") if task_id]
    records_builder = ReflectionRecords()
    records = []
    for task_id in task_ids:
        trajectory = load_trajectory(
            outputs_dir / "tasks" / task_id / "misc", task_id, evaluation
        )
        score = 1.0 if trajectory["success"] else 0.0
        records.append(records_builder.build(args.component, trajectory, score))

    prompt = InstructionProposalSignature.prompt_renderer(
        {
            "current_instruction_doc": sources[args.component],
            "dataset_with_feedback": records,
            "prompt_template": template,
        }
    )
    reflection_lm = build_reflection_lm(config)
    raw_output = reflection_lm(prompt)
    new_text = extract_proposed_source(raw_output)

    host = ComponentHost.from_config(config)
    out_dir = Path(args.out)
    outcome = None
    validate_ok = None
    validate_errors: list[str] = []
    try:
        if new_text.strip() == "NO_CHANGE":
            outcome = "no_change"
        else:
            # S 是纯文本，没有宿主可校验的导出与权限
            if args.component == "S":
                validate_ok = True
            else:
                validation = host.validate({args.component: new_text})
                validate_ok = bool(validation.get("ok"))
                validate_errors = list(validation.get("errors") or [])
            if not validate_ok:
                outcome = "invalid"
            elif _same_normalized(host, args.component, new_text, sources[args.component]):
                outcome = "noop_equal"
            else:
                outcome = "proposed"
        if outcome == "proposed":
            new_sources = dict(sources)
            new_sources[args.component] = new_text
            write_candidate(out_dir, new_sources, host)
    finally:
        host.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "parent": {
            "dir": str(Path(candidate.root).resolve()),
            "hash": candidate_hash(sources),
        },
        "component": args.component,
        "outcome": outcome,
        "validate_ok": validate_ok,
        "validate_errors": validate_errors,
        "tasks": task_ids,
        "new_candidate_hash": (
            candidate_hash({**sources, args.component: new_text})
            if outcome == "proposed"
            else None
        ),
        "raw_output": raw_output[:PROPOSAL_RAW_MAX_CHARS],
    }
    (out_dir / "proposal_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"[propose] component={args.component} outcome={outcome} out={out_dir}", flush=True)


def _same_normalized(host: ComponentHost, component: str, left: str, right: str) -> bool:
    if component == "S":
        return normalize_component_text("S", left) == normalize_component_text("S", right)
    return host.normalize(component, left) == host.normalize(component, right)


if __name__ == "__main__":
    main()
