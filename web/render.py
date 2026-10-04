"""
web/render.py — Jinja2 template engine setup and the shared render() helper.

Extracted from web/app.py so that other web/ modules (security.py, and
potentially others) can render error pages without creating a circular
import back into app.py. This module has no dependencies on the rest of
web/ — only on Jinja2 and info.py's branding constants — so anything in
web/ can safely import from here.
"""

import os

from jinja2 import Environment, FileSystemLoader, select_autoescape

from info import SITE_NAME, SITE_TAGLINE, CREATOR_NAME

_templates_dir = os.path.join(os.path.dirname(__file__), "templates")
jinja_env = Environment(
    loader=FileSystemLoader(_templates_dir),
    autoescape=select_autoescape(["html"]),
)
# Branding available in EVERY template without passing it each time.
jinja_env.globals["site_name"] = SITE_NAME
jinja_env.globals["site_tagline"] = SITE_TAGLINE
jinja_env.globals["creator_name"] = CREATOR_NAME
# bot_username is set properly once create_app() runs (see web/app.py) — it
# needs the live Bot instance, which isn't available at module import time.
# Default to empty string here purely so Jinja never raises UndefinedError
# if a template renders before create_app() sets it.
jinja_env.globals["bot_username"] = ""


def render(template_name: str, **ctx) -> str:
    return jinja_env.get_template(template_name).render(**ctx)
