QK.panel("farm", {
  render(element, data) {
    const h = QK.h;
    if (!data.hosts) {
      return QK.fill(element, h("div", { class: "card" }, h("h2", null, "Farm"),
        h("p", { class: data.error ? "danger wrap" : "muted" }, data.error || "Reading the Macs…")));
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
