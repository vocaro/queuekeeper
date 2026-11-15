# The Mac hardware plugin

The Mac hardware plugin keeps a lane on the chip its results are valid on. Most lanes give the same
result on any Mac and need nothing from it. A lane whose output differs by GPU, such as one that
compares rendered images byte for byte, lists the chips it has results for.

Switch it on in `queuekeeper.json` and give such a lane its chips:

```json
{
  "extensions": ["queuekeeper_mac_hardware"],
  "lanes": {
    "gate": {"command": ["./check.sh", "--gates"], "resource": "exclusive", "chips": ["apple-m5-max"]}
  }
}
```

A lane without `chips` is placed as before, on whichever Mac has room.

## Where a lane with chips runs

| The lane's chips match | What happens |
|---|---|
| Both Macs | The lane is placed as any other |
| Only this Mac | It runs here, as a lane that says `here` does, and waits here while this Mac is busy |
| Only the second Mac | It is sent there and waits there for its admission. It never runs here, and `--here` is refused |
| Neither Mac | The run is refused, naming each Mac's chip. `qk doctor` warns of it beforehand |

A lane group runs on one Mac, so it goes where its restricted lane must. A group whose lanes must
run on different Macs is refused.

If the second Mac cannot be reached, its chip is unread. A lane that matches this Mac then runs
here, and a lane that matches only the second Mac cannot run until it is back.

## Naming a chip

A chip is named as macOS names it, in lowercase with hyphens: "Apple M5 Max" is `apple-m5-max`. On
Apple silicon the chip is the CPU and the GPU both. Names match exactly, so `apple-m5` does not
match `apple-m5-max`.

```bash
python3 scripts/qk host chip
```

This prints this Mac's chip and the second Mac's, as a lane writes them.

## How it fits the core

The core knows nothing of chips. A lane key a plugin adds may say where its lane must run
(`LaneKey.placement` in `extensions.py`): on this Mac, only on the second Mac, or either. This
plugin's `chips` key answers from the two Macs' chips. Another plugin can restrict a lane the same
way for its own reason.

## Limits

- The merge queue's wait before it retries a lane does not know that a lane runs only on the second
  Mac, so it may also wait for this Mac to settle.
- The chip says nothing of the graphics driver or the macOS version, which can also change rendered
  output.
