import argparse
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")
sys.path.insert(0, str(REPO_ROOT))

from appworld import AppWorld, load_task_ids, update_root
from appworld.common.path_store import path_store

update_root(os.environ["APPWORLD_ROOT"])

from engine.agent_runtime import chunk_and_return, load_candidate
from engine.appworld_vendor.react_code_agent import SimplifiedReActCodeAgent
from engine.component_host import CandidateError, ComponentHost
from engine.guard_hardening import GUARD_HARDENING_VERSION, install as install_guard_hardening

# 官方 SafetyGuard 未覆盖的进程执行/动态加载面在入口处补齐；所有任务执行都经本文件子进程
install_guard_hardening()


def load_config(path: str) -> dict:
    with open(Path(REPO_ROOT) / path, encoding="utf-8") as f:
        return json.load(f)


def build_component_host(config: dict) -> ComponentHost:
    return ComponentHost.from_config(config)


def build_agent(
    candidate, config, component_host: ComponentHost
) -> SimplifiedReActCodeAgent:
    if candidate.interface_version != "2":
        raise ValueError(
            f"v1 候选已归档，请用 v2（interface_version=2）：{candidate.root} 是 "
            f"interface_version={candidate.interface_version}"
        )
    model_config = {
        "rate_card": config["rate_card"],
        "max_tokens": config["sampling"]["max_tokens"],
        "temperature": config["sampling"]["temperature"],
        "seed": config["sampling"]["seed"],
        "max_retries": config["sampling"]["max_retries"],
        "retry_after_n_seconds": config["sampling"]["retry_after_n_seconds"],
    }
    return SimplifiedReActCodeAgent(
        component_host=component_host,
        candidate_id=candidate.name,
        skills_text=candidate.skills_text,
        ignore_multiple_calls=config["agent"]["ignore_multiple_calls"],
        model_config=model_config,
        appworld_config=config["appworld_config"],
        logger_config={"verbose": True},
        usage_tracker_config=config["usage_caps"],
        max_steps=config["agent"]["max_steps"],
        log_lm_calls=True,
        skip_if_finished=True,
    )


def run_shard(args, config) -> None:
    candidate = load_candidate(str(Path(REPO_ROOT) / args.candidate))
    if args.task_ids:
        task_ids = args.task_ids
    else:
        task_ids = load_task_ids(args.dataset)
    task_ids = chunk_and_return(
        task_ids, num_chunks=args.num_processes, chunk_index=args.process_index, balanced=True
    )
    outputs_dir = Path(path_store.experiment_outputs) / args.experiment
    outputs_dir.mkdir(parents=True, exist_ok=True)
    with build_component_host(config) as component_host:
        # 装载前先做宿主校验：不合法的候选在此刻失败，不浪费任务运行
        validation = component_host.validate(
            {
                component: source
                for component, source in candidate.component_sources.items()
                if component in ("W", "T", "C")
            }
        )
        if not validation.get("ok"):
            raise ValueError(
                f"候选 {candidate.name} 未通过宿主校验：{validation.get('errors')}"
            )
        component_host.load(candidate.name, candidate.component_files)
        agent = build_agent(candidate, config, component_host)
        try:
            _run_tasks(agent, candidate, args, config, task_ids, outputs_dir)
        finally:
            agent.close_component_log()
        component_host.unload(candidate.name)


