# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip hatchling

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --no-cache-dir build && python -m build --wheel --outdir /build/dist

FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g 10001 personaos && \
    useradd -u 10001 -g personaos -m -s /bin/bash personaos

COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -rf /tmp/*.whl

COPY src/personaos/db/migrations/ /app/src/personaos/db/migrations/
COPY policies/ /app/policies/
COPY profiles/ /app/profiles/

RUN chown -R personaos:personaos /app
USER personaos

EXPOSE 8000
CMD ["personaos-api"]
