// All bookmark/tag/author content below comes from other people's tweets on
// X, so it's untrusted. Every render path here builds DOM nodes via
// textContent/createElement instead of innerHTML, so nothing in a bookmark
// can execute as HTML/script in this page. FTS5 match highlights arrive
// wrapped in the non-printable U+0001/U+0002 delimiters below, which we split
// on to build <mark> nodes rather than trusting any HTML from the index.

const SNIPPET_START = "\x01";
const SNIPPET_END = "\x02";

// Per-content-type visual language: a glyph, a CSS accent class, and the base
// label shown in the meta row. Extras (post count, etc.) are appended when the
// backend has them.
const TYPES = {
  video: { glyph: "▸", label: "VIDEO" }, // ▸
  thread: { glyph: "≡", label: "THREAD" }, // ≡
  article: { glyph: "¶", label: "ARTICLE" }, // ¶
  tweet: { glyph: "“", label: "TWEET" }, // "
};

const state = {
  view: "library", // library | queue | detail
  from: "library", // where detail was opened from, for the back button
  q: "",
  typeFilter: "all",
  activeTag: null,
  selectedId: null,
  theme: null, // resolved on init
  syncing: false,
};

// ---- small helpers -----------------------------------------------------

const root = document.getElementById("root");
const content = document.getElementById("content");

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  if (opts.on) for (const [ev, fn] of Object.entries(opts.on)) node.addEventListener(ev, fn);
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

function clear(node) {
  node.textContent = "";
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function relativeTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "never";
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

function initials(name, username) {
  const src = (name || username || "").trim();
  if (!src) return "?";
  return src
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function typeLabel(item) {
  const base = (TYPES[item.type] || TYPES.tweet).label;
  if (item.type === "thread" && item.post_count) return `${base} · ${item.post_count} posts`;
  return base;
}

function xUrl(item) {
  return `https://x.com/${encodeURIComponent(item.author_username)}/status/${item.id}`;
}

// Split FTS-highlighted text into text + <mark> nodes (never innerHTML).
function snippetNodes(text) {
  const frag = document.createDocumentFragment();
  let i = 0;
  while (i < text.length) {
    const start = text.indexOf(SNIPPET_START, i);
    if (start === -1) {
      frag.appendChild(document.createTextNode(text.slice(i)));
      break;
    }
    frag.appendChild(document.createTextNode(text.slice(i, start)));
    const end = text.indexOf(SNIPPET_END, start + 1);
    if (end === -1) {
      frag.appendChild(document.createTextNode(text.slice(start + 1)));
      break;
    }
    const mark = document.createElement("mark");
    mark.textContent = text.slice(start + 1, end);
    frag.appendChild(mark);
    i = end + 1;
  }
  return frag;
}

// Build a thumbnail box: the striped placeholder from CSS, with the real
// image layered on top when present (removed on error so the placeholder
// shows through), plus an optional label and play affordance.
function thumbBox(cls, item, { label, play } = {}) {
  const box = el("div", { class: `thumb ${cls}` });
  if (label) box.appendChild(el("span", { class: "thumb-label", text: label }));
  if (item && item.thumbnail) {
    const img = document.createElement("img");
    img.className = "thumb-img";
    img.src = item.thumbnail;
    img.alt = "";
    img.loading = "lazy";
    img.addEventListener("error", () => img.remove());
    box.appendChild(img);
  }
  if (play) box.appendChild(el("span", { class: `play ${play}`, text: "▸" }));
  return box;
}

// ---- data cache for sidebar summary ------------------------------------

let summary = { total: 0, types: {}, watch_later: { unwatched: 0, watched: 0 }, last_sync: null };

// ---- sidebar -----------------------------------------------------------

function setNav() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    const isActive = btn.dataset.view === (state.view === "detail" ? state.from : state.view);
    btn.classList.toggle("active", isActive);
  });
}

function renderSidebarCounts() {
  document.getElementById("lib-count").textContent = summary.total;
  document.getElementById("queue-count").textContent = summary.watch_later.unwatched;
}

