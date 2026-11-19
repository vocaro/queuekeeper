"""A lane's "chips", and where it puts the lane.

    "gate": {"command": ["./check.sh", "--gates"], "resource": "exclusive", "chips": ["apple-m5-max"]}

The lane runs only on a Mac whose chip it lists. When every Mac has one it is placed as any lane is. When some
have, it is placed among those, and waits for them while they are busy; it never runs on another. When none has, it
is refused, and `qk doctor` says so beforehand. `qk host chip` prints a Mac's chip as a lane names
it."""
from queuekeeper import plugins, farm
from queuekeeper.config import unadmitted
from . import chip

KEY = "chips"


def reading():
    """(this Mac's chip, {the SSH destination of each of the farm's Macs that take work now: its chip or None}), the
    farm's Macs in their order."""
    return chip.here(), {host: chip.there(host) for host in chip.others()}


def named(mine, theirs):
    """The words that say what each Mac has, the farm's by their names."""
    unread = "a chip that could not be read"
    here = f"this Mac has {mine or unread}"
    if not theirs: return f"{here}, and the farm has no other Mac"
    return ", and ".join([here, *(f"{farm.named(host)} has {found or unread}" for host, found in theirs.items())])


class Chips(plugins.LaneKey):
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
        mine, theirs = reading()
        macs = {host for host, found in theirs.items() if found in wanted} | ({plugins.HERE} if mine in wanted else set())
        if len(macs) == len(theirs) + 1: return None
        listed = f"its chips are {', '.join(wanted)}; {named(mine, theirs)}"
        if not macs: raise ValueError(f"no Mac has a chip the lane lists: {listed}")
        return macs, listed


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
    """Print this Mac's chip and each of the farm's Macs', as a lane names them; exit 2 when this Mac's cannot be
    read."""
    mine, theirs = reading()
    out(f"this Mac: {mine or 'could not be read'}")
    for host, found in theirs.items(): out(f"{farm.named(host)}: {found or 'could not be read'}")
    return 0 if mine else 2


def register():
    """Add the key, its doctor check and the host read to the core."""
    if not isinstance(plugins.LANE_KEYS.get(KEY), Chips): plugins.LANE_KEYS[KEY] = Chips()
    if warnings not in plugins.DOCTOR: plugins.DOCTOR.append(warnings)
    plugins.HOST_READS["chip"] = (
        "print this Mac's chip and each of the farm's Macs', as a lane's \"chips\" names them, such as apple-m5-max",
        arguments, read)
