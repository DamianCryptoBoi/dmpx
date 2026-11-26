// Small progressive enhancements; every page works without this file.
(function () {
  // Reload pages that show in-flight work, unless the admin is mid-edit.
  const live = document.querySelector("[data-refresh-seconds]");
  if (live) {
    const seconds = Number(live.getAttribute("data-refresh-seconds")) || 10;
    let dirty = false;
    document.addEventListener("input", () => { dirty = true; });
    setInterval(() => {
      const editing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      const open = document.querySelector("details[open] form");
      if (!dirty && !editing && !open) location.reload();
    }, seconds * 1000);
  }

  // Overview counters refresh in place.
  const stats = document.querySelector("[data-status-url]");
  if (stats) {
    setInterval(async () => {
      try {
        const response = await fetch(stats.getAttribute("data-status-url"), { credentials: "same-origin" });
        if (!response.ok) return;
        const data = await response.json();
        for (const el of stats.querySelectorAll("[data-stat]")) {
          const value = data[el.getAttribute("data-stat")];
          if (value !== undefined) el.textContent = value;
        }
      } catch (_) { /* offline; try again next tick */ }
    }, 5000);
  }

  // Render ISO timestamps in the viewer's locale.
  for (const el of document.querySelectorAll("time[datetime]")) {
    const date = new Date(el.getAttribute("datetime"));
    if (!Number.isNaN(date.getTime())) el.textContent = date.toLocaleString();
  }
})();
