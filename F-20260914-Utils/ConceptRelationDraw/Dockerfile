FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml ./
COPY concept_relation ./concept_relation
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home app && mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8091
CMD ["uvicorn", "concept_relation.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
