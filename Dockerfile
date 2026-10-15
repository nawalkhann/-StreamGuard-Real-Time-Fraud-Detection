FROM python:3.12-slim AS build
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . 
# Bootstrap model on synthetic data so the image works out of the box.
# For production, replace /app/models/model.joblib with a model trained on your labelled history.
RUN mkdir -p /app/models && STREAMGUARD_MODEL_PATH=/app/models/model.joblib python -m streamguard.train --accounts 2000

FROM python:3.12-slim
RUN useradd --system --create-home --uid 10001 sg
WORKDIR /app
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/uvicorn /usr/local/bin/uvicorn
COPY --from=build /app/models /app/models
RUN mkdir -p /data && chown sg /data
USER sg
ENV STREAMGUARD_DB_PATH=/data/streamguard.db STREAMGUARD_MODEL_PATH=/app/models/model.joblib \
    STREAMGUARD_SANCTIONS_DIR=/data/sanctions
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
# One worker: feature state lives in process memory. Scale by partitioning accounts across instances.
CMD ["uvicorn", "streamguard.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
