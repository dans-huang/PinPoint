from __future__ import annotations

import html
import re
from urllib.parse import urlencode


INVITATION_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def invitation_code_is_valid(code: str) -> bool:
    return bool(INVITATION_CODE_PATTERN.fullmatch(code))


def custom_scheme_activation_url(code: str) -> str:
    if not invitation_code_is_valid(code):
        raise ValueError("Invitation code is invalid")
    return "pinpoint://invite?" + urlencode({"code": code})


def public_activation_url(invite_base_url: str, code: str) -> str:
    if not invitation_code_is_valid(code):
        raise ValueError("Invitation code is invalid")
    return invite_base_url + "?" + urlencode({"code": code})


def apple_app_site_association(apple_app_id: str) -> dict[str, object]:
    return {
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appIDs": [apple_app_id],
                    "components": [
                        {
                            "/": "/invite",
                            "comment": "Open one PinPoint invitation.",
                        }
                    ],
                }
            ],
        }
    }


def invitation_landing_html(code: str) -> str:
    """Render a no-script custom-scheme fallback for a Universal Link.

    The raw code is present only in the button target. It is deliberately not
    printed into visible page copy, analytics, scripts, or external resources.
    """
    custom_url = html.escape(custom_scheme_activation_url(code), quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>Open PinPoint</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }}
    body {{ min-height: 100vh; margin: 0; display: grid; place-items: center; background: #f5f5f2; color: #171717; }}
    main {{ width: min(30rem, calc(100% - 3rem)); }}
    .mark {{ width: 2.75rem; height: 2.75rem; display: grid; place-items: center; border-radius: 0.85rem; background: #171717; color: #fff; font-size: 1.35rem; }}
    h1 {{ margin: 1.5rem 0 0.65rem; font-size: clamp(2rem, 8vw, 3.4rem); line-height: 0.98; letter-spacing: -0.055em; }}
    p {{ margin: 0; max-width: 27rem; color: #62625d; font-size: 1.03rem; line-height: 1.55; }}
    a {{ display: inline-flex; min-height: 3rem; align-items: center; justify-content: center; margin-top: 1.75rem; padding: 0 1.2rem; border-radius: 999px; background: #171717; color: #fff; font-weight: 650; text-decoration: none; }}
    a:focus-visible {{ outline: 3px solid #7c5cff; outline-offset: 3px; }}
    small {{ display: block; margin-top: 1.1rem; color: #777772; line-height: 1.45; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #151515; color: #f5f5f2; }}
      .mark, a {{ background: #f5f5f2; color: #171717; }}
      p, small {{ color: #aaa9a2; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="mark" aria-hidden="true">P</div>
    <h1>Invitation ready.</h1>
    <p>Open PinPoint on your Apple silicon Mac to finish setup.</p>
    <a href="{custom_url}">Open PinPoint</a>
    <small>If nothing happens, make sure PinPoint is installed, then open this link again.</small>
  </main>
</body>
</html>"""
