QK.panel("history", {
  // For one repository, or for every repository at once (context.repository null), where data.repositories has a
  // row for each and each run, entry and event names its repository.
  render(element, data, context) {
    const h = QK.h, svg = QK.svg, today = data.today, yesterday = data.yesterday || {}, all = !context.repository;
    const hues = all ? QK.repositoryHues(data.repositories.map(one => one.name)) : {};
    const tag = name => all && name ? h("span", { class: "tag" }, h("i", { class: "dot " + (hues[name] || "other") }), name) : null;
    // The change from yesterday to the same time of day, colored by whether it is good: more landed is good, more
    // flaky tests or a slower gate is not, and rounds are neither.
    function change(now, then, better, seconds) {
      if (typeof now !== "number" || typeof then !== "number") return null;
      const by = now - then, text = seconds ? QK.span(Math.abs(by)) : String(Math.abs(by));
      if (!by) return { text: "same", tone: "flat", title: "The same as yesterday at this time" };
      return { text: (by > 0 ? "▲ " : "▼ ") + text, tone: !better ? "flat" : (by > 0) === (better === "more") ? "good" : "bad",
        title: (seconds ? QK.span(then) : then) + " yesterday at this time" };
    }
    // Today's lane runs as bars on a lane per Mac: green passed, red failed, purple unmeasured; a bar opens its log.
    // Where every repository is shown, a bar takes its repository's hue, a failure outlined red and a run left
    // unmeasured outlined dashed; the toggle colors them by how they ended instead (byRepository).
    const byRepository = all && context.state.laneColor !== "status";
    function swimlanes() {
      const runs = data.gates || [];
      if (!runs.length) return h("p", { class: "sub" }, "No lane has finished today.");
      const macs = [...new Set(runs.map(run => run.mac || "this Mac"))].sort((a, b) => (b === "this Mac") - (a === "this Mac") || a.localeCompare(b));
      const first = Math.min(...runs.map(run => run.began)), last = Math.max(Date.now() / 1000, ...runs.map(run => run.ended));
      const from = Math.floor(first / 3600) * 3600, to = Math.ceil(last / 3600) * 3600, hours = Math.max(1, (to - from) / 3600);
      const W = 960, left = 170, lane = 34, H = macs.length * lane + 26, x = at => left + (at - from) / (to - from) * (W - left - 8);
      const step = hours > 12 ? 3 : hours > 6 ? 2 : 1;
      return h("div", { class: "chart-scroll" }, svg("svg", { class: "swim", viewBox: `0 0 ${W} ${H}`, role: "img",
        "aria-label": QK.count(runs.length, "lane run") + " today on " + QK.count(macs.length, "Mac") },
        Array.from({ length: hours + 1 }, (_, index) => from + index * 3600).map((at, index) => [
          svg("line", { class: "grid", x1: x(at), x2: x(at), y1: 2, y2: H - 20 }),
          index % step ? null : svg("text", { class: "axis", x: x(at), y: H - 5, "text-anchor": "middle" },
            new Date(at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }))]),
        macs.map((mac, row) => [svg("text", { class: "name", x: 0, y: row * lane + 21 }, mac.length > 22 ? mac.slice(0, 21) + "…" : mac, svg("title", null, mac)),
          runs.filter(run => (run.mac || "this Mac") === mac).map(run => {
            const tone = run.status === 0 ? "good" : run.status === 3 ? "unknown" : "danger";
            const kind = byRepository ? "repo " + (hues[run.repository] || "other") + (run.status === 0 ? "" : " " + tone) : tone;
            const bar = svg("rect", { class: "run " + kind + (run.lane === "gate" ? "" : " minor"), x: x(run.began), y: row * lane + 8 + (run.lane === "gate" ? 0 : 5),
              width: Math.max(2, x(run.ended) - x(run.began)), height: run.lane === "gate" ? 18 : 8, rx: 3 },
              svg("title", null, (all ? run.repository + " · " : "") + run.lane + " " + (run.status === 0 ? "passed" : run.status === 3 ? "unmeasured" : "exited " + run.status)
                + " · " + QK.span(run.ended - run.began) + " · ended " + QK.clock(run.ended)));
            return run.log ? svg("a", { href: QK.logHref(context, run.repository, run.log) }, bar) : bar;
          })])));
    }
    // Landings by day and hour, a darker cell for more.
    function heatmap() {
      const days = data.landings || [];
      if (!days.length) return h("p", { class: "sub" }, "Nothing has landed yet.");
      const most = Math.max(1, ...days.flatMap(day => day.hours)), cell = 22, left = 54, W = left + 24 * cell, H = days.length * cell + 18;
      return svg("svg", { class: "heat", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Landings by day and hour" },
        days.map((day, row) => [svg("text", { class: "axis", x: 0, y: row * cell + 15 },
          new Date(day.day * 1000).toLocaleDateString([], { weekday: "short", day: "numeric" })),
          day.hours.map((count, hour) => svg("rect", { class: "cell" + (count ? " level" + Math.min(4, Math.ceil(4 * count / most)) : ""),
            x: left + hour * cell + 1, y: row * cell + 1, width: cell - 3, height: cell - 3, rx: 3 },
            svg("title", null, QK.count(count, "landed", "landed") + ", " + String(hour).padStart(2, "0") + ":00")))]),
        [0, 6, 12, 18, 23].map(hour => svg("text", { class: "axis", x: left + hour * cell + cell / 2, y: H - 2, "text-anchor": "middle" },
          String(hour).padStart(2, "0"))));
    }
    // How long passing gates take, as bins with the median marked.
    function durations() {
      const runs = data.gateSeconds || [];
      if (runs.length < 3) return h("p", { class: "sub" }, "Too few passing gate runs to chart yet.");
      const sorted = runs.slice().sort((a, b) => a - b), median = sorted[Math.floor(sorted.length / 2)];
      const minutes = sorted.map(seconds => seconds / 60), low = Math.floor(minutes[0]), high = Math.ceil(minutes[minutes.length - 1]) + 1;
      const width = Math.max(1, Math.ceil((high - low) / 12)), bins = [];
      for (let from = low; from < high; from += width) bins.push({ from, count: minutes.filter(value => value >= from && value < from + width).length });
      const most = Math.max(...bins.map(bin => bin.count)), W = 480, H = 150, pad = 20, slot = (W - 8) / bins.length;
      const x = value => 4 + (value - low) / (bins.length * width) * (W - 8), y = count => H - pad - count / most * (H - pad - 14);
      return svg("svg", { class: "hist", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Passing gate durations, median " + QK.span(median) },
        bins.map(bin => svg("rect", { class: "bin", x: x(bin.from) + 1, y: y(bin.count), width: Math.max(1, slot - 3), height: H - pad - y(bin.count), rx: 2 },
          svg("title", null, QK.count(bin.count, "run") + " of " + bin.from + "–" + (bin.from + width) + " min"))),
        svg("line", { class: "base", x1: 0, x2: W, y1: H - pad, y2: H - pad }),
        svg("line", { class: "median", x1: x(median / 60), x2: x(median / 60), y1: 4, y2: H - pad }),
        svg("text", { class: "axis strong", x: Math.min(W - 70, x(median / 60) + 4), y: 12 }, "median " + QK.span(median)),
        bins.filter((_, index) => index % Math.ceil(bins.length / 6) === 0).map(bin =>
          svg("text", { class: "axis", x: x(bin.from), y: H - 5 }, bin.from + "m")));
    }
    // Each repository's day beside yesterday's to this time, and its passing gates' times as a strip of dots on one
    // scale of minutes, the median marked: a repository's gate is its own, so no median is taken across them. The
    // scale is logarithmic, so a one-minute gate and a hundred-minute one each keep room to show their spread.
    function repositories() {
      // Those that did something today first, the busiest first; the rest in the listed order.
      const busy = one => one.error ? 1e9 : one.today.landed + one.today.cameBack + one.today.rounds;
      const rows = data.repositories.slice().sort((a, b) => busy(b) - busy(a)), timed = rows.filter(one => (one.gateSeconds || []).length);
      const minutes = timed.flatMap(one => one.gateSeconds).map(seconds => seconds / 60);
      const NICE = [0.25, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000];
      const low = minutes.length ? Math.max(...NICE.filter(tick => tick <= Math.max(0.25, Math.min(...minutes)))) : 1;
      const high = minutes.length ? Math.min(...NICE.filter(tick => tick >= Math.max(...minutes)), 1000) : 10;
      const span = Math.log(high / low) || 1;
      const share = value => (Math.log(Math.min(high, Math.max(low, value))) - Math.log(low)) / span;
      const ticks = NICE.filter(tick => tick >= low && tick <= high);
      const W = 300, H = 26, x = value => 6 + share(value) * (W - 12);
      function strip(one) {
        const runs = one.gateSeconds || [];
        if (runs.length < 3) return h("span", { class: "sub" }, runs.length ? QK.count(runs.length, "passing gate") : "No passing gate yet");
        const sorted = runs.slice().sort((a, b) => a - b), median = sorted[Math.floor(sorted.length / 2)];
        return svg("svg", { class: "strip-plot", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
          "aria-label": one.name + ": median gate " + QK.span(median) + " over " + QK.count(runs.length, "passing run") },
          svg("title", null, "Median " + QK.span(median) + " · fastest " + QK.span(sorted[0]) + " · slowest " + QK.span(sorted[sorted.length - 1])
            + " · last " + QK.count(runs.length, "passing run")),
          svg("line", { class: "base", x1: 0, x2: W, y1: H / 2, y2: H / 2 }),
          ticks.map(tick => svg("line", { class: "tick", x1: x(tick), x2: x(tick), y1: 4, y2: H - 4 })),
          runs.map(seconds => svg("circle", { class: "run", cx: x(seconds / 60), cy: H / 2, r: 3.5 })),
          svg("line", { class: "median", x1: x(median / 60), x2: x(median / 60), y1: 2, y2: H - 2 }));
      }
      // The minutes each tick stands for, above the strips, placed as the strips place them.
      function axis() {
        let last = -100;  // a label closer than this many percent to the last one shown is left out
        return h("div", { class: "strip-axis", "aria-hidden": "true" }, ticks.map(tick => {
          const at = 100 * (6 + share(tick) * (W - 12)) / W;
          if (at - last < 9) return null;
          last = at;
          const label = h("span", null, tick < 1 ? Math.round(tick * 60) + "s" : tick >= 60 && tick % 60 === 0 ? tick / 60 + "h" : tick + "m");
          label.style.left = at + "%";
          return label;
        }));
      }
      return h("div", { class: "chart-scroll" }, h("table", { class: "repo-table" },
        h("thead", null, h("tr", null, ["Repository", "Landed", "Came back", "Flaky", "Rounds", "Median gate"].map(name => h("th", null, name)),
          h("th", { class: "strip-head" }, "Passing gates", minutes.length ? axis() : null))),
        h("tbody", null, rows.map(one => one.error ? h("tr", null, h("td", null, tag(one.name)), h("td", { class: "warning wrap", colspan: 6 }, "Cannot be read: " + one.error))
          : h("tr", null, h("td", null, h("a", { href: QK.href(one.name, "history") }, tag(one.name))),
            h("td", null, h("b", null, one.today.landed), " ", (found => found ? h("span", { class: "change " + found.tone, title: found.title }, found.text) : null)(
              change(one.today.landed, one.yesterday.landed, "more"))),
            h("td", { class: one.today.cameBack ? "danger" : null }, one.today.cameBack),
            h("td", null, one.today.flaky), h("td", null, one.today.rounds),
            h("td", null, typeof one.today.medianGateSeconds === "number" ? QK.span(one.today.medianGateSeconds) : "–"),
            h("td", { class: "strip-cell" }, strip(one)))))));
    }
    // The last 7 or 30 days, day by day, each measure against the same number of days before: what landed each day,
    // the share of what settled that came back, gate time as a share of its usual, and flaky tests found. A gate's
    // usual is its own repository's median, so every repository's runs can be set together.
    function period() {
      const state = context.state, span = state.span || 7, days = data.daily || [];
      const now = days.slice(-span), before = days.slice(-2 * span, -span);
      const sum = (list, key) => list.reduce((total, one) => total + one[key], 0);
      const rate = list => { const back = sum(list, "cameBack"), settled = back + sum(list, "landed"); return settled ? 100 * back / settled : null; };
      const median = values => { if (!values.length) return null; const sorted = values.slice().sort((a, b) => a - b); return sorted[Math.floor(sorted.length / 2)]; };
      const share = list => median(list.flatMap(one => one.gateShares));
      // The change, its arrow by direction and good or bad by whether lower is better.
      function moved(value, then, lower, said) {
        if (typeof value !== "number" || typeof then !== "number") return null;
        const by = value - then;
        if (Math.abs(by) < 0.005 * Math.max(1, Math.abs(then))) return { text: "same", tone: "flat", title: "The same as the " + span + " days before" };
        return { text: (by > 0 ? "▲ " : "▼ ") + said(Math.abs(by)), tone: (by < 0) === lower ? "good" : "bad", title: "Against the " + span + " days before" };
      }
      const perDay = (list, key) => list.length ? sum(list, key) / list.length : null;
      const landed = perDay(now, "landed"), backRate = rate(now), gate = share(now), flaky = sum(now, "flaky");
      const daily = (values, options) => QK.spark(values, { title: "Each of the last " + span + " days, oldest first", ...options });
      const last = now.length ? now[now.length - 1] : null;
      return h("div", { class: "card" },
        h("h2", null, h("span", null, "Over the last " + span + " days", h("span", { class: "muted" }, " against the " + span + " before")),
          h("span", { class: "segmented", role: "group", "aria-label": "Period" }, [7, 30].map(length => h("button", { type: "button",
            "aria-pressed": String(span === length), onclick: () => { state.span = length; context.refresh(); } }, length + " days")))),
        h("div", { class: "numbers" },
          QK.number("Landed per day", landed === null ? "–" : String(Math.round(landed)), daily(now.map(one => one.landed), { bars: true, tone: "good" }),
            sum(now, "landed") + " in all", moved(landed, perDay(before, "landed"), false, value => String(Math.round(value)))),
          QK.number("Came back", backRate === null ? "–" : Math.round(backRate) + "%",
            daily(now.map(one => one.landed + one.cameBack ? 100 * one.cameBack / (one.landed + one.cameBack) : 0), { tone: "danger" }),
            "of what settled", moved(backRate, rate(before), true, value => Math.round(value) + " pts")),
          QK.number("Gate time", gate === null ? "–" : Math.round(100 * gate) + "%", daily(now.map(one => share([one]) === null ? 0 : 100 * share([one]))),
            all ? "of each repository's usual" : "of usual" + (last && last.medianGateSeconds ? " · today " + QK.span(last.medianGateSeconds) : ""),
            moved(gate === null ? null : 100 * gate, share(before) === null ? null : 100 * share(before), true, value => Math.round(value) + " pts")),
          QK.number("Flaky tests", String(flaky), daily(now.map(one => one.flaky), { bars: true, tone: "warning" }), "found",
            moved(flaky, sum(before, "flaky"), true, value => String(Math.round(value))))));
    }

    const runs = data.gateSeconds || [];
    QK.fill(element,
      period(),
      all ? h("div", { class: "card" }, h("h2", null, "By repository", h("span", { class: "muted" }, "Today, against yesterday at this time")),
        repositories()) : null,
      h("div", { class: "card" }, h("h2", null, "Lane runs", all ? h("span", { class: "chips", role: "group", "aria-label": "Color the runs by" },
          [["repository", "By repository"], ["status", "By result"]].map(([value, label]) => h("button", { type: "button",
            "aria-pressed": String((value === "repository") === byRepository),
            onclick: () => { context.state.laneColor = value; context.refresh(); } }, label)))
          : h("span", { class: "muted" }, "Today, by Mac")), swimlanes(),
        (data.gates || []).length ? h("div", { class: "legend sub" }, byRepository
          ? [...new Set(data.gates.map(run => run.repository))].sort((a, b) => a.localeCompare(b)).map(name =>
              h("span", null, h("i", { class: "dot " + (hues[name] || "other") }), name)).concat(
              h("span", null, h("i", { class: "key failed" }), "failed"), h("span", null, h("i", { class: "key unmeasured" }), "unmeasured"))
          : [h("span", null, h("i", { class: "dot good" }), "passed"),
          h("span", null, h("i", { class: "dot danger" }), "failed"), h("span", null, h("i", { class: "dot unknown" }), "unmeasured")],
          h("span", null, "Today, by Mac. Thin bars are lanes other than the gate." + (all ? " Every repository's runs share their Macs." : "") + " A bar opens its log.")) : null),
      all ? h("div", { class: "card" }, h("h2", null, "When work lands", h("span", { class: "muted" }, "Every repository, by day and hour")), heatmap())
        : h("div", { class: "columns" },
          h("div", { class: "card" }, h("h2", null, "When work lands", h("span", { class: "muted" }, "By day and hour")), heatmap()),
          h("div", { class: "card" }, h("h2", null, "Gate duration", h("span", { class: "muted" }, runs.length ? "Last " + QK.count(runs.length, "passing run") : "")), durations())));
  },
});
