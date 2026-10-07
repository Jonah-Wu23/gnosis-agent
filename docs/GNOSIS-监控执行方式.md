# GNOSIS 运行监控方式与约束

## 1. 监控启动命令

活动账本部署于 WSL 原生 Linux 文件系统（ext4，位于 `/home/jonahwu/.gnosis/research_ledger.sqlite`，通过 `state/research_ledger.sqlite` 软链接访问）。全部数据库读写操作**统一由 WSL 执行**。

在 Windows 终端中直接执行：

```powershell
wsl -d Ubuntu --cd "/mnt/e/AI/GNOSIS Agent" -- bash monitor_fixed.sh
```

该脚本每 10 秒刷新一次，输出：
- `python -m engine.run_research` 运行进程及资源占用；
- 已生成的 execution JSON 数量；
- API 调用次数、理论参照成本与折算实际计费；
- 最新 10 行日志。

按 `Ctrl+C` 安全退出，不影响实验后台进程。

## 2. 账本查询与锁机制规范

监控查询必须严格遵守标准只读事务契约：
- **禁止使用 `immutable=1`**：活动账本在实验运行期间持续处于写入状态，使用 `immutable=1` 会让 SQLite 旁路数据变更检测与锁协调，导致读出撕裂数据甚至触发 `SQLITE_CORRUPT`。
- **使用标准 `mode=ro`**：活动账本使用 WAL，读事务读取一致快照；连接设置 60 秒锁等待时间。
- **保留完整异常输出**：数据库查询失败时，Python 向 stderr 输出完整异常调用链，监控脚本以失败状态退出。
