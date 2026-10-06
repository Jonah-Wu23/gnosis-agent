import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from scipy.stats import binomtest

from engine.activation import HOOKS, components_version, validate_scope, validation_plan, reuse_evidence
from engine.scoped_units import POLICY, save_json, append_record, fingerprint, BudgetExceeded


class ActionRejected(ValueError):
    """自主修复操作不符合协议：退回修复器重新选择，不中断实验。"""


class EditableUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    hooks: list[Literal["task_start", "tool_discovery", "before_call", "after_call", "context", "workflow", "operation"]] = Field(min_length=1)
    summary: str = Field(min_length=1)
    content: str = Field(min_length=1)
    component: Literal["T", "C", "W", "E"] | None
    source: str | None

    @field_validator("id")
    @classmethod
    def identifier(cls, value):
        if not value.isidentifier() or not value.isascii():
            raise ValueError("组件 ID 必须是 ASCII 标识符")
        return value

    @field_validator("scope")
    @classmethod
    def scope_contract(cls, value):
        validate_scope(value)
        return value

    @model_validator(mode="after")
    def interface_contract(self):
        if bool(self.component) != bool(self.source):
            raise ValueError("组件类型与非空源码必须同时提供")
        if len(self.hooks) != len(set(self.hooks)):
            raise ValueError("扩展位置禁止重复")
        if self.component != "E" and set(self.hooks) - {"task_start", "after_call"}:
            raise ValueError("自定义扩展位置必须使用 E 组件")
        return self


class EvolutionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["inspect", "edit", "execute", "validate", "stop"]
    reason: str = Field(min_length=1)
    task: str | None = None
    units: list[EditableUnit] | None = None

    @model_validator(mode="after")
    def action_contract(self):
        if self.action in ("execute", "validate", "inspect") and not self.task:
            raise ValueError("此操作需要任务 ID")
        if (self.action == "edit") != (self.units is not None):
            raise ValueError("仅 edit 操作提供整组组件")
        return self


class VersionLibrary:
    def __init__(self, workspace, version):
        self.root = workspace.root / "versions" / version / "units"
        self.output = workspace.root
        self._units = workspace.load_version(version)

    def units(self, candidate=None):
        if candidate is not None:
            raise ValueError("研究版本按整组组件加载")
        return self._units


