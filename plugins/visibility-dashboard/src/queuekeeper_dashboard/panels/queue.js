QK.panel("queue", {
  render(element, data, context) {
    const h = QK.h, log = name => name ? h("a", { href: QK.href(context.repository, "logs", { name }) }, "log") : null;
    const roleName = role => ({ batch: "Current batch", stacked: "Testing ahead" }[role] || role);
    const facts = rows => h("dl", { class: "facts" }, rows.filter(([, value]) => value !== null && value !== undefined && value !== "")
      .flatMap(([label, value]) => [h("dt", { class: "muted" }, label), h("dd", { class: "wrap" }, value)]));
    const back = () => h("p", null, h("a", { href: QK.href(context.repository, "queue") }, "‹ Back to Queue"));
    if (data.detail) {
      const detail = data.detail;
      if (detail.kind === "missing") return QK.fill(element, back(),
        h("div", { class: "card" }, h("h2", null, "Details unavailable"),
          h("p", null, "This entry could not be found, or this test round has finished."), h("p", { class: "mono wrap" }, detail.id)));
      if (detail.kind === "entry") {
        const one = detail.entry;
        return QK.fill(element, back(),
          h("div", { class: "card hero" }, h("h1", { class: "wrap" }, one.subject || "Queue entry"),
            h("p", { class: "mono wrap" }, one.id)),
          h("div", { class: "card" }, h("h2", null, "Details"), facts([
            ["Status", one.state], ["Branch", one.branch], ["Submitted", QK.clock(one.submitted)],
            ["Settled", QK.clock(one.settled)], ["Commits", one.commits], ["Tip", one.tip], ["Base", one.base],
            ["Landed as", one.landedAs], ["Priority", one.front ? "Front of the queue" : null],
            ["Checks", one.noGate ? "Gate bypass requested" : one.ungated ? "Landed ungated" : null],
            ["Flaky tests", one.flakes], ["Output", log(one.log)]])),
          one.note ? h("div", { class: "card" }, h("h2", null, "Note"), h("p", { class: "wrap" }, one.note)) : null,
          one.why ? h("div", { class: "card" }, h("h2", null, "Outcome"), h("p", { class: "wrap" }, one.why)) : null,
          one.commitList.length ? h("div", { class: "card" }, h("h2", null, "Commits"),
            h("ul", { class: "rows" }, one.commitList.map(commit => h("li", { class: "mono wrap" }, commit)))) : null);
      }
      const one = detail.gate;
      return QK.fill(element, back(),
        h("div", { class: "card hero" }, h("h1", null, roleName(one.role)), h("p", { class: "mono wrap" }, detail.round)),
        h("div", { class: "card" }, h("h2", null, "Test status"), facts([
          ["Status", one.waiting ? "Waiting" : "Running"], ["Lane", one.lane], ["Mac", one.mac],
          ["Elapsed", QK.since(one.began)], ["Phase", one.phase],
          ["Waiting for", one.waiting], ["Waiting since", QK.clock(one.waitingSince)], ["Output", log(one.log)]])),
        one.stuck ? h("div", { class: "banner warning" }, one.stuck) : null,
        one.role === "stacked" ? h("div", { class: "card" }, h("h2", null, "Testing ahead"),
          h("p", null, "New arrivals are tested on top of the current batch. They can land after that batch lands; if its result changes, they are tested again.")) : null,
        h("div", { class: "card" }, h("h2", null, "Entries"), h("ul", { class: "rows entries" },
          detail.entries.map(entry => QK.entry(entry, context, { compact: true })))));
    }
    function gate(found) {
      return h("a", { class: "gate gate-link", href: QK.href(context.repository, "queue", { gate: found.role, round: data.round.name }) },
        h("div", { class: "top" }, h("span", null, roleName(found.role)), h("span", { class: "muted" }, QK.since(found.began), " ›")),
        h("div", { class: "sub ellipsis" }, [found.lane, found.waiting ? "Waiting" : found.phase || "Running", found.mac].filter(Boolean).join(" · ")));
    }
    const round = data.round, overview = data.overview;
    const activeIds = new Set((round ? round.entries : []).concat(round ? round.gates.flatMap(gate => gate.entries || []) : []));
    const active = data.waiting.filter(entry => activeIds.has(entry.id) || ["testing", "passed"].includes(entry.state));
    const pending = data.waiting.filter(entry => !active.includes(entry));
    QK.fill(element,
      h("div", { class: "card hero" },
        h("h1", null, h("span", { class: "dot " + overview.level }), overview.headline),
        h("p", { class: "wrap" }, overview.detail),
        h("div", { class: "numbers" },
          [["In the queue", overview.counts.waiting], ["Landed today", overview.counts.landed], ["Came back today", overview.counts.cameBack]].map(([label, count]) =>
            h("div", { class: "number" }, h("span", { class: "muted" }, label), h("b", null, count))))),
      overview.lines.length ? h("div", { class: "card" }, h("ul", { class: "rows lines" }, overview.lines.map(line =>
        h("li", null, h("a", { class: "row", href: QK.href(context.repository, line.panel === "queue" && line.title === "Came back" ? "history" : line.panel) },
          h("b", { class: line.level === "plain" ? null : line.level }, line.title),
          h("span", { class: "wrap" }, line.text), h("span", { class: "muted" }, "›")))))) : null,
      data.held ? h("div", { class: "banner danger" }, h("b", null, "Held "), data.held) : null,
      !data.held && !data.worker && data.waiting.length ? h("div", { class: "banner warning" },
        h("b", null, "No worker is running. "), "qk queue resume starts one.") : null,
      round ? h("div", { class: "card" },
        h("h2", null, "Current round", h("span", { class: "muted" },
          QK.count(round.entries.length, "entry", "entries"))),
        round.bisect ? h("p", { class: "sub" }, "Bisecting " + QK.count(round.bisect.suspects || 0, "entry", "entries")
          + ": older half " + (round.bisect.older || 0) + ", newer half " + (round.bisect.newer || "waiting for the older")) : null,
        round.gates.length ? h("div", { class: "gates" }, round.gates.map(gate))
          : h("p", { class: "sub" }, "No gate is running yet.")) : null,
      active.length ? h("div", { class: "card" }, h("h2", null, "In progress"),
        h("ul", { class: "rows entries" }, active.map(found => QK.entry(found, context, { compact: true })))) : null,
      h("div", { class: "card" }, h("h2", null, "Up next", h("span", { class: "muted" }, "Oldest first")),
        pending.length ? h("ul", { class: "rows entries" }, pending.map(found => QK.entry(found, context, { compact: true })))
          : h("p", { class: "sub" }, "Nothing is waiting for a round.")));
  },
});
