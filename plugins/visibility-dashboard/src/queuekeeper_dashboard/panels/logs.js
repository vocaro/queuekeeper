QK.panel("logs", {
  // Once a log is chosen, each request asks for what was written after the last chunk.
  query(context) {
    const state = context.state;
    if (!state.name) return context.query.name ? { name: context.query.name } : {};
    return state.next === undefined ? { name: state.name } : { name: state.name, offset: state.next };
  },
  render(element, data, context) {
    const h = QK.h, state = context.state, LIMIT = 2 << 20;
    if (!state.built) {
      state.built = true;
      state.picker = h("select", { "aria-label": "Log", onchange: () => choose(state.picker.value) });
      state.follow = h("input", { type: "checkbox", checked: true });
      state.note = h("p", { class: "sub" });
      state.text = h("pre", { class: "log" });
      QK.fill(element, h("div", { class: "card" },
        h("div", { class: "controls" }, state.picker, h("label", null, state.follow, " Follow")), state.text, state.note));
    }
    function choose(name) {
      state.name = name;
      state.next = undefined;
      state.text.textContent = "";
      history.replaceState(null, "", QK.href(context.repository, "logs", { name }));
      context.refresh();
    }
    const logs = data.logs || [], names = logs.map(log => log.name).join("\n");
    if (names !== state.names) {
      state.names = names;
      QK.fill(state.picker, logs.map(log => h("option", { value: log.name },
        log.name + " · " + QK.size(log.size) + " · " + QK.ago(log.modified))));
      if (state.name) state.picker.value = state.name;
    }
    if (!logs.length && !state.name) { state.note.textContent = "The queue has no logs yet."; return; }
    if (!state.name) return choose(context.query.name || logs[0].name);
    if (data.name !== state.name) return;
    if (state.next !== undefined && data.offset !== state.next) return;
    if (state.next === undefined && data.offset > 0) state.text.textContent = "… the earlier " + QK.size(data.offset) + " is not shown …\n";
    if (data.text) {
      state.text.append(data.text);
      if (state.text.textContent.length > LIMIT) state.text.textContent = state.text.textContent.slice(-LIMIT / 2);
      if (state.follow.checked) state.text.scrollTop = state.text.scrollHeight;
    }
    state.next = data.next;
    state.note.textContent = QK.size(data.size) + (data.next < data.size ? ", reading…" : ", up to date");
    if (data.next < data.size) context.refresh();
  },
});
