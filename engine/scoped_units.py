import hashlib
import os
import fcntl
import json
import sqlite3
import uuid
import subprocess
import time
from email.utils import parsedate_to_datetime
from pathlib import Path

from scipy.stats import binomtest

POLICY = {
    "service_retries": 10, "service_retry_delay_seconds": 2, "service_retry_max_delay_seconds": 60,
    "format_retries": 1, "candidate_repairs": 3,
    "pilot_unit_rounds": 6, "main_experiment_rounds_per_failed_task": 1,
    "round_budget_tasks": 20, "observed_cost_default_usd": 0.03,
    "max_steps": 50, "execution_output_tokens": 8192, "proposal_output_tokens": 32768,
    "max_pairs": 8, "seed": 100, "sage_alpha": 0.05, "sage_penalty": 1.0,
    "sage_regression_cap": 0.0, "task_cost_cap_usd": 0.15, "default_components": True,
    "pilot_limit_usd": 5.0, "total_limit_usd": 45.0,
    "window_limit_usd": 12.0, "window_seconds": 18000,
}


class AIFormatError(ValueError):
    pass


class ExecutionBlocked(RuntimeError):
    pass


def require_model_access():
    if (Path(__file__).resolve().parent.parent / "experiments/pilot/paid_execution_hold.json").exists():
        raise ExecutionBlocked("先导付费执行已暂停，启动条件尚未满足")
    if os.environ.get("GNOSIS_DISABLE_MODEL_CALLS") == "1":
        raise ExecutionBlocked("本次验证禁止实际模型调用")


class ProposalInterrupted(RuntimeError):
    def __init__(self, category, call):
        self.category = category
        self.call = call
        super().__init__("提议请求尚未完成：" + category + "，调用记录 " + str(call))


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    with pending.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)


def harness_blocker(output, error, phase, task=None):
    record = {"phase": phase, "task": task, "error_type": type(error).__name__,
              "category": "harness_or_integration_error", "at": time.time()}
    append_record(Path(output) / "harness_failures.jsonl", record)
    save_json(Path(output) / "harness_blocker.json", record)


