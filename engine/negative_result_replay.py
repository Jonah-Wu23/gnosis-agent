import argparse
import asyncio
import copy
import hashlib
import json
import os
import subprocess
from collections import Counter, defaultdict
from pathlib import Path, PureWindowsPath

os.environ["GNOSIS_DISABLE_MODEL_CALLS"] = "1"
os.environ["AUTOMATIONBENCH_STRICT_ASSERTIONS"] = "1"

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "experiments/negative_result_offline/replay"


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def differences(old, new, pointer=""):
    if type(old) is not type(new):
        return [{"pointer": pointer, "saved": old, "replayed": new}]
    if isinstance(old, dict):
        result = []
        for key in sorted(old.keys() | new.keys()):
            child = pointer + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in old or key not in new:
                result.append({"pointer": child, "saved": old.get(key), "replayed": new.get(key),
                               "saved_present": key in old, "replayed_present": key in new})
            else:
                result.extend(differences(old[key], new[key], child))
        return result
    if isinstance(old, list):
        if len(old) != len(new):
            return [{"pointer": pointer, "saved": old, "replayed": new}]
        return [d for i, (a, b) in enumerate(zip(old, new)) for d in differences(a, b, pointer + f"/{i}")]
    return [] if old == new else [{"pointer": pointer, "saved": old, "replayed": new}]


def translate(value, identities):
    if isinstance(value, dict):
        return {k: translate(v, identities) for k, v in value.items()}
    if isinstance(value, list):
        return [translate(v, identities) for v in value]
    if isinstance(value, str):
        for old, new in sorted(identities.items(), key=lambda x: -len(x[0])):
            value = value.replace(old, new)
    elif isinstance(value, int) and str(value) in identities:
        value = int(identities[str(value)])
    return value


def decoded(raw):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def strings(value):
    if isinstance(value, dict):
        return {s for child in value.values() for s in strings(child)}
    if isinstance(value, list):
        return {s for child in value for s in strings(child)}
    return {value} if isinstance(value, str) else set()


def learn_identities(old, new, identities, evidence, initial_strings, pointer=""):
    if isinstance(old, dict) and isinstance(new, dict):
        for key in old.keys() & new.keys():
            a, b = old[key], new[key]
            # 仅按服务返回的实体身份字段建立对应，全部原值及差异另存。
            identity_fields = {"id", "Id", "ID", "threadId", "thread_id", "message_id", "ts", "key", "uuid",
                               "envelopeId", "templateId", "PaymentID", "payment_id", "InvoiceID", "invoice_id"}
            if key in identity_fields and type(a) is type(b) and isinstance(a, (str, int)) and a != b and str(a) not in initial_strings and str(b) not in initial_strings:
                a, b = str(a), str(b)
                if a in identities and identities[a] != b:
                    raise ValueError(f"实体身份对应冲突：{pointer}/{key}")
                identities[a] = b
                evidence.append({"pointer": pointer + "/" + key, "saved": a, "replayed": b})
            else:
                learn_identities(a, b, identities, evidence, initial_strings, pointer + "/" + key)
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new)):
            learn_identities(a, b, identities, evidence, initial_strings, pointer + f"/{i}")


