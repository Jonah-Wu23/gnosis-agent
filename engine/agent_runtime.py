import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import TypeVar

TItem = TypeVar("TItem")


def chunk_list(
    input_list: list[TItem],
    chunk_size: int | None = None,
    num_chunks: int | None = None,
    balanced: bool = False,
) -> list[list[TItem]]:
    # 逐字搬运自 appworld 仓库 main 的 common/collections.py（pip 0.1.3.post1 尚无此模块）
    if chunk_size is None and num_chunks is None:
        raise ValueError("Either chunk_size or num_chunks must be passed.")
    if chunk_size is not None and num_chunks is not None:
        raise ValueError("Only one of chunk_size or num_chunks can be passed.")
    if chunk_size is not None:
        if not chunk_size > 0:
            raise ValueError(f"Invalid chunk_size: {chunk_size}. Must be > 0.")
        if not chunk_size <= len(input_list):
            raise ValueError(
                f"Invalid chunk_size: {chunk_size}. Must be <= len(input_list): {len(input_list)}."
            )
        result = [
            input_list[index : index + chunk_size]
            for index in range(0, len(input_list), chunk_size)
        ]
    else:
        assert num_chunks is not None
        if not num_chunks > 0:
            raise ValueError(f"Invalid num_chunks: {num_chunks}. Must be > 0.")
        if not num_chunks <= len(input_list):
            raise ValueError(
                f"Invalid num_chunks: {num_chunks}. Must be <= len(input_list): {len(input_list)}."
            )
        if balanced:
            n = len(input_list)
            base, extra = divmod(n, num_chunks)
            result = []
            start = 0
            for i in range(num_chunks):
                size = base + (1 if i < extra else 0)
                end = start + size
                result.append(input_list[start:end])
                start = end
        else:
            chunk_size = len(input_list) // num_chunks
            has_left_out = len(input_list) % num_chunks != 0
            result = [
                input_list[index : index + chunk_size]
                for index in range(0, len(input_list), chunk_size)
            ]
            if has_left_out:
                extra_items = [item for chunk in result[num_chunks:] for item in chunk]
                result_ = result[:num_chunks]
                result_[-1].extend(extra_items)
                result = result_
        assert len(result) == num_chunks
    return result


def chunk_and_return(
    input_list: list[TItem], num_chunks: int | None, chunk_index: int | None, balanced: bool = False
) -> list[TItem]:
    # 逐字搬运自 appworld 仓库 main 的 common/collections.py
    if not input_list:
        return []
    if num_chunks is None or chunk_index is None:
        return input_list
    num_chunks = int(num_chunks)
    chunk_index = int(chunk_index)
    if num_chunks <= 0:
        raise ValueError(f"Invalid num_chunks: {num_chunks}. Must be > 0.")
    if chunk_index < 0 or chunk_index >= num_chunks:
        raise ValueError(
            f"Invalid chunk index: {chunk_index}. Must be between 0 and {num_chunks - 1}."
        )
    chunks = chunk_list(input_list, num_chunks=num_chunks, balanced=balanced)
    return chunks[chunk_index]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0

    def text(self) -> str:
        return (
            f"input={self.input_tokens} (cached={self.cached_input_tokens}) "
            f"output={self.output_tokens} (reasoning={self.reasoning_tokens}) "
            f"cost=${self.cost:.4f}"
        )


