/* ResinPilot — web client */

const THEME_KEY = "resinpilot_theme";

const state = {
  resins: [],
  printers: [],
  models: [],
  fleet: null,
  today: null,
  reportPrinterId: null,
  charts: {},
  curveKey: "",
  curveOn: null,
  curveTemps: null,
  meta: null,
  settings: null,
  lastRecommend: null,
  newPrinters: [],
  removedPrinters: [],
};

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function chartColors() {
  return {
    ink: cssVar("--ink") || "#1c1c1e",
    muted: cssVar("--muted") || "#8e8e93",
    blue: cssVar("--blue") || "#007aff",
    green: cssVar("--green") || "#34c759",
    red: cssVar("--red") || "#ff3b30",
    orange: cssVar("--orange") || "#ff9500",
    fill: cssVar("--fill") || "#e5e5ea",
    card: cssVar("--card") || "#ffffff",
  };
}

function resolvedTheme(mode) {
  if (mode === "dark" || mode === "light") return mode;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme(mode) {
  const m = mode || localStorage.getItem(THEME_KEY) || "system";
  localStorage.setItem(THEME_KEY, m);
  const resolved = resolvedTheme(m);
  if (m === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", resolved);
  }
  const sel = $("themeSelect");
  if (sel) sel.value = m;
  // Refresh charts if visible so Chart.js picks new colors
  if ($("panel-charts")?.classList.contains("active")) {
    loadCharts().catch(() => {});
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch (_) {}
    throw new Error(detail);
  }
  return res.json();
}

function $(id) {
  return document.getElementById(id);
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** Fix legacy double /assets/assets/ paths from older server builds. */
function photoSrc(url) {
  if (!url) return "";
  return String(url).replace(/\/assets\/assets\//g, "/assets/");
}

function setImg(el, url, alt = "") {
  if (!el) return;
  const src = photoSrc(url);
  if (!src) {
    el.hidden = true;
    el.removeAttribute("src");
    return;
  }
  el.alt = alt;
  el.src = src;
  el.hidden = false;
  el.onerror = () => {
    el.hidden = true;
  };
}

let segPillsReady = false;

function placeSegPill(seg, animate = true) {
  const pill = seg?.querySelector(".seg-pill");
  const btn = seg?.querySelector("button.active:not([hidden])");
  if (!pill) return;
  pill.classList.toggle("no-anim", !animate);
  pill.style.opacity = btn ? "1" : "0";
  if (btn) {
    pill.style.width = `${btn.offsetWidth}px`;
    pill.style.transform = `translateX(${btn.offsetLeft}px)`;
    pill.style.top = `${btn.offsetTop}px`;
  }
}

function animatePanel(panel, cls) {
  if (!panel) return;
  panel.classList.remove("enter-right", "enter-left", "enter-fade");
  void panel.offsetWidth; // restart the animation
  panel.classList.add(cls);
}

function setPanel(name) {
  const buttons = [...document.querySelectorAll(".nav button:not([hidden])")];
  const from = buttons.findIndex((b) => b.classList.contains("active"));
  const to = buttons.findIndex((b) => b.dataset.panel === name);
  document.querySelectorAll(".nav button").forEach((b) => {
    b.classList.toggle("active", b.dataset.panel === name);
  });
  document.querySelectorAll(".panel").forEach((p) => {
    p.classList.toggle("active", p.id === `panel-${name}`);
  });
  placeSegPill($("nav"));
  history.replaceState(null, "", name === "today" ? location.pathname : `#${name}`);
  if (from !== to && from >= 0 && to >= 0) animatePanel($(`panel-${name}`), to > from ? "enter-right" : "enter-left");
  if (name === "charts") loadCharts();
  if (name === "history") loadHistory();
  if (name === "calibrate") syncCalibrateForm();
  if (name === "printers") loadPrintersPanel();
}

function fillSelect(sel, items, valueKey, labelKey, preferred) {
  sel.innerHTML = items
    .map((it) => `<option value="${esc(it[valueKey])}">${esc(it[labelKey])}</option>`)
    .join("");
  if (preferred && [...sel.options].some((o) => o.value === preferred)) {
    sel.value = preferred;
  }
}

function renderKpis(summary) {
  const film = summary.film_due ? ` · ${summary.film_due} film due` : "";
  $("headerSub").textContent =
    `${summary.printing} printing · ${summary.idle} idle · ${summary.attention} attention${film}`;
  renderStatusBar();
}

function durationText(s) {
  if (s == null) return "";
  const m = Math.round(s / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} m left` : `${m} min left`;
}

const ALERT_RANK = { critical: 0, warn: 1, info: 2 };
const ALERT_SOON_S = 15 * 60;

function sharedTodayWarnings() {
  const cards = state.today?.cards || [];
  const lists = cards.map((c) => new Set((c.profiles || []).flatMap((pr) => pr.warnings || [])));
  if (lists.length < 2) return [];
  return [...lists[0]].filter((w) => lists.every((s) => s.has(w)));
}

function collectAlerts() {
  const fleet = state.fleet;
  if (!fleet) return [];
  const alerts = [];
  const jobs = fleet.open_jobs || [];
  for (const p of fleet.printers || []) {
    const name = p.short_name;
    const kind = p.status_kind || "offline";
    if (kind === "failed") {
      alerts.push({ level: "critical", text: `${name}: ${p.status_label || "Error"}`, detail: p.error_message || "" });
    } else if (kind === "offline") {
      alerts.push({ level: "warn", text: `${name} is offline`, detail: p.error_message || p.ip || "" });
    }
    if (jobs.some((j) => j.printer_id === p.printer_id && j.awaiting_outcome)) {
      alerts.push({ level: "info", text: `${name} finished — report the outcome`, action: { label: "Report", report: p.printer_id } });
    }
    if (kind === "printing" && p.time_left_s != null && p.time_left_s <= ALERT_SOON_S) {
      alerts.push({ level: "info", text: `${name} finishes in ${durationText(p.time_left_s).replace(/ left$/, "")}` });
    }
    if (p.film?.level === "critical") alerts.push({ level: "critical", text: `${name}: replace the release film`, detail: p.film.warning });
    else if (p.film?.level === "warn") alerts.push({ level: "warn", text: `${name}: release film near its limit`, detail: p.film.warning });
  }
  const parked = fleet.summary?.unreported || 0;
  if (parked) {
    alerts.push({
      level: "info",
      text: `${parked} print${parked === 1 ? "" : "s"} in History waiting for a report`,
      action: { label: "Open History", panel: "history" },
    });
  }
  for (const w of sharedTodayWarnings()) alerts.push({ level: "warn", text: w });
  const sent = state.detected?.sent;
  if (sent && Date.now() - new Date(sent.sent_at).getTime() < DETECT_AUTO_APPLY_MS) {
    const off = (sent.checks || []).filter((c) => !c.ok);
    if (off.length && off.length === (sent.checks || []).length) {
      alerts.push({
        level: "warn",
        text: `Sent file ${sent.file} differs from the recommended settings`,
        detail: off.map((c) => `${c.short_name}: ${c.detail || ""}`).join("\n"),
      });
    }
  }
  return alerts.sort((a, b) => ALERT_RANK[a.level] - ALERT_RANK[b.level]);
}

const ALERTS_COLLAPSED = 4;
let alertsExpanded = false;

function renderAlerts() {
  const bar = $("alertBar");
  if (!bar || !state.fleet) return;
  const alerts = collectAlerts();
  if (!alerts.length) {
    bar.className = "alert-bar clear";
    bar.innerHTML = `<span class="al-dot"></span><span class="al-text">All clear — nothing in the space needs attention</span>`;
    return;
  }
  const top = alerts[0].level;
  const expanded = alertsExpanded;
  const shown = expanded ? alerts : alerts.slice(0, ALERTS_COLLAPSED);
  const rest = alerts.length - shown.length;
  bar.className = `alert-bar ${top}${expanded ? " expanded" : ""}`;
  bar.innerHTML =
    `<span class="al-count">${alerts.length} need${alerts.length === 1 ? "s" : ""} attention</span>` +
    shown
      .map((a, i) => {
        const btn = a.action ? `<button class="al-action" data-alert="${i}">${esc(a.action.label)}</button>` : "";
        return `<span class="al-item ${a.level}" title="${esc(a.detail || a.text)}"><span class="al-dot"></span><span class="al-text">${esc(a.text)}</span>${btn}</span>`;
      })
      .join("") +
    `<button type="button" class="al-more" aria-expanded="${expanded}">${expanded ? "Show less" : rest ? `+${rest} more` : "Show all"}</button>`;
  bar.querySelectorAll("[data-alert]").forEach((b) =>
    b.addEventListener("click", () => {
      const act = shown[Number(b.dataset.alert)].action;
      if (act.report) openReport(act.report);
      if (act.panel) setPanel(act.panel);
    })
  );
  const toggle = bar.querySelector(".al-more");
  toggle.addEventListener("click", () => {
    alertsExpanded = !alertsExpanded;
    renderAlerts();
  });
  if (!expanded && !rest) {
    const clipped = [...bar.querySelectorAll(".al-text")].some((t) => t.scrollWidth > t.clientWidth);
    if (!clipped) toggle.remove();
  }
}

function renderStatusBar() {
  const bar = $("statusBar");
  const printers = state.fleet?.printers;
  if (!bar || !printers) return;
  // Don't rebuild under someone choosing a resin.
  if (bar.contains(document.activeElement) && document.activeElement.tagName === "SELECT") return;
  const jobs = state.fleet.open_jobs || [];
  bar.innerHTML = printers
    .map((p) => {
      const kind = p.status_kind || "offline";
      const printing = kind === "printing";
      const pct = p.progress_pct != null ? Math.round(p.progress_pct) : null;
      const awaiting = jobs.find((j) => j.printer_id === p.printer_id && j.awaiting_outcome);
      const report = awaiting
        ? `<button class="btn orange" data-report="${esc(p.printer_id)}">Report outcome</button>`
        : printing
          ? `<button class="btn secondary" data-report="${esc(p.printer_id)}">Report early</button>`
          : "";
      const detail = p.print_substatus || p.filename || (kind === "offline" ? p.error_message || "" : "");
      const film = p.film?.cycles != null
        ? `<span class="film-text ${esc(p.film.level || "ok")}" title="${esc(p.film.warning || "")}">Film ${Number(p.film.cycles).toLocaleString()} / ${Number(p.film.max || p.release_film_max).toLocaleString()}</span>`
        : "";
      const opts = state.resins
        .map((r) => `<option value="${esc(r.id)}" ${r.id === p.vat_resin_id ? "selected" : ""}>${esc(r.name)}</option>`)
        .join("");
      const since = p.vat_resin_set_at ? `since ${new Date(p.vat_resin_set_at).toLocaleDateString()}` : "not set yet";
      return `<div class="st-tile ${esc(kind)}">
        <div class="st-top">
          <div class="st-photo">
            <img src="${esc(photoSrc(p.photo))}" alt="" onerror="this.style.visibility='hidden'" />
            ${p.preview_url ? `<img class="st-plate" src="${esc(photoSrc(p.preview_url))}" alt="" title="Current / last plate" onerror="this.remove()" />` : ""}
          </div>
          <div class="st-main">
            <div class="st-name">${esc(p.short_name)}${p.is_room_ref ? ' <span class="st-tag" title="Its vat sensor gives the room temperature">room ref</span>' : ""}</div>
            <div class="st-status">${esc(p.status_label || "…")}${printing && pct != null ? ` <span class="st-pct">${pct}%</span>` : ""}</div>
          </div>
          ${report}
        </div>
        ${printing ? `<div class="st-progress"><span style="width:${pct || 0}%"></span></div>` : ""}
        <div class="st-detail" title="${esc(detail)}">${esc([detail, printing ? durationText(p.time_left_s) : ""].filter(Boolean).join(" · ") || "\u00a0")}</div>
        <div class="st-meta">${esc(p.temp_display || "—")}${film ? " · " : ""}${film}</div>
        <label class="st-resin">
          <span class="st-label">Resin in tank <span class="swatch" style="background:${esc(p.vat_resin_swatch || "transparent")}"></span></span>
          <select data-vat-resin="${esc(p.printer_id)}" title="Set the resin in this printer's tank (${esc(since)})">${opts}</select>
        </label>
      </div>`;
    })
    .join("");
  bar.querySelectorAll("[data-report]").forEach((btn) => btn.addEventListener("click", () => openReport(btn.dataset.report)));
  renderAlerts();
  bar.querySelectorAll("[data-vat-resin]").forEach((sel) =>
    sel.addEventListener("change", () => setVatResin(sel.dataset.vatResin, sel.value))
  );
}

function renderToday() {
  const root = $("todayCards");
  const data = state.today;
  if (!data) {
    root.innerHTML = `<div class="card">Loading…</div>`;
    return;
  }
  if (!data.cards.length) {
    $("roomNote").textContent = "";
    renderWelcome(root);
    renderAlerts();
    return;
  }
  const measured = (state.fleet?.room_vat_c ?? null) != null;
  const ref = state.printers.find((p) => p.id === data.room_ref);
  $("roomNote").textContent = measured ? `Room ${data.vat_c.toFixed(0)}°C` : `Room ${data.vat_c.toFixed(0)}°C (assumed)`;
  $("roomNote").title = measured
    ? `Measured by ${ref ? ref.name : "a printer"}'s vat sensor; profiles follow it as the fleet refreshes`
    : "No printer is reporting a vat temperature — using the room temperature from Settings";

  const shared = new Set(sharedTodayWarnings());
  root.innerHTML = data.cards
    .map((card) => {
      const profiles = (card.profiles || [])
        .map((pr) => {
          if (pr.error) {
            return `<div class="profile"><div class="lh">${pr.layer_height.toFixed(3)} mm</div>
              <div class="profile-note warn" title="${esc(pr.error)}">${esc(pr.error)}</div></div>`;
          }
          const why = [
            ...(pr.warnings || []),
            pr.exposure_detail,
            pr.part_detail,
            pr.bottom_detail,
            pr.source_detail,
            `Confidence: ${pr.confidence}`,
          ]
            .filter(Boolean)
            .join("\n\n");
          return `<div class="profile">
            <div class="lh">${pr.layer_height.toFixed(3)} mm<span class="why" title="${esc(why)}">why?</span></div>
            <div class="exp-row">
              <div class="exp-n"><span class="exp-label">Layer</span>${pr.normal_exposure_time.toFixed(2)}s</div>
              <div class="exp-b"><span class="exp-label">Bottom</span>${pr.bottom_exposure_time.toFixed(1)}s</div>
            </div>
            <div class="more">${pr.bottom_layer_count} bottom · ${pr.transition_layer_count} transition · rest ${pr.rest_time_after_retract.toFixed(1)}s</div>
            ${profileCue(pr, shared)}
          </div>`;
        })
        .join("");

      const swatch = (state.resins.find((r) => r.id === card.resin_id) || {}).swatch || "";
      const basis = resinBasisText(card);
      const runs = card.resin_runs || 0;
      return `<div class="card today-card">
        <div class="tc-head">
          <div class="name">${esc(card.short_name)}</div>
          <div class="tc-resin" title="${esc(basis)}">
            ${swatch ? `<span class="swatch" style="background:${esc(swatch)}"></span>` : ""}<strong>${esc(card.resin_name)}</strong>
            <span class="tc-basis ${runs >= 5 ? "learned" : ""}">${runs >= 5 ? `Learned · ${runs} runs` : "Starting point"}</span>
          </div>
        </div>
        <div class="tc-profiles">${profiles}</div>
      </div>`;
    })
    .join("");
  renderAlerts();
}

function profileCue(pr, shared = new Set()) {
  const warn = (pr.warnings || []).find((w) => !shared.has(w));
  if (warn) return `<div class="profile-cue warn" title="${esc(warn)}">${esc(warn)}</div>`;
  const pct = /([+-]\d+%)/.exec(pr.exposure_detail || "")?.[1];
  if (pct) return `<div class="profile-cue signal" title="${esc(pr.exposure_detail)}">${pct} from print reports</div>`;
  if (pr.part_detail) return `<div class="profile-cue" title="${esc(pr.part_detail)}">Tuned for this part</div>`;
  return "";
}

function resinBasisText(card) {
  const n = card.resin_runs || 0;
  if (n >= 5) return `Calibrated from ${n} successful runs with this resin on this printer`;
  const few = n ? `Only ${n} run${n === 1 ? "" : "s"}` : "No runs yet";
  return `${few} with this resin here — using its official settings adjusted by this printer's history with other resins`;
}

async function setVatResin(printerId, resinId) {
  try {
    await api(`/api/vat-resin/${encodeURIComponent(printerId)}`, {
      method: "POST",
      body: JSON.stringify({ resin_id: resinId }),
    });
    await loadFleet(false);
    await loadToday();
    if ($("calPrinter")?.value === printerId) {
      $("calResin").value = resinId;
      updateCalHero();
    }
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function importPrinterHistory() {
  const btn = $("btnImportPrinters");
  btn.disabled = true;
  btn.textContent = "Reading printers…";
  try {
    const preview = await api("/api/history/import-printers", {
      method: "POST",
      body: JSON.stringify({ dry_run: true }),
    });
    if (!preview.imported) {
      alert("Nothing new — every completed print on the printers is already in History.");
      return;
    }
    const lines = preview.printers.map(
      (p) =>
        `${p.printer}: ${p.imported} new${p.oldest ? ` (since ${p.oldest.slice(0, 10)})` : ""} · ` +
        `resin: each printer's "Resin in vat"` +
        (p.incomplete ? ` · ${p.incomplete} stopped/errored skipped` : "")
    );
    const ok = confirm(
      `Import ${preview.imported} completed prints as SUCCESS?\n\n${lines.join("\n")}\n\n` +
        "Prints already in History are left alone. You can fix the resin of any run in History afterwards."
    );
    if (!ok) return;
    btn.textContent = "Importing…";
    const res = await api("/api/history/import-printers", {
      method: "POST",
      body: JSON.stringify({ dry_run: false }),
    });
    alert(`Imported ${res.imported} prints into History.`);
    await loadHistory();
  } catch (err) {
    alert(err.message || String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = "Import printer history";
  }
}

function vatResinFor(printerId) {
  const row = (state.fleet?.printers || []).find((p) => p.printer_id === printerId);
  return row?.vat_resin_id || "";
}

// ---- Printers panel: find, add, rename, remove; settings -------------------------------

const printersUi = { found: [], searching: false, searched: false };

async function loadPrintersPanel() {
  try {
    const [printers, settings, removed] = await Promise.all([
      api("/api/printers"),
      api("/api/settings"),
      api("/api/fleet/removed"),
    ]);
    state.printers = printers;
    state.settings = settings;
    state.removedPrinters = removed.printers || [];
  } catch (err) {
    console.error(err);
  }
  renderFleetList();
  renderRemoved();
  renderFound();
  renderSettings();
  if (!state.printers.length && !printersUi.searching && !printersUi.found.length) searchPrinters();
}

function renderFleetList() {
  const root = $("fleetList");
  if (!root) return;
  if (!state.printers.length) {
    root.innerHTML = `<div class="empty-note">No printers yet — search your network below, or add one by its IP address.</div>`;
    return;
  }
  root.innerHTML = state.printers
    .map(
      (p) => `<div class="fleet-row" data-id="${esc(p.id)}">
        <div class="st-photo">${p.photo ? `<img src="${esc(photoSrc(p.photo))}" alt="" onerror="this.style.display='none'" />` : ""}</div>
        <div class="fr-main">
          <div class="name">${esc(p.name)}${p.is_room_ref ? ` <span class="tag">room ref</span>` : ""}</div>
          <div class="caption">${esc([p.brand, p.model_name].filter(Boolean).join(" "))} · ${esc(p.sdcp_ip || "")} · ${esc(p.xy_label || "")}</div>
        </div>
        <div class="fr-actions">
          <button class="btn secondary sm" data-rename="${esc(p.id)}">Rename</button>
          <button class="btn secondary sm" data-host="${esc(p.id)}">Change IP</button>
          <button class="btn red sm" data-remove="${esc(p.id)}">Remove</button>
        </div>
      </div>`
    )
    .join("");
  root.querySelectorAll("[data-rename]").forEach((b) =>
    b.addEventListener("click", () => editPrinter(b.dataset.rename, "name"))
  );
  root.querySelectorAll("[data-host]").forEach((b) =>
    b.addEventListener("click", () => editPrinter(b.dataset.host, "host"))
  );
  root.querySelectorAll("[data-remove]").forEach((b) =>
    b.addEventListener("click", () => removePrinter(b.dataset.remove))
  );
}

async function editPrinter(id, field) {
  const p = state.printers.find((x) => x.id === id);
  if (!p) return;
  const current = field === "name" ? p.name : p.sdcp_ip;
  const next = prompt(field === "name" ? "Printer name" : "Printer IP address", current || "");
  if (next === null || !next.trim() || next.trim() === current) return;
  try {
    await api(`/api/fleet/printers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify({ [field]: next.trim() }),
    });
    await reloadAfterFleetChange();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function removePrinter(id) {
  const p = state.printers.find((x) => x.id === id);
  if (!p || !confirm(`Remove ${p.name}? Its print history is kept and comes back if you add it again.`)) return;
  try {
    await api(`/api/fleet/printers/${encodeURIComponent(id)}`, { method: "DELETE" });
    await reloadAfterFleetChange();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function searchPrinters(target = "") {
  const btn = $("btnSearchPrinters");
  printersUi.searching = true;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Searching…";
  }
  $("foundList").innerHTML = `<div class="caption">${target ? `Asking ${esc(target)}…` : "Looking for printers on your network…"}</div>`;
  try {
    const res = await api(`/api/discover${target ? `?target=${encodeURIComponent(target)}` : ""}`);
    printersUi.found = res.printers || [];
    printersUi.errors = res.errors || [];
  } catch (err) {
    printersUi.found = [];
    printersUi.errors = [err.message || String(err)];
  } finally {
    printersUi.searching = false;
    printersUi.searched = true;
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Search network";
    }
  }
  renderFound(target);
}

function historyNote(count) {
  const n = Number(count) || 0;
  if (!n) return "";
  return ` · ${n} print${n === 1 ? "" : "s"} in History come${n === 1 ? "s" : ""} back with it`;
}

function renderRemoved() {
  const root = $("removedList");
  if (!root) return;
  const seen = new Set((state.newPrinters || []).map((p) => p.key));
  const rows = (state.removedPrinters || []).filter((p) => p.host && !seen.has(p.key));
  if (!rows.length) {
    root.innerHTML = "";
    return;
  }
  root.innerHTML =
    `<div class="caption" style="margin-top:14px">Removed from the fleet — add again from here to restore the same history</div>` +
    rows
      .map(
        (p) => `<div class="fleet-row">
          <div class="fr-main">
            <div class="name">${esc(p.name)}</div>
            <div class="caption">${esc([p.brand, p.model_name].filter(Boolean).join(" "))} · ${esc(p.host)}${historyNote(p.history_count)}</div>
          </div>
          <div class="fr-actions"><button class="btn sm" data-restore="${esc(p.key)}">Add</button></div>
        </div>`
      )
      .join("");
  root.querySelectorAll("[data-restore]").forEach((b) =>
    b.addEventListener("click", () => restorePrinter(b.dataset.restore))
  );
}

async function restorePrinter(key) {
  const p = (state.removedPrinters || []).find((row) => row.key === key);
  if (!p) return;
  try {
    await api("/api/fleet/printers", { method: "POST", body: JSON.stringify({ printers: [p] }) });
    await reloadAfterFleetChange();
    await pollNewPrinters();
  } catch (err) {
    alert(err.message || String(err));
  }
}

function renderFound(target = "") {
  const root = $("foundList");
  if (!root || printersUi.searching) return;
  const rows = printersUi.searched ? printersUi.found || [] : state.newPrinters || [];
  const errors = (printersUi.errors || []).map((e) => `<div class="caption warn">${esc(e)}</div>`).join("");
  if (!printersUi.searched && !rows.length) {
    root.innerHTML = `<div class="empty-note">Press Search network to look for printers.</div>`;
    return;
  }
  const preface = !printersUi.searched
    ? `<div class="caption">Found on your network and not in the fleet yet.</div>`
    : "";
  if (!rows.length) {
    root.innerHTML =
      errors +
      `<div class="empty-note">${
        target ? `No printer answered at ${esc(target)}.` : "No printers found yet."
      } Make sure the printer is on, connected to the same network as this computer, and that its network
      features are enabled. Some networks block discovery — then type the printer's IP address (shown in its
      network settings) and press Add by IP.</div>`;
    return;
  }
  root.innerHTML =
    preface +
    errors +
    rows
      .map(
        (f, i) => `<label class="found-row ${f.added || f.unsupported ? "added" : ""}" title="${esc(f.unsupported || "")}">
          <input type="checkbox" data-found="${i}" ${f.added || f.unsupported ? "disabled" : "checked"} />
          <div class="st-photo">${f.photo ? `<img src="${esc(photoSrc(f.photo))}" alt="" />` : ""}</div>
          <div class="fr-main">
            <input class="found-name" data-found-name="${i}" value="${esc(f.name || f.model_name)}" ${f.added || f.unsupported ? "disabled" : ""} />
            <div class="caption">${esc(f.model_label)} · ${esc(f.host)}${historyNote(f.history_count)}${f.firmware ? ` · firmware ${esc(f.firmware)}` : ""}${
              f.unsupported
                ? ` · ${esc(f.unsupported)}`
                : f.known_model
                  ? ""
                  : " · not in the model library yet — added with generic starting exposures"
            }</div>
          </div>
          <span class="tag">${f.added ? "Added" : f.unsupported ? "Not yet" : "New"}</span>
        </label>`
      )
      .join("") +
    (rows.some((f) => !f.added && !f.unsupported) ? `<button class="btn" id="btnAddFound">Add selected printers</button>` : "");
  $("btnAddFound")?.addEventListener("click", addFoundPrinters);
}

async function addFoundPrinters() {
  const picks = [];
  document.querySelectorAll("[data-found]").forEach((cb) => {
    if (!cb.checked || cb.disabled) return;
    const i = Number(cb.dataset.found);
    const source = printersUi.searched ? printersUi.found : state.newPrinters;
    const f = source[i];
    const name = document.querySelector(`[data-found-name="${i}"]`)?.value.trim();
    picks.push({ ...f, name: name || f.name });
  });
  if (!picks.length) return;
  try {
    await api("/api/fleet/printers", { method: "POST", body: JSON.stringify({ printers: picks }) });
    const addedHosts = new Set(picks.map((p) => p.host));
    printersUi.found = (printersUi.found || []).map((f) => (addedHosts.has(f.host) ? { ...f, added: true } : f));
    state.newPrinters = (state.newPrinters || []).filter((f) => !addedHosts.has(f.host));
    await reloadAfterFleetChange();
    await pollNewPrinters();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function reloadAfterFleetChange() {
  state.printers = await api("/api/printers");
  fillPrinterSelects();
  syncCalibrateForm();
  await loadPrintersPanel();
  await loadFleet(true);
  await loadToday();
}

function renderSettings() {
  const s = state.settings;
  const root = $("settingsForm");
  if (!s || !root) return;
  const refOptions = [`<option value="">Automatic (first printer reporting a vat temperature)</option>`]
    .concat(state.printers.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`))
    .join("");
  root.innerHTML = `
    <div>
      <label for="setRoomRef">Room temperature from</label>
      <select id="setRoomRef">${refOptions}</select>
      <div class="caption">Resin printers with a vat heater/sensor report its temperature; one stands in for the room.</div>
    </div>
    <div>
      <label for="setFallback">Room °C when no printer reports one</label>
      <input id="setFallback" type="number" step="0.5" value="${esc(s.fallback_room_c)}" />
    </div>
    <div>
      <label for="setLayers">Layer heights on Today (mm, comma separated)</label>
      <input id="setLayers" value="${esc((s.today_layer_heights || []).join(", "))}" />
    </div>
    <div>
      <label for="setMinBottom">Minimum bottom exposure (s, 0 = none)</label>
      <input id="setMinBottom" type="number" step="1" min="0" value="${esc(s.min_bottom_exposure_s)}" />
    </div>
    <div>
      <label class="check"><input id="setWatch" type="checkbox" ${s.watch_chitubox ? "checked" : ""} /> Watch Chitubox on this computer</label>
      <div class="caption">Checks files you send from Chitubox against the recommended exposures.</div>
    </div>
    <div>
      <label>Data folder</label>
      <div class="caption mono">${esc(s.data_folder)}</div>
    </div>
    <div><button class="btn" id="btnSaveSettings">Save settings</button> <span class="caption" id="settingsSaved"></span></div>`;
  $("setRoomRef").value = s.room_ref_printer_id || "";
  $("btnSaveSettings").addEventListener("click", saveSettings);
}

async function saveSettings() {
  const layers = $("setLayers")
    .value.split(/[,\s]+/)
    .map(Number)
    .filter((v) => v > 0);
  try {
    state.settings = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({
        room_ref_printer_id: $("setRoomRef").value,
        fallback_room_c: Number($("setFallback").value),
        today_layer_heights: layers,
        min_bottom_exposure_s: Number($("setMinBottom").value),
        watch_chitubox: $("setWatch").checked,
      }),
    });
    $("settingsSaved").textContent = "Saved";
    setTimeout(() => ($("settingsSaved").textContent = ""), 2000);
    await loadFleet(false);
    await loadToday();
    state.printers = await api("/api/printers");
    renderFleetList();
  } catch (err) {
    alert(err.message || String(err));
  }
}

function renderWelcome(root) {
  root.innerHTML = `<div class="card welcome">
    <h2>Welcome to ResinPilot</h2>
    <p>Add your resin printers to get exposure recommendations, live status and a print log that learns from every print.</p>
    <button class="btn" id="btnWelcomeFind">Find my printers</button>
  </div>`;
  $("btnWelcomeFind").addEventListener("click", () => setPanel("printers"));
}

function fillPrinterSelects() {
  fillSelect($("chartPrinter"), state.printers, "id", "short_name", state.printers[0]?.id);
}

const SIZE_LABELS = { small: "Small", medium: "Medium", bulky: "Bulky" };

function sizeBadge(sizeClass, title = "") {
  if (!sizeClass) return "";
  return ` <span class="size-badge ${esc(sizeClass)}" title="${esc(title)}">${esc(SIZE_LABELS[sizeClass] || sizeClass)}</span>`;
}

const SHAPE_LABELS = { channels: "Channels", fine: "Fine" };

function shapeBadges(tags, title = "") {
  return (tags || [])
    .map((t) => ` <span class="shape-badge ${esc(t)}" title="${esc(title)}">${esc(SHAPE_LABELS[t] || t)}</span>`)
    .join("");
}

function tableFromRows(table, rows, { actions = false, printer = false } = {}) {
  const head = table.querySelector("thead");
  const body = table.querySelector("tbody");
  if (!rows.length) {
    head.innerHTML = "";
    body.innerHTML = `<tr><td>No runs yet.</td></tr>`;
    return;
  }
  head.innerHTML = `<tr>
    <th>ID</th><th>When</th>${printer ? "<th>Printer</th>" : ""}<th>Status</th><th>Part</th><th>Resin</th><th>Layer height</th>
    <th>Layer exposure</th><th>Bottom exposure</th><th>Temp</th><th>UV</th><th>Layers</th><th>Time</th>
    <th>File</th>${actions ? `<th class="row-actions"></th>` : ""}
  </tr>`;
  body.innerHTML = rows
    .map((r, i) => {
      let pill;
      if (r.outcome === "SUCCESS") pill = `<span class="pill ok">OK</span>`;
      else if (r.outcome === "UNREPORTED") pill = `<span class="pill pending">Unreported</span>`;
      else if (r.outcome === "PRINTING") pill = `<span class="pill printing">Printing</span>`;
      else pill = `<span class="pill fail">${esc(r.outcome)}</span>`;
      const thumb = r.preview
        ? `<img class="hist-thumb" src="/previews/${esc(r.preview.split(/[/\\\\]/).pop())}" alt="" />`
        : "";
      const detail = [r.issue, r.quality, r.note, r.exp_note].filter(Boolean).join(" · ");
      let action = "";
      if (actions) {
        const printing = r.outcome === "PRINTING";
        const label = r.report_kind === "run" ? "Edit report" : "Report";
        const tone = r.report_kind === "run" ? "secondary" : printing ? "" : "orange";
        action = `<td class="row-actions"><button type="button" class="btn ${tone} sm" data-report-row="${i}">${label}</button>`;
        if (r.report_kind === "pending") {
          action += ` <button type="button" class="btn secondary sm" data-dismiss-row="${i}" title="Not a real print — remove">✕</button>`;
        }
        action += "</td>";
      }
      return `<tr class="${r.outcome === "UNREPORTED" ? "row-pending" : ""}">
        <td>${esc(r.id)}</td>
        <td>${esc(r.when)}</td>
        ${printer ? `<td class="printer-cell">${esc(r.printer || r.printer_id || "—")}</td>` : ""}
        <td>${pill}</td>
        <td class="part-cell">${r.part_group ? `<div class="part-group">${esc(r.part_group)}</div>` : ""}${esc(r.part_family || "—")}${sizeBadge(r.size_class, r.size)}${shapeBadges(r.shape_tags, r.shape)}</td>        <td>${esc(r.resin)}</td>
        <td>${esc(r.layer)}</td>
        <td>${esc(r.layer_exp)}</td>
        <td>${esc(r.bottom_exp)}</td>
        <td>${esc(r.temp)}</td>
        <td>${esc(r.uv)}</td>
        <td>${esc(r.layers)}</td>
        <td>${esc(r.duration)}</td>
        <td class="file-cell">${thumb}${esc(r.file)}${detail ? `<div style="color:var(--muted)">${esc(detail)}</div>` : ""}</td>
        ${action}
      </tr>`;
    })
    .join("");
  if (actions) {
    body.querySelectorAll("[data-report-row]").forEach((b) =>
      b.addEventListener("click", () => openHistoryReport(rows[Number(b.dataset.reportRow)]))
    );
    body.querySelectorAll("[data-dismiss-row]").forEach((b) =>
      b.addEventListener("click", () => dismissPending(rows[Number(b.dataset.dismissRow)]))
    );
  }
}

async function loadCharts() {
  const pid = $("chartPrinter").value;
  if (!pid) return;
  const resinSel = $("chartResinFilter");
  const data = await api(`/api/charts/${pid}?resin_id=${encodeURIComponent(resinSel.value)}`);
  resinSel.innerHTML =
    `<option value="">All resins</option>` +
    data.resins_used
      .map((r) => `<option value="${esc(r.id)}" ${r.id === data.resin_id ? "selected" : ""}>${esc(r.name)} (${r.n})</option>`)
      .join("");
  const k = data.kpis;
  $("ckRuns").textContent = k.total;
  $("ckRate").textContent = `${k.success_rate.toFixed(0)}%`;
  $("ckPerWeek").textContent = k.prints_per_week.toFixed(1);
  $("ckHours").textContent = `${k.hours_per_week.toFixed(0)}h`;
  $("ckTime").textContent = k.avg_duration_min != null ? `${k.avg_duration_min.toFixed(0)}m` : "—";
  $("ckTempKpi").hidden = !k.measured_temp_runs;
  $("ckTemp").textContent = k.avg_vat_c != null ? `${k.avg_vat_c.toFixed(0)}°` : "—";
  $("insights").textContent = data.insights.map((l) => `• ${l}`).join("\n\n");

  const colors = chartColors();
  renderTimelineChart(data.timeline, colors);
  renderCureChart(data.curves, colors, pid, resinSel.value);
  renderRecipeChart(data, colors);
  renderBottomChart(data, colors);
  renderWeeklyChart(data.weekly, colors);
  renderFailureChart(data.failures, colors);
  tableFromRows($("chartTable"), data.rows, { actions: true });
  loadParts(pid).catch(() => {});
  loadFleetCharts(colors).catch(() => {});
}

function drawChart(key, canvasId, config) {
  if (state.charts[key]) state.charts[key].destroy();
  state.charts[key] = new Chart($(canvasId), config);
}

function chartScale(colors, title, extra = {}) {
  return {
    ticks: { color: colors.muted },
    grid: { color: colors.fill },
    title: { display: !!title, text: title, color: colors.muted },
    ...extra,
  };
}

function chartBaseOptions(colors, extra = {}) {
  const { plugins = {}, ...rest } = extra;
  return {
    responsive: true,
    maintainAspectRatio: false,
    ...rest,
    plugins: { legend: { labels: { color: colors.ink } }, ...plugins },
  };
}

function withAlpha(color, alpha = "88") {
  return /^#[0-9a-f]{6}$/i.test(color) ? `${color}${alpha}` : color;
}

function shortDate(ms) {
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function weekLabel(iso) {
  return shortDate(new Date(`${iso}T00:00:00`).getTime());
}

function renderTimelineChart(points, colors) {
  const pointColor = (p) => (p.outcome === "fail" ? colors.red : colors.blue);
  const pointColorB = (p) => (p.outcome === "fail" ? colors.red : colors.orange);
  const radius = (p) => (p.outcome === "fail" ? 6 : 3);
  drawChart("timeline", "chartTimeline", {
    type: "line",
    data: {
      datasets: [
        {
          label: "Layer exposure (s)",
          data: points.map((p) => ({ x: p.t, y: p.exp, p })),
          yAxisID: "y",
          borderColor: colors.blue,
          backgroundColor: points.map(pointColor),
          pointBackgroundColor: points.map(pointColor),
          pointRadius: points.map(radius),
          borderWidth: 1.5,
          stepped: true,
        },
        {
          label: "Bottom exposure (s)",
          data: points.map((p) => ({ x: p.t, y: p.bottom, p })),
          yAxisID: "y2",
          borderColor: colors.orange,
          backgroundColor: points.map(pointColorB),
          pointBackgroundColor: points.map(pointColorB),
          pointRadius: points.map(radius),
          borderWidth: 1.5,
          borderDash: [4, 3],
          stepped: true,
        },
      ],
    },
    options: chartBaseOptions(colors, {
      interaction: { mode: "nearest", intersect: false },
      plugins: {
        tooltip: {
          callbacks: {
            title: (items) => {
              const p = items[0].raw.p;
              return `${new Date(p.t).toLocaleString()} · ${p.layer} mm · ${p.outcome === "fail" ? "FAIL" : "success"}`;
            },
            label: (item) => `${item.dataset.label}: ${item.parsed.y}`,
            afterBody: (items) => {
              const p = items[0].raw.p;
              return [p.file, p.issue].filter(Boolean);
            },
          },
        },
      },
      scales: {
        x: chartScale(colors, "", { type: "linear", ticks: { color: colors.muted, callback: (v) => shortDate(v), maxTicksLimit: 10 } }),
        y: chartScale(colors, "Layer exposure (s)", { position: "left" }),
        y2: chartScale(colors, "Bottom exposure (s)", { position: "right", grid: { drawOnChartArea: false } }),
      },
    }),
  });
}

const CURVE_PALETTE = ["#0a84ff", "#ff9f0a", "#30d158", "#ff453a", "#bf5af2", "#64d2ff", "#ff375f", "#ac8e68", "#ffd60a", "#5e5ce6"];
const CURVE_COMPARE = ["elegoo_8k_standard_space_grey", "elegoo_abs_like_3_black", "anycubic_clear", "superflex"];

function renderCureChart(curves, colors, printerId, resinFilter) {
  if (!curves) return;
  const key = `${printerId}|${resinFilter}`;
  if (state.curveKey !== key) {
    state.curveKey = key;
    state.curveOn = null;
    state.curveTemps = null;
  }
  const resins = curves.resins || [];
  if (!state.curveOn) {
    const on = new Set(resins.filter((r) => r.used).map((r) => r.id));
    if (!resinFilter && on.size < 3) {
      for (const id of CURVE_COMPARE) {
        if (resins.some((r) => r.id === id)) on.add(id);
      }
    }
    if (!on.size && resins[0]) on.add(resins[0].id);
    state.curveOn = on;
  }
  if (!state.curveTemps) state.curveTemps = new Set((curves.temps || []).map(Number));
  const colorOf = Object.fromEntries(resins.map((r, i) => [r.id, CURVE_PALETTE[i % CURVE_PALETTE.length]]));
  $("curveTemps").innerHTML = (curves.temps || [])
    .map((t) => {
      const dash = Number(t) === 25 ? "solid" : Number(t) < 25 ? "dashed" : "dotted";
      const checked = state.curveTemps.has(Number(t)) ? "checked" : "";
      return `<label><input type="checkbox" data-curve-temp="${t}" ${checked}><span class="curve-swatch" style="border-top-style:${dash};border-top-color:${colors.ink}"></span>${t} °C</label>`;
    })
    .join("");
  $("curveResins").innerHTML = resins
    .map((r) => {
      const hint = r.source === "fit" ? `fit from this printer, Dp ${r.dp_um} µm` : `manufacturer baseline, Dp ${r.dp_um} µm`;
      const checked = state.curveOn.has(r.id) ? "checked" : "";
      return `<label title="${esc(r.name)} · ${esc(hint)}"><input type="checkbox" data-curve-resin="${esc(r.id)}" ${checked}><span class="curve-swatch" style="border-top-color:${colorOf[r.id]}"></span>${esc(r.label)}</label>`;
    })
    .join("");
  $("curveTemps").onchange = (e) => {
    const box = e.target.closest("input[data-curve-temp]");
    if (!box) return;
    const temp = Number(box.dataset.curveTemp);
    if (box.checked) state.curveTemps.add(temp);
    else state.curveTemps.delete(temp);
    drawCure(curves, colors, colorOf);
  };
  $("curveResins").onchange = (e) => {
    const box = e.target.closest("input[data-curve-resin]");
    if (!box) return;
    if (box.checked) state.curveOn.add(box.dataset.curveResin);
    else state.curveOn.delete(box.dataset.curveResin);
    drawCure(curves, colors, colorOf);
  };
  drawCure(curves, colors, colorOf);
}

function drawCure(curves, colors, colorOf) {
  const resins = Object.fromEntries((curves.resins || []).map((r) => [r.id, r]));
  const datasets = [];
  for (const series of curves.series || []) {
    if (!state.curveOn.has(series.resin_id) || !state.curveTemps.has(Number(series.temp))) continue;
    const resin = resins[series.resin_id];
    if (!resin) continue;
    const temp = Number(series.temp);
    datasets.push({
      label: `${resin.label} · ${temp} °C`,
      data: (curves.heights || []).map((h, i) => ({ x: h, y: series.exposure[i] })),
      borderColor: colorOf[series.resin_id],
      backgroundColor: colorOf[series.resin_id],
      borderDash: temp === 25 ? [] : temp < 25 ? [7, 4] : [2, 3],
      borderWidth: temp === 25 ? 3 : 2,
      pointRadius: 2,
      pointHoverRadius: 5,
      tension: 0.25,
    });
  }
  for (const resinId of state.curveOn) {
    const resin = resins[resinId];
    if (!resin) continue;
    const prints = (curves.prints || []).filter((p) => p.resin_id === resinId);
    if (!prints.length) continue;
    datasets.push({
      type: "scatter",
      label: `${resin.label} prints`,
      data: prints.map((p) => ({ x: p.layer, y: p.exp, p })),
      pointRadius: 4,
      pointBackgroundColor: withAlpha(colorOf[resinId], "cc"),
      borderColor: colorOf[resinId],
    });
  }
  const shown = (curves.resins || []).filter((r) => state.curveOn.has(r.id));
  const fit = shown.filter((r) => r.source === "fit").map((r) => r.label);
  const baseline = shown.filter((r) => r.source !== "fit").map((r) => r.label);
  const bits = ["Solid is 25 °C, dashed is 20 °C, dotted is 30 °C. Dots are successful prints."];
  if (fit.length) bits.push(`Fit from this printer: ${fit.join(", ")}.`);
  if (baseline.length) bits.push(`Manufacturer baseline: ${baseline.join(", ")}.`);
  $("curveCaption").textContent = bits.join(" ");
  drawChart("curves", "chartCurves", {
    type: "line",
    data: { datasets },
    options: chartBaseOptions(colors, {
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => `${Number(items[0].parsed.x).toFixed(3)} mm`,
            label: (item) => {
              const printed = item.raw.p;
              if (printed) return `${item.dataset.label}: ${printed.exp} s at ${printed.temp} °C`;
              return `${item.dataset.label}: ${item.parsed.y} s`;
            },
          },
        },
      },
      scales: {
        x: chartScale(colors, "Layer height (mm)", {
          type: "linear",
          min: 0.015,
          max: 0.105,
          ticks: { color: colors.muted, callback: (v) => Number(v).toFixed(3) },
        }),
        y: chartScale(colors, "Layer exposure (s)"),
      },
    }),
  });
}

function renderRecipeChart(data, colors) {
  const maxN = Math.max(1, ...data.recipes.map((r) => r.n));
  const bubble = (r) => ({ x: r.layer, y: r.exp, r: 4 + 16 * Math.sqrt(r.n / maxN), g: r });
  $("recipeCaption").textContent =
    `Bubble size = number of runs at that layer height and exposure · stars = today's recommendation for ${data.recommended_resin}`;
  drawChart("recipes", "chartRecipes", {
    type: "bubble",
    data: {
      datasets: [
        {
          label: "All succeeded",
          data: data.recipes.filter((r) => !r.fail).map(bubble),
          backgroundColor: withAlpha(colors.green),
          borderColor: colors.green,
        },
        {
          label: "Had a fail",
          data: data.recipes.filter((r) => r.fail).map(bubble),
          backgroundColor: withAlpha(colors.red),
          borderColor: colors.red,
        },
        {
          type: "scatter",
          label: "Recommended now",
          data: data.recommended.map((r) => ({ x: r.layer, y: r.exp })),
          pointStyle: "star",
          pointRadius: 10,
          borderWidth: 2,
          borderColor: colors.ink,
          backgroundColor: colors.ink,
        },
      ],
    },
    options: chartBaseOptions(colors, {
      plugins: {
        tooltip: {
          callbacks: {
            label: (item) => {
              const g = item.raw.g;
              if (!g) return `Recommended: ${item.parsed.x} mm @ ${item.parsed.y}s`;
              return `${g.layer} mm @ ${g.exp}s — ${g.n} runs (${g.ok} ok, ${g.fail} fail)`;
            },
          },
        },
      },
      scales: {
        x: chartScale(colors, "Layer height (mm)", { min: 0.015, max: 0.065 }),
        y: chartScale(colors, "Layer exposure (s)"),
      },
    }),
  });
}

function renderBottomChart(data, colors) {
  const bins = data.bottom_bins;
  const rec = data.recommended[0];
  const recBin = rec ? bins.findIndex((b) => rec.bottom >= b.lo && rec.bottom < b.hi) : -1;
  $("bottomCaption").textContent = rec
    ? `Runs per 5 s bucket, split by outcome · recommended now: ${rec.bottom}s${recBin < 0 ? " (outside the logged range)" : " (outlined bucket)"}`
    : "Runs per 5 s bottom exposure bucket, split by outcome";
  const outline = bins.map((_, i) => (i === recBin ? colors.ink : "transparent"));
  drawChart("bottom", "chartBottom", {
    type: "bar",
    data: {
      labels: bins.map((b) => b.label),
      datasets: [
        { label: "Success", data: bins.map((b) => b.ok), backgroundColor: colors.green, borderColor: outline, borderWidth: 2 },
        { label: "Fail", data: bins.map((b) => b.fail), backgroundColor: colors.red, borderColor: outline, borderWidth: 2 },
      ],
    },
    options: chartBaseOptions(colors, {
      scales: {
        x: chartScale(colors, "Bottom exposure", { stacked: true }),
        y: chartScale(colors, "Runs", { stacked: true, ticks: { color: colors.muted, precision: 0 } }),
      },
    }),
  });
}

function renderWeeklyChart(weeks, colors) {
  drawChart("weekly", "chartWeekly", {
    data: {
      labels: weeks.map((w) => weekLabel(w.week)),
      datasets: [
        { type: "bar", label: "Successful prints", data: weeks.map((w) => w.prints - w.fails), backgroundColor: colors.blue, stack: "p", yAxisID: "y" },
        { type: "bar", label: "Failed prints", data: weeks.map((w) => w.fails), backgroundColor: colors.red, stack: "p", yAxisID: "y" },
        { type: "line", label: "Print hours", data: weeks.map((w) => w.hours), borderColor: colors.orange, backgroundColor: colors.orange, yAxisID: "y2", tension: 0.3 },
      ],
    },
    options: chartBaseOptions(colors, {
      scales: {
        x: chartScale(colors, "Week of", { stacked: true }),
        y: chartScale(colors, "Prints", { stacked: true, ticks: { color: colors.muted, precision: 0 } }),
        y2: chartScale(colors, "Hours", { position: "right", grid: { drawOnChartArea: false } }),
      },
    }),
  });
}

function renderFailureChart(failures, colors) {
  const empty = !failures.length;
  $("failuresEmpty").hidden = !empty;
  $("chartFailures").parentElement.hidden = empty;
  if (empty) return;
  drawChart("failures", "chartFailures", {
    type: "bar",
    data: {
      labels: failures.map((f) => f.label),
      datasets: [{ label: "Fails", data: failures.map((f) => f.n), backgroundColor: colors.red }],
    },
    options: chartBaseOptions(colors, {
      indexAxis: "y",
      plugins: { legend: { display: false } },
      scales: {
        x: chartScale(colors, "Fails", { ticks: { color: colors.muted, precision: 0 } }),
        y: chartScale(colors, ""),
      },
    }),
  });
}

const FLEET_COLORS = ["#007aff", "#ff9500", "#34c759", "#af52de", "#5ac8fa", "#ff2d55"];

async function loadFleetCharts(colors = chartColors()) {
  const data = await api("/api/charts-fleet");
  const fmt = (v, d = 1, unit = "") => (v == null ? "—" : `${Number(v).toFixed(d)}${unit}`);
  const table = $("fleetTable");
  table.querySelector("thead").innerHTML =
    "<tr><th>Printer</th><th>Runs</th><th>Success</th><th>Fails</th><th>Prints / wk</th><th>Hours / wk</th><th>Main layer</th><th>Typical layer exp.</th><th>Typical bottom exp.</th><th>Last print</th></tr>";
  table.querySelector("tbody").innerHTML = data.printers
    .map(
      (p) => `<tr>
        <td>${esc(p.name)}</td><td>${p.total}</td><td>${fmt(p.success_rate, 0, "%")}</td><td>${p.fail}</td>
        <td>${fmt(p.prints_per_week)}</td><td>${fmt(p.hours_per_week, 0, "h")}</td>
        <td>${p.top_layer != null ? `${p.top_layer} mm` : "—"}</td>
        <td>${fmt(p.typical_exp, 2, "s")}</td><td>${fmt(p.typical_bottom, 0, "s")}</td>
        <td>${p.last_print ? new Date(p.last_print).toLocaleDateString() : "—"}</td>
      </tr>`
    )
    .join("");
  drawChart("fleetWeekly", "chartFleetWeekly", {
    type: "bar",
    data: {
      labels: data.weeks.map(weekLabel),
      datasets: data.printers.map((p, i) => ({
        label: p.name,
        data: data.weekly_prints[p.printer_id] || [],
        backgroundColor: FLEET_COLORS[i % FLEET_COLORS.length],
      })),
    },
    options: chartBaseOptions(colors, {
      scales: {
        x: chartScale(colors, "Week of", { stacked: true }),
        y: chartScale(colors, "Prints", { stacked: true, ticks: { color: colors.muted, precision: 0 } }),
      },
    }),
  });
}

async function loadHistory({ keepScroll = false } = {}) {
  state.histData = await api("/api/history");
  fillHistFilterOptions(state.histData.rows);
  renderHistory();
  if (!keepScroll) $("histScroll").scrollTop = 0;
}

const HIST_FILTER_IDS = [
  "hfSearch", "hfPrinter", "hfStatus", "hfPart", "hfSize", "hfResin", "hfLayer", "hfExpMin", "hfExpMax", "hfFrom", "hfTo",
];

function fillHistFilterOptions(rows) {
  const fill = (id, allLabel, values) => {
    const sel = $(id);
    const keep = sel.value;
    sel.innerHTML =
      `<option value="">${allLabel}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    if (values.includes(keep)) sel.value = keep;
  };
  const uniq = (key) => [...new Set(rows.map((r) => r[key]).filter(Boolean))];
  fill("hfPrinter", "All printers", uniq("printer").sort());
  const partSel = $("hfPart");
  const keepPart = partSel.value;
  const parts = new Map();
  rows.forEach((r) => r.part_family && parts.set(r.part_family, r.part_group || "Other"));
  const partNames = [...parts.keys()].sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
  partSel.innerHTML =
    `<option value="">All parts</option>` +
    groupedOptions(
      partNames,
      (n) => parts.get(n),
      (n) => `<option value="p:${esc(n)}">${esc(n)}</option>`,
      (g) => `<option value="g:${esc(g)}">All ${esc(g)} parts</option>`
    );
  if ([...partSel.options].some((o) => o.value === keepPart)) partSel.value = keepPart;
  fill("hfResin", "All resins", uniq("resin").sort());
  fill("hfLayer", "All layer heights", uniq("layer").sort((a, b) => Number(a) - Number(b)));
}

function histRowMatches(r, f) {
  if (f.printer && r.printer !== f.printer) return false;
  if (f.resin && r.resin !== f.resin) return false;
  if (f.part.startsWith("p:") && r.part_family !== f.part.slice(2)) return false;
  if (f.part.startsWith("g:") && (r.part_group || "Other") !== f.part.slice(2)) return false;
  if (f.size && r.size_class !== f.size) return false;
  if (f.layer && r.layer !== f.layer) return false;
  if (f.status) {
    const known = ["SUCCESS", "UNREPORTED", "PRINTING"];
    const kind = known.includes(r.outcome) ? r.outcome : "FAIL";
    if (kind !== f.status) return false;
  }
  const exp = parseFloat(r.layer_exp);
  if (f.expMin !== "" && !(exp >= Number(f.expMin))) return false;
  if (f.expMax !== "" && !(exp <= Number(f.expMax))) return false;
  const day = (r.when || "").slice(0, 10);
  if (f.from && (!day || day < f.from)) return false;
  if (f.to && (!day || day > f.to)) return false;
  if (f.search) {
    const hay = [r.file, r.app, r.note, r.issue, r.quality, r.id, r.printer, r.resin, r.part_family, r.part_group]
      .join(" ")
      .toLowerCase();
    if (!f.search.split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  return true;
}

function renderHistory() {
  const data = state.histData;
  if (!data) return;
  const f = {
    search: $("hfSearch").value.trim().toLowerCase(),
    printer: $("hfPrinter").value,
    status: $("hfStatus").value,
    part: $("hfPart").value,
    size: $("hfSize").value,
    resin: $("hfResin").value,
    layer: $("hfLayer").value,
    expMin: $("hfExpMin").value,
    expMax: $("hfExpMax").value,
    from: $("hfFrom").value,
    to: $("hfTo").value,
  };
  const filtering = HIST_FILTER_IDS.some((id) => $(id).value !== "");
  HIST_FILTER_IDS.forEach((id) => $(id).classList.toggle("active", $(id).value !== ""));
  const rows = filtering ? data.rows.filter((r) => histRowMatches(r, f)) : data.rows;

  const bits = [];
  if (filtering) {
    const ok = rows.filter((r) => r.outcome === "SUCCESS").length;
    const reported = rows.filter((r) => !["UNREPORTED", "PRINTING"].includes(r.outcome)).length;
    bits.push(
      `Showing ${rows.length} of ${data.rows.length}` +
        (reported ? ` · ${Math.round((ok / reported) * 100)}% success of ${reported} reported` : "")
    );
  } else {
    bits.push(`${data.stats.total} reported · ${(data.stats.success_rate * 100).toFixed(0)}% success`);
    if (data.stats.printing) bits.push(`${data.stats.printing} printing now`);
    if (data.stats.unreported) bits.push(`${data.stats.unreported} waiting for a report`);
  }
  $("histStats").textContent = bits.join(" · ");
  if (filtering && !rows.length) {
    $("histTable").querySelector("thead").innerHTML = "";
    $("histTable").querySelector("tbody").innerHTML = `<tr><td>No prints match these filters.</td></tr>`;
    return;
  }
  tableFromRows($("histTable"), rows, { actions: true, printer: true });
}

function bindHistFilters() {
  HIST_FILTER_IDS.forEach((id) => {
    const el = $(id);
    el.addEventListener(el.tagName === "SELECT" || el.type === "date" ? "change" : "input", () => {
      renderHistory();
      $("histScroll").scrollTop = 0;
    });
  });
  $("hfClear").addEventListener("click", () => {
    HIST_FILTER_IDS.forEach((id) => ($(id).value = ""));
    renderHistory();
  });
}

function openHistoryReport(row) {
  openReport(row.printer_id, { history: row });
}

async function dismissPending(row) {
  if (!confirm(`Remove unreported print "${row.file}" without logging it?`)) return;
  try {
    await api(`/api/history/pending/${row.report_id}`, { method: "DELETE" });
    await loadHistory();
  } catch (err) {
    alert(err.message || String(err));
  }
}

function openReport(printerId, ctx = null) {
  state.reportPrinterId = printerId;
  state.reportCtx = ctx;
  const hist = ctx?.history;
  const job = hist ? null : (state.fleet?.open_jobs || []).find((j) => j.printer_id === printerId);
  const printer = state.printers.find((p) => p.id === printerId);
  const prev = $("reportPreview");
  let preview = job?.preview_url || "";
  if (hist) {
    const verb = hist.report_kind === "run" ? "Edit report" : "Report";
    $("reportTitle").textContent = `${verb} · ${printer?.short_name || printerId}${hist.report_kind === "run" ? ` ${hist.id}` : ""}`;
    $("reportSummary").textContent =
      `${hist.file || "(unknown file)"} · ${hist.when} · layer height ${hist.layer} mm · ` +
      `layer exposure ${hist.layer_exp} · bottom exposure ${hist.bottom_exp}` +
      (hist.report_kind === "run" ? ` · currently ${hist.outcome.toLowerCase()}` : "");
    if (hist.preview) preview = `/previews/${hist.preview.split(/[/\\\\]/).pop()}`;
  } else {
    $("reportTitle").textContent = `Report · ${printer?.short_name || printerId}`;
    $("reportSummary").textContent = job
      ? `${job.filename || "(unknown file)"} · L${job.current_layer ?? "?"}/${job.total_layers ?? "?"} · ${job.vat_temperature_c?.toFixed?.(0) ?? "—"}°`
      : "No tracked SDCP job — will log a web report for this printer.";
  }
  if (preview) {
    prev.src = preview;
    prev.hidden = false;
  } else {
    prev.removeAttribute("src");
    prev.hidden = true;
  }
  const reportResin =
    hist?.resin_id || ctx?.resin_id || job?.resin_id || vatResinFor(printerId) || state.resins[0]?.id;
  const resinOpts = state.resins.some((r) => r.id === reportResin)
    ? state.resins
    : [...state.resins, { id: reportResin, name: hist?.resin || reportResin }];
  fillSelect($("reportResin"), resinOpts, "id", "name", reportResin);
  $("reportApp").value = (hist ? hist.part_application : job?.part_application) || "";
  $("reportIssue").value = hist?.failure_issue || "";
  $("reportNotes").value = "";
  state.reportQuality = { ...(hist?.quality_answers || {}) };
  renderQualityChecks();
  renderReportSettings(hist?.report_kind === "run" ? hist.settings : null);
  $("reportModal").classList.add("show");
}

const EDITABLE_SETTINGS = [
  ["layer_height", "Layer height (mm)", "0.005"],
  ["normal_exposure_time", "Layer exposure (s)", "0.1"],
  ["bottom_exposure_time", "Bottom exposure (s)", "0.5"],
  ["bottom_layer_count", "Bottom layers", "1"],
  ["transition_layer_count", "Transition layers", "1"],
];

function renderReportSettings(settings) {
  const wrap = $("reportSettingsWrap");
  const root = $("reportSettings");
  if (!settings) {
    wrap.hidden = true;
    root.innerHTML = "";
    return;
  }
  wrap.hidden = false;
  root.innerHTML = EDITABLE_SETTINGS.map(
    ([key, label, step]) =>
      `<label>${esc(label)}<input type="number" min="0" step="${step}" data-setting="${key}"
        data-orig="${settings[key] ?? ""}" value="${settings[key] ?? ""}" /></label>`
  ).join("");
  root.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", () => {
      inp.classList.toggle("changed", Number(inp.value) !== Number(inp.dataset.orig));
    });
  });
}

function changedReportSettings() {
  const out = {};
  document.querySelectorAll("#reportSettings input[data-setting]").forEach((inp) => {
    if (inp.value === "") return;
    const val = Number(inp.value);
    if (val !== Number(inp.dataset.orig)) out[inp.dataset.setting] = val;
  });
  return out;
}

function renderQualityChecks() {
  const checks = state.meta?.quality_checks || [];
  const answers = state.reportQuality || {};
  const root = $("reportChecks");
  root.innerHTML = checks
    .map((c) => {
      const btn = (val, label) => {
        const on = answers[c.key] === val;
        const tone = val === c.good_answer ? "good" : "bad";
        return `<button type="button" class="${on ? "on " + tone : ""}" data-qc="${esc(c.key)}" data-val="${val}">${label}</button>`;
      };
      return `<div class="qc-row"><span>${esc(c.question)}</span>
        <span class="qc-seg">${btn(true, "Yes")}${btn(false, "No")}</span></div>`;
    })
    .join("");
  root.querySelectorAll("[data-qc]").forEach((b) => {
    b.addEventListener("click", () => {
      const key = b.dataset.qc;
      const val = b.dataset.val === "true";
      if (state.reportQuality[key] === val) delete state.reportQuality[key];
      else state.reportQuality[key] = val;
      renderQualityChecks();
    });
  });
}

function qualityProblems() {
  const answers = state.reportQuality || {};
  return (state.meta?.quality_checks || []).filter(
    (c) => c.key in answers && answers[c.key] !== c.good_answer
  );
}

function closeReport() {
  $("reportModal").classList.remove("show");
  state.reportPrinterId = null;
  state.reportCtx = null;
}

async function submitReport(outcome) {
  const notes = $("reportNotes").value.trim();
  const issue = $("reportIssue").value;
  if (outcome === "fail" && !issue && !notes && !qualityProblems().length) {
    alert("Mark what went wrong in the quality check or pick a failure type.");
    return;
  }
  const ctx = state.reportCtx || {};
  const hist = ctx.history;
  const body = {
    printer_id: state.reportPrinterId,
    outcome,
    notes,
    resin_id: $("reportResin").value || null,
    part_application: $("reportApp").value || null,
    failure_issue: outcome === "success" ? "" : issue,
    layer_height: ctx.layer_height || null,
    quality: state.reportQuality || {},
    settings: hist?.report_kind === "run" ? changedReportSettings() : {},
  };
  let url = "/api/report";
  if (hist?.report_kind === "run") url = `/api/history/run/${hist.report_id}/report`;
  else if (hist?.report_kind === "pending") url = `/api/history/pending/${hist.report_id}/report`;
  try {
    await api(url, { method: "POST", body: JSON.stringify(body) });
    const fromCalibrate = !!state.reportCtx && !hist;
    closeReport();
    await loadFleet(false);
    await loadToday();
    if (hist) await loadHistory();
    if ($("panel-charts").classList.contains("active")) await loadCharts();
    if (fromCalibrate) await runRecommend();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function loadFleet(force = false) {
  state.fleet = await api(`/api/fleet${force ? "?refresh=true" : ""}`);
  renderKpis(state.fleet.summary);
  if (state.fleet.room_vat_c != null && $("calTemp") && !$("calTemp").dataset.dirty) {
    $("calTemp").value = state.fleet.room_vat_c.toFixed(1);
  }
}

function syncCalibrateForm() {
  if (!state.printers?.length) return;
  fillSelect($("calPrinter"), state.printers, "id", "short_name", $("calPrinter").value || state.printers[0].id);
  fillSelect(
    $("calResin"),
    state.resins,
    "id",
    "name",
    $("calResin").value || vatResinFor($("calPrinter").value) || state.resins[0]?.id
  );
  updateCalHero();
  loadCalParts().catch(() => {});
}

function updateCalHero() {
  const pid = $("calPrinter")?.value;
  const p = state.printers.find((x) => x.id === pid);
  if (!p) return;
  $("calPrinterName").textContent = p.short_name || p.name;
  $("calPrinterSpec").textContent = (p.specs || []).slice(0, 2).join(" · ");
  setImg($("calPrinterPhoto"), p.photo, p.short_name);
  const rid = $("calResin")?.value;
  const r = state.resins.find((x) => x.id === rid);
  if (r) {
    $("calResinName").textContent = r.name;
    setImg($("calResinPhoto"), r.photo, r.name);
  }
}

function groupedOptions(items, groupOf, render, header = null) {
  const groups = new Map();
  items.forEach((it) => {
    const g = groupOf(it) || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(it);
  });
  const names = [...groups.keys()].sort((a, b) =>
    a === "Other" ? 1 : b === "Other" ? -1 : a.localeCompare(b, undefined, { sensitivity: "base" })
  );
  return names
    .map((g) => `<optgroup label="${esc(g)}">${header ? header(g) : ""}${groups.get(g).map(render).join("")}</optgroup>`)
    .join("");
}

async function loadCalParts() {
  const pid = $("calPrinter").value;
  if (!pid) return;
  const data = await api(`/api/parts?printer_id=${encodeURIComponent(pid)}`);
  state.calParts = data.families;
  const sel = $("calPart");
  const keep = sel.value;
  const parts = [...data.families].sort((a, b) => a.family.localeCompare(b.family, undefined, { sensitivity: "base" }));
  sel.innerHTML =
    `<option value="">Any / new part</option>` +
    groupedOptions(parts, (p) => p.group, (p) =>
      `<option value="${esc(p.family_id)}">${esc(p.family)} (${p.runs} run${p.runs === 1 ? "" : "s"})</option>`
    );
  if (parts.some((p) => p.family_id === keep)) sel.value = keep;
}

async function loadParts(pid) {
  const data = await api(`/api/parts?printer_id=${encodeURIComponent(pid)}`);
  renderShapeScanStatus(data.shape_scan, pid);
  const table = $("partsTable");
  const rows = data.families;
  if (!rows.length) {
    table.querySelector("thead").innerHTML = "";
    table.querySelector("tbody").innerHTML = `<tr><td>No prints yet.</td></tr>`;
    return;
  }
  table.querySelector("thead").innerHTML = `<tr>
    <th>Part</th><th>Size</th><th>Shape</th><th>Runs</th><th>Success</th><th>Typical layer exposure</th><th>Typical bottom</th>
    <th>Fail reasons</th><th>Printers</th><th>Last print</th><th></th></tr>`;
  const body = rows.map((p, i) => {
      const size = p.size_class
        ? `${sizeBadge(p.size_class)} <span class="muted">${p.volume_ml.toFixed(0)} ml · ${p.avg_area_mm2.toFixed(0)} mm²/layer</span>`
        : `<span class="muted">—</span>`;
      const rate = p.runs ? `${p.success_rate.toFixed(0)}% <span class="muted">(${p.success}/${p.runs})</span>` : "—";
      return `<tr>
        <td title="${esc(p.latest_file)}">${esc(p.family)}</td>
        <td>${size}</td>
        <td class="shape-cell">${
          p.shape
            ? `${shapeBadges(p.shape_tags, p.shape_file)} <span class="muted">${esc(p.shape)}</span>`
            : `<span class="muted">Not scanned</span>`
        }</td>
        <td>${p.runs}</td>
        <td class="${p.fail ? (p.success_rate < 75 ? "bad" : "") : "good"}">${rate}</td>
        <td>${p.typical_exp.toFixed(2)} s</td>
        <td>${p.typical_bottom.toFixed(0)} s</td>
        <td>${esc(p.fail_issues.join(", ") || "—")}</td>
        <td>${esc(p.printers.join(", "))}</td>
        <td>${p.last_print ? esc(shortDate(Date.parse(p.last_print))) : "—"}</td>
        <td class="nowrap"><button type="button" class="btn secondary sm" data-rename-part="${i}">Rename</button>
          <button type="button" class="btn secondary sm" data-group-part="${i}">Group…</button></td>
      </tr>`;
    });
  const groups = [];
  rows.forEach((p, i) => {
    let g = groups[groups.length - 1];
    if (!g || g.name !== p.group) groups.push((g = { name: p.group, idx: [] }));
    g.idx.push(i);
  });
  table.querySelector("tbody").innerHTML = groups
    .map((g, gi) => {
      const parts = g.idx.map((i) => rows[i]);
      const runs = parts.reduce((s, p) => s + p.runs, 0);
      const ok = parts.reduce((s, p) => s + p.success, 0);
      const header = `<tr class="group-row"><td colspan="11">
        <strong>${esc(g.name)}</strong>
        <span class="muted">${parts.length} part${parts.length === 1 ? "" : "s"} · ${runs} runs · ${runs ? Math.round((ok / runs) * 100) : 0}% success</span>
        <button type="button" class="btn secondary sm" data-rename-group="${gi}">Rename group</button></td></tr>`;
      return header + g.idx.map((i) => body[i]).join("");
    })
    .join("");
  table.querySelectorAll("[data-rename-part]").forEach((b) =>
    b.addEventListener("click", () => renamePart(rows[Number(b.dataset.renamePart)], pid))
  );
  table.querySelectorAll("[data-group-part]").forEach((b) =>
    b.addEventListener("click", () => {
      const p = rows[Number(b.dataset.groupPart)];
      const names = [...new Set(rows.map((r) => r.group))].join(", ");
      setPartGroup(
        [p.family_id],
        `Group for "${p.family}" (now ${p.group}).\nExisting groups: ${names}\nType a new name to start a group. Leave empty for the automatic group.`,
        p.group,
        pid
      );
    })
  );
  table.querySelectorAll("[data-rename-group]").forEach((b) =>
    b.addEventListener("click", () => {
      const g = groups[Number(b.dataset.renameGroup)];
      setPartGroup(
        g.idx.map((i) => rows[i].family_id),
        `New name for the "${g.name}" group (${g.idx.length} parts).\nUse an existing group's name to merge them.`,
        g.name,
        pid
      );
    })
  );
}

