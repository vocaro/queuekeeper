"""Give this Mac and another Mac of the farm the same simulator runtimes of the Xcode at this Mac's developer
directory, for `qk remote setup --runtimes` (queuekeeper.plugins.REMOTE_SETUP_RUNTIMES) and `qk doctor`
(doctor.fix_hosts).

The plan and its order are the Apple platform's: which runtime each Mac lacks, whether it is deleted where nothing
uses it, copied as its disk image, or downloaded by its build, and how much free space each must leave. Core
queuekeeper.remote keeps the transport: what each Mac lists (toolchains, runtime_images), the lock that keeps a
second change from starting (runtime_lock), the transport that sends and fetches a file over SSH (ask, send_file,
fetch_file) and the download (download_runtime). The runtime images' own reads and changes, on either Mac, are this
plugin's: runtime_host.py, a standalone host program that core remote.ask sends whole to another Mac and that this
Mac imports (runtime_images, delete_runtime, send_runtime, fetch_runtime).

An older core, from before that transport (older_core), lists, deletes and copies the images itself, and this module
then asks it to."""
import os
from pathlib import Path
import re
import shlex

from queuekeeper import offload_host, processes, remote
from . import runtime_host

# How messages name this Mac beside another Mac.
HERE = remote.HERE
# Free bytes a runtime's copy or download must leave on the volume that receives it: room for simctl
# to copy the image once more where it cannot clone it, as its help says it may, and for the 8-14 GB
# building worktrees of the sessions that share the volume.
RUNTIME_MARGIN = 40 << 30
# The bytes a download is taken to need when no image says: more than the largest runtime image on
# any Mac, iOS 26.5's 8.5 GB (October 2026).
RUNTIME_ESTIMATE = 10 << 30
# The platforms whose runtimes `xcodebuild -downloadPlatform` downloads (Xcode 27.2's help).
DOWNLOADABLE = {"iOS", "watchOS", "tvOS", "visionOS"}
# A runtime as offload_host.toolchain names it: NAME (BUILD).
RUNTIME = re.compile(r"^(.*) \(([^()]+)\)$")


# The host program another Mac runs for runtime_images, delete_runtime and send_runtime.
PROGRAM = Path(runtime_host.__file__)


def older_core():
    """Whether the core predates send_file and the rest of the transport this module's images use, and answers for
    the images itself (remote.runtime_images and the rest; offload_host.delete_runtime)."""
    return not hasattr(remote, "send_file")


def runtime_images(host, side, developer, clear=False):
    """A Mac's runtime images, the free space where a copy to it is staged, and what an earlier copy
    left staged there, which clear removes: runtime_host.images and runtime_host.stage on side. Each side
    is given the builds the toolkit pins (remote.runtime_pins), which keep their users there though superseded."""
    if older_core(): return remote.runtime_images(host, side, developer, clear)
    if side == HERE:
        return {"images": runtime_host.images(developer, remote.runtime_pins()),
                **runtime_host.stage(Path.home() / remote.LOCAL_RUNTIME_STAGING, clear)}
    return remote.ask(host, {"images": developer, "staging": remote.RUNTIME_STAGING, "clear": clear,
                             "pinned": remote.runtime_pins()}, program=PROGRAM)


def delete_runtime(host, side, developer, build):
    """Delete build's runtime image on side as runtime_host.delete_runtime does, given the builds the toolkit pins.
    Return None once it is deleted, else why not."""
    pins = remote.runtime_pins()
    if side == HERE:
        return (offload_host if older_core() else runtime_host).delete_runtime(developer, build, pins)
    return remote.ask(host, {"deleteRuntime": {"developer": developer, "build": build, "pinned": pins}},
                      **({} if older_core() else {"program": PROGRAM})).get("problem")


def send_runtime(host, image, developer, report):
    """Send this Mac's image of a runtime to host, where runtime_host.receive stages it and simctl adds
    it. Return None once it is added there, else why not."""
    if older_core(): return remote.send_runtime(host, image, developer, report)
    request = {"receive": {"staging": remote.RUNTIME_STAGING, "build": image["build"], "size": image["sizeBytes"],
                           "developerDir": developer}}
    answer, failure = remote.send_file(host, PROGRAM, request, image["file"], report)
    return failure or answer.get("failed")


def fetch_runtime(host, image, developer, report):
    """Fetch host's image of a runtime over SSH, then stage and add it here as runtime_host.receive does
    there. Return None once it is added here, else why not."""
    if older_core(): return remote.fetch_runtime(host, image, developer, report)
    staging = Path.home() / remote.LOCAL_RUNTIME_STAGING
    return remote.fetch_file(host, image["file"], lambda stream: runtime_host.receive(
        stream, staging, image["build"], image["sizeBytes"], developer, report))


def gib(size):
    return f"{size / (1 << 30):.1f} GiB"


