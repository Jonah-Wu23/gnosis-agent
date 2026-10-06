import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.agent_runtime import load_candidate
from engine.gepa_adapter import candidate_hash


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把模型生成的机制候选装配进配对池：与搜索种子同父（固定 base_v2），"
        "val_subscores 来自各候选的全量 train 评测摘要"
    )
    parser.add_argument("--search-result", required=True, help="主搜索结果（提供种子及其 valset 向量）")
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--pool", required=True, nargs="+",
                        help="候选目录:摘要json 对，如 experiments/mechanism/W/candidate:experiments/stage3_v2/pool_mechW_r1_summary.json")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result = json.loads((REPO_ROOT / args.search_result).read_text(encoding="utf-8"))
    seed = json.loads(result["candidates"][0])
    task_ids = result["task_ids"]
    candidates = [result["candidates"][0]]
    parents = [result["parents"][0]]
    val_subscores = [result["val_subscores"][0]]

    for spec in args.pool:
        candidate_dir, summary_path = spec.split(":", 1)
        candidate = load_candidate(str(REPO_ROOT / candidate_dir))
        if candidate.interface_version != "2":
            raise ValueError(f"池候选必须是 v2：{candidate_dir}")
        sources = candidate.component_sources
        digest = candidate_hash(sources)
        # 池候选与种子同父：除一个组件外其余必须与种子逐字节一致
        changed = [c for c in ("W", "T", "C", "S") if sources[c] != seed[c]]
        if len(changed) != 1:
            raise ValueError(f"池候选 {candidate_dir} 相对种子改了 {changed}，不是单组件子代")
        # 纳入 gepa_candidates，extract_pairs 才能按哈希找到 normalized.json
        target = REPO_ROOT / args.stage_root / "gepa_candidates" / digest
        if not target.exists():
            shutil.copytree(candidate.root, target)
        summary = json.loads((REPO_ROOT / summary_path).read_text(encoding="utf-8"))
        wins = {r["task_id"]: (1.0 if r["success"] else 0.0) for r in summary["records"]}
        missing = [t for t in task_ids if t not in wins]
        if missing:
            raise ValueError(f"{summary_path} 缺 {len(missing)} 个任务的记录：{missing[:3]}...")
        candidates.append(json.dumps(sources, ensure_ascii=False))
        parents.append([0])
        val_subscores.append(wins)

    payload = {
        "candidates": candidates,
        "parents": parents,
        "val_subscores": val_subscores,
        "task_ids": task_ids,
        "assembled_from": args.search_result,
        "pool_specs": args.pool,
    }
    out_path = REPO_ROOT / args.out
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"配对池写出: {out_path}（{len(candidates)} 个候选）", flush=True)


if __name__ == "__main__":
    main()
