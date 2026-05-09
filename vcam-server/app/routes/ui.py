"""HTML page routes for the admin panel.

Two server-rendered pages
-------------------------

* ``GET  /admin/login``  — login form
* ``POST /admin/login``  — credential check + cookie issue
* ``POST /admin/logout`` — clear cookie
* ``GET  /admin``        — single-page shell (Alpine.js takes over)

Everything else is JSON. The shell HTML at ``static/admin.html``
fetches data from the JSON APIs and renders cards/tables in the
browser. We deliberately keep the server side ignorant of UI
state — a future React rewrite touches zero backend code.
"""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import auth
from ..config import SETTINGS, TEMPLATES_DIR

router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Bare-domain → admin login. Customers don't browse here;
    they hit ``/api/v1/...``."""
    return RedirectResponse(url="/admin", status_code=302)


@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = ""):
    """Login form. Pre-fills ``error`` if redirected back from a
    failed POST."""
    if auth.maybe_current_admin(request) is not None:
        return RedirectResponse(url="/admin", status_code=302)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error, "brand": "NP Create Admin"},
    )


@router.post("/admin/login")
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
):
    """Verify creds, set cookie, redirect to dashboard.

    Bad creds return a 303 back to the login form with an error
    query string — never a 4xx HTML response, because browsers
    treat 4xx HTML as "the form might be poisoned" and some
    block password-manager fill on it."""
    user = auth.authenticate(email, password)
    if user is None:
        return RedirectResponse(
            url="/admin/login?error=invalid",
            status_code=303,
        )
    response = RedirectResponse(url="/admin", status_code=303)
    auth.issue_session_cookie(response, user.id)
    auth.write_audit(user, "admin.login", details=f"ip={request.client.host if request.client else ''}")
    return response


@router.post("/admin/logout")
def logout(request: Request):
    response = RedirectResponse(url="/admin/login", status_code=303)
    auth.clear_session_cookie(response)
    admin = auth.maybe_current_admin(request)
    if admin is not None:
        auth.write_audit(admin, "admin.logout")
    return response


@router.get("/admin", response_class=HTMLResponse)
def admin_shell(request: Request):
    """Dashboard SPA shell. Served only to authenticated admins;
    anyone else gets bounced to /admin/login."""
    if auth.maybe_current_admin(request) is None:
        return RedirectResponse(url="/admin/login", status_code=302)
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "brand": "NP Create Admin"},
    )
