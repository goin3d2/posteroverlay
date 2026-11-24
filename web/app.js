"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let settings = null, savedStyle = null, status = null;

async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (_) {}
    throw new Error(msg);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}
function toast(msg, err) {
  const d = document.createElement("div"); d.textContent = msg; if (err) d.className = "err";
  $("#toast").append(d); setTimeout(() => d.remove(), err ? 7000 : 3500);
}
const ago = (t) => { if (!t) return "never"; const s = Date.now() / 1000 - t;
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };

// ------------------------------------------------------------------------------------- tabs
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
  location.hash = name;
  if (name === "activity") loadActivity();
  if (name === "collections") { loadCollections(); loadServerCollections(); }
  if (name === "posters") { renderStyle(); schedulePreview(); fillServerSelect($("#pv-server")); }
  if (name === "servers") renderServers();
  if (name === "settings") renderGeneral();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

// ------------------------------------------------------------------------------- form helper
// spec: [key, label, type, extra]; type: check | range [min,max,unit] | color | text | password | select [options] | number
function fieldRow(obj, [key, label, type, extra, help], onChange) {
  const row = document.createElement("div"); row.className = "field";
  const id = "f_" + key + "_" + Math.random().toString(36).slice(2, 7);
  row.innerHTML = `<label for="${id}">${esc(label)}</label><div class="ctl"></div>` + (help ? `<div class="help">${esc(help)}</div>` : "");
  const ctl = $(".ctl", row); let input;
  if (type === "check") { input = Object.assign(document.createElement("input"), { type: "checkbox", checked: !!obj[key] }); }
  else if (type === "range") {
    input = Object.assign(document.createElement("input"), { type: "range", min: extra[0], max: extra[1], step: extra[3] || 1, value: obj[key] });
    const out = document.createElement("output"); out.textContent = obj[key] + (extra[2] || "");
    input.addEventListener("input", () => (out.textContent = input.value + (extra[2] || "")));
    ctl.append(input, out); input.id = id;
  } else if (type === "select") {
    input = document.createElement("select");
    for (const o of extra) { const [v, t] = Array.isArray(o) ? o : [o, o]; input.add(new Option(t, v, false, String(obj[key]) === String(v))); }
  } else { input = Object.assign(document.createElement("input"), { type: type === "color" ? "color" : type === "number" ? "number" : type, value: obj[key] ?? "" });
    if (type !== "color") input.placeholder = extra || ""; }
  if (!input.id) { input.id = id; ctl.append(input); }
  const read = () => type === "check" ? input.checked : (type === "range" || type === "number") ? Number(input.value) : input.value;
  input.addEventListener(type === "check" || type === "select" || type === "color" ? "change" : "input", () => { obj[key] = read(); onChange && onChange(key); });
  if (type === "color") input.addEventListener("input", () => { obj[key] = read(); onChange && onChange(key); });
  return row;
}
function fieldset(title, obj, fields, onChange, intro) {
  const fs = document.createElement("fieldset");
  fs.innerHTML = `<legend>${esc(title)}</legend>` + (intro ? `<p class="hint">${esc(intro)}</p>` : "");
  fields.forEach((f) => fs.append(fieldRow(obj, f, onChange)));
  return fs;
}