class UsageTracker:
    """按任务与全程两档累计真实 usage，并对超上限的任务发出停止信号。"""

    def __init__(
        self,
        max_cost_overall: float | None = None,
        max_cost_per_task: float | None = None,
        max_output_tokens_per_task: int | None = None,
    ):
        self.max_cost_overall = max_cost_overall
        self.max_cost_per_task = max_cost_per_task
        self.max_output_tokens_per_task = max_output_tokens_per_task
        self._per_task: dict[str, Usage] = {}

    def reset(self, task_id: str) -> None:
        self._per_task.setdefault(task_id, Usage())

    def add(self, task_id: str, usage: Usage) -> None:
        t = self._per_task.setdefault(task_id, Usage())
        t.input_tokens += usage.input_tokens
        t.output_tokens += usage.output_tokens
        t.cached_input_tokens += usage.cached_input_tokens
        t.reasoning_tokens += usage.reasoning_tokens
        t.cost += usage.cost

    def exceeded(self, task_id: str) -> bool:
        t = self._per_task.get(task_id, Usage())
        if self.max_cost_per_task is not None and t.cost > self.max_cost_per_task:
            return True
        if (
            self.max_output_tokens_per_task is not None
            and t.output_tokens > self.max_output_tokens_per_task
        ):
            return True
        if self.max_cost_overall is not None and self.overall_usage.cost > self.max_cost_overall:
            return True
        return False

    def task_usage(self, task_id: str) -> Usage:
        return self._per_task.setdefault(task_id, Usage())

    @property
    def overall_usage(self) -> Usage:
        total = Usage()
        for t in self._per_task.values():
            total.input_tokens += t.input_tokens
            total.output_tokens += t.output_tokens
            total.cached_input_tokens += t.cached_input_tokens
            total.reasoning_tokens += t.reasoning_tokens
            total.cost += t.cost
        return total

    def save(self, file_path: str, task_id: str) -> None:
        data = {"task_id": task_id, "task": self._per_task.get(task_id, Usage()).__dict__}
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)


class Logger:
    """最小实现：逐任务写 agent_log.jsonl 到该任务的 misc 目录，并向 stdout 打进度。"""

    def __init__(self, color: bool = False, verbose: bool = True, usage_tracker=None):
        self.verbose = verbose
        self.usage_tracker = usage_tracker
        self.experiment_name = None
        self._log_file = None

    def initialize(
        self,
        experiment_name: str | None = None,
        num_tasks: int = 0,
        num_processes: int = 1,
        process_index: int = 0,
    ) -> None:
        self.experiment_name = experiment_name
        print(
            f"[logger] experiment={experiment_name} tasks={num_tasks} "
            f"process={process_index + 1}/{num_processes}",
            flush=True,
        )

    def start_task(self, world) -> None:
        path = os.path.join(world.output_misc_directory, "agent_log.jsonl")
        self._log_file = open(path, "w", encoding="utf-8")
        print(f"[logger] start task {world.task_id}", flush=True)

    def show_message(
        self,
        role: str,
        content: str,
        step_number: int | None = None,
        reasoning_content: str | None = None,
        raw_message: dict | None = None,
    ) -> None:
        record = {"role": role, "step": step_number, "content": content}
        if reasoning_content:
            record["reasoning_content"] = reasoning_content
        if raw_message is not None:
            record["raw_message"] = raw_message
        if self._log_file is not None:
            self._log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._log_file.flush()
        if self.verbose and role != "environment":
            preview = content.replace("\n", " ")[:120]
            print(f"[step {step_number}] {role}: {preview}", flush=True)

    def complete_task(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None


@dataclass
class Candidate:
    name: str
    root: str
    manifest: dict
    interface_version: str
    # v1 字段（老产物仍可读）：prompt_text 是拼好技能的完整模板文本
    prompt_text: str = ""
    observation_format: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)
    # v2 字段：四组件的源码文本，交给宿主装载
    workflow_source: str = ""
    wrapper_source: str = ""
    context_source: str = ""
    skills_text: str = ""

    @property
    def component_hashes(self) -> dict:
        return self.manifest["files"]

    @property
    def component_files(self) -> dict:
        if self.interface_version == "2":
            return {
                "W": os.path.join(self.root, "W", "workflow.ts"),
                "T": os.path.join(self.root, "T", "wrapper.ts"),
                "C": os.path.join(self.root, "C", "context.ts"),
            }
        return {
            "W": os.path.join(self.root, "W", "instructions.txt"),
            "T": os.path.join(self.root, "T", "observation.json"),
            "C": os.path.join(self.root, "C", "context.json"),
        }

    @property
    def component_sources(self) -> dict:
        """四组件的源码文本，键序与 GEPA 候选字典一致（W/T/C/S）。

        S 取原始 skills.md 文本（不是拼好段头的 skills_text），回写候选目录时才能原样落盘。
        """
        if self.interface_version != "2":
            raise ValueError(f"component_sources 只对 v2 候选有意义：{self.root}")
        sources = {
            "W": self.workflow_source,
            "T": self.wrapper_source,
            "C": self.context_source,
            "S": "",
        }
        skills_path = os.path.join(self.root, "S", "skills.md")
        if os.path.exists(skills_path):
            sources["S"] = _read_text(skills_path)
        return sources


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skills_text(skill_dir: str) -> str:
    """S 目录下每个 .md 装成 `## <name>\\n\\n<正文>`，段间以空行相连。

    v1 把它接到模板尾部时前面再加 `# Skills` 段头；v2 把段头留给 agent 拼接，
    本函数两种形态共用，保证技能段逐字节一致。
    """
    if not os.path.isdir(skill_dir):
        return ""
    names = sorted(n for n in os.listdir(skill_dir) if n.endswith(".md"))
    blocks = []
    for name in names:
        with open(os.path.join(skill_dir, name), encoding="utf-8") as f:
            blocks.append(f"## {name[:-3]}\n\n{f.read().strip()}")
    return "\n\n".join(blocks)


