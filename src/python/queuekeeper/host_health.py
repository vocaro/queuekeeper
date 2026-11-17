"""A Mac short of memory, named with what holds it and what the owner can do (offload_host.host_health reads it).

On 2026-11-15 another Mac's fseventsd held 34 of its 64 GB after 15 days, swap was 31.7 of 32 GB and the
one-minute load reached 720, so a gate timed out with 2235 tests passed, none failed and 375 never reached.
Admission reads memory pressure, but nothing named the process or said how to free it. `qk doctor` and the Farm tab
now warn when swap is nearly full while macOS reports memory pressure, or one process holds an outsized share of
physical memory, naming the largest processes. Any process can be the cause, so the check names none in advance; a
system daemon that launchd starts again is only told apart in what the warning suggests. Nothing here stops a
process: that takes the owner's password.

Swap near its total says nothing by itself: macOS keeps swap in files, /private/var/vm/swapfile* (dynamic_pager(8)),
so vm.swapusage's total is not fixed and follows what is used. On 2026-11-15 a healthy 36 GB Mac, with 45% of its
memory free and its pressure normal, had 5.7 of 7.0 GB of swap used and was warned of, and later that day its total
was 6.0 GB (vocaro/queuekeeper#168).
"""

# Swap used, as a share of its total, at or above which a Mac under memory pressure is short of memory.
SWAP_SHARE = 0.8
# The memory pressure levels, of kern.memorystatus_vm_pressure_level, at which swap counts, each with its name:
# the system memory pressure condition that dispatch_source_create(3) describes for
# DISPATCH_SOURCE_TYPE_MEMORYPRESSURE, whose dispatch/source.h gives normal as 1, warning as 2 and critical as 4.
PRESSED = {2: "warning", 4: "critical"}
# One process's resident memory, as a share of physical memory, above which it holds an outsized share.
PROCESS_SHARE = 0.25
# How many of the largest processes a warning names.
NAMED = 3
# System daemons launchd starts again when they end, which `sudo killall NAME` frees at once, losing nothing.
RESTARTED = frozenset({"fseventsd", "mds_stores", "mds"})


def size(count):
    """Bytes as GB with one decimal, or MB below a GB."""
    return f"{count / 1024 ** 3:.1f} GB" if count >= 1024 ** 3 else f"{count / 1024 ** 2:.0f} MB"


def uptime(seconds):
    """Seconds as the largest two units, as `15d 2h` or `3m`."""
    if seconds is None: return "for an unknown time"
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = [f"{value}{unit}" for value, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if value]
    return " ".join(parts[:2]) or "under a minute"


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def shortage(reading):
    """What makes a Mac short of memory, as data, or None: its memory, its swap (used, total, its share used and
    the pressure level's name) when swap is at or over SWAP_SHARE of its total while macOS reports memory pressure
    at a PRESSED level, else None; its largest NAMED processes by resident memory, each with its share of memory
    and whether it holds an outsized one (over PROCESS_SHARE); and what to free, each named with whether launchd
    starts it again. host_health reading, reading: a reading that is missing or unread says nothing, and neither
    does swap whose reading has no pressure level, as one from an older toolkit."""
    if not isinstance(reading, dict): return None
    memory, used, total = (number(reading.get(key)) for key in ("memoryBytes", "swapUsedBytes", "swapTotalBytes"))
    largest = [one for one in reading.get("largest") or [] if isinstance(one, dict)
               and number(one.get("pid")) is not None and number(one.get("residentBytes")) is not None
               and isinstance(one.get("name"), str)]
    largest.sort(key=lambda one: -one["residentBytes"])
    level = number(reading.get("memoryLevel"))
    swapping = bool(total) and used is not None and used >= SWAP_SHARE * total and level in PRESSED
    outsized = [one for one in largest if memory and one["residentBytes"] > PROCESS_SHARE * memory]
    if not (swapping or outsized): return None
    shown = lambda one: {"name": one["name"], "pid": one["pid"], "residentBytes": one["residentBytes"],
                         "seconds": number(one.get("seconds")), "share": one["residentBytes"] / memory if memory else None,
                         "outsized": one in outsized, "restarted": one["name"] in RESTARTED}
    # What to free: each process over its share, or with swap alone nearly full under pressure, the largest.
    return {"memoryBytes": memory,
            "swap": {"usedBytes": used, "totalBytes": total, "share": used / total, "pressure": PRESSED[level]} if swapping else None,
            "largest": [shown(one) for one in largest[:NAMED]], "free": [shown(one) for one in outsized or largest[:1]]}


def assessed(reading, where):
    """(diagnosis, remedy) for a Mac whose host_health reading, reading, shows it short of memory (shortage), or
    None. where names the Mac, as `this Mac` or its host."""
    found = shortage(reading)
    if not found: return None
    swap, memory, reasons = found["swap"], found["memoryBytes"], []
    if swap:
        level = next(key for key, name in PRESSED.items() if name == swap["pressure"])
        reasons.append(f"swap is {size(swap['usedBytes'])} of {size(swap['totalBytes'])} used ({round(100 * swap['share'])}%) "
                       f"while macOS reports memory pressure at level {level} ({swap['pressure']})")
    reasons += [f"{one['name']} holds {size(one['residentBytes'])} of its {size(memory)} of memory "
                f"({round(100 * one['share'])}%)" for one in found["largest"] if one["outsized"]]
    named = ", ".join(f"{one['name']} (PID {one['pid']}, up {uptime(one['seconds'])}, {size(one['residentBytes'])})"
                      for one in found["largest"])
    diagnosis = (f"{where} is short of memory, so its runs slow and a lane's tests may time out unreached: "
                 f"{'; '.join(reasons)}. Its largest processes by resident memory are {named or 'unread'}")
    advice = []
    for one in found["free"]:
        if one["restarted"]:
            advice.append(f"{one['name']} is a system daemon that launchd starts again, so the owner can run "
                          f"`sudo killall {one['name']}` on {where}, which is safe and frees its memory at once")
        else:
            advice.append(f"the owner can quit {one['name']} (PID {one['pid']}) on {where}, or restart {where} if "
                          "it is no app they can quit")
    remedy = ("; ".join(advice) if advice else f"the owner can quit what holds it, or restart {where}") \
        + ". Queuekeeper stops nothing itself, since that takes the owner's password"
    return diagnosis, remedy


def warnings(reading, where):
    """qk doctor's warning for a Mac short of memory (assessed), or none."""
    found = assessed(reading, where)
    return [f"{found[0]}. {found[1][0].upper()}{found[1][1:]}"] if found else []
