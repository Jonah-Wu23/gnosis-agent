import argparse
import base64
import json
import hashlib
from collections import Counter, defaultdict
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rows(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 编号是阅读 263 条要求后指定的原始行号。数值只用于关联既有断言身份，不推断自然语言含义。
GROUPS = [
    ([0], [0]), ([1, 31, 206], [5]), ([30, 205], [0, 1, 2]),
    ([5, 122], [0, 9]), ([6, 9, 117, 123, 127, 177], [1]),
    ([7], [2, 3]), ([115, 175], [0]), ([116, 176], [9]),
    ([118, 124, 144, 178], [2]), ([119, 125, 179], [3]),
    ([12], [1, 5]), ([13, 20], [2, 3, 4, 6, 7]),
    ([95, 110, 132, 137, 142, 152, 156, 160, 164, 228], [2, 3, 4, 7]),
    ([22, 181, 199, 217], [2]), ([23, 182, 200, 216, 227], [6]),
    ([93, 129, 134, 140, 150, 154, 158, 162, 202, 213], [0]),
    ([94, 109, 131, 136, 141, 151, 155, 159, 163], [1, 5, 6]),
    ([201], [7]), ([214, 220], [1, 2, 3, 4, 5, 6, 7]),
    ([25], [0, 1, 6]), ([26, 98, 167, 171], [0, 1, 6]),
    ([27, 168], [8]), ([28], [9]), ([97], [0, 1, 6, 8, 9]),
    ([166, 170], [0, 1, 6, 9]), ([172], [8, 10, 11, 12]),
    ([33, 38, 101], [0, 2]), ([34, 39, 102], [1, 3, 6, 7]),
    ([35, 49, 100, 241], [8]), ([40], [5]), ([43], [0]), ([44], [2]),
    ([45], [1]), ([46], [3]), ([47], [6]), ([48], [7]),
    ([240], [4, 5]), ([242], [0, 1, 2, 3, 6, 7]),
    ([51], [1]), ([52], [2]), ([53, 260], [7]), ([54, 261], [8]),
    ([81], [7, 8]), ([259], [1, 2]),
    ([56, 146, 208], [1]), ([57, 147, 209], [2]), ([58, 210], [3]),
    ([148], [3, 5]),
    ([60, 66, 71, 74, 78, 84, 224, 252, 255], [1]),
    ([61, 67, 72, 75, 79, 222], [2]), ([62, 68, 73, 76, 80, 223], [7]),
    ([63, 69], [3, 4]), ([251, 256], [0]), ([253, 257], [2, 7]),
    ([82], [13, 14, 15]), ([83, 247], [16]), ([244], [13]),
    ([245], [14]), ([246], [15]), ([249], [13, 14, 15, 16]),
    ([86, 230, 232, 234, 236, 238], [3]),
    ([104, 184, 190], [0]), ([106, 186, 193], [4]),
    ([185, 192], [2, 3]), ([187, 191], [1, 5]),
]

SUMMARY_ONLY = {
    3, 8, 10, 15, 21, 24, 29, 32, 37, 42, 50, 55, 59, 65, 85, 87, 92,
    96, 99, 103, 107, 111, 112, 113, 120, 126, 128, 133, 139, 143, 145,
    149, 153, 157, 161, 165, 169, 173, 180, 183, 189, 195, 196, 198,
    204, 207, 211, 215, 218, 221, 225, 229, 231, 233, 235, 237, 239,
    243, 248, 250, 254, 258, 262,
}

# 这些条目包含断言以外的流程、计算或对象选择义务；映射部分通过后仍需读取动作和授权资料。
PARTIAL = {
    0, 1, 5, 7, 13, 20, 23, 25, 26, 27, 30, 31, 33, 34, 35, 38, 39,
    49, 58, 63, 69, 81, 94, 97, 98, 100, 101, 102, 122, 129, 131, 134,
    136, 140, 144, 148, 150, 154, 162, 166, 167, 170, 171, 172, 175,
    178, 179, 182, 190, 200, 202, 205, 206, 208, 213, 214, 216, 217,
    224, 227, 228, 230, 232, 234, 236, 240, 241, 242, 249, 252, 253,
    255, 257, 259,
}

PARTIAL -= {5, 7, 13, 20, 23, 27, 34, 35, 39, 49, 58, 63, 69, 81,
            94, 100, 102, 122, 129, 134, 140, 144, 148, 150, 172, 175,
            190, 202, 208, 213, 214, 216, 217, 228, 240, 241, 252, 253, 255, 257}


def payload(value):
    return json.loads(value) if isinstance(value, str) else value


def decoded_messages(record):
    messages = []
    for i, action in enumerate(record["actual_tool_actions"]):
        args = action["arguments"]
        if action["tool"] != "api_fetch" or action["failed"]:
            continue
        if not args.get("url") or urlsplit(args["url"]).path != "/gmail/v1/users/me/messages/send":
            continue
        data = payload(args.get("body"))
        raw = data["raw"]
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        body = message.get_body(preferencelist=("plain", "html"))
        messages.append({"action_index": i, "to": [address.addr_spec for address in message["To"].addresses],
                         "cc": [address.addr_spec for address in message["Cc"].addresses] if message["Cc"] else [],
                         "subject": str(message["Subject"]),
                         "body": body.get_content() if body else ""})
    return messages


def action_review(position, record):
    actions = record["actual_tool_actions"]
    messages = decoded_messages(record)

    def mail(address, required=(), forbidden=(), subject=()):
        return any(address in message["to"] and all(value in message["body"] for value in required)
                   and not any(value in message["body"] for value in forbidden)
                   and all(value in message["subject"] for value in subject) for message in messages)

    names = [("carol.diaz", "Carol Diaz"), ("dave.kim", "Dave Kim"),
             ("frank.torres", "Frank Torres"), ("tom.bradford", "Tom Bradford")]
    checks = None
    fetches = [(i, action, urlsplit(action["arguments"]["url"])) for i, action in enumerate(actions)
               if action["tool"] == "api_fetch" and action["arguments"].get("url")]
    successful = [(i, action, url) for i, action, url in fetches if not action["failed"]]
    if position in {0, 1, 2, 30, 31, 205, 206}:
        reads = [(i, payload(action["raw"])) for i, action, url in successful
                 if action["arguments"].get("method") == "GET"
                 and url.path.startswith("/v4/spreadsheets/ss_inv_registry/values/")]
        values = [value["values"] for _, value in reads if "values" in value]
        if not values:
            return {"status": "not_adjudicated", "missing": "缺少 Registry 单元格的实际读取结果"}
        final = values[-1]
        checks = [final[0] == ["Invoice", "Vendor", "Amount"], len(final) == 4,
                  all(len(row) == 3 for row in final)]
        if position in {0, 30, 205}:
            expected = [["ACM-2026-0088", "Acme", "3,400"], ["TS-22104", "TechServe", "11,200"],
                        ["BI-5501", "Bright Ideas", "2,750"]]
            if position == 0:
                expected = expected[:1]
            checks += [any(all(value in str(cell) for value, cell in zip(row, actual, strict=True))
                           for actual in final[1:]) for row in expected]
        if position in {1, 2, 31}:
            sheet_writes = [(i, action, url) for i, action, url in fetches
                            if url.hostname == "sheets.googleapis.com" and action["arguments"].get("method") != "GET"]
            checks += [len(sheet_writes) == 1,
                       all(url.path.startswith("/v4/spreadsheets/ss_inv_registry/values/") for _, _, url in sheet_writes)]
    elif position in {33, 38, 101, 242}:
        fields = []
        for _, action, url in successful:
            if url.hostname == "api.airtable.com" and action["arguments"].get("method") == "POST":
                data = payload(action["arguments"].get("body"))
                if isinstance(data, dict) and "fields" in data:
                    fields.append(data["fields"])
        checks = [any(value.get("Employee ID") == identity for value in fields)
                  for identity in ["EMP-2010", "EMP-3001", "EMP-2011", "EMP-2003"]]
        if position == 101:
            checks.append(not any(value.get("Employee ID") == "EMP-3003" for value in fields))
        if position == 242:
            checks += [mail(local + "@company.example.com", [path]) for local, path in
                       [("kenji.watanabe", "Foundation"), ("nadia.petrova", "Advanced"),
                        ("marcus.lindgren", "Leadership"), ("tom.bradford", "Foundation")]]
    elif position == 89:
        checks = [mail("eng-lead@company.example.com", ["19,500"], subject=["January"]),
                  mail("mktg-lead@company.example.com", ["19,200"], subject=["January"])]
    elif position == 91:
        customers = {}
        actual_events = []
        for _, action, url in successful:
            if url.hostname != "api.helpcrunch.com":
                continue
            returned = payload(action["raw"])
            if url.path == "/v1/customers" and action["arguments"].get("method") == "GET":
                customers.update({item["id"]: item for item in returned["data"]})
            elif url.path.endswith("/tags") and "id" in returned:
                customers[returned["id"]] = returned
            elif url.path == "/v1/events" and action["arguments"].get("method") == "POST":
                sent = payload(action["arguments"]["body"])
                actual_events.append((sent["customer"], returned["event"]["event_name"]))
        expected_tags = {"eng_hc1": "highly-engaged", "eng_hc2": "highly-engaged",
                         "eng_hc3": "engaged", "eng_hc4": "engaged", "eng_hc5": "passive",
                         "eng_hc6": "passive", "eng_hc7": "disengaged", "eng_hc8": "disengaged"}
        checks = [identity in customers and customers[identity]["tags"] == [tag]
                  for identity, tag in expected_tags.items()]
        checks += [set(actual_events) == {(identity, "engagement-assessed") for identity in
                                        ["eng_hc1", "eng_hc2", "eng_hc3", "eng_hc6"]}, len(actual_events) == 4]
    elif position == 90:
        # Handbook Section 6 与当前 roster 给出三位超过30天、无豁免的升级对象。
        committee = [message for message in messages if "compliance-committee@company.example.com" in message["to"] + message["cc"]]
        checks = [all(not any(name in message["body"] for name in
                             ["Patricia Chen", "Ursula Martin", "Samantha Green", "Roberto Alvarez", "Wendy Torres"])
                      for message in committee)]
    elif position == 88:
        prohibited = ["Software Licenses", "Events", "Training"]
        bodies = [message["body"] for message in messages]
        bodies += [str(payload(action["arguments"].get("body")).get("text", ""))
                   for _, action, url in successful if url.hostname == "slack.com" and url.path == "/api/chat.postMessage"]
        checks = [all(not any(entity in body for entity in prohibited) for body in bodies)]
    elif position in {4, 114, 121, 174}:
        source_reads = [i for i, action, url in successful if action["arguments"].get("method") == "GET"
                        and url.path == "/gmail/v1/users/me/messages/msg_monitor_001"]
        target_writes = [(i, action) for i, action, url in successful if action["arguments"].get("method") in {"POST", "PUT"}
                         and url.hostname != "gmail.googleapis.com"]
        checks = [bool(source_reads), bool(target_writes),
                  bool(source_reads) and bool(target_writes) and source_reads[0] < target_writes[0][0],
                  all(not any(entity in canonical(payload(action["arguments"].get("body"))) for entity in
                              ["Auth Service Down", "Database Cluster Failover", "Cache Layer Degraded", "CDN Edge Node Failure"])
                      for _, action in target_writes),
                  any("API Gateway Timeout" in canonical(payload(action["arguments"].get("body"))) for _, action in target_writes)]
    elif position == 178:
        checks = [any(payload(action["arguments"].get("body")).get("cloudId") == "cloud_ops"
                      and payload(action["arguments"].get("body")).get("spaceId") == "SP_INCIDENTS"
                      for _, action, url in successful if url.path == "/wiki/api/v2/pages"
                      and action["arguments"].get("method") == "POST")]
    elif position == 259:
        checks = [mail("procurement@supplychainco.com", ["PO-5002", "PO-5005"], subject=["mismatch"])]
    elif position in {188, 194}:
        located = [i for i, action, url in successful if url.path.startswith("/v4/spreadsheets/ss_proj_profit/values/")
                   and action["arguments"].get("method") == "GET" and payload(action["raw"]).get("values")]
        failed_sheets = [i for i, action, url in fetches if action["failed"] and url.hostname == "sheets.googleapis.com"]
        checks = [bool(located), len(failed_sheets) <= 2]
    elif position == 11:
        source = [i for i, action, url in successful if action["arguments"].get("method") == "GET"
                  and url.path.startswith("/v4/spreadsheets/ss_prepaids/values/") and payload(action["raw"]).get("values")]
        writes = [i for i, action, _ in successful if action["arguments"].get("method") in {"POST", "PUT", "PATCH"}]
        guesses = [i for i, action, url in fetches if action["failed"] and url.hostname == "sheets.googleapis.com"]
        checks = [bool(source), bool(source) and (not writes or source[0] < min(writes)), len(guesses) <= 2]
    elif position in {154, 162}:
        sent = [message["action_index"] for message in messages if "controller@company.example.com" in message["to"]]
        checks = [bool(sent)]
        if position == 154 and sent:
            boundary = actions[sent[0]].get("boundary_index")
            at = next(item for item in record["boundary_trace"] if item["index"] == boundary)
            checks.append(at["step"] <= 33)
    elif position == 203:
        checks = [any(item.get("id") == "ss_prepaids" for _, action, url in successful
                      if url.path == "/drive/v3/files" and action["arguments"].get("method") == "GET"
                      for item in payload(action["raw"]).get("files", []))]
    elif position == 105:
        checks = [mail("cfo@company.example.com", ["Alpha", "22,200", "Beta", "Gamma", "At Risk", "Total profit: $22,200"])]
    elif position == 224:
        allocated = [item for _, action, url in successful if url.path == "/api.xro/2.0/CreditNotes/xcn_002/Allocations"
                     and action["arguments"].get("method") == "PUT" for item in payload(action["raw"])["Allocations"]]
        checks = [mail("ap@baxter.example.com", ["INV-5004", "2,000", "4,800"]),
                  any(item["Invoice"]["InvoiceID"] == "xi_004" and item["Amount"] == 2000 for item in allocated)]
    elif position in {230, 232, 234, 236}:
        prompts = []
        returned_text = []
        for _, action, url in successful:
            if url.hostname != "api.openai.com" or url.path not in {"/v1/chat/completions", "/chat/completions", "/v1/responses"}:
                continue
            sent = payload(action["arguments"].get("body"))
            if "messages" in sent:
                prompts += [item["content"] for item in sent["messages"] if item["role"] == "user"]
                returned_text += [item["message"]["content"] for item in payload(action["raw"])["choices"]]
            else:
                prompts.append(sent["input"])
                returned_text.append(canonical(payload(action["raw"])["output"]))
        checks = [bool(prompts), all(all(value in prompt[:100] for value in ["Marcus Chen", "VP of Engineering", "TechVentures"])
                                   for prompt in prompts), any("VP" in text for text in returned_text)]
    elif position == 249:
        posts = [payload(action["arguments"].get("body")) for _, action, url in successful
                 if url.hostname == "slack.com" and url.path == "/api/chat.postMessage"]
        checks = [mail("mkt.head@ourcompany.example.com", ["12,000"]),
                  mail("sales.head@ourcompany.example.com", ["8,000"]),
                  mail("eng.head@ourcompany.example.com", ["3,500"]),
                  any(all(value in post.get("text", "") for value in ["12,000", "8,000", "3,500"]) for post in posts)]
    elif position in {16, 130, 135}:
        drive_calls = [(i, action) for i, action, url in fetches if url.path == "/drive/v3/files"]
        limit = 2 if position == 16 else 1
        checks = [len(drive_calls) <= limit]
    elif position in {36, 41}:
        creates = [i for i, action, url in successful if url.hostname == "api.airtable.com" and action["arguments"].get("method") == "POST"]
        checks = [bool(creates), bool(messages)]
    elif position in {14, 19}:
        checks = [mail("controller@company.example.com", ["Insurance", "4,000", "Cloud Hosting", "300"])]
    elif position == 212:
        checks = [all(isinstance(payload(action["arguments"].get(field)), dict)
                      for _, action, _ in fetches for field in ["params", "body"])]
    elif position in {25, 97, 166, 170}:
        people = names[:3] if position == 25 else names
        checks = [mail(local + "@company.example.com", [name]) for local, name in people]
        if position == 97:
            checks.append(mail("hr-director@company.example.com", ["Tom Bradford"]))
    elif position in {26, 98, 167, 171}:
        checks = [mail("carol.diaz@company.example.com", ["recording"]),
                  mail("dave.kim@company.example.com", ["allhands-mar2026"]),
                  mail("frank.torres@company.example.com", ["recording"])]
    elif position in {17, 108, 182, 200, 227}:
        required = ["Total amortization: $"] if position in {17, 108} else ["Total amortization: $4,300"]
        if position == 227:
            required += ["Insurance", "4,000", "Cloud Hosting", "300"]
        checks = [mail("controller@company.example.com", required)]
    elif position in {131, 136}:
        checks = [mail("controller@company.example.com", ["Insurance", "4,000", "Cloud Hosting", "300", "$", "4,300"])]
    elif position == 179:
        checks = [mail("incident-stakeholders@company.example.com", ["API Gateway Timeout", "Alex Rivera"], subject=["API Gateway Timeout"])]
    elif position in {64, 70, 77}:
        writes = [(i, action) for i, action in enumerate(actions) if action["tool"] == "api_fetch"
                  and action["arguments"].get("method") in {"POST", "PUT", "PATCH"}
                  and urlsplit(action["arguments"]["url"]).hostname == "api.xero.com"
                  and urlsplit(action["arguments"]["url"]).path.startswith("/api.xro/2.0/Contacts")]
        checks = [not writes]
    elif position == 18:
        writes = [action for action in actions if action["tool"] == "api_fetch" and not action["failed"]
                  and action["arguments"].get("method") == "PUT"
                  and urlsplit(action["arguments"]["url"]).path.startswith("/v4/spreadsheets/ss_prepaids/values/")]
        checks = [bool(writes)]
    elif position == 226:
        events = [event for event in record["injections"] if event.get("unit") == "ace_api_call_hygiene"]
        if not events:
            return {"status": "not_observed", "reason": "候选执行未加载 ace_api_call_hygiene，未出现该组件的协议表达机会",
                    "execution_completed": record["execution_error"] is None and bool(actions)}
        checks = [bool(events), all(isinstance(event.get("output"), dict) for event in events),
                  record["execution_error"] is None, bool(actions)]
    if checks is None:
        return None
    return {"status": "pass" if all(checks) else "fail", "checks": checks,
            "review_method": "manually_assigned_requirement_predicate_on_recorded_actions",
            "action_count": len(actions), "sent_message_action_positions": [message["action_index"] for message in messages],
            "evidence_pointers": ["/actual_tool_actions/" + str(i) for i, _, _ in successful]}


UNIT_REVIEWS = {
    "research/global/edit/31": ("split_and_expand", "来源定位规则延续到 exp_source_discovery_budget，并新增 exp_finish_within_budget 分担交付与复读；一项职责扩展为两项。"),
    "research/global/edit/50": ("merge", "exp_source_discovery_budget 与 exp_finish_within_budget 合并为 exp_deliver_first_batch；保留发现额度、按名称写入和批量交付。"),
    "research/global/edit/130": ("temporary_probe_replacement", "十五项操作经验整体撤下，换成只读取 Xero 并报告发现结果的 probe_credit_note_discovery；原功能没有转移到探针。"),
    "research/global/edit/134": ("restore_after_probe", "删除发现探针，恢复原十五项经验身份与各自职责；信用票据发现细则写入 exp_credit_note_allocation。"),
    "research/gnosis/edit/94": ("rename_and_revise", "ace_op_budget_timebox 的发现额度与停止搜索规则迁入 ace_op_deliver_first，同时强化提前交付并加入 schedule 写回。"),
    "research/gnosis/edit/110": ("different_function_replacement", "全局预算与交付规则被移除，新增事故专用的四项交付流程；两者适用范围和职责不同。"),
}


def reference(file, pointer):
    return {"file": str(file).replace("\\", "/"), "pointer": pointer}


def record_path(root, value):
    from engine.negative_result_history import original_path
    return original_path(root, value)


def assertion_identity(item):
    return item["type"], canonical(item["params"])


def evaluate_assertions(expected, actual):
    index = defaultdict(list)
    for position, item in enumerate(actual):
        index[assertion_identity(item)].append((position, item))
    evidence = []
    for item in expected:
        matches = index.get(assertion_identity(item), [])
        if not matches:
            return {"status": "missing_assertion_identity", "evidence": evidence,
                    "missing": item}
        flags = {bool(value["passed"]) for _, value in matches}
        if len(flags) != 1:
            raise ValueError("同一要求断言身份的保存结果冲突")
        evidence.append({"type": item["type"], "params": item["params"],
                         "positions": [position for position, _ in matches], "passed": flags.pop()})
    return {"status": "pass" if all(item["passed"] for item in evidence) else "fail",
            "evidence": evidence}


def unit_relationships(root, output):
    changes = rows(output / "history/unit_version_events.jsonl")
    grouped = defaultdict(list)
    for row in changes:
        grouped[row["event_id"]].append(row)
    result = []
    for event, values in grouped.items():
        parent = read(root / values[0]["parent_reference"]["file"])["units"]
        child = read(root / values[0]["child_reference"]["file"])["units"]
        old = {unit["id"]: unit for unit in parent}
        new = {unit["id"]: unit for unit in child}
        removed = sorted(set(old) - set(new))
        added = sorted(set(new) - set(old))
        exact = []
        for before in removed:
            for after in added:
                left = {key: value for key, value in old[before].items() if key != "id"}
                right = {key: value for key, value in new[after].items() if key != "id"}
                if canonical(left) == canonical(right):
                    exact.append({"parent_unit": before, "child_unit": after,
                                  "relationship": "identical_definition_with_changed_id"})
        semantic = UNIT_REVIEWS.get(event)
        if removed and added and semantic is None:
            raise ValueError("存在尚未阅读的删除新增关系: " + event)
        result.append({"event_id": event, "removed": removed, "added": added,
                       "exact_definition_renames": exact,
                       "semantic_split_review": semantic[0] if semantic else "no_removed_added_pair",
                       "semantic_reading": semantic[1] if semantic else None,
                       "parent_reference": values[0]["parent_reference"],
                       "child_reference": values[0]["child_reference"]})
    return result


def guard_input_review(root, output):
    from engine.negative_result_components import Versions

    versions = Versions()
    events = []
    with (output / "history/e_component_events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            if item["unit"] == "ace_op_vp_guard":
                events.append(item)
    for path in sorted((root / "experiments/feedback_chain/paid/executions").glob("*/*.json")):
        record = read(path)
        boundaries = {event["index"]: event for event in record["boundary_trace"]}
        for i, event in enumerate(record["injections"]):
            if event.get("unit") == "ace_op_vp_guard":
                events.append(dict(event, execution_id=record["execution_id"], task=record["task"],
                                   experiment_id="feedback_chain_paid", version=record["version"],
                                   boundary_payload=boundaries[event["boundary_index"]],
                                   reference=reference(path, "/injections/" + str(i))))
    reviewed = []
    sources = {}
    for event in events:
        version = event["version"]
        if version not in sources:
            if version not in versions.index:
                manifests = list((root / "experiments/feedback_chain/paid/workspace/versions").glob(version + "/manifest.json"))
                if not manifests:
                    manifests = list((root / "experiments/feedback_chain/source_data/versions").glob("*/" + version + "/manifest.json"))
                if not manifests:
                    raise ValueError("guard 所用版本没有 manifest: " + version)
                units = read(manifests[0])["units"]
                manifest = manifests[0]
            else:
                units = versions.units(version)
                manifest = versions.index[version]
            unit = next(unit for unit in units if unit["id"] == "ace_op_vp_guard")
            sources[version] = {"source_sha256": hashlib.sha256(unit["source"].encode()).hexdigest(),
                                "source": unit["source"], "hooks": unit["hooks"],
                                "reference": reference(manifest, "/units/" + str(units.index(unit)))}
        boundary = event["boundary_payload"]
        inputs = event["input"] or {}
        args = inputs.get("arguments", inputs.get("args"))
        if args is None and any(inputs.get(key) for key in ["url", "method", "body"]):
            args = inputs
        if isinstance(args, str):
            args = json.loads(args)
        url = str(args.get("url") or "") if isinstance(args, dict) else ""
        target = "chat/completions" in url.lower() or "responses" in url.lower()
        hook = boundary["hook"]
        body_decode_error = None
        try:
            body = payload(args.get("body")) if target and isinstance(args, dict) else None
        except json.JSONDecodeError as error:
            body = None
            body_decode_error = str(error)
        user_text = None
        texts = []
        if isinstance(body, dict):
            if isinstance(body.get("messages"), list):
                texts = [message.get("content", "") for message in body["messages"]]
                user = next((message for message in body["messages"] if message.get("role") == "user"),
                            body["messages"][0] if body["messages"] else {})
                user_text = user.get("content")
            elif isinstance(body.get("input"), str):
                user_text = body["input"]
                texts = [user_text]
        if hook != "before_call":
            gate = "hook_not_before_call"
        elif args is None:
            gate = "input_arguments_absent"
        elif not target:
            gate = "url_not_chat_or_responses"
        elif body_decode_error:
            gate = "target_body_invalid_json"
        elif not isinstance(body, dict):
            gate = "body_not_object"
        elif isinstance(user_text, str) and "VP" in user_text[:200]:
            gate = "target_prompt_already_contains_VP_in_first_200"
        else:
            gate = "target_requires_source_branch_reading"
        reviewed.append({"execution_id": event["execution_id"], "task": event["task"],
                         "experiment_id": event["experiment_id"], "version": version,
                         "hook": hook, "boundary_index": event["boundary_index"], "url": url,
                         "input_keys": sorted(inputs), "argument_keys": sorted(args) if isinstance(args, dict) else None,
                         "target_route": target, "gate": gate, "effects": event["effects"],
                         "body_decode_error": body_decode_error,
                         "output_keys": sorted(event["output"]),
                         "user_first_200": user_text[:200] if isinstance(user_text, str) else user_text,
                         "source_sha256": sources[version]["source_sha256"], "reference": event["reference"]})
    return {"invocations": len(reviewed), "gates": dict(Counter(item["gate"] for item in reviewed)),
            "by_experiment": {name: dict(Counter(item["gate"] for item in reviewed if item["experiment_id"] == name))
                              for name in sorted({item["experiment_id"] for item in reviewed})},
            "source_versions": sources, "events": reviewed,
            "host_protocol_reference": reference("engine/automation_adapter.py", "extension: inputs.event + inputs.input=value; injections.input=value"),
            "reading": "按每次真实 input.arguments 与边界 hook 检查组件源码的协议入口；目标路由之外的调用在 URL 条件直接返回 state。"}


def run(root, output):
    root, output = Path(root), Path(output)
    destination = output / "requirements"
    destination.mkdir(parents=True, exist_ok=True)
    events = rows(output / "history/requirement_events.jsonl")
    registry = {row["execution_id"]: row for row in rows(output / "history/executions.jsonl")}
    defs_path = Path("experiments/feedback_chain/out/ab_task_defs.json")
    definitions = read(root / defs_path)
    edges = rows(output / "history/version_events.jsonl")
    children = defaultdict(set)
    for edge in edges:
        if edge["experiment_id"] == "research" and edge.get("parent_version") and edge.get("candidate_version"):
            children[edge["parent_version"]].add(edge["candidate_version"])

    def descendants(version):
        found, pending = {version}, [version]
        while pending:
            for child in children[pending.pop()] - found:
                found.add(child)
                pending.append(child)
        return found
    mappings = {}
    for identities, positions in GROUPS:
        for identity in identities:
            if identity in mappings:
                raise ValueError("重复人工要求编号: " + str(identity))
            mappings[identity] = positions
    if len(events) != 263 or set(mappings) & SUMMARY_ONLY:
        raise ValueError("要求历史数量或人工分类不一致")
    records = {}

    def get_record(identity):
        if identity not in records:
            entry = registry[identity]
            records[identity] = read(record_path(root, entry["evidence_path"]))
            if records[identity]["execution_id"] != identity:
                raise ValueError("执行记录身份不一致")
        return records[identity]

    reviewed = []
    observations = []
    retention = []
    for position, event in enumerate(events):
        indices = mappings.get(position, [])
        expected = [{"type": value["type"], "params": {key: item for key, item in value.items() if key != "type"}}
                    for value in [definitions[event["task"]]["assertions"][i] for i in indices]]
        kind = "summary_only_no_requirement_identity" if position in SUMMARY_ONLY else (
            "partial_assertion_mapping" if indices and position in PARTIAL else
            "exact_assertion_mapping" if indices else "process_or_composite_requirement_requires_action_review")
        entry = dict(event, review_index=position, identity_review=kind,
                     assertion_positions=indices, assertion_identities=expected,
                     definition_reference=reference(defs_path, "/" + event["task"]),
                     requirement_completion_unit="individual_requirement; task_success_kept_separately")
        observed = []
        for identity in event["same_candidate_task_execution_ids"]:
            record = get_record(identity)
            value = evaluate_assertions(expected, record["assertions"]) if expected else {
                "status": "not_adjudicated", "evidence": []}
            status = value["status"]
            # 复合断言可能多含姓名或链接条件；未逐项分解的通过与失败均保留在断言层。
            completion = status if kind == "exact_assertion_mapping" else "not_adjudicated"
            action_check = action_review(position, record)
            if action_check is not None:
                completion = action_check["status"]
            observation = {"requirement_event_id": event["requirement_event_id"], "review_index": position,
                           "execution_id": identity, "version": record["version"],
                           "task_success": bool(record["success"]), "mapped_assertion_result": status,
                           "action_review": action_check,
                           "requirement_completion": completion, "evidence": value["evidence"],
                           "reference": reference(registry[identity]["evidence_path"], "/assertions")}
            observations.append(observation)
            observed.append(observation)
        entry["same_candidate_observations"] = observed
        entry["followup_status"] = "observed" if observed else "not_observed"
        entry["missing_evidence"] = (
            "编辑输入仅提供任务级摘要，没有指出具体业务要求；需要把原执行的任务资料与失败动作重新对应。"
            if position in SUMMARY_ONLY else
            "已映射断言只覆盖该复合要求的一部分；需要原执行 actual_tool_actions 与任务授权资料的逐项判读。"
            if kind == "partial_assertion_mapping" else
            "需要原执行 actual_tool_actions、发现过程与任务授权资料；现有断言没有对应完整身份。"
            if not expected else None)
        reviewed.append(entry)
        if position in SUMMARY_ONLY or event["task"] not in definitions:
            continue
        lineage_versions = descendants(event["candidate_version"])
        applicable = [row for row in registry.values() if row["experiment_id"] == "research"
                      and row["arm"] == event["arm"] and row["task"] == event["task"]
                      and row["version"] in lineage_versions
                      and not row["archived_before_cutoff"]]
        applicable.sort(key=lambda row: (row["recorded_at"], row["execution_id"]))
        values = []
        for execution in applicable:
            record = get_record(execution["execution_id"])
            check = evaluate_assertions(expected, record["assertions"]) if expected else {"status": "not_adjudicated"}
            action_check = action_review(position, record)
            completion = action_check["status"] if action_check is not None else (
                check["status"] if kind == "exact_assertion_mapping" else "not_adjudicated")
            values.append({"execution_id": record["execution_id"], "version": record["version"],
                           "recorded_at": record["recorded_at"], "mapped_assertion_result": check["status"],
                           "requirement_completion": completion, "action_review": action_check,
                           "task_success": bool(record["success"]),
                           "reference": reference(execution["evidence_path"], "/assertions")})
        first = next((i for i, value in enumerate(values) if value["requirement_completion"] == "pass"), None)
        after = values[first + 1:] if first is not None else []
        retention.append({"requirement_event_id": event["requirement_event_id"], "review_index": position,
                          "identity_review": kind, "observations": values,
                          "first_verified_assertion_pass": None if first is None else values[first],
                          "later_observations": len(after),
                          "later_assertion_results": dict(Counter(value["mapped_assertion_result"] for value in after)),
                          "later_requirement_results": dict(Counter(value["requirement_completion"] for value in after)),
                          "retention_status": "no_verified_start" if first is None else "not_observed" if not after else
                          "retained" if all(value["requirement_completion"] == "pass" for value in after) else
                          "later_failure_observed" if any(value["requirement_completion"] == "fail" for value in after) else "later_not_adjudicated",
                          "scope": "candidate_and_descendant_versions_same_arm_same_task_requirement",
                          "completion_scope": "mapped_assertions_only" if kind != "exact_assertion_mapping" else "individual_requirement"})
    relationships = unit_relationships(root, output)
    components = read(output / "history/component_business.json")
    effect_ids = {entry["execution_id"] for case in components["effect_cases"]["cases"] for entry in case["executions"]}
    business_tasks = read(output / "business/task_requirements.json")["tasks"]
    effect_paths = [{"execution_id": identity, "task": registry[identity]["task"],
                     "path": str(record_path(root, registry[identity]["evidence_path"])),
                     "business_task_available": registry[identity]["task"] in business_tasks}
                    for identity in effect_ids]
    effect_paths.sort(key=lambda item: (not item["business_task_available"], item["task"], item["execution_id"]))
    save(destination / "historical_effect_paths.json", effect_paths)
    guard = guard_input_review(root, output)
    save(destination / "vp_guard_inputs.json", guard)
    contexts = []
    for identity, record in records.items():
        contexts.append({"execution_id": identity, "task": record["task"], "messages": decoded_messages(record),
                         "termination_status": record.get("termination_status"),
                         "execution_error": record.get("execution_error"),
                         "actions": [{"index": i, "tool": action["tool"], "arguments": action["arguments"],
                                      "failed": action["failed"],
                                      "raw": action["raw"] if action["tool"] == "api_fetch" else None}
                                     for i, action in enumerate(record["actual_tool_actions"])],
                         "reference": reference(registry[identity]["evidence_path"], "/actual_tool_actions")})
    for name, values in (("events.json", reviewed), ("observations.json", observations),
                         ("retention.json", retention), ("unit_relationships.json", relationships)):
        save(destination / name, values)
    save(destination / "action_context.json", contexts)
    summary = {"events": len(reviewed), "identity_review": dict(Counter(row["identity_review"] for row in reviewed)),
               "same_candidate_requirement_execution_pairs": len(observations),
               "requirement_completion": dict(Counter(row["requirement_completion"] for row in observations)),
               "mapped_assertion_results": dict(Counter(row["mapped_assertion_result"] for row in observations)),
               "requirement_pass_in_failed_task": sum(row["requirement_completion"] == "pass" and not row["task_success"] for row in observations),
               "no_same_candidate_followup": sum(row["followup_status"] == "not_observed" for row in reviewed),
               "retention_sequences": len(retention),
               "retention_status": dict(Counter(row["retention_status"] for row in retention)),
               "unit_change_events": len(relationships),
               "exact_definition_renames": sum(len(row["exact_definition_renames"]) for row in relationships),
               "removed_added_events_requiring_semantic_review": sum(row["semantic_split_review"] == "requires_reading" for row in relationships),
               "raw_execution_records_read": len(records),
               "guard_gates": guard["gates"],
               "assumption": "原始保存断言 type+params 身份及 passed 值忠实对应该次执行；撤掉后要求结果需要重放世界状态。",
               "coverage": "263条要求均已阅读并记录身份、适用机会与证据；63条纯任务摘要保留为缺少具体要求身份，32条过程或复合要求保留原始动作上下文。"}
    save(destination / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("experiments/negative_result_offline"))
    arguments = parser.parse_args()
    run(arguments.root, arguments.output)
