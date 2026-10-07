#!/usr/bin/env bash

INTERVAL=10

while true; do
    clear
    echo "========== GNOSIS 运行监控 =========="
    echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo

    echo "[1] Research 进程"
    PIDS=$(pgrep -f 'python -m engine.run_research' || true)
    if [ -n "$PIDS" ]; then
        for pid in $PIDS; do
            ps -p "$pid" -o pid=,etime=,%cpu=,%mem=,cmd= 2>/dev/null
        done
    else
        echo "未运行"
    fi
    echo

    echo "[2] Execution 进度"
    EXEC_COUNT=$(find experiments/research/executions -type f -name '*.json' 2>/dev/null | wc -l)
    echo "JSON 数量: $EXEC_COUNT"
    echo

    echo "[3] API 调用 / 成本"
    python3 -c '
import sqlite3
from contextlib import closing
with closing(sqlite3.connect("file:state/research_ledger.sqlite?mode=ro", uri=True, timeout=60)) as c:
    c.execute("PRAGMA busy_timeout = 60000")
    n, cost, billed = c.execute("SELECT COUNT(*), COALESCE(SUM(cost),0)*7.1, COALESCE(SUM(COALESCE(billed,cost)),0)*7.1 FROM calls").fetchone()
    print(f"调用次数: {n}")
    print(f"理论成本: ¥{cost:.2f}")
    print(f"计费成本: ¥{billed:.2f}")
' || exit "$?"
    echo

    echo "[4] 最新日志"
    echo "--------------------------------------"
    if [ -f experiments/run.log ]; then
        tail -n 10 experiments/run.log
    else
        echo "experiments/run.log 不存在"
    fi
    echo "--------------------------------------"
    echo "每 ${INTERVAL} 秒刷新 · Ctrl+C 退出"

    sleep "$INTERVAL"
done
