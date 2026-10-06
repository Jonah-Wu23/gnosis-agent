import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engine.agent_runtime import normalize_component_text

# AppWorld 官方评分逐任务为二值成功，dev 分即 0/1；取满分记为成功
SUCCESS_THRESHOLD = 1.0
COMPONENTS = ("W", "T", "C", "S")


def load_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve(path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else REPO_ROOT / path


def candidate_hash(candidate: dict) -> str:
    payload = json.dumps(sorted(candidate.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def parse_candidates(result: dict) -> list[dict]:
    return [json.loads(text) for text in result["candidates"]]


def parent_index(result: dict, index: int) -> int | None:
    # 种子候选的 parents 为 [null] 或 []
    parents = result["parents"][index]
    if not parents or parents[0] is None:
        return None
    return int(parents[0])


def success_vector(result: dict, index: int) -> dict[str, float]:
    """val_subscores 的键先按下标映射到 task_ids，映射失败时把键本身当 task_id。"""
    task_ids = result["task_ids"]
    vector = {}
    for key, score in (result["val_subscores"][index] or {}).items():
        try:
            position = int(key)
        except (TypeError, ValueError):
            task_id = key
        else:
            task_id = task_ids[position] if 0 <= position < len(task_ids) else key
        vector[task_id] = score
    return vector


def wins(vector: dict[str, float]) -> set[str]:
    return {task_id for task_id, score in vector.items() if score >= SUCCESS_THRESHOLD}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def legacy_normalized_hashes(candidate: dict) -> dict:
    """v1 组件的归一化：W/S 取 strip 后 sha256，T/C 只取运行时实际使用的字段。"""
    return {
        component: (
            _sha256_text(candidate[component].strip())
            if component in ("W", "S")
            else normalize_component_text(component, candidate[component])
        )
        for component in COMPONENTS
    }


def normalized_hashes(gepa_candidates_dir: Path, candidate: dict, legacy_v1: bool) -> dict:
    if legacy_v1:
        return legacy_normalized_hashes(candidate)
    path = gepa_candidates_dir / candidate_hash(candidate) / "normalized.json"
    if not path.exists():
        raise FileNotFoundError(f"候选缺少 normalized.json，无法判断有效变异：{path}")
    return load_json(path)


def collect_profiles(
    result: dict, gepa_candidates_dir: Path, legacy_v1: bool
) -> list[dict]:
    candidates = parse_candidates(result)
    hashes = [
        normalized_hashes(gepa_candidates_dir, candidate, legacy_v1) for candidate in candidates
    ]
    profiles = []
    for index, candidate in enumerate(candidates):
        parent = parent_index(result, index)
        vector = success_vector(result, index)
        win_set = wins(vector)
        changed: list[str] | None = None
        noop = False
        gained: list[str] = []
        lost: list[str] = []
        if parent is not None:
            changed = [name for name in COMPONENTS if hashes[index][name] != hashes[parent][name]]
            noop = not changed
            parent_wins = wins(success_vector(result, parent))
            gained = sorted(win_set - parent_wins)
            lost = sorted(parent_wins - win_set)
        profiles.append(
            {
                "index": index,
                "hash": candidate_hash(candidate),
                "parent_idx": parent,
                "changed_components": changed,
                "noop": noop,
                "wins": win_set,
                "gained": gained,
                "lost": lost,
            }
        )
    return profiles


def extract_pairs(profiles: list[dict], rules: dict) -> list[dict]:
    """同父 + 改动组件不同 + 双侧非 noop + 每侧 gained ≥ 1 + 互补数达标。"""
    min_complementary = rules["min_complementary_each_side"]
    eligible = [p for p in profiles if p["parent_idx"] is not None]
    pairs = []
    for left in range(len(eligible)):
        for right in range(left + 1, len(eligible)):
            a, b = eligible[left], eligible[right]
            if a["parent_idx"] != b["parent_idx"]:
                continue
            if a["changed_components"] == b["changed_components"]:
                continue
            if a["noop"] or b["noop"]:
                continue
            if not a["gained"] or not b["gained"]:
                continue
            only_a = sorted(a["wins"] - b["wins"])
            only_b = sorted(b["wins"] - a["wins"])
            complementary = min(len(only_a), len(only_b))
            if complementary < min_complementary:
                continue
            pairs.append(
                {
                    "a_idx": a["index"],
                    "b_idx": b["index"],
                    "parent_idx": a["parent_idx"],
                    "components": {
                        "a": a["changed_components"],
                        "b": b["changed_components"],
                    },
                    "only_a": only_a,
                    "only_b": only_b,
                    "gained_a": a["gained"],
                    "gained_b": b["gained"],
                    "lost_a": a["lost"],
                    "lost_b": b["lost"],
                    "complementary": complementary,
                }
            )
    pairs.sort(key=lambda pair: (-pair["complementary"], pair["a_idx"], pair["b_idx"]))
    return pairs[: rules["max_pairs"]]


def build_payload(profiles: list[dict], pairs: list[dict], rules: dict) -> dict:
    return {
        "rules": rules,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "candidates": [
            {
                "idx": profile["index"],
                "hash": profile["hash"],
                "parent_idx": profile["parent_idx"],
                "changed_components": profile["changed_components"],
                "noop": profile["noop"],
                "wins": sorted(profile["wins"]),
                "gained": profile["gained"],
                "lost": profile["lost"],
            }
            for profile in profiles
        ],
        "pairs": pairs,
        "excluded_noop": [profile["index"] for profile in profiles if profile["noop"]],
    }


def print_summary(payload: dict) -> None:
    print(f"候选总数 {len(payload['candidates'])}，配对规则：{payload['rules']['rule']}")
    for candidate in payload["candidates"]:
        changed = (
            "（种子）" if candidate["changed_components"] is None else ",".join(candidate["changed_components"])
        )
        print(
            f"  候选 {candidate['idx']}: parent={candidate['parent_idx']} "
            f"changed=[{changed}] noop={candidate['noop']} "
            f"gained={candidate['gained']} lost={candidate['lost']}"
        )
    parent_histogram: dict[int | None, int] = {}
    for candidate in payload["candidates"]:
        if candidate["parent_idx"] is not None:
            parent_histogram[candidate["parent_idx"]] = (
                parent_histogram.get(candidate["parent_idx"], 0) + 1
            )
    for candidate in payload["candidates"]:
        if candidate["parent_idx"] is not None and parent_histogram[candidate["parent_idx"]] < 2:
            print(
                f"  候选 {candidate['idx']} 的父版 {candidate['parent_idx']} 没有兄弟候选，不参与配对"
            )
    print(f"excluded_noop={payload['excluded_noop']}")
    print(f"pairs={json.dumps(payload['pairs'], ensure_ascii=False)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="从 GEPA 搜索结果抽取同父互补候选对（§5.4）")
    parser.add_argument("--search-result", required=True)
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--out", default=None)
    parser.add_argument("--legacy-v1", action="store_true")
    args = parser.parse_args()

    stage_root = resolve(args.stage_root)
    config = load_json(stage_root / "config.json")
    rules = config["pair_extraction"]
    result = load_json(resolve(args.search_result))
    profiles = collect_profiles(
        result, stage_root / "gepa_candidates", legacy_v1=args.legacy_v1
    )
    pairs = extract_pairs(profiles, rules)
    payload = build_payload(profiles, pairs, rules)

    out_path = resolve(args.out) if args.out else stage_root / "pairs.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print_summary(payload)
    print(f"写出: {out_path}")


if __name__ == "__main__":
    main()
