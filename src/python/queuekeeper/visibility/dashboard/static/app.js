// The dashboard's page: the header, the routes, and the loop that asks a panel's data and has the panel draw it.
// A panel's script calls QK.panel(NAME, {render(element, data, context), query(context)}) and draws with QK.h, which
// only ever sets text, so nothing the queue or a log says is read as markup.
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
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      element.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return element;
  };
  QK.fill = function (element, ...children) { element.replaceChildren(...children.flat().filter(Boolean)); };
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
    return bytes < 1024 ? bytes + " B" : bytes < 1048576 ? (bytes / 1024).toFixed(0) + " KB" : (bytes / 1048576).toFixed(1) + " MB";
  };
  QK.href = function (repository, tab, query) {
    const text = query ? new URLSearchParams(query).toString() : "";
    return "/r/" + encodeURIComponent(repository) + "/" + tab + (text ? "?" + text : "");
  };
  QK.panel = function (name, panel) { QK.panels[name] = panel; };
  QK.go = function (href, replace) {
    history[replace ? "replaceState" : "pushState"](null, "", href);
    show();
  };

  async function ask(path) {
    const response = await fetch(path, { cache: "no-store" });
    const found = await response.json().catch(() => ({ error: "the server's answer could not be read" }));
    if (!response.ok) throw new Error(found.error || "the server answered " + response.status);
    return found;
  }

  function route() {
    const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
    const query = Object.fromEntries(new URLSearchParams(location.search));
    return parts[0] === "r" && parts[1] ? { repository: parts[1], tab: parts[2] || "home", query } : { query };
  }

  function live(ok) {
    const element = document.getElementById("live");
    QK.fill(element, QK.h("span", { class: "dot " + (ok ? "active" : "danger") }), ok ? " Live" : " The server is not answering");
  }

  function header() {
    const picker = document.getElementById("repository"), name = document.getElementById("repository-name");
    const tabs = document.getElementById("tabs"), current = state.route;
    const many = state.repositories.length > 1;
    picker.hidden = !many;
    name.hidden = many || !current.repository;
    name.textContent = current.repository || "";
    if (many) {
      const marks = { active: "● ", warning: "▲ ", danger: "▲ ", idle: "" };
      QK.fill(picker, QK.h("option", { value: "" }, "All repositories"), state.repositories.map(one =>
        QK.h("option", { value: one.name, selected: one.name === current.repository }, (marks[one.level] || "") + one.name)));
      picker.value = current.repository || "";
      picker.onchange = () => QK.go(picker.value ? QK.href(picker.value, current.tab || "home") : "/");
    }
    QK.fill(tabs, current.repository ? state.panels.map(panel => QK.h("a", {
      href: QK.href(current.repository, panel.name), class: panel.name === current.tab ? "on" : null,
      "aria-current": panel.name === current.tab ? "page" : null }, panel.title)) : []);
    document.title = (current.repository ? current.repository + " · " : "") + "Queuekeeper";
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

  function all() {
    if (!state.repositories.length) {
      return QK.fill(main, QK.h("div", { class: "card hero" }, QK.h("h1", null, "No repository yet"),
        QK.h("p", null, "Queue a commit with qk land, or start qk serve inside a repository.")));
    }
    QK.fill(main, QK.h("div", { class: "card" }, QK.h("h2", null, "All repositories"),
      QK.h("ul", { class: "rows lines" }, state.repositories.map(one => QK.h("li", null, QK.h("a", {
        class: "row", href: QK.href(one.name, "home") },
        QK.h("span", null, QK.h("span", { class: "dot " + one.level }), " ", QK.h("b", null, one.name)),
        QK.h("span", { class: "ellipsis" }, one.headline, QK.h("span", { class: "muted" }, " " + one.detail)),
        QK.h("span", { class: "muted" }, "›")))))));
  }

  async function tick(generation) {
    const current = state.route, context = state.context;
    try {
      state.repositories = await ask("/api/repositories");
      if (generation !== state.generation) return;
      live(true);
      header();
      if (!current.repository) {
        if (state.repositories.length === 1) return QK.go(QK.href(state.repositories[0].name, "home"), true);
        if (!selecting()) all();
        return;
      }
      const known = state.panels.find(panel => panel.name === current.tab);
      if (!known) return fault("No such section", "This dashboard has no section named " + current.tab + ".");
      if (known.error) return fault(known.title, "This panel did not load: " + known.error);
      await script(current.tab);
      const panel = QK.panels[current.tab];
      if (!panel) throw new Error("its script defined no panel");
      const query = panel.query ? panel.query(context) : current.query;
      const data = await ask("/api/r/" + encodeURIComponent(current.repository) + "/panel/" + encodeURIComponent(current.tab)
        + "?" + new URLSearchParams(query || {}));
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
    if (href !== "/" && !href.startsWith("/r/")) return;
    event.preventDefault();
    QK.go(href);
  });
  window.addEventListener("popstate", show);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && state.context) state.context.refresh(); });

  ask("/api/panels").then(found => { state.panels = found; show(); }, error => { live(false); fault("Queuekeeper", error); });
})();
