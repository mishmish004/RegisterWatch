"""The OpenAPI document, with one repair FastAPI needs.

FastAPI serialises the document with `exclude_none=True`, which also strips
every `null` out of schema examples, so an example of a nullable required field
(`"next_cursor": null`) stops matching its own schema. The response models' own
examples are put back here, nulls included.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from registerwatch.http import models


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
        return app.openapi_schema  # type: ignore[return-value]

    app.openapi = openapi  # type: ignore[method-assign]