async def replay_record(path, dataset, output=OUT):
    from automationbench.rubric import create_rubric, partial_credit, task_completed_correctly
    from engine.automation_adapter import EXECUTION_STATE, ScopedAutomationEnv, restore_messages
    from engine.scoped_units import fingerprint

    old = json.loads(path.read_text(encoding="utf-8"))
    index = next(i for i, row in enumerate(dataset) if json.loads(row["info"])["task_name"] == old["task"])
    row = dataset[index]
    if old["task_contract"] != fingerprint(row):
        raise ValueError(f"保存的任务契约与官方定义不一致：{old['execution_id']}")
    env = ScopedAutomationEnv(dataset=dataset.select([index]), rubric=create_rubric(), toolset="api",
                              execution_output=OUT / "environment")
    state = await env.setup_state({"prompt": restore_messages(row["prompt"]), "info": row["info"],
                                   "trajectory": [], "completion": []})
    initial = json.loads(row["info"])["initial_state"]
    initial_runtime = state["world"].model_dump(mode="json")
    initial_strings = strings(initial_runtime)
    identities, identity_evidence, actions = {}, [], []
    for i, action in enumerate(old["actual_tool_actions"]):
        arguments = translate(copy.deepcopy(action["arguments"]), identities)
        token = EXECUTION_STATE.set(state)
        try:
            args = env.update_tool_args(action["tool"], arguments, [], state)
            result = await env.call_tool(action["tool"], args, action["tool_call_id"])
            raw = result.content
        except Exception as error:
            saved_error = decoded(action["raw"])
            if not isinstance(saved_error, dict) or saved_error.get("raised") != type(error).__name__:
                raise
            raw = json.dumps({"raised": type(error).__name__, "message": str(error)}, ensure_ascii=False)
        finally:
            EXECUTION_STATE.reset(token)
        if not isinstance(raw, str):
            raise TypeError(type(raw))
        previous, current = decoded(action["raw"]), decoded(raw)
        learned = []
        learn_identities(previous, current, identities, learned, initial_strings)
        identity_evidence.extend({"action_index": i, **item} for item in learned)
        actions.append({"index": i, "tool": action["tool"], "saved_arguments": action["arguments"],
                        "replayed_arguments": arguments, "saved_raw": action["raw"], "replayed_raw": raw,
                        "raw_equal": action["raw"] == raw,
                        "differences": differences(previous, current),
                        "after_identity_mapping_differences": differences(translate(previous, identities), current)})
    partial = partial_credit(state)
    assertions = state["_assertion_results"]
    final = state["world"].model_dump(mode="json")
    source = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    report = {"execution_id": old["execution_id"], "task": old["task"], "source": source,
              "model_calls": 0, "initial_world": initial, "initial_runtime_world": initial_runtime, "final_world": final,
              "initial_differences": differences(old["initial_world"], initial) if "initial_world" in old else None,
              "final_differences": differences(old["final_world"], final) if "final_world" in old else None,
              "final_after_identity_mapping_differences": differences(translate(old["final_world"], identities), final) if "final_world" in old else None,
              "task_contract": old["task_contract"], "task_contract_equal": True,
              "identity_evidence": identity_evidence, "actions": actions,
              "saved_assertions": old["assertions"], "assertions": assertions,
              "assertion_differences": differences(old["assertions"], assertions),
              "saved_success": old["success"], "success": bool(task_completed_correctly(state)),
              "saved_partial_credit": old["partial_credit"], "partial_credit": partial}
    save(output / "records" / (old["execution_id"] + ".json"), report)
    return compact(report)


def compact(report):
    actions = report["actions"]
    return {k: report[k] for k in ("execution_id", "task", "source", "saved_success", "success",
                                  "saved_partial_credit", "partial_credit")} | {
        "initial_differences": len(report["initial_differences"]) if report["initial_differences"] is not None else None,
        "assertion_differences": len(report["assertion_differences"]),
        "raw_action_differences": sum(not x["raw_equal"] for x in actions),
        "mapped_action_differences": sum(bool(x["after_identity_mapping_differences"]) for x in actions),
        "final_differences": len(report["final_differences"]) if report["final_differences"] is not None else None,
        "mapped_final_differences": len(report["final_after_identity_mapping_differences"]) if report["final_after_identity_mapping_differences"] is not None else None,
        "actions": len(actions), "identity_mappings": len({x["saved"] for x in report["identity_evidence"]})}


