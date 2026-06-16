"""
auth.py — FastAPI auth helpers for azure-penny.

Extracts and validates Entra ID roles from the ACA Easy Auth principal header
(X-MS-CLIENT-PRINCIPAL).
"""

import base64
import json

from fastapi import HTTPException, Request


def _get_user_roles(request: Request) -> list[str]:
    """Return Entra app role values from the ACA Easy Auth principal header."""
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not header:
        return []  # no Easy Auth (local dev) — caller decides how to handle
    try:
        principal = json.loads(base64.b64decode(header + "=="))
        return [
            c["val"] for c in principal.get("claims", []) if c.get("typ") == "roles"
        ]
    except Exception:
        return []


def _require_admin(request: Request) -> None:
    """FastAPI dependency — blocks non-admin users when Easy Auth is active."""
    header = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    if not header:
        return  # local dev: no Easy Auth header, allow through
    if "penny-admin" not in _get_user_roles(request):
        raise HTTPException(status_code=403, detail="penny-admin role required")
