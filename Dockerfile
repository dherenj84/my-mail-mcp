FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=0
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY --from=builder /app/.venv /app/.venv
RUN useradd --create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH" PORT=8080
EXPOSE 8080
CMD ["my-mail-mcp"]