class EvolutionWorkspace:
    def __init__(self, root, experiment_version):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiment_version = experiment_version
        self.path = self.root / "workspace.json"
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            if self.state["experiment_version"] != experiment_version:
                raise ValueError("工作区属于其他程序版本")
            self.load_version(self.state["working"])
            self.load_version(self.state["released"])
        else:
            empty = self.write_version([])
            self.state = {"experiment_version": experiment_version, "working": empty,
                          "released": empty, "frozen": None, "executions": [], "comparisons": [],
                          "actions": [], "source_successes": {}, "pending_action": None}
            self.save()

    def save(self):
        save_json(self.path, self.state)

    def write_version(self, units):
        parsed = [EditableUnit.model_validate(u).model_dump() for u in units]
        if len({u["id"] for u in parsed}) != len(parsed):
            raise ValueError("整组组件 ID 禁止重复")
        version = components_version(parsed)
        folder = self.root / "versions" / version
        path = folder / "manifest.json"
        if path.exists():
            if self.load_version(version) != parsed:
                raise ValueError("组件版本内容冲突")
            return version
        for unit in parsed:
            unit_folder = folder / "units" / unit["id"]
            unit_folder.mkdir(parents=True, exist_ok=True)
            if unit["source"]:
                (unit_folder / "component.ts").write_text(unit["source"], encoding="utf-8")
            save_json(unit_folder / "unit.json", unit)
        save_json(path, {"version": version, "units": parsed})
        return version

    def load_version(self, version):
        record = json.loads((self.root / "versions" / version / "manifest.json").read_text(encoding="utf-8"))
        units = [EditableUnit.model_validate(u).model_dump() for u in record["units"]]
        if record["version"] != version or components_version(units) != version:
            raise ValueError("组件组版本摘要不匹配")
        for unit in units:
            folder = self.root / "versions" / version / "units" / unit["id"]
            if json.loads((folder / "unit.json").read_text(encoding="utf-8")) != unit:
                raise ValueError("组件记录与冻结版本不一致")
            if unit["source"] and (folder / "component.ts").read_text(encoding="utf-8") != unit["source"]:
                raise ValueError("可编辑源码与冻结版本不一致")
        return units

    def edit(self, units):
        if self.state["frozen"]:
            raise ActionRejected("最终版本已冻结，禁止继续学习")
        version = self.write_version(units)
        self.state["working"] = version
        self.state["source_successes"] = {}
        self.save()
        return version

    def observe(self, record):
        if record["experiment_version"] != self.experiment_version:
            raise ValueError("执行证据程序版本错误")
        self.load_version(record["version"])
        if record["learning_role"] == "heldout":
            raise PermissionError("最终留出证据禁止进入修复工作区")
        if type(record["success"]) is not bool:
            raise ValueError("执行证据必须使用严格二元评分")
        append_record(self.root / "executions.jsonl", record)
        self.state["executions"].append(record["execution_id"])
        self.save()

    def source_succeeds(self, task, version):
        """只有开发来源题完整成功才登记成熟条件，独立选择集的成功不参与。"""
        if version != self.state["working"]:
            raise ValueError("来源成功记录与当前工作版本不一致")
        self.state["source_successes"][task] = version
        self.save()

    def require_mature(self):
        """当前工作版本至少要有一道来源题完整成功，才允许进入独立验证。

        成熟条件只看工作版本，不绑定具体哪一道题：验证用的是互斥的独立选择集任务。
        """
        current = self.state["working"]
        if not any(version == current for version in self.state["source_successes"].values()):
            raise ActionRejected("当前工作版本尚未完整通过来源题，禁止独立验证")

    def publish(self, comparison):
        if comparison["before_version"] != self.state["released"] or comparison["after_version"] != self.state["working"]:
            raise ValueError("配对评估与当前整组版本不一致")
        self.state["comparisons"].append(comparison)
        if comparison["decision"] == "accepted":
            self.state["released"] = self.state["working"]
        self.save()

    def freeze(self):
        if self.state["pending_action"]:
            raise ValueError("存在未完成的自主修复操作")
        self.state["frozen"] = self.state["released"]
        self.save()
        return self.state["frozen"]

    def records(self):
        path = self.root / "executions.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def paired_selection(before, after, tasks, repeats=2):
    if repeats < 1 or not tasks or len(tasks) != len(set(tasks)):
        raise ValueError("配对评估需要固定且互不重复的任务与执行次数")
    versions = [set(r["version"] for r in rows) for rows in (before, after)]
    if any(len(v) != 1 for v in versions):
        raise ValueError("配对评估必须比较两个冻结的整组版本")
    runs = []
    for rows in (before, after):
        if len(rows) != len(tasks) * repeats or {r["task"] for r in rows} != set(tasks):
            raise ValueError("两侧配对执行数量不完整")
        if len({r["execution_id"] for r in rows}) != len(rows):
            raise ValueError("禁止用同一次执行填充复测数量")
        grouped = {}
        for task in tasks:
            task_rows = [r for r in rows if r["task"] == task]
            if len(task_rows) != repeats or {r["repeat"] for r in task_rows} != set(range(repeats)):
                raise ValueError("两侧必须使用相同的预定复测次数")
            if any(type(r["success"]) is not bool for r in task_rows):
                raise ValueError("配对评估必须使用严格二元评分")
            grouped[task] = all(r["success"] for r in task_rows)
        runs.append(grouped)
    if len({r["experiment_version"] for r in [*before, *after]}) != 1:
        raise ValueError("配对评估程序版本不同")
    for task in tasks:
        contracts = {r["task_contract"] for r in [*before, *after] if r["task"] == task}
        if len(contracts) != 1:
            raise ValueError("配对评估任务或评分契约不同")
    wins = sum(not runs[0][task] and runs[1][task] for task in tasks)
    losses = sum(runs[0][task] and not runs[1][task] for task in tasks)
    solved = sum(runs[0].values())
    penalty = POLICY["sage_penalty"]
    p_value = binomtest(wins, wins + losses, penalty / (1 + penalty), alternative="greater").pvalue if wins + losses else 1.0
    accepted = (wins - penalty * losses > 0 and p_value < POLICY["sage_alpha"]
                and (losses / solved if solved else 0) <= POLICY["sage_regression_cap"])
    return {"before_version": next(iter(versions[0])), "after_version": next(iter(versions[1])),
            "experiment_version": before[0]["experiment_version"], "tasks": tasks, "repeats": repeats,
            "wins": wins, "losses": losses, "p_value": p_value,
            "decision": "accepted" if accepted else "abstained"}


