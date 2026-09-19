"""Browser-level UI checks.

These cover the things a static scan and an API test both miss: what the
page actually measures once it is laid out. Each assertion here maps to a
defect found by auditing the rendered page --- a 220px sidebar with no
media query that forced horizontal scroll (and one-character-per-line body
text) on a phone, interactive targets as small as 15px tall, a
placeholder-only search label, toggle state carried only by a CSS class,
11.2px badge text, line-height left at browser default, and no transitions
at all.

Skipped automatically when Playwright or a browser binary is unavailable,
so the rest of the suite still runs anywhere.
"""
import os
import socket
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed"
)

# Interactive controls must be reachable by a finger.
MIN_TARGET = 44
# WCAG AA for normal-size text.
MIN_CONTRAST = 4.5
# Below this, body text stops being comfortably readable.
MIN_FONT_PX = 12
MIN_LINE_HEIGHT = 1.5

VIEWPORTS = [(320, "phone-sm"), (375, "phone"), (430, "phone-lg"), (834, "tablet"), (1280, "desktop")]

CANDIDATE_BROWSERS = [
    os.environ.get("XBM_TEST_CHROMIUM", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        last_error = None
        for path in CANDIDATE_BROWSERS + [None]:
            if path == "":
                continue
            try:
                b = p.chromium.launch(**({"executable_path": path} if path else {}))
            except Exception as e:  # no browser binary installed
                last_error = e
                continue
            yield b
            b.close()
            return
        pytest.skip(f"no chromium binary available: {last_error}")


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A real uvicorn server over a seeded database."""
    import uvicorn

    from backend import config

    tmp = tmp_path_factory.mktemp("ui")
    config.DATABASE_PATH = tmp / "ui.sqlite3"
    config.LOG_DIR = tmp / "logs"

    from scripts.init_db import init_db

    init_db()

    from datetime import datetime, timezone

    from backend import db

    conn = db.get_connection()
    now = datetime.now(timezone.utc).isoformat()
    seed_rows = [
        (1, "alice", "Alice Chen",
         "Great thread on why SQLite is underrated for local-first apps. Covers WAL "
         "mode, FTS5, and the gotchas around concurrent writers.", 1),
        (2, "bob", "Bob Nguyen", "New video: building a bookmarks tool end to end", 0),
        (3, "carol", "Carol", "Short one.", 0),
    ]
    for i, (bid, user, name, text, is_thread) in enumerate(seed_rows):
        conn.execute(
            """INSERT INTO bookmarks (id, author_id, author_username, author_name, text,
               created_at, media_urls, external_links, is_thread, thread_text, synced_at)
               VALUES (?,?,?,?,?,?,'[]','[]',?,?,?)""",
            (bid, f"a{bid}", user, name, text, f"2026-0{i + 1}-0{i + 1}T00:00:00Z",
             is_thread, "full thread" if is_thread else None, now),
        )
    for tid, tag in enumerate(["sqlite", "local-first", "video", "a-very-long-tag-name-here"], start=1):
        conn.execute("INSERT INTO tags (id, name) VALUES (?, ?)", (tid, tag))
        conn.execute("INSERT INTO bookmark_tags (bookmark_id, tag_id) VALUES (?, ?)", ((tid % 3) + 1, tid))
    # A YouTube link gives us a poster-frame thumbnail that will fail to load
    # offline - exactly the dead-thumbnail case.
    conn.execute(
        "INSERT INTO linked_content (bookmark_id, type, url, status, transcript_or_summary)"
        " VALUES (2,'youtube','https://youtu.be/dQw4w9WgXcQ','done','t')"
    )
    conn.execute("INSERT INTO watch_later (bookmark_id, status, added_at) VALUES (2,'unwatched',?)", (now,))
    conn.commit()
    conn.close()

    from backend.main import app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield base
    server.should_exit = True


@pytest.fixture
def page(browser, live_server):
    p = browser.new_page(viewport={"width": 1280, "height": 900})
    p.goto(live_server, wait_until="networkidle")
    p.wait_for_timeout(250)
    yield p
    p.close()


CONTRAST_JS = Path(__file__).parent.joinpath("contrast.js").read_text() if \
    Path(__file__).parent.joinpath("contrast.js").exists() else """
() => {
  const lum = c => { const [r,g,b]=c.map(v=>{v/=255; return v<=0.03928? v/12.92 : Math.pow((v+0.055)/1.055,2.4);});
                     return 0.2126*r+0.7152*g+0.0722*b; };
  const parse = s => { const m=s.match(/\\d+(\\.\\d+)?/g); return m? m.slice(0,3).map(Number):null; };
  const bgOf = el => { let n=el; while(n && n!==document.documentElement){ const b=getComputedStyle(n).backgroundColor;
                       const p=parse(b); if(p && !/rgba\\(.*,\\s*0\\)/.test(b)) return p; n=n.parentElement;} return [255,255,255]; };
  const ratio=(a,b)=>{const l1=lum(a),l2=lum(b);return ((Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05));};
  const out=[];
  document.querySelectorAll('button,a,p,span,h1,h2,input,li,label,mark,.badge,.tag-chip').forEach(el=>{
    const t=(el.innerText||el.value||el.placeholder||'').trim(); if(!t) return;
    const cs=getComputedStyle(el); const fg=parse(cs.color); if(!fg) return;
    const fs=parseFloat(cs.fontSize); const bold=parseInt(cs.fontWeight)>=700;
    const large = fs>=24 || (fs>=18.66 && bold);
    out.push({sel: el.className||el.tagName.toLowerCase(), text:t.slice(0,30),
              ratio:+ratio(fg,bgOf(el)).toFixed(2), need: large?3:4.5});
  });
  return out;
}
"""

TARGETS_JS = """
() => [...document.querySelectorAll('button, a[href], input')]
  .map(el => { const r = el.getBoundingClientRect();
               return {sel: el.className || el.tagName.toLowerCase(),
                       text: (el.innerText || el.placeholder || '').trim().slice(0,30),
                       w: r.width, h: r.height}; })
  .filter(t => t.w > 0)
"""


# --- responsive ------------------------------------------------------------

@pytest.mark.parametrize("width,label", VIEWPORTS)
def test_no_horizontal_scroll_at_any_width(page, width, label):
    page.set_viewport_size({"width": width, "height": 850})
    page.wait_for_timeout(250)
    metrics = page.evaluate(
        "() => ({scrollW: document.documentElement.scrollWidth,"
        " clientW: document.documentElement.clientWidth})"
    )
    assert metrics["scrollW"] <= metrics["clientW"], (
        f"{label} ({width}px) scrolls horizontally: "
        f"content {metrics['scrollW']}px in a {metrics['clientW']}px viewport"
    )


@pytest.mark.parametrize("width,label", VIEWPORTS)
def test_cards_stay_readable_at_any_width(page, width, label):
    """A card squeezed under ~200px wraps body text one character per line."""
    page.set_viewport_size({"width": width, "height": 850})
    page.wait_for_timeout(250)
    card_width = page.evaluate(
        "() => document.querySelector('.card').getBoundingClientRect().width"
    )
    assert card_width >= min(280, width - 80), (
        f"{label} ({width}px): card is only {card_width:.0f}px wide"
    )


def test_the_sidebar_stops_being_a_fixed_column_on_phones(page):
    page.set_viewport_size({"width": 375, "height": 850})
    page.wait_for_timeout(250)
    sidebar = page.evaluate("() => document.querySelector('.sidebar').getBoundingClientRect().width")
    assert sidebar > 300, f"sidebar still a narrow fixed column at 375px ({sidebar:.0f}px)"


# --- touch targets ---------------------------------------------------------

def test_every_control_meets_the_touch_target_minimum(page):
    too_small = [
        t for t in page.evaluate(TARGETS_JS)
        if t["h"] < MIN_TARGET or t["w"] < MIN_TARGET
    ]
    assert not too_small, "controls under 44x44: " + ", ".join(
        f"{t['sel']} {t['text']!r} {t['w']:.0f}x{t['h']:.0f}" for t in too_small
    )


def test_watch_later_controls_also_meet_the_minimum(page):
    page.click(".nav-item[data-view='watch-later']")
    page.wait_for_timeout(400)
    too_small = [t for t in page.evaluate(TARGETS_JS) if t["h"] < MIN_TARGET or t["w"] < MIN_TARGET]
    assert not too_small, "controls under 44x44: " + ", ".join(
        f"{t['sel']} {t['text']!r} {t['w']:.0f}x{t['h']:.0f}" for t in too_small
    )


# --- contrast --------------------------------------------------------------

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_meets_wcag_aa_in_both_themes(browser, live_server, scheme):
    p = browser.new_page(viewport={"width": 1280, "height": 900}, color_scheme=scheme)
    try:
        p.goto(live_server, wait_until="networkidle")
        p.wait_for_timeout(250)
        fails = [r for r in p.evaluate(CONTRAST_JS) if r["ratio"] < r["need"]]
        assert not fails, f"{scheme} contrast failures: " + ", ".join(
            f"{r['sel']} {r['text']!r} {r['ratio']}:1 (need {r['need']})" for r in fails
        )
    finally:
        p.close()


# --- typography ------------------------------------------------------------

def test_no_text_is_smaller_than_the_floor(page):
    small = page.evaluate("""() => {
      const out=[];
      document.querySelectorAll('*').forEach(el=>{
        if(!el.innerText || !el.innerText.trim()) return;
        const fs=parseFloat(getComputedStyle(el).fontSize);
        if(fs < %d) out.push((el.className||el.tagName)+' '+fs+'px');
      });
      return out;
    }""" % MIN_FONT_PX)
    assert not small, f"text under {MIN_FONT_PX}px: {small}"


def test_text_uses_a_readable_line_height(page):
    tight = page.evaluate("""() => {
      const out=[];
      document.querySelectorAll('p, .text, .nav-item, .status-line, li, label').forEach(el=>{
        if(!el.innerText || !el.innerText.trim()) return;
        const cs=getComputedStyle(el); const fs=parseFloat(cs.fontSize); const lh=parseFloat(cs.lineHeight);
        if(!isNaN(lh) && lh/fs < %s) out.push((el.className||el.tagName)+' '+(lh/fs).toFixed(2));
      });
      return out;
    }""" % MIN_LINE_HEIGHT)
    assert not tight, f"line-height below {MIN_LINE_HEIGHT}: {tight}"


# --- accessibility semantics ----------------------------------------------

def test_the_search_input_has_a_real_label(page):
    info = page.evaluate("""() => {
      const el=document.getElementById('search-input');
      const lab=document.querySelector('label[for="search-input"]');
      return {label: lab ? lab.innerText.trim() : null, ariaLabel: el.getAttribute('aria-label')};
    }""")
    assert info["label"] or info["ariaLabel"], "search input is placeholder-only"


def test_toggle_state_is_exposed_to_assistive_tech(page):
    """The blue pill is visual only; state has to be in the accessibility tree."""
    counts = page.evaluate("""() => ({
      navTotal: document.querySelectorAll('.nav-item').length,
      navCurrent: document.querySelectorAll('.nav-item[aria-current="page"]').length,
      tagsTotal: document.querySelectorAll('.tag-filter').length,
      tagsWithState: document.querySelectorAll('.tag-filter[aria-pressed]').length,
    })""")
    assert counts["navCurrent"] == 1, "exactly one nav item should be aria-current"
    assert counts["tagsWithState"] == counts["tagsTotal"] > 0


def test_aria_current_follows_the_active_view(page):
    page.click(".nav-item[data-view='watch-later']")
    page.wait_for_timeout(300)
    active = page.evaluate(
        """() => document.querySelector('.nav-item[aria-current="page"]').dataset.view"""
    )
    assert active == "watch-later"


def test_aria_pressed_follows_tag_selection(page):
    page.click(".tag-filter")
    page.wait_for_timeout(400)
    pressed = page.evaluate(
        """() => document.querySelectorAll('.tag-filter[aria-pressed="true"]').length"""
    )
    assert pressed == 1


def test_watch_later_filters_expose_pressed_state(page):
    page.click(".nav-item[data-view='watch-later']")
    page.wait_for_timeout(300)
    page.click(".wl-filter[data-status='watched']")
    page.wait_for_timeout(300)
    state = page.evaluate("""() => [...document.querySelectorAll('.wl-filter')]
        .map(b => [b.dataset.status, b.getAttribute('aria-pressed')])""")
    assert dict(state)["watched"] == "true"
    assert dict(state)["unwatched"] == "false"


def test_result_counts_are_announced(page):
    live = page.evaluate("""() => [...document.querySelectorAll('[aria-live]')].map(e => e.id)""")
    assert "results-status" in live
    assert "sync-status" in live


def test_keyboard_focus_is_clearly_visible(page):
    page.keyboard.press("Tab")
    outline = page.evaluate("""() => { const cs=getComputedStyle(document.activeElement);
        return {style: cs.outlineStyle, width: parseFloat(cs.outlineWidth)}; }""")
    assert outline["style"] != "none" and outline["width"] >= 2, (
        f"focus ring too weak: {outline}"
    )


# --- motion ----------------------------------------------------------------

def test_state_changes_are_not_instant(page):
    duration = page.evaluate(
        "() => getComputedStyle(document.querySelector('.card')).transitionDuration"
    )
    assert any(float(d.rstrip("s")) > 0 for d in duration.split(", ")), (
        "no transition on .card - state changes snap instantly"
    )


def test_reduced_motion_is_respected(browser, live_server):
    p = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    try:
        p.goto(live_server, wait_until="networkidle")
        p.wait_for_timeout(200)
        duration = p.evaluate(
            "() => getComputedStyle(document.querySelector('.card')).transitionDuration"
        )
        assert all(float(d.rstrip("s")) < 0.01 for d in duration.split(", ")), duration
    finally:
        p.close()


# --- resilience ------------------------------------------------------------

def test_a_dead_thumbnail_leaves_no_broken_image(page):
    """YouTube poster frames vanish when a video is deleted."""
    page.wait_for_timeout(700)
    broken = page.evaluate(
        "() => [...document.images].filter(i => i.complete && i.naturalWidth === 0).length"
    )
    assert broken == 0, f"{broken} broken-image icon(s) visible"


def test_errors_do_not_look_like_ordinary_status(page):
    normal = page.evaluate("""() => { const el=document.getElementById('results-status');
        setStatus(el, '75 bookmarks'); return getComputedStyle(el).color; }""")
    error = page.evaluate("""() => { const el=document.getElementById('results-status');
        setStatus(el, 'Error: rate limited', {isError: true}); return getComputedStyle(el).color; }""")
    assert normal != error, "error and normal status render identically"