// ---------------------------------------------------------------------------------- status
async function refreshStatus() {
  try { status = await api("GET", "/api/status"); } catch (e) { return; }
  $("#version").textContent = "v" + status.version;
  const m = $("#mode");
  m.textContent = status.apply_changes ? "Applying changes to servers" : "Preview only";
  m.className = "mode " + (status.apply_changes ? "live" : "dry");
  const ban = $("#apply-banner");
  ban.classList.toggle("hidden", status.apply_changes);
  ban.innerHTML = "<b>Preview only.</b> Runs work out every poster and collection and save poster previews, but nothing is sent to Emby or Plex. Turn on <i>Apply changes to servers</i> in Settings when you're happy.";
  const q = status.mdblist_quota;
  const cards = [
    ...status.servers.map((s) => card(s.name, `${s.posters.applied || 0}`, `posters with overlays · ${s.kind}${s.enabled ? "" : " · off"}`)),
    card("Collections", status.collections, "defined here"),
    ...(status.memory_mb ? [card("Memory", `${status.memory_mb.toLocaleString()} MB`, "used by the container")] : []),
    card("Badge icons", `${status.icons.have}/${status.icons.total}`, status.icons.have < status.icons.total ? "some missing; they download on the first run" : "ready"),
    card("IMDb ratings", status.imdb_titles.toLocaleString(), "titles from the IMDb dataset"),
    card("MDBList", q ? `${Math.max(0, q.limit - q.used)}` : "—", q ? `requests left today of ${q.limit}` + (status.mdblist_waiting ? ` · ${status.mdblist_waiting.toLocaleString()} titles waiting for scores` : "") : "not used yet"),
    card("Next poster run", status.schedule.overlays.enabled ? `every ${status.schedule.overlays.hours} h` : "off", "last " + ago(status.schedule.overlays.last)),
    card("Next collection sync", status.schedule.collections.enabled ? `every ${status.schedule.collections.hours} h` : "off", "last " + ago(status.schedule.collections.last)),
  ];
  $("#status-cards").innerHTML = cards.join("");
  const jobs = Object.values(status.jobs).sort((a, b) => b.started - a.started);
  $("#jobs").innerHTML = jobs.length ? "<h2>Jobs</h2>" + jobs.map(jobHtml).join("") : "";
}
const card = (label, big, sub) => `<div class="card"><div class="label">${esc(label)}</div><div class="big">${esc(big)}</div><div class="hint">${esc(sub)}</div></div>`;
function jobHtml(j) {
  const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
  const names = { overlays: "Posters", collections: "Collections", restore: "Restore originals", icons: "Badge icons" };
  return `<div class="job"><div class="row between"><b>${esc(names[j.name] || j.name)}</b>
    ${j.running ? `<button class="small" onclick="cancelJob('${j.name}')">Stop</button>` : `<span class="hint">${ago(j.finished)}</span>`}</div>
    ${j.running ? `<div class="hint">${esc(j.phase)} ${j.total ? `${j.done} / ${j.total}` : ""}</div><div class="bar"><div style="width:${pct}%"></div></div>` : ""}
    ${j.result ? `<div class="hint" style="white-space:pre-wrap">${esc(j.result)}</div>` : ""}
    ${j.error ? `<div class="hint" style="color:var(--err)">${esc(j.error)}</div>` : ""}</div>`;
}
async function runJob(kind, params = {}) {
  const qs = new URLSearchParams(params).toString();
  try {
    const r = await api("POST", `/api/jobs/${kind}${qs ? "?" + qs : ""}`);
    toast(r.started ? "Started" : "Already running");
    refreshStatus();
  } catch (e) { toast(e.message, true); }
}
async function cancelJob(name) { await api("POST", `/api/jobs/${name}/cancel`); toast("Stopping…"); }

