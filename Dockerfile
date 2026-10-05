# registerwatch API. uv resolves from uv.lock, so the image runs exactly the
# versions the tests ran.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, in their own layer: they change far less than the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src

RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"
# The container's disk is gone at the next deploy, and the blobs are evidence.
ENV BLOB_BACKEND=s3

# PORT comes from the platform (Railway, Fly, Render); 8000 otherwise.
EXPOSE 8000
CMD ["registerwatch", "serve"]
