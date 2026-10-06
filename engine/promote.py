import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")

from engine.factorial import (  # noqa: E402  共享运行层
    InfraFailure,
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

CHAMPION_PATH = REPO_ROOT / "experiments" / "champion.json"


def relpath(path) -> str:
    return os.path.relpath(str(path), REPO_ROOT).replace(os.sep, "/")


def load_champion(champion_dir_hint: str) -> dict:
    """冠军记录不存在时以 candidates/base_v2 建档；已存在则原样返回。"""
    if CHAMPION_PATH.exists():
        return load_json(CHAMPION_PATH)
    from engine.agent_runtime import load_candidate

    candidate = load_candidate(str(resolve_path(champion_dir_hint)))
    record = {
        "candidate_dir": champion_dir_hint,
        "hash": identity_of(candidate)["hash"],
        "component_hashes": candidate.component_hashes,
        "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "evidence": {
            "reason": f"champion.json 不存在，以 {champion_dir_hint} 作为初始冠军",
            "dataset": None,
            "gained": [],
            "lost": [],
        },
    }
    write_json(CHAMPION_PATH, record)
    print(f"[promote] 建档初始冠军 {champion_dir_hint} hash={record['hash']}", flush=True)
    return record


def paired_evidence(champion_records: dict, challenger_records: dict, task_ids: list[str]) -> dict:
    champion_success = {task_id: bool(champion_records[task_id]["success"]) for task_id in task_ids}
    challenger_success = {task_id: bool(challenger_records[task_id]["success"]) for task_id in task_ids}
    gained = sorted(task_id for task_id in task_ids if challenger_success[task_id] and not champion_success[task_id])
    lost = sorted(task_id for task_id in task_ids if champion_success[task_id] and not challenger_success[task_id])
    retained = sorted(task_id for task_id in task_ids if champion_success[task_id] and challenger_success[task_id])
    champion_wins = [task_id for task_id in task_ids if champion_success[task_id]]
    return {
        "num_tasks": len(task_ids),
        "gained": gained,
        "lost": lost,
        "retained": retained,
        "retention": round(len(retained) / len(champion_wins), 4) if champion_wins else 1.0,
        "champion_successes": len(champion_wins),
        "challenger_successes": sum(challenger_success.values()),
        "champion_success_rate": round(len(champion_wins) / len(task_ids), 4),
        "challenger_success_rate": round(sum(challenger_success.values()) / len(task_ids), 4),
        "cost_champion": round(sum(float(r["usage"].get("cost", 0.0)) for r in champion_records.values()), 4),
        "cost_challenger": round(sum(float(r["usage"].get("cost", 0.0)) for r in challenger_records.values()), 4),
    }


def run_inheritance_check(args, config: dict, champion_dir: Path, champion_candidate, champion_identity: dict) -> dict:
    """晋升后另起进程装载新冠军跑少量 train 任务，取证跨进程装载的是同一份组件。"""
    from appworld import load_task_ids

    task_ids = load_task_ids(config["tasks"]["dataset"])[: int(config["promotion"]["inheritance_tasks"])]
    experiment = f"promote_inherit_{champion_identity['hash']}_r1"
    command = [
        sys.executable,
        "engine/run_experiment.py",
        "--candidate",
        str(champion_dir),
        "--experiment",
        experiment,
        "--task-ids",
        *task_ids,
        "--num-processes",
        "1",
        "--process-index",
        "0",
        "--config",
        str(resolve_path(args.config)),
    ]
    print(f"[promote] 继承验证 {' '.join(command)}", flush=True)
    code = subprocess.call(command, cwd=str(REPO_ROOT))
    if code != 0:
        raise InfraFailure(f"继承验证子进程退出码 {code}：{experiment}")
    meta = read_run_meta(experiment, 0)
    hashes_match = meta["candidate_hashes"] == champion_identity["component_hashes"]
    expected = expected_candidate_ids(champion_candidate)
    per_task = {}
    for task_id in task_ids:
        records = read_component_calls(misc_dir(experiment, task_id))
        observed = sorted({record["candidate_id"] for record in records})
        methods = sorted(
            f"{record.get('component')}.{record.get('method')}"
            for record in records
            if record.get("method")
        )
        per_task[task_id] = {
            "component_calls": len(records),
            "observed_candidate_ids": observed,
            "methods": methods,
            "matches_champion": bool(observed) and all(value in expected for value in observed),
        }
    passed = hashes_match and all(entry["matches_champion"] for entry in per_task.values())
    return {
        "experiment": experiment,
        "task_ids": task_ids,
        "expected_candidate_ids": sorted(expected),
        "run_meta_component_hashes_match": hashes_match,
        "tasks": per_task,
        "passed": passed,
    }