def load_candidate(root: str) -> Candidate:
    """加载候选目录并校验 manifest：每个登记文件的 SHA256 必须与现状一致。"""
    manifest_path = os.path.join(root, "manifest.json")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    files = manifest["files"]
    for rel, expected in files.items():
        actual = _sha256(os.path.join(root, rel))
        if actual != expected:
            raise ValueError(f"候选文件被改动但未更新 manifest：{rel}")
    if manifest["interface_version"] == "2":
        skills_text = _skills_text(os.path.join(root, "S"))
        return Candidate(
            name=os.path.basename(root.rstrip("/\\")),
            root=root,
            manifest=manifest,
            interface_version="2",
            workflow_source=_read_text(os.path.join(root, "W", "workflow.ts")),
            wrapper_source=_read_text(os.path.join(root, "T", "wrapper.ts")),
            context_source=_read_text(os.path.join(root, "C", "context.ts")),
            skills_text=skills_text,
        )
    with open(os.path.join(root, "W", "instructions.txt"), encoding="utf-8") as f:
        prompt_text = f.read().lstrip()
    skills_text = _skills_text(os.path.join(root, "S"))
    if skills_text:
        prompt_text = prompt_text + "\n\n# Skills\n\n" + skills_text
    with open(os.path.join(root, "T", "observation.json"), encoding="utf-8") as f:
        observation_format = json.load(f)
    with open(os.path.join(root, "C", "context.json"), encoding="utf-8") as f:
        context = json.load(f)
    return Candidate(
        name=os.path.basename(root.rstrip("/\\")),
        root=root,
        manifest=manifest,
        interface_version=manifest["interface_version"],
        prompt_text=prompt_text,
        observation_format=observation_format,
        context=context,
        skills_text=skills_text,
    )


def write_manifest(root: str, interface_version: str) -> dict:
    """扫描候选目录，生成 manifest.json（接口版本 + 全文件 SHA256）。"""
    files = {}
    for dirpath, _, filenames in os.walk(root):
        for name in sorted(filenames):
            if name == "manifest.json":
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            files[rel] = _sha256(path)
    manifest = {"interface_version": interface_version, "files": files}
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def normalize_component_text(component: str, value: str) -> str:
    """文本形态组件的归一化哈希。

    S：strip 后 sha256。v1 的 T/C 是 JSON，取运行时实际使用的字段 canonical dump 后
    sha256，键顺序、注释与未使用字段的差异不算有效变异。TS 组件（W/T/C 的 v2 形态）
    的归一化由宿主的 AST 去注释实现，不在这里。
    """
    if component == "S":
        return _sha256_text(value.strip())
    if component in ("T", "C"):
        keys = ("prefix", "suffix", "ensure_trailing_newline")
        if component == "C":
            keys = ("max_prompt_length", "max_output_length")
        parsed = json.loads(value)
        used = {key: parsed[key] for key in keys}
        canonical = json.dumps(used, sort_keys=True, ensure_ascii=False)
        return _sha256_text(canonical)
    raise ValueError(f"组件 {component} 是 TS 组件，归一化必须走宿主 normalize")


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()
