"""The dashboard's panels, each a module of its own here with a script of the same name beside it.

A panel module has:

    TITLE     its tab's label
    ORDER     where its tab stands among the others, lowest first
    data(repository, query)   what its script draws, as JSON: query is the request's, as {name: text}
    everywhere(repositories, query)   what its script draws for every repository at once, the page's first view:
                              repositories is {name: repository.Repository} in the order listed. A panel without
                              it has no tab there.
    summary(repository)       optional: its line on Home, {"title", "text", "level"}, or None for none

and NAME.js, which calls QK.panel(NAME, {render(element, data, context)}): context.repository is None where it
draws every repository. A panel reads a repository through `repository` (repository.Repository) and imports no
other panel but through `load`, as Home does for their summaries. A panel that raises is answered as an error for
that panel alone, and one repository that cannot be read in `everywhere` is said so beside the others (each)."""
import importlib
from pathlib import Path
import pkgutil

FOLDER = Path(__file__).parent


def available():
    return sorted(found.name for found in pkgutil.iter_modules(__path__) if not found.name.startswith("_"))


def load(name):
    if name not in available(): raise LookupError(f"no panel is named {name}")
    return importlib.import_module(f"{__name__}.{name}")


def listed():
    """Each panel that loads, as {"name", "title", "order"}, in tab order; one that does not load is listed with
    its fault, so that its tab says so."""
    found = []
    for name in available():
        try:
            module = load(name)
            if getattr(module, "HIDDEN", False): continue
            found.append({"name": name, "title": module.TITLE, "order": module.ORDER,
                          "everywhere": hasattr(module, "everywhere")})
        except Exception as error:  # noqa: BLE001
            found.append({"name": name, "title": name.capitalize(), "order": 1000, "everywhere": True, "error": str(error)})
    return sorted(found, key=lambda panel: (panel["order"], panel["name"]))


def each(repositories, read):
    """[(name, what read(repository) gives, None)] for each of repositories, in order, or (name, None, why) for one
    that raises, so that one repository that cannot be read leaves the others shown."""
    found = []
    for name, repository in repositories.items():
        try: found.append((name, read(repository), None))
        except Exception as error:  # noqa: BLE001
            found.append((name, None, str(error) or type(error).__name__))
    return found
