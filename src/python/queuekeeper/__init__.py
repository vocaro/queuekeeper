"""Shared development tools. Shipping applications do not import this package."""


def _version():
    """The core's version, kept in one place (vocaro/queuekeeper#350): the `version` of the queuekeeper
    pyproject.toml beside this package, as a checkout has it, which tools/install-checkout.py installs without
    package metadata; else the installed distribution's metadata; else "unknown". It never raises."""
    from pathlib import Path
    import tomllib
    try:
        project = tomllib.loads((Path(__file__).resolve().parents[3] / "pyproject.toml").read_text()).get("project")
        if isinstance(project, dict) and project.get("name") == "queuekeeper" and isinstance(project.get("version"), str):
            return project["version"]
    except (OSError, ValueError, IndexError):  # no file, an unreadable one, or one tomllib cannot parse
        pass
    try:
        from importlib import metadata
        return metadata.version("queuekeeper")
    except Exception:  # PackageNotFoundError, or metadata it cannot read: the version is not worth an import error
        return "unknown"


__version__ = _version()
