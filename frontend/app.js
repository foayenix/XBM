// "Later" — single-page UI for the X bookmarks library.
//
// All bookmark/tag/author content comes from other people's tweets on X, so
// it's untrusted. Every render path here builds DOM nodes via textContent /
// createElement instead of innerHTML, so nothing in a bookmark can execute
// as HTML/script in this page.

// Non-printable markers the backend wraps around FTS5 match highlights.
const SNIPPET_START = "";
const SNIPPET_END = "";

const TYPE_GLYPHS = { video: "▸", thread: "≡", article: "¶", tweet: "“" };

const state = {
  view: "library", // library | queue | detail
  from: "library", // view to return to from detail
  q: "",
  typeFilter: "all",
  activeTag: null,
  detailId: null,
  stats: null,
  syncing: false,
  syncPhase: "",
  syncMessage: null, // {text, error} shown in the footer after a sync
};

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function api(path, options) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

// ---------- small formatters ----------

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const opts = { month: "short", day: "numeric" };
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString("en-US", opts);
}

function relTime(iso) {
  if (!iso) return "never";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(secs)) return "never";
  if (secs < 90) return "just now";
  if (secs < 3600) return Math.round(secs / 60) + "m ago";
  if (secs < 86400) return Math.round(secs / 3600) + "h ago";
  return Math.round(secs / 86400) + "d ago";
}

function typeLabel(b) {
  if (b.content_type === "thread" && b.thread_count > 1) return `THREAD · ${b.thread_count} posts`;
  return b.content_type.toUpperCase();
}

function xUrl(b) {
  return `https://x.com/${encodeURIComponent(b.author_username)}/status/${b.id}`;
}

function videoUrl(detail) {
  const yt = (detail.linked_content || []).find((l) => l.type === "youtube");
  return yt ? yt.url : null;
}

