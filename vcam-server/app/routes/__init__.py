"""Route modules grouped by resource.

* ``admin_*`` — gated behind ``current_admin`` dependency.
* ``public_*`` — exposed to customer-side ``vcam-pc`` over the
  public internet. No auth, but every payload is bound to a
  specific license key (Ed25519-signed) where mutation matters.
* ``ui`` — the HTML pages (login + dashboard shell). API routes
  return JSON; UI routes return HTML.
"""
from __future__ import annotations
