#!/bin/bash
# 单容器同时启动 核验API 与 Streamlit 网页
uvicorn api:app --host 0.0.0.0 --port 8000 &
exec streamlit run app.py --server.port 8501 --server.address 0.0.0.0 --browser.gatherUsageStats false
