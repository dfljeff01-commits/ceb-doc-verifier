# 中欧班列单证智能核验（内部系统）—— 容器镜像
# 部署：docker compose up -d --build（含 PostgreSQL，见 docker-compose.yml）
#   网页：http://<内网服务器IP>:8501   API文档：http://<内网服务器IP>:8000/docs
# 本系统仅限内部部署使用，不面向公网提供服务。
#
# 镜像内含扫描件OCR所需的系统级依赖：tesseract-ocr + 中文包 chi_sim + poppler-utils

FROM python:3.12-slim

ENV PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 系统级依赖：OCR（tesseract 中文）与 poppler（pdf2image 光栅化）
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        poppler-utils \
        fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖层（利用构建缓存）
COPY requirements.txt .
RUN pip install -r requirements.txt

# 应用代码（doc_contract.py 为数据契约层，被引擎/摄取/API/网页共同引用）
COPY verification_engine.py risk_model.py semantic.py doc_contract.py llm_layer.py \
     llm_presets.json pdf_ingest.py api.py selftest.py evaluation.py start.sh \
     chat_assistant.py email_generator.py knowledge_base.py llm_endpoint.py \
     mobile_store.py doc_rules.py doc_rules.yaml upload_wizard.py \
     db.py auth_service.py audit.py init_db.py migrate_json_to_pg.py \n     field_extraction.py ./
# 网页端（webapp 双入口：登录 + 单据核对/数据核对/用户管理/操作日志）
COPY webapp/ webapp/
COPY sample_data/ sample_data/
COPY sample_pdfs/ sample_pdfs/
COPY evaluation_set/ evaluation_set/

EXPOSE 8501 8000

# API健康状态可见（F06）：/health 失败即标记 unhealthy
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)" || exit 1

CMD ["bash", "start.sh"]
