// All bookmark/tag/author content below comes from other people's tweets on
// X, so it's untrusted. Every render path here builds DOM nodes via
// textContent/createElement instead of innerHTML, so nothing in a bookmark
// can execute as HTML/script in this page.

// Must match SNIPPET_START/SNIPPET_END in backend/queries.py.
const SNIPPET_START = "\u0001";
const SNIPPET_END = "\u0002";
const PAGE_SIZE = 40;
const TAG_INDEX_LIMIT = 40;
const CLAMP_CHARS = 700;

const VIEWS = {
  inbox: { title: "Inbox", icon: "i-inbox", key: "i" },
  all: { title: "All bookmarks", icon: "i-all", key: "a" },
  threads: { title: "Threads", icon: "i-thread", key: "t" },
  articles: { title: "Articles", icon: "i-article", key: "r" },
  videos: { title: "Videos", icon: "i-video", key: "v" },
  watch: { title: "Watch later", icon: "i-watch", key: "w" },
};

const state = {
  view: "inbox",
  q: "",
  tags: new Set(),
  watch: "unwatched",
  items: [],
  total: 0,
  facets: [],
  counts: {},
  offset: 0,
  exhausted: false,
  loading: false,
  selectedId: null,
  anchorIndex: -1,
  detail: null,
  xrefIndex: -1,
  showAllTags: false,
  lastFiled: null,
  allTags: null,
};

const detailCache = new Map();
const $ = (id) => document.getElementById(id);
const wide = window.matchMedia("(min-width: 761px)");

// ---------- DOM helpers ----------

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#" + name);
  svg.appendChild(use);
  return svg;
}

function kbd(...keys) {
  return keys.map((k) => el("kbd", { text: k }));
}

function renderMarked(node, text) {
  node.textContent = "";
  let i = 0;
  while (i < text.length) {
    const start = text.indexOf(SNIPPET_START, i);
    if (start === -1) {
      node.appendChild(document.createTextNode(text.slice(i)));
      return node;
    }
    node.appendChild(document.createTextNode(text.slice(i, start)));
    const end = text.indexOf(SNIPPET_END, start + 1);
    if (end === -1) {
      node.appendChild(document.createTextNode(text.slice(start + 1)));
      return node;
    }
    node.appendChild(el("mark", { text: text.slice(start + 1, end) }));
    i = end + 1;
  }
  return node;
}

const hasMarks = (text) => !!text && text.includes(SNIPPET_START);

// ---------- Formatting ----------

const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" });

function shortDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60 * 60) return rtf.format(-Math.max(1, Math.round(diff / 60)), "minute");
  if (diff < 60 * 60 * 24) return rtf.format(-Math.round(diff / 3600), "hour");
  if (diff < 60 * 60 * 24 * 7) return rtf.format(-Math.round(diff / 86400), "day");
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, sameYear ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "numeric" });
}

function longDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

const plural = (n, one, many = one + "s") => `${n.toLocaleString()} ${n === 1 ? one : many}`;
const tweetUrl = (b) => `https://x.com/${encodeURIComponent(b.author_username)}/status/${b.id}`;

// ---------- API ----------

async function api(path, options) {
  const res = await fetch(path, options);
  let data = null;
  try {
    data = await res.json();
  } catch {
    // non-JSON error bodies fall through to the generic message
  }
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

// ---------- URL state ----------

function writeHash() {
  const p = new URLSearchParams();
  if (state.view !== "inbox") p.set("view", state.view);
  if (state.q) p.set("q", state.q);
  for (const t of state.tags) p.append("tag", t);
  if (state.view === "watch" && state.watch !== "unwatched") p.set("watch", state.watch || "all");
  if (state.selectedId) p.set("id", state.selectedId);
  const hash = p.toString();
  history.replaceState(null, "", hash ? "#" + hash : location.pathname);
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const view = p.get("view");
  state.view = view && VIEWS[view] ? view : "inbox";
  state.q = p.get("q") || "";
  state.tags = new Set(p.getAll("tag"));
  const watch = p.get("watch");
  state.watch = watch === "all" ? "" : watch === "watched" ? "watched" : "unwatched";
  state.selectedId = p.get("id");
}

// ---------- Counts & rail ----------

async function loadCounts() {
  try {
    state.counts = await api("/api/views");
  } catch {
    return;
  }
  document.querySelectorAll("[data-count]").forEach((node) => {
    const n = state.counts[node.dataset.count];
    node.textContent = n ? n.toLocaleString() : "";
  });
}

function renderViews() {
  document.querySelectorAll(".view-item").forEach((btn) => {
    if (btn.dataset.view === state.view) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
  $("list-title").textContent = VIEWS[state.view].title;
  const wf = $("watch-filter");
  wf.hidden = state.view !== "watch";
  wf.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.watch === state.watch)));
  document.title = `${VIEWS[state.view].title} · XBM`;
}