async function renderTags() {
  const list = document.getElementById("tag-list");
  let tags = [];
  try {
    tags = await getJSON("/api/tags");
  } catch {
    tags = [];
  }
  clear(list);
  if (!tags.length) {
    list.appendChild(el("div", { class: "tags-empty", text: "no tags yet" }));
    return;
  }
  for (const t of tags) {
    const btn = el(
      "button",
      {
        class: "tag-btn" + (state.activeTag === t.name ? " active" : ""),
        attrs: { type: "button" },
        on: {
          click: () => {
            state.activeTag = state.activeTag === t.name ? null : t.name;
            state.view = "library";
            renderTags();
            render();
          },
        },
      },
      [el("span", { text: t.name }), el("span", { class: "tag-count", text: String(t.count) })]
    );
    list.appendChild(btn);
  }
}

function renderSyncBox() {
  const box = document.getElementById("sync-box");
  clear(box);
  if (state.syncing) {
    box.appendChild(el("div", { class: "sync-label", text: state.syncLabel || "syncing…" }));
    box.appendChild(el("div", { class: "progress" }, [el("div", { class: "progress-bar" })]));
    return;
  }
  const when = el("span", { class: "sync-when", text: `synced ${relativeTime(summary.last_sync)}` });
  const btn = el("button", {
    class: "sync-btn",
    text: "sync now",
    attrs: { type: "button" },
    on: { click: runSync },
  });
  box.appendChild(el("div", { class: "sync-row" }, [when, btn]));
  if (state.syncError) {
    box.appendChild(el("div", { class: "sync-label sync-error", text: state.syncError }));
  }
}

function renderThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  btn.textContent = state.theme === "dark" ? "◐ light mode" : "◐ dark mode";
}

// ---- library / search view --------------------------------------------

function libraryHeader() {
  const input = el("input", {
    class: "search-input",
    attrs: {
      type: "search",
      placeholder: "Search tweets, summaries, transcripts…",
      autocomplete: "off",
      value: state.q,
    },
    on: {
      input: debounce((e) => {
        state.q = e.target.value.trim();
        loadResults();
      }, 250),
    },
  });
  searchInputRef = input;

  const searchWrap = el("div", { class: "search-wrap" }, [
    input,
    el("span", { class: "search-hint", text: "/" }),
  ]);

  const chipRow = el("div", { class: "chip-row" });
  const chipDefs = [
    ["all", "All"],
    ["thread", "Threads"],
    ["article", "Articles"],
    ["video", "Videos"],
    ["tweet", "Tweets"],
  ];
  for (const [key, label] of chipDefs) {
    const count = key === "all" ? summary.total : summary.types[key] || 0;
    chipRow.appendChild(
      el(
        "button",
        {
          class: "chip" + (state.typeFilter === key ? " active" : ""),
          attrs: { type: "button" },
          on: {
            click: () => {
              state.typeFilter = key;
              render();
            },
          },
        },
        [el("span", { text: label }), el("span", { class: "chip-count", text: String(count) })]
      )
    );
  }

  if (state.activeTag) {
    chipRow.appendChild(
      el(
        "button",
        {
          class: "tag-clear",
          attrs: { type: "button" },
          on: {
            click: () => {
              state.activeTag = null;
              renderTags();
              render();
            },
          },
        },
        [el("span", { text: `tag: ${state.activeTag}` }), el("span", { text: "×" })]
      )
    );
  }

  const resultLabel = el("div", { class: "result-label", attrs: { id: "result-label" }, text: "" });
  chipRow.appendChild(resultLabel);

  return el("header", { class: "lib-header" }, [searchWrap, chipRow]);
}

function libraryRow(item) {
  const t = TYPES[item.type] || TYPES.tweet;

  const glyph = el("div", { class: `glyph type-${item.type}`, text: t.glyph });

  const meta = el("div", { class: "row-meta" }, [
    el("span", { class: "author", text: item.author_name || item.author_username }),
    el("span", { class: "handle", text: "@" + item.author_username }),
    el("span", { class: `type-${item.type}`, text: typeLabel(item) }),
    el("span", { class: "date", text: formatDate(item.created_at) }),
  ]);

  const body = el("div", { class: "row-body" }, [meta]);

  if (item.title) {
    body.appendChild(el("div", { class: "row-title", text: item.title }));
  }

  const snippet = el("div", { class: "row-snippet" });
  snippet.appendChild(snippetNodes(item.snippet || item.text || ""));
  body.appendChild(snippet);

  if (item.tags && item.tags.length) {
    const tagsEl = el("div", { class: "row-tags" });
    for (const name of item.tags) {
      tagsEl.appendChild(
        el("button", {
          class: "pill",
          text: name,
          attrs: { type: "button" },
          on: {
            click: (e) => {
              e.stopPropagation();
              state.activeTag = name;
              state.view = "library";
              renderTags();
              render();
            },
          },
        })
      );
    }
    body.appendChild(tagsEl);
  }

  const children = [glyph, body];
  if (item.type === "video") {
    children.push(thumbBox("row-thumb", item, { label: "thumbnail" }));
  }

  return el(
    "div",
    {
      class: "row",
      on: { click: () => openDetail(item.id) },
    },
    children
  );
}