function initials(name) {
  return (name || "?")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

// Render text that may contain FTS5 highlight markers, turning marked
// spans into <mark> without ever interpreting the text as HTML.
function renderSnippetInto(node, text) {
  node.textContent = "";
  let i = 0;
  while (i < text.length) {
    const start = text.indexOf(SNIPPET_START, i);
    if (start === -1) {
      node.appendChild(document.createTextNode(text.slice(i)));
      return;
    }
    node.appendChild(document.createTextNode(text.slice(i, start)));
    const end = text.indexOf(SNIPPET_END, start + 1);
    if (end === -1) {
      node.appendChild(document.createTextNode(text.slice(start + 1)));
      return;
    }
    const mark = el("mark", null, text.slice(start + 1, end));
    node.appendChild(mark);
    i = end + 1;
  }
}

function thumbEl(src, alt) {
  if (src) {
    const img = el("img", "thumb");
    img.src = src;
    img.alt = alt || "";
    img.loading = "lazy";
    // A dead thumbnail URL degrades to the striped placeholder instead of
    // the browser's broken-image icon.
    img.addEventListener("error", () => img.replaceWith(el("div", "thumb")), { once: true });
    return img;
  }
  return el("div", "thumb");
}

function monoLink(href, text) {
  const a = el("a", "mono-link", text);
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.addEventListener("click", (e) => e.stopPropagation());
  return a;
}

// ---------- sidebar ----------

function renderNav() {
  $("nav-library").classList.toggle("active", state.view !== "queue");
  $("nav-queue").classList.toggle("active", state.view === "queue");
  if (state.stats) {
    $("nav-library-count").textContent = state.stats.total;
    $("nav-queue-count").textContent = state.stats.unwatched;
  }
}

async function loadTags() {
  const tags = await api("/api/tags");
  const list = $("tag-list");
  list.textContent = "";
  if (!tags.length) {
    list.appendChild(el("div", "tags-empty", "no tags yet"));
    return;
  }
  for (const t of tags) {
    const btn = el("button", "tag-btn" + (state.activeTag === t.name ? " active" : ""));
    btn.type = "button";
    btn.appendChild(el("span", null, t.name));
    btn.appendChild(el("span", "count", String(t.count)));
    btn.addEventListener("click", () => {
      state.activeTag = state.activeTag === t.name ? null : t.name;
      setView("library");
      loadTags();
    });
    list.appendChild(btn);
  }
}

function renderTheme() {
  const theme = document.documentElement.dataset.theme;
  $("theme-toggle").textContent = theme === "light" ? "◐ dark mode" : "◐ light mode";
}

function renderSyncArea() {
  const area = $("sync-area");
  area.textContent = "";
  if (state.syncing) {
    area.appendChild(el("div", "sync-label", state.syncPhase));
    const bar = el("div", "sync-bar");
    bar.appendChild(el("div", "sync-bar-fill"));
    area.appendChild(bar);
    return;
  }
  if (state.syncMessage) {
    area.appendChild(
      el("div", "sync-label" + (state.syncMessage.error ? " error" : ""), state.syncMessage.text)
    );
  }
  const row = el("div", "sync-row");
  const when = state.stats ? relTime(state.stats.last_synced_at) : "…";
  row.appendChild(el("span", "sync-when", "synced " + when));
  const btn = el("button", "sync-btn", "sync now");
  btn.type = "button";
  btn.addEventListener("click", syncNow);
  row.appendChild(btn);
  area.appendChild(row);
}

// ---------- data ----------

async function loadStats() {
  state.stats = await api("/api/stats");
  renderNav();
  renderSyncArea();
}

// ---------- library view ----------

function buildRow(b) {
  const row = el("div", "row");
  row.addEventListener("click", () => openDetail(b.id));

  row.appendChild(el("div", "glyph t-" + b.content_type, TYPE_GLYPHS[b.content_type]));

  const main = el("div", "row-main");
  const meta = el("div", "row-meta");
  meta.appendChild(el("span", "author", b.author_name || b.author_username));
  meta.appendChild(el("span", "handle", "@" + b.author_username));
  meta.appendChild(el("span", "fg-" + b.content_type, typeLabel(b)));
  meta.appendChild(el("span", "date", shortDate(b.created_at)));
  main.appendChild(meta);

  if (b.title) main.appendChild(el("div", "row-title", b.title));

  const snippet = el("div", "row-snippet");
  renderSnippetInto(snippet, b.snippet || b.text || "");
  main.appendChild(snippet);

  if (b.tags && b.tags.length) {
    const tagsRow = el("div", "row-tags");
    for (const t of b.tags) {
      const pill = el("button", "tag-pill", t);
      pill.type = "button";
      pill.addEventListener("click", (e) => {
        e.stopPropagation();
        state.activeTag = t;
        setView("library");
        loadTags();
      });
      tagsRow.appendChild(pill);
    }
    main.appendChild(tagsRow);
  }
  row.appendChild(main);

  if (b.content_type === "video" && b.thumbnail) {
    row.appendChild(thumbEl(b.thumbnail));
  } else {
    row.appendChild(el("div"));
  }
  return row;
}

function renderChips(container) {
  container.textContent = "";
  const byType = state.stats ? state.stats.by_type : {};
  const total = state.stats ? state.stats.total : 0;
  const defs = [
    ["all", "All", total],
    ["thread", "Threads", byType.thread || 0],
    ["article", "Articles", byType.article || 0],
    ["video", "Videos", byType.video || 0],
    ["tweet", "Tweets", byType.tweet || 0],
  ];
  for (const [key, label, count] of defs) {
    const chip = el("button", "chip" + (state.typeFilter === key ? " active" : ""));
    chip.type = "button";
    chip.appendChild(el("span", null, label));
    chip.appendChild(el("span", "count", String(count)));
    chip.addEventListener("click", () => {
      state.typeFilter = key;
      refreshLibrary();
    });
    container.appendChild(chip);
  }
  if (state.activeTag) {
    const chip = el("button", "chip chip-tag");
    chip.type = "button";
    chip.appendChild(el("span", null, "tag: " + state.activeTag));
    chip.appendChild(el("span", "count", "×"));
    chip.addEventListener("click", () => {
      state.activeTag = null;
      refreshLibrary();
      loadTags();
    });
    container.appendChild(chip);
  }
  const label = el("div", "result-label", "");
  label.id = "result-label";
  container.appendChild(label);
}

async function refreshLibrary() {
  const chipRow = $("chip-row");
  if (chipRow) renderChips(chipRow);

  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.activeTag) params.append("tag", state.activeTag);
  if (state.typeFilter !== "all") params.set("type", state.typeFilter);
  params.set("limit", "200");

  let data;
  try {
    data = await api("/api/search?" + params.toString());
  } catch (err) {
    data = { results: [], total: 0, error: err.message };
  }

  const label = $("result-label");
  if (label) {
    label.textContent = data.total + " saved · " + (state.q ? "best match first" : "newest first");
  }

  const list = $("result-list");
  if (!list) return;
  list.textContent = "";

  if (!data.results.length) {
    const wrap = el("div", "no-results");
    wrap.appendChild(
      el("div", "no-results-q", data.error
        ? "search error: " + data.error
        : state.q
        ? "no matches for “" + state.q + "”"
        : "no matches")
    );
    wrap.appendChild(el("div", "no-results-hint", "Try fewer words, or browse by tag instead."));
    const clear = el("button", "clear-btn", "clear search & filters");
    clear.type = "button";
    clear.addEventListener("click", () => {
      state.q = "";
      state.typeFilter = "all";
      state.activeTag = null;
      const input = $("search-input");
      if (input) input.value = "";
      refreshLibrary();
      loadTags();
    });
    wrap.appendChild(clear);
    list.appendChild(wrap);
    return;
  }
  for (const b of data.results) list.appendChild(buildRow(b));
}

