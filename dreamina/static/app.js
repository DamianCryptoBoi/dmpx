// Progressive enhancements; every page still works without this file.
(function () {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : n);

  // Reload pages that show in-flight work, unless the admin is busy on them.
  const live = $("[data-refresh-seconds]");
  if (live) {
    const seconds = Number(live.getAttribute("data-refresh-seconds")) || 10;
    let dirty = false;
    document.addEventListener("input", () => { dirty = true; });
    setInterval(() => {
      const editing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      const open = $("details[open] form");
      const playing = $$("video").some((v) => !v.paused);
      if (!dirty && !editing && !open && !playing) location.reload();
    }, seconds * 1000);
  }

  // Overview counters refresh in place.
  const stats = $("[data-status-url]");
  if (stats) {
    setInterval(async () => {
      try {
        const response = await fetch(stats.getAttribute("data-status-url"), { credentials: "same-origin" });
        if (!response.ok) return;
        const data = await response.json();
        for (const el of $$("[data-stat]")) {
          const value = data[el.getAttribute("data-stat")];
          if (value !== undefined) el.textContent = fmt(value);
        }
      } catch (_) { /* offline; try again next tick */ }
    }, 5000);
  }

  // Timestamps: relative ("4 min ago") with the full local time on hover.
  const relative = (date) => {
    const s = (Date.now() - date.getTime()) / 1000;
    if (s < 45) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    if (s < 7 * 86400) return `${Math.round(s / 86400)} d ago`;
    return date.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };
  const paintTimes = () => {
    for (const el of $$("time[datetime]")) {
      const date = new Date(el.getAttribute("datetime"));
      if (Number.isNaN(date.getTime())) continue;
      el.title = date.toLocaleString();
      el.textContent = el.hasAttribute("data-relative") ? relative(date) : date.toLocaleString();
    }
  };
  paintTimes();
  setInterval(paintTimes, 30000);
  for (const el of $$("[data-elapsed-from]")) {
    const ms = new Date(el.getAttribute("data-elapsed-to")) - new Date(el.getAttribute("data-elapsed-from"));
    if (ms > 0) el.textContent = `· took ${ms >= 60000 ? Math.floor(ms / 60000) + " min " : ""}${Math.round((ms % 60000) / 1000)} s`;
  }

  // Absolute links for copying.
  const absolute = (path) => new URL(path, location.origin).toString();
  for (const input of $$("input[data-absolute]")) {
    input.value = absolute(input.getAttribute("data-absolute"));
    input.addEventListener("focus", () => input.select());
  }
  // Copy buttons: a fixed path, another field's value, or a fresh direct
  // link fetched from the dashboard (result links expire).
  const flash = (button, ok) => {
    button.classList.add(ok ? "copied" : "failed");
    const title = button.title;
    button.title = ok ? "Copied" : "Could not get a link";
    setTimeout(() => { button.classList.remove("copied", "failed"); button.title = title; }, 1600);
  };
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy], [data-copy-value], [data-copy-link]");
    if (!button) return;
    let text = null;
    if (button.hasAttribute("data-copy")) text = absolute(button.getAttribute("data-copy"));
    if (button.hasAttribute("data-copy-value")) text = ($(button.getAttribute("data-copy-value")) || {}).value || null;
    if (button.hasAttribute("data-copy-link")) {
      try {
        const response = await fetch(button.getAttribute("data-copy-link"), { credentials: "same-origin" });
        text = response.ok ? (await response.json()).url : null;
      } catch (_) { text = null; }
    }
    if (!text) { flash(button, false); return; }
    try {
      await navigator.clipboard.writeText(text);
      flash(button, true);
    } catch (_) {
      window.prompt("Copy this link", text);
    }
  });

  // Video tiles load only when played: each one streams from upstream.
  document.addEventListener("click", (event) => {
    const tile = event.target.closest("button.tile[data-video]");
    if (!tile) return;
    const video = document.createElement("video");
    video.src = tile.getAttribute("data-video");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    tile.replaceChildren(video);
    tile.removeAttribute("data-video");
    tile.disabled = false;
    tile.style.cursor = "default";
  });

  // Filters apply on change.
  for (const form of $$("form[data-autosubmit]")) {
    form.addEventListener("change", () => form.submit());
  }

  // New-generation page.
  const form = $("#create-form");
  const catalogEl = $("#catalog");
  if (!form || !catalogEl) return;
  const catalog = JSON.parse(catalogEl.textContent);
  const credits = Number(form.getAttribute("data-credits")) || 0;
  const duration = $("#duration");
  const durationOut = $("#duration-out");
  const prompt = $("#prompt");

  const modeValue = () => ($('input[name="mode"]:checked') || {}).value || "unified";
  const model = () => catalog[($('input[name="model"]:checked', form) || {}).value];

  function applyMode() {
    const mode = modeValue();
    for (const block of $$("[data-mode]", form)) {
      const on = block.getAttribute("data-mode") === mode;
      block.hidden = !on;
      // Hidden fields must not be submitted (e.g. both reference kinds).
      for (const field of $$("input, select, textarea", block)) field.disabled = !on;
    }
    update();
  }

  function applyModel() {
    const spec = model();
    if (!spec) return;
    duration.min = spec.duration_seconds.min;
    duration.max = spec.duration_seconds.max;
    duration.value = Math.min(Math.max(Number(duration.value), spec.duration_seconds.min), spec.duration_seconds.max);
    $("#duration-range").textContent = `${spec.duration_seconds.min}–${spec.duration_seconds.max} s`;
    const allowed = new Set(spec.resolutions);
    const radios = $$('input[name="resolution"]', form);
    for (const radio of radios) radio.disabled = !allowed.has(radio.value);
    const checked = radios.find((r) => r.checked);
    if (!checked || checked.disabled) {
      const fallback = radios.find((r) => r.value === "720p" && !r.disabled) || radios.find((r) => !r.disabled);
      if (fallback) fallback.checked = true;
    }
    const limits = spec.reference_limits;
    $("#ref-limits").textContent = `up to ${limits.images} images · ${limits.audio} audio (${limits.audio_seconds} s each)`;
    prompt.maxLength = limits.prompt_chars;
    update();
  }

  function update() {
    const spec = model();
    durationOut.textContent = `${duration.value} s`;
    const resolution = ($('input[name="resolution"]:checked', form) || {}).value;
    const perSecond = spec && spec.pricing_credits_per_second[resolution];
    const costEl = $("#cost");
    if (!perSecond) {
      costEl.textContent = "—";
      $("#formula").textContent = " ";
      $("#after").textContent = "—";
      return;
    }
    const cost = perSecond * Number(duration.value);
    costEl.textContent = fmt(cost);
    $("#formula").textContent = `${perSecond} credits/s × ${duration.value} s at ${resolution}`;
    const after = credits - cost;
    $("#after").textContent = fmt(after);
    $("#after-line").classList.toggle("low", after < 0);
  }

  // Previews and @-tags for uploads.
  function previews(input, target, kind) {
    target.replaceChildren();
    const picked = input.closest("[data-dropzone]")?.querySelector("[data-picked]");
    if (picked) picked.textContent = input.files.length ? `${input.files.length} selected` : "";
    [...input.files].forEach((file, index) => {
      const item = document.createElement("div");
      item.className = kind === "audio" ? "preview audio" : "preview";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = kind === "frame" ? (input.id === "first-frame" ? "first" : "last") : `@${kind}${index + 1}`;
      item.append(tag);
      if (kind === "audio") {
        const name = document.createElement("span");
        name.textContent = file.name.length > 22 ? file.name.slice(0, 20) + "…" : file.name;
        item.append(name);
      } else {
        const img = document.createElement("img");
        img.alt = "";
        img.src = URL.createObjectURL(file);
        img.onload = () => URL.revokeObjectURL(img.src);
        item.append(img);
      }
      target.append(item);
    });
    renderTokens();
  }

  function renderTokens() {
    const box = $("#tokens");
    box.replaceChildren();
    const add = (count, kind) => {
      for (let i = 1; i <= count; i++) {
        const token = document.createElement("button");
        token.type = "button";
        token.className = "token";
        token.textContent = `@${kind}${i}`;
        token.title = "Insert into the prompt";
        box.append(token);
      }
    };
    add($("#images").files.length, "image");
    add($("#audio").files.length, "audio");
  }

  $("#tokens").addEventListener("click", (event) => {
    const token = event.target.closest(".token");
    if (!token) return;
    const text = token.textContent;
    const start = prompt.selectionStart ?? prompt.value.length;
    const end = prompt.selectionEnd ?? prompt.value.length;
    const before = prompt.value.slice(0, start);
    const pad = before && !/\s$/.test(before) ? " " : "";
    prompt.setRangeText(`${pad}${text} `, start, end, "end");
    prompt.focus();
  });

  $("#images").addEventListener("change", (e) => previews(e.target, $("#image-previews"), "image"));
  $("#audio").addEventListener("change", (e) => previews(e.target, $("#audio-previews"), "audio"));
  $("#first-frame").addEventListener("change", (e) => previews(e.target, $("#first-preview"), "frame"));
  $("#last-frame").addEventListener("change", (e) => previews(e.target, $("#last-preview"), "frame"));

  for (const zone of $$("[data-dropzone]")) {
    const on = () => zone.classList.add("dragover");
    const off = () => zone.classList.remove("dragover");
    zone.addEventListener("dragenter", on);
    zone.addEventListener("dragover", on);
    zone.addEventListener("dragleave", off);
    zone.addEventListener("drop", off);
  }
  for (const radio of $$('input[name="mode"]')) radio.addEventListener("change", applyMode);
  for (const radio of $$('input[name="model"]', form)) radio.addEventListener("change", applyModel);
  form.addEventListener("change", update);
  duration.addEventListener("input", update);
  form.addEventListener("submit", () => {
    const button = $('button[type="submit"]', form);
    button.disabled = true;
    button.textContent = "Submitting…";
  });

  applyMode();
  applyModel();
})();