async function loadResults() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  if (state.activeTag) params.append("tag", state.activeTag);
  if (state.typeFilter && state.typeFilter !== "all") params.set("type", state.typeFilter);

  let data = { results: [], total: 0 };
  try {
    data = await getJSON("/api/search?" + params.toString());
  } catch (e) {
    data = { results: [], total: 0, error: e.message };
  }

  const label = document.getElementById("result-label");
  if (label) label.textContent = `${data.total} saved · newest first`;

  const listWrap = document.getElementById("result-list");
  if (!listWrap) return;
  clear(listWrap);

  if (!data.results.length) {
    listWrap.appendChild(noResults());
    return;
  }
  for (const item of data.results) {
    listWrap.appendChild(libraryRow(item));
  }
}

function noResults() {
  const wrap = el("div", { class: "no-results" });
  const msg = state.q
    ? `no matches for “${state.q}”`
    : state.activeTag
    ? `no ${state.typeFilter === "all" ? "" : state.typeFilter + " "}items tagged “${state.activeTag}”`
    : "nothing here yet";
  wrap.appendChild(el("div", { class: "no-results-msg", text: msg }));
  wrap.appendChild(el("div", { class: "no-results-sub", text: "Try fewer words, or browse by tag instead." }));
  wrap.appendChild(
    el("button", {
      class: "btn-ghost",
      text: "clear search & filters",
      attrs: { type: "button", style: "margin-top:18px" },
      on: {
        click: () => {
          state.q = "";
          state.typeFilter = "all";
          state.activeTag = null;
          renderTags();
          render();
        },
      },
    })
  );
  return wrap;
}

function renderLibrary() {
  const screen = el("div", { class: "screen" }, [
    libraryHeader(),
    el("div", { class: "scroll" }, [el("div", { class: "result-list", attrs: { id: "result-list" } })]),
  ]);
  content.appendChild(screen);
  loadResults();
}

// ---- watch later queue -------------------------------------------------

function watchCheck(item, on) {
  return el("button", {
    class: "check" + (on ? " on" : ""),
    text: on ? "✓" : "",
    attrs: { type: "button", title: on ? "Mark unwatched" : "Mark watched" },
    on: {
      click: async (e) => {
        e.stopPropagation();
        await fetch(`/api/watch-later/${item.id}/toggle`, { method: "POST" });
        await refreshSummary();
        render();
      },
    },
  });
}

function upNextCard(item) {
  const media = thumbBox("upnext-thumb", item, { label: "video thumbnail", play: "play-lg" });

  const openX = el("a", {
    text: "open on x ↗",
    attrs: { href: xUrl(item), target: "_blank", rel: "noopener noreferrer" },
    on: { click: (e) => e.stopPropagation() },
  });
  const markBtn = el("button", {
    class: "btn-ghost",
    text: "✓ mark watched",
    attrs: { type: "button" },
    on: {
      click: async (e) => {
        e.stopPropagation();
        await fetch(`/api/watch-later/${item.id}/toggle`, { method: "POST" });
        await refreshSummary();
        render();
      },
    },
  });

  const info = el("div", { attrs: { style: "min-width:0" } }, [
    el("div", { class: "upnext-kicker", text: "up next" }),
    el("div", { class: "upnext-title", text: item.title || item.text || "Untitled video" }),
    el("div", {
      class: "upnext-meta",
      text: `${item.author_name || item.author_username} · @${item.author_username} · saved ${formatDate(item.created_at)}`,
    }),
    el("div", { class: "upnext-snippet", text: item.text || "" }),
    el("div", { class: "upnext-actions" }, [markBtn, openX]),
  ]);

  return el("div", { class: "upnext", on: { click: () => openDetail(item.id) } }, [media, info]);
}

