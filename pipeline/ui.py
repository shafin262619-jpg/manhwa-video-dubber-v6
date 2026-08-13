"""Shared HTML chrome for the web UI (UI1 visual polish).

Small helpers so every page renders with the same stylesheet link and a
consistent header/nav. Pure presentation only - no behavior, no logic.
"""

STYLESHEET_HREF = "/static/style.css"
FONTS_HREF = (
    "https://fonts.googleapis.com/css2?"
    "family=Baloo+Da+2:wght@500;600;700;800&"
    "family=Hind+Siliguri:wght@400;500;600;700&"
    "family=IBM+Plex+Mono:wght@400;500&display=swap"
)


def page_head(title):
    """Open the HTML document with the shared fonts + stylesheet linked.

    Same font stack and design tokens as BlueprintTube (UI2): Baloo Da 2 /
    Hind Siliguri / IBM Plex Mono via Google Fonts, so the two sibling
    pipelines share one visual identity.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="{FONTS_HREF}" rel="stylesheet">
  <link rel="stylesheet" href="{STYLESHEET_HREF}">
</head>
<body>
<div class="grain"></div>
"""


def site_header(page_title):
    """Shared header: home link + page title, then the main content wrapper."""
    return f"""<header class="site-header">
  <a class="brand" href="/">Manhwa Video Dubber</a>
  <span class="page-title">{page_title}</span>
</header>
<main class="site-content">
"""


def page(title, body):
    """Full HTML document: shared head + header + body in a main wrapper."""
    return (
        page_head(title)
        + site_header(title)
        + body
        + "\n</main>\n</body>\n</html>\n"
    )
