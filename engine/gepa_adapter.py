import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from gepa.core.adapter import EvaluationBatch
from gepa.strategies.instruction_proposal import InstructionProposalSignature

from engine.agent_runtime import load_candidate, normalize_component_text, write_manifest
from engine.component_host import ComponentHost
from engine.gnosis_lm import GnosisLM
from engine.guard_hardening import GUARD_HARDENING_VERSION

os.environ.setdefault("APPWORLD_ROOT", "/home/jonahwu/appworld_root")

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ("W", "T", "C", "S")
INTERFACE_VERSION = "2"
# 步骤摘要里的截断长度；单条记录总量由 max_record_chars 再兜一层
AGENT_EXCERPT_CHARS = 1200
OUTPUT_EXCERPT_CHARS = 800
INSTRUCTION_MAX_CHARS = 2000
# proposals.jsonl 里原始输出的截断长度：W 模块全文超 11k，截断太短会丢掉排错所需的信息
PROPOSAL_RAW_MAX_CHARS = 20000
# 种子缓存复用来源实验的评分文件标签，与 stage2 基线运行时的 --dataset train 对应
SOURCE_DATASET_LABEL = "train"
LEDGER_FIELDS = (
    "task_cost_usd",
    "reflection_cost_usd",
    "real_task_runs",
    "evaluate_calls",
    "cache_hits",
    "num_proposals",
)


class InfraFailure(RuntimeError):
    """基础设施失败（无 finished 标记或分片进程异常退出）；候选自身失败不算。"""

    def __init__(self, task_ids: list[str], reason: str):
        self.task_ids = list(task_ids)
        self.reason = reason
        super().__init__(f"{reason}；涉及 {len(self.task_ids)} 个任务：{', '.join(self.task_ids)}")


