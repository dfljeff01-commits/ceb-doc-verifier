# 中欧班列单证智能核验 Demo —— 容器镜像
# 构建：docker build -t ceb-doc-verifier .
# 运行：docker run -p 8501:8501 -p 8000:8000 ceb-doc-verifier
#   网页：http://localhost:8501   API文档：http://localhost:8000/docs
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

# 应用代码
COPY verification_engine.py risk_model.py semantic.py llm_layer.py llm_presets.json \
     pdf_ingest.py api.py app.py selftest.py evaluation.py start.sh \
     chat_assistant.py email_generator.py knowledge_base.py llm_endpoint.py ./
COPY sample_data/ sample_data/
COPY sample_pdfs/ sample_pdfs/

EXPOSE 8501 8000

CMD ["bash", "start.sh"]