def _run_tasks(agent, candidate, args, config, task_ids, outputs_dir) -> None:
    run_meta = {
        "candidate": candidate.name,
        "candidate_hashes": candidate.component_hashes,
        "guard_hardening": GUARD_HARDENING_VERSION,
        "config_sha256": hashlib.sha256(
            json.dumps(config, sort_keys=True).encode()
        ).hexdigest(),
        "task_ids": task_ids,
        "num_processes": args.num_processes,
        "process_index": args.process_index,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    with open(outputs_dir / f"run_meta_p{args.process_index}.json", "w", encoding="utf-8") as f:
        json.dump(run_meta, f, ensure_ascii=False, indent=1)
    agent.logger.initialize(
        experiment_name=args.experiment,
        num_tasks=len(task_ids),
        num_processes=args.num_processes,
        process_index=args.process_index,
    )
    infra_failures = []
    consecutive_infra = 0
    AppWorld.init_defaults.update(experiment_name=args.experiment, **config["appworld_config"])
    for task_id in task_ids:
        if agent.is_finished(args.experiment, task_id):
            print(f"Skipping already finished task: {task_id}", flush=True)
            continue
        try:
            agent.solve_task(task_id)
            agent.set_finished(args.experiment, task_id)
            consecutive_infra = 0
        except CandidateError as exception:
            # 候选自身失败：该任务按 0 分计，打 finished 标记让官方评分正常出分
            agent.log_usage()
            agent.set_finished(args.experiment, task_id)
            consecutive_infra = 0
            print(f"CANDIDATE_FAILURE task={task_id} {exception}", flush=True)
        except Exception:
            # 基础设施失败：保留原异常，单独计数，批次继续；与候选自身失败分开统计
            infra_failures.append({"task_id": task_id, "traceback": traceback.format_exc()})
            consecutive_infra += 1
            print(f"INFRA_FAILURE task={task_id}", flush=True)
            if consecutive_infra >= 3:
                _write_infra(outputs_dir, args.process_index, infra_failures)
                raise RuntimeError(f"连续 {consecutive_infra} 个任务基础设施失败，中止批次")
    _write_infra(outputs_dir, args.process_index, infra_failures)
    print(
        f"[done] shard {args.process_index}: {len(task_ids) - len(infra_failures)} 完成，"
        f"{len(infra_failures)} 个基础设施失败",
        flush=True,
    )


def _write_infra(outputs_dir: Path, process_index: int, infra_failures: list) -> None:
    with open(
        outputs_dir / f"infra_failures_p{process_index}.json", "w", encoding="utf-8"
    ) as f:
        json.dump(infra_failures, f, ensure_ascii=False, indent=1)


def evaluate_and_summarize(args, config) -> None:
    from appworld.evaluator import evaluate_tasks
    from appworld.common.utils import table_data_to_string, write_file, write_json
    from appworld.evaluator import Metric

    outputs_dir = Path(path_store.experiment_outputs) / args.experiment
    meta_files = sorted(outputs_dir.glob("run_meta_p*.json"))
    if not meta_files:
        raise FileNotFoundError(f"{outputs_dir} 下没有 run_meta_p*.json，先跑 run 分片")
    task_ids, candidates, infra_failures = [], set(), []
    for meta_file in meta_files:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        task_ids.extend(meta["task_ids"])
        candidates.add(meta["candidate"])
    # 补跑会覆盖同名 run_meta 并重复登记任务，按 task_id 去重保持首次出现顺序
    task_ids = list(dict.fromkeys(task_ids))
    for infra_file in sorted(outputs_dir.glob("infra_failures_p*.json")):
        infra_failures.extend(json.loads(infra_file.read_text(encoding="utf-8")))
    # 基础设施失败以"当前无 finished 标记"为准：补跑成功的任务不再计入
    infra_task_ids = {
        f["task_id"]
        for f in infra_failures
        if not (outputs_dir / "tasks" / f["task_id"] / "misc" / "finished").exists()
    }

    evaluation_dict = evaluate_tasks(
        task_ids=task_ids,
        experiment_name=args.experiment,
        suppress_errors=True,
        include_details=True,
        save_reports=True,
    )
    label = args.dataset or "subset"
    evaluations_directory = outputs_dir / "evaluations"
    evaluations_directory.mkdir(exist_ok=True)
    write_json(evaluation_dict, str(evaluations_directory / f"{label}.json"), silent=True)
    data = Metric.build_report(evaluation_dict)
    write_file(table_data_to_string(data), str(evaluations_directory / f"{label}.txt"))

    records = []
    for task_id in task_ids:
        misc = outputs_dir / "tasks" / task_id / "misc"
        usage = {}
        usage_path = misc / "usage.json"
        if usage_path.exists():
            usage = json.loads(usage_path.read_text(encoding="utf-8"))["task"]
        steps = 0
        log_path = misc / "agent_log.jsonl"
        if log_path.exists():
            with open(log_path, encoding="utf-8") as f:
                steps = sum(1 for line in f if '"role": "agent"' in line)
        individual = evaluation_dict.get("individual", {}).get(task_id, {})
        records.append(
            {
                "task_id": task_id,
                "scenario": task_id.split("_")[0],
                "success": bool(individual.get("success", False)),
                "num_tests": individual.get("num_tests"),
                "steps": steps,
                "usage": usage,
                "infrastructure_failed": task_id in infra_task_ids,
            }
        )
    finished = [r for r in records if not r["infrastructure_failed"]]
    total_cost = sum(r["usage"].get("cost", 0.0) for r in records)
    summary = {
        "experiment": args.experiment,
        "candidates": sorted(candidates),
        "num_tasks": len(task_ids),
        "num_infrastructure_failed": len(infra_task_ids),
        "aggregate": evaluation_dict["aggregate"],
        "task_goal_completion_finished_only": round(
            100 * sum(r["success"] for r in finished) / max(len(finished), 1), 1
        ),
        "total_cost_usd": round(total_cost, 4),
        "mean_cost_per_task_usd": round(total_cost / max(len(records), 1), 4),
        "total_input_tokens": sum(r["usage"].get("input_tokens", 0) for r in records),
        "total_output_tokens": sum(r["usage"].get("output_tokens", 0) for r in records),
        "total_cached_input_tokens": sum(
            r["usage"].get("cached_input_tokens", 0) for r in records
        ),
        "total_reasoning_tokens": sum(r["usage"].get("reasoning_tokens", 0) for r in records),
        "infrastructure_failures": infra_failures,
        "records": records,
    }
    # 汇总落到 config 所在目录：stage2 配置在 experiments/stage2/ 下，行为不变
    out_path = Path(REPO_ROOT) / Path(args.config).parent / f"{args.experiment}_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(f"summary written: {out_path}", flush=True)
    print(
        f"TGC={summary['aggregate']['task_goal_completion']} "
        f"SGC={summary['aggregate']['scenario_goal_completion']} "
        f"cost=${summary['total_cost_usd']} mean=${summary['mean_cost_per_task_usd']}",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", default="candidates/base")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--task-ids", nargs="*", default=None)
    parser.add_argument("--num-processes", type=int, default=1)
    parser.add_argument("--process-index", type=int, default=0)
    parser.add_argument("--config", default="experiments/stage2/config.json")
    parser.add_argument("--evaluate-only", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.evaluate_only:
        evaluate_and_summarize(args, config)
    else:
        run_shard(args, config)


if __name__ == "__main__":
    main()
