FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HF_HOME=/models \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=10
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*
# pip 25.0.1 in the base image cannot resume interrupted wheel downloads.
# Keep hash verification enabled; use a version with download-resume fixes.
RUN python -m pip install --upgrade "pip==26.2.1"
ENV PIP_RESUME_RETRIES=10
# Separate layers preserve the large CPU wheels when a later dependency fails.
RUN python -m pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
COPY pyproject.toml ./
# Dependency metadata is independent of application source. pip >=26.2 supports --only-deps.
RUN python -m pip install --only-deps '.[docling]'
COPY clc ./clc
RUN python -m pip install --no-deps . && python -m pip check
CMD ["uvicorn", "clc.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
