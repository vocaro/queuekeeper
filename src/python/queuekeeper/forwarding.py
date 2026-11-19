"""A moved module's old name, kept for a while so that the projects importing it do not break the moment a Mac's
shared checkout takes the move (vocaro/queuekeeper#103).

Each Mac's `~/Development/queuekeeper` moves on its own, and every project's `qk` loads from it, so a module that
moves with nothing left at its old name breaks each project's scripts and tests on that Mac until the project
changes, whatever the project's queue holds. A forwarding module at the old name is one call:

    from queuekeeper import forwarding
    forwarding.forward(__name__, "queuekeeper_mobile.leases", moved="2026-11-18", until="2027-02-18")

It puts the new module in the old one's place in sys.modules, so `import`, `from ... import` and `mock.patch` of
the old name all reach the new module, warns with a DeprecationWarning naming the new one, and runs the new one as
`python3 -m OLD` would have run the old. The developer guide's "Moving a module" has the rule; the architecture
check tests/architecture/test_module_names.py holds each forwarder to it."""
import importlib
import runpy
import sys
import warnings


def forward(name, to, *, moved, until):
    """Make the module called name, a forwarder, stand for the module to: moved is the date it moved, until the date
    after which the forwarder may go."""
    if name == "__main__":
        # `python3 -m OLD`: the module was named by a string, as a driver's subprocess may name it (#213).
        spec = getattr(sys.modules["__main__"], "__spec__", None)
        warnings.warn(message(spec.name if spec else sys.argv[0], to, moved, until), DeprecationWarning, stacklevel=2)
        runpy.run_module(to, run_name="__main__", alter_sys=True)
        return None
    module = importlib.import_module(to)
    # Two levels up from here is the forwarder's own body; the importer is the next frame that is not importlib's.
    warnings.warn(message(name, to, moved, until), DeprecationWarning, stacklevel=3)
    sys.modules[name] = module
    return module


def message(name, to, moved, until):
    return (f"{name} moved to {to} on {moved}; import {to} instead. "
            f"The old name forwards to it until {until} (vocaro/queuekeeper#103).")
