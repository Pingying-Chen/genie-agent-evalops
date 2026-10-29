"""Write the backend's OpenAPI spec to frontend/openapi.json (input for `npm run gen:types`).

uv run python scripts/export_openapi.py && (cd frontend && npm run gen:types)
"""

import json
import pathlib

from evalops.main import create_app

out = pathlib.Path(__file__).resolve().parents[1] / "frontend" / "openapi.json"
out.write_text(json.dumps(create_app().openapi(), indent=1) + "\n")
print(f"wrote {out}")
