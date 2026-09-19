// All bookmark/tag/author content below comes from other people's tweets on
// X, so it's untrusted. Every render path here builds DOM nodes via
// textContent/createElement instead of innerHTML, so nothing in a bookmark
// can execute as HTML/script in this page.

const SNIPPET_START = "\x01";
const SNIPPET_END = "\x02";

const PAGE_SIZE = 30;

const KIND_LABEL = { thread: "THR", video: "VID", article: "ART", tweet: "TWT" };

const state = {
  q: "",
  tags: new Set(),
  wlStatus: "unwatched",
  offset: 0,
  loading: false,
  view: "search",
};

// Status text is the only feedback channel in this UI, so an error must not
// look identical to "75 bookmarks". `is-error` recolours it, and the
// aria-live region on these elements announces either kind.
function setStatus(el, message, { isError = false } = {}) {
  el.textContent = message;
  el.classList.toggle("is-error", isError);
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

function renderSnippetInto(el, text) {
  el.textContent = "";
  let i = 0;
  while (i < text.length) {
    const start = text.indexOf(SNIPPET_START, i);
    if (start === -1) {
      el.appendChild(document.createTextNode(text.slice(i)));
      return;
    }
    el.appendChild(document.createTextNode(text.slice(i, start)));
    const end = text.indexOf(SNIPPET_END, start + 1);
    if (end === -1) {
      el.appendChild(document.createTextNode(text.slice(start + 1)));
      return;
    }
    const mark = document.createElement("mark");
    mark.textContent = text.slice(start + 1, end);
    el.appendChild(mark);
    i = end + 1;
  }
}

function eyeIcon(watched) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", watched ? "M20 6L9 17l-5-5" : "M12 5v14M5 12h14");
  svg.appendChild(path);
  return svg;
}

// One row. The whole row is the link to X, so Enter on a focused row opens
// it natively and `o` has something real to activate.
function buildRow(bookmark, opts = {}) {
  const row = document.createElement("a");
  row.className = "row";
  row.href = `https://x.com/${bookmark.author_username}/status/${bookmark.id}`;
  row.target = "_blank";
  row.rel = "noopener noreferrer";
  row.dataset.id = String(bookmark.id);

  if (opts.showThumb && bookmark.thumbnail) {
    const img = document.createElement("img");
    img.className = "row-thumb";
    img.src = bookmark.thumbnail;
    img.alt = "";
    img.loading = "lazy";
    // YouTube poster frames disappear when a video is deleted, and tweet
    // media 404s eventually too. Drop the element rather than showing the
    // browser's broken-image icon.
    img.addEventListener("error", () => img.remove());
    row.appendChild(img);
  }

  const author = document.createElement("span");
  author.className = "row-author";
  author.textContent = bookmark.author_name || bookmark.author_username;
  row.appendChild(author);

  const mainEl = document.createElement("span");
  mainEl.className = "row-main";

  const kind = bookmark.kind || "tweet";
  const badge = document.createElement("span");
  badge.className = `kind kind-${kind}`;
  badge.textContent = KIND_LABEL[kind] || KIND_LABEL.tweet;
  // The three-letter badge is an abbreviation; give it the full word too.
  badge.title = kind;
  mainEl.appendChild(badge);

  const textEl = document.createElement("span");
  textEl.className = "row-text";
  renderSnippetInto(textEl, bookmark.snippet || bookmark.text || "");
  mainEl.appendChild(textEl);
  row.appendChild(mainEl);

  const tagsEl = document.createElement("span");
  tagsEl.className = "row-tags";
  tagsEl.textContent = (bookmark.tags || []).join(" ");
  row.appendChild(tagsEl);

  const date = document.createElement("span");
  date.className = "row-date";
  const d = new Date(bookmark.created_at);
  date.textContent = d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
  row.appendChild(date);

  if (opts.showWatchToggle) {
    const watched = bookmark.watch_status === "watched";
    const btn = document.createElement("button");
    btn.className = "watch-toggle";
    btn.type = "button";
    btn.setAttribute("aria-pressed", String(watched));
    btn.setAttribute("aria-label", watched ? "Mark as unwatched" : "Mark as watched");
    btn.appendChild(eyeIcon(watched));
    btn.addEventListener("click", async (e) => {
      // The row is a link; a click on its toggle must not also navigate.
      e.preventDefault();
      e.stopPropagation();
      btn.disabled = true;
      await toggleWatchLater(bookmark.id);
      opts.onToggle?.();
    });
    row.appendChild(btn);
  }

  return row;
}

