"""
routers/pages.py — HTML page routes for azure-penny.
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from auth import _get_user_roles

_templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))

# APP_VERSION is injected by main.py via router.app_version after include.
# We read it lazily from the FastAPI app state to avoid a circular import.
_APP_VERSION = "1.5.0"

router = APIRouter(include_in_schema=False)


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return _templates.TemplateResponse(
        "dashboard.html", {"request": request, "version": _APP_VERSION}
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    return _templates.TemplateResponse(
        "dashboard.html", {"request": request, "version": _APP_VERSION}
    )


@router.get("/manager", response_class=HTMLResponse)
async def manager(request: Request):
    return _templates.TemplateResponse(
        "dashboard.html", {"request": request, "version": _APP_VERSION}
    )


@router.get("/technician", response_class=HTMLResponse)
async def technician(request: Request):
    return _templates.TemplateResponse(
        "technician.html", {"request": request, "version": _APP_VERSION}
    )


@router.get("/live", response_class=HTMLResponse)
async def live_view(request: Request):
    is_admin = "penny-admin" in _get_user_roles(request)
    return _templates.TemplateResponse(
        "live.html", {"request": request, "is_admin": is_admin, "version": _APP_VERSION}
    )


@router.get("/guide", response_class=HTMLResponse)
async def guide_view(request: Request):
    return _templates.TemplateResponse(
        "guide.html", {"request": request, "version": _APP_VERSION}
    )


@router.get("/shield", response_class=HTMLResponse)
async def shield_view(request: Request):
    return _templates.TemplateResponse(
        "shield.html", {"request": request, "version": _APP_VERSION}
    )