def candidate_hash(candidate: dict) -> str:
    payload = json.dumps(sorted(candidate.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def build_reflection_lm(config: dict) -> GnosisLM:
    # 与 run_experiment.build_agent 共用同一套 rate_card 与采样五参；
    # 提议要输出完整 TS 模块（W 基线就有 11k 字符），max_tokens 走 reflection 节的更大上限
    return GnosisLM(
        rate_card=config["rate_card"],
        max_tokens=config.get("reflection", {}).get(
            "max_tokens", config["sampling"]["max_tokens"]
        ),
        temperature=config["sampling"]["temperature"],
        seed=config["sampling"]["seed"],
        max_retries=config["sampling"]["max_retries"],
        retry_after_n_seconds=config["sampling"]["retry_after_n_seconds"],
    )


def _slim_tests(items: list[dict]) -> list[dict]:
    # 官方 failures 的 trace 字段可达数百 KB，缓存只留 requirement 与 label
    return [{"requirement": item["requirement"], "label": item["label"]} for item in items]


def build_cache_entry(
    task_id: str,
    success: bool,
    num_tests: int | None,
    passes: list[dict],
    failures: list[dict],
    steps: int,
    usage: dict,
    experiment: str,
    misc_dir: str,
    fingerprint: str,
) -> dict:
    return {
        "task_id": task_id,
        "success": bool(success),
        "num_tests": num_tests,
        "passes": _slim_tests(passes),
        "failures": _slim_tests(failures),
        "steps": steps,
        "usage": usage,
        "experiment": experiment,
        "misc_dir": misc_dir,
        "fingerprint": fingerprint,
    }


def write_cache_entry(cache_dir: Path, entry: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    with open(cache_dir / f"{entry['task_id']}.json", "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=1)


def read_cache_entry(cache_dir: Path, task_id: str) -> dict | None:
    path = cache_dir / f"{task_id}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def prefill_seed_cache(
    seed_hash: str,
    task_ids: list[str],
    prefill_config: dict,
    score_cache_dir: Path,
    fingerprint: str,
) -> tuple[int, int]:
    """把与种子配置完全一致的历史测量搬进磁盘缓存，符合科研计划 §5.5 的复用条件。

    分数与用量取自 source_summary，逐测试项明细与 misc_dir 取自来源实验的输出目录。
    返回 (预填前已有条目数, 本次写入条目数)。
    """
    cache_dir = score_cache_dir / seed_hash
    existing = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0
    if existing >= len(task_ids):
        return existing, 0
    source_summary = json.loads(
        (REPO_ROOT / prefill_config["source_summary"]).read_text(encoding="utf-8")
    )
    source_experiment = prefill_config["source_experiment"]
    outputs_dir = Path(os.environ["APPWORLD_ROOT"]) / "experiments" / "outputs" / source_experiment
    details = json.loads(
        (outputs_dir / "evaluations" / f"{SOURCE_DATASET_LABEL}.json").read_text(encoding="utf-8")
    )["individual"]
    records = {record["task_id"]: record for record in source_summary["records"]}
    written = 0
    for task_id in task_ids:
        if (cache_dir / f"{task_id}.json").exists():
            continue
        record = records.get(task_id)
        detail = details.get(task_id)
        if record is None or detail is None or record["infrastructure_failed"]:
            continue
        entry = build_cache_entry(
            task_id=task_id,
            success=record["success"],
            num_tests=record["num_tests"],
            passes=detail.get("passes", []),
            failures=detail.get("failures", []),
            steps=record["steps"],
            usage=record["usage"],
            experiment=source_experiment,
            misc_dir=str(outputs_dir / "tasks" / task_id / "misc"),
            fingerprint=fingerprint,
        )
        write_cache_entry(cache_dir, entry)
        written += 1
    return existing, written


def extract_proposed_source(raw_output: str) -> str:
    """从反思模型输出里取组件源码。

    W 的源码自身含几十个 ``` 围栏，GEPA 默认 output_extractor 的首尾截取在多段输出
    时会把解释文本一起吃进来；改为取最长的围栏块，无围栏时退化为全文。
    NO_CHANGE 哨兵的判定在调用方。
    """
    blocks = re.findall(r"```[^\n]*\n(.*?)```", raw_output, flags=re.DOTALL)
    if not blocks:
        return raw_output.strip()
    return max(blocks, key=len).strip("\n")


class ReflectionRecords:
    """轨迹 → 反思记录。GEPA 主流程与 engine/propose.py 共用同一套渲染。"""

    def __init__(self, max_record_chars: int = 12000):
        self.max_record_chars = max_record_chars

    def build(
        self, component: str, trajectory: dict, score: float, extra_inputs: dict | None = None
    ) -> dict:
        misc_dir = trajectory["misc_dir"]
        instruction = self._task_instruction(misc_dir) if misc_dir else ""
        steps_text, execution_errors = (
            self._read_agent_log(misc_dir) if misc_dir else ("", [])
        )
        feedback_parts = []
        if trajectory["success"]:
            feedback_parts.append("task succeeded")
        else:
            feedback_parts.append(
                f"official evaluation failed: {len(trajectory['failures'])} tests failed"
            )
        for failure in trajectory["failures"]:
            feedback_parts.append(f"- [{failure['label']}] {failure['requirement']}")
        # 结构校验失败等没有官方 failures 的场景，把真实错误原因交给反思模型
        if not trajectory["failures"] and trajectory.get("error"):
            feedback_parts.append("Error: " + trajectory["error"])
        for execution_error in execution_errors:
            feedback_parts.append("Execution error (raw):\n" + execution_error)
        inputs = {"Task Instruction": instruction, "Component Name": component}
        for key, value in (extra_inputs or {}).items():
            inputs[key] = value
        record = {
            "Inputs": inputs,
            "Generated Outputs": steps_text,
            "Feedback": "\n".join(feedback_parts),
            "score": score,
        }
        return self._fit_record(record)

    def _task_instruction(self, misc_dir: str) -> str:
        # agent_log 不含 user 消息，任务指令取首次 LM 请求里的首条 user 消息
        with open(Path(misc_dir) / "lm_calls" / "0001.json", encoding="utf-8") as f:
            record = json.load(f)
        user_content = next(
            message["content"]
            for message in record["request"]["messages"]
            if message["role"] == "user"
        )
        index = user_content.rfind("Task: ")
        if index == -1:
            return self._truncate(user_content, INSTRUCTION_MAX_CHARS)
        return user_content[index + len("Task: ") :].split("\n", 1)[0].strip()[
            :INSTRUCTION_MAX_CHARS
        ]

    def _read_agent_log(self, misc_dir: str) -> tuple[str, list[str]]:
        steps: list[str] = []
        execution_errors: list[str] = []
        with open(Path(misc_dir) / "agent_log.jsonl", encoding="utf-8") as f:
            for line in f:
                entry = json.loads(line)
                if entry["role"] == "agent":
                    steps.append(
                        f"STEP {entry['step']}\n"
                        f"{self._truncate(entry['content'], AGENT_EXCERPT_CHARS)}"
                    )
                elif entry["role"] == "environment":
                    content = entry["content"]
                    steps.append(f"Output:\n{self._truncate(content, OUTPUT_EXCERPT_CHARS)}")
                    # appworld 执行器与沙箱护栏的报错原文，原样保留
                    if content.startswith("Execution failed"):
                        execution_errors.append(content)
        return "\n\n".join(steps), execution_errors

    def _fit_record(self, record: dict) -> dict:
        for _ in range(32):
            if len(json.dumps(record, ensure_ascii=False)) <= self.max_record_chars:
                break
            fields = [
                (record["Inputs"], "Task Instruction"),
                (record, "Generated Outputs"),
                (record, "Feedback"),
            ]
            target, key = max(fields, key=lambda item: len(item[0][item[1]]))
            value = target[key]
            if len(value) <= 300:
                break
            target[key] = self._truncate(value, len(value) // 2)
        return record

    def _truncate(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        head = limit * 3 // 5
        tail = limit - head
        return text[:head] + "\n[TRUNCATED]\n" + text[-tail:]


class GnosisAppWorldAdapter:
    """GEPA 与 AppWorld 之间的适配层：四组件候选 → 候选目录 → 真实执行 → 官方分数。

    evaluate 在主线程串行被调，本类内部用 subprocess 分片并行跑任务；
    单个任务的失败（候选自身没做对）记 0 分，基础设施失败才抛异常。
    """

    def __init__(
        self,
        config_path: str,
        experiment_prefix: str,
        num_processes: int = 4,
        seed: int = 100,
        max_record_chars: int = 12000,
    ):
        self.config_path = config_path
        self.experiment_prefix = experiment_prefix
        self.num_processes = num_processes
        self.seed = seed
        self.max_record_chars = max_record_chars
        with open(REPO_ROOT / config_path, encoding="utf-8") as f:
            self.config = json.load(f)
        for section in ("rate_card", "sampling", "agent", "appworld_config", "usage_caps", "host"):
            if section not in self.config:
                raise ValueError(f"config 缺少 {section} 节：{config_path}")
        if "stage_root" not in self.config:
            raise ValueError(f"config 缺少 stage_root：{config_path}")
        self.stage_root = REPO_ROOT / self.config["stage_root"]
        self.candidates_dir = self.stage_root / "gepa_candidates"
        self.score_cache_dir = self.stage_root / "score_cache"
        self.proposals_path = self.stage_root / "proposals.jsonl"
        # 账本按 experiment_prefix 分开：smoke 与正式搜索同 stage_root 时互不污染
        self.ledger_path = self.stage_root / f"cost_ledger_{experiment_prefix}.json"
        self.component_host = ComponentHost(
            str(REPO_ROOT / self.config["host"]["host_dir"]), self.config["host"]["node_bin_dir"]
        )
        self.records = ReflectionRecords(max_record_chars)
        self.reflection_templates = self.config["gepa"].get("reflection_prompt_templates") or {}
        for name, template in self.reflection_templates.items():
            InstructionProposalSignature.validate_prompt_template(template)
        self.reflection_lm: GnosisLM | None = None
        self._fingerprint: str | None = None
        self.ledger = {
            "task_cost_usd": 0.0,
            "reflection_cost_usd": 0.0,
            "real_task_runs": 0,
            "evaluate_calls": 0,
            "cache_hits": 0,
            "num_proposals": 0,
            "infra_failures": [],
        }
        self._load_ledger()
        # GEPA 读 adapter.propose_new_texts 属性，绑定后即取代默认提议实现
        self.propose_new_texts = self._propose_new_texts

    def attach_reflection_lm(self, reflection_lm: GnosisLM) -> None:
        """提议与费用计数必须用同一个 LM 实例，否则 MaxReflectionCostStopper 读不到真实成本。"""
        self.reflection_lm = reflection_lm

    # ---------------- 账本 ----------------

    def _load_ledger(self) -> None:
        if not self.ledger_path.exists():
            return
        stored = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        for field in LEDGER_FIELDS:
            if field in stored:
                self.ledger[field] = max(self.ledger[field], stored[field])
        for failure in stored.get("infra_failures") or []:
            if failure not in self.ledger["infra_failures"]:
                self.ledger["infra_failures"].append(failure)

    def flush_ledger(self, reflection_cost_usd: float | None = None) -> None:
        if reflection_cost_usd is not None:
            self.ledger["reflection_cost_usd"] = reflection_cost_usd
        payload = {field: self.ledger[field] for field in LEDGER_FIELDS}
        payload["infra_failures"] = list(self.ledger["infra_failures"])
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)

    # ---------------- GEPA 可选状态持久化 ----------------

    def get_adapter_state(self) -> dict:
        return {**self.ledger, "infra_failures": list(self.ledger["infra_failures"])}

    def set_adapter_state(self, state: dict) -> None:
        if not state:
            return
        # 检查点可能比磁盘账本旧，数值字段取大者，避免回滚已花的费用
        for field in LEDGER_FIELDS:
            if field in state:
                self.ledger[field] = max(self.ledger[field], state[field])
        for failure in state.get("infra_failures") or []:
            if failure not in self.ledger["infra_failures"]:
                self.ledger["infra_failures"].append(failure)

    # ---------------- 协议指纹 ----------------

    def protocol_fingerprint(self) -> str:
        if self._fingerprint is None:
            sampling = {
                key: self.config["sampling"][key]
                for key in ("temperature", "seed", "max_tokens", "max_retries", "retry_after_n_seconds")
            }
            payload = {
                "model_id": self.config["model"]["id"],
                "reasoning_effort": "high",
                "sampling": sampling,
                "interface_version": INTERFACE_VERSION,
                "appworld_version": self.config["tasks"]["appworld_version"],
                "task_list_sha256": self.config["tasks"]["list_sha256"],
                "max_steps": self.config["agent"]["max_steps"],
                "appworld_config": hashlib.sha256(
                    json.dumps(self.config["appworld_config"], sort_keys=True).encode("utf-8")
                ).hexdigest()[:16],
                "guard_hardening_version": GUARD_HARDENING_VERSION,
                "usage_caps": self.config["usage_caps"],
            }
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            self._fingerprint = digest[:16]
        return self._fingerprint

    # ---------------- 候选物化与结构校验 ----------------

    def materialize(self, candidate: dict) -> str:
        digest = candidate_hash(candidate)
        root = self.candidates_dir / digest
        if (root / "manifest.json").exists():
            try:
                existing = load_candidate(str(root))
            except (ValueError, FileNotFoundError):
                existing = None
            if existing is not None and existing.interface_version == INTERFACE_VERSION:
                return os.path.relpath(root, REPO_ROOT).replace(os.sep, "/")
        for component in COMPONENTS:
            (root / component).mkdir(parents=True, exist_ok=True)
        (root / "W" / "workflow.ts").write_text(candidate["W"], encoding="utf-8")
        (root / "T" / "wrapper.ts").write_text(candidate["T"], encoding="utf-8")
        (root / "C" / "context.ts").write_text(candidate["C"], encoding="utf-8")
        # load_candidate 把 S/ 下每个 .md 装成 "## name" 段供 agent 拼接，故单文件固定命名
        if candidate["S"].strip():
            (root / "S" / "skills.md").write_text(candidate["S"], encoding="utf-8")
        normalized = self.normalized_hashes(candidate)
        (root / "normalized.json").write_text(
            json.dumps(normalized, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        write_manifest(str(root), INTERFACE_VERSION)
        return os.path.relpath(root, REPO_ROOT).replace(os.sep, "/")

    def normalized_hashes(self, candidate: dict) -> dict:
        return {
            "W": self.component_host.normalize("W", candidate["W"]),
            "T": self.component_host.normalize("T", candidate["T"]),
            "C": self.component_host.normalize("C", candidate["C"]),
            "S": normalize_component_text("S", candidate["S"]),
        }

    def validate(self, candidate: dict) -> str | None:
        if not isinstance(candidate, dict):
            return f"候选不是字典：{type(candidate).__name__}"
        for component in COMPONENTS:
            if component not in candidate:
                return f"候选缺少组件 {component}"
            if not isinstance(candidate[component], str):
                return f"组件 {component} 不是字符串：{type(candidate[component]).__name__}"
        result = self.component_host.validate(
            {component: candidate[component] for component in ("W", "T", "C")}
        )
        if not result.get("ok"):
            errors = result.get("errors") or ["宿主校验失败但未给出错误详情"]
            return "；".join(errors)
        return None

    # ---------------- 提议 ----------------

    def _propose_new_texts(
        self, candidate: dict, reflective_dataset, components_to_update: list[str]
    ) -> dict[str, str]:
        if self.reflection_lm is None:
            raise RuntimeError("propose 需要 reflection_lm：先调 adapter.attach_reflection_lm")
        proposed: dict[str, str] = {}
        for component in components_to_update:
            records = list(reflective_dataset.get(component) or [])
            if not records:
                # 与 GEPA 默认实现一致：该组件没有反思数据就跳过，不发起请求
                continue
            template = self.reflection_templates.get(component)
            if template is None:
                raise ValueError(f"reflection_prompt_templates 缺少组件 {component} 的模板")
            prompt = InstructionProposalSignature.prompt_renderer(
                {
                    "current_instruction_doc": candidate[component],
                    "dataset_with_feedback": records,
                    "prompt_template": template,
                }
            )
            raw_output = self.reflection_lm(prompt)
            new_text = extract_proposed_source(raw_output)
            original = candidate[component]
            validate_ok = None
            validate_errors: list[str] = []
            if new_text.strip() == "NO_CHANGE":
                outcome = "no_change"
                new_text = original
            else:
                validate_ok, validate_errors = self._validate_source(component, new_text)
                if validate_ok and self._same_normalized(component, new_text, original):
                    outcome = "noop_equal"
                    new_text = original
                else:
                    # 校验不过的提议也放行：GEPA 会经 evaluate → validate 记 0 分
                    outcome = "proposed"
            self._record_proposal(
                candidate, component, outcome, raw_output, validate_ok, validate_errors
            )
            proposed[component] = new_text
        return proposed

    def _validate_source(self, component: str, text: str) -> tuple[bool, list[str]]:
        if component == "S":
            return True, []
        result = self.component_host.validate({component: text})
        return bool(result.get("ok")), list(result.get("errors") or [])

    def _same_normalized(self, component: str, left: str, right: str) -> bool:
        if component == "S":
            return normalize_component_text("S", left) == normalize_component_text("S", right)
        return self.component_host.normalize(component, left) == self.component_host.normalize(
            component, right
        )

    def _record_proposal(
        self,
        candidate: dict,
        component: str,
        outcome: str,
        raw_output: str,
        validate_ok,
        validate_errors: list[str] | None = None,
    ) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "parent_hash": candidate_hash(candidate),
            "component": component,
            "outcome": outcome,
            "validate_ok": validate_ok,
            "validate_errors": validate_errors or [],
            "raw_output": raw_output[:PROPOSAL_RAW_MAX_CHARS],
        }
        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.proposals_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.ledger["num_proposals"] += 1

    def recent_proposals(self, parent_hash: str, limit: int = 3) -> list[dict]:
        if not self.proposals_path.exists():
            return []
        matched = []
        with open(self.proposals_path, encoding="utf-8") as f:
            for line in f:
                record = json.loads(line)
                if record["parent_hash"] == parent_hash:
                    matched.append(
                        {
                            "component": record["component"],
                            "outcome": record["outcome"],
                            "validate_ok": record["validate_ok"],
                        }
                    )
        return matched[-limit:]

    # ---------------- 评测 ----------------

    def evaluate(
        self, batch: list[dict], candidate: dict, capture_traces: bool = False
    ) -> EvaluationBatch:
        self.ledger["evaluate_calls"] += 1
        task_ids = [item["task_id"] for item in batch]
        error = self.validate(candidate)
        if error is not None:
            # 结构不通过：整批 0 分，不物化、不跑任务、不计费
            return EvaluationBatch(
                outputs=[{"task_id": task_id, "error": error} for task_id in task_ids],
                scores=[0.0] * len(task_ids),
                trajectories=[
                    self._failure_trajectory(task_id, error) for task_id in task_ids
                ],
                num_metric_calls=0,
            )
        candidate_dir = self.materialize(candidate)
        digest = candidate_hash(candidate)
        experiment = f"{self.experiment_prefix}_{digest}"
        cache_dir = self.score_cache_dir / digest
        fingerprint = self.protocol_fingerprint()
        entries: dict[str, dict] = {}
        missing: list[str] = []
        # GEPA 的 epoch_shuffled 采样器可能在一个 minibatch 里放重复任务；去重后再跑，
        # 避免同一任务被两个分片并发执行
        for task_id in task_ids:
            if task_id in entries or task_id in missing:
                continue
            entry = read_cache_entry(cache_dir, task_id)
            if entry is None or entry.get("fingerprint") != fingerprint:
                missing.append(task_id)
            else:
                entries[task_id] = entry
        self.ledger["cache_hits"] += len(entries)

        if missing:
            self._evaluate_missing(candidate_dir, experiment, missing, cache_dir, entries)

        outputs, scores, trajectories = [], [], []
        for task_id in task_ids:
            entry = entries[task_id]
            success = bool(entry["success"])
            outputs.append(
                {
                    "task_id": task_id,
                    "success": success,
                    "num_tests": entry["num_tests"],
                    "steps": entry["steps"],
                    "cost": entry["usage"].get("cost", 0.0),
                }
            )
            scores.append(1.0 if success else 0.0)
            trajectories.append(
                {
                    "task_id": task_id,
                    "success": success,
                    "misc_dir": entry["misc_dir"],
                    "failures": entry["failures"],
                    "steps": entry["steps"],
                    "error": (
                        None
                        if success
                        else f"官方评测未通过：{len(entry['failures'])} 项测试失败"
                    ),
                }
            )
        return EvaluationBatch(
            outputs=outputs,
            scores=scores,
            trajectories=trajectories,
            num_metric_calls=len(missing),
        )

    def _failure_trajectory(self, task_id: str, error: str) -> dict:
        return {
            "task_id": task_id,
            "success": False,
            "misc_dir": None,
            "failures": [],
            "steps": 0,
            "error": error,
        }

    def _evaluate_missing(
        self,
        candidate_dir: str,
        experiment: str,
        missing: list[str],
        cache_dir: Path,
        entries: dict[str, dict],
    ) -> None:
        returncodes = self._run_shards(candidate_dir, experiment, missing)
        evaluate_code = self._run_evaluate_only(candidate_dir, experiment, missing)
        if evaluate_code != 0:
            raise InfraFailure(
                missing,
                f"--evaluate-only 退出码 {evaluate_code}，本批拿不到官方分数",
            )
        summary_path = self._summary_path(experiment)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        records = {record["task_id"]: record for record in summary["records"]}
        details = self._read_evaluation_details(experiment)
        outputs_dir = self._outputs_dir(experiment)
        fingerprint = self.protocol_fingerprint()
        infra: list[str] = []
        for task_id in missing:
            record = records.get(task_id)
            if record is None:
                raise RuntimeError(f"汇总缺少任务 {task_id} 的记录：{summary_path}")
            self.ledger["task_cost_usd"] += record["usage"].get("cost", 0.0)
            finished = (outputs_dir / "tasks" / task_id / "misc" / "finished").exists()
            if record["infrastructure_failed"] or not finished:
                infra.append(task_id)
                continue
            entry = build_cache_entry(
                task_id=task_id,
                success=record["success"],
                num_tests=record["num_tests"],
                passes=details.get(task_id, {}).get("passes", []),
                failures=details.get(task_id, {}).get("failures", []),
                steps=record["steps"],
                usage=record["usage"],
                experiment=experiment,
                misc_dir=str(outputs_dir / "tasks" / task_id / "misc"),
                fingerprint=fingerprint,
            )
            write_cache_entry(cache_dir, entry)
            entries[task_id] = entry
        self.ledger["real_task_runs"] += len(missing)
        self.flush_ledger()
        if infra:
            self.ledger["infra_failures"].append(
                {"task_ids": infra, "returncodes": returncodes, "experiment": experiment}
            )
            raise InfraFailure(infra, f"任务无 finished 标记（分片退出码 {returncodes}）")
        if any(code != 0 for code in returncodes):
            raise InfraFailure(
                missing,
                f"分片子进程退出码 {returncodes}，本批结果不完整",
            )

    def _run_shards(self, candidate_dir: str, experiment: str, task_ids: list[str]) -> list[int]:
        num_shards = min(self.num_processes, len(task_ids))
        processes = []
        for index in range(num_shards):
            command = [
                sys.executable,
                "engine/run_experiment.py",
                "--candidate",
                candidate_dir,
                "--experiment",
                experiment,
                "--task-ids",
                *task_ids,
                "--num-processes",
                str(num_shards),
                "--process-index",
                str(index),
                "--config",
                self.config_path,
            ]
            print(f"[adapter] shard {index + 1}/{num_shards}: {' '.join(command)}", flush=True)
            processes.append(subprocess.Popen(command, cwd=str(REPO_ROOT)))
        return [process.wait() for process in processes]

    def _run_evaluate_only(self, candidate_dir: str, experiment: str, task_ids: list[str]) -> int:
        command = [
            sys.executable,
            "engine/run_experiment.py",
            "--candidate",
            candidate_dir,
            "--experiment",
            experiment,
            "--task-ids",
            *task_ids,
            "--config",
            self.config_path,
            "--evaluate-only",
        ]
        print(f"[adapter] evaluate: {' '.join(command)}", flush=True)
        return subprocess.call(command, cwd=str(REPO_ROOT))

    def _summary_path(self, experiment: str) -> Path:
        return REPO_ROOT / Path(self.config_path).parent / f"{experiment}_summary.json"

    def _outputs_dir(self, experiment: str) -> Path:
        return Path(os.environ["APPWORLD_ROOT"]) / "experiments" / "outputs" / experiment

    def _read_evaluation_details(self, experiment: str) -> dict:
        # --task-ids 模式下官方评分文件的 label 是 subset，且每次 --evaluate-only 会覆盖
        path = self._outputs_dir(experiment) / "evaluations" / "subset.json"
        return json.loads(path.read_text(encoding="utf-8"))["individual"]

    # ---------------- 反思数据集 ----------------

    def make_reflective_dataset(
        self, candidate: dict, eval_batch: EvaluationBatch, components_to_update: list[str]
    ) -> dict[str, list[dict]]:
        trajectories = eval_batch.trajectories or []
        scores = eval_batch.scores
        parent_hash = candidate_hash(candidate)
        successes = [
            trajectory["task_id"]
            for index, trajectory in enumerate(trajectories)
            if index < len(scores) and scores[index] >= 1.0
        ]
        extra_inputs = {
            "Candidate Hash": parent_hash,
            "Allowed Components": list(components_to_update),
            "Successes To Preserve": successes,
            "Recent Proposals": self.recent_proposals(parent_hash),
        }
        dataset = {}
        for component in components_to_update:
            records = []
            for index, trajectory in enumerate(trajectories):
                score = scores[index] if index < len(scores) else 0.0
                records.append(self.records.build(component, trajectory, score, extra_inputs))
            dataset[component] = records
        return dataset

    def close(self) -> None:
        self.component_host.close()
