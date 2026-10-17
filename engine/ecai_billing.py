import csv
import argparse
import asyncio
import datetime
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET_DAY = "2026-10-09"
PRICES = {"input_cache_miss_tokens": Decimal("0.000001"),
          "input_cache_hit_tokens": Decimal("0.00000002"), "output_tokens": Decimal("0.000004")}


def official_export(folder):
    amounts = list(folder.glob("amount-*.csv"))
    costs = list(folder.glob("cost-*.csv"))
    if len(amounts) != 1 or len(costs) != 1:
        raise ValueError("官方导出必须包含一份用量 CSV 和一份费用 CSV")
    with amounts[0].open(encoding="utf-8-sig", newline="") as stream:
        usage = list(csv.DictReader(stream))
    with costs[0].open(encoding="utf-8-sig", newline="") as stream:
        charges = list(csv.DictReader(stream))
    usage = [row for row in usage if row["start_time_iso"].startswith(TARGET_DAY)]
    charges = [row for row in charges if row["start_time_iso"].startswith(TARGET_DAY)]
    if not usage or not charges:
        raise ValueError("导出缺少本批日期")
    # 凭据字段只在内存核对导出的一致性，不写入对账记录或日志。
    if len({row["api_key"] for row in usage}) != 1 or {row["model"] for row in usage + charges} != {"deepseek-flash"}:
        raise ValueError("本批导出包含多个凭据或模型，不能合并结算")
    if {row["currency"] for row in charges} != {"CNY"}:
        raise ValueError("官方费用币种不符合本批现金账本")
    totals = {key: 0 for key in (*PRICES, "request_count")}
    calculated = Decimal(0)
    for row in usage:
        kind = row["type"]
        totals[kind] += int(row["amount"])
        if kind in PRICES:
            if Decimal(row["price"]) != PRICES[kind]:
                raise ValueError("官方导出单价与冻结条件不同")
            calculated += PRICES[kind] * int(row["amount"])
    paid = sum((Decimal(row["cost"]) for row in charges), Decimal(0))
    if calculated != paid:
        raise ValueError("官方用量与官方费用不一致")
    return {"official_cash_cny": float(paid), "totals": totals,
            "exported_at": min(path.stat().st_mtime for path in amounts + costs),
            "files": [str(path.relative_to(ROOT)) for path in amounts + costs]}


def reconcile():
    exports = [official_export(ROOT / "docs" / name) for name in
               ("usage_data_2026-09-10_2026-10-09", "usage_data_2026-10-09_2026-10-09")]
    if exports[0]["totals"] != exports[1]["totals"] or exports[0]["official_cash_cny"] != exports[1]["official_cash_cny"]:
        raise ValueError("同日两份官方导出不一致")
    begin = datetime.datetime.fromisoformat(TARGET_DAY + "T00:00:00+08:00").timestamp()
    end = begin + 86400
    paths = [ROOT / "state/ecai_p1_ledger.sqlite",
             *sorted((ROOT / "archive").glob("2026-10-09-ecai*-void*/ecai_p1_ledger.sqlite"))]
    ledgers = []
    known_totals = {key: 0 for key in (*PRICES, "request_count")}
    for path in paths:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
            records = db.execute("SELECT id,cost,input_tokens,output_tokens,cached_tokens,at FROM calls").fetchall()
        if any(not begin <= row[5] < end for row in records):
            raise ValueError("本批费用账本存在导出日期之外的调用")
        if any(row[5] >= exports[1]["exported_at"] for row in records):
            raise ValueError("账本存在官方导出之后的调用")
        known = {key: 0 for key in (*PRICES, "request_count")}
        for _, cost, input_tokens, output_tokens, cached_tokens, _ in records:
            if cost is not None and cost > 0:
                known["request_count"] += 1
                known["input_cache_miss_tokens"] += input_tokens - cached_tokens
                known["input_cache_hit_tokens"] += cached_tokens
                known["output_tokens"] += output_tokens
        for key in known:
            known_totals[key] += known[key]
        known_cash = sum((PRICES[key] * known[key] for key in PRICES), Decimal(0))
        ledgers.append({"ledger": str(path.relative_to(ROOT)), "covered_call_ids": [row[0] for row in records],
                        "known_cash_cny": float(known_cash), "usage": known,
                        "usage_unknown_calls": sum(row[1] is None for row in records)})
    official = exports[1]
    count = sum(len(row["covered_call_ids"]) for row in ledgers)
    residual = {key: official["totals"][key] - known_totals[key] for key in known_totals}
    if any(value < 0 for value in residual.values()) or official["totals"]["request_count"] > count:
        raise ValueError("官方用量不能覆盖本地已结算调用，不能关闭预留")
    known_cash = sum((Decimal(str(row["known_cash_cny"])) for row in ledgers), Decimal(0))
    unassigned = Decimal(str(official["official_cash_cny"])) - known_cash
    if unassigned != sum((PRICES[key] * residual[key] for key in PRICES), Decimal(0)):
        raise ValueError("未知用量费用与官方余项不一致")
    result = {"status": "official_aggregate_reconciled", "day": TARGET_DAY,
              "assumption": "该凭据在导出日期内的调用全部来自有效与归档账本，导出之后本批未继续调用",
              "official_cash_cny": official["official_cash_cny"], "official_usage": official["totals"],
              "known_cash_cny": float(known_cash), "unassigned_cash_cny": float(unassigned),
              "unassigned_usage": residual, "ledgers": ledgers,
              "export_files": list(dict.fromkeys(file for export in exports for file in export["files"])),
              "exported_at": official["exported_at"],
              "individual_usage_status": "未知调用继续保留原状态，官方汇总费用覆盖其现金责任；不分摊伪造逐调用用量"}
    target = ROOT / "state/ecai_billing_reconciliation.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "official_cash_cny", "known_cash_cny", "unassigned_cash_cny")}), flush=True)


