"""Where a system alert sits among the screen's windows, from the window list's front-to-back order and frames
(offload_host.ORDER_SCRIPT). The window list reports a window behind five others as on screen just as it does one
in front, as on build-host on 2026-11-12, where the owner at that Mac saw no alert (vocaro/queuekeeper#14). Kept out
of offload_host, which travels to another Mac in each SSH command."""

import json

# Windows at this layer or above are the Dock's, the menu bar's and the like: the Dock's spans the whole screen
# and hides nothing. An app's window, floating or modal, sits below it.
COVERING_LAYER = 20


def windows(found):
    """The screen's windows front to back, each as {"app", "layer", "frame"}, from found, what
    offload_host.ORDER printed; None when it cannot be read."""
    try: listed = json.loads(found)
    except (TypeError, ValueError): return None
    if not isinstance(listed, list): return None
    read = []
    for window in listed:
        try: read.append({"app": str(window["app"]), "layer": int(window["layer"]),
                          "frame": [int(float(value)) for value in window["frame"]]})
        except (KeyError, TypeError, ValueError): continue
    return read


def _overlap(frame, other):
    x, y = max(frame[0], other[0]), max(frame[1], other[1])
    right, bottom = min(frame[0] + frame[2], other[0] + other[2]), min(frame[1] + frame[3], other[1] + other[3])
    return (x, y, right, bottom) if right > x and bottom > y else None


def _covered_area(rectangles):
    """The area the union of rectangles, each (left, top, right, bottom), covers."""
    xs = sorted({edge for rectangle in rectangles for edge in (rectangle[0], rectangle[2])})
    ys = sorted({edge for rectangle in rectangles for edge in (rectangle[1], rectangle[3])})
    area = 0
    for left, right in zip(xs, xs[1:]):
        for top, bottom in zip(ys, ys[1:]):
            if any(r[0] <= left and right <= r[2] and r[1] <= top and bottom <= r[3] for r in rectangles):
                area += (right - left) * (bottom - top)
    return area


def placement(alert, windows):
    """Where alert sits among windows, the screen's windows front to back: {"state": "front", "partly" or
    "hidden", "coveredBy": the owners of the windows in front of it that overlap it, front first}, or None when
    the list does not show it."""
    if not windows: return None
    frame = alert["frame"]
    index = next((index for index, window in enumerate(windows)
                  if window["app"] == alert["app"] and window["frame"] == frame), None)
    if index is None: return None
    over = [(window["app"], _overlap(frame, window["frame"])) for window in windows[:index]
            if window["layer"] < COVERING_LAYER]
    over = [(app, rectangle) for app, rectangle in over if rectangle]
    if not over: return {"state": "front", "coveredBy": []}
    hidden = _covered_area([rectangle for _app, rectangle in over]) >= frame[2] * frame[3]
    return {"state": "hidden" if hidden else "partly", "coveredBy": [app for app, _rectangle in over]}


def placed(placing):
    """What placing, an alert's placement, says of where it sits, or an empty text when it is not known."""
    if not placing: return ""
    if placing["state"] == "front": return "; it is in front of every other window"
    count = len(placing["coveredBy"])
    named = f"{count} window{'s' if count != 1 else ''}: {', '.join(placing['coveredBy'])}"
    if placing["state"] == "hidden": return f"; it is fully hidden behind {named}, so it cannot be seen there"
    return f"; it is partly hidden behind {named}"


def place_alerts(alerts, found):
    """alerts, each with its placement among the screen's windows added when found, what offload_host.ORDER
    printed, shows it."""
    listed = windows(found) if alerts else None
    placed_alerts = []
    for alert in alerts or []:
        where = placement(alert, listed)
        placed_alerts.append({**alert, "placement": where} if where else alert)
    return placed_alerts if alerts else alerts
