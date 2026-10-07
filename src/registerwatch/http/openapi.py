"""The OpenAPI document, with what FastAPI cannot express on its own.

- FastAPI serialises the document with `exclude_none=True`, which also strips
  every `null` out of schema examples, so an example of a nullable required
  field (`"next_cursor": null`) stops matching its own schema. The response
  models' own examples are put back here, nulls included.
- The error responses (`components.responses`, and the `Problem` schema they
  share) come from the catalog in http/problems.py. v1 operations refer to
  them; FastAPI's default 422 is dropped from v1, which answers 400 instead.
- Response headers no route declares: every v1 answer but the probes' carries
  the rate limit fields (http/ratelimit.py), and every one `Cache-Control`;
  reads also an `ETag` and a 304 for `If-None-Match` (http/caching.py). The
  status and the probes are never stored, so they have neither.

The probes (`/livez`, `/readyz`) are unversioned paths but v1 operations, and
are treated as such here.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from registerwatch.http import caching, models, problems, ratelimit


def install(app: FastAPI) -> None:
    generate = app.openapi  # FastAPI's own, which caches in app.openapi_schema

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schemas = generate().get("components", {}).get("schemas", {})
            for name, model in inspect.getmembers(models, inspect.isclass):
                if not (issubclass(model, BaseModel) and model.__module__ == models.__name__):
                    continue
                extra = model.model_config.get("json_schema_extra")
                if isinstance(extra, dict) and "examples" in extra and name in schemas:
                    schemas[name]["examples"] = extra["examples"]
            _problems(app.openapi_schema)  # type: ignore[arg-type]
            _headers(app.openapi_schema)  # type: ignore[arg-type]
        return app.openapi_schema  # type: ignore[return-value]

    app.openapi = openapi  # type: ignore[method-assign]


def _v1(path: str) -> bool:
    return path.startswith("/v1/") or path in problems.PROBES


def _problems(spec: dict[str, Any]) -> None:
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    problem = models.Problem.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(problem.pop("$defs", {}))
    schemas["Problem"] = problem
    components["responses"] = problems.components()
    for path, item in spec.get("paths", {}).items():
        if not _v1(path):
            continue
        for op in item.values():
            responses = op.get("responses", {})
            if "$ref" not in responses.get("422", {"$ref": ""}):  # FastAPI's own; v1 answers 400
                responses.pop("422")
            for code, response in responses.items():
                if "$ref" in response:  # FastAPI adds a description beside the reference
                    responses[code] = {"$ref": response["$ref"]}
            op["responses"] = dict(sorted(responses.items()))
    if not any("422" in op.get("responses", {}) for item in spec.get("paths", {}).values() for op in item.values()):
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)


def _headers(spec: dict[str, Any]) -> None:
    components = spec["components"]
    components["headers"] = {**caching.HEADERS, **ratelimit.HEADERS}
    ref = {name: {"$ref": f"#/components/headers/{name}"} for name in components["headers"]}
    limits = {name: ref[name] for name in ratelimit.HEADERS}
    for response in components["responses"].values():
        response["headers"] = {**response.get("headers", {}), **limits}
    components["responses"]["NotModified"] = {**caching.NOT_MODIFIED,
                                              "headers": {**caching.NOT_MODIFIED["headers"], **limits}}
    for path, item in spec.get("paths", {}).items():
        if not _v1(path):
            continue
        limited = path not in problems.PROBES
        for method, op in item.items():
            # Ingest, the status and the probes are no-store: no validator to send back.
            read = method == "get" and op.get("tags") not in (["ingest"], ["operations"])
            for status, response in op["responses"].items():
                if status.startswith("2"):
                    response["headers"] = {**response.get("headers", {}), "Cache-Control": ref["Cache-Control"],
                                           **({"ETag": ref["ETag"]} if read else {}),
                                           **(limits if limited else {})}
            if read:
                op["responses"]["304"] = {"$ref": "#/components/responses/NotModified"}
                op["responses"] = dict(sorted(op["responses"].items()))
