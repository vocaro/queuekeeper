"""Which simulators an admitted command's words name, which keep it on this Mac: a pool simulator is this Mac's
alone, so another Mac cannot run the command as it runs here (queuekeeper.plugins.COMMAND_KEPT_HERE)."""
import re

# A simulator's identifier, a UUID, as a word of uppercased text: a member of this Mac's pool.
SIMULATOR = re.compile(r"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}")
# A destination's id= key, as `-destination id=UUID` or `platform=iOS Simulator,id=UUID` gives it, at the
# start of a word or after a comma or `=` of the destination: not the end of another key, such as --build-id=.
DESTINATION = re.compile(rf"(?:\A|[,=])ID=({SIMULATOR.pattern})(?=\Z|,)")
# What ends one command of a shell's words, after which a UUID no longer follows simctl.
SEPARATOR = re.compile(r"[;&|\n()]+")


def simulators(command):
    """The simulator identifiers command names, uppercased: a UUID where a simulator goes, as a destination's
    id=, any word after `simctl` in the same shell command, or --device's argument. One inside a path, such
    as an interpreter in a session's scratchpad (#116, #142), or in another word or argument names none."""
    found = set()
    for part in SEPARATOR.split(" ".join(map(str, command)).upper()):
        words = [word.strip("'\"") for word in part.split()]
        simctl = False
        for previous, word in zip([None, *words], words):
            found.update(DESTINATION.findall(word))
            value = word.removeprefix("--DEVICE=") if previous != "--DEVICE" else word
            if SIMULATOR.fullmatch(value) and (simctl or previous == "--DEVICE" or value != word):
                found.add(value)
            simctl = simctl or word == "SIMCTL" or word.endswith("/SIMCTL")
    return found


def kept_here(command):
    """Why another Mac cannot run command as it runs here because it names a simulator, or None."""
    return "it names a simulator by its identifier, a member of this Mac's pool" if simulators(command) else None
