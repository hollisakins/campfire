# Hosted MCP server for the empirical NIRSpec ETC (streamable HTTP).
# Build context: this directory (etc/). Used by fly.toml; any container host works.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY campfire_etc ./campfire_etc
RUN pip install --no-cache-dir ".[mcp]"

# The public URL and accepted Host headers are set per deployment (fly.toml [env]).
ENV PORT=8080
EXPOSE 8080

# Unprivileged user; nothing is written to disk (downloads live in memory).
RUN useradd --create-home --uid 1000 etc
USER etc

CMD ["campfire-etc", "serve", "--http", "--host", "0.0.0.0", "--port", "8080"]
