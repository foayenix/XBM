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
def test_rows_stay_readable_at_any_width(page, width, label):
    """A row squeezed under ~200px wraps body text one character per line."""
    page.set_viewport_size({"width": width, "height": 850})
    page.wait_for_timeout(250)
    row_width = page.evaluate(
        "() => document.querySelector('.row').getBoundingClientRect().width"
    )
    assert row_width >= min(280, width - 80), (
        f"{label} ({width}px): row is only {row_width:.0f}px wide"
    )


@pytest.mark.parametrize("selector", [".row", ".topbar"])
@pytest.mark.parametrize("width", [375, 430, 1280])
def test_visual_order_matches_dom_order(page, selector, width):
    """CSS `order` desyncs what is read aloud from what is seen, and makes
    Tab jump around the screen. Neither container uses it."""
    page.set_viewport_size({"width": width, "height": 850})
    page.wait_for_timeout(300)
    mismatches = page.evaluate("""(sel) => {
      const box = document.querySelector(sel);
      const parts = [...box.children].filter(c => c.offsetParent !== null);
      // Items on one line rarely share an exact `top` — they are centred and
      // have different heights — so "same line" means their vertical ranges
      // overlap, and only then does left-to-right decide.
      const visual = [...parts].sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        const sameLine = ra.bottom > rb.top + 1 && rb.bottom > ra.top + 1;
        return sameLine ? ra.left - rb.left : ra.top - rb.top;
      });
      return parts.map((p, i) => p === visual[i] ? null : (p.className || p.tagName)).filter(Boolean);
    }""", selector)
    assert not mismatches, f"{selector} at {width}px renders out of DOM order: {mismatches}"


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
    open_watch_later(page)
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


def test_the_inactive_view_is_actually_hidden(page):
    """`display: flex` on .view can override the hidden attribute's
    `display: none`, leaving both views stacked on screen at once."""
    assert page.locator("#watch-later-view").is_visible() is False
    assert page.locator(".wl-filters").is_visible() is False

    page.click(".nav-item[data-view='watch-later']")
    page.wait_for_timeout(400)
    assert page.locator("#search-view").is_visible() is False
    assert page.locator("#watch-later-view").is_visible() is True


def test_only_one_view_worth_of_rows_is_visible(page):
    page.wait_for_timeout(300)
    visible = page.evaluate(
        """() => [...document.querySelectorAll('.row')]
             .filter(r => r.offsetParent !== null).length"""
    )
    total = page.evaluate("() => document.querySelectorAll('.row').length")
    assert visible == total, f"{total - visible} rows from the inactive view are on screen"


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
        "() => getComputedStyle(document.querySelector('.row')).transitionDuration"
    )
    assert any(float(d.rstrip("s")) > 0 for d in duration.split(", ")), (
        "no transition on .row - state changes snap instantly"
    )