// ------------------------------------------------------------------------------------ style
const BADGE_NAMES = { resolution: "Resolution", hdr: "HDR / Dolby Vision", audio: "Audio", imdb: "IMDb", rtcritic: "Rotten Tomatoes critics", rtaudience: "Rotten Tomatoes audience", tmdb: "TMDB" };
function renderStyle() {
  const s = settings.style, f = $("#style-form"); f.innerHTML = "";
  const ch = () => schedulePreview();
  f.append(fieldset("Which posters", s, [["movies", "Movies", "check"], ["series", "Shows", "check"], ["seasons", "Seasons", "check"]], ch));
  f.append(fieldset("Status banner (shows)", s, [
    ["banner", "Show the banner", "check"], ["banner_position", "Position", "select", ["Top", "Bottom"]],
    ["banner_height", "Height", "range", [4, 20, "%"]], ["banner_opacity", "Opacity", "range", [20, 100, "%"]],
    ["banner_letter_spacing", "Letter spacing", "range", [0, 30]], ["text_font", "Font", "select", ["Regular", "Condensed"]],
    ["banner_text_color", "Text colour", "color"],
    ["airing_window_days", "Airing means an episode within", "range", [1, 60, " days"]], ["show_upcoming", "Show UPCOMING", "check"],
    ["airing_text", "Airing text", "text"], ["airing_color", "Airing colour", "color"],
    ["returning_text", "Returning text", "text"], ["returning_color", "Returning colour", "color"],
    ["ended_text", "Ended text", "text"], ["ended_color", "Ended colour", "color"],
    ["canceled_text", "Canceled text", "text"], ["canceled_color", "Canceled colour", "color"],
    ["upcoming_text", "Upcoming text", "text"], ["upcoming_color", "Upcoming colour", "color"],
  ], ch));
  f.append(fieldset("Badges", s, [
    ["badge_style", "Style", "select", [["Column", "One column"], ["Separate", "Separate badges"]]],
    ["badge_position", "Position", "select", [["TopLeft", "Top left"], ["TopRight", "Top right"], ["MiddleLeft", "Middle left"],
      ["BottomLeft", "Bottom left"], ["BottomRight", "Bottom right"], ["Auto", "Automatic (keep clear of titles)"]], "",
      "Automatic: top left unless the poster's title or other text is there, then the spot with the least text"],
    ["badge_height", "Badge size", "range", [3, 12, "%"]], ["badge_icon", "Icon size", "range", [50, 200, "%", 5]],
    ["badge_margin", "Margin", "range", [0, 10, "%"]], ["max_badges", "Most badges", "range", [1, 10]],
    ["badge_bg_color", "Background", "color"], ["badge_bg_opacity", "Background opacity", "range", [0, 100, "%"]],
    ["use_icons", "Use logo icons", "check", null, "Off: plain text labels"],
    ["show_resolution", "Resolution", "check"], ["show_hdr", "HDR / Dolby Vision", "check"], ["show_audio", "Audio", "check"],
    ["show_basic_audio", "Also show basic audio (DD, DTS, AAC)", "check"],
    ["show_quality_on_shows", "Quality badges on shows and seasons", "check"],
    ["quality_rule", "Show quality from", "select", [["LatestSeason", "Latest season"], ["BestAvailable", "Best available"], ["MostEpisodes", "Most episodes"]]],
    ["show_imdb", "IMDb rating", "check"], ["show_rt_critic", "Rotten Tomatoes critics", "check"],
    ["show_rt_audience", "Rotten Tomatoes audience", "check"], ["show_tmdb", "TMDB rating", "check"],
    ["show_ratings_on_seasons", "Ratings on seasons", "check"],
  ], ch));
  const fs = document.createElement("fieldset"); fs.innerHTML = "<legend>Badge order</legend>";
  const ord = document.createElement("div"); ord.className = "order";
  const draw = () => { ord.innerHTML = ""; s.badge_order.forEach((k, i) => { const d = document.createElement("div");
    d.innerHTML = `<span>${esc(BADGE_NAMES[k] || k)}</span><span><button class="small" ${i ? "" : "disabled"}>▲</button> <button class="small" ${i < s.badge_order.length - 1 ? "" : "disabled"}>▼</button></span>`;
    const [up, dn] = $$("button", d);
    up.onclick = () => { [s.badge_order[i - 1], s.badge_order[i]] = [s.badge_order[i], s.badge_order[i - 1]]; draw(); ch(); };
    dn.onclick = () => { [s.badge_order[i + 1], s.badge_order[i]] = [s.badge_order[i], s.badge_order[i + 1]]; draw(); ch(); };
    ord.append(d); }); };
  draw(); fs.append(ord); f.append(fs);
}
let pvTimer = null, pvUrl = null;
function schedulePreview() { clearTimeout(pvTimer); pvTimer = setTimeout(drawPreview, 150); }
async function drawPreview() {
  try {
    const r = await api("POST", "/api/preview", { style: settings.style, kind: $("#pv-kind").value, status: $("#pv-status").value });
    const url = URL.createObjectURL(await r.blob());
    $("#pv-img").src = url; if (pvUrl) URL.revokeObjectURL(pvUrl); pvUrl = url;
  } catch (e) { toast("Preview: " + e.message, true); }
}
$("#pv-kind").onchange = schedulePreview; $("#pv-status").onchange = schedulePreview;
async function saveStyle() { await saveSettings(); savedStyle = JSON.parse(JSON.stringify(settings.style)); }
function resetStyle() { settings.style = JSON.parse(JSON.stringify(savedStyle)); renderStyle(); schedulePreview(); }
function fillServerSelect(sel) {
  sel.innerHTML = settings.servers.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.kind)}</option>`).join("");
}
async function loadPreviews() {
  const sid = $("#pv-server").value; if (!sid) return;
  const list = await api("GET", `/api/previews/${sid}`);
  $("#previews").innerHTML = list.length ? list.map((p) => `<a href="/api/previews/${sid}/${p.item}.jpg" target="_blank" title="${esc(p.title)}"><img loading="lazy" src="/api/previews/${sid}/${p.item}.jpg?t=${p.time}"></a>`).join("")
    : '<p class="hint">No previews yet. Run "Update posters" while Preview only is on.</p>';
}

// ---------------------------------------------------------------------------------- settings
async function loadSettings() { settings = await api("GET", "/api/settings"); savedStyle = JSON.parse(JSON.stringify(settings.style)); }
async function saveSettings() {
  try { settings = await api("PUT", "/api/settings", settings); toast("Saved"); refreshStatus(); }
  catch (e) { toast("Not saved: " + e.message, true); }
}
function renderGeneral() {
  const f = $("#general-form"); f.innerHTML = "";
  f.append(fieldset("Safety", settings, [
    ["apply_changes", "Apply changes to servers", "check", null, "Off: preview only. Nothing on Emby or Plex is changed."],
    ["remove_missing", "Remove titles that leave a collection's sources", "check"],
  ]));
  f.append(fieldset("Schedule", settings, [
    ["overlays_enabled", "Update posters automatically", "check"], ["overlay_hours", "Every (hours)", "number"],
    ["collections_enabled", "Sync collections automatically", "check"], ["collection_hours", "Every (hours)", "number"],
  ]));
  f.append(fieldset("Ratings and status", settings, [
    ["tmdb_key", "TMDB API key", "password", "v3 key or v4 read token", "Show status (airing, ended…) and TMDB collections"],
    ["mdblist_key", "MDBList API key", "password", "", "Rotten Tomatoes scores and MDBList lists"],
    ["mdblist_reserve", "Keep this many MDBList requests spare each day", "number"],
    ["mdblist_per_run", "Most MDBList lookups per run", "number", null, "Keeps runs short; titles not reached get their Rotten Tomatoes scores on later runs. 0: no limit"],
    ["imdb_dataset", "IMDb ratings from the IMDb dataset", "check", null, "Free daily file; saves MDBList requests"],
    ["missing_images_per_run", "Fetch missing posters and backgrounds, at most this many titles per run", "number", null, "Emby only: titles with no poster or background are refreshed online (nothing is replaced); each one at most once a day. 0: off"],
  ]));
  const imp = document.createElement("fieldset");
  imp.innerHTML = `<legend>Import from the Emby Poster Overlays plugin</legend>
    <p class="hint">Saves the first run from fetching every rating and show status again. Pick <code>overlay-data.json</code> from Emby's config folder, under <code>plugins/PosterOverlays/</code>.</p>
    <div class="row"><input type="file" accept=".json,application/json"><button type="button" class="small">Import</button><span class="hint"></span></div>`;
  const [file, go, out] = [$("input", imp), $("button", imp), $(".row .hint", imp)];
  go.onclick = async () => {
    if (!file.files[0]) return toast("Choose overlay-data.json first", true);
    out.textContent = "Importing…";
    try {
      const r = await fetch("/api/import/emby-plugin", { method: "POST", body: file.files[0] });
      const j = await r.json(); if (!r.ok) throw new Error(j.detail || r.statusText);
      out.textContent = j.message; refreshStatus();
    } catch (e) { out.textContent = ""; toast(e.message, true); }
  };
  f.append(imp);
  f.append(fieldset("Files", settings, [
    ["xtream_categories_path", "xtreamfs categories.json", "text", "/xtreamfs/categories.json"],
    ["icon_source", "Badge icon source", "url"],
    ["original_max_width", "Keep originals up to (pixels wide)", "number", null, "Posters are drawn and uploaded at this size. 1000 looks sharp on a TV and keeps the config folder small."],
  ]));
}

// ---------------------------------------------------------------------------------- servers
function addServer(kind) {
  settings.servers.push({ id: Math.random().toString(36).slice(2, 10), kind, name: kind === "emby" ? "Emby" : "Plex", url: "", token: "", enabled: true, libraries: [], path_map_from: "", path_map_to: "", originals_from: "", skip_paths: [] });
  renderServers();
}
function renderServers() {
  const box = $("#server-list"); box.innerHTML = "";
  if (!settings.servers.length) box.innerHTML = '<p class="hint">No servers yet. Add your Emby and Plex servers.</p>';
  settings.servers.forEach((s, i) => {
    const el = document.createElement("div"); el.className = "server-box";
    el.innerHTML = `<div class="head"><b>${esc(s.kind === "emby" ? "Emby" : "Plex")}</b><span class="row">
      <button class="small" data-a="test">Test and list libraries</button><button class="small" data-a="restore">Restore original posters</button><button class="small danger" data-a="del">Remove</button></span></div>`;
    el.append(fieldRow(s, ["enabled", "Enabled", "check"]));
    el.append(fieldRow(s, ["name", "Name", "text"]));
    el.append(fieldRow(s, ["url", "Address", "url", s.kind === "emby" ? "http://192.168.1.10:8096" : "http://192.168.1.10:32400"]));
    el.append(fieldRow(s, ["token", s.kind === "emby" ? "API key" : "Plex token", "password", "",
      s.kind === "emby" ? "Emby dashboard → Advanced → API Keys" : "See README: Finding your Plex token"]));
    if (s.kind === "emby") {
      el.append(fieldRow(s, ["path_map_from", "xtreamfs path in categories.json", "text", "/vod", "Only if Emby sees the .strm files under another path"]));
      el.append(fieldRow(s, ["path_map_to", "Same folder as Emby sees it", "text", "/mnt/xtreamfs"]));
    }
    const others = settings.servers.filter((o) => o.id !== s.id);
    if (others.length) el.append(fieldRow(s, ["originals_from", "Draw on original posters from", "select",
      [["", "This server's own posters"], ...others.map((o) => [o.id, `${o.name || o.kind} when the title is there too`])],
      "Keeps both servers' artwork identical, and stores no second copy of each original"]));
    const skip = { v: (s.skip_paths || []).join(", ") };
    el.append(fieldRow(skip, ["v", "Skip titles in folders", "text", "/vod, /mnt/media/Other", "Comma separated. Titles whose file is under these folders get no overlay"], () => {
      s.skip_paths = skip.v.split(",").map((x) => x.trim()).filter(Boolean); }));
    const libs = document.createElement("div"); libs.className = "field";
    libs.innerHTML = `<label>Libraries</label><div class="ctl libs">${s.libraries.length ? s.libraries.map((l) => `<span class="pill">${esc(l)}</span>`).join("") : '<span class="hint">All movie and TV libraries. Test to choose.</span>'}</div>`;
    el.append(libs);
    const msg = document.createElement("div"); msg.className = "hint"; el.append(msg);
    $('[data-a="del"]', el).onclick = () => { if (confirm(`Remove ${s.name}?`)) { settings.servers.splice(i, 1); renderServers(); } };
    $('[data-a="restore"]', el).onclick = () => { if (confirm(`Put every original poster back on ${s.name}?`)) runJob("restore", { server: s.id }); };
    $('[data-a="test"]', el).onclick = async () => {
      msg.textContent = "Testing…";
      const r = await api("POST", "/api/servers/test", s);
      msg.innerHTML = (r.ok ? "✔ " : "✖ ") + esc(r.message) + r.warnings.map((w) => `<div class="notice">${esc(w)}</div>`).join("");
      if (r.ok) {
        const ctl = $(".libs", libs); ctl.innerHTML = "";
        r.libraries.forEach((l) => { const lab = document.createElement("label");
          const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked: !s.libraries.length || s.libraries.includes(l.id) });
          cb.onchange = () => { const all = $$("input", ctl); const on = all.filter((c) => c.checked).map((c) => c.dataset.id);
            s.libraries = on.length === all.length ? [] : on; };
          cb.dataset.id = l.id; lab.append(cb, `${l.name} (${l.kind})`); ctl.append(lab); });
      }
    };
    box.append(el);
  });
}

// ------------------------------------------------------------------------------ collections
const SRC_TYPES = [["mdblist", "MDBList / IMDb list"], ["tmdb_collection", "TMDB collection"], ["xtream", "xtreamfs category"],
  ["server_collection", "Collection on a server"], ["label", "Tag / label"]];
const SRC_HINT = { mdblist: "List URL, user/list-name, or external/<id> for an IMDb list added in MDBList", tmdb_collection: "TMDB collection id, e.g. 10194",
  xtream: "movie:<id> or series:<id>", server_collection: "<server id>:<collection name>", label: "Emby tag / Plex label" };
let collections = [];
async function loadServerCollections() {
  const box = $("#server-cols");
  const servers = settings.servers.filter((s) => s.url);
  if (!servers.length) { box.innerHTML = '<p class="hint">No servers set up yet.</p>'; return; }
  box.innerHTML = '<p class="hint">Loading…</p>';
  const parts = await Promise.all(servers.map(async (s) => {
    try { return [s, await api("GET", `/api/servers/${s.id}/collections`)]; } catch (e) { return [s, e]; }
  }));
  box.innerHTML = "";
  for (const [s, list] of parts) {
    const h = document.createElement("h3"); h.textContent = (s.name || s.kind) + (s.enabled ? "" : " (disabled: nothing is synced here)"); box.append(h);
    if (list instanceof Error) { box.insertAdjacentHTML("beforeend", `<p class="hint">Could not read collections: ${esc(list.message)}</p>`); continue; }
    if (!list.length) { box.insertAdjacentHTML("beforeend", '<p class="hint">No collections.</p>'); continue; }
    const t = document.createElement("table"); t.className = "list";
    t.innerHTML = `<tr><th>Name</th>${s.kind === "plex" ? "<th>Library</th>" : ""}<th>Titles</th><th>Home screen</th><th>Managed here</th><th></th></tr>` +
      list.map((c, i) => `<tr><td><b>${esc(c.name)}</b></td>${s.kind === "plex" ? `<td>${esc(c.library)}</td>` : ""}<td>${c.count}</td>
        <td class="hint" data-home="${i}">…</td>
        <td>${c.managed_by ? `<span class="pill">${esc(c.managed_by.name)}</span>` : '<span class="hint">no</span>'}</td>
        <td style="white-space:nowrap">${c.managed_by ? `<button class="small" data-edit="${esc(c.managed_by.id)}">Edit</button>` : `<button class="small" data-adopt="${i}">Manage here</button>`}
          <button class="small danger" data-del="${i}">Delete</button></td></tr>`).join("");
    box.append(t);
    api("GET", `/api/servers/${s.id}/collections/home`)
      .then((homes) => list.forEach((c, i) => { $(`[data-home="${i}"]`, t).textContent = homes[c.id] || "—"; }))
      .catch(() => list.forEach((c, i) => { $(`[data-home="${i}"]`, t).textContent = "?"; }));
    $$("[data-edit]", t).forEach((b) => (b.onclick = async () => { await loadCollections(); editCollection(b.dataset.edit); }));
    $$("[data-adopt]", t).forEach((b) => (b.onclick = async () => {
      const c = list[b.dataset.adopt];
      try { const made = await api("POST", `/api/servers/${s.id}/collections/${c.id}/adopt`); toast(`"${c.name}" is now managed here`);
        await loadCollections(); loadServerCollections(); editCollection(made.id); } catch (e) { toast(e.message, true); }
    }));
    $$("[data-del]", t).forEach((b) => (b.onclick = async () => {
      const c = list[b.dataset.del];
      if (!confirm(`Delete "${c.name}" on ${s.name}${c.library ? " (" + c.library + ")" : ""}?\n\nThe collection is removed from the server now. The movies and shows in it are not touched.`)) return;
      try { await api("DELETE", `/api/servers/${s.id}/collections/${c.id}`); toast("Deleted"); loadCollections(); loadServerCollections(); }
      catch (e) { toast(e.message, true); }
    }));
  }
}

async function loadCollections() {
  collections = await api("GET", "/api/collections");
  const names = Object.fromEntries(settings.servers.map((s) => [s.id, s.name || s.kind]));
  const t = $("#col-table");
  if (!collections.length) { t.innerHTML = '<tr><td class="hint">No collections yet.</td></tr>'; return; }
  t.innerHTML = "<tr><th>Name</th><th>Sources</th><th>Servers</th><th>On servers</th><th></th></tr>" + collections.sort((a, b) => a.name.localeCompare(b.name)).map((c) => `
    <tr><td><b>${esc(c.name)}</b>${c.enabled ? "" : ' <span class="pill">off</span>'}${c.only_add ? ' <span class="pill">add only</span>' : ""}</td>
    <td>${c.sources.map((s) => `<span class="pill">${esc((SRC_TYPES.find((x) => x[0] === s.type) || [, s.type])[1])}: ${esc(s.label || s.value)}</span>`).join("")}</td>
    <td>${(c.servers.length ? c.servers : Object.keys(names)).map((id) => `<span class="pill">${esc(names[id] || id)}</span>`).join("")}</td>
    <td>${c.mapped.length ? c.mapped.map((m) => `<span class="pill">${esc(names[m.server] || m.server)}${m.section ? " · lib " + esc(m.section) : ""}</span>`).join("") : '<span class="hint">not yet</span>'}</td>
    <td style="white-space:nowrap"><button class="small" onclick="runJob('collections',{only:'${c.id}'})">Sync</button>
      <button class="small" onclick="editCollection('${c.id}')">Edit</button></td></tr>`).join("");
}
let xtreamCache = null;
async function editCollection(id) {
  const c = id ? JSON.parse(JSON.stringify(collections.find((x) => x.id === id))) : {
    name: "", enabled: true, sort_name: "", summary: "", servers: [], sources: [{ type: "mdblist", value: "" }], exclude: [], only_add: false, sort: "Default",
    artwork: { mode: "Auto", url: "", kometa_path: "", tmdb_collection_id: 0, generated_style: "Title", color_top: "#1F2A44", color_bottom: "#0B0F19" } };
  if (!xtreamCache || !xtreamCache.categories.length) xtreamCache = await api("GET", "/api/xtream/categories").catch(() => ({ categories: [] }));
  const dlg = $("#dlg"), f = $("#dlg-body"); f.innerHTML = `<h2>${id ? "Edit" : "New"} collection</h2>`;
  f.append(fieldRow(c, ["name", "Name", "text"]), fieldRow(c, ["enabled", "Enabled", "check"]),
    fieldRow(c, ["sort_name", "Sort title", "text", "optional"]), fieldRow(c, ["summary", "Summary", "text", "optional"]),
    fieldRow(c, ["sort", "Order inside", "select", [["Default", "Server default"], ["ReleaseDate", "Release date"], ["Name", "Title"]]]),
    fieldRow(c, ["only_add", "Add only (never remove titles)", "check"]));
  const tg = document.createElement("div"); tg.className = "field";
  tg.innerHTML = `<label>Keep on</label><div class="ctl libs"></div><div class="help">None ticked: every server</div>`;
  settings.servers.forEach((s) => { const lab = document.createElement("label");
    const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked: c.servers.includes(s.id) });
    cb.onchange = () => { c.servers = cb.checked ? [...c.servers, s.id] : c.servers.filter((x) => x !== s.id); };
    lab.append(cb, s.name || s.kind); $(".ctl", tg).append(lab); });
  f.append(tg);
  f.append(sourceList("Titles from", c.sources), sourceList("Leave out titles from", c.exclude));
  f.append(artworkEditor(c));
  f.append(homeEditor(c));
  const btns = document.createElement("div"); btns.className = "row between";
  btns.innerHTML = `<span>${id ? '<button type="button" class="danger" data-a="del">Delete</button>' : ""}</span>
    <span class="row"><button value="cancel">Cancel</button><button type="button" class="primary" data-a="save">Save</button></span>`;
  f.append(btns);
  $('[data-a="save"]', f).onclick = async () => {
    c.sources = c.sources.filter((s) => String(s.value).trim()); c.exclude = c.exclude.filter((s) => String(s.value).trim());
    if (!c.name.trim()) return toast("Give the collection a name", true);
    try { await api(id ? "PUT" : "POST", id ? `/api/collections/${id}` : "/api/collections", c); dlg.close(); toast("Saved"); loadCollections(); }
    catch (e) { toast(e.message, true); }
  };
  if (id) $('[data-a="del"]', f).onclick = async () => {
    const also = confirm(`Delete "${c.name}"?\n\nOK: also delete it on the servers.\nCancel: keep it on the servers; just stop managing it here.`);
    await api("DELETE", `/api/collections/${id}?remove_from_servers=${also}`); dlg.close(); loadCollections();
  };
  dlg.showModal();
}
function sourceList(title, list) {
  const fs = document.createElement("fieldset"); fs.innerHTML = `<legend>${esc(title)}</legend>`;
  const rows = document.createElement("div"); fs.append(rows);
  const draw = () => { rows.innerHTML = ""; list.forEach((s, i) => {
    const r = document.createElement("div"); r.className = "src-row";
    const type = document.createElement("select"); SRC_TYPES.forEach(([v, t]) => type.add(new Option(t, v, false, s.type === v)));
    let val;
    if (s.type === "xtream" && !xtreamCache.categories.length) {
      val = document.createElement("div");
      val.innerHTML = `<input type="text" placeholder="movie:<id> or series:<id>" style="width:100%"><div class="notice" style="margin:6px 0 0">
        No xtreamfs categories found. The container looked for <code>categories.json</code> in ${(xtreamCache.looked_in || []).map(esc).join(", ")}.
        In the container settings, map the folder that holds it (xtreamfs's config folder, or the top of its .strm folder) to <code>/xtreamfs</code>, then reopen this editor.</div>`;
      const inp = $("input", val); inp.value = s.value; inp.oninput = () => { s.value = inp.value; s.label = ""; };
    } else if (s.type === "xtream") {
      val = document.createElement("select"); val.add(new Option("Choose a category…", ""));
      xtreamCache.categories.forEach((x) => val.add(new Option(`${x.kind === "movie" ? "Movies" : "Series"}: ${x.name} (${x.count})`, x.value, false, x.value === s.value)));
      val.onchange = () => { s.value = val.value; s.label = val.selectedOptions[0].text; };
    } else if (s.type === "server_collection") {
      val = document.createElement("select"); val.add(new Option("Loading…", s.value || "", true, true));
      Promise.all(settings.servers.map((sv) => api("GET", `/api/servers/${sv.id}/collections`).then((cs) => cs.map((x) => [sv, x])).catch(() => [])))
        .then((all) => { val.innerHTML = ""; val.add(new Option("Choose a collection…", ""));
          const seen = new Set();
          all.flat().forEach(([sv, x]) => { const v = `${sv.id}:${x.name}`; if (seen.has(v)) return; seen.add(v);
            val.add(new Option(`${sv.name}: ${x.name} (${x.count})`, v, false, v === s.value)); }); });
      val.onchange = () => { s.value = val.value; s.label = val.selectedOptions[0].text; };
    } else {
      val = Object.assign(document.createElement("input"), { type: "text", value: s.value, placeholder: SRC_HINT[s.type] });
      val.oninput = () => { s.value = val.value; s.label = ""; };
    }
    type.onchange = () => { s.type = type.value; s.value = ""; s.label = ""; draw(); };
    const del = Object.assign(document.createElement("button"), { type: "button", textContent: "✕", className: "small" });
    del.onclick = () => { list.splice(i, 1); draw(); };
    r.append(type, val, del); rows.append(r); }); };
  draw();
  const add = Object.assign(document.createElement("button"), { type: "button", textContent: "Add source", className: "small" });
  add.onclick = () => { list.push({ type: "mdblist", value: "", label: "" }); draw(); };
  fs.append(add); return fs;
}
const EMBY_SORTS = [["ProductionYear|Descending", "Newest first (release year)"], ["DateCreated|Descending", "Recently added first"],
  ["default|Ascending", "Collection order"], ["SortName|Ascending", "Name"], ["CommunityRating|Descending", "Highest rated first"]];
