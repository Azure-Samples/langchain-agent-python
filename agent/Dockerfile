# syntax=docker/dockerfile:1.7
FROM python:3.11-slim

# Install uv for fast, reproducible Python installs.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --system -r requirements.txt

# Package layout:
#   /app/app/         (the Python package: agent.py, main.py, middleware/, tools/, prompts/)
#   /app/static/      (chat UI)
COPY app/ ./app/
COPY static/ ./static/

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

