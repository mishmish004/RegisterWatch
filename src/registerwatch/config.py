"""Environment -> one typed settings object. Nothing else reads os.environ."""

from __future__ import annotations

import logging
import pathlib
import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The project's .env, then one in the working directory (later wins). `uv run`
# from any directory and a container with real env vars resolve the same way;
# a missing file is simply skipped.
ENV_FILES = (PROJECT_ROOT / ".env", pathlib.Path(".env"))

_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")

# Bearer tokens: 32 characters is ~190 bits from token_urlsafe, out of guessing range.
MIN_TOKEN_LENGTH = 32


class Settings(BaseSettings):
    # hide_input_in_errors: a refused value (a token, a database URL with its
    # password) is not repeated in the error, which ends up in startup logs.
    model_config = SettingsConfigDict(env_file=ENV_FILES, extra="ignore", hide_input_in_errors=True)

    # Postgres (Supabase pooler URI is fine; use the session pooler for CLI runs)
    database_url: str

    # Blob storage. Supabase Storage speaks S3, so this block covers both:
    #   Supabase -> https://<project>.storage.supabase.co/storage/v1/s3
    #   AWS/R2   -> the usual endpoint, or leave endpoint blank for AWS default
    #
    # `local` writes under snapshot_root. Right for a laptop; wrong for a
    # container, whose disk is gone at the next deploy along with the evidence.
    blob_backend: Literal["local", "s3"] = "local"
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = "registerwatch-raw"
    # Empty is legitimate: the local disk store needs no credentials, and step 1
    # is runnable end-to-end without an object store at all.
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_force_path_style: bool = True  # required by Supabase, harmless on R2

    # Identify yourself. This is protection, not exposure: it turns "unknown bot"
    # into "known operator we can email before we block." Avoid the word
    # "monitoring": ACMA's load balancer answered user agents saying "compliance
    # change monitoring" with its uptime-check JSON most of the time (4 Oct 2026).
    user_agent: str = Field(
        default=(
            "RegisterWatch/0.1 (gambling licence register tracking; "
            "+https://example.com/bot; contact@example.com)"
        )
    )

    # Where the local blob store writes. Snapshots land under
    #   <snapshot_root>/<slug>/<YYYY-MM-DD>/<run_id>/
    snapshot_root: pathlib.Path = pathlib.Path("snapshots")

    http_timeout_s: float = 30.0
    max_attempts: int = 4
    respect_robots: bool = True

    # Skip a fetch if a *good* snapshot for this source already landed within N
    # hours, unless --force. Stops a cron double-fire from doubling your request
    # volume; failed runs do not count, so a retry slot gets a real retry.
    min_refetch_interval_h: float = 20.0

    # API. The ingest endpoint needs a bearer token and refuses to run without
    # one configured, so a forgotten env var is a 503, not an open trigger.
    ingest_token: str = ""
    # Read endpoints (registers, rows, search, domain checks). Empty = open:
    # the data is public registers. Set it to require `Bearer <READ_TOKEN>`
    # (the ingest token is accepted too).
    read_token: str = ""
    host: str = "0.0.0.0"
    port: int = 8000  # PORT, as set by Railway / Fly / Render

    # /status answers 503 once the newest good snapshot is older than this.
    # 26h = one daily run plus slack for the retry slots.
    stale_after_h: float = 26.0

    # Every statement of a v1 request's transaction is cancelled after this
    # (a 504), so one slow query cannot hold a pooled connection. 0 = no limit.
    read_statement_timeout_ms: int = Field(default=5000, ge=0)

    # Connections for reads (every API request) come from one pool of at most
    # DB_POOL_MAX; a request that waits DB_POOL_TIMEOUT_S for one is a 503
    # `database-unavailable`. Ingest runs have a pool of their own (2), so a
    # batch never takes a connection a read needs. Sync handlers run in
    # 2 x DB_POOL_MAX threads, so a burst queues for a thread rather than
    # timing out on the pool.
    db_pool_max: int = Field(default=10, ge=1)
    db_pool_timeout_s: float = Field(default=3.0, gt=0)

    # Requests per minute per client: per bearer token when one of ours is sent,
    # else per client address. Kept in each process, so each replica (and each
    # worker) counts on its own. 0 turns a class off.
    rate_limit_read_per_min: int = Field(default=600, ge=0)
    rate_limit_search_per_min: int = Field(default=60, ge=0)   # /v1/search and /v1/domains
    rate_limit_ingest_per_min: int = Field(default=10, ge=0)   # starting ingest runs

    # `registerwatch serve` (plan.md P9.2, P9.3). Worker processes, each with
    # its own pools, buckets and caches. On SIGTERM the server stops accepting,
    # a running ingest finishes the register in hand and records the rest as
    # `not_started`, and in-flight requests finish: it waits SHUTDOWN_GRACE_S + 5
    # for them, and the process is gone 4 s after that whatever is still running.
    # Give the platform's stop timeout more than SHUTDOWN_GRACE_S + 10.
    web_concurrency: int = Field(default=1, ge=1)
    shutdown_grace_s: float = Field(default=60.0, ge=0)
    # Proxies whose X-Forwarded-For / X-Forwarded-Proto uvicorn believes: the
    # client address (and so the rate-limit bucket) comes from the header only
    # when the connection is from one of these. Comma-separated addresses or
    # networks, or `*` when nothing but the platform's proxy can reach the app.
    forwarded_allow_ips: str = "127.0.0.1"
    # Browser origins allowed to read the API with GET (plan.md P10.5), comma-
    # separated (`https://app.example`), or `*` for any. Empty: no CORS at all,
    # which suits a server-to-server API. Never with credentials.
    cors_allow_origins: str = ""

    # Test only (plan.md T9.3.b): every register of a v1 ingest run sleeps
    # 20 s and records a skip instead of fetching, so a stop can land mid-run
    # in a real container.
    fake_slow_ingest: bool = Field(default=False, validation_alias="REGISTERWATCH_FAKE_SLOW_INGEST")

    log_level: str = "INFO"

    @field_validator("ingest_token", "read_token")
    @classmethod
    def _strong_token(cls, v: str) -> str:
        # Empty is allowed (ingest off, reads open). A short token is refused at
        # startup: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
        # makes one. The message never repeats the value: it would reach the logs.
        if v and len(v) < MIN_TOKEN_LENGTH:
            raise ValueError(f"must be at least {MIN_TOKEN_LENGTH} characters (got {len(v)}); "
                             "use secrets.token_urlsafe(32)")
        return v

    @field_validator("cors_allow_origins")
    @classmethod
    def _origins(cls, v: str) -> str:
        # An origin is scheme://host[:port] and nothing else: "https://app.example/"
        # (a trailing slash) never equals the Origin a browser sends, so it would
        # silently allow nothing.
        for origin in (o.strip() for o in v.split(",") if o.strip()):
            if origin != "*" and not _ORIGIN.match(origin):
                raise ValueError(f"{origin!r} is not an origin: scheme://host[:port], no path or trailing slash")
        return v

    @property
    def cors_origins(self) -> tuple[str, ...]:
        return tuple(o.strip() for o in self.cors_allow_origins.split(",") if o.strip())

    @field_validator("s3_endpoint_url", "database_url", "user_agent")
    @classmethod
    def _no_placeholders(cls, v: str | None) -> str | None:
        # Was a module-level function, so pydantic never attached it — which is
        # how a `<your-email>` user agent reached the regulator.
        if v and ("<" in v or ">" in v):
            raise ValueError(f"placeholder left in .env: {v}")
        return v


@lru_cache
def settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    if any(p in s.user_agent for p in ("example.com", "yourdomain")):
        logging.getLogger(__name__).warning(
            "USER_AGENT names no real contact (%s); set it before running against a live register",
            s.user_agent,
        )
    return s
