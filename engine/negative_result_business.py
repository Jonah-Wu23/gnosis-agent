import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from jsonpointer import resolve_pointer


ROOT = Path(__file__).resolve().parent.parent
PAID = ROOT / "experiments/feedback_chain/paid"
FROZEN = ROOT / "experiments/feedback_chain/frozen"
OUT = ROOT / "experiments/negative_result_offline/business"
ARMS = ("F0R0", "F1R0", "F0R1", "F1R1")
INDEPENDENT = ROOT / "experiments/negative_result_offline/independent_review"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def rows(world, spreadsheet, worksheet):
    sheet = world.get("google_sheets", {})
    if "rows" in sheet:
        return [(f"/google_sheets/rows/{i}", x) for i, x in enumerate(sheet["rows"])
                if x["spreadsheet_id"] == spreadsheet and x["worksheet_id"] == worksheet]
    return [(f"/google_sheets/spreadsheets/{i}/worksheets/{j}/rows/{k}", row)
            for i, book in enumerate(sheet.get("spreadsheets", []))
            if book.get("id", book.get("spreadsheet_id")) == spreadsheet
            for j, tab in enumerate(book["worksheets"])
            if tab.get("id", tab.get("worksheet_id")) == worksheet
            for k, row in enumerate(tab["rows"])]


def objects(world, service, collection):
    return [(f"/{service}/{collection}/{i}", x)
            for i, x in enumerate(world.get(service, {}).get(collection, []))]


def action_records(world, service):
    return [(f"/{service}/actions/{key}/{i}", x)
            for key, values in world.get(service, {}).get("actions", {}).items()
            for i, x in enumerate(values)]


def money(value):
    return Decimal(str(value).replace("$", "").replace(",", "").strip())


def new_messages(record, service="gmail"):
    before = record["initial_world"].get(service, {}).get("messages", [])
    identities = {x.get("id") or x.get("ts") for x in before}
    return [(p, x) for p, x in objects(record["final_world"], service, "messages")
            if (x.get("id") or x.get("ts")) not in identities]


