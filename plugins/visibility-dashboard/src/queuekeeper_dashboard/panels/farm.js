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
    function work(one, host, running) {
      const content = [h("span", { class: "work-label" },
        h("span", { class: "ellipsis work-title" }, one.label || "Unnamed work"),
        h("span", { class: "sub mono wrap work-id" }, one.id || "ID unavailable")),
        h("span", { class: "muted" }, typeof (running ? one.heldSeconds : one.waitedSeconds) === "number"
          ? QK.span(running ? one.heldSeconds : one.waitedSeconds) : "–")];
      return h("li", null, one.id ? h("a", { class: "row", href: QK.href(context.repository, "farm", {
        host: host.role, admission: one.id }) }, ...content) : content);
    }
    // What admission judges the Mac by. A share at or above its limit opens no further slot.
    function load(found) {
      if (!found) return null;
      if (found.error) return h("p", { class: "sub wrap" }, "Its load could not be read: " + found.error);
      const busy = typeof found.cpuBusy === "number", over = busy && typeof found.busyLimit === "number"
        && found.cpuBusy >= found.busyLimit;
      const facts = [typeof found.load === "number" ? "load " + found.load.toFixed(1) : null,
        found.memory ? "memory " + found.memory : null].filter(Boolean).join(" · ");
      return [
        busy ? h("div", { class: "bar" }, h("i", { class: over ? "over" : null, width: Math.min(100, Math.round(found.cpuBusy)) + "%" })) : null,
        h("div", { class: "sub" + (over || found.memory === "critical" ? " warning" : "") },
          (busy ? "CPU " + Math.round(found.cpuBusy) + "% busy" + (typeof found.busyLimit === "number"
            ? ", limit " + Math.round(found.busyLimit) + "%" : "") : "CPU busy share not read") + (facts ? " · " + facts : "")),
        h("div", { class: "sub" + (found.screen === "locked" ? " warning" : "") },
          found.screen === "locked" ? "Screen locked: lanes that need an unlocked screen wait"
            : found.screen === "unlocked" ? "Screen unlocked" : "Screen state not read"),
        found.health ? h("p", { class: "sub wrap warning" }, found.health) : null,
      ];
    }
    function host(found) {
      const slots = found.slots, read = found.state === "observed";
      return h("div", { class: "card" },
        h("h2", null, found.name, read ? null : h("span", { class: "warning" }, found.state)),
        read ? [
          h("div", { class: "bar" }, h("i", { width: Math.round(100 * slots.used / Math.max(1, slots.most)) + "%" })),
          h("div", { class: "sub" }, slots.used + " of " + slots.most + " slots in use"
            + (slots.guaranteed ? ", " + slots.guaranteed + " always open" : "")),
        ] : found.reasons.map(reason => h("p", { class: "sub wrap" }, reason)),
        load(found.load),
        found.holders.length ? [h("h3", { class: "label" }, "Running"), h("ul", { class: "rows pairs work" },
          found.holders.map(one => work(one, found, true)))] : null,
        found.waiters.length ? [h("h3", { class: "label" }, "Waiting"), h("ul", { class: "rows pairs work" },
          found.waiters.map(one => work(one, found, false)))] : null,
        read && !found.holders.length && !found.waiters.length ? h("p", { class: "sub" }, "Nothing is running or waiting.") : null);
    }
    const record = data.deviceRecord;
    const connectionNames = { connected: "Connected", disconnected: "Disconnected", connecting: "Connecting",
      unavailable: "Unavailable", notDetected: "Not detected", unknown: "Connection unknown" };
    const transportNames = { localNetwork: "Network", network: "Network", wired: "USB", usb: "USB" };
    const reservationNames = { released: "Unreserved", held: "Reserved", reserved: "Reserved" };
    QK.fill(element, data.hosts ? h("div", { class: "columns" }, data.hosts.map(host))
        : h("div", { class: "card" }, h("h2", null, "Macs"),
          h("p", { class: data.error ? "danger wrap" : "muted" }, data.error || "Reading the Macs…")),
      h("div", { class: "card" }, h("h2", null, "iOS devices",
        record && record.checkedAt ? h("span", { class: "muted" }, "Checked " + QK.ago(record.checkedAt)) : null),
        h("p", { class: "sub" }, "Connection status on this Mac and reservations for physical devices."),
        !record ? h("p", { class: "sub" }, "Reading device reservations…") : [
          record.error ? h("p", { class: "danger wrap" }, record.error) : null,
          record.connectionError ? h("p", { class: "warning sub wrap" }, "Connection status could not be checked: " + record.connectionError) : null,
          record.deferredTo ? h("p", { class: "warning wrap" }, "Physical-device reservations are managed on " + record.deferredTo + ". Open its dashboard to see those reservations.") : null,
          record.devices.length ? h("ul", { class: "rows pairs" }, record.devices.map(device => h("li", null,
            h("span", null, h("b", null, device.name),
              h("div", { class: "sub wrap" }, [device.purpose, device.owner ? "Owner: " + device.owner : null,
                device.releaseBy ? "Expected release: " + device.releaseBy : null,
                device.waiting ? QK.count(device.waiting, "recorded waiter") : null].filter(Boolean).join(" · "))),
            h("span", { class: "device-status" },
              h("span", { class: "pill " + (device.connection || "unknown"), title: "Connection status reported by Xcode" },
                (connectionNames[device.connection] || "Connection unknown")
                + (device.connection === "connected" && transportNames[device.transport] ? " · " + transportNames[device.transport] : "")),
              h("span", { class: "pill", title: "Reservation status" }, reservationNames[device.state] || device.state))))) : !record.error && !record.deferredTo
              ? h("p", { class: "sub" }, "No physical devices recorded yet. Devices appear after a reservation with qk devices take or qk devices reserve.") : null,
        ]),
      data.readAt ? h("p", { class: "sub" }, "Every project's work on each Mac. Read " + QK.ago(data.readAt) + "."
        + (data.error ? " The last reading failed: " + data.error : "")) : null);
  },
});