function renderTagIndex() {
  const list = $("tag-index");
  list.textContent = "";
  const facets = [...state.facets];
  // Selected tags always stay visible at the top of the index.
  facets.sort((a, b) => (state.tags.has(b.name) ? 1 : 0) - (state.tags.has(a.name) ? 1 : 0));
  const shown = state.showAllTags ? facets : facets.slice(0, TAG_INDEX_LIMIT);
  const max = Math.max(1, ...facets.map((f) => f.count));

  for (const f of shown) {
    const pressed = state.tags.has(f.name);
    const btn = el(
      "button",
      {
        class: "tag-btn",
        type: "button",
        "aria-pressed": String(pressed),
        title: pressed ? `Remove filter #${f.name}` : `Filter by #${f.name}`,
        onclick: () => toggleTag(f.name),
      },
      el("span", { class: "tag-name", text: f.name }),
      el("span", { class: "tag-bar", text: f.count.toLocaleString() })
    );
    btn.style.setProperty("--share", (f.count / max).toFixed(3));
    list.appendChild(el("li", {}, btn));
  }

  if (facets.length > TAG_INDEX_LIMIT) {
    list.appendChild(
      el(
        "li",
        {},
        el("button", {
          class: "tag-btn",
          type: "button",
          text: state.showAllTags ? "Show fewer" : `Show all ${facets.length}`,
          onclick: () => {
            state.showAllTags = !state.showAllTags;
            renderTagIndex();
          },
        })
      )
    );
  }

  const filtered = state.q || state.tags.size || state.view !== "all";
  $("index-hint").textContent = !facets.length
    ? "Tags appear here after enrichment runs."
    : filtered
      ? `Tags inside these ${plural(state.total, "result")}`
      : "Most used first";
}

function renderActiveFilters() {
  const box = $("active-filters");
  box.textContent = "";
  for (const t of state.tags) {
    box.appendChild(
      el(
        "button",
        { class: "filter-chip", type: "button", title: `Remove filter #${t}`, onclick: () => toggleTag(t) },
        `#${t}`,
        icon("i-x")
      )
    );
  }
  if (state.tags.size > 1 || (state.tags.size && state.q)) {
    box.appendChild(el("button", { class: "clear-filters", type: "button", text: "Clear all", onclick: clearFilters }));
  }
}

// ---------- List ----------

let listSeq = 0;

function showSkeleton() {
  const list = $("list");
  list.textContent = "";
  $("list-empty").hidden = true;
  for (let i = 0; i < 6; i++) {
    list.appendChild(el("li", { class: "row-skeleton", "aria-hidden": "true" }, el("span"), el("span"), el("span")));
  }
}

function buildParams(offset) {
  const p = new URLSearchParams();
  p.set("view", state.view);
  if (state.q) p.set("q", state.q);
  for (const t of state.tags) p.append("tag", t);
  if (state.view === "watch" && state.watch) p.set("watch", state.watch);
  p.set("limit", PAGE_SIZE);
  p.set("offset", offset);
  return p;
}

async function loadList({ keepSelection = true } = {}) {
  const seq = ++listSeq;
  state.loading = true;
  state.offset = 0;
  state.exhausted = false;
  if (!state.items.length) showSkeleton();

  let data;
  try {
    data = await api("/api/search?" + buildParams(0));
  } catch (err) {
    if (seq !== listSeq) return;
    state.loading = false;
    state.items = [];
    $("list").textContent = "";
    showEmpty("Couldn't load bookmarks", err.message);
    return;
  }
  if (seq !== listSeq) return;

  state.items = data.results;
  state.total = data.total;
  state.facets = data.facets;
  state.offset = data.results.length;
  state.exhausted = state.offset >= data.total;
  state.loading = false;

  renderList();
  renderTagIndex();
  renderActiveFilters();

  const stillThere = keepSelection && state.selectedId;
  if (stillThere) {
    markSelectedRow();
  } else if (wide.matches && state.items.length) {
    select(state.items[0].id, { scroll: true });
  } else if (!state.items.length) {
    state.selectedId = null;
    renderReaderEmpty();
  }
  writeHash();
}

async function loadMore() {
  if (state.loading || state.exhausted) return;
  const seq = listSeq;
  state.loading = true;
  try {
    const data = await api("/api/search?" + buildParams(state.offset));
    if (seq !== listSeq) return;
    state.items.push(...data.results);
    state.offset += data.results.length;
    state.exhausted = !data.results.length || state.offset >= data.total;
    const list = $("list");
    for (const b of data.results) list.appendChild(buildRow(b));
    renderListFoot();
  } catch {
    state.exhausted = true;
  } finally {
    state.loading = false;
  }
}

function renderList() {
  const list = $("list");
  list.textContent = "";
  $("list-count").textContent = state.total ? state.total.toLocaleString() : "";

  if (!state.items.length) {
    renderEmptyState();
    return;
  }
  $("list-empty").hidden = true;
  for (const b of state.items) list.appendChild(buildRow(b));
  renderListFoot();
  markSelectedRow();
}

