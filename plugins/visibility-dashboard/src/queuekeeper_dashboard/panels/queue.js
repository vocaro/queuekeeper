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
    // A gate of the gate lane has a ring of how far it has gone against that lane's last passing runs.
    function gate(found) {
      const median = data.overview.trend && data.overview.trend.medianGateSeconds;
      const ring = found.lane === "gate" && median && typeof found.began === "number" ? QK.ring(Date.now() / 1000 - found.began, median) : null;
      return h("a", { class: "gate gate-link" + (ring ? " ringed" : ""), href: QK.href(context.repository, "queue", { gate: found.role, round: data.round.name }) },
        ring, h("div", { class: "gate-text" },
          h("div", { class: "top" }, h("span", null, roleName(found.role)), h("span", { class: "muted" }, QK.since(found.began), " ›")),
          h("div", { class: "sub ellipsis" }, [found.lane, found.waiting ? "Waiting" : found.phase || "Running", found.mac].filter(Boolean).join(" · ")),
          ring ? h("div", { class: "sub" }, "Median " + QK.span(median)) : null));
    }
    // The flow map: who has entries in the queue, by branch prefix, then the queue, the round's gates, the Macs they
    // run on and main. Dots travel the edges while the worker runs; the last entry to come back within the hour draws
    // a dashed arc back to whoever sent it. Its colors are app.css's, by class.
    function flowMap(flow, round, waiting, landed) {
      const svg = QK.svg, W = 900, PITCH = 62, NODE = 50, TOP = 28, moving = data.worker && !data.held;
      const X = { agents: [0, 168], queue: [214, 136], round: [396, 176], farm: [618, 176], main: [840, 60] };
      const back = flow.back, gates = round ? round.gates : [], macs = flow.macs || [];
      const agents = flow.agents.slice();
      if (back && !agents.some(one => one.name === back.agent)) agents.push({ name: back.agent, waiting: 0, testing: 0, back: true });
      const rows = Math.max(2, agents.length, gates.length, macs.length), body = rows * PITCH - (PITCH - NODE);
      const height = TOP + body + (back ? 50 : 8), first = count => TOP + (body - (count * PITCH - (PITCH - NODE))) / 2;
      const fit = (value, most) => (value = String(value || "")).length > most ? value.slice(0, most - 1) + "…" : value;
      const text = (x, y, value, kind, anchor) => svg("text", { x, y, class: kind || null, "text-anchor": anchor || null }, value);
      const box = (x, y, width, tall, kind) => svg("rect", { x, y, width, height: tall, rx: 8, class: "node " + (kind || "") });
      const link = (href, tip, ...children) => svg("a", { href }, svg("title", null, tip), ...children);
      const hue = name => "p" + (1 + [...name].reduce((sum, letter) => (sum * 31 + letter.charCodeAt(0)) % 9973, 7) % 5);
      const edges = [], dots = [], nodes = [];
      function edge(x1, y1, x2, y2, kind, live) {
        const middle = (x1 + x2) / 2, name = "flow-edge-" + (edges.length + 1), seconds = 1.6 + edges.length % 3 * 0.4;
        edges.push(svg("path", { id: name, class: "edge " + kind + (live ? " live" : ""), d: `M${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }));
        // Each draw starts its dots where the wall clock puts them, so the page's redraw every two seconds does not jolt them.
        if (live) dots.push(svg("circle", { class: "moving " + kind, r: 3.5 }, svg("animateMotion", { dur: seconds + "s",
          begin: -(Date.now() / 1000 % seconds).toFixed(2) + "s", repeatCount: "indefinite" }, svg("mpath", { href: "#" + name }))));
      }
      nodes.push([["AGENTS", "agents"], ["QUEUE", "queue"], ["ROUND", "round"], ["FARM", "farm"], ["MAIN", "main"]]
        .map(([title, column]) => text(X[column][0], 12, title, "head")));
      const queueY = TOP + body / 2, [qx, qw] = X.queue, [ax, aw] = X.agents, [rx, rw] = X.round, [fx, fw] = X.farm, [mx, mw] = X.main;
      // Agents
      if (!agents.length) nodes.push(box(ax, first(1), aw, NODE, "idle"), text(ax + 12, first(1) + 30, "Nobody has an entry waiting", "sub"));
      agents.forEach((one, index) => {
        const y = first(agents.length) + index * PITCH, kind = one.back ? "danger" : one.more || one.name === "other" ? "" : hue(one.name);
        const said = one.back ? (back.state === "conflict" ? "conflicted " : "came back ") + QK.ago(back.settled)
          : [one.testing ? one.testing + " testing" : null, one.waiting ? one.waiting + " waiting" : null].filter(Boolean).join(" · ");
        const drawn = [box(ax, y, aw, NODE, one.back ? "back" : ""), svg("circle", { cx: ax + 16, cy: y + 18, r: 5, class: "agent " + kind }),
          text(ax + 28, y + 22, fit(one.name, 19), "name"), text(ax + 12, y + 40, said, "sub" + (one.back ? " danger" : ""))];
        nodes.push(one.back ? link(QK.href(context.repository, "queue", { entry: back.id }), back.subject || back.id, drawn) : drawn);
        if (!one.back) edge(ax + aw, y + NODE / 2, qx, queueY, "submit " + kind, moving);
      });
      // The queue
      const testing = gates.length ? round.entries.length : 0;
      nodes.push(box(qx, TOP, qw, body, data.held ? "held" : ""),
        text(qx + qw / 2, queueY - 4, waiting, "big", "middle"), text(qx + qw / 2, queueY + 14, "waiting", "sub", "middle"),
        data.held ? text(qx + qw / 2, queueY + 32, "held", "sub danger", "middle")
          : testing ? text(qx + qw / 2, queueY + 32, testing + " in the round", "sub", "middle") : null);
      // The round's gates
      const gateY = {};
      if (!gates.length) nodes.push(box(rx, first(1), rw, NODE, "idle"), text(rx + 12, first(1) + 22, "No round", "name"),
        text(rx + 12, first(1) + 40, data.worker ? "starts when one arrives" : "no worker", "sub"));
      gates.forEach((one, index) => {
        const y = gateY[one.role] = first(gates.length) + index * PITCH;
        nodes.push(link(QK.href(context.repository, "queue", { gate: one.role, round: round.name }), one.waiting || roleName(one.role),
          box(rx, y, rw, NODE, one.waiting ? "waiting" : "running"), text(rx + 12, y + 22, fit(roleName(one.role), 15), "name"),
          text(rx + rw - 10, y + 22, QK.since(one.began), "sub", "end"),
          text(rx + 12, y + 40, fit([one.lane, one.waiting ? "waiting for a Mac" : one.phase || "running"].filter(Boolean).join(" · "), 26),
            "sub" + (one.waiting ? " warning" : ""))));
        edge(qx + qw, queueY, rx, y + NODE / 2, "test", moving && !one.waiting);
      });
      // The Macs, each with a cell for every slot it can open
      if (!macs.length) nodes.push(box(fx, first(1), fw, NODE, "idle"), text(fx + 12, first(1) + 30, "The farm is not read yet", "sub"));
      macs.forEach((one, index) => {
        const y = first(macs.length) + index * PITCH, read = one.state === "observed", most = Math.min(12, one.most || 0);
        const cells = read ? Array.from({ length: most }, (_, cell) => svg("rect", { x: fx + 12 + cell * 13, y: y + 30, width: 10, height: 10,
          rx: 2.5, class: "cell" + (cell < one.used ? " held" : "") })) : [text(fx + 12, y + 40, one.state, "sub warning")];
        nodes.push(link(QK.href(context.repository, "farm"), one.name, box(fx, y, fw, NODE, one.gates.length ? "running" : ""),
          text(fx + 12, y + 20, fit(String(one.name).split(" (")[0], 16), "name"),
          read ? text(fx + fw - 10, y + 20, one.used + " of " + one.most, "sub", "end") : null, cells));
        one.gates.forEach(role => edge(rx + rw, gateY[role] + NODE / 2, fx, y + NODE / 2, "test", moving && !gates.find(gate => gate.role === role).waiting));
      });
      // Main, where what passes lands
      const mainY = TOP + body / 2 - 35;
      nodes.push(link(QK.href(context.repository, "history"), "Landed today", box(mx, mainY, mw, 70, "main"),
        text(mx + mw / 2, mainY + 18, "main", "name good", "middle"), text(mx + mw / 2, mainY + 46, landed, "big small", "middle"),
        text(mx + mw / 2, mainY + 62, "landed", "sub", "middle")));
      edge(macs.length ? fx + fw : rx + rw, TOP + body / 2, mx, TOP + body / 2, "land", false);
      // What came back, as an arc from the round to whoever sent it
      if (back) {
        // It leaves the round's foot and comes in at the side of the agent's node, so it passes behind no other.
        const index = agents.findIndex(one => one.name === back.agent), from = [rx + rw / 2, TOP + body], to = [ax + aw, first(agents.length) + index * PITCH + NODE - 10];
        edges.push(svg("path", { class: "edge back", d: `M${from[0]} ${from[1]} C ${from[0]} ${height - 22}, ${to[0] + 60} ${height - 22}, ${to[0]} ${to[1]}` }));
        nodes.push(text((from[0] + to[0]) / 2, height - 4, fit((back.state === "conflict" ? "conflicted · " : "came back · ") + (back.subject || back.id), 60), "sub danger", "middle"));
      }
      return svg("svg", { viewBox: `0 0 ${W} ${height}`, role: "group", "aria-label": "Agents' entries flow through the queue and the round's gates on the farm's Macs to main" },
        edges, dots, nodes);
    }
    const round = data.round, overview = data.overview;
    const activeIds = new Set((round ? round.entries : []).concat(round ? round.gates.flatMap(gate => gate.entries || []) : []));
    const active = data.waiting.filter(entry => activeIds.has(entry.id) || ["testing", "passed"].includes(entry.state));
    const pending = data.waiting.filter(entry => !active.includes(entry));
    QK.fill(element,
      h("div", { class: "card hero" },
        h("h1", null, h("span", { class: "dot " + overview.level }), overview.headline),
        h("p", { class: "wrap" }, overview.detail),
        QK.overview(overview.counts, overview.trend)),
      overview.lines.length ? h("div", { class: "card" }, h("ul", { class: "rows lines" }, overview.lines.map(line =>
        h("li", null, h("a", { class: "row", href: QK.href(context.repository, line.panel === "queue" && line.title === "Came back" ? "history" : line.panel) },
          h("b", { class: line.level === "plain" ? null : line.level }, line.title),
          h("span", { class: "wrap" }, line.text), h("span", { class: "muted" }, "›")))))) : null,
      data.held ? h("div", { class: "banner danger" }, h("b", null, "Held "), data.held) : null,
      !data.held && !data.worker && data.waiting.length ? h("div", { class: "banner warning" },
        h("b", null, "No worker is running. "), "qk queue resume starts one.") : null,
      data.flow ? h("div", { class: "card" }, h("h2", null, "Flow", h("span", { class: "muted" }, "Agents by branch prefix")),
        h("div", { class: "flow-map" }, flowMap(data.flow, round, pending.length, overview.counts.landed))) : null,
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
