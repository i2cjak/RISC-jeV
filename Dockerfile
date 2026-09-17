FROM python:3.12-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY . .
RUN make build && python scripts/compiler_root.py
ENV COMPILER_ROOT=/opt/compiler-root PYTHONUNBUFFERED=1 PORT=8080
CMD ["sh", "-c", "exec .venv/bin/gunicorn --workers 1 --threads 16 --timeout 120 --bind 0.0.0.0:${PORT} --access-logfile - app:app"]
