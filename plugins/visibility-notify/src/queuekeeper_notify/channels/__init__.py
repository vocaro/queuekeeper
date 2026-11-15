"""The ways of telling the owner, each a module of its own here, found by its name.

A channel has `ready(config)`, which returns None when config lets it send or else why it cannot, and
`send(notice, config)`, which raises when the notice did not go. config is the object the feature's settings hold
under `channels` and the channel's name; its `enabled`, when false, turns the channel off. A channel imports no
other channel."""
import importlib
import pkgutil


def available():
    return sorted(found.name for found in pkgutil.iter_modules(__path__) if not found.name.startswith("_"))


def config_of(settings, name):
    stated = settings.get("channels") if isinstance(settings.get("channels"), dict) else {}
    return stated[name] if isinstance(stated.get(name), dict) else {}


def state(settings, name):
    """(the channel's module or None, its config, why it is off or None when it is on)."""
    config = config_of(settings, name)
    try: module = importlib.import_module(f"{__name__}.{name}")
    except Exception as error:  # noqa: BLE001
        return None, config, f"it cannot be imported: {error}"
    if config.get("enabled") is False: return module, config, "turned off"
    try: return module, config, module.ready(config)
    except Exception as error:  # noqa: BLE001
        return module, config, f"it cannot say whether it is ready: {error}"


def on(settings):
    """Each channel that is on, as (name, module, config)."""
    found = []
    for name in available():
        module, config, off = state(settings, name)
        if off is None: found.append((name, module, config))
    return found
