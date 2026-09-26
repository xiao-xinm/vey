FROM python:3.13-slim AS builder
ARG PYTHON_PACKAGE_INDEX=https://pypi.org/simple
WORKDIR /app
RUN pip install --no-cache-dir --index-url "$PYTHON_PACKAGE_INDEX" uv==0.12.19
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv export --frozen --no-dev --no-emit-project --output-file /tmp/requirements.txt \
    && uv venv .venv \
    && uv pip install --require-hashes --index-url "$PYTHON_PACKAGE_INDEX" -r /tmp/requirements.txt \
    && uv pip install --no-deps --index-url "$PYTHON_PACKAGE_INDEX" .

FROM python:3.13-slim
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10000
ENTRYPOINT ["vey"]
CMD ["serve", "core"]