def runtime_differences(here, there, host):
    """What only this Mac's and only host's list of runtimes names, as `qk doctor` says it."""
    only = lambda side, other: [runtime for runtime in side if runtime not in other]
    return [f"only {name} has {', '.join(runtimes)}" for name, runtimes in
            [(HERE, only(here, there)), (host, only(there, here))] if runtimes]


def runtime_plan(found, stores, host):
    """A step for each runtime that only one Mac lists: the runtime, its build, the Mac that has it
    (source), the one that lacks it (target), and how the two come to match: a delete from the source
    when no simulator but Xcode's defaults uses it there, so a runtime nobody uses costs no Mac
    its space (none uses a build its identifier no longer resolves to, runtime_host.images), else a
    copy of the source's image to the target when that can be read, else a download there by its
    build, else None, with why."""
    plan = []
    for source, target in ((HERE, host), (host, HERE)):
        for runtime in sorted(set(found[source]["runtimes"]) - set(found[target]["runtimes"])):
            matched = RUNTIME.match(runtime)
            name, build = matched.groups() if matched else (runtime, None)
            step = {"runtime": runtime, "build": build, "source": source, "target": target}
            held = [image for image in stores[target]["images"] if image["build"] == build]
            images = [image for image in stores[source]["images"] if image["build"] == build]
            readable = [image for image in images if image["file"]]
            platform = (name.split() or [""])[0]
            why = images[0]["unreadable"] if images else f"{source} lists no disk image of it"
            if build is None:
                step.update(how=None, why="its name gives no build")
            elif len(images) == 1 and images[0].get("users") == []:
                step.update(how="delete", size=images[0]["sizeBytes"], supersededBy=images[0].get("supersededBy"))
            elif held:
                step.update(how=None, why=f"{target} holds its image ({held[0]['state']}), which the Xcode there does "
                            "not list as available; see `xcrun simctl runtime list` there")
            elif readable:
                step.update(how="copy", image=readable[0], size=readable[0]["sizeBytes"])
            elif platform in DOWNLOADABLE:
                known = images and isinstance(images[0]["sizeBytes"], int)
                step.update(how="download", why=why, platform=platform,
                            size=images[0]["sizeBytes"] if known else RUNTIME_ESTIMATE,
                            architectures=images[0]["architectures"] if images else [])
                # Core remote.download_runtime runs it on the target.
                step["command"] = download_command(step)
            else:
                step.update(how=None, why=f"{why}, and xcodebuild downloads no {platform} runtime")
            plan.append(step)
    return plan


def reporter(runtime, size):
    """A copy's progress: a line at each tenth of the image. A requested stop ends the copy."""
    shown = [0]
    def report(done):
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"while copying {runtime}")
        if done - shown[0] >= size / 10 or done == size:
            shown[0] = done
            print(f"runtimes: {runtime}: {gib(done)} of {gib(size)} copied", flush=True)
    return report


def download_command(step):
    """xcodebuild's download of step's runtime by its build, for the architectures of its image."""
    architectures = set(step["architectures"])
    variant = ["-architectureVariant", "arm64"] if architectures == {"arm64"} else \
        ["-architectureVariant", "universal"] if {"arm64", "x86_64"} <= architectures else []
    return ["xcodebuild", "-downloadPlatform", step["platform"], "-buildVersion", step["build"], *variant]


def match_runtimes(host, check):
    """Bring this Mac and host to the union of the simulator runtimes that the Xcode at this Mac's
    developer directory lists, as `qk doctor` compares them, and return 0 once both list the same.

    A runtime only one Mac lists that no simulator but Xcode's defaults uses there is deleted from
    that Mac (the owner's decision, 2026-11-03). Any other goes to another Mac as its own disk
    image, over SSH: staged there, and added with `simctl runtime add --move`, so no second copy
    stays. One whose image cannot be read is downloaded there with xcodebuild, by its build. A copy or
    download that would leave less than RUNTIME_MARGIN free where it lands is refused before it
    starts. Then each Mac's runtimes are listed again, and what still differs is named. With
    check, say what it would do and change nothing."""
    developer = remote.developer_directory()
    if developer is None:
        print("runtimes: this Mac has no Xcode selected, so there are no runtimes to compare", flush=True)
        return 1
    lock = None
    if not check:
        lock = remote.runtime_lock()
        if lock is None:
            print("runtimes: another `qk remote setup --runtimes` is running on this Mac; run it again once that "
                  "one ends", flush=True)
            return 1
    try:
        with processes.forwarding_signals():
            return bring_runtimes(host, developer, check)
    except remote.Misconfigured as problem:
        print(f"runtimes: {problem}", flush=True)
        return 1
    finally:
        if lock is not None: os.close(lock)


