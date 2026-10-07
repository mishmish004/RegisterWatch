"""The OpenAPI document, with what FastAPI cannot express on its own.

- FastAPI serialises the document with `exclude_none=True`, which also strips
  every `null` out of schema examples, so an example of a nullable required
  field (`"next_cursor": null`) stops matching its own schema. The response
  models' own examples are put back here, nulls included.
- The error responses (`components.responses`, and the `Problem` schema they
  share) come from the catalog in http/problems.py. v1 operations refer to
  them; FastAPI's default 422 is dropped from v1, which answers 400 instead.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from registerwatch.http import models, problems


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
        return app.openapi_schema  # type: ignore[return-value]

    app.openapi = openapi  # type: ignore[method-assign]


def _problems(spec: dict[str, Any]) -> None:
    components = spec.setdefault("components", {})
    schemas = components.setdefault("schemas", {})
    problem = models.Problem.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(problem.pop("$defs", {}))
    schemas["Problem"] = problem
    components["responses"] = problems.components()
    for path, item in spec.get("paths", {}).items():
        if not path.startswith("/v1/"):
            continue
        for op in item.values():
            responses = op.get("responses", {})
            responses.pop("422", None)
            for code, response in responses.items():
                if "$ref" in response:  # FastAPI adds a description beside the reference
                    responses[code] = {"$ref": response["$ref"]}
            op["responses"] = dict(sorted(responses.items()))
    if not any("422" in op.get("responses", {}) for item in spec.get("paths", {}).values() for op in item.values()):
        schemas.pop("HTTPValidationError", None)
        schemas.pop("ValidationError", None)
