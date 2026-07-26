"""Making an arbitrary UP engine available, without the harness knowing it.

The toolkit never imports a planner by name. A planner configuration says which
engine to run (``up-planner-name``), and this module is what turns that string
into an engine class the UP factory can instantiate. Four ways, tried in order,
so that anything reachable from the environment can be benchmarked:

1. ``up-planner-class``: ``"my_pkg.my_module:MyEngine"`` — registered with the
   UP factory under ``up-planner-name``. This covers an engine class that is
   not a packaged plugin at all, including one you wrote this morning.
2. ``up-planner-module``: module(s) to import first, for a plugin that
   registers itself on import but ships no entry point.
3. Whatever UP already knows: engines built into ``unified_planning`` and any
   plugin it picked up from its own entry points.
4. Plugin auto-discovery: every installed distribution that looks like a UP
   engine plugin (an entry point in a ``unified_planning`` group, or a name
   starting with ``up-``) is imported, which is how self-registering plugins
   such as ``up_enhsp`` or ``up_pypmt`` announce themselves. Discovery is
   lazy — it only runs when the engine was not found by steps 1-3, so the
   common case pays for one import, not for every planner installed.

Nothing here is specific to a planner: :data:`KNOWN_ENGINE_MODULES` is a
shortcut that saves a full discovery pass for engines we see often, never a
restriction on what can run.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# Engine name (lowercase) -> module(s) whose import registers it. Purely an
# optimisation and a better error message; an engine missing from this table is
# still found by discovery.
KNOWN_ENGINE_MODULES: Dict[str, Tuple[str, ...]] = {
    'smtplanner': ('up_pypmt',),
    'pypmt': ('up_pypmt',),
    'enhsp': ('up_enhsp',),
    'fast-downward': ('up_fast_downward',),
    'fast_downward': ('up_fast_downward',),
    'symk': ('up_symk',),
    'pyperplan': ('up_pyperplan',),
    'patty': ('up_patty',),
    'aries': ('up_aries',),
    'tamer': ('up_tamer',),
    'lpg': ('up_lpg',),
    'plasp': ('aspplanners', 'aspplanner'),
    'aba': ('aspplanners', 'aspplanner'),
    'rantanplan': ('rantanplan',),
    'planmt': ('rantanplan',),
}

# Entry-point groups a UP plugin may publish itself under.
_PLUGIN_GROUPS = ('unified_planning.plugins', 'unified_planning.engines', 'up_plugins')

_discovered = False          # discovery is a one-shot, process-wide


class EngineNotFound(Exception):
    """Raised when ``up-planner-name`` names nothing this environment can run."""


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------

def resolve_engine(name: str,
                   modules: Optional[Sequence[str]] = None,
                   engine_class: Optional[Union[str, Dict[str, str]]] = None,
                   environment=None) -> Any:
    """Return the engine class registered under `name`, making it available first.

    `modules` are imported before looking, `engine_class` is registered under
    `name` if given. Raises :class:`EngineNotFound` — with the list of engines
    that *are* available — when nothing provides it.
    """
    factory = _factory(environment)
    notes: List[str] = []

    if engine_class:
        module_name, class_name = _split_class_spec(engine_class)
        _register_class(factory, name, module_name, class_name)
        notes.append(f'registered {module_name}:{class_name} as "{name}"')

    for module in modules or ():
        ok, error = _import(module)
        notes.append(f'imported {module}' if ok else f'{module}: {error}')

    found = _lookup(factory, name)
    if found is not None:
        return found

    for module in KNOWN_ENGINE_MODULES.get(name.lower(), ()) or _known_by_prefix(name):
        ok, error = _import(module)
        notes.append(f'imported {module}' if ok else f'{module}: {error}')
        found = _lookup(factory, name)
        if found is not None:
            return found

    imported, failures = discover_plugins()
    notes += [f'discovered {m}' for m in imported]
    notes += [f'{m}: {e}' for m, e in failures]
    found = _lookup(factory, name)
    if found is not None:
        return found

    available = available_engines(environment)
    raise EngineNotFound(
        f'no UP engine named "{name}" is available.\n'
        f'  available: {", ".join(available) if available else "(none)"}\n'
        f'  tried    : {"; ".join(notes) if notes else "(nothing to try)"}\n'
        f'  fix      : install the plugin that provides it, or point the planner '
        f'configuration at it with "up-planner-module" / "up-planner-class".')


def available_engines(environment=None) -> List[str]:
    """Every engine name the factory can instantiate right now."""
    factory = _factory(environment)
    names = getattr(factory, 'engines', None)
    if isinstance(names, dict):
        names = list(names.keys())
    elif names is None:
        names = list(getattr(factory, '_engines', {}).keys())
    return sorted(str(n) for n in names)


def engine_report(environment=None) -> Dict[str, Any]:
    """What ``pypmtevalcli engines`` prints: engines, plugins, failures."""
    before = set(available_engines(environment))
    imported, failures = discover_plugins()
    after = available_engines(environment)
    return {
        'engines': after,
        'added-by-discovery': sorted(set(after) - before),
        'plugins-imported': imported,
        'plugins-failed': [{'module': m, 'error': e} for m, e in failures],
    }


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def discover_plugins(force: bool = False) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Import every installed distribution that looks like a UP engine plugin.

    Returns ``(imported modules, [(module, error), ...])``. Runs once per
    process unless `force`; a plugin that fails to import is reported rather
    than raised, since one broken plugin must not take down a sweep that does
    not use it.
    """
    global _discovered
    if _discovered and not force:
        return [], []
    _discovered = True

    imported: List[str] = []
    failures: List[Tuple[str, str]] = []
    for module in sorted(_plugin_module_names()):
        ok, error = _import(module)
        if ok:
            imported.append(module)
        elif error:
            failures.append((module, error))
    return imported, failures