def reconcile_connect_timeouts():
    from engine.llm_client import BASE_URL
    from verifiers.clients import OpenAIChatCompletionsClient
    from verifiers.types import ClientConfig
    from engine.scoped_units import save_json

    probe = OpenAIChatCompletionsClient(ClientConfig(api_key_var="MODEL_API", api_base_url=BASE_URL))
    timeouts = probe.client.timeout.as_dict()
    asyncio.run(probe.close())
    if timeouts != {"connect": 5.0, "read": 3600.0, "write": 3600.0, "pool": 3600.0}:
        raise ValueError("客户端超时条件发生变化，不能重建原请求的连接阶段")
    billing = json.loads((ROOT / "state/ecai_billing_reconciliation.json").read_text(encoding="utf-8"))
    covered = set(next(row["covered_call_ids"] for row in billing["ledgers"]
                       if row["ledger"] == "state/ecai_p1_ledger.sqlite"))
    settled = []
    ledger = ROOT / "state/ecai_p1_ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        pending = db.execute("SELECT id,reserved,phase FROM calls WHERE cost IS NULL").fetchall()
        for call_id, reserved, phase in pending:
            if call_id in covered:
                continue
            if not phase.startswith("evaluation/B0prime/execution/"):
                continue
            folder = ROOT / "experiments/ecai_2027/mechanism/io" / call_id
            failure_path = folder / "failure.json"
            if not failure_path.exists() or (folder / "response.json").exists():
                continue
            failure = json.loads(failure_path.read_text(encoding="utf-8"))
            request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
            if not (datetime.datetime.fromisoformat("2026-10-09T10:01:45+00:00").timestamp()
                    <= request["at"] <= datetime.datetime.fromisoformat("2026-10-09T10:17:10+00:00").timestamp()):
                continue
            if "sampling_args" not in request["parameters"] or "timeout" in request["parameters"]:
                raise ValueError("请求不符合已确认的任务执行客户端契约")
            elapsed = failure_path.stat().st_mtime - request["at"]
            if failure["error_type"] != "APITimeoutError" or not 0 < elapsed < 3600:
                continue
            # 调用链没有单次 timeout 覆盖；其余三个传输阶段均须等待 3600 秒。
            # 撤掉固定客户端超时条件，短历时就不能推出推理请求尚未发送。
            evidence = {"call_id": call_id, "elapsed_upper_bound_seconds": elapsed, "client_timeouts": timeouts,
                        "stage": "connect", "cash_cny": 0.0, "released_reserved_usd": reserved}
            db.execute("UPDATE calls SET cost=0,note=? WHERE id=? AND cost IS NULL",
                       ("固定客户端超时与请求历时确认连接建立超时，推理请求未发送", call_id))
            failure.update(billing_unconfirmed=False, settlement_status="unsent_connect_timeout_reconciled",
                           transport_stage_evidence=evidence)
            save_json(failure_path, failure)
            settled.append(evidence)
    save_json(ROOT / "state/ecai_transport_reconciliation.json", {"status": "reconciled", "calls": settled,
              "model_calls": 0, "client_timeouts": timeouts})
    print(json.dumps({"status": "reconciled", "unsent_calls": len(settled),
                      "released_reserved_cny": sum(row["released_reserved_usd"] for row in settled) * 7.1,
                      "model_calls": 0}), flush=True)


def record_balance():
    import httpx
    from engine.llm_client import client
    from engine.scoped_units import save_json

    with sqlite3.connect((ROOT / "state/ecai_p1_ledger.sqlite").resolve().as_uri() + "?mode=ro", uri=True) as db:
        identifiers = [row[0] for row in db.execute("SELECT id FROM calls")]
    response = client.with_options(max_retries=0, timeout=httpx.Timeout(60, connect=30)).get(
        "/user/balance", cast_to=dict[str, object])
    balances = [row for row in response["balance_infos"] if row["currency"] == "CNY"]
    if len(balances) != 1 or not response["is_available"]:
        raise ValueError("官方余额不可用或币种不唯一")
    receipt = {"at": time.time(), "balance_cny": float(balances[0]["total_balance"]),
               "covered_call_ids": identifiers, "rounding_margin_cny": 0.01,
               "assumption": "余额快照之后没有充值或本实验之外的账户支出",
               "model_calls": 0}
    save_json(ROOT / "state/ecai_balance_receipt.json", receipt)
    print(json.dumps({key: receipt[key] for key in ("balance_cny", "at", "model_calls")}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=["official", "connect_timeouts", "balance"], default="official")
    arguments = parser.parse_args()
    if arguments.command == "official":
        reconcile()
    elif arguments.command == "connect_timeouts":
        reconcile_connect_timeouts()
    else:
        record_balance()