def test_reduced_motion_is_respected(browser, live_server):
    p = browser.new_page(viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
    try:
        p.goto(live_server, wait_until="networkidle")
        p.wait_for_timeout(200)
        duration = p.evaluate(
            "() => getComputedStyle(document.querySelector('.row')).transitionDuration"
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


@pytest.mark.parametrize("element_id", ["results-status", "sync-status"])
def test_errors_do_not_look_like_ordinary_status(page, element_id):
    normal = page.evaluate("""(id) => { const el=document.getElementById(id);
        setStatus(el, '75 bookmarks'); return getComputedStyle(el).color; }""", element_id)
    error = page.evaluate("""(id) => { const el=document.getElementById(id);
        setStatus(el, 'Error: rate limited', {isError: true}); return getComputedStyle(el).color; }""", element_id)
    assert normal != error, f"#{element_id}: error and normal status render identically"


def test_a_failed_search_says_so_in_the_list(page):
    """The empty/error state is the visible channel, not just aria-live."""
    colour = page.evaluate("""() => {
      const c = document.getElementById('results');
      showEmpty(c, 'Error: rate limited', {isError: true});
      return getComputedStyle(c.querySelector('.empty')).color;
    }""")
    plain = page.evaluate("""() => {
      const c = document.getElementById('results');
      showEmpty(c, 'no matches');
      return getComputedStyle(c.querySelector('.empty')).color;
    }""")
    assert colour != plain


# --- the Console direction's own promises --------------------------------

def open_watch_later(page):
    """Open the queue on the `all` filter.

    The module-scoped database is shared, so a test that toggles an item
    would otherwise empty the default `unwatched` view for whatever runs
    next. `all` is stable whichever way earlier tests left things.
    """
    page.click(".nav-item[data-view='watch-later']")
    page.wait_for_timeout(300)
    page.click(".wl-filter[data-status='']")
    page.wait_for_timeout(500)


def test_every_row_carries_a_content_type_badge(page):
    kinds = page.evaluate(
        """() => [...document.querySelectorAll('.row')].map(r => {
             const b = r.querySelector('.kind');
             return b ? b.textContent.trim() : null; })"""
    )
    assert kinds and all(k in ("THR", "VID", "ART", "TWT") for k in kinds), kinds


def test_the_rail_shows_live_counts(page):
    counts = page.evaluate(
        """() => ({all: document.getElementById('count-all').textContent,
                  watch: document.getElementById('count-watch').textContent})"""
    )
    assert counts["all"].isdigit() and int(counts["all"]) > 0
    assert counts["watch"].isdigit()


def test_slash_focuses_the_search_box(page):
    page.keyboard.press("/")
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.activeElement.id") == "search-input"


def test_typing_a_slash_in_the_box_does_not_hijack_it(page):
    page.click("#search-input")
    page.keyboard.type("a/b")
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.getElementById('search-input').value") == "a/b"


def test_ctrl_k_reaches_the_search_box(page):
    page.keyboard.press("Control+k")
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.activeElement.id") == "search-input"


def test_j_and_k_walk_the_rows(page):
    page.keyboard.press("j")
    page.wait_for_timeout(120)
    first = page.evaluate("() => document.activeElement.dataset.id")
    assert first, "j did not land on a row"

    page.keyboard.press("j")
    page.wait_for_timeout(120)
    second = page.evaluate("() => document.activeElement.dataset.id")
    assert second and second != first, "j did not advance"

    page.keyboard.press("k")
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.activeElement.dataset.id") == first, "k did not go back"


def test_j_stops_at_the_end_instead_of_wrapping_or_erroring(page):
    for _ in range(12):
        page.keyboard.press("j")
    page.wait_for_timeout(200)
    assert page.evaluate("() => !!document.activeElement.dataset.id")


def test_shortcuts_do_not_fire_while_typing(page):
    page.click("#search-input")
    page.keyboard.type("jkw")
    page.wait_for_timeout(120)
    assert page.evaluate("() => document.activeElement.id") == "search-input"
    assert page.evaluate("() => document.getElementById('search-input').value") == "jkw"


def test_w_toggles_watch_state_on_the_focused_row(page):
    open_watch_later(page)
    before = page.evaluate(
        """() => document.querySelector('.row .watch-toggle').getAttribute('aria-pressed')"""
    )
    page.keyboard.press("j")
    page.wait_for_timeout(150)
    page.keyboard.press("w")
    page.wait_for_timeout(700)
    after = page.evaluate(
        """() => { const t = document.querySelector('.row .watch-toggle');
                   return t ? t.getAttribute('aria-pressed') : 'row-gone'; }"""
    )
    assert after != before, f"w did not change watch state ({before} -> {after})"


def test_the_watch_toggle_does_not_navigate_the_row(page):
    """The row is a link; its toggle must not also open X."""
    open_watch_later(page)
    page.click(".row .watch-toggle")
    page.wait_for_timeout(600)
    assert "/api" not in page.url and "x.com" not in page.url


def test_advertised_shortcuts_all_exist(page):
    """The status bar promises these; a promise in the UI has to be real."""
    advertised = page.evaluate(
        """() => [...document.querySelectorAll('.statusbar .shortcut kbd')].map(k => k.textContent.trim())"""
    )
    assert set(advertised) == {"j", "k", "o", "w", "/"}, advertised
