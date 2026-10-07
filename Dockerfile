# registerwatch API (plan.md P9.1). Two stages: uv builds the virtualenv from
# uv.lock, so the image runs exactly the versions the tests ran; the image is
# the same slim Python with only that virtualenv copied in. No uv, no build
# cache, no source tree, nothing installed beyond the base.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${PYTHON_IMAGE} AS build
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
# The base image's Python, at the path the runtime stage has it too: the venv
# links to it.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12

# Dependencies first, in their own layer: they change far less than the code.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md ./
COPY src ./src
# Not editable: the package goes into the venv with its data (the migrations
# and the Slovak certificate chain), so the venv is all the image needs.
RUN uv sync --locked --no-dev --no-editable


FROM ${PYTHON_IMAGE}
RUN groupadd --system --gid 10001 registerwatch \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin registerwatch
COPY --from=build /app/.venv /app/.venv
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
# The container's disk is gone at the next deploy, and the blobs are evidence.
ENV BLOB_BACKEND=s3
# Runs with a read-only root filesystem; give it a tmpfs at /tmp.
USER 10001:10001

# PORT comes from the platform (Railway, Fly, Render); 8000 otherwise.
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=1s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/livez' % os.environ.get('PORT', '8000'), timeout=2)"]
# Exec form: the server is PID 1 and gets the platform's SIGTERM itself.
CMD ["registerwatch", "serve"]