function renderRows(container, items, opts = {}) {
  if (!opts.append) container.textContent = "";
  for (const b of items) container.appendChild(buildRow(b, opts));
}

function showEmpty(container, message, { isError = false } = {}) {
  container.textContent = "";
  const p = document.createElement("p");
  p.className = "empty" + (isError ? " is-error" : "");
  p.textContent = message;
  container.appendChild(p);
}

async function toggleWatchLater(id) {
  await fetch(`/api/watch-later/${id}/toggle`, { method: "POST" });
  loadStats();
}

// --- data loading ---------------------------------------------------------

async function loadStats() {
  try {
    const res = await fetch("/api/stats");
    if (!res.ok) return;
    const s = await res.json();
    document.getElementById("count-all").textContent = s.bookmarks;
    document.getElementById("count-watch").textContent = s.watch_later_unwatched;

    // The counts already sit in the rail and in the live result count, so
    // the status bar only carries what nothing else shows.
    const info = document.getElementById("statusbar-info");
    info.textContent = s.last_synced_at
      ? `synced ${relativeTime(s.last_synced_at)}`
      : "never synced";
  } catch {
    /* the status bar is decoration; never break the page over it */
  }
}

function relativeTime(iso) {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "recently";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

async function runSearch({ append = false } = {}) {
  if (state.loading) return;
  state.loading = true;

  const moreBtn = document.getElementById("load-more");
  const statusEl = document.getElementById("results-status");
  const container = document.getElementById("results");

  if (!append) state.offset = 0;

  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  for (const t of state.tags) params.append("tag", t);
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(state.offset));

  try {
    const res = await fetch("/api/search?" + params.toString());
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Search failed");

    if (!append && data.total === 0) {
      showEmpty(container, state.q || state.tags.size ? "no matches" : "no bookmarks yet — hit sync");
      moreBtn.hidden = true;
      setStatus(statusEl, "No bookmarks");
      return;
    }

    renderRows(container, data.results, { append });
    state.offset += data.results.length;

    const shown = state.offset;
    setStatus(
      statusEl,
      shown < data.total
        ? `Showing ${shown} of ${data.total} bookmarks`
        : data.total === 1
          ? "1 bookmark"
          : `${data.total} bookmarks`
    );

    moreBtn.hidden = !data.has_more;
    moreBtn.textContent = `load ${Math.min(PAGE_SIZE, data.total - shown)} more`;
  } catch (err) {
    showEmpty(container, "Error: " + err.message, { isError: true });
    setStatus(statusEl, "Error: " + err.message, { isError: true });
    moreBtn.hidden = true;
  } finally {
    state.loading = false;
  }
}