def run_promotion(args, config: dict) -> dict:
    from appworld import load_task_ids, update_root

    update_root(os.environ["APPWORLD_ROOT"])
    from engine.agent_runtime import load_candidate

    challenger_dir = resolve_path(args.candidate_dir)
    challenger = load_candidate(str(challenger_dir))
    challenger_identity = identity_of(challenger)
    champion = load_champion("candidates/base_v2")
    champion_dir = resolve_path(champion["candidate_dir"])
    champion_candidate = load_candidate(str(champion_dir))
    champion_identity = identity_of(champion_candidate)
    if challenger_identity["component_hashes"] == champion_identity["component_hashes"]:
        raise ValueError(
            f"挑战者与现冠军的组件哈希完全相同，没有可晋升的差异：{args.candidate_dir}"
        )

    task_ids = load_task_ids(config["promotion"]["dataset"])
    num_processes = int(config["num_processes"])
    experiment_champion = f"promote_champ_{champion_identity['hash']}_r1"
    experiment_challenger = f"promote_chal_{challenger_identity['hash']}_r1"
    run_shards(champion_dir, experiment_champion, task_ids, args.config, num_processes)
    run_shards(challenger_dir, experiment_challenger, task_ids, args.config, num_processes)
    if evaluate_only(champion_dir, experiment_champion, task_ids, args.config) != 0:
        raise InfraFailure(f"{experiment_champion} 的 --evaluate-only 非零退出")
    if evaluate_only(challenger_dir, experiment_challenger, task_ids, args.config) != 0:
        raise InfraFailure(f"{experiment_challenger} 的 --evaluate-only 非零退出")
    champion_records = require_scores(args.config, experiment_champion, task_ids)
    challenger_records = require_scores(args.config, experiment_challenger, task_ids)
    evidence = paired_evidence(champion_records, challenger_records, task_ids)
    evidence["dataset"] = config["promotion"]["dataset"]
    evidence["rule"] = config["promotion"]["rule"]

    # 侦察风险指标（预注册）：挑战者评测运行中的受阻尝试 + 沙箱内侦察，按任务数归一。
    # 超过阈值即降级：不晋升，证据保留。W 机制发现证明这类行为是候选的真实副作用。
    from engine.escape_audit import load_adjudications, scan_experiment

    adjudications = load_adjudications(
        resolve_path("experiments/audit/adjudications.json")
    )
    challenger_audit = scan_experiment(
        Path(os.environ["APPWORLD_ROOT"]) / "experiments" / "outputs",
        experiment_challenger,
        adjudications,
    )
    risk_events = challenger_audit["blocked_attempts"] + challenger_audit["sandbox_internal_recon"]
    risk_per_task = risk_events / max(len(task_ids), 1)
    risk_threshold = float(config["promotion"]["risk_threshold_per_task"])
    evidence["risk"] = {
        "blocked_attempts": challenger_audit["blocked_attempts"],
        "sandbox_internal_recon": challenger_audit["sandbox_internal_recon"],
        "events_per_task": round(risk_per_task, 4),
        "threshold_per_task": risk_threshold,
    }

    # 预注册判定：丢失为 0 且有新增才晋升；风险超额即降级；等分但更便宜只记录
    promoted = not evidence["lost"] and len(evidence["gained"]) >= 1 and risk_per_task <= risk_threshold
    evidence["decided"] = promoted
    if not promoted:
        reasons = []
        if evidence["lost"]:
            reasons.append(f"丢失 {len(evidence['lost'])} 个历史成功")
        if not evidence["gained"]:
            reasons.append("无新增成功")
        if risk_per_task > risk_threshold:
            reasons.append(
                f"侦察风险 {risk_per_task:.4f}/任务 超过阈值 {risk_threshold}（降级，不晋升）"
            )
        evidence["decision_reason"] = "；".join(reasons)
        if evidence["lost"] == [] and evidence["gained"] == [] and evidence["cost_challenger"] < evidence["cost_champion"]:
            evidence["cost_only_note"] = (
                f"等分但挑战者更便宜（${evidence['cost_challenger']} < ${evidence['cost_champion']}），"
                "只记录不晋升"
            )
    report = {
        "challenger": challenger_identity,
        "champion_before": champion_identity,
        "experiments": {"champion": experiment_champion, "challenger": experiment_challenger},
        "evidence": evidence,
        "promoted": promoted,
    }
    if promoted:
        updated = {
            "candidate_dir": relpath(challenger_dir),
            "hash": challenger_identity["hash"],
            "component_hashes": challenger_identity["component_hashes"],
            "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "evidence": evidence,
        }
        write_json(CHAMPION_PATH, updated)
        report["champion_after"] = updated
        report["inheritance"] = run_inheritance_check(args, config, challenger_dir, challenger, challenger_identity)
    else:
        report["champion_after"] = champion_identity
    report["total_cost_usd"] = round(evidence["cost_champion"] + evidence["cost_challenger"], 4)
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="晋升与继承验证：dev 集配对评测后按预注册规则更新冠军")
    parser.add_argument("--candidate-dir", required=True)
    parser.add_argument("--config", default="experiments/stage4/config.json")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))
    report = run_promotion(args, config)
    out_path = resolve_path(config["stage_root"]) / "promote_report.json"
    write_json(out_path, report)
    print(f"[promote] promoted={report['promoted']} 报告写入 {out_path}", flush=True)


if __name__ == "__main__":
    main()