function renderListFoot() {
  const pane = document.querySelector(".list-pane");
  pane.querySelector(".list-more")?.remove();
  if (state.exhausted && state.items.length > 8) {
    $("sentinel").before(el("p", { class: "list-more", text: `End of ${VIEWS[state.view].title.toLowerCase()}. ${plural(state.total, "bookmark")}.` }));
  }
}

function buildRow(b) {
  const row = el("li", {
    class: "row" + (b.is_read ? " is-read" : ""),
    id: "row-" + b.id,
    role: "option",
    "aria-selected": "false",
    dataset: { id: b.id },
    onclick: () => select(b.id, { open: true }),
  });

  row.appendChild(el("span", { class: "dot", title: b.is_read ? null : "In Inbox" }));

  const kinds = el("span", { class: "row-kinds" });
  if (b.is_thread) kinds.appendChild(withTitle(icon("i-thread"), "Thread"));
  if (b.has_article) kinds.appendChild(withTitle(icon("i-article"), "Has a linked article"));
  if (b.has_video) kinds.appendChild(withTitle(icon("i-video"), b.watch_status === "watched" ? "Video, watched" : "Video"));

  row.appendChild(
    el(
      "div",
      { class: "row-meta" },
      el("span", { class: "row-author", text: b.author_name || b.author_username }),
      el("span", { class: "row-handle", text: "@" + b.author_username }),
      kinds,
      el("time", { class: "row-date", datetime: b.created_at, text: shortDate(b.created_at) })
    )
  );

  const text = el("p", { class: "row-text" });
  renderMarked(text, hasMarks(b.snippet) ? b.snippet : b.text || "");
  row.appendChild(text);

  // The hit was somewhere other than the tweet itself: say where, so a
  // result that "doesn't mention" the query still makes sense.
  if (state.q && !hasMarks(b.snippet) && hasMarks(b.match_snippet)) {
    const where = b.is_thread ? "In the thread or attached notes" : "In attached notes";
    const match = el("p", { class: "row-match" }, el("span", { class: "where", text: where + ": " }));
    match.appendChild(renderMarked(el("span"), b.match_snippet));
    row.appendChild(match);
  }

  if (b.tags?.length) {
    row.appendChild(el("div", { class: "row-tags" }, b.tags.slice(0, 4).map((t) => el("span", { text: "#" + t }))));
  }
  return row;
}

function withTitle(node, title) {
  const t = document.createElementNS("http://www.w3.org/2000/svg", "title");
  t.textContent = title;
  node.prepend(t);
  node.removeAttribute("aria-hidden");
  node.setAttribute("role", "img");
  node.setAttribute("aria-label", title);
  return node;
}

function showEmpty(title, body, action) {
  const box = $("list-empty");
  box.textContent = "";
  box.append(el("h2", { text: title }), el("p", {}, ...[body].flat()));
  if (action) box.appendChild(action);
  box.hidden = false;
}

function renderEmptyState() {
  if (state.counts.all === 0) {
    showEmpty(
      "Nothing saved here yet",
      "Sync pulls your X bookmarks in, then enrichment adds article summaries, video transcripts and tags.",
      el("button", { class: "btn btn-primary", type: "button", onclick: runSync }, icon("i-sync"), "Sync now")
    );
  } else if (state.q || state.tags.size) {
    const what = state.q ? `“${state.q}”` : "these tags";
    showEmpty(
      `No matches for ${what} in ${VIEWS[state.view].title}`,
      [
        "Search matches the start of every word you type, across tweets, threads, summaries and transcripts. Try fewer words, or ",
        el("button", { class: "clear-filters", type: "button", text: "clear filters", onclick: clearFilters }),
        state.view !== "all" ? [" or ", el("button", { class: "clear-filters", type: "button", text: "search all bookmarks", onclick: () => setView("all") })] : "",
        ".",
      ]
    );
  } else if (state.view === "inbox") {
    showEmpty("Inbox clear", ["Every bookmark is filed. New ones land here after the next sync. Browse everything with ", ...kbd("g", "a"), "."]);
  } else if (state.view === "watch") {
    showEmpty(
      state.watch === "unwatched" ? "Nothing left to watch" : "No videos here",
      "Bookmarks with a YouTube link join this queue automatically once enrichment fetches their transcript."
    );
  } else {
    showEmpty(`No ${VIEWS[state.view].title.toLowerCase()} yet`, "They'll show up here after the next sync and enrichment run.");
  }
}

function rowFor(id) {
  return document.getElementById("row-" + id);
}

function markSelectedRow() {
  const list = $("list");
  list.querySelectorAll('.row[aria-selected="true"]').forEach((r) => r.setAttribute("aria-selected", "false"));
  const row = state.selectedId && rowFor(state.selectedId);
  if (row) {
    row.setAttribute("aria-selected", "true");
    list.setAttribute("aria-activedescendant", row.id);
  } else {
    list.removeAttribute("aria-activedescendant");
  }
}

function scrollRowIntoView(row) {
  const head = document.querySelector(".list-head");
  row.style.scrollMarginTop = head.offsetHeight + "px";
  row.scrollIntoView({ block: "nearest" });
}

// ---------- Selection & reader ----------