async function loadTags() {
  const res = await fetch("/api/tags");
  const tags = await res.json();

  const list = document.getElementById("tag-list");
  list.textContent = "";
  for (const t of tags) {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tag-filter";
    btn.setAttribute("aria-pressed", String(state.tags.has(t.name)));

    const label = document.createElement("span");
    label.className = "nav-label";
    label.textContent = t.name;
    btn.appendChild(label);

    const count = document.createElement("span");
    count.className = "count";
    count.textContent = t.count;
    btn.appendChild(count);

    btn.addEventListener("click", () => {
      if (state.tags.has(t.name)) state.tags.delete(t.name);
      else state.tags.add(t.name);
      loadTags();
      runSearch();
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function loadWatchLater() {
  const container = document.getElementById("watch-later-results");
  const statusEl = document.getElementById("watch-later-status");

  const params = new URLSearchParams();
  if (state.wlStatus) params.set("status", state.wlStatus);

  const res = await fetch("/api/watch-later?" + params.toString());
  const items = await res.json();

  if (!items.length) {
    showEmpty(container, "nothing queued");
    setStatus(statusEl, "0 videos");
    setStatus(document.getElementById("results-status"), "0 videos");
    return;
  }

  renderRows(container, items, {
    showWatchToggle: true,
    showThumb: true,
    onToggle: loadWatchLater,
  });
  const label = items.length === 1 ? "1 video" : `${items.length} videos`;
  setStatus(statusEl, label);
  setStatus(document.getElementById("results-status"), label);
}

// --- keyboard -------------------------------------------------------------

function visibleRows() {
  const container =
    state.view === "search"
      ? document.getElementById("results")
      : document.getElementById("watch-later-results");
  return [...container.querySelectorAll(".row")];
}

function moveSelection(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  const current = rows.indexOf(document.activeElement.closest?.(".row") ?? document.activeElement);
  let next = current === -1 ? (delta > 0 ? 0 : rows.length - 1) : current + delta;
  next = Math.max(0, Math.min(rows.length - 1, next));
  rows[next].focus();
  rows[next].scrollIntoView({ block: "nearest" });
}

function isTyping(target) {
  return target instanceof HTMLElement &&
    (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
}

function setupKeyboard() {
  const searchInput = document.getElementById("search-input");

  document.addEventListener("keydown", (e) => {
    // Cmd/Ctrl-K reaches the search box from anywhere, including the box.
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
      return;
    }

    if (isTyping(e.target)) {
      if (e.key === "Escape") searchInput.blur();
      return;
    }
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    switch (e.key) {
      case "j":
        e.preventDefault();
        moveSelection(1);
        break;
      case "k":
        e.preventDefault();
        moveSelection(-1);
        break;
      case "/":
        e.preventDefault();
        searchInput.focus();
        break;
      case "o": {
        const row = document.activeElement.closest?.(".row");
        if (row) {
          e.preventDefault();
          window.open(row.href, "_blank", "noopener");
        }
        break;
      }
      case "w": {
        const row = document.activeElement.closest?.(".row");
        if (!row) break;
        e.preventDefault();
        const toggle = row.querySelector(".watch-toggle");
        if (toggle) toggle.click();
        break;
      }
      default:
        break;
    }
  });
}

// --- wiring ---------------------------------------------------------------

function setupViewSwitching() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.removeAttribute("aria-current"));
      btn.setAttribute("aria-current", "page");
      const view = btn.dataset.view;
      state.view = view;
      document.getElementById("search-view").hidden = view !== "search";
      document.getElementById("watch-later-view").hidden = view !== "watch-later";
      if (view === "watch-later") loadWatchLater();
    });
  });
}

function setupWatchLaterFilters() {
  document.querySelectorAll(".wl-filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".wl-filter").forEach((b) => b.setAttribute("aria-pressed", "false"));
      btn.setAttribute("aria-pressed", "true");
      state.wlStatus = btn.dataset.status;
      loadWatchLater();
    });
  });
}

function setupSearchInput() {
  document.getElementById("search-input").addEventListener(
    "input",
    debounce((e) => {
      state.q = e.target.value.trim();
      runSearch();
    }, 250)
  );
}

function setupLoadMore() {
  document.getElementById("load-more").addEventListener("click", () => {
    runSearch({ append: true });
  });
}

function setupSyncButton() {
  const btn = document.getElementById("sync-btn");
  const statusEl = document.getElementById("sync-status");

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      setStatus(statusEl, "syncing…");
      const syncRes = await fetch("/api/sync", { method: "POST" });
      const syncData = await syncRes.json();
      // 409 means the background scheduler (or another tab) already has a
      // run in flight - that is not an error worth alarming about.
      if (syncRes.status === 409) throw new Error(syncData.detail || "A sync is already running.");
      if (!syncRes.ok) throw new Error(syncData.detail || "Sync failed");

      setStatus(statusEl, "enriching…");
      const enrichRes = await fetch("/api/enrich", { method: "POST" });
      const enrichData = await enrichRes.json();
      if (enrichRes.status === 409) throw new Error(enrichData.detail || "An enrichment run is already in progress.");
      if (!enrichRes.ok) throw new Error(enrichData.detail || "Enrichment failed");

      setStatus(
        statusEl,
        `+${syncData.new} new, ${syncData.updated} updated, ${enrichData.bookmarks_tagged} tagged`
      );

      await Promise.all([loadTags(), runSearch(), loadStats()]);
    } catch (err) {
      setStatus(statusEl, err.message, { isError: true });
    } finally {
      btn.disabled = false;
    }
  });
}

setupViewSwitching();
setupWatchLaterFilters();
setupSearchInput();
setupSyncButton();
setupLoadMore();
setupKeyboard();
loadTags();
runSearch();
loadStats();
