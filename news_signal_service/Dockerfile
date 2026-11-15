FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    HF_HOME=/opt/hf

WORKDIR /srv

COPY requirements.txt requirements-ml.txt ./
# CPU-only torch first, so sentence-transformers does not pull the multi-GB CUDA build.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements-ml.txt
# Bake the similarity model into the image (outside /data, which is a Volume) so a run never has to download it.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

COPY app ./app
COPY data ./data

# ONE worker on purpose: the "one run at a time" guard (app/jobs.py) is in-process.
# Railway injects PORT; default for local `docker run`.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
