"""The API's contract: the committed spec, and the app held to it.

openapi/v1.yaml is generated from the app, like the register DDL, and must stay
generated: clients are built from it and scripts/verify/breaking.sh diffs it.
"""

from __future__ import annotations

import pathlib

from registerwatch.cli import OPENAPI_SPEC, openapi_yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_spec_file_matches_app():
    assert (ROOT / OPENAPI_SPEC).read_text() == openapi_yaml(), \
        f"{OPENAPI_SPEC} is stale: run `uv run registerwatch openapi --write`"