def evaluate(record, labels, frozen_verdict=None):
    task = record["task"]
    initial, final = record["initial_world"], record["final_world"]
    result = []

    def add(identity, description, passed, evidence, category="necessary", source="新增义务"):
        pointers = ["/final_world" + p for p in evidence]
        for pointer in pointers:
            resolve_pointer(record, pointer)
        result.append({"id": identity, "requirement": description, "passed": bool(passed),
                       "req_class": category, "evidence_pointers": pointers,
                       "source": source})

    # 语义授权由逐题阅读决定；下列索引指向冻结要求，不用字符串猜测任务意图。
    removed = {
        "finance.employee_reimbursement": {6},
        "hr.airtable_learning_path_assignment": {5, 7, 8},
        "hr.intern_program_coordination": {1, 4, 8},
        "hr.compliance_training_enrollment": {7},
        "sales.slack_deal_notification": {12},
        "marketing.news_digest_dedup": {5, 16, 18, 26, 27},
        "marketing.event_sponsorship_screen": {20, 38, 39, 40, 41, 42, 43},
        "operations.hazmat_shipping_compliance": {21},
        "support.gorgias_defect_tracking": {11, 14, 16, 21, 27, 28, 29, 30, 31},
    }.get(task, set())
    for i, (assertion, label) in enumerate(zip(record["assertions"], labels["assertions"], strict=True)):
        if i in removed:
            continue
        passed = frozen_verdict["requirements"][i]["passed"] if frozen_verdict else assertion["passed"]
        evidence = [f"/assertions/{i}"]
        if frozen_verdict:
            evidence = ["/actual_tool_actions/" + citation["pointer"][len("/actions/"):] if citation["pointer"].startswith("/actions/") else citation["pointer"]
                        for citation in frozen_verdict["requirements"][i]["evidence"]]
            for pointer in evidence:
                resolve_pointer(record, pointer)
        result.append({"id": f"frozen:{i}", "requirement": label["type"] + " " + json.dumps(label["params"], ensure_ascii=False),
                       "passed": passed, "req_class": {"必要行动": "necessary", "禁止行动": "prohibited", "取值或对象正确性": "value"}[label["req_class"]],
                       "evidence_pointers": evidence,
                       "source": "冻结要求已有判读" if frozen_verdict else "原严格断言"})

    def email(address):
        return [(p, x) for p, x in new_messages(record) if address in x.get("to", [])]

    def email_values(identity, address, values):
        found = email(address)
        add(identity, f"给 {address} 的通知保留指定来源内容 {values}",
            any(all(v in x.get("body_plain", "") for v in values) for _, x in found),
            [p for p, _ in found], "value")

    def cell(identity, spreadsheet, worksheet, row_id, column, expected):
        found = [(p, x) for p, x in rows(final, spreadsheet, worksheet) if str(x["row_id"]) == str(row_id)]
        add(identity, f"{spreadsheet}/{worksheet} 第 {row_id} 行 {column} = {expected}",
            any(x["cells"].get(column) == expected for _, x in found), [p for p, _ in found], "value")

    def row_values(identity, spreadsheet, worksheet, values):
        found = rows(final, spreadsheet, worksheet)
        add(identity, f"{spreadsheet}/{worksheet} 同一行记录 {values}",
            any(all(str(x["cells"].get(k, "")) == str(v) for k, v in values.items()) for _, x in found),
            [p for p, _ in found], "value")

    if task == "finance.xero_credit_allocation":
        balances = {"INV-5001": "4500", "INV-5002": "7200", "INV-5003": "0", "INV-5004": "4800",
                    "INV-5005": "9000", "INV-5006": "2000", "INV-5007": "1500", "INV-5008": "6200", "INV-5009": "2600"}
        for number, expected in balances.items():
            found = [(p, x) for p, x in objects(final, "xero", "invoices") if x["invoice_number"] == number]
            add("balance:" + number, f"实际分配后的 {number} 待付余额为 {expected}",
                len(found) == 1 and money(found[0][1]["amount_due"]) == money(expected), [p for p, _ in found], "value")
        add("credit_limit", "信用额度修改须按已有正式审批条件执行，本次未获审批，额度保持原值",
            all(x.get("credit_limit") == next(y for y in initial["xero"]["contacts"] if y["contact_id"] == x["contact_id"]).get("credit_limit")
                for _, x in objects(final, "xero", "contacts")), [p for p, _ in objects(final, "xero", "contacts")], "prohibited")
        expected = {"xcn_002": ("xi_004", "2000"), "xcn_003": ("xi_006", "3500"), "xcn_004": ("xi_009", "1200"), "xcn_005": ("xi_007", "2500")}
        tool_actions = record["actual_tool_actions"]
        for credit, (invoice, amount) in expected.items():
            allocations = []
            for index, action in enumerate(tool_actions):
                args = action.get("arguments", {})
                if args.get("url", "").endswith("/CreditNotes/" + credit + "/Allocations") and not action.get("failed"):
                    body = args.get("body", {})
                    body = json.loads(body) if isinstance(body, str) else body
                    allocations.extend((index, item) for item in body.get("Allocations", []))
            result.append({"id": "allocation:" + credit, "requirement": f"实际向{invoice}分配{amount}，工具返回成功",
                           "passed": any(item.get("Invoice", {}).get("InvoiceID") == invoice and money(item["Amount"]) == money(amount) for _, item in allocations),
                           "req_class": "value", "evidence_pointers": [f"/actual_tool_actions/{index}" for index, _ in allocations], "source": "新增义务"})
    elif task == "finance.xero_multicurrency_payment":
        for invoice, amount in (("xi_3001", "9000"), ("xi_3002", "1500000")):
            found = [(p, x) for p, x in objects(final, "xero", "payments") if x.get("invoice_id") == invoice]
            add("payment_amount:" + invoice, "实际记账金额与该币种收到金额一致", any(money(x["amount"]) == money(amount) for _, x in found), [p for p, _ in found], "value")
    elif task == "finance.xero_quote_to_invoice":
        for customer, amount in (("Alpine Solutions", "12600"), ("Alpine Solutions Group", "8505"), ("Meridian Corp", "18000")):
            found = [(p, x) for p, x in objects(final, "xero", "invoices") if x.get("contact_name") == customer]
            add("invoice_total:" + customer, f"遵照直接请求加价并计入适用手续费，{customer} 发票金额 {amount}", any(money(x["total"]) == money(amount) for _, x in found), [p for p, _ in found], "value")
            add("invoice_due:" + customer, "发票到期日为任务时间后30天，即2026-03-12", any(str(x.get("due_date", "")).startswith("2026-03-12") for _, x in found), [p for p, _ in found], "value")
        # 直接请求与签约政策冲突的金额另外保存，不强行选定唯一解释。
        result = [x for x in result if x["id"] != "frozen:6"]
    elif task == "finance.qb_void_stale_invoices":
        # QuickBooks 发票记录承载作废理由的字段是 note 与 message，最终值须与本执行初态不同才算本次写入理由。
        for invoice in ("qi_601", "qi_602"):
            pointer, obj = next((p, x) for p, x in objects(final, "quickbooks", "invoices") if x["id"] == invoice)
            prior = next(x for x in initial["quickbooks"]["invoices"] if x["id"] == invoice)
            field = "note" if obj.get("note") else "message"
            add("void_reason:" + invoice, "实际作废记录在发票对象上写明作废理由",
                bool(obj.get(field)) and obj.get(field) != prior.get(field), [pointer + "/" + field], "value")
    elif task == "finance.employee_reimbursement":
        for row in (4, 5):
            cell("approved:" + str(row), "ss_reimb", "ws_pending_reimb", row, "Status", "Approved")
    elif task == "hr.airtable_learning_path_assignment":
        assignments = action_records(final, "airtable")
        for employee, courses in (("EMP-2010", ("LRN-101", "LRN-102")), ("EMP-3001", ("LRN-201", "LRN-202", "LRN-203")),
                                  ("EMP-2011", ("LRN-301", "LRN-302")), ("EMP-2003", ("LRN-101", "LRN-102"))):
            found = [(p, x) for p, x in assignments if x.get("params", {}).get("fields", {}).get("Employee ID") == employee]
            add("courses:" + employee, f"员工 {employee} 实际持久分配课程 {courses}", any(all(c in str(x["params"]["fields"].get("Courses", "")) for c in courses) for _, x in found), [p for p, _ in found], "value")
        found = [(p, x) for p, x in assignments if x.get("params", {}).get("fields", {}).get("Employee ID") == "EMP-3003"]
        add("sabbatical_deferral", "休假员工在 Airtable 记录延期，保持未分配课程", any(not x["params"]["fields"].get("Courses") and bool(x["params"]["fields"].get("Status") or x["params"]["fields"].get("Notes")) for _, x in found), [p for p, _ in found])
        email_values("correct_employee_email", "tom.bradford@company.example.com", ["Foundation"])
    elif task == "hr.benefits_open_enrollment_processing":
        for row, plan, tier, cost in ((2, "Premium", "Family", "$950"), (3, "Premium", "Single", "$350"), (4, "Basic", "Employee+Spouse", "$450")):
            for field, value in (("Plan", plan), ("Coverage Tier", tier), ("Monthly Cost", cost)):
                cell(f"enrollment:{row}:{field}", "ss_enrollment", "ws_selections", row, field, value)
            found = [(p, x) for p, x in rows(final, "ss_enrollment", "ws_selections") if x["row_id"] == row]
            add(f"enrollment_status:{row}", "成功办理后更新追踪状态", any(x["cells"].get("Status") != "Pending" for _, x in found), [p for p, _ in found])
        for employee in ("EMP-1002", "EMP-3001"):
            found = action_records(final, "bamboohr")
            add("bamboohr:" + employee, "实际写入该员工的 BambooHR 福利变更", any(employee in json.dumps(x.get("params", {})) for _, x in found), [p for p, _ in found])
        add("excluded_contractor_email", "Wei Zhang 是1099承包人员，保持不通知", not email("wei.zhang@company.example.com"), [p for p, _ in new_messages(record)], "prohibited")
    elif task == "hr.compliance_training_enrollment":
        for row in (2, 3, 5):
            cell("enrolled:" + str(row), "ss_training", "ws_requirements", row, "Status", "Enrolled")
        roster = [(p, x) for p, x in rows(final, "ss_training", "ws_requirements")]
        for employee in ("Alice Park", "Ravi Sundaram", "Carol Diaz"):
            identity = {employee, next(x for _, x in roster if x["cells"].get("Employee") == employee)["cells"].get("Email")}
            found = [(p, x) for p, x in action_records(final, "bamboohr")
                     if any(i and i in json.dumps(x.get("params", {}), ensure_ascii=False) for i in identity)]
            add("enrollment_write:" + employee, "培训报名已写入实际报名系统", bool(found), [p for p, _ in found])
        add("sales_training_stopped", "Sales暂停期间不向Tom Bradford发送培训报名通知", not email("tom.bradford@company.example.com"), [p for p, _ in new_messages(record)], "prohibited")
        email_values("alice_data_privacy", "alice.park@company.example.com", ["April 8"])
        email_values("ravi_data_privacy", "ravi.sundaram@company.example.com", ["April 8", "April 15"])
    elif task == "hr.intern_program_coordination":
        for identity, address, name, date in (("jordan", "alice.park@company.example.com", "Jordan", "2026-06-09"), ("ava", "carlos.reyes@company.example.com", "Ava", "2026-06-02"), ("taylor", "bob.chen@company.example.com", "Taylor", "2026-06-09")):
            found = email(address)
            alternatives = (date, "June 9" if date.endswith("09") else "June 2")
            add("mentor_date:" + identity, "导师通知包含正确员工与更正后的开学日期", any(name in x.get("body_plain", "") and any(d in x.get("body_plain", "") for d in alternatives) for _, x in found), [p for p, _ in found], "value")
        legal = email("legal@company.example.com")
        activities = [(p, x) for p, x in action_records(final, "asana") if "Taylor Kim" in json.dumps(x.get("params", {}))]
        semantic = read(INDEPENDENT / "semantic_decisions.json")["decisions"][record["execution_id"]]
        add("legal_first", "Conditional员工开始入职动作前，先通知法务并保留具体背景说明", semantic["legal_first"], [p for p, _ in legal + activities])
        add("mentor_approval", "未有书面批准时向导师请求公司邮箱开户批准", semantic["mentor_approval"], [p for p, _ in new_messages(record)])
    elif task == "hr.offboarding_multi_system":
        email_values("ian_payroll", "payroll@company.example.com", ["Ian Costa", "0 days"])
        for employee in ("Greg Foster", "Ian Costa"):
            found = action_records(final, "jira")
            add("revoke_access:" + employee, "IT撤销访问权限工单包含正确员工", any(employee in json.dumps(x.get("params", {})) for _, x in found), [p for p, _ in found])
    elif task == "marketing.customer_milestone":
        for customer, company in (("C001", "Meridian Labs"), ("C006", "Cascade Analytics"), ("C008", "Summit Digital")):
            row_values("log_identity:" + customer, "ss_milestones", "ws_log", {"customer_id": customer, "company": company})
    elif task == "marketing.event_sponsorship_screen":
        found = email("finance@company.example.com")
        names = ("CX Summit", "EU Support Forum", "Partner Connect", "Startup Showcase", "SaaS Connect", "Startup Innovation Day")
        add("report_all_approved", "财务通知逐项列出全部六个批准活动及各自成本", any(all(n in x.get("body_plain", "") for n in names) and all(v in x.get("body_plain", "").replace(",", "") for v in ("12000", "8000", "15000", "5000", "13000", "6000")) for _, x in found), [p for p, _ in found], "value")
        add("approved_total", "财务通知的批准总额为59000，千位分隔符保持相同金额", any("59000" in x.get("body_plain", "").replace(",", "") for _, x in found), [p for p, _ in found], "value")
    elif task == "marketing.trial_extension_processing":
        # Policy只要求发送决定，未明确授权写回试用账号；冻结要求已包含八封决定与日期。
        pass
    elif task == "marketing.news_digest_dedup":
        titles = ("AI automation roundup", "Support tools comparison 2026", "AI customer service trends", "Data policy changes reshape SaaS compliance", "Enterprise AI adoption trends", "DevOps platform consolidation trends", "[PAYWALLED] B2B SaaS market size forecast 2026", "AI product launches January 2026")
        found = email("editor@company.example.com")
        add("verbatim_titles", "digest按原文保留全部合格文章标题，付费文章带标记", any(all(t in x.get("body_plain", "") for t in titles) for _, x in found), [p for p, _ in found], "value")
        for identity, url in (("same_day_embargo", "https://news.example.com/devops-consolidation-2026"), ("curator_article", "https://news.example.com/ai-product-launches-jan")):
            row_values(identity, "ss_newslog", "ws_log", {"URL": url})
        logged = rows(final, "ss_newslog", "ws_log")
        add("digest_row_count", "已有3篇保留，新增8篇合格文章，共11行", len(logged) == 11, [p for p, _ in logged], "value")
        email_values("digest_count", "editor@company.example.com", ["8 article"])
        notices = [(p, x) for p, x in new_messages(record, "slack") if x.get("channel_id") == "ch_content_updates"]
        add("digest_slack_count", "content-updates摘要记录8篇新文章", any("8 article" in x.get("text", "") for _, x in notices), [p for p, _ in notices], "value")
    elif task == "marketing.campaign_handoff":
        for row in (2, 4, 6, 9):
            found = [(p, x) for p, x in rows(final, "ss_handoff", "ws_campaigns") if x["row_id"] == row]
            add("handoff_notes:" + str(row), "发送后将本次交接日期写入该活动notes", any("handoff sent on" in x["cells"].get("notes", "").lower() for _, x in found), [p for p, _ in found])
        # 本任务环境不落地 Drive files 集合、也不记账文档正文，归档与文档建立按 create_file 动作记录核验。
        created = [(p, x) for p, x in action_records(final, "google_drive") if x.get("action_key") == "create_file"]
        ops = {x["params"]["file"] for _, x in created if x["params"].get("name") == "ops"
               and x["params"].get("mimeType") == "application/vnd.google-apps.folder"}
        archived = [(p, x) for p, x in created if x["params"].get("folder") in ops
                    and x["params"].get("mimeType") == "application/vnd.google-apps.document"]
        add("archive_copy", "ops目录归档实际含交接文档", bool(archived), [p for p, _ in archived])
        for name in ("Q1 Webinar Series", "Partner Promotion", "Brand Refresh", "Co-Brand Launch"):
            found = [(p, x) for p, x in archived if name in str(x["params"].get("name", ""))]
            add("document_archive:" + name, "该活动的交接文档已创建在ops目录", bool(found), [p for p, _ in found], "value")
    elif task == "operations.purchase_order_three_way_match":
        for po, values in (("PO-5002", ("200", "180", "2700", "2500")), ("PO-5005", ("1650", "1500"))):
            found = action_records(final, "monday")
            add("mismatch_details:" + po, "Monday差异条目记录该PO及导致差异的实际数值", any(po in json.dumps(x.get("params", {})) and all(v in json.dumps(x.get("params", {})).replace(",", "") for v in values) for _, x in found), [p for p, _ in found], "value")
    elif task == "operations.equipment_calibration_scheduling":
        found = email("lab.manager@researchlab.org")
        add("all_instruments_summary", "实验室经理收到全部到期目标及各自日期", any(all(v in x.get("body_plain", "") for v in ("LAB-2001", "LAB-2003", "LAB-2006", "LAB-2007", "LAB-2009")) for _, x in found), [p for p, _ in found], "value")
    elif task == "operations.fleet_vehicle_maintenance":
        for vehicle in ("TRK-101", "TRK-102", "TRK-103", "SUV-301"):
            found = [(p, x) for p, x in objects(final, "google_calendar", "events") if x.get("summary") == "Service: " + vehicle]
            def times(x):
                start = x.get("start", {}).get("dateTime", x.get("start__dateTime", ""))
                end = x.get("end", {}).get("dateTime", x.get("end__dateTime", ""))
                return bool(start and end) and datetime.fromisoformat(start).astimezone(timezone.utc).hour == 8 and datetime.fromisoformat(end).astimezone(timezone.utc).hour == 10 and datetime.fromisoformat(start).day == 14
            add("service_time:" + vehicle, "实际日历预约在14日UTC08:00至10:00", any(times(x) for _, x in found), [p for p, _ in found], "value")
    elif task == "operations.hazmat_shipping_compliance":
        found = objects(final, "docusign", "envelopes")
        for address in ("h.weber@logistik-gmbh.example.com", "t.nakamura@jplogistics.example.com"):
            matched = [(p, x) for p, x in found if any(y.get("email") == address for y in x.get("signers", []))]
            add("sent_declaration:" + address, "国际危化品声明使用国际模板且实际发出", any(x.get("status") == "sent" and x.get("template_id") == "tpl_intl_hazmat" for _, x in matched), [p for p, _ in matched])
        email_values("sanctions_reporting", "compliance@company.example.com", ["SHP-810"])
    elif task == "operations.compressed_gas_cylinder_tracking":
        found = action_records(final, "jira")
        add("ticket_details", "CYL-007测试工单保留Hydrogen、Lab A和2020-12-01", any(all(v in json.dumps(x.get("params", {})) for v in ("CYL-007", "Hydrogen", "Lab A", "2020-12-01")) for _, x in found), [p for p, _ in found], "value")
    elif task in ("sales.slack_deal_notification", "sales.multi_hop_lookup", "sales.docusign_completion_processor"):
        targets = {"sales.slack_deal_notification": {"006xx000004QTM1"}, "sales.multi_hop_lookup": {"006xx000004MER1"}, "sales.docusign_completion_processor": {"opp_alpha", "opp_beta"}}[task]
        originals = {x["id"]: x for x in initial["salesforce"]["opportunities"]}
        for pointer, obj in objects(final, "salesforce", "opportunities"):
            if obj["id"] not in originals:
                continue
            prior = originals[obj["id"]]
            # 通知币种换算不能改变商机的原始货币价值；初态可能存 currency 为显式 null，末态可能不带该键，按实际取值比较。
            expected_amount = {"opp_alpha": 75000, "opp_beta": 50000}.get(obj["id"], prior.get("amount"))
            add("crm_amount:" + obj["id"], "CRM金额与币种表达相同的实际价值", money(obj.get("amount", 0)) == money(expected_amount) and obj.get("currency") == prior.get("currency"), [pointer], "value")
            if obj["id"] not in targets:
                add("unrelated_opportunity:" + obj["id"], "未授权的其他商机保持原有阶段", obj.get("stage", obj.get("stage_name")) == prior.get("stage", prior.get("stage_name")), [pointer], "prohibited")
        if task == "sales.slack_deal_notification":
            result = [item for item in result if item["id"] not in ("frozen:0", "frozen:11")]
            notices = [(p, x) for p, x in new_messages(record, "slack") if x.get("channel_id") == "C001DEALS"]
            add("latest_director_prefix", "按Feb18针对本交易的最新指示，公告使用MAJOR WIN前缀", any(x.get("text", "").startswith("MAJOR WIN:") for _, x in notices), [p for p, _ in notices], "value")
    elif task == "sales.demo_scheduling":
        found = [(p, x) for p, x in objects(final, "zoom", "meetings") if x.get("topic") == "Demo: Enterprise Platform"]
        add("demo_duration", "Zoom产品演示时长为60分钟", any(x.get("duration") == 60 for _, x in found), [p for p, _ in found], "value")
        add("demo_passcode", "Zoom产品演示已设置passcode", any(bool(x.get("passcode") or x.get("password")) for _, x in found), [p for p, _ in found], "value")
        messages = new_messages(record, "slack")
        semantic = read(INDEPENDENT / "semantic_decisions.json")["decisions"][record["execution_id"]]
        add("meeting_details", "通知中的入会链接和开始时间来自实际创建会议",
            semantic["meeting_details"], [p for p, _ in found + messages], "value")
    elif task == "sales.slack_channel_for_new_account":
        pass
    elif task == "support.gorgias_channel_consolidation":
        for duplicate, primary, address in (("g_204", "g_201", "anna@techfirm.com"), ("g_208", "g_201", "anna@techfirm.com"), ("g_206", "g_202", "marco@designs.co"), ("g_209", "g_203", "lee.chen@dataworks.io")):
            row_values("consolidation_log:" + duplicate, "ss_consol", "ws_log", {"Closed Ticket": duplicate, "Primary Ticket": primary, "Customer Email": address})
    elif task == "support.reamaze_cross_platform_dedup":
        for rm, fd, address in (("rm_1101", "fd_201", "alice@corp.com"), ("rm_1102", "fd_202", "bob@corp.com"), ("rm_1103", "fd_203", "carol@corp.com"), ("rm_1109", "fd_204", "alice@corp.com")):
            found = rows(final, "ss_dedup", "ws_log")
            add("dedup_log:" + rm, "去重日志中的跨平台身份、邮箱与实际完成状态一致", any(x["cells"].get("Reamaze ID") == rm and x["cells"].get("Freshdesk ID") == fd and x["cells"].get("Email") in (address, "a.chen@personal.com" if rm == "rm_1109" else address) and x["cells"].get("Status") in ("resolved", "Resolved", "Closed", "closed", "Completed", "completed") for _, x in found), [p for p, _ in found], "value")
    elif task == "support.freshdesk_auto_merge":
        for primary, duplicate, subject in (("fd_mg01", "fd_mg02", "Cannot access dashboard"), ("fd_mg03", "fd_mg04", "Billing overcharge"), ("fd_mg05", "fd_mg06", "SSO login failure")):
            row_values("merge_log:" + primary, "ss_merge_rules", "ws_log", {"Original Ticket": primary, "Duplicate Ticket": duplicate, "Subject": subject})
    elif task == "support.zendesk_cross_org_merge":
        for source, target, count in (("org_s1", "org_t1", 2), ("org_s2", "org_t2", 1), ("org_s5", "org_t3b", 0), ("org_s6", "org_t5", 3)):
            row_values("merge_count:" + source, "ss_merge", "ws_results", {"Source Org ID": source, "Target Org ID": target, "Tickets Transferred": count})
        found = [(p, x) for p, x in objects(final, "zendesk", "organizations") if x["id"] == "org_t3b"]
        add("empty_source_note", "Epsilon Target记录空来源EpsilonEmpty的已完成合并", any("EpsilonEmpty" in x.get("notes", "") for _, x in found), [p for p, _ in found], "value")
    elif task == "support.gorgias_defect_tracking":
        for ticket in ("g_df04", "g_df07", "g_df09", "g_df10", "g_df11"):
            found = rows(final, "ss_defects", "ws_log")
            add("log_every_defect:" + ticket, "任务要求log everything，记录被排除立项的缺陷及处理结局", any(x["cells"].get("Ticket ID") == ticket for _, x in found), [p for p, _ in found])
    else:
        raise ValueError("尚未阅读的任务: " + task)

    return result