function homeEditor(c) {
  c.home = Object.assign({ enabled: false, name: "", emby_sort: "ProductionYear|Descending", emby_view: "", emby_users: [],
    plex_recommended: true, plex_home: true, plex_shared: false }, c.home || {});
  const h = c.home, fs = document.createElement("fieldset");
  fs.innerHTML = "<legend>Home screen</legend>";
  const body = document.createElement("div");
  const draw = () => {
    body.innerHTML = "";
    if (!h.enabled) return;
    const emby = settings.servers.filter((s) => s.kind === "emby" && (!c.servers.length || c.servers.includes(s.id)));
    const plex = settings.servers.filter((s) => s.kind === "plex" && (!c.servers.length || c.servers.includes(s.id)));
    if (emby.length) {
      body.insertAdjacentHTML("beforeend", '<h3>Emby</h3>');
      body.append(fieldRow(h, ["name", "Row name", "text", c.name || "the collection's name"]),
        fieldRow(h, ["emby_sort", "Sort", "select", EMBY_SORTS]),
        fieldRow(h, ["emby_view", "Style", "select", [["", "Cards"], ["spotlight", "Spotlight (large banners)"]]]));
      const us = document.createElement("div"); us.className = "field";
      us.innerHTML = '<label>Users</label><div class="ctl libs"><span class="hint">Loading…</span></div><div class="help">None ticked: every user. Emby rows are per user.</div>';
      body.append(us);
      Promise.all(emby.map((s) => api("GET", `/api/servers/${s.id}/users`).catch(() => []))).then((all) => {
        const ctl = $(".ctl", us); ctl.innerHTML = "";
        all.flat().forEach((u) => { const lab = document.createElement("label");
          const cb = Object.assign(document.createElement("input"), { type: "checkbox", checked: h.emby_users.includes(u.id) });
          cb.onchange = () => { h.emby_users = cb.checked ? [...h.emby_users, u.id] : h.emby_users.filter((x) => x !== u.id); };
          lab.append(cb, u.name); ctl.append(lab); });
      });
    }
    if (plex.length) {
      body.insertAdjacentHTML("beforeend", '<h3>Plex</h3>');
      body.append(fieldRow(h, ["plex_recommended", "Library Recommended", "check"]),
        fieldRow(h, ["plex_home", "Your Home page", "check"]), fieldRow(h, ["plex_shared", "Friends' Home pages", "check"]));
    }
    body.insertAdjacentHTML("beforeend", '<p class="hint">Applied on the next sync of this collection.</p>');
  };
  fs.append(fieldRow(h, ["enabled", "Show on home screens", "check", null, "Off: rows this page added are removed on the next sync"], draw), body);
  draw();
  return fs;
}