function queueRow(item, num) {
  const media = thumbBox("qthumb", item, {});
  return el(
    "div",
    { class: "qrow", on: { click: () => openDetail(item.id) } },
    [
      el("span", { class: "qnum", text: num }),
      watchCheck(item, false),
      media,
      el("div", { attrs: { style: "min-width:0" } }, [
        el("div", { class: "qtitle", text: item.title || item.text || "Untitled" }),
        el("div", { class: "qmeta", text: `${item.author_name || item.author_username} · @${item.author_username}` }),
      ]),
      el("span", { class: "qdate", text: formatDate(item.created_at) }),
    ]
  );
}

function watchedRow(item) {
  return el(
    "div",
    { class: "qrow watched", on: { click: () => openDetail(item.id) } },
    [
      el("span", {}),
      watchCheck(item, true),
      el("div", { class: "qthumb thumb" }),
      el("div", { attrs: { style: "min-width:0" } }, [
        el("div", { class: "qtitle", text: item.title || item.text || "Untitled" }),
        el("div", { class: "qmeta", text: `${item.author_name || item.author_username}` }),
      ]),
      el("span", { class: "qdate", text: formatDate(item.created_at) }),
    ]
  );
}

async function renderQueue() {
  let items = [];
  try {
    items = await getJSON("/api/watch-later?status=");
  } catch {
    items = [];
  }
  const unwatched = items.filter((i) => i.watch_status === "unwatched");
  const watched = items.filter((i) => i.watch_status === "watched");

  const wrap = el("div", { class: "queue-wrap" });
  wrap.appendChild(
    el("div", { class: "queue-head" }, [
      el("h1", { class: "queue-title", text: "Watch Later" }),
      el("span", { class: "queue-sub", text: `${unwatched.length} unwatched` }),
    ])
  );

  if (!unwatched.length && !watched.length) {
    wrap.appendChild(
      el("div", {
        class: "empty-body",
        attrs: { style: "margin-top:24px" },
        text: "No videos queued yet. Videos are added here automatically when a bookmarked YouTube link is synced.",
      })
    );
  }

  if (unwatched.length) {
    wrap.appendChild(upNextCard(unwatched[0]));
    const rest = unwatched.slice(1);
    if (rest.length) {
      wrap.appendChild(el("div", { class: "queue-section-label", text: "in the queue" }));
      rest.forEach((item, i) => wrap.appendChild(queueRow(item, String(i + 2).padStart(2, "0"))));
    }
  }

  if (watched.length) {
    wrap.appendChild(el("div", { class: "queue-section-label", text: `watched · ${watched.length}` }));
    watched.forEach((item) => wrap.appendChild(watchedRow(item)));
  }

  content.appendChild(el("div", { class: "screen" }, [el("div", { class: "scroll" }, [wrap])]));
}

// ---- item detail -------------------------------------------------------

async function openDetail(id) {
  state.from = state.view === "detail" ? state.from : state.view;
  state.selectedId = id;
  state.view = "detail";
  render();
}

