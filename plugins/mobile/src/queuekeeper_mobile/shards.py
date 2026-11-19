"""A run's tests spread over several simulators or emulators by how long each unit took the last time.

A unit is what one shard runs whole: a test class or suite. Its seconds come from the last run that recorded them,
and a unit with none counts the median of those that have one. Units go longest first into the shard with the
least so far (longest processing time first), which keeps the longest shard close to the shortest; the order
breaks ties by name, so a run with the same timings packs the same way. iOS simulator tests pack -only-testing
arguments this way (queuekeeper_apple.simulators.matrix) and Android emulator tests their classes
(queuekeeper_android.emulator_tests).
"""
import json
from pathlib import Path


def packed(units, seconds, count):
    """count lists of the names in units, an iterable, packed by seconds, {name: seconds}."""
    units = list(dict.fromkeys(units))
    known = sorted(seconds[name] for name in units if name in seconds)
    guess = known[len(known) // 2] if known else 1.0
    bins = [[0.0, []] for _ in range(max(count, 1))]
    for name in sorted(units, key=lambda name: (-seconds.get(name, guess), name)):
        emptiest = min(bins, key=lambda entry: entry[0])
        emptiest[0] += seconds.get(name, guess)
        emptiest[1].append(name)
    return [names for _, names in bins]


def read_timings(path):
    """{unit: seconds} kept at path, or none when there is no such file or it cannot be read."""
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return {name: float(seconds) for name, seconds in value.items()
            if isinstance(name, str) and isinstance(seconds, (int, float))} if isinstance(value, dict) else {}


def keep_timings(path, seconds):
    """Merge seconds into the timings kept at path, the newest run's over the older."""
    if not seconds: return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**read_timings(path), **seconds}, indent=1, sort_keys=True) + "\n")
