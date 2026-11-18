QK.panel("about", {
  // Which Queuekeeper this is, its license, and the notices of others' work it uses, each credited as its license asks.
  render(element, data) {
    const h = QK.h, link = (href, text, rel) => h("a", { href, rel: rel || "noopener", target: "_blank" }, text);
    QK.fill(element,
      h("div", { class: "card hero" }, h("h1", null, "About Queuekeeper"),
        h("dl", { class: "facts" },
          h("div", null, h("dt", null, "Version"), h("dd", null, data.version)),
          data.commit ? h("div", null, h("dt", null, "Commit"), h("dd", { class: "mono" }, data.commit.slice(0, 12))) : null,
          h("div", null, h("dt", null, "License"), h("dd", null, link(data.license.url, data.license.name, "noopener license")))),
        h("p", { class: "sub" }, data.copyright + ".")),
      h("div", { class: "card" }, h("h2", null, "Third-party notices", h("span", { class: "muted" }, "Work of others this dashboard uses")),
        h("ul", { class: "rows notices" }, data.notices.map(one => h("li", null,
          h("b", null, one.name),
          h("span", { class: "sub" }, one.used + ". By ", link(one.url, one.by), ", licensed ", link(one.licenseUrl, one.license, "noopener license"), "."))))));
  },
});
