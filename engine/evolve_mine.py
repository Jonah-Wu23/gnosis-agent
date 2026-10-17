import argparse
import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXP = ROOT / "experiments"
DISCOVERY = EXP / "evo_path" / "discovery"
OUT = EXP / "evolve_mine" / "extract"

# WSL 实体的规范形态；Windows 侧符号链接已断，本模块须在 WSL 内运行
RESEARCH = Path.home() / ".gnosis" / "research"
RESEARCH_LEDGER = Path.home() / ".gnosis" / "research_ledger.sqlite"
LEDGERS = {
    "research": RESEARCH_LEDGER,
    "ecai_p1": ROOT / "state" / "ecai_p1_ledger.sqlite",
    "ecai_followup": ROOT / "state" / "ecai_followup_ledger.sqlite",
    "timing_v4": ROOT / "state" / "timing_v4.sqlite",
}

MAX_STEPS = 50  # 实验固定条件：每题最多 50 步
FX = 7.1

_ID_SEG = re.compile(r"^(?=.*\d)[A-Za-z0-9_\-]{12,}$")


def endpoint_tpl(endpoint):
    # 端点规范化：含数字的长 ID 段替换为 *，保留 服务:方法 路径骨架
    if not endpoint:
        return None
    head, _, path = endpoint.partition(" ")
    if not path:
        return endpoint
    segs = ["*" if _ID_SEG.match(s) else s for s in path.split("/")]
    return head + " " + "/".join(segs)


def args_sig(arguments):
    if not isinstance(arguments, dict):
        return None, None
    keys = ",".join(sorted(arguments))
    canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    return keys, hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


def load_call_times():
    # 全部账本的 call -> (at, cost, billed)；execution_id -> 聚合。只读。
    call_info, exec_cost = {}, {}
    for path in LEDGERS.values():
        if not path.exists():
            continue
        with sqlite3.connect("file:%s?mode=ro" % path.as_posix(), uri=True) as db:
            for cid, phase, cost, billed, at in db.execute(
                    "SELECT id, phase, cost, billed, at FROM calls"):
                call_info[cid] = (at, cost or 0.0, billed or 0.0)
                parts = (phase or "").split("/")
                if "execution" in parts and parts[-1] != "execution":
                    slot = exec_cost.setdefault(parts[-1], {"cost": 0.0, "billed": 0.0, "calls": 0})
                    slot["cost"] += cost or 0.0
                    slot["billed"] += billed or 0.0
                    slot["calls"] += 1
    return call_info, exec_cost


def resolve_evidence(evidence_path):
    if evidence_path.startswith("experiments/research/"):
        return RESEARCH / evidence_path[len("experiments/research/"):]
    return ROOT / evidence_path


def schema_gen(record):
    if record.get("knowledge_treatment") is not None or record.get("knowledge_requests") is not None:
        return "v3"
    if record.get("component_invocations") is not None or record.get("component_capability_calls") is not None:
        return "v2"
    return "v1"