def completion_states(record, checks):
    normal = record["termination_status"] == "completed" and record["execution_error"] is None
    unavailable = []
    environment = []
    if record["task"] == "hr.offboarding_multi_system":
        unavailable.append("初始环境缺少按离职类型处理的政策，PTO支付等义务尚无授权依据")
    if record["task"] == "marketing.campaign_handoff":
        unavailable.append("Drive动作未保存交接文档正文，无法核验文档所含信息与已发邮件的归档一致性")
    if record["task"] == "hr.compliance_training_enrollment":
        unavailable.append("BambooHR员工档案与课程类型在环境缺失，报名身份只能按提交动作核验")
    if record["task"] == "finance.xero_credit_allocation":
        environment = ["balance:INV-5004", "balance:INV-5006", "balance:INV-5007", "balance:INV-5009"]
    known = all(item["passed"] for item in checks) and normal
    executable = all(item["passed"] for item in checks if item["id"] not in environment) and normal
    state = "failure" if not known else "unknown" if unavailable else "success"
    alternatives = {}
    if record["task"] == "finance.xero_quote_to_invoice":
        values = [item["passed"] for item in checks if item["id"] != "invoice_total:Alpine Solutions Group"]
        correct = any(x.get("contact_name") == "Alpine Solutions Group" and money(x["total"]) == 7875 for _, x in objects(record["final_world"], "xero", "invoices"))
        alternatives["accepted_quote_policy"] = all(values) and correct and normal
    if record["task"] == "sales.slack_deal_notification":
        values = [item["passed"] for item in checks if item["id"] != "latest_director_prefix"]
        correct = any(x.get("channel_id") == "C001DEALS" and x.get("text", "").startswith("CLOSED:") for _, x in new_messages(record, "slack"))
        alternatives["vp_standard_prefix"] = all(values) and correct and normal
    if record["task"] == "support.gorgias_defect_tracking":
        values = [item["passed"] for item in checks if item["id"] not in ("log_every_defect:g_df09", "log_every_defect:g_df11")]
        alternatives["recall_excludes_logging"] = all(values) and normal
    return {"full_business_success": state == "success", "full_business_state": state,
            "known_obligation_success": known, "executable_business_success": executable,
            "executable_business_state": "failure" if not executable else "unknown" if unavailable else "success",
            "executable_obligation_success": {item["id"]: item["passed"] for item in checks if item["id"] not in environment},
            "environment_unimplemented": environment, "unknown_obligations": unavailable,
            "alternative_success": alternatives}


