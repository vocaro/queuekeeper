"""Which of the journal's events the owner is told of, and what each notice says.

A rule is on or off by itself: RULES gives each one's default, and the `rules` object of the feature's settings
turns one on or off by its name. Adding a rule changes this file alone."""
URGENT, NORMAL = "urgent", "normal"
# name -> (on unless the settings say otherwise, what it tells of).
RULES = {
    "held-owner": (True, "the queue stopped for a cause only its owner can fix"),
    "held-session": (False, "the queue stopped for a cause an agent fixes"),
    "came-back": (True, "an entry failed its gate, or conflicts with main"),
    "stuck": (True, "a gate is held up, or the queue's doctor finds the queue stuck"),
    "flaky": (True, "a failed test passed alone"),
    "ungated": (True, "an entry landed without the gate"),
    "landed": (False, "an entry landed"),
    "resumed": (False, "a hold was cleared"),
}
BODY_LIMIT = 300


def chosen(settings):
    """The names of the rules that are on."""
    stated = settings.get("rules") if isinstance(settings.get("rules"), dict) else {}
    return {name for name, (default, _) in RULES.items() if (stated.get(name) if isinstance(stated.get(name), bool)
                                                              else default)}


def candidates(event):
    """The rules that would tell of event, the most particular first: the first of them that is on does."""
    kind = event.get("kind")
    if kind == "queue.held": return ["held-owner" if event.get("for") == "owner" else "held-session"]
    if kind in ("entry.failed", "entry.conflict"): return ["came-back"]
    if kind == "test.flaky": return ["flaky"]
    if kind == "gate.stuck": return ["stuck"]
    if kind == "entry.landed": return ["ungated", "landed"] if event.get("ungated") else ["landed"]
    if kind == "queue.resumed": return ["resumed"]
    return []


def rule_of(event, on):
    return next((name for name in candidates(event) if name in on), None)


def finding_notice(finding, repository):
    """The notice of a finding of the queue's doctor (merge_queue.diagnose) that has lasted (check): what it found,
    then what would heal it."""
    return {"rule": "stuck", "level": URGENT, "title": "Queue looks stuck", "repository": repository, "event": None,
            "body": cut(f"{finding.get('diagnosis')}. Next: {finding.get('remedy')}"), "finding": finding}


def cut(text):
    text = " ".join(str(text).split())
    return text if len(text) <= BODY_LIMIT else text[:BODY_LIMIT - 1] + "…"


def notice(event, on, repository):
    """The notice of event when a rule in on tells of it, else None: its rule, level, title and body, the repository
    whose queue it is and the event itself."""
    rule = rule_of(event, on)
    if rule is None: return None
    subject = event.get("subject") or event.get("entry") or "an entry"
    why = event.get("why") or "no reason was recorded"
    if rule == "held-owner": title, body, level = "Queue held for you", why, URGENT
    elif rule == "held-session": title, body, level = "Queue held for an agent", why, NORMAL
    elif rule == "came-back":
        title = "Entry has a conflict" if event["kind"] == "entry.conflict" else "Entry came back"
        body, level = f"{subject}: {why}", URGENT
    elif rule == "stuck": title, body, level = "Gate is stuck", f"{event.get('lane') or 'A lane'} is {why}", URGENT
    elif rule == "flaky":
        title, level = "Flaky test", NORMAL
        body = (f"{event.get('test') or 'A test'} failed its gate and passed alone"
                + (", so its batch landed" if event.get("landed") else ", and its batch was bisected"))
    elif rule == "ungated":
        said = event["ungated"].get("why") if isinstance(event.get("ungated"), dict) else None
        title, body, level = "Landed without the gate", subject + (f": {said}" if said else ""), NORMAL
    elif rule == "landed":
        title, body, level = "Landed", f"{subject} as {str(event.get('landedAs') or '?')[:12]}", NORMAL
    else: title, body, level = "Queue resumed", "Its hold was cleared", NORMAL
    return {"rule": rule, "level": level, "title": title, "body": cut(body), "repository": repository, "event": event}
