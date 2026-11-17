(function () {
  "use strict";
  const roleName = role => ({ batch: "Current batch", stacked: "Testing ahead" }[role] || role);
  const hueOf = name => "p" + (1 + [...name].reduce((sum, letter) => (sum * 31 + letter.charCodeAt(0)) % 9973, 7) % 5);
  const key = gate => gate.repository + "\n" + gate.role;

  // The flow map: who has entries in the queues, by branch prefix, then the queues, the rounds' gates, the Macs they
  // run on and main. model is {queues, agents, gates, macs, back, landed, moving}: a queue for each repository shown,
  // each agent with the queues it has entries in, each gate and each Mac's gates with their repository. With one
  // queue it is a column of its own; with more, a node for each, in its repository's hue (hues). Dots travel the
  // edges while a worker runs; the last entry to come back within the hour draws a dashed arc back to whoever sent
  // it. Its colors are app.css's, by class.
  function flowMap(model, context, hues) {
    const svg = QK.svg, W = 900, PITCH = 62, NODE = 50, TOP = 28, moving = model.moving;
    const X = { agents: [0, 168], queue: [214, 136], round: [396, 176], farm: [618, 176], main: [840, 60] };
    const back = model.back, gates = model.gates, macs = model.macs || [], single = model.queues.length === 1;
    // With more than one queue, those with nothing in them and no gate share one node.
    const busy = one => single || one.waiting || one.testing || one.held || gates.some(gate => gate.repository === one.name);
    const quiet = model.queues.filter(one => !busy(one)), queues = model.queues.filter(busy);
    if (quiet.length) queues.push({ name: QK.count(quiet.length, "idle queue"), quiet: quiet.map(one => one.name), waiting: 0, testing: 0 });
    const agents = model.agents.slice();
    if (back && !agents.some(one => one.name === back.agent)) agents.push({ name: back.agent, waiting: 0, testing: 0, back: true, queues: [] });
    const rows = Math.max(2, agents.length, gates.length, macs.length, single ? 0 : queues.length), body = rows * PITCH - (PITCH - NODE);
    const height = TOP + body + (back ? 50 : 8), first = count => TOP + (body - (count * PITCH - (PITCH - NODE))) / 2;
    const fit = (value, most) => (value = String(value || "")).length > most ? value.slice(0, most - 1) + "…" : value;
    const text = (x, y, value, kind, anchor) => svg("text", { x, y, class: kind || null, "text-anchor": anchor || null }, value);
    const box = (x, y, width, tall, kind) => svg("rect", { x, y, width, height: tall, rx: 8, class: "node " + (kind || "") });
    const link = (href, tip, ...children) => svg("a", { href }, svg("title", null, tip), ...children);
    const edges = [], dots = [], nodes = [];
    function edge(x1, y1, x2, y2, kind, live) {
      const middle = (x1 + x2) / 2, name = "flow-edge-" + (edges.length + 1), seconds = 1.6 + edges.length % 3 * 0.4;
      edges.push(svg("path", { id: name, class: "edge " + kind + (live ? " live" : ""), d: `M${x1} ${y1} C ${middle} ${y1}, ${middle} ${y2}, ${x2} ${y2}` }));
      // Each draw starts its dots where the wall clock puts them, so the page's redraw every two seconds does not jolt them.
      if (live) dots.push(svg("circle", { class: "moving " + kind, r: 3.5 }, svg("animateMotion", { dur: seconds + "s",
        begin: -(Date.now() / 1000 % seconds).toFixed(2) + "s", repeatCount: "indefinite" }, svg("mpath", { href: "#" + name }))));
    }
    nodes.push([["AGENTS", "agents"], [single ? "QUEUE" : "QUEUES", "queue"], [single ? "ROUND" : "ROUNDS", "round"], ["FARM", "farm"], ["MAIN", "main"]]
      .map(([title, column]) => text(X[column][0], 12, title, "head")));
    const [qx, qw] = X.queue, [ax, aw] = X.agents, [rx, rw] = X.round, [fx, fw] = X.farm, [mx, mw] = X.main;
    // The queues: one as a column, or a node for each repository
    const queueY = {}, queueOf = {};
    queues.forEach(one => { queueOf[one.name] = one; });
    if (single) {
      const one = queues[0], y = TOP + body / 2;
      queueY[one.name] = y;
      nodes.push(box(qx, TOP, qw, body, one.held ? "held" : ""),
        text(qx + qw / 2, y - 4, one.waiting, "big", "middle"), text(qx + qw / 2, y + 14, "waiting", "sub", "middle"),
        one.held ? text(qx + qw / 2, y + 32, "held", "sub danger", "middle")
          : one.testing ? text(qx + qw / 2, y + 32, one.testing + " in the round", "sub", "middle") : null);
    } else queues.forEach((one, index) => {
      const y = first(queues.length) + index * PITCH;
      queueY[one.name] = y + NODE / 2;
      if (one.quiet) return nodes.push(svg("g", null, svg("title", null, one.quiet.join(", ")), box(qx, y, qw, NODE, "idle"),
        text(qx + 12, y + 22, one.name, "name"), text(qx + 12, y + 40, "nothing waiting", "sub")));
      const said = one.held ? "held" : !one.worker && one.waiting ? one.waiting + " waiting, no worker"
        : [one.waiting + " waiting", one.testing ? one.testing + " testing" : null].filter(Boolean).join(" · ");
      nodes.push(link(QK.href(one.name, "queue"), "Only " + one.name, box(qx, y, qw, NODE, one.held ? "held" : one.testing ? "running" : ""),
        svg("circle", { cx: qx + 14, cy: y + 18, r: 4.5, class: "agent " + (hues[one.name] || "") }),
        text(qx + 24, y + 22, fit(one.name, 14), "name"),
        text(qx + 12, y + 40, fit(said, 20), "sub" + (one.held || (!one.worker && one.waiting) ? " danger" : ""))));
    });
    // Agents, each with an edge to every queue it has entries in
    if (!agents.length) nodes.push(box(ax, first(1), aw, NODE, "idle"), text(ax + 12, first(1) + 30, "Nobody has an entry waiting", "sub"));
    agents.forEach((one, index) => {
      const y = first(agents.length) + index * PITCH, kind = one.back ? "danger" : one.more || one.name === "other" ? "" : hueOf(one.name);
      const said = one.back ? (back.state === "conflict" ? "conflicted " : "came back ") + QK.ago(back.settled)
        : [one.testing ? one.testing + " testing" : null, one.waiting ? one.waiting + " waiting" : null].filter(Boolean).join(" · ");
      const drawn = [box(ax, y, aw, NODE, one.back ? "back" : ""), svg("circle", { cx: ax + 16, cy: y + 18, r: 5, class: "agent " + kind }),
        text(ax + 28, y + 22, fit(one.name, 19), "name"), text(ax + 12, y + 40, said, "sub" + (one.back ? " danger" : ""))];
      nodes.push(one.back ? link(QK.href(back.repository, "queue", { entry: back.id }), back.subject || back.id, drawn) : drawn);
      if (!one.back) (one.queues || []).filter(name => name in queueY).forEach(name =>
        edge(ax + aw, y + NODE / 2, qx, queueY[name], "submit " + kind, moving && queueOf[name].worker && !queueOf[name].held));
    });
    // The rounds' gates
    const gateY = {};
    if (!gates.length) nodes.push(box(rx, first(1), rw, NODE, "idle"), text(rx + 12, first(1) + 22, "No round", "name"),
      text(rx + 12, first(1) + 40, queues.some(one => one.worker) ? "starts when one arrives" : "no worker", "sub"));
    gates.forEach((one, index) => {
      const y = gateY[key(one)] = first(gates.length) + index * PITCH;
      nodes.push(link(QK.href(one.repository, "queue", { gate: one.role, round: one.round }), (single ? "" : one.repository + ": ") + (one.waiting || roleName(one.role)),
        box(rx, y, rw, NODE, one.waiting ? "waiting" : "running"),
        single ? null : svg("circle", { cx: rx + 14, cy: y + 18, r: 4.5, class: "agent " + (hues[one.repository] || "") }),
        text(rx + (single ? 12 : 24), y + 22, fit(single ? roleName(one.role) : one.repository, single ? 15 : 14), "name"),
        text(rx + rw - 10, y + 22, QK.since(one.began), "sub", "end"),
        text(rx + 12, y + 40, fit([single || one.role === "batch" ? null : roleName(one.role), one.lane, one.waiting ? "waiting for a Mac" : one.phase || "running"]
          .filter(Boolean).join(" · "), 26), "sub" + (one.waiting ? " warning" : ""))));
      if (one.repository in queueY) edge(qx + qw, queueY[one.repository], rx, y + NODE / 2, "test", moving && !one.waiting);
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
      one.gates.filter(gate => key(gate) in gateY).forEach(gate => edge(rx + rw, gateY[key(gate)] + NODE / 2, fx, y + NODE / 2, "test",
        moving && !gates.find(found => key(found) === key(gate)).waiting));
    });
    // Main, where what passes lands
    const mainY = TOP + body / 2 - 35;
    nodes.push(link(QK.href(context.repository, "history"), "Landed today", box(mx, mainY, mw, 70, "main"),
      text(mx + mw / 2, mainY + 18, "main", "name good", "middle"), text(mx + mw / 2, mainY + 46, model.landed, "big small", "middle"),
      text(mx + mw / 2, mainY + 62, "landed", "sub", "middle")));
    edge(macs.length ? fx + fw : rx + rw, TOP + body / 2, mx, TOP + body / 2, "land", false);
    // What came back, as an arc from the rounds to whoever sent it
    if (back) {
      // It leaves the rounds' foot and comes in at the side of the agent's node, so it passes behind no other.
      const index = agents.findIndex(one => one.name === back.agent), from = [rx + rw / 2, TOP + body], to = [ax + aw, first(agents.length) + index * PITCH + NODE - 10];
      edges.push(svg("path", { class: "edge back", d: `M${from[0]} ${from[1]} C ${from[0]} ${height - 22}, ${to[0] + 60} ${height - 22}, ${to[0]} ${to[1]}` }));
      nodes.push(text((from[0] + to[0]) / 2, height - 4, fit((back.state === "conflict" ? "conflicted · " : "came back · ")
        + (single ? "" : back.repository + " · ") + (back.subject || back.id), 60), "sub danger", "middle"));
    }
    return svg("svg", { viewBox: `0 0 ${W} ${height}`, role: "group", "aria-label": "Agents' entries flow through the queue"
      + (single ? "" : "s") + " and the gates on the farm's Macs to main" }, edges, dots, nodes);
  }

  // A gate as a tile: a gate of the gate lane has a ring of how far it has gone against that lane's last passing
  // runs in its repository, median their median. hue, where every repository is shown, tags it with its repository.
  function gateTile(found, median, round, context, hue) {
    const h = QK.h;
    const ring = found.lane === "gate" && median && typeof found.began === "number" ? QK.ring(Date.now() / 1000 - found.began, median) : null;
    return h("a", { class: "gate gate-link" + (ring ? " ringed" : ""), href: QK.href(found.repository || context.repository, "queue", { gate: found.role, round }) },
      ring, h("div", { class: "gate-text" },
        h("div", { class: "top" }, h("span", { class: "ellipsis" }, hue ? [h("i", { class: "dot " + hue }), " ", found.repository] : roleName(found.role)),
          h("span", { class: "muted nowrap" }, QK.since(found.began), " ›")),
        h("div", { class: "sub ellipsis" }, [hue ? roleName(found.role) : null, found.lane, found.waiting ? "Waiting" : found.phase || "Running", found.mac]
          .filter(Boolean).join(" · ")),
        ring ? h("div", { class: "sub" }, "Median " + QK.span(median)) : null));
  }

  // Whether the queues are healthy, as `qk queue health` says it, then what they are doing, the forecast and the
  // signs. began is when the earliest running gate began, for the forecast's bar; tab is where a sign goes.
  function verdict(found, said, began, context) {
    const h = QK.h, glyph = { ok: "✓", warning: "!", danger: "✕", unknown: "?", idle: "–" };
    const clock = at => new Date(at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const now = Date.now() / 1000, cast = found.forecast;
    const share = cast && began && cast.clearsAt > began ? Math.min(100, Math.max(2, 100 * (now - began) / (cast.clearsAt - began))) : null;
    return [
      h("div", { class: "status" },
        h("span", { class: "chip " + found.level }, h("span", { class: "dot" }), found.label),
        h("span", { class: "sub wrap" }, said)),
      h("p", { class: "verdict wrap" }, found.sentence),
      cast ? h("div", { class: "forecast" },
        h("span", { class: "sub" }, "Forecast"),
        h("div", { class: "bar" }, share === null ? null : h("i", { class: found.level === "warning" ? "over" : null, width: Math.round(share) + "%" })),
        h("span", { class: "sub" }, (found.level === "warning" ? "Clears around " : "Clears about ") + clock(cast.clearsAt))) : null,
      h("div", { class: "signs" }, found.signs.map(one =>
        h("a", { class: "sign " + one.level, href: QK.href(context.repository, one.panel), title: one.text },
          h("span", { class: "glyph", "aria-label": one.level }, glyph[one.level]),
          h("b", null, one.title), h("span", { class: "sub" }, one.short || one.text)))),
    ];
  }

  // Every repository's queue at once: their health, the day's numbers, what needs the owner, a row for each
  // repository, the flow map, every round being tested, and every entry in progress and up next, oldest first.
  function everywhere(element, data, context) {
    const h = QK.h, hues = QK.repositoryHues(data.repositories.map(one => one.name)), rows = data.repositories;
    const began = data.rounds.flatMap(round => round.gates).map(gate => gate.began).filter(at => typeof at === "number");
    const said = [QK.count(rows.length, "repository", "repositories"), data.counts.testing ? data.counts.testing + " testing" : null,
      data.counts.waiting + " in the queues"].filter(Boolean).join(" · ");
    const trend = data.trend || {}, hourly = "per hour, last 24 h";
    const testing = rows.filter(one => one.gates);
    // What needs a look first, then what is busy, in the listed order; quiet repositories fold into one line of tags.
    const rank = one => one.error || one.held || ["danger", "warning", "unknown"].includes(one.health && one.health.level) ? 0
      : one.testing || one.counts.waiting ? 1 : 2;
    const shown = rows.filter(one => rank(one) < 2 || rows.length <= 4).sort((a, b) => rank(a) - rank(b));
    const folded = rows.filter(one => !shown.includes(one));
    QK.fill(element,
      h("div", { class: "card hero" },
        data.health ? verdict(data.health, said, began.length ? Math.min(...began) : null, context) : h("h1", null, said),
        h("div", { class: "numbers" },
          QK.number("In the queues", data.counts.waiting, QK.spark(trend.waiting, { title: "Entries waiting at the end of each hour, last 24 hours" }), "last 24 h"),
          QK.number("Landed today", data.counts.landed, QK.spark(trend.landed, { bars: true, tone: "good", title: "Entries landed each hour, last 24 hours" }), hourly),
          QK.number("Came back today", data.counts.cameBack, QK.spark(trend.cameBack, { bars: true, tone: "danger", floor: 3,
            title: "Entries that came back each hour, last 24 hours" }), hourly),
          QK.number("Testing now", data.counts.testing, null, null),
        )),
      data.lines.length ? h("div", { class: "card" }, h("ul", { class: "rows lines tagged" }, data.lines.map(line =>
        h("li", null, h("a", { class: "row", href: QK.href(line.repository || null,
          line.panel === "queue" && line.title === "Came back" ? "history" : line.panel) },
          h("b", { class: line.level === "plain" ? null : line.level }, line.title),
          h("span", { class: "wrap" }, line.repository ? h("span", { class: "tag" }, h("i", { class: "dot " + (hues[line.repository] || "other") }), line.repository) : null, line.text),
          h("span", { class: "muted" }, "›")))))) : null,
      h("div", { class: "card" }, h("h2", null, "Repositories", h("span", { class: "muted" }, "Choose one to see it alone")),
        h("ul", { class: "rows repos" }, shown.map(one => h("li", null, h("a", { class: "row", href: QK.href(one.name, "queue") },
          h("span", { class: "repository-label" }, h("i", { class: "dot " + (hues[one.name] || "other") }), h("b", null, one.name)),
          h("span", { class: "chip " + (one.error ? "warning" : (one.health && one.health.level) || one.level) }, h("span", { class: "dot" }),
            one.error ? "Can't read" : (one.health && one.health.label) || one.headline),
          h("span", { class: "ellipsis" }, one.error ? h("span", { class: "muted" }, one.error)
            : [one.headline, h("span", { class: "muted" }, " " + one.detail)]),
          one.error ? h("span") : h("span", { class: "repo-numbers sub" },
            h("span", { title: "Waiting" }, one.counts.waiting + " waiting"),
            h("span", { title: "Landed today" }, one.counts.landed + " landed"),
            one.counts.cameBack ? h("span", { class: "danger", title: "Came back today" }, one.counts.cameBack + " back") : null),
          one.error ? h("span") : QK.spark(one.landedTrend, { bars: true, tone: "good", title: "Entries " + one.name + " landed each hour, last 24 hours" }) || h("span"),
          h("span", { class: "muted" }, "›")))),
          folded.length ? h("li", { class: "quiet" }, h("span", { class: "sub" }, QK.count(folded.length, "quiet repository", "quiet repositories")),
            h("span", { class: "quiet-tags" }, folded.map(one => QK.tag(one.name, hues[one.name],
              one.counts.landed ? "✓ " + one.counts.landed : null, one.counts.landed ? one.name + ": " + one.counts.landed + " landed today" : null)))) : null)),
      h("div", { class: "card" }, h("h2", null, "Flow", h("span", { class: "muted" }, "Agents by branch prefix, across every repository")),
        h("div", { class: "flow-map" }, flowMap({
          queues: data.flow.queues, agents: data.flow.agents, macs: data.flow.macs, back: data.flow.back,
          gates: data.rounds.flatMap(round => round.gates.map(gate => ({ ...gate, round: round.name }))),
          landed: data.counts.landed, moving: data.worker }, context, hues))),
      testing.length ? h("div", { class: "card" }, h("h2", null, "Testing now", h("span", { class: "muted" },
          QK.count(data.rounds.length, "round") + " · " + QK.count(data.counts.testing, "entry", "entries"))),
        data.rounds.filter(round => round.bisect).map(round => h("p", { class: "sub" }, round.repository + ": bisecting "
          + QK.count(round.bisect.suspects || 0, "entry", "entries") + ": older half " + (round.bisect.older || 0) + ", newer half "
          + (round.bisect.newer || "waiting for the older"))),
        h("div", { class: "gates" }, data.rounds.flatMap(round => round.gates.map(gate =>
          gateTile(gate, round.medianGateSeconds, round.name, context, hues[round.repository] || "other"))))) : null,
      data.active.length ? h("div", { class: "card" }, h("h2", null, "In progress"),
        h("ul", { class: "rows entries" }, data.active.map(found => QK.entry(found, context, { compact: true, hues })))) : null,
      h("div", { class: "card" }, h("h2", null, "Up next", h("span", { class: "muted" }, "Oldest first, each in its own repository's queue")),
        data.pending.length ? h("ul", { class: "rows entries" }, data.pending.map(found => QK.entry(found, context, { compact: true, hues })))
          : h("p", { class: "sub" }, "Nothing is waiting for a round.")));
  }

QK.panel("queue", {
  render(element, data, context) {
    if (!context.repository) return everywhere(element, data, context);
    const h = QK.h, log = name => name ? h("a", { href: QK.href(context.repository, "logs", { name }) }, "log") : null;
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
    const round = data.round, overview = data.overview;
    const activeIds = new Set((round ? round.entries : []).concat(round ? round.gates.flatMap(gate => gate.entries || []) : []));
    const active = data.waiting.filter(entry => activeIds.has(entry.id) || ["testing", "passed"].includes(entry.state));
    const pending = data.waiting.filter(entry => !active.includes(entry));
    const gate = found => gateTile(found, data.overview.trend && data.overview.trend.medianGateSeconds, data.round.name, context);
    const began = round ? round.gates.filter(one => typeof one.began === "number").map(one => one.began) : [];
    const health = found => verdict(found, overview.headline + ". " + overview.detail, began.length ? Math.min(...began) : null, context);
    QK.fill(element,
      h("div", { class: "card hero" },
        data.health ? health(data.health) : [
          h("h1", null, h("span", { class: "dot " + overview.level }), overview.headline),
          h("p", { class: "wrap" }, overview.detail)],
        QK.overview(overview.counts, overview.trend)),
      overview.lines.length ? h("div", { class: "card" }, h("ul", { class: "rows lines" }, overview.lines.map(line =>
        h("li", null, h("a", { class: "row", href: QK.href(context.repository, line.panel === "queue" && line.title === "Came back" ? "history" : line.panel) },
          h("b", { class: line.level === "plain" ? null : line.level }, line.title),
          h("span", { class: "wrap" }, line.text), h("span", { class: "muted" }, "›")))))) : null,
      data.held ? h("div", { class: "banner danger" }, h("b", null, "Held "), data.held) : null,
      !data.held && !data.worker && data.waiting.length ? h("div", { class: "banner warning" },
        h("b", null, "No worker is running. "), "qk queue resume starts one.") : null,
      data.flow ? h("div", { class: "card" }, h("h2", null, "Flow", h("span", { class: "muted" }, "Agents by branch prefix")),
        h("div", { class: "flow-map" }, flowMap({
          queues: [{ name: context.repository, waiting: pending.length, testing: round && round.gates.length ? round.entries.length : 0,
            held: !!data.held, worker: data.worker }],
          agents: data.flow.agents.map(one => ({ ...one, queues: [context.repository] })),
          gates: (round ? round.gates : []).map(one => ({ ...one, repository: context.repository, round: round.name })),
          macs: (data.flow.macs || []).map(one => ({ ...one, gates: one.gates.map(role => ({ repository: context.repository, role })) })),
          back: data.flow.back ? { ...data.flow.back, repository: context.repository } : null,
          landed: overview.counts.landed, moving: data.worker && !data.held }, context))) : null,
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
})();
