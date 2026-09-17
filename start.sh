#!/bin/bash
# 单容器同时启动 核验API 与 Streamlit 网页（F06：API生命周期受管——
# API进程退出时终止整个脚本使容器退出，而不是让网页"假活"静默降级直连）。
set -u

uvicorn api:app --host 0.0.0.0 --port 8000 &
API_PID=$!

# 看门狗：API进程一旦退出 → 结束本脚本（连同网页），让编排层看到失败并重启
(
  while kill -0 "$API_PID" 2>/dev/null; do sleep 2; done
  echo "[start.sh] 核验API进程(PID $API_PID)已退出，终止容器以便重启" >&2
  kill "$$" 2>/dev/null
) &
WATCHDOG_PID=$!

cleanup() {
  kill "$API_PID" "$WATCHDOG_PID" 2>/dev/null || true
}
trap cleanup TERM INT

streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --browser.gatherUsageStats false
STATUS=$?
cleanup
exit "$STATUS"