async def main(limit=None, workers=8, resume=False, execution_ids=None):
    from automationbench.domains import get_domain_dataset
    from engine.scoped_units import VERSION_FILES, experiment_version

    folder = OUT / "environment"
    folder.mkdir(parents=True, exist_ok=True)
    save(folder / "workflow_audit.json", {
        "script_status": "clear", "experiment_version": experiment_version(folder),
        "reviewed_files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in VERSION_FILES}})
    paths = sorted((ROOT / "experiments/feedback_chain/paid/executions").glob("*/*.json"))
    if execution_ids is not None:
        identities = set(json.loads(Path(execution_ids).read_text(encoding="utf-8")))
        paths = [path for path in paths if path.stem in identities]
        if len(paths) != len(identities):
            raise ValueError("指定的重放身份未全部找到")
    datasets, rows = {}, []
    jobs = []
    for path in paths[:limit]:
        old = json.loads(path.read_text(encoding="utf-8"))
        prior_report = OUT / "records" / (old["execution_id"] + ".json")
        if resume and prior_report.exists():
            rows.append(compact(json.loads(prior_report.read_text(encoding="utf-8"))))
            continue
        domain = old["domain"]
        if domain not in datasets:
            datasets[domain] = get_domain_dataset(domain)
        jobs.append((path, domain))
    semaphore = asyncio.Semaphore(workers)

    async def execute(path, domain):
        async with semaphore:
            result = await replay_record(path, datasets[domain])
            rows.append(result)
            print(json.dumps({"done": len(rows), **result}), flush=True)

    await asyncio.gather(*(execute(path, domain) for path, domain in jobs))
    rows.sort(key=lambda x: x["source"])
    summary = {"model_calls": 0, "executions": len(rows), "available_executions": len(paths),
               "assertions_equal": sum(x["assertion_differences"] == 0 for x in rows),
               "success_equal": sum(x["success"] == x["saved_success"] for x in rows),
               "initial_equal": sum(x["initial_differences"] == 0 for x in rows),
               "totals": {k: sum(x[k] for x in rows) for k in ("actions", "raw_action_differences", "mapped_action_differences", "final_differences", "mapped_final_differences")},
               "rows": rows}
    save(OUT / ("summary.json" if limit is None and execution_ids is None else "selected_summary.json" if execution_ids is not None else "sample_summary.json"), summary)


def export_contracts():
    from automationbench.domains import get_domain_dataset
    from automationbench.runner import compute_allowed_services, strip_none_values
    from engine.scoped_units import fingerprint

    records = []
    for domain in ("finance", "hr", "marketing", "operations", "sales", "support"):
        for row in get_domain_dataset(domain):
            info = json.loads(row["info"])
            clean = strip_none_values(info)
            records.append({"task": info["task_name"], "domain": domain, "task_contract": fingerprint(row),
                            "initial_state": info["initial_state"],
                            "initial_state_digest": fingerprint(info["initial_state"]),
                            "allowed_services": compute_allowed_services(clean["initial_state"], clean["assertions"], clean["zapier_tools"]),
                            "prompt": row["prompt"]})
    save(OUT / "task_contracts.json", records)
    print(json.dumps({"task_contracts": len(records)}), flush=True)


def local_path(value):
    path = PureWindowsPath(value)
    if path.drive.lower() == "e:":
        return Path("/mnt/e").joinpath(*path.parts[1:])
    if path.parts[0].lower() == "\\\\wsl.localhost\\ubuntu\\":
        return Path("/").joinpath(*path.parts[1:])
    return Path(value)


async def history_effects():
    from automationbench.domains import get_domain_dataset
    from engine.scoped_units import fingerprint

    entries = json.loads((ROOT / "experiments/negative_result_offline/requirements/historical_effect_paths.json").read_text(encoding="utf-8"))
    output = OUT / "history_effects"
    datasets, contracts = {}, {}
    for entry in entries:
        domain = entry["task"].split(".")[0]
        if domain not in datasets:
            dataset = get_domain_dataset(domain)
            datasets[domain] = dataset
            contracts.update({json.loads(row["info"])["task_name"]: fingerprint(row) for row in dataset})
    rows, unresolved = [], []
    semaphore = asyncio.Semaphore(8)

    async def execute(entry):
        async with semaphore:
            path = local_path(entry["path"])
            old = json.loads(path.read_text(encoding="utf-8"))
            if old.get("task_contract") != contracts[old["task"]]:
                unresolved.append({**entry, "reason": "保存的任务契约与当前官方定义不一致",
                                   "saved_task_contract": old.get("task_contract"), "official_task_contract": contracts[old["task"]]})
                return
            rows.append(await replay_record(path, datasets[old["domain"]], output))
            print(json.dumps({"history_done": len(rows), **rows[-1]}), flush=True)

    await asyncio.gather(*(execute(entry) for entry in entries))
    save(output / "summary.json", {"model_calls": 0, "requested": len(entries), "executions": len(rows),
                                   "assertions_equal": sum(x["assertion_differences"] == 0 for x in rows),
                                   "success_equal": sum(x["success"] == x["saved_success"] for x in rows),
                                   "rows": rows, "unresolved": unresolved})


def investigate():
    groups = defaultdict(lambda: {"count": 0, "executions": set(), "examples": []})
    mismatch = []
    for path in sorted((OUT / "records").glob("*.json")):
        report = json.loads(path.read_text(encoding="utf-8"))
        if report["assertion_differences"]:
            mismatch.append({"execution_id": report["execution_id"], "task": report["task"],
                             "differences": report["assertion_differences"]})
        scopes = [("final_world", report["final_after_identity_mapping_differences"])]
        scopes.extend(("tool_response", action["after_identity_mapping_differences"])
                      for action in report["actions"])
        for scope, changes in scopes:
            for change in changes:
                shape = "/".join("*" if part.isdigit() else part for part in change["pointer"].split("/"))
                group = groups[(scope, shape)]
                group["count"] += 1
                group["executions"].add(report["execution_id"])
                if len(group["examples"]) < 2:
                    group["examples"].append({"execution_id": report["execution_id"], "task": report["task"], **change})
    records = [{"scope": scope, "pointer_shape": shape, "count": group["count"],
                "executions": len(group["executions"]), "examples": group["examples"]}
               for (scope, shape), group in sorted(groups.items())]
    result = {"executions": len(list((OUT / "records").glob("*.json"))),
              "assertion_mismatches": mismatch, "difference_groups": records}
    save(OUT / "investigation.json", result)
    print(json.dumps({"executions": result["executions"], "assertion_mismatches": len(mismatch),
                      "groups": [{k: x[k] for k in ("scope", "pointer_shape", "count")} for x in records]}), flush=True)


def verify_runtime():
    source = ROOT / "scratch/AutomationBench/automationbench"
    runtime = Path("/home/jonahwu/gnosis-offline-replay/automationbench")
    files = []
    for path in sorted(source.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(source)
        original = hashlib.sha256(path.read_bytes()).hexdigest()
        copied = hashlib.sha256((runtime / relative).read_bytes()).hexdigest()
        if original != copied:
            raise ValueError(f"原装包复制内容不一致：{relative}")
        files.append({"path": relative.as_posix(), "sha256": original, "equal": True})
    save(OUT / "runtime_source.json", {"source": str(source), "runtime": str(runtime),
        "commit": subprocess.check_output(["git", "-C", str(source.parent), "rev-parse", "HEAD"], text=True).strip(),
        "files": files, "all_equal": True, "reason": "避免跨文件系统逐次读取API schema的耗时"})
    print(json.dumps({"runtime_files_equal": len(files)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--contracts", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--investigate", action="store_true")
    parser.add_argument("--verify-runtime", action="store_true")
    parser.add_argument("--history-effects", action="store_true")
    parser.add_argument("--execution-ids", type=Path)
    args = parser.parse_args()
    if args.history_effects:
        asyncio.run(history_effects())
    elif args.verify_runtime:
        verify_runtime()
    elif args.investigate:
        investigate()
    elif args.contracts:
        export_contracts()
    else:
        asyncio.run(main(args.limit, args.workers, args.resume, args.execution_ids))