def extract_execution(row, call_info):
    record = json.loads(resolve_evidence(row["evidence_path"]).read_text(encoding="utf-8"))
    eid = record["execution_id"]
    boundaries = record.get("boundary_trace") or []
    wires = record.get("wire_trace") or []
    actions = record.get("actual_tool_actions") or []

    # tool_call_id -> (wire 序号, call id, finish_reason)
    call_of = {}
    for wi, wire in enumerate(wires):
        resp = wire.get("response") or {}
        for tc in (resp.get("tool_calls") or []):
            fn = tc.get("function") or {}
            call_of[tc.get("id")] = (wi, wire.get("call"), wire.get("finish_reason"), fn.get("name"))

    steps = []
    for seq, act in enumerate(actions):
        bi = act.get("boundary_index")
        bound = boundaries[bi] if isinstance(bi, int) and 0 <= bi < len(boundaries) else {}
        wi, call, finish, _ = call_of.get(act.get("tool_call_id"), (None, None, None, None))
        at = call_info.get(call, (None, 0.0, 0.0))[0] if call else None
        keys, sig = args_sig(act.get("arguments"))
        steps.append({
            "execution_id": eid, "seq": seq, "tool": act.get("tool"),
            "service": bound.get("service"), "endpoint": bound.get("endpoint"),
            "endpoint_tpl": endpoint_tpl(bound.get("endpoint")),
            "hook": bound.get("hook"),
            "arg_keys": keys, "args_sig": sig,
            "raw_len": len(act.get("raw") or ""),
            "failed": bool(act.get("failed")), "rejected": bool(act.get("rejected")),
            "component_call": bool(act.get("component_call")),
            "boundary_index": bi, "wire_seq": wi, "call": call,
            "finish_reason": finish, "at": at,
        })

    injections = record.get("injections") or []
    n_e = sum(1 for i in injections if i.get("component") == "E")
    n_know = sum(1 for i in injections if i.get("kind") == "knowledge_injection")
    hooks = Counter(b.get("hook") for b in boundaries)
    assertions = record.get("assertions") or []
    symbols = [s["endpoint_tpl"] or s["tool"] or "?" for s in steps]
    counts = Counter(symbols)
    total = len(symbols)
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values()) if total else 0.0
    bigram_same = sum(1 for a, b in zip(symbols, symbols[1:]) if a == b)

    feats = {
        "execution_id": eid, "experiment_id": row["experiment_id"], "arm": row.get("arm"),
        "task": record.get("task"), "domain": record.get("domain"),
        "version": record.get("version"), "repeat": row.get("repeat"),
        "task_role": row.get("task_role"), "schema_gen": schema_gen(record),
        "recorded_at": record.get("recorded_at"),
        "n_actions": total, "n_wire_calls": len(wires), "n_boundaries": len(boundaries),
        "n_tool_discovery": hooks.get("tool_discovery", 0),
        "n_before_call": hooks.get("before_call", 0), "n_after_call": hooks.get("after_call", 0),
        "n_failed": sum(1 for s in steps if s["failed"]),
        "n_rejected": sum(1 for s in steps if s["rejected"]),
        "n_component_calls": sum(1 for s in steps if s["component_call"]),
        "n_distinct_tools": len({s["tool"] for s in steps}),
        "n_distinct_endpoint_tpl": len(set(symbols)),
        "n_services": len({s["service"] for s in steps if s["service"]}),
        "services": sorted({s["service"] for s in steps if s["service"]}),
        "action_entropy": round(entropy, 4),
        "bigram_same_ratio": round(bigram_same / (total - 1), 4) if total > 1 else 0.0,
        "n_injections": len(injections), "n_knowledge_injections": n_know, "n_e_injections": n_e,
        "n_active_extensions": len(record.get("active_extensions") or {}),
        "n_first_effects": len(record.get("first_effect_boundaries") or {}),
        "hit_step_cap": len(wires) >= MAX_STEPS,
        "assertions_total": len(assertions),
        "assertions_passed": sum(1 for a in assertions if a.get("passed")),
        "assertions_excluded": sum(1 for a in assertions if a.get("excluded")),
        "termination_status": record.get("termination_status"),
        "execution_error": (record.get("execution_error") or {}).get("category"),
        "cost_usd": record.get("cost_usd"), "validation_cost_usd": record.get("validation_cost_usd"),
        "duration_s": record.get("duration_s"),
        "ledger_cost_usd": None, "ledger_billed_usd": None, "ledger_calls": None,
        "evidence_path": row["evidence_path"],
    }
    return steps, feats


def extract(source):
    rows = [json.loads(line) for line in (DISCOVERY / "outcome.jsonl").open(encoding="utf-8")
            if line.strip()]
    if source == "research":
        rows = [r for r in rows if r["experiment_id"] == "research"]
    elif source == "ecai":
        rows = [r for r in rows if r["experiment_id"].startswith("ecai_")]
    elif source == "timing":
        rows = [r for r in rows if r["experiment_id"].startswith("timing")]
    rows = [r for r in rows if r.get("kind") == "execution"]

    call_info, exec_cost = load_call_times()
    shard = OUT / "shards"
    shard.mkdir(parents=True, exist_ok=True)
    missing, done = [], 0
    with (shard / f"{source}.steps.jsonl").open("w", encoding="utf-8") as fs, \
         (shard / f"{source}.exec_features.jsonl").open("w", encoding="utf-8") as ff:
        for row in rows:
            path = resolve_evidence(row["evidence_path"])
            if not path.exists():
                missing.append(row["execution_id"])
                continue
            steps, feats = extract_execution(row, call_info)
            cost = exec_cost.get(feats["execution_id"], {"cost": 0.0, "billed": 0.0, "calls": 0})
            feats["ledger_cost_usd"] = round(cost["cost"], 6)
            feats["ledger_billed_usd"] = round(cost["billed"], 6)
            feats["ledger_calls"] = cost["calls"]
            for s in steps:
                fs.write(json.dumps(s, ensure_ascii=False) + "\n")
            ff.write(json.dumps(feats, ensure_ascii=False) + "\n")
            done += 1
    verify = {"source": source, "registered": len(rows), "extracted": done, "missing": missing}
    (shard / f"{source}._verify.json").write_text(json.dumps(verify, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
    print(json.dumps({k: v if k != "missing" else len(v) for k, v in verify.items()}))


def merge():
    shard = OUT / "shards"
    for name in ("steps", "exec_features"):
        with (OUT / f"{name}.jsonl").open("w", encoding="utf-8") as out:
            for part in sorted(shard.glob(f"*.{name}.jsonl")):
                with part.open(encoding="utf-8") as src:
                    for line in src:
                        out.write(line)
    checks = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(shard.glob("*._verify.json"))]
    summary = {"shards": checks,
               "registered": sum(c["registered"] for c in checks),
               "extracted": sum(c["extracted"] for c in checks)}
    (OUT / "_verify.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["extract", "merge"])
    parser.add_argument("--source", choices=["research", "ecai", "timing"], default="research")
    args = parser.parse_args()
    if args.command == "extract":
        extract(args.source)
    else:
        merge()


if __name__ == "__main__":
    main()
