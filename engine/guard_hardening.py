import os
import sys

# 官方 SafetyGuard 的置空清单未覆盖进程执行家族（os.exec*/spawn*/posix_spawn*）、
# ctypes 动态库加载与 pty.spawn；运行时 __import__ 又绕过 AST 导入白名单。
# 本模块在类级别包住 SafetyGuard.enable/disable：原 enable 之后追加置空上述家族，
# disable 之后恢复原值；sys.addaudithook 在守卫激活期间兜底拦截对应审计事件。
# 不改 site-packages 官方包；装载点在 engine/run_experiment.py 入口。

GUARD_HARDENING_VERSION = 1

OS_NULL_NAMES = [
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
    "posix_spawn", "posix_spawnp", "startfile",
]

BLOCKED_AUDIT_EVENTS = {
    "os.exec", "os.spawn", "os.posix_spawn", "subprocess.Popen",
    "ctypes.dlopen", "ctypes.dlsym",
}

_guard_depth = 0
_installed = False


def _noop(*args, **kwargs):
    return None


def _audit_hook(event, args):
    if _guard_depth > 0 and event in BLOCKED_AUDIT_EVENTS:
        raise PermissionError(f"SafetyGuard 激活期间禁止该操作：{event}")


def _null_targets():
    import ctypes
    import pty

    targets = [(os, name) for name in OS_NULL_NAMES]
    targets.append((pty, "spawn"))
    targets += [(ctypes, "CDLL"), (ctypes, "PyDLL")]
    for loader_name in ("cdll", "pydll", "windll"):
        loader = getattr(ctypes, loader_name, None)
        if loader is not None:
            targets.append((loader, "LoadLibrary"))
    return targets


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    sys.addaudithook(_audit_hook)
    from appworld.common.safety_guard import SafetyGuard

    original_enable = SafetyGuard.enable
    original_disable = SafetyGuard.disable
    saved = []

    def hardened_enable(self):
        global _guard_depth
        original_enable(self)
        if _guard_depth == 0:
            for obj, name in _null_targets():
                if hasattr(obj, name):
                    saved.append((obj, name, getattr(obj, name)))
                    setattr(obj, name, _noop)
        _guard_depth += 1

    def hardened_disable(self):
        global _guard_depth
        _guard_depth = max(_guard_depth - 1, 0)
        try:
            original_disable(self)
        finally:
            if _guard_depth == 0:
                while saved:
                    obj, name, value = saved.pop()
                    setattr(obj, name, value)

    SafetyGuard.enable = hardened_enable
    SafetyGuard.disable = hardened_disable
