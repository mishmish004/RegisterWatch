"""The versioned HTTP API. `registerwatch.api` builds the app and mounts it.

  deps     the database, the read check, identifier lookup
  models   every v1 request and response shape (the OpenAPI schemas)
  cursor   opaque page cursors
  v1/      one module per resource

The unversioned routes still live in `registerwatch.api` as the legacy surface
until they are retired (plan.md, Phase 12). Nothing here imports them.
"""