let detailSeq = 0;

async function select(id, { scroll = false, open = false } = {}) {
  state.selectedId = id;
  const index = state.items.findIndex((b) => b.id === id);
  if (index !== -1) state.anchorIndex = index;
  markSelectedRow();
  const row = rowFor(id);
  if (row && scroll) scrollRowIntoView(row);
  if (open) document.getElementById("app").classList.add("reading");
  writeHash();

  const seq = ++detailSeq;
  if (detailCache.has(id)) {
    renderReader(detailCache.get(id));
    return;
  }
  const timer = setTimeout(() => seq === detailSeq && renderReaderSkeleton(), 120);
  try {
    const detail = await api("/api/bookmarks/" + id);
    detailCache.set(id, detail);
    if (seq === detailSeq) renderReader(detail);
  } catch (err) {
    if (seq === detailSeq) renderReaderError(err.message);
  } finally {
    clearTimeout(timer);
  }
}

function moveSelection(delta) {
  if (!state.items.length) return;
  let index = state.items.findIndex((b) => b.id === state.selectedId);
  if (index === -1) index = delta > 0 ? state.anchorIndex : state.anchorIndex + 1;
  const next = Math.min(state.items.length - 1, Math.max(0, index + delta));
  select(state.items[next].id, { scroll: true });
  if (next >= state.items.length - 5) loadMore();
}

function closeReader() {
  document.getElementById("app").classList.remove("reading");
}

function renderReaderEmpty() {
  const reader = $("reader");
  reader.textContent = "";
  state.detail = null;
  reader.appendChild(
    el(
      "div",
      { class: "reader-empty" },
      el("h2", { text: "Pick a bookmark to read it here" }),
      el("p", { text: "Threads, article summaries and transcripts open alongside the tweet." }),
      el(
        "div",
        { class: "key-row" },
        el("span", {}, ...kbd("j", "k"), " move"),
        el("span", {}, ...kbd("e"), " file"),
        el("span", {}, ...kbd("/"), " search"),
        el("span", {}, ...kbd("?"), " all shortcuts")
      )
    )
  );
}

function renderReaderSkeleton() {
  const reader = $("reader");
  reader.textContent = "";
  const skel = el("div", { class: "reader-skel", "aria-hidden": "true" });
  for (const w of [30, 95, 88, 92, 60]) {
    const bar = el("span");
    bar.style.width = w + "%";
    skel.appendChild(bar);
  }
  reader.appendChild(skel);
}

function renderReaderError(message) {
  const reader = $("reader");
  reader.textContent = "";
  reader.appendChild(
    el("div", { class: "reader-empty" }, el("h2", { text: "Couldn't open this bookmark" }), el("p", { text: message }))
  );
}

function clampable(text, label) {
  const body = el("div", { class: "attach-body" });
  body.textContent = text;
  if (text.length <= CLAMP_CHARS) return [body];
  body.classList.add("is-clamped");
  const btn = el("button", {
    class: "more-btn",
    type: "button",
    "aria-expanded": "false",
    text: `Show full ${label}`,
    onclick: () => {
      const open = body.classList.toggle("is-clamped") === false;
      btn.setAttribute("aria-expanded", String(open));
      btn.textContent = open ? `Show less` : `Show full ${label}`;
    },
  });
  return [body, btn];
}