async function renderDetail() {
  let d = null;
  try {
    d = await getJSON(`/api/bookmark/${state.selectedId}`);
  } catch {
    d = null;
  }
  const wrap = el("div", { class: "detail-wrap" });

  wrap.appendChild(
    el("button", {
      class: "back-btn",
      text: "← back",
      attrs: { type: "button" },
      on: { click: () => { state.view = state.from || "library"; render(); } },
    })
  );

  if (!d) {
    wrap.appendChild(el("div", { class: "empty-body", attrs: { style: "margin-top:24px" }, text: "Couldn't load that bookmark." }));
    content.appendChild(el("div", { class: "screen" }, [el("div", { class: "scroll" }, [wrap])]));
    return;
  }

  const t = TYPES[d.type] || TYPES.tweet;

  wrap.appendChild(
    el("div", { class: "detail-type" }, [
      el("div", { class: `detail-glyph glyph type-${d.type}`, text: t.glyph }),
      el("span", { class: `detail-typelabel type-${d.type}`, text: typeLabel(d) }),
      el("span", { class: "detail-saved", text: `saved ${formatDate(d.created_at)}` }),
    ])
  );

  wrap.appendChild(
    el("div", { class: "detail-author" }, [
      el("div", { class: "avatar", text: initials(d.author_name, d.author_username) }),
      el("div", {}, [
        el("div", { class: "detail-name", text: d.author_name || d.author_username }),
        el("div", { class: "detail-handle", text: "@" + d.author_username }),
      ]),
      el("a", {
        class: "detail-openx",
        text: "open on x ↗",
        attrs: { href: xUrl(d), target: "_blank", rel: "noopener noreferrer" },
      }),
    ])
  );

  if (d.title) wrap.appendChild(el("h1", { class: "detail-title", text: d.title }));

  // body varies by type
  if (d.type === "tweet") {
    wrap.appendChild(el("p", { class: "detail-tweet", text: d.text || "" }));
  } else if (d.type === "thread" && d.posts && d.posts.length) {
    const posts = el("div", { class: "thread-posts" });
    d.posts.forEach((text, i) => {
      posts.appendChild(
        el("div", { class: "thread-post" }, [
          el("span", { class: "thread-num", text: String(i + 1).padStart(2, "0") }),
          el("p", { class: "thread-text", text }),
        ])
      );
    });
    wrap.appendChild(posts);
  } else {
    wrap.appendChild(el("p", { class: "detail-lead", text: d.text || "" }));
  }

  // linked media (video embed / article summary / transcript)
  const video = (d.linked || []).find((l) => l.type === "youtube");
  const article = (d.linked || []).find((l) => l.type === "article");

  if (d.type === "video") {
    const embed = thumbBox("video-embed", d, { label: "embedded video", play: "play-xl" });
    const box = el("div", { class: "detail-video" }, [embed]);
    if (video && video.content) {
      box.appendChild(
        el("div", { class: "excerpt-card" }, [
          el("div", { class: "excerpt-label", text: "transcript excerpt" }),
          el("p", { class: "excerpt-text transcript", text: video.content }),
        ])
      );
    }
    wrap.appendChild(box);
  } else if (d.type === "article" && article) {
    if (article.content) {
      wrap.appendChild(
        el("div", { attrs: { style: "margin-top:20px" } }, [
          el("div", { class: "excerpt-card", attrs: { style: "margin-top:0" } }, [
            el("div", { class: "excerpt-label", text: "summary" }),
            el("p", { class: "excerpt-text", text: article.content }),
          ]),
        ])
      );
    }
    let domain = "";
    try {
      domain = new URL(article.url).hostname.replace(/^www\./, "");
    } catch {
      domain = article.url;
    }
    const src = el("div", { class: "detail-source" }, [document.createTextNode("source: ")]);
    src.appendChild(
      el("a", { text: `${domain} ↗`, attrs: { href: article.url, target: "_blank", rel: "noopener noreferrer" } })
    );
    wrap.appendChild(src);
  }

  wrap.appendChild(tagsEditor(d));

  content.appendChild(el("div", { class: "screen" }, [el("div", { class: "scroll" }, [wrap])]));
}

function tagsEditor(d) {
  const editor = el("div", { class: "tags-editor" });
  editor.appendChild(
    el("div", { class: "tags-editor-head" }, [
      el("span", { class: "tags-editor-title", text: "tags" }),
      el("span", { class: "tags-editor-hint", text: "auto-generated · click × to correct" }),
    ])
  );
  const list = el("div", { class: "tags-editor-list" });

  const rebuild = (tags) => {
    clear(list);
    for (const name of tags) {
      const x = el("button", {
        class: "edit-pill-x",
        text: "×",
        attrs: { type: "button", title: "Remove tag" },
        on: {
          click: async () => {
            const res = await fetch(`/api/bookmark/${d.id}/tags/${encodeURIComponent(name)}`, { method: "DELETE" });
            const data = await res.json();
            rebuild(data.tags);
            renderTags();
          },
        },
      });
      list.appendChild(el("span", { class: "edit-pill" }, [document.createTextNode(name + " "), x]));
    }
    const input = el("input", {
      class: "tag-add",
      attrs: { placeholder: "add tag…", value: "" },
      on: {
        keydown: async (e) => {
          if (e.key !== "Enter") return;
          const name = e.target.value.trim();
          if (!name) return;
          const res = await fetch(`/api/bookmark/${d.id}/tags`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name }),
          });
          if (res.ok) {
            const data = await res.json();
            rebuild(data.tags);
            renderTags();
            const fresh = list.querySelector(".tag-add");
            if (fresh) fresh.focus();
          }
        },
      },
    });
    list.appendChild(input);
  };

  rebuild(d.tags || []);
  editor.appendChild(list);
  return editor;
}

