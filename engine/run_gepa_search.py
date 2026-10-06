import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")
sys.path.insert(0, str(REPO_ROOT))

import gepa
from appworld import load_task_ids, update_root
from gepa.utils.stop_condition import MaxCandidateProposalsStopper

update_root(os.environ["APPWORLD_ROOT"])

from engine.gepa_adapter import (
    COMPONENTS,
    GnosisAppWorldAdapter,
    build_reflection_lm,
    candidate_hash,
    prefill_seed_cache,
)

SMOKE_OVERRIDE_KEYS = (
    "run_dir",
    "experiment_prefix",
    "max_metric_calls",
    "max_candidate_proposals",
    "max_reflection_cost_usd",
    "task_cost_cap_usd",
)


class TaskCostStopper:
    """任务执行费用触顶即停；每次被调顺带把账本（含反思费用）刷盘。"""

    def __init__(self, adapter: GnosisAppWorldAdapter, cap_usd: float, reflection_lm):
        self.adapter = adapter
        self.cap_usd = float(cap_usd)
        self.reflection_lm = reflection_lm

    def __call__(self, gepa_state) -> bool:
        self.adapter.flush_ledger(self.reflection_lm.total_cost)
        return self.adapter.ledger["task_cost_usd"] >= self.cap_usd


class CountingMaxProposalsStopper:
    """包装 MaxCandidateProposalsStopper，暴露当前提议计数供结果落盘。"""

    def __init__(self, max_proposals: int):
        self.max_proposals = max_proposals
        self._inner = MaxCandidateProposalsStopper(max_proposals)
        self.count = 0

    def __call__(self, gepa_state) -> bool:
        self.count = max(gepa_state.i + 1, 0)
        return self._inner(gepa_state)


class SingleComponentSelector:
    """定向臂：每轮只提议指定组件。"""

    def __init__(self, component: str):
        self.component = component

    def __call__(self, state, trajectories, subsample_scores, candidate_idx, candidate) -> list[str]:
        return [self.component]


def load_seed_candidate() -> dict:
    base = REPO_ROOT / "candidates" / "base_v2"
    return {
        "W": (base / "W" / "workflow.ts").read_text(encoding="utf-8"),
        "T": (base / "T" / "wrapper.ts").read_text(encoding="utf-8"),
        "C": (base / "C" / "context.ts").read_text(encoding="utf-8"),
        "S": "",
    }


def load_task_list(config: dict, smoke: dict, smoke_mode: bool) -> list[str]:
    if smoke_mode and smoke.get("task_ids_from_stage2_perturbation_subset"):
        stage2_config = json.loads(
            (REPO_ROOT / "experiments" / "stage2" / "config.json").read_text(encoding="utf-8")
        )
        return list(stage2_config["perturbation_subset"])
    task_ids = load_task_ids(config["tasks"]["dataset"])
    digest = hashlib.sha256("\n".join(task_ids).encode()).hexdigest()
    expected_digest = config["tasks"]["list_sha256"]
    if len(task_ids) != config["tasks"]["count"] or digest != expected_digest:
        raise ValueError(
            f"任务清单与 config 不一致：count={len(task_ids)}（期望 {config['tasks']['count']}），"
            f"sha256={digest}（期望 {expected_digest}）"
        )
    return task_ids