class ResearchBudget:
    """研究账本：费用不设上限，只保留实验设计里的组学习额度。

    组额度是对照条件：两个自主修复组在同样的学习与验证花费下比较，GNOSIS 省下的验证费用
    直接变成更多修复次数，这正是要测的量。撤掉组额度，两组可以无限学习，比较失去意义。
    """
    BUCKETS = ("learning", "selection", "evaluation")

    def __init__(self, meter, starting_spent, live_quota=None):
        self.meter = meter
        self.starting_spent = starting_spent
        self.output = meter.output
        self.rates = meter.rates
        self.cost_fluctuation = False

    def used(self, bucket):
        return sum(row["cost_usd"] + row["unsettled_usd"] for row in self.meter.totals()
                   if row["phase"].split("/", 1)[0] == bucket)

    def state(self):
        from engine.research_plan import ARM_LEARNING_LIMITS
        state = self.meter.state()
        state["buckets"] = {bucket: {"used_usd": self.used(bucket)} for bucket in self.BUCKETS}
        state["arm_learning"] = {}
        with self.meter.connect() as db:
            for arm, cap in ARM_LEARNING_LIMITS.items():
                used = self.arm_used(db, arm)
                state["arm_learning"][arm] = {"limit_usd": cap, "used_usd": used, "remaining_usd": cap - used}
        state["observed_task_cost_usd"] = self.observed_task_cost()
        return state

    @staticmethod
    def arm_used(db, arm):
        return db.execute("SELECT COALESCE(SUM(COALESCE(cost,reserved)),0) FROM calls WHERE phase LIKE ? OR phase LIKE ?",
                          ("learning/" + arm + "/%", "selection/" + arm + "/%")).fetchone()[0]

    def observed_task_cost(self):
        with self.meter.connect() as db:
            values = [row[0] for row in db.execute("SELECT SUM(cost) FROM calls "
                       "WHERE phase LIKE '%/execution/%' AND cost IS NOT NULL GROUP BY phase") if row[0] > 0]
        return sum(values) / len(values) if values else 0.0

    def reserve(self, phase, prompt, max_tokens, guard=None, contingency_reason=None):
        bucket = phase.split("/", 1)[0]
        if bucket not in self.BUCKETS:
            raise ValueError("请求未登记预算用途")

        def check(db, amount, reason):
            if bucket in ("learning", "selection"):
                # 组额度按已结算费用判定：最坏预留按 393216 输出 token 折算，用它拦截会让组额度内的请求提前停下。
                from engine.research_plan import ARM_LEARNING_LIMITS
                arm = phase.split("/", 2)[1]
                if self.arm_used(db, arm) >= ARM_LEARNING_LIMITS[arm]:
                    raise BudgetExceeded("实验组学习与验证的合计额度已用完")
            if guard is not None:
                guard(db, amount, reason)
        return self.meter.reserve(phase, prompt, max_tokens, guard=check, contingency_reason=contingency_reason)

    def window_release_seconds(self):
        return self.meter.window_release_seconds()

    def settle(self, *args):
        return self.meter.settle(*args)

    def release(self, *args):
        return self.meter.release(*args)

    def totals(self):
        return self.meter.totals()
