import atexit
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import TimeoutError as FutureTimeoutError

from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.exceptions import JsonRpcException
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

METHOD_COMPONENT = {
    "w.template": "W",
    "w.beforeStep": "W",
    "t.formatObservation": "T",
    "c.buildContext": "C",
    "e.onBoundary": "E",
}


class CandidateError(Exception):
    """候选代码真实失败：该任务记 0 分，不是基础设施故障。"""

    def __init__(self, candidate_id: str, component: str, message: str, method: str | None = None):
        self.candidate_id = candidate_id
        self.component = component
        self.message = message
        self.method = method
        super().__init__(f"[{candidate_id}/{component}] {method or ''} {message}".strip())


class HostError(RuntimeError):
    """桥接/协议/装载失败：基础设施故障，按批次中断处理。"""


class ComponentHost:
    """Python 侧的宿主桥：spawn `tsx src/main.ts`，经 stdio JSON-RPC 调组件。

    每次 request 生成一条契约 §7 的记录：有 sink 就落盘，没有就留在 recent_calls 里。
    """

    @classmethod
    def from_config(cls, config: dict) -> "ComponentHost":
        """从实验配置的 host 节构造；run_experiment/propose/factorial 共用同一入口。"""
        host_config = config["host"]
        host_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", host_config["host_dir"])
        return cls(os.path.normpath(host_dir), host_config["node_bin_dir"])

    def __init__(self, host_dir: str, node_bin_dir: str):
        self.host_dir = os.path.abspath(host_dir)
        self.node_bin_dir = node_bin_dir
        self.step: int | None = None
        # 无 sink 时的内存尾巴，只留最近若干条，长搜索不会因此涨内存
        self.recent_calls: deque = deque(maxlen=200)
        self._process: subprocess.Popen | None = None
        self._endpoint: Endpoint | None = None
        self._reader: JsonRpcStreamReader | None = None
        self._reader_thread: threading.Thread | None = None
        self._sink = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._last_request_id = ""
        self._closed = False
        atexit.register(self.close)

    # ---------------- 生命周期 ----------------

    def __enter__(self) -> "ComponentHost":
        return self

    def __exit__(self, *_exception) -> None:
        self.close()

    def _ensure_started(self) -> None:
        if self._closed:
            raise HostError("ComponentHost 已关闭，无法再发起请求")
        if self._process is not None:
            if self._process.poll() is not None:
                raise HostError(f"宿主进程已退出，退出码 {self._process.returncode}")
            return
        launcher = os.path.join(self.host_dir, "node_modules", ".bin", "tsx")
        if not os.path.exists(launcher):
            raise HostError(f"宿主启动器不存在：{launcher}")
        env = dict(os.environ)
        env["PATH"] = self.node_bin_dir + os.pathsep + env.get("PATH", "")
        self._process = subprocess.Popen(
            [launcher, "src/main.ts"],
            cwd=self.host_dir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # 宿主 stderr 直接继承，Node 侧的真实报错必须可见，不做静默吞掉
        )
        self._reader = JsonRpcStreamReader(self._process.stdout)
        writer = JsonRpcStreamWriter(self._process.stdin)
        self._endpoint = Endpoint({}, writer.write, id_generator=self._next_id)
        self._reader_thread = threading.Thread(
            target=self._reader.listen,
            args=(self._endpoint.consume,),
            name="gnosis-component-host-reader",
            daemon=True,
        )
        self._reader_thread.start()

    def _next_id(self) -> str:
        self._request_id += 1
        self._last_request_id = f"gnosis-{self._request_id}"
        return self._last_request_id

    def set_log_sink(self, sink) -> None:
        self._sink = sink

    def set_step(self, step: int | None) -> None:
        self.step = step

    def close(self) -> None:
        """shutdown + 进程回收。清理路径不抛异常：宿主已被关掉了，再抛也无人可修。"""
        if self._closed:
            return
        process = self._process
        if process is not None:
            try:
                self.request("shutdown", {}, timeout=10.0)
            except Exception as exception:
                print(f"[component_host] shutdown 失败：{exception}", file=sys.stderr, flush=True)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._closed = True
        if self._endpoint is not None:
            self._endpoint.shutdown()
        for stream in (process.stdin, process.stdout) if process is not None else ():
            try:
                stream.close()
            except OSError:
                pass
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)

    # ---------------- 请求 ----------------

    def request(self, method: str, params: dict, timeout: float = 120.0) -> dict:
        self._ensure_started()
        # AppWorld 会冻结进程时钟，时长必须用单调钟，墙钟只用于 ts 字段
        started = time.monotonic()
        result = None
        error = None
        try:
            with self._lock:
                future = self._endpoint.request(method, params)
                request_id = self._last_request_id
                try:
                    result = self._await(future, timeout)
                except FutureTimeoutError:
                    error = f"timeout after {timeout}s"
                    future.cancel()
                    # 宿主不回包时清掉悬空 future，迟到响应由 reader 线程按未知 id 丢弃
                    self._endpoint._server_request_futures.pop(request_id, None)
                    raise HostError(
                        f"宿主请求超时（{timeout}s）：{method}；进程状态 {self._process.poll()}"
                    )
                except JsonRpcException as exception:
                    error = exception.message
                    raise self._map_error(method, params, exception) from exception
                except HostError as exception:
                    error = str(exception)
                    raise
        finally:
            self._record(method, params, result, error, started)
        return result

    def _await(self, future, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FutureTimeoutError()
            try:
                return future.result(timeout=min(remaining, 1.0))
            except FutureTimeoutError:
                # 宿主进程已经没了就立刻失败，不把剩下的超时时间空等完
                if self._process.poll() is not None:
                    raise HostError(
                        f"宿主进程已退出，退出码 {self._process.returncode}"
                    ) from None

    def _map_error(self, method: str, params: dict, exception: JsonRpcException) -> Exception:
        data = exception.data if isinstance(exception.data, dict) else {}
        if data.get("candidate_error") is True:
            return CandidateError(
                candidate_id=data.get("candidate_id", params.get("candidate_id", "")),
                component=data.get("component", METHOD_COMPONENT.get(method, "")),
                message=exception.message or "",
                method=method,
            )
        return HostError(f"宿主返回错误（{method}，code={exception.code}）：{exception.message}")

    def _record(self, method: str, params: dict, result, error, started: float) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "candidate_id": params.get("candidate_id"),
            "component": METHOD_COMPONENT.get(method, "host"),
            "method": method,
            "step": self.step,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "ok": error is None,
            "in_bytes": self._bytes_of(params),
            "out_bytes": self._bytes_of(result),
        }
        if error is not None:
            record["error"] = error
        if isinstance(result, dict):
            if "trace" in result:
                record["trace"] = result["trace"]
            if method == "c.buildContext":
                record["in_messages"] = len((params.get("input") or {}).get("messages") or [])
                if "messages" in result:
                    record["out_messages"] = len(result["messages"])
                if "lmRequest" in result:
                    request = result["lmRequest"]
                    record["lm_request"] = {
                        "purpose": request.get("purpose"),
                        "maxTokens": request.get("maxTokens"),
                    }
        self.recent_calls.append(record)
        if self._sink is not None:
            self._sink(record)

    @staticmethod
    def _bytes_of(value) -> int:
        if value is None:
            return 0
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    # ---------------- 契约 §3 方法封装 ----------------

    def load(self, candidate_id: str, files: dict) -> dict:
        return self.request("load", {"candidate_id": candidate_id, "files": files})

    def unload(self, candidate_id: str) -> None:
        self.request("unload", {"candidate_id": candidate_id})

    def validate(self, sources: dict) -> dict:
        return self.request("validate", {"sources": sources})

    def normalize(self, component: str, source: str) -> str:
        return self.request("normalize", {"component": component, "source": source})["sha256"]

    def w_template(self, candidate_id: str) -> str:
        return self.request("w.template", {"candidate_id": candidate_id})["template"]

    def w_before_step(self, candidate_id: str, state: dict) -> dict:
        return self.request("w.beforeStep", {"candidate_id": candidate_id, "state": state})

    def t_format_observation(self, candidate_id: str, raw: str, step: int, code: str) -> dict:
        return self.request(
            "t.formatObservation",
            {"candidate_id": candidate_id, "raw": raw, "step": step, "code": code},
        )

    def c_build_context(self, candidate_id: str, context_input: dict) -> dict:
        return self.request(
            "c.buildContext", {"candidate_id": candidate_id, "input": context_input}
        )

    def e_on_boundary(self, candidate_id: str, inputs: dict) -> dict:
        return self.request("e.onBoundary", {"candidate_id": candidate_id, "input": inputs})