function showLibrary(main) {
  const view = el("div", "library");

  const header = el("header", "library-header");
  const wrap = el("div", "search-wrap");
  const input = el("input", "search-input");
  input.id = "search-input";
  input.type = "search";
  input.placeholder = "Search tweets, summaries, transcripts…";
  input.autocomplete = "off";
  input.value = state.q;
  input.addEventListener(
    "input",
    debounce(() => {
      state.q = input.value.trim();
      refreshLibrary();
    }, 250)
  );
  wrap.appendChild(input);
  wrap.appendChild(el("span", "search-kbd", "/"));
  header.appendChild(wrap);

  const chipRow = el("div", "chip-row");
  chipRow.id = "chip-row";
  header.appendChild(chipRow);
  view.appendChild(header);

  const list = el("div", "result-list");
  list.id = "result-list";
  view.appendChild(list);

  main.appendChild(view);
  refreshLibrary();
}

// ---------- first-run empty state ----------

function showFirstRun(main) {
  const wrap = el("div", "empty-wrap");
  const box = el("div", "empty");
  box.appendChild(el("div", "empty-quote", "“"));
  box.appendChild(el("div", "empty-title", "Nothing saved here yet."));
  box.appendChild(
    el(
      "div",
      "empty-body",
      "Run your first sync to pull in your X bookmarks — text, threads, article summaries and video transcripts, all searchable."
    )
  );
  if (!state.syncing) {
    const btn = el("button", "empty-cta", "Sync bookmarks");
    btn.type = "button";
    btn.addEventListener("click", syncNow);
    box.appendChild(btn);
  } else {
    box.appendChild(el("div", "empty-note", state.syncPhase));
  }
  box.appendChild(el("div", "empty-note", "reads are billed by x · usually under a minute"));
  wrap.appendChild(box);
  main.appendChild(wrap);
}

// ---------- watch later queue ----------

function queueMeta(v) {
  return (v.author_name || v.author_username) + " · @" + v.author_username;
}

