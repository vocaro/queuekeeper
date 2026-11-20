"""A build plan that names a Metal Toolchain mount macOS has since replaced is the Mac's fault, not the commits'.

macOS mounts the Metal Toolchain from a cryptex, at /var/run/com.apple.security.cryptexd/mnt/
com.apple.MobileAsset.MetalToolchain-VERSION.SUFFIX/, and mounts it again with a new random suffix. A build folder
reused from before keeps the old path in its build plan (Intermediates.noindex/XCBuildData), and every .metal file
then fails with "unable to spawn process '<old mount>/Metal.xctoolchain/usr/bin/metal' (No such file or directory)"
although the toolchain is installed. On 2026-11-19 a StoreUILib worker failed a one-line configuration change so,
and would have failed every later gate until its build folders were removed by hand (vocaro/queuekeeper#281).

The build wrappers (stale_modules.main) read the failed spawn in a build's output, remove each build plan inside the
checkout that names the vanished mount, and run the build once more. A build that still fails so, or whose plan is
not inside the checkout, is recorded in the lane's results as FAULT; the lane then ends unmeasured, not failed
(host_fault, queuekeeper.plugins.HOST_FAULTS).

Standard library only: it starts with every build a lane runs.
"""
import os
from pathlib import Path
import re

# The words of a failed spawn, which the wrappers watch the build's output for.
SPAWN = "unable to spawn process"
MISSING = re.compile(r"unable to spawn process '([^']+)' \(No such file or directory\)")
# Where cryptexd mounts what it serves, the Metal Toolchain among them; a mount's folder is the part after it.
MOUNTS = "/com.apple.security.cryptexd/mnt/"
INTERMEDIATES = "Intermediates.noindex"
PLAN = "XCBuildData"
# Folders a build plan is never inside, passed over while looking for one.
PASSED_OVER = frozenset({".git", "node_modules", "Products", "SourcePackages", "Logs", "Index.noindex", "checkouts",
                         "repositories", "artifacts"})
NOTICE = "queuekeeper: STALE TOOLCHAIN"
RECORD = "stale-toolchain.log"
FAULT = "toolchain-fault.txt"
ISSUE = "vocaro/queuekeeper#281"


def mounts(text):
    """The cryptex mounts that the tools text failed to spawn, for want of the file, were in, each once in order."""
    found = []
    for tool in MISSING.findall(text):
        at = tool.find(MOUNTS)
        if at < 0: continue
        name = tool[at + len(MOUNTS):].split("/", 1)[0]
        mount = tool[:at + len(MOUNTS)] + name
        if name and mount not in found: found.append(mount)
    return found


def names(folder, words):
    """Whether a file in folder holds any of words, which are bytes."""
    for directory, _, files in os.walk(folder):
        for file in files:
            try: content = (Path(directory) / file).read_bytes()
            except OSError: continue
            if any(word in content for word in words): return True
    return False


def plans(root, gone):
    """Each build plan folder inside root, an Intermediates.noindex/XCBuildData, whose files name one of the mounts
    gone. A mount is matched by its own folder's name, which holds the random suffix, so that /private/var and /var
    spellings of it are one. Links are not followed."""
    words = [Path(mount).name.encode() for mount in gone]
    found = []
    for directory, folders, _ in os.walk(root):
        if Path(directory).name == INTERMEDIATES:
            plan = Path(directory) / PLAN
            if plan.is_dir() and not plan.is_symlink() and names(plan, words): found.append(plan)
            folders[:] = []
            continue
        folders[:] = [folder for folder in folders if folder not in PASSED_OVER]
    return found


def fault(tool, gone, root, removed=()):
    """Why a build of tool that could not spawn a tool from the mounts gone is the Mac's fault, and what to do."""
    mount = ", ".join(gone)
    words = (f"{tool} could not start a tool from the Metal Toolchain mount {mount}, which is gone from this Mac: macOS "
             "mounted the toolchain again at a new path, and a build plan made before still names the old one. That "
             "is this Mac's fault, not the commits'.")
    if removed:
        words += (f" Queuekeeper removed {', '.join(map(str, removed))} and ran it once more, which failed the same "
                  "way: check that `xcodebuild -showComponent MetalToolchain` says it is installed and `xcrun -f metal` "
                  "names a file, then remove the build folders that still name the old mount")
    else:
        where = f"inside {root}" if root else "inside the checkout"
        words += (f" No build plan {where} names it, so nothing was removed: remove the Intermediates.noindex/"
                  f"{PLAN} folder of the build folder that names it, such as a DerivedData folder outside the "
                  "checkout, and `xcrun -f metal` names the mount in use")
    return words + f" ({ISSUE})"


def record_fault(words, results=None):
    """Keep words in the lane's results, where host_fault finds them once the lane has failed."""
    results = os.environ.get("QUEUEKEEPER_RESULTS_DIR") if results is None else results
    if not results: return
    try:
        Path(results).mkdir(parents=True, exist_ok=True)
        with (Path(results) / FAULT).open("a") as file: file.write(" ".join(words.split()) + "\n")
    except OSError:
        pass


def host_fault(results):
    """Why the lane whose results folder is results failed for a Metal Toolchain mount gone from its Mac, or None
    (queuekeeper.plugins.HOST_FAULTS): the last fault a build wrapper recorded there in this run."""
    try: lines = (Path(results) / FAULT).read_text(errors="replace").splitlines()
    except OSError: return None
    lines = [line for line in lines if line.strip()]
    return lines[-1] if lines else None