function artworkEditor(c) {
  const a = c.artwork, fs = document.createElement("fieldset"); fs.innerHTML = "<legend>Poster</legend>";
  const body = document.createElement("div"); fs.append(body);
  const draw = () => { body.innerHTML = "";
    body.append(fieldRow(a, ["mode", "Poster", "select", [["Auto", "Automatic (URL → Kometa → TMDB → generated)"], ["Url", "From a URL"], ["Kometa", "Kometa default image"],
      ["TmdbCollection", "TMDB collection poster"], ["Generated", "Generated here"], ["None", "Leave as it is"]]], () => draw()));
    if (["Auto", "Url"].includes(a.mode)) body.append(fieldRow(a, ["url", "Image URL", "url", "https://…"]));
    if (["Auto", "Kometa"].includes(a.mode)) {
      body.append(fieldRow(a, ["kometa_path", "Kometa image", "text", "blank: match the collection name"]));
      const row = document.createElement("div"); row.className = "row";
      const q = Object.assign(document.createElement("input"), { type: "text", placeholder: "Search Kometa images", value: c.name });
      const go = Object.assign(document.createElement("button"), { type: "button", textContent: "Search", className: "small" });
      const grid = document.createElement("div"); grid.className = "kometa-grid";
      go.onclick = async () => { const hits = await api("GET", `/api/kometa/search?q=${encodeURIComponent(q.value)}`);
        grid.innerHTML = hits.length ? "" : '<span class="hint">No match</span>';
        hits.forEach((h) => { const img = Object.assign(document.createElement("img"), { src: h.url, title: h.path, loading: "lazy" });
          if (h.path === a.kometa_path) img.classList.add("sel");
          img.onclick = () => { a.kometa_path = h.path; draw(); }; grid.append(img); }); };
      row.append(q, go); body.append(row, grid);
    }
    if (["Auto", "TmdbCollection"].includes(a.mode)) body.append(fieldRow(a, ["tmdb_collection_id", "TMDB collection id", "number"]));
    if (["Auto", "Generated"].includes(a.mode)) body.append(fieldRow(a, ["generated_style", "Generated style", "select", [["Title", "Title on colour"], ["Mosaic", "Mosaic of member posters"]]]),
      fieldRow(a, ["color_top", "Top colour", "color"]), fieldRow(a, ["color_bottom", "Bottom colour", "color"]));
  };
  draw(); return fs;
}

// --------------------------------------------------------------------------------- activity
async function loadActivity() {
  const rows = await api("GET", "/api/activity?limit=600");
  const lvl = $("#act-level").value;
  $("#activity").innerHTML = rows.reverse().filter((r) => !lvl || r.level !== "info").map((r) =>
    `<div class="${esc(r.level)}"><span class="t">${new Date(r.time * 1000).toLocaleString()}</span><span class="s">${esc(r.scope)}</span>${esc(r.message)}</div>`).join("") || '<div class="hint">Nothing yet.</div>';
}
$("#act-level").onchange = loadActivity;

// ------------------------------------------------------------------------------------ start
(async function init() {
  await loadSettings();
  await refreshStatus();
  setInterval(() => { if ($("#tab-status").classList.contains("active") || Object.values(status?.jobs || {}).some((j) => j.running)) refreshStatus(); }, 3000);
  const tab = location.hash.slice(1);
  showTab(["status", "posters", "collections", "servers", "settings", "activity"].includes(tab) ? tab : "status");
})();