function buildUpNext(v) {
  const card = el("div", "up-next");
  card.addEventListener("click", () => openDetail(v.id));

  const thumbWrap = el("div", "up-next-thumb");
  thumbWrap.appendChild(thumbEl(v.thumbnail));
  thumbWrap.appendChild(el("span", "play-badge", "▸"));
  card.appendChild(thumbWrap);

  const body = el("div");
  body.appendChild(el("div", "up-next-label", "up next"));
  body.appendChild(el("div", "up-next-title", v.title || v.text || ""));
  body.appendChild(
    el("div", "up-next-meta", queueMeta(v) + " · saved " + shortDate(v.created_at))
  );
  if (v.title && v.text) body.appendChild(el("div", "up-next-snippet", v.text));

  const actions = el("div", "up-next-actions");
  const watchBtn = el("button", "watch-btn", "✓ mark watched");
  watchBtn.type = "button";
  watchBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await toggleWatched(v.id);
  });
  actions.appendChild(watchBtn);
  actions.appendChild(monoLink(xUrl(v), "open on x ↗"));
  body.appendChild(actions);
  card.appendChild(body);
  return card;
}

function buildQueueRow(v, num) {
  const watched = v.watch_status === "watched";
  const row = el("div", "queue-row" + (watched ? " watched" : ""));
  row.addEventListener("click", () => openDetail(v.id));

  row.appendChild(el("span", "queue-num", num || ""));

  const check = el("button", "check-btn" + (watched ? " checked" : ""), watched ? "✓" : "");
  check.type = "button";
  check.title = watched ? "Mark unwatched" : "Mark watched";
  check.addEventListener("click", async (e) => {
    e.stopPropagation();
    await toggleWatched(v.id);
  });
  row.appendChild(check);

  row.appendChild(thumbEl(v.thumbnail));

  const main = el("div", "queue-row-main");
  main.appendChild(el("div", "queue-row-title", v.title || v.text || ""));
  main.appendChild(el("div", "queue-row-meta", queueMeta(v)));
  row.appendChild(main);

  row.appendChild(el("span", "queue-row-date", shortDate(v.created_at)));
  return row;
}

async function toggleWatched(id) {
  await api(`/api/watch-later/${id}/toggle`, { method: "POST" });
  await loadStats();
  if (state.view === "queue") render();
  else if (state.view === "detail") render();
}

async function showQueue(main) {
  const scroll = el("div", "queue-scroll");
  const queue = el("div", "queue");
  scroll.appendChild(queue);
  main.appendChild(scroll);

  let items;
  try {
    items = await api("/api/watch-later");
  } catch (err) {
    queue.appendChild(el("div", "no-results-q", "error: " + err.message));
    return;
  }

  const unwatched = items.filter((v) => v.watch_status !== "watched");
  const watched = items.filter((v) => v.watch_status === "watched");

  const head = el("div", "queue-head");
  head.appendChild(el("h1", null, "Watch Later"));
  head.appendChild(el("span", "queue-sub", unwatched.length + " unwatched"));
  queue.appendChild(head);

  if (!items.length) {
    const wrap = el("div", "no-results");
    wrap.appendChild(el("div", "no-results-q", "nothing in the queue"));
    wrap.appendChild(
      el("div", "no-results-hint", "Bookmarked videos land here automatically after a sync.")
    );
    queue.appendChild(wrap);
    return;
  }

  if (unwatched.length) {
    queue.appendChild(buildUpNext(unwatched[0]));
  }
  if (unwatched.length > 1) {
    queue.appendChild(el("div", "queue-section", "in the queue"));
    unwatched.slice(1).forEach((v, i) => {
      queue.appendChild(buildQueueRow(v, String(i + 2).padStart(2, "0")));
    });
  }
  if (watched.length) {
    queue.appendChild(el("div", "queue-section", "watched · " + watched.length));
    for (const v of watched) queue.appendChild(buildQueueRow(v, ""));
  }
}

// ---------- detail view ----------

function openDetail(id) {
  state.detailId = id;
  if (state.view !== "detail") state.from = state.view;
  setView("detail");
}

