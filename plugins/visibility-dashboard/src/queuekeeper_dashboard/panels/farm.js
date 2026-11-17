QK.panel("farm", {
  render(element, data, context) {
    const h = QK.h;
    if (data.admission) {
      const one = data.admission, origin = one.origin || {};
      const elapsed = one.state === "waiting" ? one.waitedSeconds : one.heldSeconds;
      const kind = one.kind || (one.exclusive ? "exclusive" : one.wholeHost ? "wholeHost" : null);
      const facts = [["Host", one.host], ["Status", one.state === "waiting" ? "Waiting" : "Running"],
        ["Elapsed", typeof elapsed === "number" ? QK.span(elapsed) : null],
        ["Admission", { ordinary: "Shared", wholeHost: "Whole host", exclusive: "Exclusive" }[kind]],
        ["Process", one.process], ["Process ID", one.pid], ["Priority position", one.position],
        ["Slots", Array.isArray(one.slots) ? one.slots.join(", ") : null],
        ["CPU", typeof one.cpuPercent === "number" ? one.cpuPercent.toFixed(1) + "%" : null],
        ["Repository", origin.repository], ["Branch", origin.branch], ["Worktree", origin.worktree],
        ["Session", origin.session], ["Origin Mac", origin.mac]];
      return QK.fill(element,
        h("p", null, h("a", { href: QK.href(context.repository, "farm") }, "‹ Back to Farm")),
        h("div", { class: "card hero" }, h("h1", null, "Admission details"), h("p", { class: "mono wrap" }, one.id)),
        one.state === "missing" ? h("div", { class: "card" },
          h("p", null, "This request is no longer in the latest farm reading. It may have finished, withdrawn, or moved to another Mac.")) : [
          h("div", { class: "card" }, h("h2", null, "Recorded label"), h("pre", { class: "log" }, one.label || "Unnamed work")),
          one.state === "waiting" ? h("div", { class: "card" }, h("h2", null, "Why it is waiting"),
            h("p", { class: "wrap" }, one.reason || "No wait reason was recorded."),
            one.stale ? h("p", { class: "sub warning" }, "This observation is stale"
              + (typeof one.sampledSeconds === "number" ? ": recorded " + QK.span(one.sampledSeconds) + " ago." : ".")) : null,
            one.away ? h("p", { class: "sub" }, "The request is away; its queue place is retained.") : null) : null,
          one.stranded ? h("div", { class: "banner warning" }, "This admission is stranded and remains counted until recovered.") : null,
          h("div", { class: "card" }, h("h2", null, "Details"), h("dl", { class: "facts" },
            facts.filter(([, value]) => value !== null && value !== undefined && value !== "").flatMap(([label, value]) =>
              [h("dt", { class: "muted" }, label), h("dd", { class: "wrap" }, value)]))),
        ]);
    }
    const samples = data.samples || [];
    // One hue per project across the page, from the slots held now and over the last hour.
    const hues = QK.hues([...(data.hosts || []).flatMap(found => [...(found.cells || []).map(cell => cell.project),
      ...found.holders.map(one => one.project), ...found.waiters.map(one => one.project)]),
      ...samples.flatMap(one => Object.keys(one.projects || {}).filter(name => name !== "other"))]);
    const hue = project => project ? hues[project] || "other" : "other";
    function work(one, host, running) {
      const content = [h("i", { class: "dot " + hue(one.project), title: one.project || "No project recorded" }),
        h("span", { class: "work-label" },
        h("span", { class: "ellipsis work-title" }, one.label || "Unnamed work"),
        h("span", { class: "sub mono wrap work-id" }, [one.project, one.id || "ID unavailable"].filter(Boolean).join(" · ")),
        !running && one.reason ? h("span", { class: "sub wrap warning work-why" }, "Waiting: " + one.reason) : null),
        h("span", { class: "muted" }, typeof (running ? one.heldSeconds : one.waitedSeconds) === "number"
          ? QK.span(running ? one.heldSeconds : one.waitedSeconds) : "–")];
      return h("li", null, one.id ? h("a", { class: "row", href: QK.href(context.repository, "farm", {
        host: host.role, admission: one.id }) }, ...content) : content);
    }
    // What admission judges the Mac by: its CPU's busy share as a gauge with its limit, a share at or above which
    // opens no further slot; its load over the last hour; and load, memory and screen as chips, amber when they hold
    // work back.
    function load(found, role, where) {
      if (!found) return null;
      if (found.error) return h("p", { class: "sub wrap" }, "Its load could not be read: " + found.error);
      const busy = typeof found.cpuBusy === "number", over = busy && typeof found.busyLimit === "number"
        && found.cpuBusy >= found.busyLimit;
      const loads = samples.map(one => (one.macs[role] || {}).load).filter(value => typeof value === "number");
      return [
        h("div", { class: "macgrid" },
          busy ? QK.gauge(found.cpuBusy, found.busyLimit, "CPU busy" + (typeof found.busyLimit === "number"
            ? " · limit " + Math.round(found.busyLimit) + "%" : "")) : h("p", { class: "sub" }, "CPU busy share not read"),
          h("div", { class: "macside" },
            loads.length > 1 ? [h("span", { class: "label" }, "Load, last " + QK.span(samples[samples.length - 1].at - samples[0].at)),
              QK.spark(loads, { tone: over ? "warning" : null, title: "Load average at each reading, oldest first" })] : null,
            h("div", { class: "signals" },
              typeof found.load === "number" ? h("span", { class: "pill" + (over ? " flag" : "") }, "load " + found.load.toFixed(1)) : null,
              found.memory && !found.shortage ? h("span", { class: "pill" + (found.memory === "normal" ? "" : " flag") }, "memory " + found.memory) : null,
              h("span", { class: "pill" + (found.screen === "locked" ? " flag" : ""), title: found.screen === "locked"
                ? "Lanes that need an unlocked screen wait" : null },
                found.screen === "locked" ? "screen locked" : found.screen === "unlocked" ? "screen unlocked" : "screen not read")))),
        found.shortage ? shortage(found.shortage, where, found.health) : found.health ? h("p", { class: "sub wrap warning" }, found.health) : null,
      ];
    }
    // A Mac short of memory: its swap against its total with macOS's pressure, its largest processes by memory,
    // and what to free; the whole warning, as qk doctor says it, is the panel's title.
    function shortage(found, where, said) {
      const swap = found.swap, largest = found.largest || [], most = Math.max(1, ...largest.map(one => one.residentBytes));
      const free = (found.free || []).map(one => one.restarted
        ? h("span", null, "Run ", h("code", null, "sudo killall " + one.name), " on " + where + " to free " + QK.size(one.residentBytes)
          + "; launchd starts it again, so nothing is lost.")
        : h("span", null, "Quit " + one.name + " to free " + QK.size(one.residentBytes) + ", or restart " + where + "."));
      return h("div", { class: "shortage", title: said || null },
        h("p", { class: "lead" }, h("b", null, "Short of memory"), h("span", { class: "sub" }, "Runs slow, and a lane's tests may time out unreached")),
        swap ? h("div", { class: "swap" }, h("span", { class: "label" }, "Swap"),
          h("span", { class: "tnum" }, QK.size(swap.usedBytes) + " of " + QK.size(swap.totalBytes) + " · " + Math.round(100 * swap.share) + "%"),
          h("span", { class: "pill flag" }, "pressure " + swap.pressure),
          h("span", { class: "meter", role: "img", "aria-label": Math.round(100 * swap.share) + "% of swap used" },
            h("i", { class: "fill share" + Math.min(10, Math.round(10 * swap.share)) }))) : null,
        largest.length ? [h("span", { class: "label" }, "Largest processes"),
          h("ul", { class: "procs" }, largest.map(one => h("li", { class: one.outsized ? "outsized" : null,
            title: "PID " + one.pid + (one.outsized ? ", more than a quarter of this Mac's memory" : "") },
            h("span", { class: "ellipsis" }, one.name),
            h("span", { class: "meter" }, h("i", { class: "fill share" + Math.max(1, Math.round(10 * one.residentBytes / most)) })),
            h("span", { class: "tnum" }, QK.size(one.residentBytes)),
            h("span", { class: "sub tnum" }, typeof one.seconds === "number" ? "up " + QK.span(one.seconds) : ""))))] : null,
        free.length ? h("p", { class: "free" }, free) : null,
        h("p", { class: "sub" }, "Queuekeeper stops nothing itself, since that takes the owner's password."));
    }
    // A Mac's or device's icon, by its kind, with a badge for its state: good, bad (faded), warning or reserved.
    const BADGES = { good: "✓", bad: "✕", warning: "!", reserved: "" };
    function icon(kind, badge, label) {
      return h("span", { class: "device-icon" + (badge === "bad" ? " off" : ""), role: "img", "aria-label": label },
        h("img", { src: "/static/device-" + (["laptop", "desktop", "iphone", "ipad"].includes(kind) ? kind : "other") + ".svg", alt: "" }),
        badge ? h("span", { class: "badge " + badge, "aria-hidden": "true" }, BADGES[badge]) : null);
    }
    function macBadge(found) {
      const load = found.load || {};
      if (found.state !== "observed" || load.error) return "bad";
      const over = typeof load.cpuBusy === "number" && typeof load.busyLimit === "number" && load.cpuBusy >= load.busyLimit;
      return over || ["warning", "critical"].includes(load.memory) || load.health || load.screen === "locked" ? "warning" : "good";
    }
    // Each slot as a cell, filled in its project's hue when held and underlined when guaranteed.
    function cells(found) {
      return h("div", { class: "slots" }, (found.cells || []).map(cell => h("span", {
        class: "slot" + (cell.held ? " held " + hue(cell.project) : "") + (cell.guaranteed ? " always" : ""),
        title: (cell.held ? (cell.project || "Work with no project") + " holds this slot" : "Free")
          + (cell.guaranteed ? ", always open" : "") })));
    }
    function host(found) {
      const slots = found.slots, read = found.state === "observed";
      const hardware = (found.load || {}).hardware || {}, kinds = { laptop: "laptop", desktop: "desktop" };
      return h("div", { class: "card mac" },
        h("div", { class: "machead" }, icon(hardware.kind, macBadge(found), (kinds[hardware.kind] || "Mac") + ", " + macBadge(found)),
          h("div", { class: "macname" }, h("h2", null, found.name, read ? null : h("span", { class: "warning" }, found.state)),
            hardware.model ? h("span", { class: "sub mono" }, hardware.model) : null),
          read ? h("span", { class: "muted slotcount" }, slots.used + " of " + slots.most + " slots") : null),
        read ? [cells(found), h("div", { class: "sub" }, slots.guaranteed ? slots.guaranteed + " always open, underlined" : null)]
          : found.reasons.map(reason => h("p", { class: "sub wrap" }, reason)),
        load(found.load, found.role, found.role === "here" ? "this Mac" : found.name),
        found.holders.length ? [h("h3", { class: "label" }, "Running"), h("ul", { class: "rows pairs work" },
          found.holders.map(one => work(one, found, true)))] : null,
        found.waiters.length ? [h("h3", { class: "label" }, "Waiting"), h("ul", { class: "rows pairs work" },
          found.waiters.map(one => work(one, found, false)))] : null,
        read && !found.holders.length && !found.waiters.length ? h("p", { class: "sub" }, "Nothing is running or waiting.") : null);
    }
    // The farm's slots in use by project over the last hour, against how many slots the read Macs have.
    function usage() {
      if (samples.length < 2) return null;
      const times = samples.map(one => one.at), names = [...new Set(samples.flatMap(one => Object.keys(one.projects || {})))]
        .sort((a, b) => (a === "other") - (b === "other") || a.localeCompare(b));
      const most = Math.max(...samples.map(one => one.most || 0));
      const layers = names.map(name => ({ name, hue: name === "other" ? "other" : hue(name),
        values: samples.map(one => (one.projects || {})[name] || 0) }));
      const covered = times[times.length - 1] - times[0];
      return h("div", { class: "card" }, h("h2", null, "Slots in use", h("span", { class: "muted" },
          "Last " + QK.span(covered) + " · " + QK.count(most, "slot") + " across the farm")),
        QK.stack(times, layers, most, Math.max(covered, 60), 120),
        h("div", { class: "axis sub" }, h("span", null, QK.span(covered) + " ago"), h("span", null, "now")),
        names.length ? h("div", { class: "legend sub" }, layers.map(layer => h("span", null, h("i", { class: "dot " + layer.hue }),
          layer.name === "other" ? "No project recorded" : layer.name))) : h("p", { class: "sub" }, "No slot was held."),
        h("p", { class: "sub" }, "Read while this page is open, so a stretch with it closed is left blank."));
    }
    const record = data.deviceRecord;
    const connectionNames = { connected: "Connected", disconnected: "Disconnected", connecting: "Connecting",
      unavailable: "Unavailable", notDetected: "Not detected", unknown: "Connection unknown" };
    const transportNames = { localNetwork: "Network", network: "Network", wired: "USB", usb: "USB" };
    const reservationNames = { released: "Unreserved", held: "Reserved", reserved: "Reserved" };
    QK.fill(element, data.hosts ? h("div", { class: "columns" }, data.hosts.map(host))
        : h("div", { class: "card" }, h("h2", null, "Macs"),
          h("p", { class: data.error ? "danger wrap" : "muted" }, data.error || "Reading the Macs…")),
      usage(),
      h("div", { class: "card" }, h("h2", null, "iOS devices",
        record && record.checkedAt ? h("span", { class: "muted" }, "Checked " + QK.ago(record.checkedAt)) : null),
        h("p", { class: "sub" }, "Connection status on this Mac and reservations for physical devices."),
        !record ? h("p", { class: "sub" }, "Reading device reservations…") : [
          record.error ? h("p", { class: "danger wrap" }, record.error) : null,
          record.connectionError ? h("p", { class: "warning sub wrap" }, "Connection status could not be checked: " + record.connectionError) : null,
          record.deferredTo ? h("p", { class: "warning wrap" }, "Physical-device reservations are managed on " + record.deferredTo + ". Open its dashboard to see those reservations.") : null,
          record.devices.length ? h("ul", { class: "rows pairs devices" }, record.devices.map(device => h("li", null,
            icon(device.kind, device.state === "needs recovery" || device.connection === "connecting" ? "warning"
              : ["notDetected", "disconnected", "unavailable"].includes(device.connection) ? "bad"
              : device.state !== "released" ? "reserved" : device.connection === "connected" ? "good" : null,
              (device.model || device.kind || "device") + ", " + (connectionNames[device.connection] || "connection unknown")),
            h("span", null, h("b", null, device.name),
              h("div", { class: "sub wrap" }, [device.model, device.purpose, device.owner ? "Owner: " + device.owner : null,
                device.releaseBy ? "Expected release: " + device.releaseBy : null,
                device.waiting ? QK.count(device.waiting, "recorded waiter") : null].filter(Boolean).join(" · "))),
            h("span", { class: "device-status" },
              h("span", { class: "pill " + (device.connection || "unknown"), title: "Connection status reported by Xcode" },
                (connectionNames[device.connection] || "Connection unknown")
                + (device.connection === "connected" && transportNames[device.transport] ? " · " + transportNames[device.transport] : "")),
              h("span", { class: "pill", title: "Reservation status" }, reservationNames[device.state] || device.state))))) : !record.error && !record.deferredTo
              ? h("p", { class: "sub" }, "No physical devices recorded yet. Devices appear after a reservation with qk devices take or qk devices reserve.") : null,
        ]),
      // The icons' license, CC BY 4.0, asks for credit and a link (THIRD_PARTY_NOTICES.md).
      h("p", { class: "sub credit" }, "Device icons by ", h("a", { href: "https://streamlinehq.com", rel: "noopener" }, "Streamline"),
        ", ", h("a", { href: "https://creativecommons.org/licenses/by/4.0/", rel: "noopener license" }, "CC BY 4.0"), "."),
      data.readAt ? h("p", { class: "sub" }, "Every project's work on each Mac. Read " + QK.ago(data.readAt) + "."
        + (data.error ? " The last reading failed: " + data.error : "")) : null);
  },
});