def resolve_stop_reason(adapter, proposals_stopper, result, search: dict) -> str:
    if adapter.ledger["task_cost_usd"] >= search["task_cost_cap_usd"]:
        return "task_cost_cap"
    if proposals_stopper.count >= search["max_candidate_proposals"]:
        return "max_candidate_proposals"
    if result.total_metric_calls >= search["max_metric_calls"]:
        return "max_metric_calls"
    return "completed"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="experiments/stage3_v2/config.json")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--once-component", choices=COMPONENTS, default=None)
    args = parser.parse_args()

    config = json.loads((REPO_ROOT / args.config).read_text(encoding="utf-8"))
    stage_root = REPO_ROOT / config["stage_root"]
    search = dict(config["gepa"])
    smoke = config.get("smoke", {})
    if args.smoke:
        for key in SMOKE_OVERRIDE_KEYS:
            if key in smoke:
                search[key] = smoke[key]

    task_ids = load_task_list(config, smoke, args.smoke)
    seed_candidate = load_seed_candidate()
    adapter = GnosisAppWorldAdapter(
        config_path=args.config,
        experiment_prefix=search["experiment_prefix"],
        num_processes=search["num_processes"],
        seed=search["seed"],
    )
    seed_hash = candidate_hash(seed_candidate)
    if "seed_cache_prefill" in config:
        existing, written = prefill_seed_cache(
            seed_hash,
            task_ids,
            config["seed_cache_prefill"],
            adapter.score_cache_dir,
            adapter.protocol_fingerprint(),
        )
        print(
            f"[search] 种子候选 hash={seed_hash} 任务数={len(task_ids)} "
            f"缓存预填：已有 {existing} 条，本次写入 {written} 条",
            flush=True,
        )
    else:
        print(f"[search] 种子候选 hash={seed_hash} 任务数={len(task_ids)} 无 seed_cache_prefill，跳过预填", flush=True)
    print(
        f"[search] 预算：max_metric_calls={search['max_metric_calls']} "
        f"max_reflection_cost=${search['max_reflection_cost_usd']} "
        f"task_cost_cap=${search['task_cost_cap_usd']} run_dir={search['run_dir']}",
        flush=True,
    )

    dataset = [{"task_id": task_id} for task_id in task_ids]
    reflection_lm = build_reflection_lm(config)
    # 提议由 adapter.propose_new_texts 负责，reflection_prompt_template 传了会被 gepa 断言拒绝
    adapter.attach_reflection_lm(reflection_lm)
    proposals_stopper = CountingMaxProposalsStopper(search["max_candidate_proposals"])
    module_selector = (
        SingleComponentSelector(args.once_component)
        if args.once_component
        else search["module_selector"]
    )
    result = gepa.optimize(
        seed_candidate=seed_candidate,
        trainset=dataset,
        valset=dataset,
        adapter=adapter,
        reflection_lm=reflection_lm,
        candidate_selection_strategy=search["candidate_selection_strategy"],
        frontier_type=search["frontier_type"],
        use_merge=search["use_merge"],
        module_selector=module_selector,
        reflection_minibatch_size=search["reflection_minibatch_size"],
        skip_perfect_score=search["skip_perfect_score"],
        perfect_score=search["perfect_score"],
        cache_evaluation=search["cache_evaluation"],
        track_best_outputs=search["track_best_outputs"],
        max_metric_calls=search["max_metric_calls"],
        max_reflection_cost=search["max_reflection_cost_usd"],
        # skip 空转不消耗 metric 预算，提议数必须单独设上限
        stop_callbacks=[
            TaskCostStopper(adapter, search["task_cost_cap_usd"], reflection_lm),
            proposals_stopper,
        ],
        run_dir=str(REPO_ROOT / search["run_dir"]),
        seed=search["seed"],
        raise_on_exception=search["raise_on_exception"],
        display_progress_bar=False,
    )
    adapter.flush_ledger(reflection_lm.total_cost)

    print(
        f"[search] total_metric_calls={result.total_metric_calls} "
        f"num_candidates={len(result.candidates)} best_idx={result.best_idx}",
        flush=True,
    )
    print(f"[search] reflection_cost=${reflection_lm.total_cost:.4f}", flush=True)
    print(f"[search] ledger={json.dumps(adapter.ledger, ensure_ascii=False)}", flush=True)

    payload = {
        # candidates 存 JSON 字符串数组，与 engine/extract_pairs.py 的 parse_candidates 口径一致
        "candidates": [
            json.dumps(candidate, ensure_ascii=False) for candidate in result.candidates
        ],
        "parents": result.parents,
        "val_aggregate_scores": result.val_aggregate_scores,
        "val_subscores": result.val_subscores,
        "total_metric_calls": result.total_metric_calls,
        "best_idx": result.best_idx,
        "task_ids": task_ids,
        "ledger": adapter.ledger,
        "seed_candidate_hash": seed_hash,
        "reflection_cost_usd": reflection_lm.total_cost,
        "total_cost_usd": adapter.ledger["task_cost_usd"] + reflection_lm.total_cost,
        "num_proposals": proposals_stopper.count,
        "stop_reason": resolve_stop_reason(adapter, proposals_stopper, result, search),
        "protocol_fingerprint": adapter.protocol_fingerprint(),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    out_path = stage_root / ("smoke_result.json" if args.smoke else "search_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"[search] 结果写入 {out_path}", flush=True)
    adapter.close()


if __name__ == "__main__":
    main()
