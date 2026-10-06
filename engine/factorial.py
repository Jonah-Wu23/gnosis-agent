import argparse
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")

COMPONENTS = ("W", "T", "C", "S")
COMPONENT_FILES = {"W": "W/workflow.ts", "T": "T/wrapper.ts", "C": "C/context.ts"}


# ---------------- 共享运行层（joint_repair / promote / mechanism_check 复用） ----------------


class InfraFailure(RuntimeError):
    """基础设施失败（分片退出码异常或任务无 finished 标记）；候选自身失败不算。"""


def load_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, payload) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def resolve_path(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_dict(candidate) -> dict:
    # v2 候选的四组件源码；S 取原始 skills.md 文本（不是拼好段头的 skills_text），
    # 与 GEPA 候选字典同口径，candidate_hash 才能对上搜索产物里的目录名
    return candidate.component_sources


def expected_candidate_ids(candidate) -> set[str]:
    """契约 §3 未固定 candidate_id 取值：宿主按目录名（GEPA 物化目录即候选 hash）或候选 hash 装载，两者都认。"""
    from engine.gepa_adapter import candidate_hash

    return {candidate.name, candidate_hash(candidate_dict(candidate))}


def identity_of(candidate) -> dict:
    from engine.gepa_adapter import candidate_hash

    return {
        "name": candidate.name,
        "root": str(candidate.root),
        "hash": candidate_hash(candidate_dict(candidate)),
        "component_hashes": candidate.component_hashes,
    }


def build_component_host(config: dict):
    from engine.component_host import ComponentHost

    return ComponentHost.from_config(config)


def materialize_candidate(root: Path, candidate: dict, host, lineage: dict | None = None) -> dict:
    """按契约 §1 落盘候选目录：组件源码 + normalized.json + manifest.json。

    normalized.json 与 lineage.json 必须先于 manifest 写，manifest 覆盖目录内全部文件。
    """
    if root.exists():
        # 生成物目录，重写前清空，避免上一版残留的 S/skills.md 混进新 manifest
        shutil.rmtree(root)
    for component, rel in COMPONENT_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(candidate[component], encoding="utf-8")
    skills = candidate["S"]
    if skills.strip():
        (root / "S").mkdir(parents=True, exist_ok=True)
        (root / "S" / "skills.md").write_text(skills, encoding="utf-8")
    normalized = {
        component: (
            sha256_text(skills.strip())
            if component == "S"
            else host.normalize(component, candidate[component])
        )
        for component in COMPONENTS
    }
    write_json(root / "normalized.json", normalized)
    if lineage is not None:
        write_json(root / "lineage.json", lineage)
    from engine.agent_runtime import write_manifest

    write_manifest(str(root), "2")
    return normalized


def run_shards(candidate_dir, experiment: str, task_ids: list[str], config_path, num_processes: int) -> list[int]:
    num_shards = max(1, min(int(num_processes), len(task_ids)))
    processes = []
    for index in range(num_shards):
        command = [
            sys.executable,
            "engine/run_experiment.py",
            "--candidate",
            str(candidate_dir),
            "--experiment",
            experiment,
            "--task-ids",
            *task_ids,
            "--num-processes",
            str(num_shards),
            "--process-index",
            str(index),
            "--config",
            str(config_path),
        ]
        print(f"[run] {experiment} shard {index + 1}/{num_shards}", flush=True)
        processes.append(subprocess.Popen(command, cwd=str(REPO_ROOT)))
    return [process.wait() for process in processes]


def evaluate_only(candidate_dir, experiment: str, task_ids: list[str], config_path) -> int:
    command = [
        sys.executable,
        "engine/run_experiment.py",
        "--candidate",
        str(candidate_dir),
        "--experiment",
        experiment,
        "--task-ids",
        *task_ids,
        "--config",
        str(config_path),
        "--evaluate-only",
    ]
    print(f"[evaluate] {experiment}", flush=True)
    return subprocess.call(command, cwd=str(REPO_ROOT))


def outputs_dir(experiment: str) -> Path:
    return Path(os.environ["APPWORLD_ROOT"]) / "experiments" / "outputs" / experiment


def misc_dir(experiment: str, task_id: str) -> Path:
    return outputs_dir(experiment) / "tasks" / task_id / "misc"


def summary_path(config_path, experiment: str) -> Path:
    return REPO_ROOT / Path(config_path).parent / f"{experiment}_summary.json"


def read_summary(config_path, experiment: str) -> dict:
    return load_json(summary_path(config_path, experiment))


def read_component_calls(misc: Path) -> list[dict]:
    path = Path(misc) / "component_calls.jsonl"
    if not path.exists():
        return []
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def read_run_meta(experiment: str, process_index: int = 0) -> dict:
    path = outputs_dir(experiment) / f"run_meta_p{process_index}.json"
    return load_json(path)


def require_scores(config_path, experiment: str, task_ids: list[str]) -> dict[str, dict]:
    """读官方评分摘要并做任务级取证：无 finished 标记或 infrastructure_failed 即基础设施失败。"""
    summary = read_summary(config_path, experiment)
    records = {record["task_id"]: record for record in summary["records"]}
    missing = [task_id for task_id in task_ids if task_id not in records]
    if missing:
        raise InfraFailure(f"{experiment} 的汇总缺少任务记录：{missing}")
    broken = [
        task_id
        for task_id in task_ids
        if records[task_id]["infrastructure_failed"]
        or not (misc_dir(experiment, task_id) / "finished").exists()
    ]
    if broken:
        raise InfraFailure(f"{experiment} 有 {len(broken)} 个任务无 finished 标记：{broken}")
    return records


# ---------------- 四组组合干预 ----------------


def load_pair(pairs_path, index: int) -> dict:
    payload = load_json(resolve_path(pairs_path))
    pairs = payload["pairs"]
    if not 0 <= index < len(pairs):
        raise ValueError(f"{pairs_path} 只有 {len(pairs)} 对候选，--pair-index {index} 越界")
    return pairs[index]


def pair_components(pair: dict) -> dict:
    """extract_pairs 的 components 形如 {"a": [组件...], "b": [组件...]}；两侧必须非空且不相交。"""
    components = pair.get("components")
    if not isinstance(components, dict) or set(components) != {"a", "b"}:
        raise ValueError(
            f"pairs.json 的 components 必须是 {{'a': [...], 'b': [...]}}，实际拿到 {components!r}"
        )
    for side in ("a", "b"):
        value = components[side]
        if (
            not isinstance(value, list)
            or not value
            or len(set(value)) != len(value)
            or any(component not in COMPONENTS for component in value)
        ):
            raise ValueError(f"pairs.json 的 components[{side!r}] 必须是无重复的非空组件列表，实际 {value!r}")
    overlap = set(components["a"]) & set(components["b"])
    if overlap:
        raise ValueError(f"A/B 的改动组件相交 {sorted(overlap)}，不构成两因子组合干预")
    return {"a": list(components["a"]), "b": list(components["b"])}


def build_four(parent: dict, candidate_a: dict, candidate_b: dict, components: dict, indexes: dict) -> dict:
    """H00=父版、H10=父版+A 的改动组件、H01=父版+B 的改动组件、H11=父版+A+B。"""
    for side, source in (("a", candidate_a), ("b", candidate_b)):
        changed = sorted(component for component in COMPONENTS if source[component] != parent[component])
        if changed != sorted(components[side]):
            raise ValueError(
                f"候选 {side}(idx={indexes[side]}) 相对父版的源码差异是 {changed}，"
                f"与 pairs.json 声明的 {sorted(components[side])} 不一致"
            )
    four = {name: dict(parent) for name in ("H00", "H10", "H01", "H11")}
    for component in components["a"]:
        four["H10"][component] = candidate_a[component]
        four["H11"][component] = candidate_a[component]
    for component in components["b"]:
        four["H01"][component] = candidate_b[component]
        four["H11"][component] = candidate_b[component]
    return four


def provenance_of(four: dict, components: dict, indexes: dict) -> dict:
    owner = {component: f"A#{indexes['a']}" for component in components["a"]}
    owner.update({component: f"B#{indexes['b']}" for component in components["b"]})
    provenance = {}
    for name in four:
        entry = {}
        for component in COMPONENTS:
            if name == "H00":
                entry[component] = f"parent#{indexes['parent']}"
            elif name == "H10":
                entry[component] = owner[component] if component in components["a"] else f"parent#{indexes['parent']}"
            elif name == "H01":
                entry[component] = owner[component] if component in components["b"] else f"parent#{indexes['parent']}"
            else:
                entry[component] = owner.get(component, f"parent#{indexes['parent']}")
        provenance[name] = entry
    return provenance


def select_tasks(pair: dict, limit: int, seed: int) -> tuple[list[str], dict]:
    """only_a/only_b 优先保留，其余来自 gained/lost 的并集按 seed 随机补足。"""
    complementary = sorted(set(pair["only_a"]) | set(pair["only_b"]))
    others = sorted(
        (set(pair["gained_a"]) | set(pair["gained_b"]) | set(pair["lost_a"]) | set(pair["lost_b"]))
        - set(complementary)
    )
    if len(complementary) >= limit:
        sampled = []
        tasks = complementary[:limit]
    else:
        sampled = sorted(random.Random(seed).sample(others, min(limit - len(complementary), len(others))))
        tasks = complementary + sampled
    return tasks, {
        "limit": limit,
        "seed": seed,
        "complementary": complementary,
        "sampled": sampled,
        "pool": complementary + others,
    }


def factorial_experiment_name(prefix: str, pair_index: int, config_name: str, task_id: str, repeat: int) -> str:
    return f"{prefix}_p{pair_index}_{config_name}_{task_id}_r{repeat}"


def run_experiment_pair(args, config: dict, pair_index: int) -> None:
    from engine.gepa_adapter import candidate_hash

    sources = config["source"]
    for key in ("search_result", "experiment_prefix"):
        if key not in sources:
            raise ValueError(f"config 的 source 节缺少 {key}：{args.config}")
    search_result = load_json(resolve_path(args.search_result))
    candidates = [json.loads(text) for text in search_result["candidates"]]
    pair = load_pair(args.pairs_file, pair_index)
    indexes = {"a": int(pair["a_idx"]), "b": int(pair["b_idx"]), "parent": int(pair["parent_idx"])}
    components = pair_components(pair)
    four = build_four(
        candidates[indexes["parent"]], candidates[indexes["a"]], candidates[indexes["b"]], components, indexes
    )
    provenance = provenance_of(four, components, indexes)
    hashes = {name: candidate_hash(candidate) for name, candidate in four.items()}
    print(f"[factorial] pair {pair_index}: a_idx={indexes['a']} b_idx={indexes['b']} "
          f"parent_idx={indexes['parent']} components=A:{','.join(components['a'])} "
          f"B:{','.join(components['b'])}", flush=True)
    for name in ("H00", "H10", "H01", "H11"):
        entry = " ".join(f"{component}<-{provenance[name][component]}" for component in COMPONENTS)
        print(f"[factorial] {name}: {entry} hash={hashes[name]}", flush=True)

    if args.dry_run:
        return

    stage_root = resolve_path(config["stage_root"])
    pair_dir = stage_root / "factorial" / f"pair{pair_index}"
    tasks, sampling = select_tasks(pair, int(config["factorial"]["tasks_per_pair"]), int(config["sampling"]["seed"]))
    repeats = int(config["factorial"]["repeats"])
    plan = [
        (name, task_id, repeat)
        for name in ("H00", "H10", "H01", "H11")
        for task_id in tasks
        for repeat in range(1, repeats + 1)
    ]
    order_seed = int(config["sampling"]["seed"])
    random.Random(order_seed).shuffle(plan)
    order = [factorial_experiment_name(config["experiment_prefix"], pair_index, *item) for item in plan]

    report = {
        "pair_index": pair_index,
        "pair": pair,
        "components": components,
        "indexes": indexes,
        "hashes": hashes,
        "provenance": provenance,
        "candidate_dirs": {},
        "tasks": tasks,
        "task_sampling": sampling,
        "repeats": repeats,
        "order_seed": order_seed,
        "order": order,
        "runs": {name: {} for name in four},
    }
    host = build_component_host(config)
    try:
        for name in ("H00", "H10", "H01", "H11"):
            candidate_dir = pair_dir / name
            lineage = {
                "kind": "factorial",
                "pair_index": pair_index,
                "config": name,
                "parent_idx": indexes["parent"],
                "source_indexes": {"a": indexes["a"], "b": indexes["b"]},
                "replaced": {
                    **{component: f"A#{indexes['a']}" for component in components["a"]},
                    **{component: f"B#{indexes['b']}" for component in components["b"]},
                },
                "provenance": provenance[name],
                "source_search_result": str(args.search_result),
            }
            materialize_candidate(candidate_dir, four[name], host, lineage)
            report["candidate_dirs"][name] = str(candidate_dir)
        try:
            for name, task_id, repeat in plan:
                experiment = factorial_experiment_name(config["experiment_prefix"], pair_index, name, task_id, repeat)
                print(f"[factorial] shard {name} {task_id} r{repeat}", flush=True)
                returncodes = run_shards(report["candidate_dirs"][name], experiment, [task_id], args.config, 1)
                if any(code != 0 for code in returncodes):
                    raise InfraFailure(f"{experiment} 分片退出码 {returncodes}")
            for name, task_id, repeat in plan:
                experiment = factorial_experiment_name(config["experiment_prefix"], pair_index, name, task_id, repeat)
                if evaluate_only(report["candidate_dirs"][name], experiment, [task_id], args.config) != 0:
                    raise InfraFailure(f"{experiment} 的 --evaluate-only 非零退出")
                record = require_scores(args.config, experiment, [task_id])[task_id]
                report["runs"][name].setdefault(task_id, {})[f"r{repeat}"] = {
                    "success": bool(record["success"]),
                    "cost": float(record["usage"].get("cost", 0.0)),
                    "experiment": experiment,
                }
        except InfraFailure as error:
            report["infrastructure_error"] = f"{type(error).__name__}: {error}"
            write_json(pair_dir / "factorial_report.json", report)
            raise
    finally:
        host.close()

    interaction_terms = []
    details = {}
    lost_items = []
    for task_id in tasks:
        per_repeat = {}
        lost_every_repeat = True
        for repeat in range(1, repeats + 1):
            key = f"r{repeat}"
            cell = {name: report["runs"][name][task_id][key]["success"] for name in four}
            baseline = 1.0 if (cell["H10"] or cell["H01"]) else 0.0
            lost = baseline == 1.0 and not cell["H11"]
            per_repeat[key] = {
                "Y00": float(cell["H00"]),
                "Y10": float(cell["H10"]),
                "Y01": float(cell["H01"]),
                "Y11": float(cell["H11"]),
                "lost": lost,
            }
            interaction_terms.append(cell["H11"] - cell["H10"] - cell["H01"] + cell["H00"])
            lost_every_repeat = lost_every_repeat and lost
        details[task_id] = per_repeat
        if lost_every_repeat:
            lost_items.append(task_id)
    report["interaction"] = sum(interaction_terms) / len(interaction_terms)
    report["degeneration"] = {
        "repeatable_loss": bool(lost_items),
        "lost_items": lost_items,
        "details": details,
    }
    report["total_cost"] = round(
        sum(
            cell["cost"]
            for name in four
            for task_id in report["runs"][name]
            for cell in report["runs"][name][task_id].values()
        ),
        4,
    )
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    write_json(pair_dir / "factorial_report.json", report)
    print(
        f"[factorial] pair {pair_index} 完成：Î={report['interaction']:.4f} "
        f"可重复损失={report['degeneration']['repeatable_loss']} 任务={lost_items} "
        f"费用=${report['total_cost']}",
        flush=True,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="四组组合干预：H00/H10/H01/H11 装配、随机化运行与交互项估计")
    parser.add_argument("--pairs-file", default="experiments/stage3_v2/pairs.json")
    parser.add_argument("--pair-index", type=int, required=True)
    parser.add_argument("--search-result", required=True)
    parser.add_argument("--config", default="experiments/stage4/config.json")
    parser.add_argument("--dry-run", action="store_true", help="只装配四组候选并打印组件来源，不物化、不跑任务")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    config = load_json(resolve_path(args.config))
    run_experiment_pair(args, config, args.pair_index)


if __name__ == "__main__":
    main()
