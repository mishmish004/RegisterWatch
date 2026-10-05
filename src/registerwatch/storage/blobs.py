"""Blob storage behind a two-method protocol.

Supabase Storage speaks the S3 protocol, so S3Blobs covers both: point
S3_ENDPOINT_URL at Supabase today, at AWS/R2 tomorrow. The protocol exists so a
native-SDK implementation stays a drop-in if the S3 endpoint disappoints.

Layout:
    <slug>/<YYYY-MM-DD>/<run_id>/page-<ordinal>.<ext>
    <slug>/<YYYY-MM-DD>/<run_id>/manifest.json     <- blob_ref points here

One object per page rather than a concatenated archive: pages are individually
addressable as evidence, and a re-parse (step 2) can stream them one at a time.

The <run_id> level is not decoration. Two runs on one day must not overwrite
each other: the earlier one is the evidence that the register looked different
six hours ago, and an object store will happily lose it for you.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
from datetime import datetime
from typing import Protocol

import boto3
from botocore.config import Config

from registerwatch.config import settings


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> str: ...
    def get(self, key: str) -> bytes: ...


class S3Blobs:
    def __init__(self) -> None:
        s = settings()
        if not (s.s3_access_key_id and s.s3_secret_access_key):
            raise RuntimeError(
                "S3_ACCESS_KEY_ID / S3_SECRET_ACCESS_KEY are unset. "
                "Set them, or use LocalBlobs(settings().snapshot_root)."
            )
        self._bucket = s.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key_id,
            aws_secret_access_key=s.s3_secret_access_key,
            config=Config(
                s3={"addressing_style": "path" if s.s3_force_path_style else "auto"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )
        return key

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()


class LocalBlobs:
    """The same protocol, backed by a directory.

    Not a test double — it is the storage you want before there is a second
    machine, and the switch to S3Blobs is one line at the call site. Keys are
    relative paths; anything trying to climb out of the root is refused, because
    a key is ultimately derived from a filename on someone else's web server.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = pathlib.Path(root).expanduser().resolve()

    @property
    def root(self) -> pathlib.Path:
        return self._root

    def _path(self, key: str) -> pathlib.Path:
        path = (self._root / key).resolve()
        if path == self._root or self._root not in path.parents:
            raise ValueError(f"key escapes blob root: {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        if path.exists() and path.read_bytes() != data:
            # Evidence is written once. Same bytes is a harmless re-run; different
            # bytes under the same key means two runs share a run_id, and the one
            # that lost the race would vanish silently.
            raise FileExistsError(f"refusing to overwrite {key} with different bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write leaves no half object behind for
        # the parser to read as a truncated register.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


def run_prefix(slug: str, run_started_at: datetime, run_id: str) -> str:
    return f"{slug}/{run_started_at:%Y-%m-%d}/{run_id}"


def write_manifest(store: BlobStore, prefix: str, manifest: dict) -> str:
    key = f"{prefix}/manifest.json"
    store.put(key, json.dumps(manifest, indent=2).encode(), "application/json")
    return key


def make_store(root: str | os.PathLike[str] | None = None) -> BlobStore:
    """The configured store. An explicit root always means local disk."""
    if root is None and settings().blob_backend == "s3":
        return S3Blobs()
    return LocalBlobs(root or settings().snapshot_root)
