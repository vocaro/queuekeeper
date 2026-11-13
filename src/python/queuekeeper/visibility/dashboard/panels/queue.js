QK.panel("queue", {
  render(element, data, context) {
    const h = QK.h, log = name => name ? h("a", { href: QK.href(context.repository, "logs", { name }) }, "log") : null;
    const short = commit => commit ? String(commit).slice(0, 12) : "";
    function entry(found) {
      const settled = typeof found.settled === "number";
      const facts = [found.id, found.branch, found.commits > 1 ? QK.count(found.commits, "commit") : null, found.note,
        found.front ? "front of the queue" : null, found.noGate ? "no gate" : null,
        found.flakes ? QK.count(found.flakes, "flaky test") : null, found.ungated ? "landed ungated" : null]
        .filter(Boolean).join(" · ");
      return h("li", null,
        h("span", { class: "pill " + found.state }, found.state),
        h("span", null,
          h("div", { class: "ellipsis" }, found.subject || found.id,
            found.landedAs ? h("span", { class: "muted mono" }, " → " + short(found.landedAs)) : null),
          h("div", { class: "sub mono wrap" }, facts),
          found.why ? h("div", { class: "sub wrap" }, found.why, " ", log(found.log)) : null),
        h("span", { class: "muted" }, settled ? QK.ago(found.settled) : QK.since(found.submitted)));
    }
    function gate(found) {
      return h("div", { class: "gate" },
        h("div", { class: "top" }, h("span", null, found.role), h("span", { class: "muted" }, QK.since(found.began))),
        h("div", { class: "sub" }, found.lane + " lane on " + (found.mac || "no Mac yet")),
        found.phase ? h("div", { class: "sub" }, "Phase: " + found.phase) : null,
        found.waiting ? h("div", { class: "sub wrap" }, "Waiting: " + found.waiting
          + (found.waitingSince ? ", for " + QK.since(found.waitingSince) : "")) : null,
        found.stuck ? h("div", { class: "sub warning wrap" }, found.stuck) : null,
        h("div", { class: "sub mono wrap" }, (found.entries || []).join(", "), " ", log(found.log)));
    }
    const round = data.round;
    QK.fill(element,
      data.held ? h("div", { class: "banner danger" }, h("b", null, "Held "), data.held) : null,
      !data.held && !data.worker && data.waiting.length ? h("div", { class: "banner warning" },
        h("b", null, "No worker is running. "), "qk queue resume starts one.") : null,
      round ? h("div", { class: "card" },
        h("h2", null, "Round " + round.name, h("span", { class: "muted" },
          QK.count(round.entries.length, "entry", "entries") + (round.base ? " on " + short(round.base) : ""))),
        round.bisect ? h("p", { class: "sub" }, "Bisecting " + QK.count(round.bisect.suspects || 0, "entry", "entries")
          + ": older half " + (round.bisect.older || 0) + ", newer half " + (round.bisect.newer || "waiting for the older")) : null,
        round.gates.length ? h("div", { class: "gates" }, round.gates.map(gate))
          : h("p", { class: "sub" }, "No gate is running yet.")) : null,
      h("div", { class: "card" }, h("h2", null, "Waiting", h("span", { class: "muted" }, "Oldest first")),
        data.waiting.length ? h("ul", { class: "rows entries" }, data.waiting.map(entry))
          : h("p", { class: "sub" }, "Nothing is waiting.")),
      data.recent.length ? h("div", { class: "card" }, h("h2", null, "Recent", h("span", { class: "muted" }, "Newest first")),
        h("ul", { class: "rows entries" }, data.recent.map(entry))) : null);
  },
});
