FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# CPU-only torch wheel, installed separately from the official CPU index so
# we don't pull the much larger CUDA-bundled default wheel -- this backend
# is CPU-only by design (see plan: docs/PRD decision on Modal vs local CPU).
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend

ENV HF_HOME=/root/.cache/huggingface
ENV PYTHONUNBUFFERED=1
# See backend/translation/translator.py: the Xet fast-download backend
# stalled indefinitely on the NLLB checkpoint in testing; plain HTTP is
# slower but reliable for first-run model downloads.
ENV HF_HUB_DISABLE_XET=1

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
