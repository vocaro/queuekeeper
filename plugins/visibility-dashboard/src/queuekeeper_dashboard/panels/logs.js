QK.panel("logs", {
  // The page holds one stretch of the chosen log, from start to next, as whole lines from line firstLine. A request
  // asks for the tail, for the stretch around a line, for what comes before the stretch, or for what was written
  // after it; ask numbers the request, so an answer to an earlier one is dropped. Where every repository is shown,
  // a log is named with its repository's name too, state.repository.
  query(context) {
    const state = context.state;
    if (!state.name) return context.query.name ? { name: context.query.name, ...(context.query.repository ? { repository: context.query.repository } : {}) } : {};
    const query = { name: state.name, ask: String(state.ask || 0) }, want = state.want || { mode: "tail" };
    if (!context.repository) query.repository = state.repository || "";
    if (want.mode !== "tail") query[want.mode] = String(want.value);
    if (state.index) query.known = String(state.index.size);
    if (state.find) query.find = state.find;
    return query;
  },
  render(element, data, context) {
    const h = QK.h, svg = QK.svg, state = context.state, LIMIT = 2 << 20, FOLD = 6, all = !context.repository;
    const hues = all ? QK.repositoryHues((data.logs || []).map(log => log.repository)) : {};
    const tag = name => all && name ? h("span", { class: "tag" }, h("i", { class: "dot " + (hues[name] || "other") }), name) : null;
    const same = log => log.name === state.name && (!all || log.repository === state.repository);
    const LANES = ["gate", "docs"], ROUND = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/;
    if (!state.built) {
      state.built = true;
      Object.assign(state, { ask: 0, want: { mode: "tail" }, text: "", follow: true, fold: true, only: false, wrap: true,
        open: new Set(), rounds: new Set(), failedOnly: false, lane: "", search: "", hit: -1, signatures: {} });
      state.list = h("div", { class: "loglist" });
      state.searchBox = h("input", { type: "search", id: "log-search", placeholder: all ? "Find a repository, commit or lane" : "Find a commit or lane", "aria-label": "Find a log",
        oninput: () => { state.search = state.searchBox.value.trim().toLowerCase(); state.listed = null; browse(state.logs || []); } });
      state.chips = h("div", { class: "filters" });
      state.crumb = h("nav", { class: "crumb", "aria-label": "Where this log is" });
      state.head = h("div", { class: "loghead" });
      state.failures = h("div", { class: "card", hidden: true });
      state.overview = h("div", { class: "overview", hidden: true });
      state.findBox = h("input", { type: "search", id: "log-find", placeholder: "Find in this log", "aria-label": "Find in this log",
        oninput: () => { clearTimeout(state.typing); state.typing = setTimeout(find, 300); },
        onkeydown: event => { if (event.key === "Enter") { event.preventDefault(); step(event.shiftKey ? -1 : 1); } } });
      state.count = h("span", { class: "count sub" });
      state.toggles = {
        only: toggle("Failures only", "only"), fold: toggle("Fold passing suites", "fold"), wrap: toggle("Wrap", "wrap") };
      state.pane = h("div", { class: "logtext", tabindex: 0, "aria-label": "Log text", onscroll: scrolled });
      state.latest = h("button", { class: "latest", type: "button", hidden: true, onclick: latest }, "↓ Latest");
      state.note = h("p", { class: "sub" });
      // A breadcrumb across the top; this round's logs on the left; on the right, the log's failures when it has
      // any, then one card with its title and facts, the bar of the whole log, the find bar and the text.
      QK.fill(element, h("div", { class: "logs" }, state.crumb,
        h("div", { class: "card browser" }, state.searchBox, state.chips, state.list),
        h("div", { class: "viewer" }, state.failures,
          h("div", { class: "card reading" }, state.head, state.overview,
            h("div", { class: "findbar" }, state.findBox, state.count,
              h("button", { type: "button", "aria-label": "Previous match", onclick: () => step(-1) }, "↑"),
              h("button", { type: "button", "aria-label": "Next match", onclick: () => step(1) }, "↓"),
              h("span", { class: "sep" }), Object.values(state.toggles)),
            h("div", { class: "logwrap" }, state.pane, state.latest), state.note))));
    }
    function toggle(label, key) {
      const button = h("button", { type: "button", "aria-pressed": String(state[key]), onclick: () => {
        state[key] = !state[key];
        button.setAttribute("aria-pressed", String(state[key]));
        state.drawn = null;
        draw();
      } }, label);
      return button;
    }
    function choose(name, repository) {
      Object.assign(state, { name, repository: all ? repository : undefined, want: { mode: "tail" }, ask: state.ask + 1, text: "", start: 0, next: undefined, firstLine: 1,
        index: null, found: null, hit: -1, follow: true, drawn: null, open: new Set(), log: null, jump: null, target: null, drawnFirst: undefined, signatures: {} });
      state.findBox.value = state.find || "";
      history.replaceState(null, "", QK.logHref(context, repository, name));
      state.listed = null;
      browse(state.logs || []);
      context.refresh();
    }
    // Ask for the stretch around line, unless it is already held; then scroll to it.
    function go(line) {
      if (!line) return;
      state.follow = false;
      state.jump = line;
      if (state.next !== undefined && line >= state.firstLine && line < state.firstLine + count()) {
        openFold(line);
        return draw();
      }
      state.want = { mode: "line", value: line };
      state.ask += 1;
      context.refresh();
    }
    function count() { return lines().length; }
    function lines() {
      if (state.split && state.split.text === state.text) return state.split.lines;
      const found = state.text.split("\n");
      if (found.length && found[found.length - 1] === "") found.pop();
      state.split = { text: state.text, lines: found };
      return found;
    }
    function latest() {
      state.follow = true;
      if (state.next !== undefined && state.next >= (state.size || 0) && state.start + LIMIT >= state.size) {
        state.pane.scrollTop = state.pane.scrollHeight;
        state.latest.hidden = true;
        return;
      }
      state.want = { mode: "tail" };
      state.ask += 1;
      state.drawn = null;
      context.refresh();
    }
    function scrolled() {
      const box = state.pane, bottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
      if (Date.now() - (state.moved || 0) < 150) return;
      state.target = state.target && !bottom ? state.target : null;
      state.follow = bottom && state.next >= state.size;
      state.latest.hidden = state.follow;
    }
    function find() {
      const text = state.findBox.value.trim();
      if (text === (state.find || "")) return;
      state.find = text;
      state.found = null;
      state.hit = -1;
      state.drawn = null;
      draw();
      context.refresh();
    }
    function step(by) {
      const found = state.found && state.found.text === state.find ? state.found.lines : [];
      if (!found.length) return;
      state.hit = (state.hit + by + found.length) % found.length;
      state.count.textContent = (state.hit + 1) + " of " + matched();
      go(found[state.hit][0]);
    }
    function matched() {
      const found = state.found;
      return found.count > found.lines.length ? found.lines.length + "+" : String(found.count);
    }

    // The browser: logs grouped by round, newest first, worker.log pinned on top.
    function mark(log, reruns) {
      if (log.status === 0) return ["good", "✓", "passed"];
      if (log.status === 3) return ["unknown", "?", "unmeasured"];
      if (typeof log.status === "number") {
        if (reruns && reruns.length && reruns.every(rerun => rerun.status === 0)) return ["warn", "≈", "failed, then passed on rerun"];
        return ["bad", "✕", "exited " + log.status];
      }
      if (log.running || Date.now() / 1000 - log.modified < 60) return ["run", "◐", "running"];
      return ["plain", "·", "no result recorded"];
    }
    function when(round) {
      const found = ROUND.exec(round || "");
      return found ? new Date(+found[1], found[2] - 1, +found[3], +found[4], +found[5], +found[6]) : null;
    }
    function day(at) {
      const today = new Date(); today.setHours(0, 0, 0, 0);
      const then = new Date(at); then.setHours(0, 0, 0, 0);
      const days = Math.round((today - then) / 86400000);
      return days === 0 ? "Today" : days === 1 ? "Yesterday" : at.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
    }
    function hhmm(at) { return at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }
    function row(log, label, extra, reruns) {
      const [tone, glyph, said] = mark(log, reruns), on = same(log);
      return h("a", { class: "logrow" + (on ? " on" : "") + (extra ? " " + extra : ""), href: QK.logHref(context, log.repository, log.name),
        "aria-current": on ? "true" : null, title: (all ? log.repository + " · " : "") + log.name + " · " + said, onclick: event => {
          if (event.metaKey || event.ctrlKey || event.shiftKey) return;
          event.preventDefault();
          choose(log.name, log.repository);
        } },
        h("span", { class: "mark " + tone, "aria-label": said }, glyph),
        h("span", { class: "lane" }, label, log.mac ? h("span", { class: "sub" }, " " + log.mac) : null),
        h("span", { class: "meta" }, typeof log.seconds === "number" ? QK.span(log.seconds) : QK.size(log.size)));
    }
    // What the queue's own lanes are for; any other lane is a project's own command, by the name it gave it.
    const MEANING = { gate: "the tests every entry must pass", docs: "documentation checks",
      "pre-push": "the check before a push", carry: "a check after carrying a change over" };
    function browse(logs) {
      const signature = JSON.stringify([state.name, state.repository, state.failedOnly, state.lane, state.search, [...state.rounds], state.workers, state.every,
        logs.map(log => [log.repository, log.name, log.status, Math.round(log.modified / 30)])]);
      if (signature === state.listed) return;
      state.listed = signature;
      // A log of a round lists that round's logs alone, beside its repository's worker.log, until the reader asks for
      // every log; this page is reached from a round on the Activity tab, which is where to find another.
      const chosen = logs.find(same), scoped = !!(chosen && chosen.round) && !state.every;
      state.searchBox.hidden = scoped;
      if (scoped) {
        logs = logs.filter(log => log.repository === chosen.repository && (log.name === "worker.log" || (log.round === chosen.round && log.commit === chosen.commit)));
        QK.fill(state.chips, h("span", { class: "label" }, "This round's logs"));
      }
      const lanes = [...new Set(logs.map(log => log.lane).filter(lane => lane && lane !== "rerun"))]
        .sort((a, b) => (LANES.indexOf(a) + 1 || 99) - (LANES.indexOf(b) + 1 || 99) || a.localeCompare(b));
      if (!lanes.includes(state.lane)) state.lane = "";
      const again = () => { state.listed = null; browse(state.logs || []); };
      // Which repositories have each lane, for its option; a lane is a command a project names in its queuekeeper.json.
      const owners = lane => [...new Set(logs.filter(log => log.lane === lane).map(log => log.repository).filter(Boolean))];
      const picker = h("select", { id: "log-lane", onchange: () => { state.lane = picker.value; again(); } },
        h("option", { value: "" }, "Every lane"),
        lanes.map(lane => {
          const by = owners(lane), said = MEANING[lane] || (/^carry-\d+$/.test(lane) ? MEANING.carry : null);
          return h("option", { value: lane, title: said || "A lane " + (by.length === 1 ? by[0] : "these projects") + " defines in its queuekeeper.json" },
            lane + (said ? " · " + said : "") + (all && by.length ? " (" + (by.length > 2 ? by.length + " repositories" : by.join(", ")) + ")" : ""));
        }));
      picker.value = state.lane;
      if (!scoped) QK.fill(state.chips,
        h("div", { class: "segmented", role: "group", "aria-label": "Show" },
          [["All results", false], ["Failed only", true]].map(([label, value]) => h("button", { type: "button", "aria-pressed": String(state.failedOnly === value),
            title: value ? "Only logs of runs that failed" : "Logs of every run, whatever its result", onclick: () => { state.failedOnly = value; again(); } }, label))),
        h("label", { class: "lanepick", for: "log-lane" }, h("span", { class: "label" }, "Lane"), picker));
      const groups = new Map(), other = [];
      for (const log of logs) {
        if (log.name === "worker.log") continue;
        if (!log.round) { other.push(log); continue; }
        const key = log.repository + "\n" + log.round + "-" + log.commit;
        if (!groups.has(key)) groups.set(key, { round: log.round, commit: log.commit, repository: log.repository, logs: [] });
        groups.get(key).logs.push(log);
      }
      const failed = log => typeof log.status === "number" && log.status !== 0 && log.status !== 3;
      const shown = log => (!state.failedOnly || failed(log)) && (!state.lane || log.lane === state.lane)
        && (!state.search || log.name.toLowerCase().includes(state.search) || (all && log.repository.toLowerCase().includes(state.search)));
      const workers = !state.failedOnly && !state.lane ? logs.filter(log => log.name === "worker.log" && (!state.search || "worker.log".includes(state.search)
        || (all && log.repository.toLowerCase().includes(state.search)))) : [];
      // Every repository has a worker.log: where all are shown they fold into one row, open while searching or when one is chosen.
      const open = !all || workers.length < 3 || state.workers || !!state.search || workers.some(same);
      const rows = all && workers.length >= 3 ? [h("button", { type: "button", class: "reruns", "aria-expanded": String(open), onclick: () => {
        state.workers = !open; state.listed = null; browse(state.logs || []); } }, (open ? "▾ " : "▸ ") + QK.count(workers.length, "worker log"))] : [];
      if (open && !scoped) rows.push(workers.map(worker => row(worker, [h("b", null, "worker.log"), all ? " " : null, tag(worker.repository)], all && workers.length >= 3 ? "indent" : null)));
      let heading = null;
      // Newest round first, whichever repository it is in.
      for (const group of [...groups.values()].sort((a, b) => b.round.localeCompare(a.round))) {
        const reruns = group.logs.filter(log => log.lane === "rerun").sort((a, b) => a.test - b.test || a.attempt - b.attempt);
        const main = group.logs.filter(log => log.lane !== "rerun" && shown(log))
          .sort((a, b) => (LANES.indexOf(a.lane) + 1 || 99) - (LANES.indexOf(b.lane) + 1 || 99) || a.lane.localeCompare(b.lane));
        if (!main.length) continue;
        const at = when(group.round), title = at ? day(at) : "";
        if (!scoped && title !== heading) { heading = title; rows.push(h("div", { class: "day label" }, title)); }
        if (!scoped) rows.push(h("div", { class: "roundhead" }, h("span", { class: "tnum" }, at ? hhmm(at) : group.round), tag(group.repository),
          h("span", { class: "mono sub" }, group.commit)));
        for (const log of main) {
          const own = log.lane === "gate" ? reruns : null;
          rows.push(row(log, log.lane, null, own));
          if (!own || !own.length) continue;
          const key = group.repository + group.round + group.commit, open = scoped || state.rounds.has(key) || own.some(same);
          const passed = own.filter(rerun => rerun.status === 0).length, tests = new Set(own.map(rerun => rerun.test)).size;
          rows.push(h("button", { type: "button", class: "reruns", "aria-expanded": String(open), onclick: () => {
            if (state.rounds.has(key)) state.rounds.delete(key); else state.rounds.add(key);
            state.listed = null;
            browse(state.logs || []);
          } }, (open ? "▾ " : "▸ ") + QK.count(own.length, "rerun") + " of " + QK.count(tests, "test") + " · "
            + (passed === own.length ? "all passed" : passed + " passed")));
          if (open) rows.push(own.map(rerun => row(rerun, "rerun " + rerun.test + "." + rerun.attempt, "indent")));
        }
      }
      // A round's own logs first, then the queue's own log, then the way to every other log.
      if (scoped) rows.push(workers.map(worker => row(worker, [h("b", null, "worker.log"), h("span", { class: "sub" }, " the queue's own")])),
        h("button", { type: "button", class: "more every", onclick: () => { state.every = true; state.listed = null; browse(state.logs || []); } }, "Every log ›"));
      const others = other.filter(shown);
      if (others.length) rows.push(h("div", { class: "day label" }, "Other logs"), others.map(log => row(log, [log.name, all ? " " : null, tag(log.repository)])));
      for (const one of data.unread || []) rows.push(h("p", { class: "sub warning wrap" }, one.repository + " cannot be read: " + one.error));
      if (rows.filter(Boolean).length === 0) rows.push(h("p", { class: "sub" }, "No log matches."));
      QK.fill(state.list, rows);
    }

    // The header: what ran, how it ended and where.
    function header() {
      const log = state.log || { name: state.name }, index = state.index;
      const [tone, glyph, said] = mark(log, state.reruns), at = when(log.round);
      const tests = index && index.summary ? /(\d+) tests? \((\d+) skipped\)/.exec(index.summary) : null;
      const failures = index ? unique(index.failures).length : 0;
      QK.fill(state.crumb, h("a", { href: QK.href(context.repository, "activity") }, "‹ Activity"),
        all && state.repository ? [h("span", { class: "muted" }, "/"), tag(state.repository)] : null,
        h("span", { class: "muted" }, "/"),
        at ? h("span", null, "round " + day(at).toLowerCase() + " " + hhmm(at), log.commit ? h("span", { class: "mono muted" }, " · " + log.commit) : null)
          : h("span", { class: "mono" }, log.name));
      const facts = [
        log.lane && log.lane !== "rerun" && typeof log.status === "number" ? ["Mac", log.mac || "this Mac"] : null,
        typeof log.seconds === "number" ? ["Took", QK.span(log.seconds)] : null,
        tests ? ["Tests", Number(tests[1]).toLocaleString() + (failures ? ", " + failures + " failed" : "")] : null,
        ["Size", QK.size(state.size || log.size || 0) + (index ? ", " + index.lines.toLocaleString() + " lines" : "")]].filter(Boolean);
      const copy = h("button", { type: "button", onclick: () => {
        const done = text => { copy.textContent = text; setTimeout(() => { copy.textContent = "Copy path"; }, 1500); };
        navigator.clipboard.writeText(state.path).then(() => done("Copied"), () => done("Could not copy"));
      } }, "Copy path");
      QK.fill(state.head,
        h("div", { class: "logtitle" }, h("h2", null, log.lane === "rerun" ? "rerun " + log.test + "." + log.attempt : log.lane || log.name),
          log.round || typeof log.status === "number" ? h("span", { class: "pill " + tone }, glyph + " " + said) : null,
          h("span", { class: "mono sub ellipsis" }, log.name),
          state.path && navigator.clipboard ? copy : null),
        h("dl", { class: "facts" }, facts.map(([key, value]) => h("div", null, h("dt", null, key), h("dd", null, value)))));
    }
    function unique(failures) {
      const seen = new Set();
      return (failures || []).filter(failure => !seen.has(failure.test) && seen.add(failure.test));
    }
    // Each failing test with its reason, a way to it, and what its reruns found.
    function failures() {
      const found = state.index ? unique(state.index.failures) : [];
      state.failures.hidden = !found.length;
      if (!found.length) return;
      const reruns = state.reruns || [], flaky = state.flaky || {};
      const passedAll = found.every((_, number) => { const own = reruns.filter(rerun => rerun.test === number + 1); return own.length && own.every(rerun => rerun.status === 0); });
      QK.fill(state.failures, h("h2", null, "Failures", h("span", { class: "muted" }, QK.count(found.length, "test")
        + (passedAll ? ", each passed on rerun" : ""))),
        h("div", { class: "failures" }, found.map((failure, number) => {
          const own = reruns.filter(rerun => rerun.test === number + 1), passed = own.filter(rerun => rerun.status === 0).length;
          const issue = flaky[failure.test];
          return h("div", { class: "failure" + (own.length && passed === own.length ? " flaky" : "") },
            h("span", { class: "name mono" }, failure.test),
            h("button", { type: "button", onclick: () => go(failure.line) }, "Go to line " + failure.line.toLocaleString()),
            failure.reason ? h("span", { class: "why mono" }, failure.reason) : null,
            own.length || failure.test in flaky ? h("span", { class: "then" },
              own.length ? QK.count(own.length, "rerun") + ": " + (passed === own.length ? "passed every time" : passed + " passed, " + (own.length - passed) + " failed") + ". " : "",
              failure.test in flaky ? h("span", null, "Recorded as flaky", issue ? [" in ", h("a", { href: issue, target: "_blank", rel: "noopener" }, issue.replace(/^.*\/issues\//, "issue "))] : "", ".") : null) : null);
        })));
    }
    // The whole log as one bar: failures, suites and heartbeats where they are, and a box for what is shown.
    function overview() {
      const index = state.index;
      state.overview.hidden = !index || index.lines < 200;
      if (state.overview.hidden) return;
      const W = 1000, H = 34, x = line => (line - 1) / Math.max(1, index.lines - 1) * W, first = state.firstLine, shown = count();
      const marks = index.marks.filter(([, kind]) => kind !== "summary"), fails = marks.filter(([, kind]) => kind === "fail" || kind === "failed");
      const bar = svg("svg", { class: "strip", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
        "aria-label": index.lines.toLocaleString() + " lines" + (fails.length ? ", failures at lines " + fails.slice(0, 5).map(([line]) => line.toLocaleString()).join(", ") : "") },
        svg("rect", { class: "ground", x: 0, y: 0, width: W, height: H, rx: 4 }),
        marks.filter(([, kind]) => kind === "pass").map(([line]) => svg("rect", { class: "pass", x: x(line), y: 22, width: 1.5, height: 10 })),
        marks.filter(([, kind]) => kind === "beat").map(([line]) => svg("rect", { class: "beat", x: x(line), y: 2, width: 1.5, height: 10 })),
        state.next !== undefined ? svg("rect", { class: "window", x: x(first), y: 1, width: Math.max(4, x(first + shown) - x(first)), height: H - 2, rx: 3 }) : null,
        fails.map(([line]) => svg("rect", { class: "fail", x: x(line) - 1, y: 0, width: 3, height: H }, svg("title", null, "Failure at line " + line.toLocaleString()))));
      bar.addEventListener("click", event => {
        const box = bar.getBoundingClientRect();
        go(Math.max(1, Math.round((event.clientX - box.left) / box.width * index.lines)));
      });
      bar.prepend(svg("title", null, "The whole log: red a failure, green a suite that passed, gray a heartbeat, the box what is loaded. Click to go there."));
      QK.fill(state.overview, bar);
    }

    // The text: a class for each line, runs of passing suites (or, with Failures only, of all but failures) folded.
    const FAILED = /^(FAIL|ERROR): /, SUITE = /^== .+?: (OK|FAILED)\b/, SUMMARY = /^\s*\S+ unittest: \d+ tests\b/;
    const EXCEPTION = /^[A-Za-z_][\w.]*(Error|Exception|Failure|Interrupt|Exit)\b/;
    function classes(all) {
      let trace = false, block = null;
      return all.map(line => {
        const suite = SUITE.exec(line);
        if (suite) block = suite[1] === "OK" ? "pass" : "failed";
        else if (line.startsWith("== ") || SUMMARY.test(line) || line.startsWith("remote: ")) block = null;
        if (line.startsWith("Traceback")) trace = true;
        else if (trace && !/^\s/.test(line)) trace = EXCEPTION.test(line);
        const kind = FAILED.test(line) ? "fail" : suite ? (suite[1] === "OK" ? "suite-ok" : "suite-bad") : SUMMARY.test(line) ? "summary"
          : trace || EXCEPTION.test(line) ? "trace" : line.startsWith("remote: ") ? "remote" : "";
        if (trace && EXCEPTION.test(line)) trace = false;
        return { kind, block };
      });
    }
    function many(number, noun, plural) { return number.toLocaleString() + " " + (number === 1 ? noun : plural || noun + "s"); }
    // Draw part again only when what it shows has changed, so a button keeps its focus between polls.
    function once(key, value, draw) {
      const signature = JSON.stringify(value);
      if (state.signatures[key] === signature) return;
      state.signatures[key] = signature;
      draw();
    }
    function highlighted(line) {
      const wanted = (state.find || "").toLowerCase();
      if (!wanted) return line;
      const parts = [], lower = line.toLowerCase();
      let from = 0, at;
      while ((at = lower.indexOf(wanted, from)) >= 0) {
        parts.push(line.slice(from, at), h("mark", null, line.slice(at, at + wanted.length)));
        from = at + wanted.length;
      }
      return from ? [...parts, line.slice(from)] : line;
    }
    function openFold(line) {
      for (const fold of state.folds || []) if (line >= fold.from && line <= fold.to) state.open.add(fold.from);
      state.drawn = null;
    }
    function draw() {
      const signature = [state.name, state.start, state.next, state.fold, state.only, state.wrap, state.find, state.open.size, state.jump, state.target].join("|");
      state.pane.classList.toggle("nowrap", !state.wrap);
      if (signature === state.drawn) return;
      const all = lines(), kinds = classes(all), first = state.firstLine || 1, box = state.pane;
      const keep = all.map((line, index) => {
        const { kind, block } = kinds[index];
        if (state.only) return block === "failed" || ["fail", "trace", "summary", "suite-bad"].includes(kind);
        return !state.fold || block !== "pass";
      });
      const nodes = [], folds = [];
      if (state.start > 0) nodes.push(h("div", { class: "earlier sub" }, "The " + QK.size(state.start) + " before line " + first.toLocaleString() + " is not loaded.",
        h("button", { type: "button", onclick: () => { state.want = { mode: "before", value: state.start }; state.ask += 1; state.follow = false; context.refresh(); } }, "Load earlier")));
      for (let index = 0; index < all.length;) {
        let end = index;
        if (!keep[index]) while (end + 1 < all.length && !keep[end + 1]) end += 1;
        const from = first + index, to = first + end;
        if (!keep[index] && end - index + 1 >= FOLD && !state.open.has(from)) {
          const run = all.slice(index, end + 1), suites = run.filter(line => /^== .+?: OK\b/.test(line)).length;
          const beats = run.filter(line => line.startsWith("remote: heartbeat ")).length;
          const hits = state.find ? run.filter(line => line.toLowerCase().includes(state.find.toLowerCase())).length : 0;
          folds.push({ from, to });
          nodes.push(h("button", { type: "button", class: "fold", onclick: () => { state.open.add(from); state.drawn = null; draw(); } },
            "▸ " + many(end - index + 1, "line") + (suites ? " · " + many(suites, "suite") + " passed" : "")
            + (beats ? " · " + many(beats, "heartbeat") : "") + (hits ? " · " + many(hits, "match", "matches") : "")
            + " · lines " + from.toLocaleString() + "–" + to.toLocaleString()));
          index = end + 1;
          continue;
        }
        for (let at = index; at <= end; at++) {
          const number = first + at;
          nodes.push(h("div", { class: "ln " + kinds[at].kind + (number === state.jump || number === state.target ? " target" : ""), "data-line": number },
            h("span", { class: "no" }, number.toLocaleString()), h("span", { class: "tx" }, highlighted(all[at]))));
        }
        index = end + 1;
      }
      if (!all.length) nodes.push(h("p", { class: "sub empty" }, state.next === undefined ? "Reading…" : "This log is empty."));
      state.folds = folds;
      const anchor = !state.follow && state.drawnFirst !== undefined && state.drawnFirst > first ? box.scrollHeight - box.scrollTop : null;
      state.moved = Date.now();
      QK.fill(box, nodes);
      const target = state.jump ? box.querySelector(`[data-line="${state.jump}"]`) : null;
      if (target) {
        box.scrollTop = target.offsetTop - box.clientHeight / 3;
        state.target = state.jump;
        state.jump = null;
      } else if (state.follow) box.scrollTop = box.scrollHeight;
      else if (anchor !== null) box.scrollTop = box.scrollHeight - anchor;
      state.drawnFirst = first;
      state.drawn = signature;
    }

    // The answer.
    state.logs = data.logs || [];
    browse(state.logs);
    if (!state.logs.length && !state.name) { QK.fill(state.list, h("p", { class: "sub" }, all ? "No queue has logs yet." : "The queue has no logs yet.")); return; }
    if (!state.name) {
      const gate = state.logs.find(log => log.lane === "gate") || state.logs[0];
      return context.query.name ? choose(context.query.name, context.query.repository) : choose(gate.name, gate.repository);
    }
    if (data.name !== state.name || (all && data.repository !== state.repository) || data.ask !== String(state.ask)) return;
    Object.assign(state, { size: data.size, path: data.path, log: data.log, reruns: data.reruns, flaky: data.flaky });
    if (data.index) state.index = data.index;
    if (data.found) state.found = data.found;
    const mode = state.want.mode;
    if (mode === "offset") {
      if (data.offset !== state.next) return;
      state.text += data.text;
    } else if (mode === "before") {
      state.text = data.text + state.text;
      Object.assign(state, { start: data.offset, firstLine: data.line || state.firstLine });
    } else {
      Object.assign(state, { text: data.text, start: data.offset, firstLine: data.line || 1, open: new Set() });
      if (mode === "line" && state.jump) openFold(state.jump);
    }
    if (mode !== "before") state.next = data.next;
    if (state.text.length > LIMIT) {
      // Drop the earlier half, at a line's end, keeping line numbers and the byte offset in step.
      const cut = state.text.indexOf("\n", state.text.length - LIMIT / 2) + 1, dropped = state.text.slice(0, cut);
      state.firstLine += (dropped.match(/\n/g) || []).length;
      state.start += new TextEncoder().encode(dropped).length;
      state.text = state.text.slice(cut);
    }
    state.want = { mode: "offset", value: state.next };
    const index = state.index ? [state.index.size, state.index.lines] : null;
    once("header", [state.name, state.log, state.size, index, !!state.path], header);
    once("failures", [state.name, index, state.reruns, state.flaky], failures);
    once("overview", [state.name, index, state.firstLine, state.next !== undefined && count()], overview);
    draw();
    const reading = state.next < data.size;
    state.latest.hidden = state.follow;
    const found = state.found && state.found.text === state.find ? state.found : null;
    state.count.textContent = !state.find ? "" : !found ? "Finding…" : !found.count ? "No match" : (state.hit >= 0 ? (state.hit + 1) + " of " : "") + matched();
    state.note.textContent = QK.size(data.size) + (reading ? ", reading…" : state.log && (state.log.running || (state.log.status === undefined && Date.now() / 1000 - (state.log.modified || 0) < 60))
      ? ", still being written; it follows new lines while you are at the bottom" : ", up to date");
    if (reading && (state.follow || state.next - state.start < LIMIT / 2)) context.refresh();
  },
});
