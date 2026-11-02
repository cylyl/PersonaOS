"""OpenAPI Schema Generator for PersonaOS Integration Gateway.

Extracts, filters, and formats the OpenAPI 3.1.0 specification for the
Integration Gateway (/integration/*) endpoints.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from personaos.api.routes.integration import router as integration_router


def get_integration_openapi_schema() -> dict[str, Any]:
    """Generate isolated OpenAPI schema specifically for the Integration Gateway."""
    app = FastAPI(
        title="PersonaOS Integration Gateway API",
        version="1.0.0",
        description=(
            "Standardized API contract for the PersonaOS Integration Gateway (ADR 0041). "
            "Mediates communication between Orca ADE workspace coordination and the PersonaOS workforce kernel."
        ),
    )
    app.include_router(integration_router)

    return get_openapi(
        title=app.title,
        version=app.version,
        openapi_version="3.1.0",
        description=app.description,
        routes=app.routes,
    )


def dump_integration_openapi_schema(output_path: str | Path | None = None) -> str:
    """Export the OpenAPI schema to JSON string and optionally write to file."""
    schema = get_integration_openapi_schema()
    json_text = json.dumps(schema, indent=2, sort_keys=True)
    if output_path is not None:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json_text, encoding="utf-8")
    return json_text


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "openapi_integration.json"
    dump_integration_openapi_schema(out)
    print(f"Exported Integration Gateway OpenAPI schema to {out}")