function buildTagsEditor(detail) {
  const section = el("div", "detail-tags");
  const head = el("div", "detail-tags-head");
  head.appendChild(el("span", "label", "tags"));
  head.appendChild(el("span", "hint", "auto-generated · click × to correct"));
  section.appendChild(head);

  const rowEl = el("div", "detail-tags-row");

  const rebuild = (tags) => {
    rowEl.textContent = "";
    for (const t of tags) {
      const pill = el("span", "tag-edit-pill");
      pill.appendChild(document.createTextNode(t));
      const x = el("button", "tag-remove", "×");
      x.type = "button";
      x.title = "Remove tag";
      x.addEventListener("click", async () => {
        const res = await api(`/api/bookmarks/${detail.id}/tags/${encodeURIComponent(t)}`, {
          method: "DELETE",
        });
        rebuild(res.tags);
        loadTags();
      });
      pill.appendChild(x);
      rowEl.appendChild(pill);
    }
    const input = el("input", "tag-add-input");
    input.placeholder = "add tag…";
    input.addEventListener("keydown", async (e) => {
      if (e.key !== "Enter") return;
      const name = input.value.trim();
      if (!name) return;
      const res = await api(`/api/bookmarks/${detail.id}/tags`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      rebuild(res.tags);
      loadTags();
    });
    rowEl.appendChild(input);
  };

  rebuild(detail.tags || []);
  section.appendChild(rowEl);
  return section;
}

async function showDetail(main) {
  const scroll = el("div", "detail-scroll");
  const box = el("div", "detail");
  scroll.appendChild(box);
  main.appendChild(scroll);

  const back = el("button", "back-btn", "← back");
  back.type = "button";
  back.addEventListener("click", () => setView(state.from || "library"));
  box.appendChild(back);

  let d;
  try {
    d = await api("/api/bookmarks/" + state.detailId);
  } catch (err) {
    box.appendChild(el("div", "no-results-q", "error: " + err.message));
    return;
  }

  const typeRow = el("div", "detail-type");
  typeRow.appendChild(el("div", "glyph t-" + d.content_type, TYPE_GLYPHS[d.content_type]));
  typeRow.appendChild(el("span", "type-label fg-" + d.content_type, typeLabel(d)));
  typeRow.appendChild(el("span", "detail-saved", "saved " + shortDate(d.created_at)));
  box.appendChild(typeRow);

  const authorRow = el("div", "detail-author");
  authorRow.appendChild(el("div", "avatar", initials(d.author_name || d.author_username)));
  const who = el("div");
  who.appendChild(el("div", "name", d.author_name || d.author_username));
  who.appendChild(el("div", "handle", "@" + d.author_username));
  authorRow.appendChild(who);
  authorRow.appendChild(monoLink(xUrl(d), "open on x ↗"));
  box.appendChild(authorRow);

  if (d.title) box.appendChild(el("h1", "detail-title", d.title));

  const article = (d.linked_content || []).find((l) => l.type === "article");
  const youtube = (d.linked_content || []).find((l) => l.type === "youtube");

  if (d.content_type === "video") {
    box.appendChild(el("p", "detail-body-text", d.text));
    const media = el("div", "detail-media");
    const url = videoUrl(d);
    const frame = el(url ? "a" : "div", "video-frame");
    if (url) {
      frame.href = url;
      frame.target = "_blank";
      frame.rel = "noopener noreferrer";
    }
    if (d.thumbnail) frame.appendChild(thumbEl(d.thumbnail));
    frame.appendChild(el("span", "play-badge", "▸"));
    media.appendChild(frame);
    if (youtube && youtube.transcript_or_summary) {
      const card = el("div", "excerpt-card");
      card.appendChild(el("div", "excerpt-label", "transcript excerpt"));
      const excerpt = youtube.transcript_or_summary.slice(0, 700);
      card.appendChild(
        el("p", "italic", "…" + excerpt + (youtube.transcript_or_summary.length > 700 ? "…" : ""))
      );
      media.appendChild(card);
    }
    box.appendChild(media);
  } else if (d.content_type === "thread" && d.thread_parts.length) {
    const list = el("div", "thread-list");
    d.thread_parts.forEach((part, i) => {
      const para = el("div", "thread-para");
      para.appendChild(el("span", "thread-num", String(i + 1).padStart(2, "0")));
      para.appendChild(el("p", null, part));
      list.appendChild(para);
    });
    box.appendChild(list);
  } else if (d.content_type === "article") {
    box.appendChild(el("p", "detail-body-text", d.text));
  } else {
    box.appendChild(el("p", "detail-tweet-text", d.text));
  }

  if (d.content_type !== "video" && d.thumbnail) {
    const img = el("img", "detail-photo");
    img.src = d.thumbnail;
    img.alt = "";
    box.appendChild(img);
  }

  if (article && article.transcript_or_summary) {
    const card = el("div", "excerpt-card");
    card.appendChild(el("div", "excerpt-label", "summary"));
    card.appendChild(el("p", null, article.transcript_or_summary));
    box.appendChild(card);
    const src = el("div", "source-line");
    src.appendChild(document.createTextNode("source: "));
    let domain = article.url;
    try {
      domain = new URL(article.url).hostname.replace(/^www\./, "");
    } catch (_) {}
    src.appendChild(monoLink(article.url, domain + " ↗"));
    box.appendChild(src);
  }

  if (d.watch_status) {
    const actions = el("div", "up-next-actions");
    const watched = d.watch_status === "watched";
    const btn = el("button", "watch-btn", watched ? "↺ mark unwatched" : "✓ mark watched");
    btn.type = "button";
    btn.addEventListener("click", () => toggleWatched(d.id));
    actions.appendChild(btn);
    box.appendChild(actions);
  }

  box.appendChild(buildTagsEditor(d));
}

// ---------- sync ----------

async function syncNow() {
  if (state.syncing) return;
  state.syncing = true;
  state.syncMessage = null;
  state.syncPhase = "syncing bookmarks…";
  renderSyncArea();
  if (state.stats && state.stats.total === 0) render();

  try {
    const sync = await api("/api/sync", { method: "POST" });
    state.syncPhase = "enriching · transcripts, summaries, tags…";
    renderSyncArea();
    if (state.stats && state.stats.total === 0) render();

    const enrich = await api("/api/enrich", { method: "POST" });
    const reads = (sync.bookmark_api_reads || 0) + (sync.thread_expansion_api_reads || 0);
    state.syncMessage = {
      text: `${sync.new} new · ${reads} x reads · ${enrich.bookmarks_tagged} tagged`,
      error: false,
    };
  } catch (err) {
    state.syncMessage = { text: err.message, error: true };
  } finally {
    state.syncing = false;
  }

  await loadStats();
  await loadTags();
  render();
}

// ---------- shell ----------

function setView(view) {
  state.view = view;
  render();
}

function render() {
  renderNav();
  renderSyncArea();
  const main = $("main");
  main.textContent = "";

  if (state.view === "detail") {
    showDetail(main);
    return;
  }
  if (state.view === "queue") {
    showQueue(main);
    return;
  }
  // Library, or the first-run hero when nothing has ever been synced and
  // there's nothing to browse.
  if (state.stats && state.stats.total === 0) {
    showFirstRun(main);
    return;
  }
  showLibrary(main);
}

function setupChrome() {
  $("nav-library").addEventListener("click", () => {
    setView("library");
  });
  $("nav-queue").addEventListener("click", () => {
    setView("queue");
  });
  $("theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("xbm-theme", next);
    renderTheme();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/INPUT|TEXTAREA/.test(e.target.tagName || "")) {
      e.preventDefault();
      if (state.view !== "library") setView("library");
      const input = $("search-input");
      if (input) input.focus();
    }
  });
}

async function init() {
  setupChrome();
  renderTheme();
  renderSyncArea();
  try {
    await loadStats();
  } catch (_) {
    state.stats = { total: 0, by_type: {}, unwatched: 0, last_synced_at: null };
  }
  loadTags();
  render();
}

init();
