/* =====================================================================
   Opportunity board: loads data/listings.json, renders stats, filters,
   the sortable table, preset views, and the request form.
   The personal tracker lives in tracker.js and uses window.Board.
   ===================================================================== */
(function () {
  "use strict";
  const CFG = window.SITE_CONFIG;
  const $ = (sel) => document.querySelector(sel);
  const DAY = 86400000;

  // Escape text before putting it in HTML (listing text comes from the web).
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Only allow http(s) links.
  const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "#");

  // ------------------------------------------------------------------
  // Preset views. Each is a filter function applied before user filters.
  // Add your own by copying an entry; it appears at #/view/<key>.
  // ------------------------------------------------------------------
  const VIEWS = {
    "biotech-summer": {
      title: "Biotech / Pharma Summer Internships",
      blurb: "Summer roles at biotech and pharmaceutical companies. Listings whose posting doesn't name a term are included too.",
      test: (l) => l.sector === "Biotech/Pharma" && (!(l.seasons || []).length || l.seasons.includes("Summer")),
    },
    aerospace: {
      title: "Aerospace & Defense Internships",
      blurb: "Many of these roles require US citizenship or fall under ITAR/export-control rules; check the badges.",
      test: (l) => l.sector === "Aerospace & Defense",
    },
    reu: {
      title: "Research Experiences for Undergraduates (REUs) & summer research",
      blurb: "Funded summer research at universities and institutes, usually with a stipend and often housing.",
      test: (l) => l.opportunity_type === "REU",
    },
    "gov-labs": {
      title: "National Labs & Government Programs",
      blurb: "DOE national laboratories, NASA, NIH and other federal programs.",
      test: (l) => ["National Lab", "Government Program"].includes(l.opportunity_type),
    },
  };

  const state = {
    data: null,
    listings: [],
    byId: new Map(),
    view: null,
    sort: { key: "deadline", dir: 1 },
    shown: CFG.pageSize,
    openRow: null,
  };

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------
  const daysAgo = (iso) => (iso ? (Date.now() - new Date(iso).getTime()) / DAY : Infinity);
  const daysUntil = (isoDate) => {
    if (!isoDate) return Infinity;
    const d = new Date(isoDate + "T23:59:59");
    return (d.getTime() - Date.now()) / DAY;
  };
  const fmtDate = (isoDate) => {
    if (!isoDate) return "";
    const d = new Date(isoDate.length <= 10 ? isoDate + "T12:00:00" : isoDate);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };
  const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "never");
  const isNew = (l) => daysAgo(l.first_seen) <= CFG.newBadgeDays;
  const isSoon = (l) => { const d = daysUntil(l.deadline); return d >= 0 && d <= CFG.deadlineSoonDays; };
  const locationText = (l) => {
    if (l.remote === "Remote") return "Remote";
    const locs = l.locations || [];
    const s = locs.slice(0, 2).join("; ") + (locs.length > 2 ? ` +${locs.length - 2}` : "");
    return s + (l.remote === "Hybrid" ? " (hybrid)" : "");
  };
  const deadlineText = (l) => (l.deadline ? fmtDate(l.deadline) : l.rolling ? "Rolling" : "");

  // ------------------------------------------------------------------
  // Data loading
  // ------------------------------------------------------------------
  async function load() {
    try {
      const res = await fetch(CFG.listingsUrl, { cache: "no-cache" });
      state.data = await res.json();
    } catch (e) {
      state.data = { listings: [], generated_at: null };
      $("#listings-body").innerHTML = `<tr><td colspan="9" class="empty">Couldn't load listings (${esc(e.message)}).</td></tr>`;
    }
    state.listings = state.data.listings || [];
    state.listings.forEach((l) => state.byId.set(l.id, l));
    buildFilterOptions();
    restoreFilters();
    route();
    document.dispatchEvent(new CustomEvent("board:loaded"));
  }

  // ------------------------------------------------------------------
  // Filters
  // ------------------------------------------------------------------
  const FILTERS = ["q", "major", "type", "season", "size", "state", "year", "paid", "housing", "nocitizen"];
  const el = (id) => $("#f-" + id);

  function fillSelect(id, label, values) {
    const s = el(id);
    s.innerHTML = `<option value="">Any ${esc(label)}</option>` +
      values.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  }
  function buildFilterOptions() {
    const uniq = (fn) => [...new Set(state.listings.flatMap(fn).filter(Boolean))].sort();
    fillSelect("major", "major", uniq((l) => l.majors || []));
    fillSelect("type", "type", uniq((l) => [l.opportunity_type]));
    fillSelect("season", "season", uniq((l) => l.seasons || []));
    fillSelect("size", "size", uniq((l) => [l.org_size]));
    fillSelect("state", "state", uniq((l) => l.states || []));
    fillSelect("year", "class year", ["Freshman", "Sophomore", "Junior", "Senior", "Graduate student", "Recent graduate", "Community college"]);
  }
  function readFilters() {
    const f = {};
    FILTERS.forEach((k) => { const e = el(k); f[k] = e.type === "checkbox" ? e.checked : e.value.trim(); });
    return f;
  }
  function saveFilters() {
    try { localStorage.setItem("stemBoard.filters", JSON.stringify(readFilters())); } catch (_) {}
  }
  function restoreFilters() {
    let f = {};
    try { f = JSON.parse(localStorage.getItem("stemBoard.filters") || "{}"); } catch (_) {}
    FILTERS.forEach((k) => {
      const e = el(k);
      if (!(k in f)) return;
      if (e.type === "checkbox") e.checked = !!f[k];
      else if (e.tagName !== "SELECT" || [...e.options].some((o) => o.value === f[k])) e.value = f[k];
    });
  }

  function matches(l, f) {
    if (state.view && !VIEWS[state.view].test(l)) return false;
    if (f.major && !(l.majors || []).includes(f.major)) return false;
    if (f.type && l.opportunity_type !== f.type) return false;
    if (f.season && !(l.seasons || []).includes(f.season)) return false;
    if (f.size && l.org_size !== f.size) return false;
    if (f.state && !(l.states || []).includes(f.state)) return false;
    // Class year: keep listings that list the year, or don't state any years.
    if (f.year && (l.class_years || []).length && !l.class_years.includes(f.year)) return false;
    if (f.paid && l.paid !== true) return false;
    if (f.housing && l.housing !== true) return false;
    if (f.nocitizen && (l.citizenship && l.citizenship !== "International students eligible")) return false;
    if (f.nocitizen && l.export_control) return false;
    if (f.q) {
      const hay = [l.organization, l.title, l.summary, (l.locations || []).join(" "), (l.majors || []).join(" ")]
        .join(" ").toLowerCase();
      if (!f.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
    }
    return true;
  }

  // ------------------------------------------------------------------
  // Sorting
  // ------------------------------------------------------------------
  function sortValue(l, key) {
    switch (key) {
      case "deadline": return l.deadline || (l.rolling ? "9998" : "9999");
      case "seasons": return (l.seasons || []).join(",") || "~";
      case "location": return locationText(l).toLowerCase() || "~";
      case "pay": { const m = (l.pay || "").replace(/,/g, "").match(/\d+(\.\d+)?/); return m ? -parseFloat(m[0]) : Infinity; }
      default: return (l[key] || "~").toString().toLowerCase();
    }
  }
  function sortList(list) {
    const { key, dir } = state.sort;
    return list.sort((a, b) => {
      const va = sortValue(a, key), vb = sortValue(b, key);
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  function renderStats(list) {
    const active = state.listings;
    const orgs = new Set(active.map((l) => l.organization)).size;
    const today = active.filter((l) => daysAgo(l.first_seen) <= 1).length;
    const week = active.filter((l) => daysAgo(l.first_seen) <= 7).length;
    const updated = state.data.last_collect || state.data.generated_at;
    const stat = (num, lbl) => `<div class="stat"><div class="num">${esc(num)}</div><div class="lbl">${esc(lbl)}</div></div>`;
    $("#stats").innerHTML =
      stat(active.length.toLocaleString(), "open roles") +
      stat(orgs.toLocaleString(), `organizations (${state.data.sources_monitored || "?"} sources monitored)`) +
      stat(today, "new today") +
      stat(week, "new this week") +
      `<div class="stat"><div class="num" style="font-size:15px">${esc(fmtDateTime(updated))}</div><div class="lbl">last updated · links checked ${esc(fmtDateTime(state.data.last_verify))}</div></div>`;
    $("#footer-updated").textContent = updated ? ` Last updated ${fmtDateTime(updated)}.` : "";
  }

  function badges(l) {
    let b = "";
    if (isNew(l)) b += `<span class="badge new">New</span>`;
    if (l.citizenship && l.citizenship !== "International students eligible") b += `<span class="badge cit" title="${esc(l.citizenship)}">US citizen${l.citizenship.includes("permanent") ? "/PR" : ""}</span>`;
    if (l.export_control) b += `<span class="badge cit" title="ITAR / export control or clearance mentioned">ITAR/Export</span>`;
    if (l.citizenship === "International students eligible") b += `<span class="badge info">Intl OK</span>`;
    if (l.housing) b += `<span class="badge info">Housing</span>`;
    if (l.letters_required) b += `<span class="badge info">${esc(l.letters_required)} letter${l.letters_required > 1 ? "s" : ""}</span>`;
    return b;
  }

  function rowHtml(l) {
    const saved = window.Tracker && window.Tracker.isSaved(l.id);
    const soon = isSoon(l);
    return `<tr class="row${soon ? " soon" : ""}" data-id="${esc(l.id)}">
      <td><button class="save-btn${saved ? " saved" : ""}" data-save="${esc(l.id)}" title="${saved ? "Saved to My Applications" : "Save to My Applications"}" aria-label="Save">${saved ? "★" : "☆"}</button></td>
      <td><div class="org">${esc(l.organization)}</div><div class="sub">${esc(l.org_size || "")}</div></td>
      <td><a href="${esc(safeUrl(l.apply_url))}" target="_blank" rel="noopener">${esc(l.title)}</a>
          <div class="sub">${esc((l.majors || []).slice(0, 4).join(", "))}${(l.majors || []).length > 4 ? "…" : ""}</div></td>
      <td>${esc(l.opportunity_type || "")}</td>
      <td>${esc((l.seasons || []).join(", "))}</td>
      <td>${esc(locationText(l))}</td>
      <td>${esc(l.pay || "")}</td>
      <td class="deadline">${esc(deadlineText(l))}${soon ? ` <span class="badge soon">${Math.ceil(daysUntil(l.deadline))}d left</span>` : ""}</td>
      <td>${badges(l)}</td>
    </tr>`;
  }

  function detailHtml(l) {
    const item = (k, v) => (v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length)) ? "" :
      `<div><b>${esc(k)}:</b> ${esc(Array.isArray(v) ? v.join(", ") : v)}</div>`;
    const yn = (v) => (v === true ? "Yes" : v === false ? "No" : null);
    const links = (l.links || []).map((x) => `<a href="${esc(safeUrl(x.url))}" target="_blank" rel="noopener">${esc(x.source)}</a>`).join(" · ");
    return `<tr class="detail"><td></td><td colspan="8">
      ${l.summary ? `<p style="margin:0 0 8px">${esc(l.summary)}</p>` : ""}
      <div class="detail-grid">
        ${item("Majors", l.majors)}${item("Class years", l.class_years)}${item("Minimum GPA", l.min_gpa)}
        ${item("Citizenship", l.citizenship)}${item("Export control / ITAR", yn(l.export_control))}
        ${item("Pay / stipend", l.pay)}${item("Housing provided", yn(l.housing))}
        ${item("Recommendation letters", l.letters_required)}${item("Opens", fmtDate(l.open_date))}
        ${item("Deadline", deadlineText(l))}${item("Locations", l.locations)}${item("Sector", l.sector)}
        ${item("First seen", fmtDate(l.first_seen))}${item("Last verified", fmtDateTime(l.last_verified))}
      </div>
      <div class="sub" style="margin-top:8px">Found via: ${links || esc((l.sources || []).join(", "))}</div>
    </td></tr>`;
  }

  function renderTable() {
    const f = readFilters();
    const list = sortList(state.listings.filter((l) => matches(l, f)));
    const shown = list.slice(0, state.shown);
    $("#result-count").textContent = `${list.length.toLocaleString()} listing${list.length === 1 ? "" : "s"}`;
    $("#listings-body").innerHTML = shown.length
      ? shown.map((l) => rowHtml(l) + (state.openRow === l.id ? detailHtml(l) : "")).join("")
      : `<tr><td colspan="9" class="empty">No listings match these filters.</td></tr>`;
    $("#show-more").hidden = list.length <= state.shown;
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      th.classList.toggle("sorted-asc", th.dataset.sort === state.sort.key && state.sort.dir === 1);
      th.classList.toggle("sorted-desc", th.dataset.sort === state.sort.key && state.sort.dir === -1);
    });
  }

  // ------------------------------------------------------------------
  // Routing (#/board, #/view/<key>, #/tracker, #/request)
  // ------------------------------------------------------------------
  function route() {
    const hash = location.hash.replace(/^#\/?/, "") || "board";
    const [page, arg] = hash.split("/");
    document.querySelectorAll(".page").forEach((p) => (p.hidden = true));
    document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.route === hash));
    if (page === "tracker") { $("#page-tracker").hidden = false; window.Tracker && window.Tracker.render(); return; }
    if (page === "request") { $("#page-request").hidden = false; return; }
    state.view = page === "view" && VIEWS[arg] ? arg : null;
    const intro = $("#view-intro");
    intro.hidden = !state.view;
    if (state.view) intro.innerHTML = `<h1>${esc(VIEWS[state.view].title)}</h1><p>${esc(VIEWS[state.view].blurb)}</p>`;
    $("#page-board").hidden = false;
    state.shown = CFG.pageSize;
    renderStats();
    renderTable();
  }

  // ------------------------------------------------------------------
  // Events
  // ------------------------------------------------------------------
  function bind() {
    window.addEventListener("hashchange", route);
    FILTERS.forEach((k) => el(k).addEventListener(el(k).type === "search" ? "input" : "change", () => {
      state.shown = CFG.pageSize; saveFilters(); renderTable();
    }));
    $("#reset-filters").addEventListener("click", () => {
      FILTERS.forEach((k) => { const e = el(k); if (e.type === "checkbox") e.checked = false; else e.value = ""; });
      saveFilters(); renderTable();
    });
    $("#toggle-filters").addEventListener("click", () => $("#filters").classList.toggle("open"));
    $("#show-more").addEventListener("click", () => { state.shown += CFG.pageSize; renderTable(); });
    document.querySelectorAll("th[data-sort]").forEach((th) => th.addEventListener("click", () => {
      const key = th.dataset.sort;
      state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
      renderTable();
    }));
    $("#listings-body").addEventListener("click", (e) => {
      const save = e.target.closest("[data-save]");
      if (save) { window.Tracker.toggleSave(state.byId.get(save.dataset.save)); renderTable(); return; }
      if (e.target.closest("a")) return;
      const row = e.target.closest("tr.row");
      if (row) { state.openRow = state.openRow === row.dataset.id ? null : row.dataset.id; renderTable(); }
    });
    $("#request-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      if (!CFG.githubRepo || CFG.githubRepo.startsWith("YOUR-USER")) {
        $("#request-hint").textContent = "The site owner hasn't set githubRepo in config.js yet, so requests can't be sent.";
        return;
      }
      const body = `Company: ${fd.get("company")}\nCareers page: ${fd.get("careers_url") || "-"}\nSector: ${fd.get("sector")}\nNotes: ${fd.get("notes") || "-"}\n\n(Submitted from the STEM Opportunity Board. The collection agent processes this automatically.)`;
      const url = `https://github.com/${CFG.githubRepo}/issues/new?labels=company-request&title=${encodeURIComponent("Company request: " + fd.get("company"))}&body=${encodeURIComponent(body)}`;
      window.open(url, "_blank", "noopener");
      $("#request-hint").textContent = "Thanks! Finish submitting in the GitHub tab that just opened.";
      e.target.reset();
    });
  }

  // Public API for tracker.js
  window.Board = {
    get listings() { return state.listings; },
    byId: (id) => state.byId.get(id),
    refresh: () => { if (!$("#page-board").hidden) renderTable(); },
    esc, safeUrl, fmtDate, daysUntil,
  };

  bind();
  load();
})();