function renderReader(d) {
  state.detail = d;
  state.xrefIndex = -1;
  const reader = $("reader");
  reader.textContent = "";
  reader.scrollTop = 0;

  const fileBtn = el(
    "button",
    { class: "btn", type: "button", onclick: () => toggleRead(d.id) },
    icon(d.is_read ? "i-inbox" : "i-file"),
    el("span", { class: "btn-label", text: d.is_read ? "Back to Inbox" : "File it" }),
    el("kbd", { text: "e" })
  );

  const bar = el(
    "div",
    { class: "reader-bar" },
    el("button", { class: "icon-btn back", type: "button", "aria-label": "Back to list", onclick: closeReader }, icon("i-back")),
    fileBtn,
    d.watch_status
      ? el(
          "button",
          { class: "btn", type: "button", onclick: () => toggleWatched(d.id) },
          icon(d.watch_status === "watched" ? "i-watch" : "i-check"),
          el("span", { class: "btn-label", text: d.watch_status === "watched" ? "Mark unwatched" : "Mark watched" }),
          el("kbd", { text: "w" })
        )
      : null,
    el("span", { class: "spacer" }),
    el(
      "a",
      { class: "btn", href: tweetUrl(d), target: "_blank", rel: "noopener noreferrer", "aria-label": "Open on X" },
      el("span", { class: "btn-label", text: "Open on X" }),
      icon("i-external")
    )
  );

  const inner = el("div", { class: "reader-inner reader-fade" });

  inner.appendChild(
    el(
      "header",
      {},
      el(
        "div",
        { class: "byline" },
        el("span", { class: "byline-name", text: d.author_name || d.author_username }),
        el("span", { class: "byline-handle", text: "@" + d.author_username }),
        el("time", { class: "byline-date", datetime: d.created_at, text: longDate(d.created_at) })
      ),
      statusPills(d)
    )
  );

  inner.appendChild(el("div", { class: "clipping", text: d.text || "" }));

  if (d.thumbnail) {
    inner.appendChild(el("img", { class: "media", src: d.thumbnail, alt: "", loading: "lazy", onerror: (e) => e.target.remove() }));
  }

  if (d.is_thread && d.thread_text && d.thread_text.trim() !== (d.text || "").trim()) {
    inner.appendChild(
      el(
        "section",
        { class: "attach", "aria-label": "Thread" },
        el("div", { class: "attach-head" }, icon("i-thread"), el("h2", { text: "Full thread" })),
        ...clampable(d.thread_text, "thread")
      )
    );
  }

  for (const link of d.linked || []) inner.appendChild(renderLinked(link));

  if (d.tags?.length) {
    inner.appendChild(
      el(
        "section",
        { class: "attach", "aria-label": "Filed under" },
        el("div", { class: "attach-head" }, icon("i-tag"), el("h2", { text: "Filed under" })),
        el(
          "div",
          { class: "filed-under" },
          d.tags.map((t) =>
            el(
              "button",
              {
                class: "tag-pick",
                type: "button",
                "aria-pressed": String(state.tags.has(t)),
                title: state.tags.has(t) ? `Remove filter #${t}` : `Show everything filed under #${t}`,
                onclick: () => toggleTag(t),
              },
              "#" + t
            )
          )
        )
      )
    );
  }

  if (d.related?.length) {
    inner.appendChild(
      el(
        "section",
        { class: "attach", "aria-label": "See also" },
        el("div", { class: "attach-head" }, icon("i-link"), el("h2", { text: "See also" })),
        el("ol", { class: "see-also", id: "see-also" }, d.related.map((r) => el("li", {}, buildXref(r))))
      )
    );
  }

  reader.append(bar, inner);
}

function statusPills(d) {
  const line = el("div", { class: "status-line" });
  line.appendChild(
    el("span", { class: "pill" + (d.is_read ? "" : " is-unread") }, icon(d.is_read ? "i-file" : "i-inbox"), d.is_read ? "Filed" : "In Inbox")
  );
  if (d.is_thread) line.appendChild(el("span", { class: "pill" }, icon("i-thread"), "Thread"));
  if (d.watch_status) {
    line.appendChild(
      el("span", { class: "pill" }, icon(d.watch_status === "watched" ? "i-check" : "i-watch"), d.watch_status === "watched" ? "Watched" : "To watch")
    );
  }
  return line;
}

function renderLinked(link) {
  const isVideo = link.type === "youtube";
  const section = el(
    "section",
    { class: "attach", "aria-label": isVideo ? "Linked video" : "Linked article" },
    el("div", { class: "attach-head" }, icon(isVideo ? "i-video" : "i-article"), el("h2", { text: isVideo ? "Video" : "Article" })),
    el(
      "a",
      { class: "attach-title", href: link.url, target: "_blank", rel: "noopener noreferrer" },
      link.title || hostOf(link.url),
      icon("i-external")
    ),
    el("p", { class: "attach-host", text: hostOf(link.url) })
  );

  if (link.status === "done" && link.body) {
    section.append(...clampable(link.body, isVideo ? "transcript" : "summary"));
  } else if (link.status === "failed") {
    section.appendChild(
      el("p", { class: "attach-note is-error", text: `Enrichment failed: ${link.error || "unknown error"}. It's retried on the next sync.` })
    );
  } else if (link.status === "pending") {
    section.appendChild(el("p", { class: "attach-note", text: `Not enriched yet. The ${isVideo ? "transcript" : "summary"} arrives with the next sync.` }));
  }
  return section;
}

function buildXref(r) {
  const why = r.reason?.tags
    ? "Also filed under " + r.reason.tags.map((t) => "#" + t).join(" ")
    : r.reason?.author
      ? `Also by @${r.reason.author}`
      : "";
  return el(
    "button",
    { class: "xref", type: "button", dataset: { id: r.id }, onclick: () => select(r.id) },
    el("span", { class: "xref-who" }, el("strong", { text: r.author_name || r.author_username }), ` @${r.author_username} · ${shortDate(r.created_at)}`),
    el("span", { class: "xref-text", text: r.text }),
    el("span", { class: "xref-why", text: why })
  );
}

function moveXref(delta) {
  const items = [...document.querySelectorAll("#see-also .xref")];
  if (!items.length) return;
  state.xrefIndex = Math.min(items.length - 1, Math.max(0, state.xrefIndex + delta));
  items.forEach((x, i) => (i === state.xrefIndex ? x.setAttribute("aria-current", "true") : x.removeAttribute("aria-current")));
  items[state.xrefIndex].scrollIntoView({ block: "nearest", behavior: "smooth" });
}

// ---------- Actions ----------

function patchBookmark(id, patch) {
  const item = state.items.find((b) => b.id === id);
  if (item) Object.assign(item, patch);
  const cached = detailCache.get(id);
  if (cached) Object.assign(cached, patch);
  return item;
}