def _plugin_module_names() -> List[str]:
    """Modules worth importing: UP entry points, then ``up-*`` distributions."""
    candidates = set()
    try:
        from importlib import metadata
    except ImportError:                                      # pragma: no cover
        return sorted(candidates)

    for distribution in _distributions(metadata):
        try:
            name = (distribution.metadata['Name'] or '').strip()
        except Exception:                                    # noqa: BLE001
            name = ''
        for entry_point in _entry_points(distribution):
            group = getattr(entry_point, 'group', '') or ''
            if any(group.startswith(prefix) for prefix in _PLUGIN_GROUPS):
                module = getattr(entry_point, 'module', None) or \
                    str(getattr(entry_point, 'value', '')).split(':')[0]
                if module:
                    candidates.add(module.split('.')[0])
        lowered = name.lower()
        if lowered.startswith('up-') or lowered.startswith('up_'):
            candidates.add(name.replace('-', '_'))
    # Import order is irrelevant, but a plugin we already know about should not
    # be missed just because its distribution metadata is unusual.
    for modules in KNOWN_ENGINE_MODULES.values():
        candidates.update(modules)
    candidates.discard('unified_planning')
    return sorted(candidates)


def _distributions(metadata) -> List[Any]:
    try:
        return list(metadata.distributions())
    except Exception:                                        # noqa: BLE001
        return []


def _entry_points(distribution) -> List[Any]:
    try:
        return list(distribution.entry_points or [])
    except Exception:                                        # noqa: BLE001
        return []


# ----------------------------------------------------------------------
# Factory plumbing
# ----------------------------------------------------------------------

def _factory(environment=None):
    if environment is None:
        import unified_planning.shortcuts as up_shortcuts
        environment = up_shortcuts.get_environment()
    return environment.factory


def _lookup(factory, name: str):
    """The engine class registered under `name`, or None."""
    try:
        return factory.engine(name)
    except Exception:                                        # noqa: BLE001
        return None


def _register_class(factory, name: str, module_name: str, class_name: str) -> None:
    try:
        factory.add_engine(name, module_name, class_name)
    except Exception as error:                               # noqa: BLE001
        raise EngineNotFound(
            f'could not register {module_name}:{class_name} as "{name}": '
            f'{type(error).__name__}: {error}') from error


def _split_class_spec(spec: Union[str, Dict[str, str]]) -> Tuple[str, str]:
    """``"pkg.mod:Class"`` / ``"pkg.mod.Class"`` / ``{"module", "class"}``."""
    if isinstance(spec, dict):
        module = spec.get('module') or spec.get('module-name')
        class_name = spec.get('class') or spec.get('class-name')
        if not (module and class_name):
            raise EngineNotFound(f'up-planner-class needs "module" and "class": {spec!r}')
        return str(module), str(class_name)
    text = str(spec)
    if ':' in text:
        module, _, class_name = text.partition(':')
    elif '.' in text:
        module, _, class_name = text.rpartition('.')
    else:
        raise EngineNotFound(
            f'up-planner-class must be "module:Class", got {spec!r}')
    return module.strip(), class_name.strip()


def _known_by_prefix(name: str) -> Tuple[str, ...]:
    """``fast-downward-opt`` and ``enhsp-opt`` are the same plugins as their stems."""
    lowered = name.lower()
    for key, modules in KNOWN_ENGINE_MODULES.items():
        if lowered.startswith(key):
            return modules
    return ()


def _import(module: str) -> Tuple[bool, str]:
    try:
        importlib.import_module(module)
        return True, ''
    except ImportError:
        return False, ''                 # not installed: not an error worth reporting
    except Exception as error:           # noqa: BLE001 -- a plugin that breaks on import
        return False, f'{type(error).__name__}: {error}'
