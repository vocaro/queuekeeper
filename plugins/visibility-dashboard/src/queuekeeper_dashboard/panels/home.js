QK.panel("home", {
  render(element, data, context) {
    const h = QK.h;
    const running = typeof data.began === "number" ? " Running " + QK.since(data.began) + "." : "";
    QK.fill(element,
      h("div", { class: "card hero" },
        h("h1", null, h("span", { class: "dot " + data.level }), data.headline),
        h("p", { class: "wrap" }, data.detail + running),
        QK.overview(data.counts, data.trend)),
      data.lines.length ? h("div", { class: "card" }, h("ul", { class: "rows lines" }, data.lines.map(line =>
        h("li", null, h("a", { class: "row", href: QK.href(context.repository, line.panel) },
          h("b", { class: line.level === "plain" ? null : line.level }, line.title),
          h("span", { class: "ellipsis" + (line.level === "plain" ? " muted" : "") }, line.text),
          h("span", { class: "muted" }, (line.at ? QK.ago(line.at) + " " : "") + "›")))))) : null);
  },
});
