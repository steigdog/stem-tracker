/* =====================================================================
   My Applications: a personal tracker stored in this browser
   (localStorage), with JSON export/import for backup.

   Each saved application keeps a *snapshot* of the listing, so it stays
   in the tracker even after the listing is removed from the board.
   ===================================================================== */
(function () {
  "use strict";
  const $ = (sel) => document.querySelector(sel);
  const { esc, safeUrl, fmtDate, daysUntil } = window.Board;
  const KEY = "stemTracker.v1";

  const STAGES = ["Saved", "Letters Requested", "Letters Received", "Applied", "Interview", "Offer", "Rejected", "Waitlisted"];
  const REC_STATUS = ["Not asked", "Asked", "Agreed", "Submitted"];
  const CHECKLIST = [["cv", "CV / résumé"], ["statement", "Personal / research statement"], ["transcript", "Transcript"]];

  let apps = loadApps();
  let open = null;            // id of the expanded application
  let stageFilter = "All";

  // ------------------------------------------------------------------
  // Storage
  // ------------------------------------------------------------------
  function loadApps() {
    try { return JSON.parse(localStorage.getItem(KEY) || "[]"); } catch (_) { return []; }
  }
  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify(apps)); }
    catch (_) { alert("Couldn't save to this browser's storage (private window?). Export a JSON backup to keep your data."); }
    $("#tracker-count").textContent = apps.length;
  }
  const newId = () => "app_" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const now = () => new Date().toISOString();

  function snapshot(l) {
    return {
      organization: l.organization, title: l.title, opportunity_type: l.opportunity_type,
      seasons: l.seasons || [], locations: l.locations || [], pay: l.pay, deadline: l.deadline,
      rolling: l.rolling, apply_url: l.apply_url, letters_required: l.letters_required,
      citizenship: l.citizenship,
    };
  }
  function blankApp(extra) {
    return {
      id: newId(), listingId: null, custom: false, stage: "Saved",
      checklist: { cv: false, statement: false, transcript: false },
      recommenders: [], notes: "", addedAt: now(), updatedAt: now(), ...extra,
    };
  }

  // ------------------------------------------------------------------
  // Public API used by the board
  // ------------------------------------------------------------------
  function isSaved(listingId) { return apps.some((a) => a.listingId === listingId); }
  function toggleSave(listing) {
    if (!listing) return;
    const i = apps.findIndex((a) => a.listingId === listing.id);
    if (i >= 0) {
      const a = apps[i];
      const touched = a.stage !== "Saved" || a.notes || a.recommenders.length;
      if (touched && !confirm("Remove this application from your tracker? Your notes and checklist for it will be deleted.")) return;
      apps.splice(i, 1);
    } else {
      // Pre-fill one recommender row per required letter.
      const recs = Array.from({ length: listing.letters_required || 0 }, () => ({ name: "", status: "Not asked" }));
      apps.push(blankApp({ listingId: listing.id, snapshot: snapshot(listing), recommenders: recs }));
    }
    persist();
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  function deadlineOf(a) { return a.deadline || (a.snapshot && a.snapshot.deadline) || null; }
  function outstandingLetters(a) {
    return a.recommenders.filter((r) => r.status !== "Submitted");
  }

  function renderSummary() {
    const counts = Object.fromEntries(STAGES.map((s) => [s, 0]));
    apps.forEach((a) => counts[a.stage]++);
    // Deadlines only matter until you've applied.
    const upcoming = apps
      .filter((a) => ["Saved", "Letters Requested", "Letters Received"].includes(a.stage))
      .map((a) => ({ a, d: daysUntil(deadlineOf(a)) }))
      .filter((x) => x.d >= 0 && x.d <= 45)
      .sort((x, y) => x.d - y.d).slice(0, 6);
    const letters = apps.flatMap((a) => outstandingLetters(a).map((r) => ({ a, r })))
      .filter((x) => !["Rejected", "Offer"].includes(x.a.stage));

    $("#tracker-summary").innerHTML = `
      <div class="card"><h2>By stage (${apps.length} total)</h2>
        <div class="stage-counts">${STAGES.map((s) => `<span class="stage-chip">${esc(s)} <b>${counts[s]}</b></span>`).join("")}</div>
      </div>
      <div class="card"><h2>Upcoming deadlines</h2>
        ${upcoming.length ? `<ul>${upcoming.map(({ a, d }) =>
          `<li><b>${Math.ceil(d)}d</b> · ${esc(a.snapshot.organization)}: ${esc(a.snapshot.title)}</li>`).join("")}</ul>`
          : `<span class="hint">No deadlines in the next 45 days for un-submitted applications.</span>`}
      </div>
      <div class="card"><h2>Letters outstanding: ${letters.length}</h2>
        ${letters.length ? `<ul>${letters.slice(0, 6).map(({ a, r }) =>
          `<li>${esc(r.name || "(unnamed)")} · ${esc(r.status)} · ${esc(a.snapshot.organization)}</li>`).join("")}</ul>`
          : `<span class="hint">None.</span>`}
      </div>`;
  }

  function appHtml(a) {
    const s = a.snapshot || {};
    const onBoard = a.listingId && window.Board.byId(a.listingId);
    const dl = deadlineOf(a);
    const d = daysUntil(dl);
    const done = CHECKLIST.filter(([k]) => a.checklist[k]).length + a.recommenders.filter((r) => r.status === "Submitted").length;
    const total = CHECKLIST.length + a.recommenders.length;
    const isOpen = open === a.id;
    return `<div class="app" data-app="${esc(a.id)}">
      <div class="app-head" data-toggle>
        <div>
          <div class="org">${esc(s.organization)} ${a.custom ? `<span class="badge info">Custom</span>` : ""}
            ${a.listingId && !onBoard ? `<span class="badge gone" title="This listing is no longer on the board (posting closed or deadline passed). Your entry is kept.">Removed from board</span>` : ""}</div>
          <div>${s.apply_url ? `<a href="${esc(safeUrl(s.apply_url))}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}</div>
          <div class="sub">${dl ? `Deadline ${esc(fmtDate(dl))}${d >= 0 && d <= 14 ? ` <span class="badge soon">${Math.ceil(d)}d left</span>` : ""}` : s.rolling ? "Rolling deadline" : "No deadline set"}
            · <span class="progress">${done}/${total} materials</span></div>
        </div>
        <select data-field="stage" aria-label="Stage">${STAGES.map((st) => `<option${st === a.stage ? " selected" : ""}>${esc(st)}</option>`).join("")}</select>
        <span class="sub">${isOpen ? "▴" : "▾"}</span>
      </div>
      ${isOpen ? `<div class="app-body">
        <div>
          <h3>Checklist</h3>
          <div class="checklist">${CHECKLIST.map(([k, label]) =>
            `<label><input type="checkbox" data-check="${k}"${a.checklist[k] ? " checked" : ""}> ${esc(label)}</label>`).join("")}</div>
          <h3 style="margin-top:12px">Recommenders</h3>
          ${a.recommenders.map((r, i) => `<div class="rec-row">
              <input data-rec-name="${i}" value="${esc(r.name)}" placeholder="Name">
              <select data-rec-status="${i}">${REC_STATUS.map((st) => `<option${st === r.status ? " selected" : ""}>${esc(st)}</option>`).join("")}</select>
              <button class="btn small ghost" data-rec-del="${i}" type="button" aria-label="Remove recommender">✕</button>
            </div>`).join("")}
          <button class="btn small ghost" data-rec-add type="button">+ Add recommender</button>
        </div>
        <div>
          <h3>Deadline</h3>
          <input type="date" data-field="deadline" value="${esc(dl || "")}">
          <h3 style="margin-top:12px">Notes</h3>
          <textarea data-field="notes" rows="6" placeholder="Contacts, essay prompts, interview notes…">${esc(a.notes)}</textarea>
          <div style="margin-top:10px"><button class="btn small danger" data-delete type="button">Delete from tracker</button></div>
        </div>
      </div>` : ""}
    </div>`;
  }

  function render() {
    $("#tracker-count").textContent = apps.length;
    if ($("#page-tracker").hidden) return;
    renderSummary();
    $("#stage-filter").innerHTML = ["All", ...STAGES].map((s) =>
      `<button type="button" data-stage="${esc(s)}" class="${s === stageFilter ? "active" : ""}">${esc(s)}</button>`).join("");
    const list = apps
      .filter((a) => stageFilter === "All" || a.stage === stageFilter)
      .sort((x, y) => (deadlineOf(x) || "9999").localeCompare(deadlineOf(y) || "9999"));
    $("#tracker-list").innerHTML = list.length ? list.map(appHtml).join("")
      : `<div class="card empty">Nothing here yet. Click the ☆ next to any listing on the board to save it, or add a custom entry.</div>`;
  }

  // ------------------------------------------------------------------
  // Events
  // ------------------------------------------------------------------
  const findApp = (node) => apps.find((a) => a.id === node.closest("[data-app]")?.dataset.app);
  function touch(a) { a.updatedAt = now(); persist(); }

  function bind() {
    const list = $("#tracker-list");
    list.addEventListener("click", (e) => {
      const a = findApp(e.target);
      if (!a) return;
      if (e.target.closest("select, input, textarea, a")) return;
      if (e.target.closest("[data-rec-add]")) { a.recommenders.push({ name: "", status: "Not asked" }); touch(a); render(); return; }
      const del = e.target.closest("[data-rec-del]");
      if (del) { a.recommenders.splice(+del.dataset.recDel, 1); touch(a); render(); return; }
      if (e.target.closest("[data-delete]")) {
        if (confirm("Delete this application and its notes?")) { apps = apps.filter((x) => x !== a); persist(); render(); window.Board.refresh(); }
        return;
      }
      if (e.target.closest("[data-toggle]")) { open = open === a.id ? null : a.id; render(); }
    });
    list.addEventListener("change", (e) => {
      const a = findApp(e.target);
      if (!a) return;
      const t = e.target;
      if (t.dataset.field === "stage") a.stage = t.value;
      else if (t.dataset.field === "deadline") a.deadline = t.value || null;
      else if (t.dataset.check) a.checklist[t.dataset.check] = t.checked;
      else if (t.dataset.recStatus !== undefined) a.recommenders[+t.dataset.recStatus].status = t.value;
      else return;
      touch(a); render();
    });
    // Text fields save as you type, without re-rendering (keeps the cursor in place).
    list.addEventListener("input", (e) => {
      const a = findApp(e.target);
      if (!a) return;
      const t = e.target;
      if (t.dataset.field === "notes") a.notes = t.value;
      else if (t.dataset.recName !== undefined) a.recommenders[+t.dataset.recName].name = t.value;
      else return;
      touch(a);
    });
    list.addEventListener("focusout", (e) => { if (e.target.dataset.recName !== undefined) renderSummary(); });

    $("#stage-filter").addEventListener("click", (e) => {
      const b = e.target.closest("[data-stage]");
      if (b) { stageFilter = b.dataset.stage; render(); }
    });

    // Custom entries
    $("#add-custom").addEventListener("click", () => { $("#custom-form").hidden = false; $("#custom-form input").focus(); });
    $("#cancel-custom").addEventListener("click", () => { $("#custom-form").hidden = true; });
    $("#custom-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      apps.push(blankApp({
        custom: true,
        snapshot: { organization: fd.get("organization"), title: fd.get("title"), apply_url: fd.get("apply_url") || null, deadline: fd.get("deadline") || null },
        deadline: fd.get("deadline") || null,
      }));
      persist(); e.target.reset(); e.target.hidden = true; render();
    });

    // Export / import
    $("#export-json").addEventListener("click", () => {
      const blob = new Blob([JSON.stringify({ version: 1, exportedAt: now(), applications: apps }, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `my-applications-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    });
    $("#import-json").addEventListener("change", async (e) => {
      const file = e.target.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        const incoming = Array.isArray(data) ? data : data.applications;
        if (!Array.isArray(incoming)) throw new Error("no applications found in file");
        // Merge: imported entries replace ones with the same id, others are added.
        const byId = new Map(apps.map((a) => [a.id, a]));
        incoming.forEach((a) => { if (a && a.id && a.snapshot) byId.set(a.id, a); });
        apps = [...byId.values()];
        persist(); render(); window.Board.refresh();
        alert(`Imported ${incoming.length} application(s).`);
      } catch (err) {
        alert("Import failed: " + err.message);
      }
      e.target.value = "";
    });
  }

  window.Tracker = { isSaved, toggleSave, render };
  bind();
  document.addEventListener("board:loaded", render);
  $("#tracker-count").textContent = apps.length;
})();
