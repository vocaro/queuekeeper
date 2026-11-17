// The dashboard's page: the header, the routes, and the loop that asks a panel's data and has the panel draw it.
// A panel's script calls QK.panel(NAME, {render(element, data, context), query(context)}) and draws with QK.h, which
// only ever sets text, so nothing the queue or a log says is read as markup. The page opens on every repository at
// once, at / and /all/TAB, where context.repository is null; the header's picker narrows it to one, at /r/NAME/TAB.
(function () {
  "use strict";
  const QK = window.QK = { panels: {}, POLL: 2000 };
  const main = document.getElementById("main");
  const state = { panels: [], repositories: [], route: null, generation: 0, timer: null, context: null, scripts: {} };

  QK.h = function (tag, attributes, ...children) {
    const element = document.createElement(tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (name === "class") element.className = value;
      else if (name === "width") element.style.width = value;
      else if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
      else element.setAttribute(name, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  };
  // An entry as a row. Where every repository is shown, the entry carries its repository's name, which the row
  // shows with that repository's hue (options.hues, from QK.hues), and its links go to that repository's pages.
  QK.entry = function (found, context, options = {}) {
    const h = QK.h, short = commit => commit ? String(commit).slice(0, 12) : "";
    const repository = found.repository || context.repository;
    const log = name => name ? h("a", { href: QK.logHref(context, repository, name) }, "log") : null;
    const settled = typeof found.settled === "number";
    const facts = options.compact ? found.id : [found.id, found.branch, found.commits > 1 ? QK.count(found.commits, "commit") : null, found.note,
      found.front ? "front of the queue" : null, found.noGate ? "no gate" : null,
      found.flakes ? QK.count(found.flakes, "flaky test") : null, found.ungated ? "landed ungated" : null]
      .filter(Boolean).join(" · ");
    return h("li", null,
      h("span", { class: "pill " + found.state }, found.state),
      h("span", null,
        h("a", { class: "entry-title ellipsis", href: QK.href(repository, "queue", { entry: found.id }) }, found.subject || found.id,
          !options.compact && found.landedAs ? h("span", { class: "muted mono" }, " → " + short(found.landedAs)) : null),
        h("div", { class: "sub mono wrap" }, found.repository && !context.repository
          ? QK.tag(found.repository, (options.hues || {})[found.repository]) : null, facts),
        !options.compact && found.why ? h("div", { class: "sub wrap" }, found.why, " ", log(found.log)) : null),
      h("span", { class: "muted" }, settled ? QK.ago(found.settled) : QK.since(found.submitted)));
  };
  // An SVG element, set as QK.h sets an HTML one, with its children's arrays nested to any depth: its colors come from app.css by class, never from an attribute.
  QK.svg = function (tag, attributes, ...children) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attributes || {})) {
      if (value !== null && value !== undefined && value !== false) element.setAttribute(name, value);
    }
    for (const child of children.flat(Infinity)) {
      if (child !== null && child !== undefined && child !== false) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  };
  // A sparkline of values, oldest first: a line over a faint area, or bars with the newest drawn strongest. Its
  // color is its tone's (good, danger or none for the accent), and title is what a pointer over it is told.
  QK.spark = function (values, options = {}) {
    const svg = QK.svg, width = 200, height = 40, pad = 3, bars = !!options.bars;
    values = (values || []).filter(value => typeof value === "number");
    if (values.length < 2 || !values.some(Boolean)) return null;
    const top = Math.max(...values, options.floor || 0) || 1;
    const x = index => pad + index * (width - 2 * pad) / (values.length - (bars ? 0 : 1));
    const y = value => height - pad - value / top * (height - 2 * pad);
    const marks = bars
      ? values.map((value, index) => value ? svg("rect", { class: index === values.length - 1 ? "now" : null, x: x(index),
          y: y(value), width: Math.max(1, (width - 2 * pad) / values.length - 1.5), height: height - pad - y(value), rx: 1.5 }) : null)
      : (points => [svg("polygon", { class: "area", points: `${x(0)},${height - pad} ${points} ${x(values.length - 1)},${height - pad}` }),
          svg("polyline", { class: "line", points })])(values.map((value, index) => `${x(index)},${y(value)}`).join(" "));
    return svg("svg", { class: "spark " + (bars ? "bars " : "") + (options.tone || ""), viewBox: `0 0 ${width} ${height}`,
      preserveAspectRatio: "none", role: "img", "aria-label": options.title || null },
      options.title ? svg("title", null, options.title) : null,
      svg("line", { class: "base", x1: 0, x2: width, y1: height - pad, y2: height - pad }), marks);
  };
  // An arc of a circle from one angle to another, in radians clockwise from three o'clock, as a path's d.
  QK.arc = function (x, y, radius, from, to) {
    const at = angle => (x + radius * Math.cos(angle)).toFixed(2) + " " + (y + radius * Math.sin(angle)).toFixed(2);
    return `M${at(from)} A${radius} ${radius} 0 ${to - from > Math.PI ? 1 : 0} 1 ${at(to)}`;
  };
  // A donut of parts, each {label, value, tone}, its tone a class app.css colors; middle and caption are its center's.
  QK.donut = function (parts, middle, caption) {
    const svg = QK.svg, total = parts.reduce((sum, part) => sum + part.value, 0), shown = parts.filter(part => part.value);
    const gap = shown.length > 1 ? 0.03 : 0, TOP = -Math.PI / 2;
    let angle = TOP;
    return svg("svg", { class: "donut", viewBox: "0 0 120 120", role: "img", "aria-label": parts.map(part => part.label + " " + part.value).join(", ") },
      svg("circle", { class: "track", cx: 60, cy: 60, r: 48 }),
      shown.map(part => {
        const span = Math.min(part.value / total * 2 * Math.PI, 2 * Math.PI - 0.0001), from = angle;
        angle += span;
        return svg("path", { class: "part " + (part.tone || ""), d: QK.arc(60, 60, 48, from + gap, from + span - gap) },
          svg("title", null, part.label + ": " + part.value));
      }),
      svg("text", { class: "middle", x: 60, y: 62, "text-anchor": "middle" }, middle),
      svg("text", { class: "caption", x: 60, y: 79, "text-anchor": "middle" }, caption));
  };
  // A ring of how far a run has gone against its median, with a tick at the median: it fills to the tick, then on
  // in amber, and is full and amber at half again past it. The middle says the share of the median, as a percentage.
  QK.ring = function (seconds, median) {
    const svg = QK.svg, TOP = -Math.PI / 2, scale = median * 1.5, at = value => TOP + Math.min(1, value / scale) * 2 * Math.PI * 0.9999;
    const tick = at(median), over = seconds > median;
    return svg("svg", { class: "ring" + (over ? " over" : ""), viewBox: "0 0 64 64", role: "img",
      "aria-label": QK.span(seconds) + " of a " + QK.span(median) + " median" },
      svg("title", null, QK.span(seconds) + " so far; the last passing runs' median is " + QK.span(median)),
      svg("circle", { class: "track", cx: 32, cy: 32, r: 26 }),
      seconds > 0 ? svg("path", { class: "fill", d: QK.arc(32, 32, 26, TOP, at(Math.min(seconds, median))) }) : null,
      over ? svg("path", { class: "fill late", d: QK.arc(32, 32, 26, tick, at(seconds)) }) : null,
      svg("line", { class: "tick", x1: 32 + 20 * Math.cos(tick), y1: 32 + 20 * Math.sin(tick), x2: 32 + 31 * Math.cos(tick), y2: 32 + 31 * Math.sin(tick) }),
      svg("text", { x: 32, y: 36, "text-anchor": "middle" }, Math.round(100 * seconds / median) + "%"));
  };
  // A half-circle gauge of a share from 0 to 100, with the part past its limit shaded and a tick at the limit: amber
  // at or past the limit. caption is the line under the share.
  QK.gauge = function (share, limit, caption) {
    const svg = QK.svg, at = value => Math.PI + Math.max(0, Math.min(100, value)) / 100 * Math.PI * 0.9999;
    const hasLimit = typeof limit === "number", over = hasLimit && share >= limit;
    return svg("svg", { class: "gauge" + (over ? " over" : ""), viewBox: "0 0 150 92", role: "img",
      "aria-label": Math.round(share) + "% " + caption },
      svg("path", { class: "track", d: QK.arc(75, 78, 60, Math.PI, 2 * Math.PI - 0.0001) }),
      hasLimit ? svg("path", { class: "beyond", d: QK.arc(75, 78, 60, at(limit), 2 * Math.PI - 0.0001) }) : null,
      share > 0 ? svg("path", { class: "fill", d: QK.arc(75, 78, 60, Math.PI, at(Math.max(0.5, share))) }) : null,
      hasLimit ? svg("line", { class: "tick", x1: 75 + 50 * Math.cos(at(limit)), y1: 78 + 50 * Math.sin(at(limit)),
        x2: 75 + 70 * Math.cos(at(limit)), y2: 78 + 70 * Math.sin(at(limit)) }) : null,
      svg("text", { class: "middle", x: 75, y: 70, "text-anchor": "middle" }, Math.round(share) + "%"),
      svg("text", { class: "caption", x: 75, y: 89, "text-anchor": "middle" }, caption));
  };
  // Each name's hue, p1 to p5 in sorted order and "other" past five, so one name keeps its color across a page.
  QK.hues = function (names) {
    const hues = {};
    [...new Set(names.filter(Boolean))].sort((a, b) => a.localeCompare(b)).forEach((name, index) => { hues[name] = index < 5 ? "p" + (index + 1) : "other"; });
    return hues;
  };
  // A stacked area of layers over time: times is each column's time in seconds, layers [{name, values, hue}] bottom
  // first, and ceiling the most the stack may reach, drawn as a dashed line. span is the seconds the x axis covers,
  // ending at the last time; a gap longer than gap seconds between two columns is left empty.
  QK.stack = function (times, layers, ceiling, span, gap) {
    const svg = QK.svg, width = 1000, height = 150, pad = 18, end = times[times.length - 1], top = Math.max(ceiling || 0, 1);
    const x = at => (1 - (end - at) / span) * width, y = value => height - pad - Math.min(top, value) / top * (height - pad - 6);
    const runs = [];  // spans of columns with no gap longer than gap between them
    times.forEach((at, index) => { if (!index || at - times[index - 1] > gap) runs.push([]); runs[runs.length - 1].push(index); });
    const base = times.map(() => 0);
    const shapes = layers.flatMap(layer => {
      const low = base.slice(), high = base.map((value, index) => value + (layer.values[index] || 0));
      high.forEach((value, index) => { base[index] = value; });
      return runs.filter(run => run.length > 1).map(run => svg("polygon", { class: "layer " + layer.hue,
        points: run.map(index => `${x(times[index]).toFixed(1)},${y(high[index]).toFixed(1)}`).concat(
          run.slice().reverse().map(index => `${x(times[index]).toFixed(1)},${y(low[index]).toFixed(1)}`)).join(" ") },
        svg("title", null, layer.name)));
    });
    return svg("svg", { class: "stack", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", role: "img",
      "aria-label": "Slots in use by project" },
      shapes,
      ceiling ? svg("line", { class: "ceiling", x1: 0, x2: width, y1: y(ceiling), y2: y(ceiling) }) : null,
      svg("line", { class: "base", x1: 0, x2: width, y1: height - pad, y2: height - pad }));
  };
  // A number tile: its label, its value, and the sparkline and caption beneath it when there is a trend to draw.
  // change, when given, is a chip beside the value: {text, tone} with tone good, bad or flat.
  QK.number = function (label, value, chart, caption, change) {
    return QK.h("div", { class: "number" }, QK.h("span", { class: "label" }, label,
      change ? QK.h("span", { class: "change " + (change.tone || "flat"), title: change.title || null }, change.text) : null),
      QK.h("b", null, value),
      chart || null, chart && caption ? QK.h("span", { class: "sub" }, caption) : null);
  };
  // The queue's overview numbers, as Queue and Home show them, each with the last day's trend behind it.
  QK.overview = function (counts, trend) {
    trend = trend || {};
    const gates = trend.gateSeconds || [], hourly = "per hour, last 24 h";
    return QK.h("div", { class: "numbers" },
      QK.number("In the queue", counts.waiting, QK.spark(trend.waiting, { title: "Entries waiting at the end of each hour, last 24 hours" }), "last 24 h"),
      QK.number("Landed today", counts.landed, QK.spark(trend.landed, { bars: true, tone: "good", title: "Entries landed each hour, last 24 hours" }), hourly),
      QK.number("Came back today", counts.cameBack, QK.spark(trend.cameBack, { bars: true, tone: "danger", floor: 3,
        title: "Entries that came back each hour, last 24 hours" }), hourly),
      QK.number("Median gate", typeof trend.medianGateSeconds === "number" ? QK.span(trend.medianGateSeconds) : "–",
        QK.spark(gates, { title: "Seconds of each passing gate run, oldest first" }), "last " + QK.count(gates.length, "passing run")));
  };
  QK.fill = function (element, ...children) { element.replaceChildren(...children.flat(Infinity).filter(Boolean)); };
  QK.span = function (seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    for (const [unit, size, below, part] of [["d", 86400, "h", 3600], ["h", 3600, "m", 60], ["m", 60, "s", 1]]) {
      if (seconds >= size) return Math.floor(seconds / size) + unit + " " + Math.floor(seconds % size / part) + below;
    }
    return seconds + "s";
  };
  QK.since = function (at) { return typeof at === "number" ? QK.span(Date.now() / 1000 - at) : ""; };
  QK.ago = function (at) { return typeof at === "number" ? QK.since(at) + " ago" : ""; };
  QK.clock = function (at) {
    if (typeof at !== "number") return "";
    const when = new Date(at * 1000), today = new Date().toDateString() === when.toDateString();
    const time = when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
    return today ? time : when.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time;
  };
  QK.count = function (number, noun, plural) { return number + " " + (number === 1 ? noun : plural || noun + "s"); };
  QK.size = function (bytes) {
    return bytes < 1024 ? bytes + " B" : bytes < 1048576 ? (bytes / 1024).toFixed(0) + " KB"
      : bytes < 1073741824 ? (bytes / 1048576).toFixed(1) + " MB" : (bytes / 1073741824).toFixed(1) + " GB";
  };
  // A tab's address: one repository's, or every repository's when repository is null, / for their Queue.
  QK.href = function (repository, tab, query) {
    const text = query ? new URLSearchParams(query).toString() : "";
    if (!repository && tab === "queue" && !text) return "/";
    return (repository ? "/r/" + encodeURIComponent(repository) + "/" : "/all/") + tab + (text ? "?" + text : "");
  };
  // A log's address in the Logs tab where the page is: one repository's, or every repository's, where a log is
  // named with its repository's name too.
  QK.logHref = function (context, repository, name) {
    return context.repository ? QK.href(context.repository, "logs", { name }) : QK.href(null, "logs", { repository, name });
  };
  // Each repository's hue, by the header's list, so that a repository keeps its color from tab to tab.
  QK.repositoryHues = function (names) {
    return QK.hues([...state.repositories.map(one => one.name), ...(names || [])]);
  };
  // A repository's name as a small tag in its hue, linking to its own Queue.
  QK.tag = function (repository, hue, extra, title) {
    return QK.h("a", { class: "tag", href: QK.href(repository, "queue"), title: title || "Only " + repository },
      QK.h("i", { class: "dot " + (hue || "other") }), repository, extra ? QK.h("span", { class: "good" }, extra) : null);
  };
  QK.panel = function (name, panel) { QK.panels[name] = panel; };
  QK.go = function (href, replace) {
    history[replace ? "replaceState" : "pushState"](null, "", href);
    show();
  };

  // The commit the server serves: one that moved, as when the server restarted itself on new code, reloads the page,
  // so that its scripts and styles are the server's.
  let served = null;
  async function ask(path) {
    const response = await fetch(path, { cache: "no-store" });
    const commit = response.headers.get("X-Queuekeeper-Commit");
    if (commit && served && commit !== served) location.reload();
    if (commit) served = commit;
    const found = await response.json().catch(() => ({ error: "the server's answer could not be read" }));
    if (!response.ok) throw new Error(found.error || "the server answered " + response.status);
    return found;
  }

  function route() {
    const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    const query = Object.fromEntries(new URLSearchParams(location.search));
    const tab = name => name === "home" || !name ? "queue" : name;
    if (parts[0] === "r" && parts[1]) return { repository: parts[1], tab: tab(parts[2]), query };
    return { repository: null, tab: tab(parts[0] === "all" ? parts[1] : null), query };
  }
  // The panels with a tab where the route is: every panel for one repository, those that draw every repository for all.
  function tabbed(current) { return state.panels.filter(panel => current.repository || panel.everywhere); }

  function live(ok) {
    const element = document.getElementById("live");
    QK.fill(element, QK.h("span", { class: "dot " + (ok ? "active" : "danger") }), ok ? " Live" : " The server is not answering");
  }

  // The picker keeps the tab: All repositories shows every repository's, a repository's name that one's alone.
  function header() {
    const picker = document.getElementById("repository"), tabs = document.getElementById("tabs"), current = state.route;
    picker.hidden = !state.repositories.length && !current.repository;
    const marks = { active: "● ", warning: "▲ ", danger: "▲ ", idle: "" };
    const names = state.repositories.map(one => one.name);
    if (current.repository && !names.includes(current.repository)) names.push(current.repository);
    QK.fill(picker, QK.h("option", { value: "" }, "All repositories"), names.map(name => {
      const one = state.repositories.find(found => found.name === name) || {};
      return QK.h("option", { value: name, selected: name === current.repository }, (marks[one.level] || "") + name);
    }));
    picker.value = current.repository || "";
    picker.onchange = () => {
      const to = picker.value || null, kept = tabbed({ repository: to }).some(panel => panel.name === current.tab);
      QK.go(QK.href(to, kept ? current.tab : "queue"));
    };
    QK.fill(tabs, tabbed(current).map(panel => QK.h("a", {
      href: QK.href(current.repository, panel.name), class: panel.name === current.tab ? "on" : null,
      "aria-current": panel.name === current.tab ? "page" : null }, panel.title)));
    document.title = (current.repository || "All repositories") + " · Queuekeeper";
  }

  function script(name) {
    if (!state.scripts[name]) {
      state.scripts[name] = new Promise((resolve, reject) => {
        const element = QK.h("script", { src: "/panels/" + encodeURIComponent(name) + ".js" });
        element.onload = resolve;
        element.onerror = () => reject(new Error("its script did not load"));
        document.head.append(element);
      });
    }
    return state.scripts[name];
  }

  function fault(title, error) {
    QK.fill(main, QK.h("div", { class: "card" }, QK.h("h2", null, title),
      QK.h("p", { class: "danger wrap" }, String(error && error.message || error))));
  }

  function selecting() {
    const selection = document.getSelection();
    return selection && !selection.isCollapsed && main.contains(selection.anchorNode);
  }

  function none() {
    QK.fill(main, QK.h("div", { class: "card hero" }, QK.h("h1", null, "No repository yet"),
      QK.h("p", null, "Queue a commit with qk land, or start qk serve inside a repository.")));
  }

  async function tick(generation) {
    const current = state.route, context = state.context;
    try {
      state.repositories = await ask("/api/repositories");
      if (generation !== state.generation) return;
      live(true);
      header();
      if (!current.repository && !state.repositories.length) return none();
      const known = tabbed(current).find(panel => panel.name === current.tab);
      if (!known) return fault("No such section", "This dashboard has no section named " + current.tab + ".");
      if (known.error) return fault(known.title, "This panel did not load: " + known.error);
      await script(current.tab);
      const panel = QK.panels[current.tab];
      if (!panel) throw new Error("its script defined no panel");
      const query = panel.query ? panel.query(context) : current.query;
      const data = await ask((current.repository ? "/api/r/" + encodeURIComponent(current.repository) : "/api/all")
        + "/panel/" + encodeURIComponent(current.tab) + "?" + new URLSearchParams(query || {}));
      if (generation !== state.generation || (selecting() && !context.fresh)) return;
      panel.render(main, data, context);
      context.fresh = false;
    } catch (error) {
      if (generation !== state.generation) return;
      if (error instanceof TypeError) live(false);
      else fault((state.panels.find(panel => panel.name === current.tab) || {}).title || "Queuekeeper", error);
      if (context) context.fresh = true;
    }
  }

  function show() {
    clearTimeout(state.timer);
    const generation = ++state.generation;
    state.route = route();
    state.context = { repository: state.route.repository, tab: state.route.tab, query: state.route.query, state: {},
      fresh: true, refresh: () => { if (generation === state.generation) { clearTimeout(state.timer); loop(); } } };
    QK.fill(main, QK.h("p", { class: "muted" }, "Loading…"));
    header();
    async function loop() {
      await tick(generation);
      if (generation === state.generation) state.timer = setTimeout(loop, QK.POLL);
    }
    loop();
  }

  document.addEventListener("click", event => {
    const link = event.target.closest && event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button || event.metaKey || event.ctrlKey || event.shiftKey) return;
    const href = link.getAttribute("href");
    if (href !== "/" && !href.startsWith("/r/") && !href.startsWith("/all/")) return;
    event.preventDefault();
    QK.go(href);
  });
  window.addEventListener("popstate", show);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.context) state.context.refresh(); });

  ask("/api/panels").then(found => { state.panels = found; show(); }, error => { live(false); fault("Queuekeeper", error); });
})();
