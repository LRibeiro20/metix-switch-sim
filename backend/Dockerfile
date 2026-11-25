FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --no-install-project

COPY shared ./shared
COPY app ./app
COPY cbs ./cbs
COPY wallet_sim ./wallet_sim

EXPOSE 8000 8443 8444 8445 8446

CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
