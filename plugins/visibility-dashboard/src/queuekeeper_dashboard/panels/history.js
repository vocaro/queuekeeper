QK.panel("history", {
  render(element, data, context) {
    const h = QK.h, today = data.today;
    const number = (label, value) => QK.number(label, value);
    // What settled today, as a donut with a key: landed, failed, conflicted or removed from the queue.
    function settled() {
      const parts = [{ label: "Landed", value: today.landed, tone: "good" }, { label: "Failed", value: today.failed || 0, tone: "danger" },
        { label: "Conflict", value: today.conflict || 0, tone: "warning" }, { label: "Removed", value: today.removed || 0, tone: "muted" }];
      const total = parts.reduce((sum, part) => sum + part.value, 0);
      return h("div", { class: "settled" }, QK.donut(parts, total, "settled"),
        h("ul", { class: "keys" }, parts.map(part => h("li", null, h("i", { class: part.tone }), h("span", null, part.label),
          h("b", null, part.value), h("span", { class: "muted" }, total ? Math.round(100 * part.value / total) + "%" : "")))));
    }
    QK.fill(element,
      h("div", { class: "card" }, h("h2", null, "Today"),
        h("div", { class: "today" }, settled(),
          h("div", { class: "numbers" }, number("Flaky tests", today.flaky), number("Rounds", today.rounds),
            number("Median gate", today.medianGateSeconds === null ? "–" : QK.span(today.medianGateSeconds))))),
      data.recent.length ? h("div", { class: "card" }, h("h2", null, "Recent changes", h("span", { class: "muted" }, "Newest first")),
        h("ul", { class: "rows entries" }, data.recent.map(found => QK.entry(found, context)))) : null,
      h("div", { class: "card" }, h("h2", null, "Events", h("span", { class: "muted" },
        data.total > data.events.length ? "The last " + data.events.length + " of " + data.total : "Newest first")),
        data.events.length ? h("ul", { class: "rows events" }, data.events.map(event => h("li", null,
          h("span", { class: "muted mono" }, QK.clock(event.time)),
          h("span", { class: "wrap " + (event.level === "plain" ? "" : event.level) }, event.text))))
          : h("p", { class: "sub" }, "The journal is empty.")));
  },
});
