QK.panel("farm", {
  render(element, data) {
    const h = QK.h;
    if (!data.hosts) {
      return QK.fill(element, h("div", { class: "card" }, h("h2", null, "Farm"),
        h("p", { class: data.error ? "danger wrap" : "muted" }, data.error || "Reading the Macs…")));
    }
    // What admission judges the Mac by. A share at or above its limit opens no further slot.
    function load(found) {
      if (!found) return null;
      if (found.error) return h("p", { class: "sub wrap" }, "Its load could not be read: " + found.error);
      const busy = typeof found.cpuBusy === "number", over = busy && typeof found.busyLimit === "number"
        && found.cpuBusy >= found.busyLimit;
      const facts = [typeof found.load === "number" ? "load " + found.load.toFixed(1) : null,
        found.memory ? "memory " + found.memory : null].filter(Boolean).join(" · ");
      return [
        busy ? h("div", { class: "bar" }, h("i", { class: over ? "over" : null, width: Math.min(100, Math.round(found.cpuBusy)) + "%" })) : null,
        h("div", { class: "sub" + (over || found.memory === "critical" ? " warning" : "") },
          (busy ? "CPU " + Math.round(found.cpuBusy) + "% busy" + (typeof found.busyLimit === "number"
            ? ", limit " + Math.round(found.busyLimit) + "%" : "") : "CPU busy share not read") + (facts ? " · " + facts : "")),
        h("div", { class: "sub" + (found.screen === "locked" ? " warning" : "") },
          found.screen === "locked" ? "Screen locked: lanes that need an unlocked screen wait"
            : found.screen === "unlocked" ? "Screen unlocked" : "Screen state not read"),
      ];
    }
    function host(found) {
      const slots = found.slots, read = found.state === "observed";
      return h("div", { class: "card" },
        h("h2", null, found.name, read ? null : h("span", { class: "warning" }, found.state)),
        read ? [
          h("div", { class: "bar" }, h("i", { width: Math.round(100 * slots.used / Math.max(1, slots.most)) + "%" })),
          h("div", { class: "sub" }, slots.used + " of " + slots.most + " slots in use"
            + (slots.guaranteed ? ", " + slots.guaranteed + " always open" : "")),
        ] : found.reasons.map(reason => h("p", { class: "sub wrap" }, reason)),
        load(found.load),
        found.holders.length ? [h("h2", { class: "sub" }, "Running"), h("ul", { class: "rows pairs" }, found.holders.map(one =>
          h("li", null, h("span", { class: "ellipsis" }, one.label || "unnamed work",
            one.wholeHost ? h("span", { class: "muted" }, " · whole host") : null,
            one.exclusive ? h("span", { class: "muted" }, " · exclusive") : null),
            h("span", { class: "muted" }, QK.span(one.heldSeconds)))))] : null,
        found.waiters.length ? [h("h2", { class: "sub" }, "Waiting"), h("ul", { class: "rows pairs" }, found.waiters.map(one =>
          h("li", null, h("span", null, h("div", { class: "ellipsis" }, one.label || "unnamed work"),
            one.reason ? h("div", { class: "sub wrap" }, one.reason) : null),
            h("span", { class: "muted" }, QK.span(one.waitedSeconds)))))] : null,
        read && !found.holders.length && !found.waiters.length ? h("p", { class: "sub" }, "Nothing is running or waiting.") : null);
    }
    QK.fill(element, h("div", { class: "columns" }, data.hosts.map(host)),
      h("p", { class: "sub" }, "Every project's work on each Mac. Read " + QK.ago(data.readAt) + "."
        + (data.error ? " The last reading failed: " + data.error : "")));
  },
});
