QK.panel("history", {
  render(element, data, context) {
    const h = QK.h, today = data.today;
    const number = (label, value) => h("div", { class: "number" }, h("span", { class: "muted" }, label), h("b", null, value));
    QK.fill(element,
      h("div", { class: "card" }, h("h2", null, "Today"),
        h("div", { class: "numbers" }, number("Landed", today.landed), number("Came back", today.cameBack),
          number("Flaky tests", today.flaky), number("Rounds", today.rounds),
          number("Median gate", today.medianGateSeconds === null ? "–" : QK.span(today.medianGateSeconds)))),
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
