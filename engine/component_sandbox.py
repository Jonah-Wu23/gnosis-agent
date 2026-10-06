import os
import subprocess
import threading
from pathlib import Path

from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

from engine.component_host import ComponentHost, CandidateError, HostError
from engine.scoped_units import append_record


class IsolatedComponentHost(ComponentHost):
    def __init__(self, host_dir, node_bin_dir, audit_path):
        super().__init__(host_dir, node_bin_dir)
        self.audit_path = Path(audit_path)
        self.set_log_sink(lambda record: append_record(self.audit_path, record))

    def sandbox_command(self, command):
        # 挂载运行库，清空环境，并隔离网络与进程；工作区、配置及评分器均未挂载。
        worker = Path(self.host_dir) / "src/scoped-worker.mjs"
        node = Path(self.node_bin_dir) / "node"
        for path in (worker, node, Path(self.host_dir) / "node_modules"):
            if not path.exists():
                raise HostError("隔离运行库缺失")
        return ["bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--clearenv",
                "--cap-drop", "ALL", "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
                "--dir", "/work", "--dir", "/runtime",
                "--ro-bind", str(Path(self.host_dir) / "node_modules"), "/runtime/node_modules",
                "--ro-bind", str(worker), "/runtime/worker.mjs", "--ro-bind", str(node), "/runtime/node",
                "--setenv", "PATH", "/runtime:/usr/bin", "--setenv", "HOME", "/work",
                "--chdir", "/work", "--", *command]

    def _ensure_started(self):
        if self._closed:
            raise HostError("组件隔离进程已关闭")
        if self._process is not None:
            if self._process.poll() is not None:
                raise HostError(f"组件隔离进程已退出：{self._process.returncode}")
            return
        self._process = subprocess.Popen(
            self.sandbox_command(["/runtime/node", "/runtime/worker.mjs"]),
            cwd=self.host_dir, env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        )
        self._reader = JsonRpcStreamReader(self._process.stdout)
        self._endpoint = Endpoint({"component.log": lambda record: append_record(self.audit_path, {"kind": "component_log", **record})},
                                  JsonRpcStreamWriter(self._process.stdin).write, id_generator=self._next_id)
        self._reader_thread = threading.Thread(target=self._reader.listen, args=(self._endpoint.consume,), daemon=True)
        self._reader_thread.start()

    def load_unit(self, unit):
        result = self.request("load", {"candidate_id": unit["id"], "component": unit["component"], "source": unit["source"]})
        append_record(self.audit_path, {"kind": "plugin_loaded", "unit": unit["id"], "plugin": result["plugin"],
                                       "interpreter": result["interpreter"], "os_isolation": "bubblewrap"})
        return result

    def _map_error(self, method, params, exception):
        error = super()._map_error(method, params, exception)
        data = exception.data if isinstance(exception.data, dict) else {}
        if data.get("permission_denied"):
            error.permission_denied = True
            append_record(self.audit_path, {"kind": "permission_denied", "unit": params.get("candidate_id"),
                                           "method": method, "error": str(error)})
        return error
