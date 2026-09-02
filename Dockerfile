FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock config.example.yaml ./
COPY src ./src
RUN uv sync --frozen --no-dev
RUN useradd --create-home trader && mkdir -p /app/sharedata && chown -R trader:trader /app
USER trader
EXPOSE 8089
CMD ["uv", "run", "--frozen", "--no-dev", "ibtrader"]