async function toggleRead(id) {
  const current = detailCache.get(id) || state.items.find((b) => b.id === id);
  if (!current) return;
  const nowRead = !current.is_read;
  try {
    await api(`/api/bookmarks/${id}/read`, { method: nowRead ? "POST" : "DELETE" });
  } catch (err) {
    toast("Couldn't update: " + err.message);
    return;
  }
  patchBookmark(id, { is_read: nowRead });
  const row = rowFor(id);

  if (nowRead && state.view === "inbox") {
    state.lastFiled = id;
    const index = state.items.findIndex((b) => b.id === id);
    const nextItem = state.items[index + 1] || state.items[index - 1];
    state.items.splice(index, 1);
    state.total = Math.max(0, state.total - 1);
    $("list-count").textContent = state.total ? state.total.toLocaleString() : "";
    if (row) {
      row.classList.add("is-leaving");
      setTimeout(() => {
        row.remove();
        if (!state.items.length) renderEmptyState();
      }, 200);
    }
    if (nextItem && state.selectedId === id) select(nextItem.id, { scroll: true });
    else if (!nextItem) renderReaderEmpty();
    toast("Filed out of Inbox", { label: "Undo", key: "z", run: undoFile });
  } else {
    if (row) row.classList.toggle("is-read", nowRead);
    if (state.detail?.id === id) renderReader(detailCache.get(id) || state.detail);
    if (nowRead) state.lastFiled = id;
    toast(nowRead ? "Filed" : "Back in Inbox", nowRead ? { label: "Undo", key: "z", run: undoFile } : null);
  }
  loadCounts();
}

async function undoFile() {
  const id = state.lastFiled;
  if (!id) return;
  state.lastFiled = null;
  try {
    await api(`/api/bookmarks/${id}/read`, { method: "DELETE" });
  } catch (err) {
    toast("Couldn't undo: " + err.message);
    return;
  }
  patchBookmark(id, { is_read: false });
  hideToast();
  state.selectedId = id;
  await loadList();
  select(id, { scroll: true });
  loadCounts();
}

async function toggleWatched(id) {
  const current = detailCache.get(id) || state.items.find((b) => b.id === id);
  if (!current?.watch_status) return;
  let result;
  try {
    result = await api(`/api/watch-later/${id}/toggle`, { method: "POST" });
  } catch (err) {
    toast("Couldn't update: " + err.message);
    return;
  }
  patchBookmark(id, { watch_status: result.status });
  if (state.detail?.id === id) renderReader(detailCache.get(id) || { ...state.detail, watch_status: result.status });
  if (state.view === "watch" && state.watch && state.watch !== result.status) {
    const index = state.items.findIndex((b) => b.id === id);
    if (index !== -1) {
      state.items.splice(index, 1);
      state.total = Math.max(0, state.total - 1);
      rowFor(id)?.remove();
      $("list-count").textContent = state.total ? state.total.toLocaleString() : "";
      if (!state.items.length) renderEmptyState();
    }
  }
  toast(result.status === "watched" ? "Marked watched" : "Back in the watch queue");
  loadCounts();
}

function setView(view) {
  if (!VIEWS[view]) return;
  state.view = view;
  state.items = [];
  state.selectedId = null;
  state.anchorIndex = -1;
  closeRail();
  renderViews();
  loadList({ keepSelection: false });
  document.querySelector(".list-pane").scrollTop = 0;
}

function toggleTag(name) {
  if (state.tags.has(name)) state.tags.delete(name);
  else state.tags.add(name);
  state.selectedId = null;
  document.querySelector(".list-pane").scrollTop = 0;
  loadList({ keepSelection: false });
}

function clearFilters() {
  state.tags.clear();
  state.q = "";
  $("search-input").value = "";
  loadList({ keepSelection: false });
}

// ---------- Sync ----------

async function runSync() {
  const btn = $("sync-btn");
  const status = $("sync-status");
  if (btn.disabled) return;
  btn.disabled = true;
  btn.setAttribute("aria-busy", "true");
  status.removeAttribute("data-tone");
  try {
    status.textContent = "Pulling bookmarks from X…";
    const sync = await api("/api/sync", { method: "POST" });
    status.textContent = `${plural(sync.new, "new bookmark")}. Enriching…`;
    const enrich = await api("/api/enrich", { method: "POST" });
    status.dataset.tone = "ok";
    status.textContent =
      `${plural(sync.new, "new bookmark")}, ${sync.updated} updated · ` +
      `${plural(enrich.linked_content_done, "link")} enriched, ${enrich.bookmarks_tagged} tagged`;
    toast(`Sync done: ${plural(sync.new, "new bookmark")}`);
    detailCache.clear();
    state.allTags = null;
    await loadCounts();
    await loadList();
  } catch (err) {
    status.dataset.tone = "error";
    status.textContent = err.message;
    toast("Sync failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
  }
}

// ---------- Toast ----------

let toastTimer;
let toastAction = null;

function toast(message, action = null) {
  const node = $("toast");
  node.textContent = "";
  node.appendChild(el("span", { text: message }));
  toastAction = action;
  if (action) {
    node.appendChild(el("button", { type: "button", onclick: action.run }, action.label, action.key ? el("kbd", { text: action.key }) : null));
  }
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, action ? 6000 : 3000);
}

