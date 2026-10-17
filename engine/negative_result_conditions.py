import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from engine.negative_result_history import original_path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "experiments/negative_result_offline"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run():
    registry = [json.loads(line) for line in (OUT / "history/executions.jsonl").read_text(encoding="utf-8").splitlines()]
    contracts = read(OUT / "replay/task_contracts.json")
    by_contract = {row["task_contract"]: row for row in contracts}
    results = []
    for index, entry in enumerate(registry):
        path = original_path(ROOT, entry["evidence_path"])
        if not path.is_file():
            results.append({"execution_id": entry["execution_id"], "task": entry["task"], "status": "source_file_missing", "source": entry["evidence_path"]})
            continue
        record = read(path)
        if record["execution_id"] != entry["execution_id"]:
            raise ValueError("历史原件与登记执行身份不一致")
        contract = by_contract.get(record.get("task_contract"))
        wires = record.get("wire_trace", [])
        batch = path.parents[2]
        request_path = batch / "io" / wires[0]["call"] / "request.json" if wires else None
        request = read(request_path) if request_path and request_path.is_file() else None
        parameters = request["parameters"] if request else None
        generating = parameters.get("sampling_args") if parameters else None
        result = {"execution_id": entry["execution_id"], "task": entry["task"], "experiment": entry["experiment_id"],
                  "source": entry["evidence_path"], "version": record["version"], "experiment_version": record["experiment_version"],
                  "task_contract": record.get("task_contract"), "official_contract_matched": contract is not None,
                  "initial_state_digest": digest(contract["initial_state"]) if contract else None,
                  "initial_state_origin": "official_task_with_exact_saved_contract" if contract else "not_recoverable_from_current_official_contract",
                  "allowed_services": contract["allowed_services"] if contract else None,
                  "actual_first_request_digest": digest(wires[0]["request"]) if wires else None,
                  "generation_parameters": generating, "actual_tool_schema_digest": digest(parameters["tools"]) if parameters else None,
                  "request_file_present": request is not None, "recorded_at": record.get("recorded_at"),
                  "strict_success": record["success"], "actions": len(record.get("actual_tool_actions", [])),
                  "termination": record.get("termination_status"), "execution_error_present": record.get("execution_error") is not None,
                  "source_pointer": "/wire_trace/0" if wires else "/", "status": "complete" if contract and parameters and wires else "partial"}
        results.append(result)
        if (index + 1) % 250 == 0:
            print(json.dumps({"historical_inputs_read": index + 1}, ensure_ascii=False), flush=True)
    groups = defaultdict(list)
    for result in results:
        if result["status"] == "complete":
            key = (result["task"], result["version"], result["experiment_version"], result["task_contract"],
                   result["actual_first_request_digest"], digest(result["generation_parameters"]), result["actual_tool_schema_digest"])
            groups[key].append(result)
    repeated = [{"task": key[0], "version": key[1], "experiment_version": key[2], "executions": len(group),
                 "execution_ids": [r["execution_id"] for r in group], "strict_passes": sum(r["strict_success"] for r in group),
                 "flipped_pairs": sum(a["strict_success"] != b["strict_success"] for i, a in enumerate(group) for b in group[i + 1:]),
                 "all_pairs": len(group) * (len(group) - 1) // 2,
                 "first_at": min(r["recorded_at"] for r in group), "last_at": max(r["recorded_at"] for r in group)}
                for key, group in groups.items() if len(group) > 1]
    summary = {"executions": len(results), "status": dict(Counter(r["status"] for r in results)),
               "official_contracts_matched": sum(r.get("official_contract_matched", False) for r in results),
               "requests_present": sum(r.get("request_file_present", False) for r in results),
               "same_input_groups": len(repeated), "repeated_executions": sum(r["executions"] for r in repeated),
               "pair_count": sum(r["all_pairs"] for r in repeated), "flipped_pairs": sum(r["flipped_pairs"] for r in repeated),
               "qualification": "只有任务契约、实际首请求、生成参数、工具定义、内容与程序版本同时相同才分到同组；初态从精确匹配的官方任务恢复"}
    save(OUT / "completion/historical_input_conditions.json", results)
    save(OUT / "completion/historical_same_input_groups.json", repeated)
    save(OUT / "completion/historical_input_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    run()