def bring_runtimes(host, developer, check):
    """match_runtimes' work, once this Mac holds the lock that a change takes."""
    found = remote.toolchains(host, developer)
    version = lambda text: ", ".join((text or "no Xcode").splitlines())
    if not found[HERE]["xcode"]:
        print(f"runtimes: the Xcode at {developer} does not run on this Mac", flush=True)
        return 1
    if found[HERE]["xcode"] != found[host].get("xcode"):
        there = version(found[host]["xcode"]) if found[host].get("xcode") else "missing"
        print(f"runtimes: the Xcode at {developer} is {version(found[HERE]['xcode'])} on this Mac and {there} on "
              f"{host}; install this Mac's Xcode at that path there first", flush=True)
        return 1
    xcode = f"{version(found[HERE]['xcode'])} at {developer}"
    stores = {side: runtime_images(host, side, developer, clear=not check) for side in (HERE, host)}
    for side in (HERE, host):
        if found[side].get("runtimes") is None or stores[side]["images"] is None:
            print(f"runtimes: simctl did not list the runtimes on {side}", flush=True)
            return 1
        if stores[side]["left"] and not check:
            print(f"runtimes: removed {', '.join(stores[side]['left'])}, which an earlier copy left staged on {side}",
                  flush=True)
    plan = runtime_plan(found, stores, host)
    if not plan:
        runtimes = found[HERE]["runtimes"]
        print(f"runtimes: this Mac and {host} list the same {len(runtimes)} runtimes of {xcode}: "
              f"{', '.join(runtimes) or 'none'}", flush=True)
        return 0
    planned = {HERE: 0, host: 0}
    for step in plan:
        runtime, source, target = step["runtime"], step["source"], step["target"]
        if processes.requested() is not None:
            raise processes.Interrupted(processes.requested(), f"before {runtime} came to {target}")
        if step["how"] is None:
            print(f"runtimes: {runtime}: cannot come to {target}: {step['why']}", flush=True)
            continue
        if step["how"] == "delete":
            heading = f"runtimes: {runtime}, {gib(step['size']) if isinstance(step['size'], int) else 'unknown size'}:"
            unused = "no simulator but Xcode's defaults uses it" + (
                f": its identifier's simulators run {step['supersededBy']}" if step.get("supersededBy") else "")
            if check:
                print(f"{heading} would be deleted from {source}, where {unused}", flush=True)
                continue
            print(f"{heading} deleting from {source}, where {unused}", flush=True)
            failure = delete_runtime(host, source, developer, step["build"])
            print(f"runtimes: {runtime}: deleted from {source}" if failure is None else
                  f"runtimes: {runtime}: not deleted from {source}: {failure}", flush=True)
            continue
        size = step["size"]
        if check:
            free = stores[target]["free"] - planned[target]
            planned[target] += size
        else:
            # Measured again before each copy, which those before it have used space for.
            fresh = runtime_images(host, target, developer)
            if any(image["build"] == step["build"] for image in fresh["images"] or []):
                print(f"runtimes: {runtime}: {target} holds it now", flush=True)
                continue
            free = fresh["free"]
        heading = f"runtimes: {runtime}, {gib(size)}:"
        if free - size < RUNTIME_MARGIN:
            print(f"{heading} {'would be refused' if check else 'refused'} on {target}: {gib(free)} is free there, "
                  f"and {gib(size)} more would leave less than the {gib(RUNTIME_MARGIN)} it must keep free; free space "
                  "there, then run it again", flush=True)
            continue
        download = (f"with `{shlex.join(download_command(step))}`, since its image on {source} cannot be copied: "
                    f"{step['why']}") if step["how"] == "download" else None
        if check and download is None:
            print(f"{heading} would be copied from {source} to {target}, which has {gib(free)} free", flush=True)
        elif check:
            print(f"{heading} would be downloaded on {target}, which has {gib(free)} free, {download}", flush=True)
        elif download is None:
            print(f"{heading} copying from {source} to {target}", flush=True)
            copy = send_runtime if target == host else fetch_runtime
            failure = copy(host, step["image"], developer, reporter(runtime, size))
            print(f"runtimes: {runtime}: added on {target}" if failure is None else
                  f"runtimes: {runtime}: not added on {target}: {failure}; nothing of it stays staged there", flush=True)
        else:
            print(f"{heading} downloading on {target} {download}", flush=True)
            status = remote.download_runtime(host, step, developer)
            print(f"runtimes: {runtime}: {'downloaded' if status == 0 else f'xcodebuild exited {status}'} on {target}",
                  flush=True)
    if check:
        print(f"runtimes: {len(plan)} runtime(s) differ; `qk remote setup --runtimes` or `qk doctor` evens them out",
              flush=True)
        return 1
    # What the Macs list now decides the outcome, not what each step reported.
    found = remote.toolchains(host, developer)
    for side in (HERE, host):
        if found[side].get("runtimes") is None:
            print(f"runtimes: simctl did not list the runtimes again on {side}", flush=True)
            return 1
    here, there = found[HERE]["runtimes"], found[host]["runtimes"]
    if here == there:
        print(f"runtimes: this Mac and {host} list the same {len(here)} runtimes of {xcode}: {', '.join(here)}",
              flush=True)
        return 0
    print(f"runtimes: the runtimes of {xcode} still differ: {'; '.join(runtime_differences(here, there, host))}",
          flush=True)
    return 1