async function setPartGroup(familyIds, message, current, pid) {
  const group = window.prompt(message, current);
  if (group === null || group.trim() === current) return;
  await api("/api/part-groups", { method: "POST", body: JSON.stringify({ family_ids: familyIds, group: group.trim() }) });
  await loadParts(pid);
  loadCalParts().catch(() => {});
  if (state.histData) loadHistory({ keepScroll: true }).catch(() => {});
}

function renderShapeScanStatus(s, pid) {
  const el = $("shapeScanStatus");
  const btn = $("btnShapeScan");
  if (!s?.uvtools) {
    el.textContent = "Shape detection needs UVtools on the host PC.";
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.disabled = !!s.running;
  let text = `${s.files_scanned} sliced file${s.files_scanned === 1 ? "" : "s"} scanned for channels / fine features`;
  if (s.running)
    text += ` · scanning ${s.current || "…"}${s.queued > 1 ? ` (${s.queued - 1} more queued)` : ""} — a few minutes per file`;
  if (s.last_error) text += ` · last problem: ${s.last_error}`;
  el.textContent = text;
  clearTimeout(state.shapeScanTimer);
  if (s.running) state.shapeScanTimer = setTimeout(() => loadParts(pid).catch(() => {}), 15000);
}

async function startShapeScan() {
  const pid = $("chartPrinter").value;
  const s = await api("/api/shape-scan", { method: "POST" });
  renderShapeScanStatus({ ...s, files_scanned: s.files_scanned ?? 0 }, pid);
  loadParts(pid).catch(() => {});
}

async function renamePart(part, pid) {
  const name = window.prompt(
    `New name for "${part.family}".\nUse an existing part's name to merge them. Leave empty to go back to the name from the file.`,
    part.family
  );
  if (name === null || name.trim() === part.family) return;
  await api("/api/part-families", { method: "POST", body: JSON.stringify({ auto_keys: part.auto_keys, name: name.trim() }) });
  await loadParts(pid);
  loadCalParts().catch(() => {});
  if (state.histData) loadHistory({ keepScroll: true }).catch(() => {});
}

async function runRecommend() {
  const body = {
    printer_id: $("calPrinter").value,
    resin_id: $("calResin").value,
    layer_height: parseFloat($("calLayer").value),
    vat_temperature_c: parseFloat($("calTemp").value),
    geometry: $("calGeom").value || "normal",
    cross_section_mm2: parseFloat($("calCross").value) || 0,
    part_family: $("calPart").value,
    size_class: $("calSize").value,
  };
  $("calConfidence").textContent = "Recommending…";
  try {
    const data = await api("/api/recommend", { method: "POST", body: JSON.stringify(body) });
    state.lastRecommend = data;
    $("calConfidence").textContent = `${data.confidence} · ${data.source_detail || ""} · ${data.vat_temperature_c?.toFixed?.(0) ?? "—"}°C`;
    $("calNormal").textContent = `${Number(data.settings.normal_exposure_time).toFixed(2)}s`;
    $("calBottom").textContent = `${Number(data.settings.bottom_exposure_time).toFixed(1)}s`;
    $("calBottomDetail").textContent = data.bottom_detail ? `Bottom exposure: ${data.bottom_detail}` : "";
    $("calPartDetail").textContent = [data.part_detail, data.exposure_detail].filter(Boolean).join(" · ");
    $("calFields").innerHTML = (data.fields || [])
      .map(
        (f) =>
          `<div class="cal-field"><span>${esc(f.label)}</span><strong>${esc(
            typeof f.value === "number"
              ? Number(f.value).toFixed(f.key.includes("count") ? 0 : f.key.includes("layer_height") ? 3 : 2)
              : f.value
          )}${f.unit ? " " + esc(f.unit) : ""}</strong></div>`
      )
      .join("");
    $("calWarnings").textContent = (data.warnings || []).join(" · ");
    $("calClipboard").textContent = data.clipboard || "";
    $("calClipboard").hidden = !data.clipboard;
    setImg($("calPrinterPhoto"), data.printer?.photo, data.printer?.short_name);
    setImg($("calResinPhoto"), data.resin?.photo, data.resin?.name);
    $("calResinName").textContent = data.resin?.name || "";
    $("calPrinterName").textContent = data.printer?.short_name || "";
  } catch (err) {
    $("calConfidence").textContent = err.message || String(err);
    state.lastRecommend = null;
  }
}

function calReport() {
  openReport($("calPrinter").value, {
    resin_id: $("calResin").value,
    layer_height: parseFloat($("calLayer").value) || null,
  });
}

async function loadTodayParts() {
  const data = await api("/api/parts");
  state.todayParts = data.families;
  const sel = $("todayPart");
  const keep = sel.value;
  const parts = [...data.families].sort((a, b) => a.family.localeCompare(b.family, undefined, { sensitivity: "base" }));
  sel.innerHTML =
    `<option value="">Any part</option>` +
    groupedOptions(parts, (p) => p.group, (p) =>
      `<option value="${esc(p.family_id)}">${esc(p.family)} (${p.runs})</option>`
    );
  if (parts.some((p) => p.family_id === keep)) sel.value = keep;
}

// A model opened in Chitubox longer ago than this is shown but not applied automatically.
const DETECT_AUTO_APPLY_MS = 6 * 3600 * 1000;

function timeAgo(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function signedPct(v) {
  return v == null ? "" : `${v > 0 ? "+" : ""}${v.toFixed(0)}%`;
}

async function pollDetected() {
  if (state.settings && !state.settings.watch_chitubox) return;
  let d;
  try {
    d = await api("/api/detected");
  } catch (_) {
    return;
  }
  state.detected = d;
  const m = d.model;
  const key = m ? `${m.file}|${m.seen_at}` : "";
  if (m?.status === "ready" && m.family_id && key !== state.detectSeen) {
    state.detectSeen = key;
    if (Date.now() - new Date(m.seen_at).getTime() < DETECT_AUTO_APPLY_MS) await applyDetectedPart(m);
  }
  renderDetected();
}

async function applyDetectedPart(m) {
  const sel = $("todayPart");
  if (![...sel.options].some((o) => o.value === m.family_id)) {
    sel.insertAdjacentHTML("afterbegin", `<option value="${esc(m.family_id)}">${esc(m.family)} (new)</option>`);
    sel.insertBefore(sel.querySelector('option[value=""]'), sel.firstChild);
  }
  sel.value = m.family_id;
  $("todaySize").value = m.size_class || "";
  state.detectApplied = m.family_id;
  renderDetected();
  await loadToday();
}

function renderDetected() {
  const box = $("detectBanner");
  const d = state.detected;
  const fresh = (iso) => iso && Date.now() - new Date(iso).getTime() < DETECT_AUTO_APPLY_MS;
  const using = d?.model && $("todayPart").value === d.model.family_id;
  const m = d?.model && (using || fresh(d.model.seen_at)) ? d.model : null;
  const s = d?.sent && fresh(d.sent.sent_at) ? d.sent : null;
  renderAlerts();
  if (!m && !s) {
    box.hidden = true;
    return;
  }
  const rows = [];
  if (m) {
    const what =
      m.status === "scanning"
        ? "reading the model…"
        : [
            m.runs ? `${m.runs} prints in History` : "new part",
            m.group && m.group !== "Other" ? `${m.group} group` : "",
            m.size_class ? `${SIZE_LABELS[m.size_class]} plates` : "",
            m.shape || m.error,
          ]
            .filter(Boolean)
            .join(" · ");
    const action = using
      ? `<span class="detect-using">✓ Settings tuned for this part</span> <button class="btn secondary" data-detect="clear">Any part</button>`
      : m.status === "ready"
        ? `<button class="btn" data-detect="use">Use for settings</button>`
        : "";
    const short = m.status === "scanning" ? "reading the model…" : m.runs ? `${m.runs} prints in History` : "new part";
    rows.push(`<div class="detect-row">
      <span class="detect-label">Opened in Chitubox</span>
      <strong>${esc(m.file)}</strong>
      <span class="detect-meta" title="${esc(what)}">${esc(timeAgo(m.seen_at))} · ${esc(short)}</span>
      <span class="detect-action">${action}</span>
    </div>`);
  }
  if (s) {
    const resin = s.resin_name
      ? `resin profile: ${s.resin_name}`
      : "resin from vat (name the resin profile in Chitubox to detect it)";
    const checks = (s.checks || [])
      .map((c) => {
        const tip = [c.detail, ...(c.notes || [])].filter(Boolean).join("\n");
        return `<span class="detect-check ${c.ok ? "ok" : "off"}" title="${esc(tip)}">${esc(c.short_name)}:
          ${c.recommended.toFixed(2)}s / ${c.recommended_bottom.toFixed(0)}s
          ${c.ok ? "✓" : `(file ${signedPct(c.diff_pct)} / ${signedPct(c.bottom_diff_pct)})`}${c.notes?.length ? " ⚠" : ""}</span>`;
      })
      .join("");
    rows.push(`<div class="detect-row">
      <span class="detect-label">Sent ${esc(timeAgo(s.sent_at))}</span>
      <strong>${esc(s.file)}</strong>
      <span class="detect-meta" title="${esc(`${s.machine} · ${resin}`)}">${s.layer_height} mm · file ${Number(s.exposure).toFixed(2)}s / ${Number(s.bottom_exposure).toFixed(0)}s</span>
      <span class="detect-action">${checks || `<span class="detect-meta">No printer matches ${esc(s.machine)}</span>`}</span>
    </div>`);
  }
  box.innerHTML = rows.join("");
  box.hidden = false;
  box.querySelector('[data-detect="use"]')?.addEventListener("click", () => applyDetectedPart(m));
  box.querySelector('[data-detect="clear"]')?.addEventListener("click", () => {
    $("todayPart").value = "";
    $("todaySize").value = "";
    renderDetected();
    loadToday();
  });
}

async function loadToday() {
  const q = new URLSearchParams({ part_family: $("todayPart").value, size_class: $("todaySize").value });
  state.today = await api(`/api/today?${q}`);
  // Merge live status onto today cards from fleet
  if (state.fleet) {
    const byId = Object.fromEntries(state.fleet.printers.map((p) => [p.printer_id, p]));
    state.today.cards.forEach((c) => {
      if (byId[c.printer_id]) c.status = byId[c.printer_id];
    });
  }
  renderToday();
}

async function pollNewPrinters() {
  try {
    const res = await api("/api/fleet/new");
    state.newPrinters = res.printers || [];
  } catch (_) {
    return;
  }
  renderNewPrinters();
  const printersOpen = $("panel-printers")?.classList.contains("active");
  if (printersOpen && !printersUi.searching && !printersUi.searched) {
    renderFound();
    renderRemoved();
  }
}

function renderNewPrinters() {
  const box = $("newPrinterNotice");
  if (!box) return;
  const rows = state.newPrinters || [];
  if (!rows.length) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = rows
    .map(
      (f) => `<div class="detect-row">
        <span class="detect-label">New printer</span>
        <strong>${esc(f.name || f.model_label || f.host)}</strong>
        <span class="detect-meta">${esc([f.model_label, f.host].filter(Boolean).join(" · "))}${historyNote(f.history_count)}. Add it to the fleet?</span>
        <span class="detect-action">
          <button class="btn" data-notice-add="${esc(f.key)}">Add to fleet</button>
          <label class="notice-check"><input type="checkbox" data-notice-ignore="${esc(f.key)}" /> Don't remind me again</label>
        </span>
      </div>`
    )
    .join("");
  box.querySelectorAll("[data-notice-add]").forEach((b) =>
    b.addEventListener("click", () => addNoticePrinter(b.dataset.noticeAdd))
  );
  box.querySelectorAll("[data-notice-ignore]").forEach((input) =>
    input.addEventListener("change", () => {
      if (input.checked) ignoreNoticePrinter(input.dataset.noticeIgnore);
    })
  );
}

async function addNoticePrinter(key) {
  const f = (state.newPrinters || []).find((row) => row.key === key);
  if (!f) return;
  try {
    await api("/api/fleet/printers", { method: "POST", body: JSON.stringify({ printers: [f] }) });
    await reloadAfterFleetChange();
    await pollNewPrinters();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function ignoreNoticePrinter(key) {
  try {
    await api("/api/fleet/ignore", { method: "POST", body: JSON.stringify({ key }) });
    state.newPrinters = (state.newPrinters || []).filter((row) => row.key !== key);
    renderNewPrinters();
    if ($("panel-printers")?.classList.contains("active") && !printersUi.searched) renderFound();
  } catch (err) {
    alert(err.message || String(err));
  }
}

async function boot() {
  applyTheme(localStorage.getItem(THEME_KEY) || "system");
  $("themeSelect").addEventListener("change", () => applyTheme($("themeSelect").value));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if ((localStorage.getItem(THEME_KEY) || "system") === "system") applyTheme("system");
  });

  document.querySelectorAll(".seg").forEach((seg) => {
    placeSegPill(seg, false);
    new ResizeObserver(() => placeSegPill(seg, segPillsReady)).observe(seg);
  });
  document.fonts?.ready.then(() => document.querySelectorAll(".seg").forEach((seg) => placeSegPill(seg, false)));

  document.querySelectorAll(".nav button").forEach((b) => {
    b.addEventListener("click", () => setPanel(b.dataset.panel));
  });
  $("btnRefresh").addEventListener("click", async () => {
    await loadFleet(true);
    await loadToday();
  });
  $("btnSearchPrinters").addEventListener("click", () => searchPrinters());
  $("btnAddByIp").addEventListener("click", () => {
    const ip = $("addIp").value.trim();
    if (ip) searchPrinters(ip);
  });
  $("addIp").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("btnAddByIp").click();
  });
  $("resinSelect").addEventListener("change", () => loadToday());
  $("todayPart").addEventListener("change", () => {
    const fam = (state.todayParts || []).find((p) => p.family_id === $("todayPart").value);
    $("todaySize").value = fam?.size_class || "";
    if (state.detected) renderDetected();
    loadToday();
  });
  $("todaySize").addEventListener("change", () => loadToday());
  $("chartPrinter").addEventListener("change", () => {
    $("chartResinFilter").value = "";
    loadCharts();
  });
  $("chartResinFilter").addEventListener("change", () => loadCharts());
  $("btnImportPrinters").addEventListener("click", importPrinterHistory);
  $("btnShapeScan").addEventListener("click", () => startShapeScan().catch(() => {}));
  bindHistFilters();
  $("btnChartRefresh").addEventListener("click", () => loadCharts());
  $("reportOk").addEventListener("click", () => submitReport("success"));
  $("reportFail").addEventListener("click", () => submitReport("fail"));
  $("reportCancel").addEventListener("click", closeReport);
  $("btnRecommend").addEventListener("click", () => runRecommend());
  $("calPrinter").addEventListener("change", () => {
    const rid = vatResinFor($("calPrinter").value);
    if (rid) $("calResin").value = rid;
    updateCalHero();
    loadCalParts().catch(() => {});
  });
  $("calPart").addEventListener("change", () => {
    const fam = (state.calParts || []).find((p) => p.family_id === $("calPart").value);
    if (fam?.size_class) $("calSize").value = fam.size_class;
  });
  $("calResin").addEventListener("change", updateCalHero);
  $("calTemp").addEventListener("input", () => {
    $("calTemp").dataset.dirty = "1";
  });
  $("btnCopySettings").addEventListener("click", async () => {
    const text = $("calClipboard").textContent || "";
    if (!text) {
      alert("Run Recommend first.");
      return;
    }
    try {
      await navigator.clipboard.writeText(text);
      alert("Settings copied.");
    } catch (_) {
      $("calClipboard").hidden = false;
      $("calClipboard").select?.();
      alert("Copy manually from the text box.");
    }
  });
  $("btnCalSuccess").addEventListener("click", () => calReport("success"));
  $("btnCalFail").addEventListener("click", () => calReport("fail"));

  const [resins, printers, meta, geometries] = await Promise.all([
    api("/api/resins"),
    api("/api/printers"),
    api("/api/meta"),
    api("/api/geometries"),
  ]);
  state.resins = resins;
  state.printers = printers;
  state.meta = meta;
  fillSelect($("resinSelect"), resins, "id", "name", resins[0]?.id);
  fillSelect($("calGeom"), geometries, "id", "label", "normal");
  fillPrinterSelects();
  $("headerSub").textContent = meta.lan
    ? `On your network · ${meta.lan_urls.slice(1).join(" · ") || meta.lan_urls[0]}`
    : `v${meta.version}`;
  $("repoLink").href = meta.repo_url;

  await loadFleet(false);
  await loadTodayParts().catch(() => {});
  syncCalibrateForm();
  await loadToday();
  segPillsReady = true;
  const linked = location.hash.slice(1);
  if (!printers.length) setPanel("printers");
  else if (linked && $(`panel-${linked}`)) setPanel(linked);
  pollDetected();
  pollNewPrinters();
  setInterval(pollDetected, 5000);
  setInterval(async () => {
    try {
      await loadFleet(false);
      await pollNewPrinters();
      const pickingResin = document.activeElement?.matches?.("[data-vat-resin]");
      if ($("panel-today").classList.contains("active") && !pickingResin) await loadToday();
      if ($("panel-history").classList.contains("active")) await loadHistory({ keepScroll: true });
    } catch (_) {}
  }, 20000);
}

boot().catch((err) => {
  console.error(err);
  $("todayCards").innerHTML = `<div class="card">Failed to load: ${esc(err.message)}</div>`;
});
