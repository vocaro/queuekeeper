"""Settle Git conflicts that only add members to a configured JSON object."""
import json
import re
from dataclasses import dataclass

# JSON whitespace, rather than all of Python's Unicode whitespace.
SPACE = re.compile(r"[ \t\r\n]*")


@dataclass(frozen=True)
class Number:
    """Compare and retain a number's spelling without rounding it through a float."""
    token: str


def unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def invalid_constant(value):
    raise ValueError(f"{value} is not a JSON number")


DECODER = json.JSONDecoder(object_pairs_hook=unique, parse_int=Number, parse_float=Number,
                           parse_constant=invalid_constant)


def selected(document, keys):
    """The selected object and the document with that object replaced by None."""
    if not isinstance(document, dict):
        raise ValueError("expected a JSON object")
    if not keys:
        return document, None
    key, *rest = keys
    members, remainder = selected(document[key], rest)
    return members, {**document, key: remainder}


def member_ranges(source, keys):
    """The opening brace and each member's (key start, value start, value end) offsets."""
    opened = SPACE.match(source).end()
    for key in [*keys, None]:
        cursor = SPACE.match(source, opened + 1).end()
        members = {}
        while source[cursor] != "}":
            name, end = DECODER.raw_decode(source, cursor)
            value = SPACE.match(source, SPACE.match(source, end).end() + 1).end()
            _, end = DECODER.raw_decode(source, value)
            members[name] = cursor, value, end
            cursor = SPACE.match(source, end).end()
            if source[cursor] != ",":
                break
            cursor = SPACE.match(source, cursor + 1).end()
        if key is not None:
            opened = members[key][1]
    return opened, members


def merge(base, ours, theirs, keys):
    """(merged bytes, disputed key), or (None, None) for changes beyond distinct additions."""
    try:
        original, carried, incoming = (source.decode("utf-8") for source in (base, ours, theirs))
        (before, outside), (ahead, ahead_outside), (added, added_outside) = (
            selected(DECODER.decode(source), keys) for source in (original, carried, incoming))
        for key in added:
            if key not in before and key in ahead and ahead[key] != added[key]:
                return None, key
        if outside != ahead_outside or outside != added_outside:
            return None, None
        if any(key not in ahead or key not in added or ahead[key] != value or added[key] != value
               for key, value in before.items()):
            return None, None
        new = added.keys() - ahead.keys()
        if not new:
            return ours, None
        opened, existing = member_ranges(carried, keys)
        new_opened, incoming_ranges = member_ranges(incoming, keys)
        # Keep the ahead side byte for byte; append raw members in incoming order. Reuse the
        # object's leading whitespace so a compact or CRLF file keeps its style.
        raw = [incoming[start:end] for key, (start, _, end) in incoming_ranges.items() if key in new]
        if existing:
            first = next(iter(existing.values()))[0]
            end = next(reversed(existing.values()))[2]
            separator = "," + carried[opened + 1:first]
            content = separator + separator.join(raw)
        else:
            end = opened + 1
            first = next(iter(incoming_ranges.values()))[0]
            gap = incoming[new_opened + 1:first]
            content = gap + ("," + gap).join(raw)
        return (carried[:end] + content + carried[end:]).encode("utf-8"), None
    except (KeyError, RecursionError, ValueError):
        return None, None


def same(first, second):
    """Whether Git's result carries the same valid document as the add-only merge."""
    try:
        return DECODER.decode(first.decode("utf-8")) == DECODER.decode(second.decode("utf-8"))
    except (RecursionError, ValueError):
        return False
