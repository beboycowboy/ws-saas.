FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
WORKDIR /srv
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY examples ./examples
RUN uv sync --frozen --no-dev && useradd --uid 10001 --create-home bot && mkdir /srv/.runtime && chown bot:bot /srv/.runtime
USER bot
ENV PATH="/srv/.venv/bin:$PATH"
CMD ["bot", "api"]