function hideToast() {
  $("toast").hidden = true;
  toastAction = null;
}

// ---------- Command palette ----------

const palette = { items: [], index: 0 };

async function getAllTags() {
  if (!state.allTags) {
    try {
      state.allTags = await api("/api/tags");
    } catch {
      state.allTags = [];
    }
  }
  return state.allTags;
}

function paletteCommands(tags) {
  const cmds = [];
  for (const [view, v] of Object.entries(VIEWS)) {
    cmds.push({
      group: "Go to",
      label: v.title,
      icon: v.icon,
      hint: state.counts[view] != null ? state.counts[view].toLocaleString() : "",
      run: () => setView(view),
    });
  }
  for (const t of tags) {
    const on = state.tags.has(t.name);
    cmds.push({
      group: "Filter by tag",
      label: "#" + t.name,
      icon: on ? "i-check" : "i-tag",
      hint: on ? "remove" : t.count.toLocaleString(),
      run: () => toggleTag(t.name),
    });
  }
  cmds.push(
    { group: "Actions", label: "Sync now", icon: "i-sync", run: runSync },
    { group: "Actions", label: "Clear search and filters", icon: "i-x", run: clearFilters },
    { group: "Actions", label: "Keyboard shortcuts", icon: "i-search", hint: "?", run: () => $("help").showModal() }
  );
  return cmds;
}

function fuzzyScore(query, text) {
  if (!query) return 1;
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  const direct = t.indexOf(q);
  if (direct !== -1) return 100 - direct + (direct === 0 || /\W/.test(t[direct - 1]) ? 50 : 0);
  let ti = 0;
  let score = 0;
  for (const ch of q) {
    const found = t.indexOf(ch, ti);
    if (found === -1) return 0;
    score += found === ti ? 3 : 1;
    ti = found + 1;
  }
  return score;
}

async function openPalette(prefix = "") {
  closeRail();
  const dialog = $("palette");
  const input = $("palette-input");
  input.value = prefix;
  dialog.showModal();
  input.focus();
  palette.all = paletteCommands(await getAllTags());
  filterPalette();
}