def main():
    labels = {t["task"]: t for b in read(FROZEN / "labels_v1.json")["blocks"] for t in b["tasks"]}
    mapping = read(PAID / "business_review/mapping.json")
    executions = []
    citation_counts = Counter()
    requirement_sets = {}
    for masked, item in mapping.items():
        verdict = read(PAID / "business_review/verdicts" / (masked + ".json"))
        bundle = read(PAID / "business_review/materials" / (masked + ".json"))
        for requirement in verdict["requirements"]:
            for citation in requirement["evidence"]:
                node = resolve_pointer(bundle, citation["pointer"])
                content = node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
                citation_counts["references"] += 1
                citation_counts["valid_nonempty_references"] += int(bool(citation["quote"]) and citation["quote"] in content)
        record = read(PAID / "executions" / item["arm"] / (item["execution_id"] + ".json"))
        checks = evaluate(record, labels[item["task"]], verdict)
        normal = record["termination_status"] == "completed" and record["execution_error"] is None
        executions.append({**item, "source_path": str((PAID / "executions" / item["arm"] / (item["execution_id"] + ".json")).relative_to(ROOT)),
                           "masked_id": masked, "strict_success": record["success"],
                           "frozen_success": all(x["passed"] for x in verdict["requirements"]) and normal,
                           **completion_states(record, checks),
                           "requirements": checks, "reviewer": "Codex代理读取任务授权资料并定义对象核验",
                           "scope": "migration", "cost_usd": record["cost_usd"]})
        requirement_sets[item["task"]] = [{k: x[k] for k in ("id", "requirement", "req_class", "source")} for x in checks]
    for arm in ("source", *(a + "_source" for a in ARMS)):
        for path in sorted((PAID / "executions" / arm).glob("*.json")):
            record = read(path)
            checks = evaluate(record, labels[record["task"]])
            normal = record["termination_status"] == "completed" and record["execution_error"] is None
            executions.append({"domain": record["domain"], "task": record["task"], "arm": arm,
                               "execution_id": record["execution_id"], "repeat": record["repeat"],
                               "source_path": str(path.relative_to(ROOT)), "strict_success": record["success"],
                               "frozen_success": None, **completion_states(record, checks),
                               "requirements": checks, "reviewer": "Codex代理读取任务授权资料并定义对象核验",
                               "scope": "source", "cost_usd": record["cost_usd"]})
            requirement_sets[record["task"]] = [{k: x[k] for k in ("id", "requirement", "req_class", "source")} for x in checks]
    if len(executions) != 462 or len(requirement_sets) != 30:
        raise ValueError("执行或任务覆盖数量不符")
    save("execution_reviews.json", executions)
    save("task_requirements.json", {"review_order": "逐题阅读提示词和初态后编写核验条件，再计算新汇总；已知既有冻结汇总；独立判读及真实读取时间保存在independent_review/", "tasks": requirement_sets})
    groups = defaultdict(list)
    for x in executions:
        groups[(x["scope"], x["domain"], x["arm"])].append(x)
    summary = [{"scope": key[0], "domain": key[1], "arm": key[2], "n": len(items),
                "strict_successes": sum(x["strict_success"] for x in items),
                "frozen_successes": sum(x["frozen_success"] for x in items) if key[0] == "migration" else None,
                "full_business_successes": sum(x["full_business_success"] for x in items),
                "full_business_unknowns": sum(x["full_business_state"] == "unknown" for x in items),
                "executable_business_successes": sum(x["executable_business_success"] for x in items),
                "strict_false_passes": sum(x["strict_success"] and x["full_business_state"] == "failure" for x in items),
                "strict_pass_business_unknown": sum(x["strict_success"] and x["full_business_state"] == "unknown" for x in items),
                "strict_false_rejections": sum(not x["strict_success"] and x["full_business_success"] for x in items)} for key, items in sorted(groups.items())]
    save("summary.json", summary)
    save("authorization_alternatives.json", [{"execution_id": item["execution_id"], "task": item["task"], "scope": item["scope"], "arm": item["arm"], "repeat": item["repeat"], "primary_success": item["full_business_success"], "primary_state": item["full_business_state"], "alternative_success": item["alternative_success"]} for item in executions if item["alternative_success"]])
    save("review_provenance.json", {"executions": 462, "migration": 432, "source": 30,
                                   "task_prompts_and_authorized_initial_materials_read": 30,
                                   "original_verdicts": 432, "citation_validation": dict(citation_counts),
                                   "original_provider": "unknown_saved_verdicts_do_not_identify_provider",
                                   "human_review": read(OUT / "human_review.json"),
                                   "review_mode": "代理阅读定义补充义务，已有冻结要求继承既有判读；程序核验对象及明确取值",
                                   "independent_agent_review": "独立代理先保存30题义务，再比较原业务判定；全部新增义务独立对象核验，审批、先后顺序与会议通知逐执行阅读。",
                                   "complete_and_executable": "full_business_state遇到缺失资料保留unknown；full_business_success仅success为true；executable_business_success只汇总已定义且可执行义务，未实现的invoice余额副作用单列，未修改实际终态或错误状态。",
                                   "authorization": {"QU104_primary": "直接任务明确要求8%加价，含5%fee为8505", "QU104_alternative": "accepted quote政策保持原价，含fee为7875", "sales_primary": "Feb18针对本deal最新Director指示MAJOR WIN", "sales_alternative": "Feb15 VP标准CLOSED"},
                                   "unobserved": ["offboarding分离类型政策在初始环境缺失", "未取得原标注提供方逐调用来源", "本次为补充评价，已知冻结汇总，未宣称全部冻结要求重新独立盲审",
                                                  "Drive 文档正文不在记录中：全部 462 次执行的 google_drive.files 集合未落地且无正文，交接文档内容按 create_file 动作的对象与目录核验",
                                                  "hr.compliance_training_enrollment 的 BambooHR 侧无员工档案与培训类型（目录与 training/type 均返回空），报名写入只能按动作中的员工身份核验"]})
    print(json.dumps({"execution_count": len(executions), "task_count": len(requirement_sets), "full_business_successes": sum(x["full_business_success"] for x in executions)}, ensure_ascii=False))


if __name__ == "__main__":
    argparse.ArgumentParser().parse_args()
    main()
