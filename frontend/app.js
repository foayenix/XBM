// All bookmark/tag/author content below comes from other people's tweets on
// X, so it's untrusted. Every render path here builds DOM nodes via
// textContent/createElement instead of innerHTML, so nothing in a bookmark
// can execute as HTML/script in this page.

const SNIPPET_START = "";
const SNIPPET_END = "";

const state = {
  q: "",
  tags: new Set(),
  wlStatus: "unwatched",
};

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

function buildCard(bookmark, opts = {}) {
  const card = document.createElement("article");
  card.className = "card";

  if (bookmark.thumbnail) {
    const img = document.createElement("img");
    img.className = "thumb";
    img.src = bookmark.thumbnail;
    img.alt = "";
    img.loading = "lazy";
    card.appendChild(img);
  }

  const body = document.createElement("div");
  body.className = "card-body";

  const meta = document.createElement("div");
  meta.className = "meta";

  const author = document.createElement("span");
  author.className = "author";
  author.textContent = bookmark.author_name || bookmark.author_username;
  meta.appendChild(author);

  const handle = document.createElement("span");
  handle.textContent = "@" + bookmark.author_username;
  meta.appendChild(handle);

  if (bookmark.is_thread) {
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = "thread";
    meta.appendChild(badge);
  }

  const date = document.createElement("span");
  date.textContent = new Date(bookmark.created_at).toLocaleDateString();
  meta.appendChild(date);

  body.appendChild(meta);

  const textEl = document.createElement("p");
  textEl.className = "text";
  renderSnippetInto(textEl, bookmark.snippet || bookmark.text || "");
  body.appendChild(textEl);

  if (bookmark.tags && bookmark.tags.length) {
    const tagsEl = document.createElement("div");
    tagsEl.className = "tags";
    for (const t of bookmark.tags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = t;
      tagsEl.appendChild(chip);
    }
    body.appendChild(tagsEl);
  }

  const actions = document.createElement("div");
  actions.className = "card-actions";

  if (opts.showWatchToggle) {
    const btn = document.createElement("button");
    btn.className = "watch-toggle";
    btn.type = "button";
    btn.textContent = bookmark.watch_status === "watched" ? "Mark unwatched" : "Mark watched";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`/api/watch-later/${bookmark.id}/toggle`, { method: "POST" });
      opts.onToggle?.();
    });
    actions.appendChild(btn);
  }

  const link = document.createElement("a");
  link.href = `https://x.com/${bookmark.author_username}/status/${bookmark.id}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "open-link";
  link.textContent = "Open on X ->";
  actions.appendChild(link);

  body.appendChild(actions);
  card.appendChild(body);
  return card;
}

function renderCards(container, items, opts = {}) {
  container.textContent = "";
  for (const b of items) {
    container.appendChild(buildCard(b, opts));
  }
}

async function runSearch() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  for (const t of state.tags) params.append("tag", t);

  const res = await fetch("/api/search?" + params.toString());
  const data = await res.json();

  renderCards(document.getElementById("results"), data.results);
  const statusEl = document.getElementById("results-status");
  statusEl.textContent = data.total === 1 ? "1 bookmark" : `${data.total} bookmarks`;
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
    btn.className = "tag-filter" + (state.tags.has(t.name) ? " active" : "");
    btn.textContent = `${t.name} (${t.count})`;
    btn.addEventListener("click", () => {
      if (state.tags.has(t.name)) {
        state.tags.delete(t.name);
      } else {
        state.tags.add(t.name);
      }
      loadTags();
      runSearch();
    });
    li.appendChild(btn);
    list.appendChild(li);
  }
}

async function loadWatchLater() {
  const params = new URLSearchParams();
  if (state.wlStatus) params.set("status", state.wlStatus);

  const res = await fetch("/api/watch-later?" + params.toString());
  const items = await res.json();

  renderCards(document.getElementById("watch-later-results"), items, {
    showWatchToggle: true,
    onToggle: loadWatchLater,
  });
  const statusEl = document.getElementById("watch-later-status");
  statusEl.textContent = items.length === 1 ? "1 video" : `${items.length} videos`;
}

function setupViewSwitching() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const view = btn.dataset.view;
      document.getElementById("search-view").hidden = view !== "search";
      document.getElementById("watch-later-view").hidden = view !== "watch-later";
      if (view === "watch-later") loadWatchLater();
    });
  });
}

function setupWatchLaterFilters() {
  document.querySelectorAll(".wl-filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".wl-filter").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
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

function setupSyncButton() {
  const btn = document.getElementById("sync-btn");
  const statusEl = document.getElementById("sync-status");

  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      statusEl.textContent = "Syncing bookmarks...";
      const syncRes = await fetch("/api/sync", { method: "POST" });
      const syncData = await syncRes.json();
      if (!syncRes.ok) throw new Error(syncData.detail || "Sync failed");

      statusEl.textContent = "Enriching...";
      const enrichRes = await fetch("/api/enrich", { method: "POST" });
      const enrichData = await enrichRes.json();
      if (!enrichRes.ok) throw new Error(enrichData.detail || "Enrichment failed");

      statusEl.textContent =
        `Synced ${syncData.new} new, ${syncData.updated} updated. ` +
        `Enriched ${enrichData.linked_content_done} links, tagged ${enrichData.bookmarks_tagged} bookmarks.`;

      await loadTags();
      await runSearch();
    } catch (err) {
      statusEl.textContent = "Error: " + err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

setupViewSwitching();
setupWatchLaterFilters();
setupSearchInput();
setupSyncButton();
loadTags();
runSearch();