function filterPalette() {
  const raw = $("palette-input").value.trim();
  const tagsOnly = raw.startsWith("#");
  const query = tagsOnly ? raw.slice(1) : raw;
  palette.items = (palette.all || [])
    .filter((c) => !tagsOnly || c.group === "Filter by tag")
    .map((c) => ({ c, s: fuzzyScore(query, c.label.replace(/^#/, "")) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => (query ? b.s - a.s : 0))
    .map((x) => x.c)
    .slice(0, query ? 50 : 60);
  palette.index = 0;
  renderPalette(!!query);
}

function renderPalette(flat) {
  const list = $("palette-list");
  list.textContent = "";
  if (!palette.items.length) {
    list.appendChild(el("li", { class: "palette-none", text: "Nothing matches. Try a view name, a tag, or “sync”." }));
    return;
  }
  let lastGroup = null;
  palette.items.forEach((c, i) => {
    if (!flat && c.group !== lastGroup) {
      list.appendChild(el("li", { class: "palette-group", role: "presentation", text: c.group }));
      lastGroup = c.group;
    }
    list.appendChild(
      el(
        "li",
        {
          class: "palette-item",
          id: "pal-" + i,
          role: "option",
          "aria-selected": String(i === palette.index),
          onclick: () => runPaletteItem(i),
          onmousemove: () => setPaletteIndex(i),
        },
        icon(c.icon),
        el("span", { text: c.label }),
        c.hint ? el("span", { class: "hint", text: c.hint }) : null
      )
    );
  });
  $("palette-input").setAttribute("aria-activedescendant", "pal-0");
}

function setPaletteIndex(i) {
  if (i === palette.index) return;
  document.getElementById("pal-" + palette.index)?.setAttribute("aria-selected", "false");
  palette.index = i;
  const node = document.getElementById("pal-" + i);
  node?.setAttribute("aria-selected", "true");
  node?.scrollIntoView({ block: "nearest" });
  $("palette-input").setAttribute("aria-activedescendant", "pal-" + i);
}

function runPaletteItem(i) {
  const c = palette.items[i];
  $("palette").close();
  c?.run();
}

function setupPalette() {
  const input = $("palette-input");
  input.addEventListener("input", filterPalette);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setPaletteIndex(Math.min(palette.items.length - 1, palette.index + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setPaletteIndex(Math.max(0, palette.index - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runPaletteItem(palette.index);
    }
  });
  $("palette").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) e.currentTarget.close();
  });
  $("palette-btn").addEventListener("click", () => openPalette());
}

// ---------- Rail (narrow screens) ----------

function openRail() {
  $("app").classList.add("rail-open");
  $("scrim").hidden = false;
  $("menu-btn").setAttribute("aria-expanded", "true");
}

function closeRail() {
  $("app").classList.remove("rail-open");
  $("scrim").hidden = true;
  $("menu-btn").setAttribute("aria-expanded", "false");
}

// ---------- Keyboard ----------

let gPending = false;
let gTimer;

function isTyping(target) {
  return target.closest?.("input, textarea, select, [contenteditable='true']");
}

function onKeydown(e) {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if ($("palette").open) $("palette").close();
    else openPalette();
    return;
  }
  if ($("palette").open || $("help").open) return;

  if (e.target.id === "search-input") {
    if (e.key === "Escape") {
      if (e.target.value) {
        e.target.value = "";
        onSearchInput("");
      } else {
        e.target.blur();
      }
    } else if (e.key === "ArrowDown" || e.key === "Enter") {
      e.preventDefault();
      e.target.blur();
      $("list").focus({ preventScroll: true });
      if (state.items.length) select(state.items[0].id, { scroll: true });
    }
    return;
  }
  if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;

  if (gPending) {
    gPending = false;
    clearTimeout(gTimer);
    const view = Object.keys(VIEWS).find((v) => VIEWS[v].key === e.key);
    if (view) {
      e.preventDefault();
      setView(view);
    }
    return;
  }

  const id = state.selectedId;
  switch (e.key) {
    case "j":
    case "ArrowDown":
      e.preventDefault();
      moveSelection(1);
      break;
    case "k":
    case "ArrowUp":
      e.preventDefault();
      moveSelection(-1);
      break;
    case "Enter": {
      const xref = document.querySelector('#see-also .xref[aria-current="true"]');
      if (xref) select(xref.dataset.id);
      else if (id) select(id, { open: true });
      break;
    }
    case "]":
      moveXref(1);
      break;
    case "[":
      moveXref(-1);
      break;
    case "e":
      if (id) toggleRead(id);
      break;
    case "z":
      if (toastAction?.key === "z") toastAction.run();
      else if (state.lastFiled) undoFile();
      break;
    case "w":
      if (id) toggleWatched(id);
      break;
    case "o":
      if (id) {
        const b = detailCache.get(id) || state.items.find((x) => x.id === id);
        if (b) window.open(tweetUrl(b), "_blank", "noopener");
      }
      break;
    case "/":
      e.preventDefault();
      $("search-input").focus();
      $("search-input").select();
      break;
    case "t":
      e.preventDefault();
      openPalette("#");
      break;
    case "g":
      gPending = true;
      gTimer = setTimeout(() => (gPending = false), 900);
      break;
    case "?":
      $("help").showModal();
      break;
    case "Escape":
      if ($("app").classList.contains("rail-open")) closeRail();
      else if ($("app").classList.contains("reading")) closeReader();
      else if (state.q) clearSearchOnly();
      else if (state.tags.size) {
        state.tags.clear();
        loadList({ keepSelection: false });
      }
      break;
  }
}

function clearSearchOnly() {
  state.q = "";
  $("search-input").value = "";
  loadList({ keepSelection: false });
}

// ---------- Wiring ----------

let searchTimer;

function onSearchInput(value) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    const q = value.trim();
    if (q === state.q) return;
    state.q = q;
    state.selectedId = null;
    document.querySelector(".list-pane").scrollTop = 0;
    loadList({ keepSelection: false });
  }, 180);
}

function setup() {
  if (/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) {
    $("palette-key").textContent = "⌘K";
    document.querySelectorAll(".mod-key").forEach((k) => (k.textContent = "⌘"));
  }
  readHash();
  $("search-input").value = state.q;
  renderViews();
  renderReaderEmpty();

  document.querySelectorAll(".view-item").forEach((btn) => btn.addEventListener("click", () => setView(btn.dataset.view)));
  $("watch-filter").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-watch]");
    if (!btn) return;
    state.watch = btn.dataset.watch;
    renderViews();
    loadList({ keepSelection: false });
  });
  $("search-input").addEventListener("input", (e) => onSearchInput(e.target.value));
  $("sync-btn").addEventListener("click", runSync);
  $("help-btn").addEventListener("click", () => {
    closeRail();
    $("help").showModal();
  });
  $("help").addEventListener("click", (e) => {
    if (e.target === e.currentTarget || e.target.closest("[data-close]")) $("help").close();
  });
  $("menu-btn").addEventListener("click", openRail);
  $("rail-close").addEventListener("click", closeRail);
  $("scrim").addEventListener("click", closeRail);
  document.addEventListener("keydown", onKeydown);
  setupPalette();

  new IntersectionObserver((entries) => entries.some((x) => x.isIntersecting) && loadMore(), {
    root: document.querySelector(".list-pane"),
    rootMargin: "400px",
  }).observe($("sentinel"));

  const initialId = state.selectedId;
  loadCounts().then(() => loadList()).then(() => {
    if (initialId) select(initialId, { scroll: true, open: !wide.matches });
  });
}

setup();