def require_harness_ready(output):
    output = Path(output)
    if (output / "harness_blocker.json").exists():
        raise ExecutionBlocked("存在未修复的实验脚本错误，禁止执行任务")
    audit = json.loads((output / "workflow_audit.json").read_text(encoding="utf-8"))
    if audit.get("script_status") != "clear":
        raise ExecutionBlocked("实验脚本审计尚未通过，禁止执行任务")
    if audit["experiment_version"] != experiment_version(output):
        raise ExecutionBlocked("实验版本改变后尚未完成脚本审计，禁止执行任务")
    root = Path(__file__).resolve().parent.parent
    for name, digest in audit["reviewed_files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ExecutionBlocked("实验脚本改变后尚未完成审计：" + name)


class ProposalCheckpoint:
    def __init__(self, path, version, request, options):
        self.path = Path(path)
        identity = fingerprint({"version": version, "request": request, "options": options})
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state["identity"] != identity:
                raise RuntimeError("提议断点的输入或实验版本发生变化")
            if self.state["status"] not in ("ready", "inflight", "complete", "failed"):
                raise ValueError("提议断点状态不符合协议")
            if not 0 <= self.state["format_attempt"] <= POLICY["format_retries"]:
                raise ValueError("提议断点的格式重试次数不符合协议")
            if not 0 <= self.state["service_attempt"] <= POLICY["service_retries"]:
                raise ValueError("提议断点的服务重试次数不符合协议")
        else:
            self.state = {"identity": identity, "status": "ready", "request": request,
                          "format_attempt": 0, "service_attempt": 0, "calls": [],
                          "retry_of": None, "call": None}
            self.save()

    def save(self, **changes):
        self.state.update(changes)
        save_json(self.path, self.state)

    def start(self, call_id):
        self.state["calls"].append(call_id)
        self.save(status="inflight", call=call_id,
                  retry_of=self.state["retry_of"] or call_id)


VERSION_FILES = ["engine/automation_adapter.py", "engine/mcp_proxy.py", "engine/component_host.py",
                 "engine/component_sandbox.py", "engine/llm_client.py", "engine/gnosis_lm.py",
                 "host/src/scoped-worker.mjs", "host/package-lock.json"]
VERSION_POLICY = ("seed", "max_steps", "execution_output_tokens", "proposal_output_tokens",
                  "default_components", "format_retries", "task_cost_cap_usd")


def experiment_version(output):
    """版本只覆盖影响模型输入与评分的部分：注入器、组件宿主与 worker、模型客户端、采样与步数参数、上游提交。

    预算上限、重试次数、门禁参数与编排代码（run_pilot）不进版本号：改它们不该让已跑的基线作废。
    """
    root = Path(__file__).resolve().parent.parent
    record = {"policy": {name: POLICY[name] for name in VERSION_POLICY},
              "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in VERSION_FILES},
              "upstream": {name: subprocess.check_output(["git", "-C", str(root / "scratch" / name), "rev-parse", "HEAD"], text=True).strip()
                           for name in ("AutomationBench", "ace")}}
    version = fingerprint(record)
    path = Path(output) / "versions" / (version + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return version


class CallJournal:
    def __init__(self, output, version):
        self.output = Path(output)
        self.version = version

    def verify_version(self):
        require_harness_ready(self.output)
        if experiment_version(self.output) != self.version:
            raise RuntimeError("接入程序或固定规则发生变化，停止当前实验；需要登记新的实验版本")

    def request(self, call_id, phase, messages, parameters, task=None, unit=None):
        save_json(self.output / "runtime_status.json", {"phase": phase, "task": task,
                  "unit": unit, "call": call_id, "at": time.time(), "experiment_version": self.version})
        folder = self.output / "io" / call_id
        folder.mkdir(parents=True, exist_ok=False)
        (folder / "request.json").write_text(json.dumps({
            "version": self.version, "phase": phase, "task": task, "unit": unit,
            "at": time.time(), "messages": messages, "parameters": parameters,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return folder

    def response(self, folder, response):
        (folder / "response.json").write_text(response.model_dump_json(indent=2), encoding="utf-8")

    def failure(self, folder, category, error, **details):
        from openai import APIStatusError

        record = {"version": self.version, "category": category, "error_type": type(error).__name__,
                  "error": f"服务返回 HTTP {error.status_code}" if isinstance(error, APIStatusError) else str(error), "call": folder.name,
                  **details}
        (folder / "failure.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        append_record(self.output / "errors.jsonl", record)

    def retry_service(self, folder, error, attempt):
        from openai import APIConnectionError, APIStatusError

        transient = isinstance(error, APIConnectionError) or (
            isinstance(error, APIStatusError) and (error.status_code in (408, 429) or error.status_code >= 500)
        )
        retry = transient and attempt < POLICY["service_retries"]
        delay = retry_delay(attempt, error) if retry else None
        self.failure(folder, "service_connection_failure", error, service_attempt=attempt,
                     retry_scheduled=retry,
                     retry_delay_seconds=delay)
        return delay


class BudgetExceeded(RuntimeError):
    pass


class TaskCostExceeded(RuntimeError):
    pass


def retry_after_seconds(value):
    if value is None:
        return 0.0
    try:
        suggested = float(value)
    except ValueError:
        suggested = parsedate_to_datetime(value).timestamp() - time.time()
    return max(0.0, suggested)


def retry_delay(attempt, error=None):
    """指数退避自身设上限；服务指定的 Retry-After 等待完整执行。"""
    base = min(POLICY["service_retry_delay_seconds"] * (2 ** attempt),
               POLICY["service_retry_max_delay_seconds"])
    headers = getattr(getattr(error, "response", None), "headers", None) or {}
    return max(base, retry_after_seconds(headers.get("retry-after")))


def _ensure_ledger(path, limit, cap=None):
    # 旧账本缺少的列就地补齐。历史请求没有时间戳，记 0 使其落在任何 5 小时窗口之外。
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=60) as db:
        db.execute("CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY, ceiling REAL)")
        db.execute("INSERT OR IGNORE INTO budget(id, ceiling) VALUES (1, ?)", (limit,))
        if "cap" not in {row[1] for row in db.execute("PRAGMA table_info(budget)")}:
            db.execute("ALTER TABLE budget ADD COLUMN cap REAL")
        if cap is not None:
            db.execute("UPDATE budget SET cap=? WHERE id=1 AND cap IS NULL", (cap,))
        db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, phase TEXT, "
                   "reserved REAL, cost REAL, input_tokens INTEGER, output_tokens INTEGER, "
                   "cached_tokens INTEGER)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
        if "at" not in columns:
            db.execute("ALTER TABLE calls ADD COLUMN at REAL")
            db.execute("UPDATE calls SET at=0 WHERE at IS NULL")
        if "note" not in columns:
            db.execute("ALTER TABLE calls ADD COLUMN note TEXT")


class Budget:
    """单一账本，三级上限同时生效：研究总预算、当前阶段上限、5 小时窗口上限。

    预留按提示的 UTF-8 字节数折算 token 上界。归档实测 3.91–4.22 字节/token
    （含中文与 JSON 的几个提示），按 3 折算留出约 30% 余量。
    结算按网关返回的真实 usage 修正；没有取得响应体的失败按未计费释放预留。
    """

    def __init__(self, path, limit, rates=None, output=None, window_limit=None,
                 window_seconds=18000, cap=None):
        self.path = Path(path)
        self.output = Path(output) if output is not None else self.path.parent
        self.rates = rates or {"input_per_mtok": 0.15, "output_per_mtok": 0.6,
                               "cache_read_per_mtok": 0.003}
        self.window_limit = window_limit
        self.window_seconds = window_seconds
        _ensure_ledger(self.path, limit, cap)
        with self.connect() as db:
            ceiling, cap_value = db.execute("SELECT ceiling, cap FROM budget WHERE id=1").fetchone()
        if ceiling != limit:
            raise ValueError("预算上限与既有账本不一致")
        if cap_value is None or ceiling > cap_value:
            raise ValueError("当前上限超出研究总预算")

    def connect(self):
        return sqlite3.connect(self.path, timeout=60)

    @classmethod
    def change_limit(cls, path, previous, new, reason, cap=None):
        if new <= 0 or not reason:
            raise ValueError("预算调整需要有效上限和授权理由")
        _ensure_ledger(path, previous, cap)
        with sqlite3.connect(path, timeout=60) as db:
            db.execute("BEGIN IMMEDIATE")
            spent = db.execute("SELECT COALESCE(SUM(COALESCE(cost, reserved)), 0) FROM calls").fetchone()[0]
            if new < spent:
                raise ValueError("新预算低于已计费和预留费用")
            cap_value = db.execute("SELECT cap FROM budget WHERE id=1").fetchone()[0]
            if cap_value is None or new > cap_value:
                raise ValueError("新上限超出研究总预算")
            changed = db.execute("UPDATE budget SET ceiling=? WHERE id=1 AND ceiling=?", (new, previous))
            if changed.rowcount != 1:
                raise ValueError("预算调整的原上限与账本不一致")
            db.execute("CREATE TABLE IF NOT EXISTS budget_changes "
                       "(previous REAL, next REAL, reason TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO budget_changes(previous,next,reason) VALUES (?,?,?)", (previous, new, reason))

    @property
    def ceiling(self):
        with self.connect() as db:
            return db.execute("SELECT ceiling FROM budget WHERE id=1").fetchone()[0]

    def _spent(self, db):
        return db.execute("SELECT COALESCE(SUM(COALESCE(cost, reserved)), 0) FROM calls").fetchone()[0]

    def _window_used(self, db, now):
        return db.execute("SELECT COALESCE(SUM(COALESCE(cost, reserved)), 0) FROM calls WHERE at > ?",
                          (now - self.window_seconds,)).fetchone()[0]

    def reserve(self, phase, prompt, max_tokens):
        tokens = len(json.dumps(prompt, ensure_ascii=False).encode()) // 3 + 1024
        amount = (tokens * self.rates["input_per_mtok"]
                  + max_tokens * self.rates["output_per_mtok"]) / 1e6
        call_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            spent = self._spent(db)
            limit = db.execute("SELECT ceiling FROM budget").fetchone()[0]
            if spent + amount > limit:
                raise BudgetExceeded(f"预算终止：已计费或预留 ${spent:.6f}，本次上界 ${amount:.6f}，上限 ${limit}")
            if self.window_limit is not None:
                window = self._window_used(db, now)
                if window + amount > self.window_limit:
                    raise BudgetExceeded(
                        f"窗口终止：{self.window_seconds // 3600} 小时内已计费或预留 ${window:.6f}，"
                        f"本次上界 ${amount:.6f}，窗口上限 ${self.window_limit}；"
                        f"阶段剩余 ${limit - spent:.6f}")
            db.execute("INSERT INTO calls(id, phase, reserved, at) VALUES (?, ?, ?, ?)",
                       (call_id, phase, amount, now))
        return call_id

    def settle(self, call_id, usage):
        cached = usage.prompt_tokens_details.cached_tokens if usage.prompt_tokens_details else 0
        cached = cached or 0
        cost = ((usage.prompt_tokens - cached) * self.rates["input_per_mtok"]
                + cached * self.rates["cache_read_per_mtok"]
                + usage.completion_tokens * self.rates["output_per_mtok"]) / 1e6
        with self.connect() as db:
            db.execute("UPDATE calls SET cost=?, input_tokens=?, output_tokens=?, cached_tokens=? WHERE id=?",
                       (cost, usage.prompt_tokens, usage.completion_tokens, cached, call_id))
        return cost

    def release(self, call_id, reason):
        # 没有取得响应体的失败按未计费释放预留；已结算的记录不变。
        with self.connect() as db:
            changed = db.execute("UPDATE calls SET cost=0.0, note=? WHERE id=? AND cost IS NULL",
                                 (reason, call_id))
        return changed.rowcount == 1

    def totals(self):
        with self.connect() as db:
            rows = db.execute("SELECT phase, COUNT(*), SUM(cost), "
                              "SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END) "
                              "FROM calls GROUP BY phase").fetchall()
        return [{"phase": p, "calls": n, "cost_usd": c or 0,
                 "unsettled_usd": r or 0} for p, n, c, r in rows]

    def state(self):
        now = time.time()
        with self.connect() as db:
            spent, pending = db.execute("SELECT COALESCE(SUM(cost), 0), "
                                        "COALESCE(SUM(CASE WHEN cost IS NULL THEN reserved ELSE 0 END), 0) "
                                        "FROM calls").fetchone()
            ceiling, cap = db.execute("SELECT ceiling, cap FROM budget WHERE id=1").fetchone()
            window = self._window_used(db, now)
        return {"ceiling_usd": ceiling, "research_cap_usd": cap, "spent_usd": spent,
                "unsettled_usd": pending, "remaining_usd": ceiling - spent - pending,
                "window_limit_usd": self.window_limit, "window_seconds": self.window_seconds,
                "window_used_usd": window,
                "window_remaining_usd": None if self.window_limit is None else self.window_limit - window}


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class UnitLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def read(self, unit_id):
        if Path(unit_id).name != unit_id or unit_id in (".", ".."):
            raise ValueError("单元 ID 必须是单个路径名称")
        record = json.loads((self.root / unit_id / "unit.json").read_text(encoding="utf-8"))
        if record["component"] and (self.root / unit_id / "component.ts").read_text(encoding="utf-8") != record["source"]:
            raise ValueError("实际组件源码与知识单元版本不一致")
        return record

    def records(self):
        return [self.read(p.parent.name) for p in sorted(self.root.glob("*/unit.json"))]

    def units(self, candidate=None):
        rows = self.records()
        active = [u for u in rows if u["status"] == "accepted" or u["id"] == candidate]
        replaced = {name for u in active for name in u.get("replaces", [])}
        return [u for u in active if u["id"] not in replaced]

    def propose(self, unit, provenance=None):
        required = {"scope", "content", "source_task", "prediction", "component", "source", "replaces"}
        if set(unit) != required or not isinstance(unit["scope"], list) or not unit["scope"]:
            raise AIFormatError("候选单元协议错误")
        if not isinstance(unit["replaces"], list) or any(not isinstance(r, str) or not r for r in unit["replaces"]):
            raise AIFormatError("replaces 必须是单元 ID 列表")
        if any(not (self.root / name / "unit.json").is_file() for name in unit["replaces"]):
            raise AIFormatError("replaces 指向不存在的单元")
        if any(not isinstance(s, str) or not s for s in unit["scope"]):
            raise AIFormatError("作用域必须使用非空字符串")
        if any(not isinstance(unit[k], str) or not unit[k] for k in ("content", "source_task", "prediction")):
            raise AIFormatError("单元内容、来源和预测必须使用非空字符串")
        if unit["component"] not in (None, "T", "C", "W"):
            raise AIFormatError("组件必须使用 null、T、C 或 W 协议")
        if bool(unit["component"]) != bool(unit["source"]):
            raise AIFormatError("组件与源码必须同时提供")
        unit_id = fingerprint(unit)[:20]
        folder = self.root / unit_id
        if (folder / "unit.json").exists():
            raise AIFormatError("AI 返回了已存在的同一单元，没有产生新版本")
        folder.mkdir(exist_ok=True)
        record = {**unit, "id": unit_id, "status": "proposed"}
        if provenance:
            record["provenance"] = provenance
        if unit["source"]:
            (folder / "component.ts").write_text(unit["source"], encoding="utf-8")
        save_json(folder / "unit.json", record)
        return record

    def decide(self, unit_id, pairs, contracts, alpha=0.05, penalty=1.0, regression_cap=0.0):
        if not 0 < alpha <= 1 or penalty < 1 or not 0 <= regression_cap <= 1:
            raise ValueError("SAGE 参数超出协议范围")
        if not pairs or len({p["task"] for p in pairs}) != len(pairs):
            raise ValueError("门禁需要互不重复的任务配对")
        record = self.read(unit_id)
        affected = set(affected_tasks(record, contracts)) - {record["source_task"]}
        if len(pairs) > 8 or any(p["task"] not in affected for p in pairs):
            raise ValueError("门禁最多使用 8 道独立且受作用域影响的任务")
        versions = [fingerprint([{k: v for k, v in u.items() if k not in ("status", "evidence")}
                                 for u in self.units(candidate)]) for candidate in (None, unit_id)]
        if any(p["before_version"] != versions[0] or p["after_version"] != versions[1] for p in pairs):
            raise ValueError("配对证据与当前知识库和候选源码版本不一致")
        if any(p["before_version"] != pairs[0]["before_version"] for p in pairs):
            raise ValueError("门禁对照版本不一致")
        if any(p["after_version"] != pairs[0]["after_version"] for p in pairs):
            raise ValueError("门禁候选版本不一致")
        if any(not p.get("experiment_version") or p.get("experiment_version") != pairs[0]["experiment_version"] for p in pairs):
            raise ValueError("配对证据必须来自同一接入程序和实验规则版本")
        if any(type(p[k]) is not bool for p in pairs for k in ("before", "after")):
            raise ValueError("SAGE 使用二元评分")
        wins = sum(not p["before"] and p["after"] for p in pairs)
        losses = sum(p["before"] and not p["after"] for p in pairs)
        solved = sum(p["before"] for p in pairs)
        p_value = binomtest(wins, wins + losses, penalty / (1 + penalty), alternative="greater").pvalue if wins + losses else 1.0
        regression = losses / solved if solved else 0.0
        accepted = wins - penalty * losses > 0 and p_value < alpha and regression <= regression_cap
        evidence = {"wins": wins, "losses": losses, "p_value": p_value, "alpha": alpha,
                    "penalty": penalty, "regression_cap": regression_cap,
                    "decision": "accepted" if accepted else "abstained", "pairs": pairs}
        record.update(status=evidence["decision"], evidence=evidence)
        (self.root / unit_id / "unit.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return evidence


    def admit(self, unit_id, source_pair, pairs, contracts, alpha=0.05, penalty=1.0):
        """阶段一准入：源任务由败转胜，且影响面内没有确认退化。

        输掉的配对必须先重跑一次（confirm_after），重跑仍输才算确认退化。
        退化率上限不参与准入；原版 SAGE 检验放在阶段末（decide_stage）。
        """
        if not 0 < alpha <= 1 or penalty < 1:
            raise ValueError("SAGE 参数超出协议范围")
        if len({p["task"] for p in pairs}) != len(pairs):
            raise ValueError("准入需要互不重复的配对任务")
        record = self.read(unit_id)
        affected = set(affected_tasks(record, contracts)) - {record["source_task"]}
        if not pairs and affected:
            raise ValueError("影响面内还有任务，准入必须给出配对")
        if len(pairs) > 8 or any(p["task"] not in affected for p in pairs):
            raise ValueError("准入最多使用 8 道独立且受作用域影响的任务")
        versions = [fingerprint([{k: v for k, v in u.items() if k not in ("status", "evidence")}
                                 for u in self.units(candidate)]) for candidate in (None, unit_id)]
        if any(p["before_version"] != versions[0] or p["after_version"] != versions[1] for p in pairs):
            raise ValueError("配对证据与当前知识库和候选源码版本不一致")
        if any(not p.get("experiment_version") or p.get("experiment_version") != pairs[0]["experiment_version"] for p in pairs):
            raise ValueError("配对证据必须来自同一接入程序和实验规则版本")
        if any(type(p[k]) is not bool for p in pairs for k in ("before", "after")):
            raise ValueError("准入使用二元评分")
        losses = [p for p in pairs if p["before"] and not p["after"]]
        unconfirmed = [p["task"] for p in losses if type(p.get("confirm_after")) is not bool]
        if unconfirmed:
            raise ValueError("存在未确认的配对，先重跑这些任务：" + ", ".join(unconfirmed))
        regressions = [p["task"] for p in losses if not p["confirm_after"]]
        reasons = []
        if source_pair["before"] or not source_pair["after"]:
            reasons.append("source_task_not_flipped")
        if regressions:
            reasons.append("confirmed_regressions:" + ",".join(regressions))
        retired = [] if reasons else [name for name in record.get("replaces", [])
                                     if self.read(name)["status"] == "accepted"]
        evidence = {"kind": "admission", "wins": sum(not p["before"] and p["after"] for p in pairs),
                    "losses": len(losses), "confirmed_regressions": regressions,
                    "source_pair": source_pair, "decision": "accepted" if not reasons else "abstained",
                    "reasons": reasons, "retired": retired, "pairs": pairs}
        record.update(status=evidence["decision"], evidence=evidence)
        save_json(self.root / unit_id / "unit.json", record)
        self.complete_admission(unit_id)
        return evidence

    def complete_admission(self, unit_id):
        record = self.read(unit_id)
        if record["status"] == "accepted" and record.get("evidence", {}).get("kind") == "admission":
            for name in record["evidence"]["retired"]:
                old = self.read(name)
                if old["status"] != "retracted":
                    self.retire(name, "被 " + unit_id + " 取代")

    def decide_stage(self, pairs, alpha=0.05, penalty=1.0, regression_cap=0.0):
        """阶段末：对本阶段新增单元的配对整体做原版 SAGE 二元检验。

        单题结果以确认后的结果为准，与准入口径一致。
        """
        if not 0 < alpha <= 1 or penalty < 1 or not 0 <= regression_cap <= 1:
            raise ValueError("SAGE 参数超出协议范围")
        if not pairs or len({p["task"] for p in pairs}) != len(pairs):
            raise ValueError("阶段检验每题只保留一个配对")
        if any(type(p["before"]) is not bool or type(p.get("confirm_after", p["after"])) is not bool for p in pairs):
            raise ValueError("阶段检验使用二元评分")
        wins = [p for p in pairs if not p["before"] and p.get("confirm_after", p["after"])]
        losses = [p for p in pairs if p["before"] and not p.get("confirm_after", p["after"])]
        solved = sum(p["before"] for p in pairs)
        p_value = binomtest(len(wins), len(wins) + len(losses), penalty / (1 + penalty), alternative="greater").pvalue if wins or losses else 1.0
        regression = len(losses) / solved if solved else 0.0
        accepted = len(wins) - penalty * len(losses) > 0 and p_value < alpha and regression <= regression_cap
        return {"kind": "stage_gate", "units": sorted({p["unit"] for p in pairs}), "wins": len(wins),
                "losses": len(losses), "p_value": p_value, "alpha": alpha, "penalty": penalty,
                "regression_cap": regression_cap,
                "confirmed_regressions": [p["task"] for p in losses if p.get("confirmed_regression") is True],
                "decision": "accepted" if accepted else "abstained", "pairs": pairs}

    def retire(self, unit_id, reason):
        record = self.read(unit_id)
        record.update(status="retracted", retracted_reason=reason)
        save_json(self.root / unit_id / "unit.json", record)
        return unit_id


def affected_tasks(unit, contracts):
    # 历史上未触达不能推出永不触达；服务白名单才提供零影响的确定边界。
    return [task for task, services in contracts.items()
            if "GLOBAL" in unit["scope"] or any(s.split(":", 1)[0] in services for s in unit["scope"])]


def append_record(path, record):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        stream.flush()
