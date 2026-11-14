FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    PADDLE_DEVICE=cpu \
    PADDLE_CPU_THREADS=2 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libgomp1 \
        libgl1 \
        libglib2.0-0 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements.ocr.txt requirements.api.txt /app/
RUN python -m pip install --no-cache-dir --upgrade pip==26.3 \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    && python -m pip install --no-cache-dir -r /app/requirements.ocr.txt \
    && python -m pip install --no-cache-dir -r /app/requirements.api.txt

RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(text_detection_model_name='PP-OCRv5_mobile_det', text_recognition_model_name='eslav_PP-OCRv5_mobile_rec', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, enable_mkldnn=False, cpu_threads=2, device='cpu')"

COPY src /app/src
COPY tests /app/tests
COPY poc/linux-ocr-benchmark /app/poc/linux-ocr-benchmark

ENTRYPOINT ["python", "-m", "financial_verification.pipeline"]
