"""The dashboard's panels, each a module of its own here with a script of the same name beside it.

A panel module has:

    TITLE     its tab's label
    ORDER     where its tab stands among the others, lowest first
    data(repository, query)   what its script draws, as JSON: query is the request's, as {name: text}
    summary(repository)       optional: its line on Home, {"title", "text", "level"}, or None for none

and NAME.js, which calls QK.panel(NAME, {render(element, data, context)}). A panel reads the repository through
`repository` (repository.Repository) and imports no other panel but through `load`, as Home does for their
summaries. A panel that raises is answered as an error for that panel alone."""
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
            found.append({"name": name, "title": module.TITLE, "order": module.ORDER})
        except Exception as error:  # noqa: BLE001
            found.append({"name": name, "title": name.capitalize(), "order": 1000, "error": str(error)})
    return sorted(found, key=lambda panel: (panel["order"], panel["name"]))
