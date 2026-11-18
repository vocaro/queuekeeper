QK.panel("activity", {
  // A page of the feed asks for more rounds than the last when the reader asks for older ones.
  query(context) { return context.state.rounds ? { rounds: String(context.state.rounds) } : {}; },
  // What the queue did, newest first: a row for each round with how each of its entries ended and a chip for each
  // lane that opens its log, and a line for each hold and resume; for one repository, or for every repository at
  // once (context.repository null), where each row names its repository.
  render(element, data, context) {
    const h = QK.h, state = context.state, all = !context.repository;
    const hues = all ? QK.repositoryHues([...new Set(data.feed.map(one => one.repository).filter(Boolean))]) : {};
    const where = one => one.repository || context.repository;
    const show = state.show || "everything", search = (state.search || "").toLowerCase();
    // A round that came back is any of whose entries did not land; one that landed, any of whose entries did.
    const back = one => one.entries.some(entry => ["back", "conflict"].includes(entry.state));
    const landed = one => one.entries.some(entry => entry.state === "landed");
    const matches = one => !search || [one.repository, one.round, ...one.entries.flatMap(entry => [entry.subject, entry.branch, entry.id, entry.landedAs]),
      ...one.lanes.map(lane => lane.lane), ...one.flaky.map(found => found.test)].some(value => value && String(value).toLowerCase().includes(search));
    const shown = data.feed.filter(one => one.kind === "notice" ? show === "everything" && !search
      : (show === "everything" || (show === "back" ? back(one) : landed(one))) && matches(one));
    const PILL = { landed: "landed", back: "failed", conflict: "conflict", removed: "removed", testing: "testing", retested: "queued" };
    const SAID = { landed: "landed", back: "came back", conflict: "conflicted", removed: "removed", testing: "testing", retested: "tested again" };
    function chip(lane, one) {
      const tone = lane.status === null || lane.status === undefined ? "run" : lane.status === 0 ? "ok" : lane.status === 3 ? "unknown" : "bad";
      const said = { run: "running", ok: "passed", unknown: "unmeasured", bad: "exited " + lane.status }[tone];
      const label = [lane.role && lane.role !== "batch" ? lane.role : null, lane.lane].filter(Boolean).join(" ");
      const time = typeof lane.seconds === "number" ? QK.span(lane.seconds) : null;
      const inside = [label, tone === "ok" ? null : h("span", { class: "muted" }, " · " + (tone === "run" ? (time ? time + " so far" : "running") : said))];
      const title = label + ": " + said + (time ? ", " + time : "") + (lane.log ? ". Opens its log." : "");
      return lane.log ? h("a", { class: "lanechip " + tone, title, href: QK.logHref(context, where(one), lane.log) }, inside)
        : h("span", { class: "lanechip " + tone, title }, inside);
    }
    // The whole of why an entry came back, behind a toggle that stays open across the page's redraw.
    const opened = state.why || (state.why = new Set());
    function whyToggle(one, entry) {
      const key = (one.repository || "") + "\n" + one.round + "\n" + entry.id;
      const element = h("details", { open: opened.has(key) }, h("summary", null, "Why"), h("p", { class: "wrap" }, entry.why));
      element.addEventListener("toggle", () => { if (element.open) opened.add(key); else opened.delete(key); });
      return element;
    }
    function round(one) {
      const states = [...new Set(one.entries.map(entry => entry.state))];
      const testing = states.includes("testing");
      const at = new Date(one.time * 1000);
      return h("li", { class: "round" + (back(one) ? " back" : "") },
        h("span", { class: "time", title: at.toLocaleString() }, at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })),
        h("div", { class: "body" },
          h("div", { class: "top" }, all ? QK.tag(one.repository, hues[one.repository]) : null,
            testing ? h("span", { class: "pill testing" }, "testing")
              : states.map(found => h("span", { class: "pill " + PILL[found] }, (one.entries.length > 1 ? one.entries.filter(entry => entry.state === found).length + " " : "") + SAID[found])),
            one.entries.length > 1 ? h("span", { class: "sub" }, QK.count(one.entries.length, "entry", "entries") + " tested together") : null,
            one.entries.length === 1 && one.entries[0].landedAs ? h("span", { class: "mono muted" }, "→ " + one.entries[0].landedAs.slice(0, 8)) : null),
          h("ul", { class: "what" }, one.entries.map(entry => h("li", null,
            one.entries.length > 1 ? h("span", { class: "pill " + PILL[entry.state] }, SAID[entry.state]) : null,
            h("a", { class: "subject", href: QK.href(where(one), "queue", { entry: entry.id }) }, entry.subject || entry.id),
            entry.branch ? h("span", { class: "sub mono" }, entry.branch) : null,
            entry.why ? h("div", { class: "why" }, entry.reason || entry.why,
              entry.reason && entry.reason !== entry.why ? whyToggle(one, entry) : null) : null))),
          one.flaky.length ? h("div", { class: "flaky sub" }, one.flaky.map(found => h("div", { class: "wrap" }, "≈ flaky: ", h("span", { class: "mono" }, found.test),
            found.issue ? [" · ", h("a", { href: found.issue, target: "_blank", rel: "noopener" }, found.issue.replace(/^.*\/issues\//, "issue "))] : null))) : null,
          one.lanes.length ? h("div", { class: "lanechips" }, one.lanes.map(lane => chip(lane, one))) : null));
    }
    let day = null;
    const rows = [];
    for (const one of shown) {
      const at = new Date(one.time * 1000), heading = at.toDateString();
      if (heading !== day) {
        day = heading;
        const today = new Date().toDateString(), yesterday = new Date(Date.now() - 86400000).toDateString();
        rows.push(h("li", { class: "day label" }, heading === today ? "Today" : heading === yesterday ? "Yesterday"
          : at.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })));
      }
      rows.push(one.kind === "round" ? round(one) : h("li", { class: "notice-row " + one.level },
        h("span", { class: "time" }, at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })),
        h("span", { class: "wrap" }, all ? [QK.tag(one.repository, hues[one.repository]), " "] : null, one.text)));
    }
    const count = data.feed.filter(one => one.kind === "round").length;
    // The filters are built once, so a redraw every two seconds keeps the search box's focus and caret.
    if (!state.built) {
      state.built = true;
      state.feed = h("div");
      const search = h("input", { type: "search", id: "activity-search", "aria-label": "Find in the activity",
        placeholder: "Find a subject, branch, commit, lane or test",
        oninput: () => { clearTimeout(state.typing); state.typing = setTimeout(() => { state.search = search.value.trim(); context.refresh(); }, 250); } });
      const buttons = [["everything", "Everything"], ["back", "Came back"], ["landed", "Landed"]].map(([value, label]) =>
        h("button", { type: "button", "aria-pressed": String(value === "everything"), onclick: () => {
          state.show = value;
          buttons.forEach(button => button.setAttribute("aria-pressed", String(button === buttons.find(one => one.dataset.value === value))));
          context.refresh();
        }, "data-value": value }, label));
      QK.fill(element, h("div", { class: "card activity" },
        h("div", { class: "filters" }, h("span", { class: "segmented", role: "group", "aria-label": "Show" }, buttons), search), state.feed));
    }
    QK.fill(state.feed,
      (data.unread || []).map(one => h("p", { class: "warning sub" }, one.name + " cannot be read: " + one.error)),
      rows.length ? h("ul", { class: "feed" }, rows) : h("p", { class: "sub" }, data.feed.length ? "Nothing in the last " + QK.count(count, "round") + " matches." : "No round has run yet."),
      count < data.total ? h("button", { type: "button", class: "more", onclick: () => { state.rounds = count + 60; context.refresh(); } },
        "Show older rounds · " + (data.total - count).toLocaleString() + " more") : null);
  },
});
