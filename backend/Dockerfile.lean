# Build explicitly during qualification; existing Railway configs keep the
# compatibility image until queued legacy work and artifacts are reconciled.
FROM python:3.11-slim AS source
WORKDIR /src
COPY . .
RUN python ops/runtime/build.py /src /runtime

FROM python:3.11-slim AS runtime
WORKDIR /app
COPY backend/requirements-core.txt /tmp/requirements-core.txt
RUN pip install --no-cache-dir -r /tmp/requirements-core.txt \
    && rm /tmp/requirements-core.txt
COPY --from=source /runtime /app
RUN adduser --disabled-password --gecos '' appuser && chown -R appuser /app
USER appuser
ENV PYTHONPATH=/app \
    FLASK_APP=backend/run.py \
    RECOVERY_ANALYSIS_MODE=legacy_simulation \
    RECOVERY_DIRECT_MERCHANT_IDS=""
WORKDIR /app/backend
EXPOSE ${PORT:-5001}
CMD ["/bin/sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5001} --workers 2 --timeout 60 --preload run:app"]

FROM runtime AS hosted
ENTRYPOINT ["/app/ops/typesafe/hosted-entrypoint.sh"]

# Default docker build has no hosted deployment-profile requirement.
FROM runtime AS default
