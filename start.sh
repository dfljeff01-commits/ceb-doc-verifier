#!/bin/bash
# 单容器同时启动 核验API 与 Streamlit 网页（内部部署形态，配合 docker-compose.yml）。
#
# 启动顺序（架构升级后）：
#   1. 等待 PostgreSQL 就绪（db 独立容器，Docker网络互联）；
#   2. python init_db.py —— 建表（幂等）+ 首次种子账号；
#   3. python migrate_json_to_pg.py --quiet —— 若挂载了旧 mobile_results/ JSON
#      备份则自动迁入（幂等，可重跑）；
#   4. 拉起 uvicorn API（生命周期受管：API退出则容器退出，F06）+ Streamlit 网页。
set -u

echo "[start.sh] 等待 PostgreSQL 就绪…"
python - <<'PYWAIT'
import sys, time
import db
deadline = time.time() + 60
while True:
    try:
        if db.ping():
            sys.exit(0)
    except Exception:
        pass
    if time.time() > deadline:
        print("[start.sh] 数据库60秒内未就绪，退出以便编排层重启", file=sys.stderr)
        sys.exit(1)
    time.sleep(2)
PYWAIT
if [ $? -ne 0 ]; then exit 1; fi

echo "[start.sh] 初始化数据库 schema 与种子账号…"
python init_db.py || exit 1

echo "[start.sh] 检查并迁移旧JSON数据（如有）…"
python migrate_json_to_pg.py --quiet || echo "[start.sh] 警告：JSON迁移校验未通过，请人工核查（服务继续启动，不影响库内已迁数据）"

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

streamlit run webapp/Home.py --server.port 8501 --server.address 0.0.0.0 --browser.gatherUsageStats false
STATUS=$?
cleanup
exit "$STATUS"