// ---- first-run empty state --------------------------------------------

function renderFirstRun() {
  const inner = el("div", { class: "empty-inner" }, [
    el("div", { class: "empty-quote", text: "“" }),
    el("div", { class: "empty-title", text: "Nothing saved here yet." }),
    el("div", {
      class: "empty-body",
      text:
        "Run your first sync to pull in your X bookmarks — text, threads, article summaries and video transcripts, all searchable.",
    }),
  ]);

  if (state.syncing) {
    inner.appendChild(
      el("div", { class: "empty-sync" }, [
        el("div", { class: "sync-label", text: state.syncLabel || "syncing…" }),
        el("div", { class: "progress" }, [el("div", { class: "progress-bar" })]),
      ])
    );
  } else {
    inner.appendChild(
      el("button", {
        class: "empty-cta",
        text: "Sync bookmarks",
        attrs: { type: "button" },
        on: { click: runSync },
      })
    );
  }
  if (state.syncError) inner.appendChild(el("div", { class: "empty-note sync-error", text: state.syncError }));
  inner.appendChild(el("div", { class: "empty-note", text: "reads are billed by x · usually under a minute" }));

  content.appendChild(el("div", { class: "empty" }, [inner]));
}

// ---- sync flow ---------------------------------------------------------

async function runSync() {
  if (state.syncing) return;
  state.syncing = true;
  state.syncError = null;
  state.syncLabel = "fetching bookmarks…";
  renderSyncBox();
  render();
  try {
    const syncRes = await fetch("/api/sync", { method: "POST" });
    const syncData = await syncRes.json();
    if (!syncRes.ok) throw new Error(syncData.detail || "Sync failed");

    state.syncLabel = "enriching…";
    renderSyncBox();
    const enrichRes = await fetch("/api/enrich", { method: "POST" });
    const enrichData = await enrichRes.json();
    if (!enrichRes.ok) throw new Error(enrichData.detail || "Enrichment failed");

    state.syncing = false;
    await refreshSummary();
    await renderTags();
    render();
  } catch (e) {
    state.syncing = false;
    state.syncError = "sync failed: " + e.message;
    renderSyncBox();
    render();
  }
}

async function refreshSummary() {
  try {
    summary = await getJSON("/api/summary");
  } catch {
    /* keep last-known summary */
  }
  renderSidebarCounts();
}

// ---- router ------------------------------------------------------------

let searchInputRef = null;

function render() {
  clear(content);
  setNav();
  renderSyncBox();

  const firstRun = summary.total === 0 && !state.q && !state.activeTag && state.typeFilter === "all";
  if (firstRun && state.view !== "detail") {
    renderFirstRun();
    return;
  }

  if (state.view === "queue") {
    renderQueue();
  } else if (state.view === "detail") {
    renderDetail();
  } else {
    renderLibrary();
  }
}

// ---- theme -------------------------------------------------------------

function applyTheme() {
  root.setAttribute("data-app-theme", state.theme);
  renderThemeToggle();
}

function initTheme() {
  const stored = localStorage.getItem("xbm-theme");
  if (stored === "light" || stored === "dark") {
    state.theme = stored;
  } else {
    state.theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  applyTheme();
}

// ---- wiring ------------------------------------------------------------

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.view = btn.dataset.view;
    render();
  });
});

document.getElementById("theme-toggle").addEventListener("click", () => {
  state.theme = state.theme === "dark" ? "light" : "dark";
  localStorage.setItem("xbm-theme", state.theme);
  applyTheme();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/INPUT|TEXTAREA/.test((e.target && e.target.tagName) || "")) {
    e.preventDefault();
    if (state.view !== "library") {
      state.view = "library";
      render();
    }
    if (searchInputRef) searchInputRef.focus();
  }
});

async function init() {
  initTheme();
  await refreshSummary();
  await renderTags();
  render();
}

init();
