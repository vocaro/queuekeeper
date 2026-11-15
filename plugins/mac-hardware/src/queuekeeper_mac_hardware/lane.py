"""A lane's "chips", and where it puts the lane.

    "gate": {"command": ["./check.sh", "--gates"], "resource": "exclusive", "chips": ["apple-m5-max"]}

The lane runs only on a Mac whose chip it lists. When both Macs have one it is placed as any lane is. When one
has, it runs there and waits for that Mac while it is busy; it never runs on the other. When neither has, it is
refused, and `qk doctor` says so beforehand. `qk host chip` prints a Mac's chip as a lane names
it."""
from queuekeeper import extensions
from queuekeeper.config import unadmitted
from . import chip

KEY = "chips"


def reading():
    """(this Mac's chip, the second Mac's SSH name or None, its chip or None)."""
    host = chip.second_mac()
    return chip.here(), host, chip.there(host) if host else None


def named(mine, host, theirs):
    """The words that say what each Mac has."""
    here = f"this Mac has {mine or 'a chip that could not be read'}"
    if not host: return f"{here}, and there is no second Mac"
    return f"{here}, and {host} has {theirs or 'a chip that could not be read'}"


class Chips(extensions.LaneKey):
    def check(self, name, lane):
        chips = lane[KEY]
        if not isinstance(chips, list) or not chips or any(
                not isinstance(entry, str) or not chip.NAME.fullmatch(entry) for entry in chips):
            raise ValueError(f"{name}.{KEY} must be a non-empty list of chips, such as [\"apple-m5-max\"], as "
                             "`qk host chip` prints them")
        if unadmitted(lane):
            raise ValueError(f"{name}.{KEY} needs an admitted lane: queuekeeper places only an admitted lane on a Mac, "
                             "and a self lane runs where it starts")

    def placement(self, lane):
        wanted = lane[KEY]
        mine, host, theirs = reading()
        if mine in wanted and theirs in wanted: return None
        listed = f"its chips are {', '.join(wanted)}; {named(mine, host, theirs)}"
        if mine in wanted: return extensions.HERE, listed
        if theirs in wanted: return extensions.THERE, listed
        raise ValueError(f"no Mac has a chip the lane lists: {listed}")


def warnings(root, config):
    """`qk doctor`'s warning for each lane no Mac can run."""
    key, found = Chips(), []
    for name, lane in sorted(config.get("lanes", {}).items()):
        if not isinstance(lane, dict) or KEY not in lane: continue
        try: key.placement(lane)
        except ValueError as error: found.append(f"{name}.{KEY}: {error}, so the lane cannot run")
    return found


def arguments(parser):
    pass


def read(args, out=print):
    """Print this Mac's chip and the second Mac's, as a lane names them; exit 2 when this Mac's cannot be read."""
    mine, host, theirs = reading()
    out(f"this Mac: {mine or 'could not be read'}")
    if host: out(f"{host}: {theirs or 'could not be read'}")
    return 0 if mine else 2


def register():
    """Add the key, its doctor check and the host read to the core."""
    if not isinstance(extensions.LANE_KEYS.get(KEY), Chips): extensions.LANE_KEYS[KEY] = Chips()
    if warnings not in extensions.DOCTOR: extensions.DOCTOR.append(warnings)
    extensions.HOST_READS["chip"] = (
        "print this Mac's chip and the second Mac's, as a lane's \"chips\" names them, such as apple-m5-max",
        arguments, read)
