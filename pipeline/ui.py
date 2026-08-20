"""Shared HTML chrome for the web UI (F13 "control deck" redesign).

Small helpers so every page renders with the same stylesheet link and a
consistent left-rail nav + header. Pure presentation only - no behavior,
no logic.
"""

STYLESHEET_HREF = "/static/style.css"
FONTS_HREF = (
    "https://fonts.googleapis.com/css2?"
    "family=Baloo+Da+2:wght@500;600;700;800&"
    "family=Hind+Siliguri:wght@400;500;600;700&"
    "family=IBM+Plex+Mono:wght@400;500&display=swap"
)

# (route, label, small inline icon) - kept as plain geometric SVGs so the
# nav never depends on an icon-font CDN.
_NAV_ITEMS = [
    (
        "home",
        "/",
        "Home",
        '<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 9.5L10 4l7 5.5" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M5 8.5V16a1 1 0 0 0 1 1h3v-4.5h2V17h3a1 1 0 0 0 1-1V8.5" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>',
    ),
    (
        "history",
        "/history",
        "History",
        '<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.6"/>'
        '<path d="M10 6v4l3 2" stroke="currentColor" stroke-width="1.6" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>',
    ),
    (
        "settings",
        "/settings",
        "Settings",
        '<svg viewBox="0 0 20 20" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 6h8M14 6h3M3 14h3M9 14h8" stroke="currentColor" '
        'stroke-width="1.6" stroke-linecap="round"/>'
        '<circle cx="11" cy="6" r="2" stroke="currentColor" stroke-width="1.6"/>'
        '<circle cx="6" cy="14" r="2" stroke="currentColor" stroke-width="1.6"/>'
        "</svg>",
    ),
]


def _sidebar(active=None):
    """Left rail: brand + tally dot, then Home / History / Settings.

    Collapses to a horizontal top bar on narrow screens (pure CSS, no JS).
    """
    links = []
    for key, href, label, icon in _NAV_ITEMS:
        active_class = " is-active" if key == active else ""
        links.append(
            f'<a class="side-link{active_class}" href="{href}">'
            f'<span class="side-icon">{icon}</span>'
            f'<span class="side-label">{label}</span></a>'
        )
    nav = "\n    ".join(links)
    return f"""<aside class="app-sidebar">
  <a class="side-brand" href="/">
    <span class="side-brand-dot" aria-hidden="true"></span>
    <span>Manhwa Video Dubber</span>
  </a>
  <nav class="side-nav">
    {nav}
  </nav>
</aside>
"""


def page_head(title, active=None):
    """Open the HTML document, link shared fonts + stylesheet, and open
    the app shell (sidebar + main column)."""
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
<div class="app-shell">
{_sidebar(active)}<div class="app-main">
"""


def site_header(page_title):
    """Slim in-column top bar (page title only - nav lives in the sidebar
    now), then open the content well."""
    return f"""<header class="site-header">
  <span class="page-title">{page_title}</span>
</header>
<main class="site-content">
"""


def page(title, body, active=None):
    """Full HTML document: shared head + sidebar + header + body in a main
    wrapper.

    F10.5: every page ends with a consistent back/home nav so users never
    get stranded - "আগের পাতায় যান" (``history.back()``) first, then
    "হোমে যান".

    F15 Part 4D: every page also includes the key-limit modal (a system-wide
    overlay that appears when any job is in ``api_limit_wait``) — built into
    the shared layout so the modal works on every page without per-page wiring.
    """
    return (
        page_head(title, active)
        + site_header(title)
        + body
        + '\n<nav class="page-nav">\n'
        '  <a href="javascript:history.back()">আগের পাতায় যান</a>\n'
        '  <span class="page-nav-sep" aria-hidden="true">·</span>\n'
        '  <a href="/">হোমে যান</a>\n'
        "</nav>\n"
        + _key_limit_modal()
        + "\n</main>\n"
        + "</div><!-- /app-main -->\n"
        + "</div><!-- /app-shell -->\n"
        + "</body>\n</html>\n"
    )


def _key_limit_modal():
    """System-wide API key-limit warning modal (F15 Part 4D).

    Injected into every page. A small JS poll of ``/api/history`` (which now
    carries each job's ``api_limit_wait`` block and waiting-segment keys)
    shows the overlay whenever ANY job is waiting out an API rate limit —
    top-level stage waits and per-segment QA-gate waits alike. It stays
    visible until the condition clears (the next poll auto-hides it when no
    job is waiting — never a timer) or the user closes it.
    """
    return """
  <div id="key-limit-modal" class="key-limit-modal" hidden>
    <div class="key-limit-modal-box">
      <p class="wait-banner-title">সব জেমিনি API কী-এর দৈনিক কোটার সীমা পূর্ণ</p>
      <p>এক বা একাধিক কাজ API কোটার সীমার কারণে অপেক্ষায় আছে — কাজগুলো
      কোটার রিসেটের পরে (বা নতুন কী যোগ করার সাথে সাথে) স্বয়ংক্রিয়ভাবে
      আবার শুরু হবে।</p>
      <ul id="key-limit-modal-list"></ul>
      <button type="button" id="key-limit-modal-close">বুঝেছি</button>
    </div>
  </div>
  <script>
    var keyLimitDismissed = false;
    function keyLimitWaiting() {
      return fetch('/api/history')
        .then(function (r) { return r.json(); })
        .then(function (data) {
          return (data.history || []).filter(function (e) {
            return (e.api_limit_wait && typeof e.api_limit_wait === 'object') ||
                   (e.api_limit_wait_segments || []).length > 0;
          });
        })
        .catch(function () { return []; });
    }
    function renderKeyLimitModal(entries) {
      var modal = document.getElementById('key-limit-modal');
      if (!modal) return;
      if (!entries.length) {
        modal.hidden = true;
        keyLimitDismissed = false;
        return;
      }
      if (keyLimitDismissed) return;
      var list = document.getElementById('key-limit-modal-list');
      list.innerHTML = '';
      entries.forEach(function (e) {
        var li = document.createElement('li');
        var block = e.api_limit_wait || {};
        var label = 'জব ' + e.job_id;
        if (block.stage) { label += ' — স্টেজ ' + block.stage; }
        if (block.next_retry_at) {
          label += ' (পরবর্তী চেষ্টা: ' + block.next_retry_at + ' UTC)';
        }
        if ((e.api_limit_wait_segments || []).length) {
          label += ' — ' + e.api_limit_wait_segments.length + 'টি সেগমেন্ট অপেক্ষায়';
        }
        li.textContent = label;
        list.appendChild(li);
      });
      modal.hidden = false;
    }
    function keyLimitPoll() {
      keyLimitWaiting().then(function (entries) {
        renderKeyLimitModal(entries);
        setTimeout(keyLimitPoll, 15000);
      });
    }
    document.addEventListener('DOMContentLoaded', function () {
      var closeBtn = document.getElementById('key-limit-modal-close');
      if (closeBtn) {
        closeBtn.addEventListener('click', function () {
          keyLimitDismissed = true;
          document.getElementById('key-limit-modal').hidden = true;
        });
      }
      keyLimitPoll();
    });
  </script>"""
