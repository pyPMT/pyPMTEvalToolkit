"""Discovery of planning tasks in a benchmark repository.

Three repository layouts are recognised, so the same command works on all of
the benchmark sets we care about:

``api.py``   a domain directory with an ``api.py`` that defines a ``domains``
             list of ``{'name', 'ipc', 'problems': [(domain, problem), ...]}``
             dicts, with the paths relative to the *parent* of that directory.
             This is `AI-Planning/classical-domains
             <https://github.com/AI-Planning/classical-domains>`_ and
             `pyPMT/numeric-domains <https://github.com/pyPMT/numeric-domains>`_.

``instances``  a domain directory holding ``instances/*.pddl`` plus either a
             single ``domain.pddl`` or a ``domains/domain-<n>.pddl`` per
             instance (airport and friends). This is
             `potassco/pddl-instances <https://github.com/potassco/pddl-instances>`_.

``flat``     a directory with a ``domain.pddl`` and its problems as siblings —
             the shape of most hand-made task sets.

The track a task belongs to is decided by *reading the domain file*, not by
which repository it came from: a repo named "classical-domains" still holds
domains with ``:functions``, and the temporal instances sit in the middle of
the IPC tree next to their non-temporal siblings. Sniffing keeps the three
tracks meaningful whatever you point the tool at.
"""

from __future__ import annotations

import importlib.util
import os
import re
from dataclasses import dataclass, asdict
from fnmatch import fnmatch
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Directories that never hold tasks; pruned from the walk.
_PRUNED = {'.git', '.github', '__pycache__', '.idea', '.vscode', 'generator',
           'generators', 'venv', '.venv', 'node_modules'}

TRACKS = ('classical', 'numeric', 'temporal')

_COMMENT_RE = re.compile(r';[^\n]*')
_IPC_YEAR_RE = re.compile(r'ipc[-_]?(\d{4})', re.IGNORECASE)
_TRAILING_NUM_RE = re.compile(r'(\d+)(?!.*\d)')
_NATURAL_RE = re.compile(r'(\d+)')


@dataclass(frozen=True)
class Task:
    """One (domain file, problem file) pair, with everything needed to run and
    to name it."""

    suite: str            # label of the repository it came from
    domain: str           # domain name
    instance: str         # instance name (problem file stem)
    domain_file: str
    problem_file: str
    track: str            # 'classical' | 'numeric' | 'temporal'
    ipc: Optional[str] = None

    @property
    def task_id(self) -> str:
        return f'{self.suite}:{self.domain}:{self.instance}'

    @property
    def slug(self) -> str:
        """Filesystem-safe form of :attr:`task_id`, used to name result files."""
        return re.sub(r'[^A-Za-z0-9._-]+', '_', self.task_id)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d['task_id'] = self.task_id
        return d


# ----------------------------------------------------------------------
# Track classification
# ----------------------------------------------------------------------

def classify_domain(domain_file: str) -> str:
    """Read `domain_file` and say which track it belongs to.

    Durative actions (or PDDL+ processes/events) make it temporal; a
    ``(:functions ...)`` block or a ``:numeric-fluents`` requirement makes it
    numeric; anything else is classical. Comments are stripped first so a
    commented-out ``:durative-action`` does not mislabel a whole domain.
    """
    try:
        with open(domain_file, 'r', errors='replace') as handle:
            text = handle.read(2 * 1024 * 1024)
    except OSError:
        return 'classical'
    text = _COMMENT_RE.sub(' ', text).lower()
    if ':durative-action' in text or ':process' in text or ':event' in text:
        return 'temporal'
    if '(:functions' in text or ':numeric-fluents' in text or ':fluents' in text:
        return 'numeric'
    return 'classical'


# ----------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------

def discover(root: str, suite: Optional[str] = None,
             skipped: Optional[List[Tuple[str, str]]] = None) -> List[Task]:
    """Find every task under `root`. `suite` defaults to its directory name.

    `skipped` collects ``(directory, reason)`` for directories that look like a
    domain but yielded nothing, so a benchmark set that silently contributes
    fewer instances than it holds shows up in the report instead of in nobody's
    coverage denominator.
    """
    root = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root):
        raise NotADirectoryError(f'benchmark directory not found: {root}')
    suite = suite or os.path.basename(root.rstrip(os.sep))

    tasks: List[Task] = []
    seen_pairs = set()          # (domain_file, problem_file) realpaths

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _PRUNED)
        found: List[Task] = []
        looks_like_domain = False

        if 'api.py' in filenames:
            looks_like_domain = True
            found = _from_api(dirpath, suite)
        # An api.py that resolves to nothing does not veto the directory: a few
        # domains in the wild point their api.py at paths that were renamed or
        # never shipped, and the instances are sitting right there on disk.
        if not found and 'instances' in dirnames:
            looks_like_domain = True
            found = _from_instances_dir(dirpath, suite)
        if not found:
            shared = _shared_domain_file(dirpath)
            # A domain file with no problems beside it is not a flat domain
            # directory -- most often it is an `instances/` directory whose one
            # file the parent already took as a problem.
            others = [f for f in filenames
                      if f.endswith('.pddl') and shared and f != os.path.basename(shared)]
            if shared and others:
                looks_like_domain = True
                found = _from_flat_dir(dirpath, filenames, suite, shared)

        if looks_like_domain and not found and skipped is not None:
            skipped.append((dirpath, _skip_reason(dirpath, dirnames, filenames)))

        for task in found or []:
            key = (os.path.realpath(task.domain_file), os.path.realpath(task.problem_file))
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            tasks.append(task)

    if skipped:
        # A directory whose *children* turned out to hold the tasks (one
        # sub-directory per instance) is not a skip; drop those entries.
        productive = {os.path.dirname(t.problem_file) for t in tasks}
        skipped[:] = [(directory, reason) for directory, reason in skipped
                      if not any(p.startswith(directory + os.sep) for p in productive)]

    return sort_tasks(tasks)


def _from_api(dirpath: str, suite: str) -> List[Task]:
    """Load ``<dirpath>/api.py`` and read its ``domains`` list.

    Paths inside it are relative to the parent of the domain directory
    (``agricola-opt18/domain.pddl`` living in ``classical/agricola-opt18``).
    """
    api = os.path.join(dirpath, 'api.py')
    base = os.path.dirname(dirpath)
    module_name = f'pypmteval_api_{abs(hash(dirpath))}'
    spec = importlib.util.spec_from_file_location(module_name, api)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        entries = list(getattr(module, 'domains', []))
    except Exception:
        # A broken api.py should cost us that one domain, not the whole walk.
        return []

    inside = os.path.realpath(dirpath) + os.sep
    tasks: List[Task] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('name') or os.path.basename(dirpath))
        ipc = _clean_ipc(entry.get('ipc'))
        for problem in entry.get('problems') or []:
            if not (isinstance(problem, (list, tuple)) and len(problem) >= 2):
                continue
            domain_file = os.path.join(base, str(problem[0]))
            problem_file = os.path.join(base, str(problem[1]))
            if not (os.path.isfile(domain_file) and os.path.isfile(problem_file)):
                continue
            # A domain's api.py must reference its own directory. Some in the
            # wild are copy-pasted from a sibling and resolve to *that* domain's
            # files -- taking them would file a hundred instances under the wrong
            # domain and hide the ones actually sitting here. Ignoring them lets
            # the directory-based discovery below find the real instances.
            if not os.path.realpath(problem_file).startswith(inside):
                continue
            tasks.append(Task(
                suite=suite,
                domain=_qualified_domain_name(name, dirpath),
                instance=_stem(problem_file),
                domain_file=domain_file,
                problem_file=problem_file,
                track=classify_domain(domain_file),
                ipc=ipc or _ipc_from_path(dirpath),
            ))
    return tasks


def _from_instances_dir(dirpath: str, suite: str) -> List[Task]:
    """IPC layout: ``instances/*.pddl`` beside one ``domain.pddl`` or a
    ``domains/`` directory with one domain file per instance."""
    instances_dir = os.path.join(dirpath, 'instances')
    problems = sorted(
        (os.path.join(instances_dir, f) for f in os.listdir(instances_dir)
         if f.endswith('.pddl')),
        key=natural_key,
    )
    if not problems:
        return []

    shared = _shared_domain_file(dirpath)
    per_instance: Dict[str, str] = {}
    domains_dir = os.path.join(dirpath, 'domains')
    if os.path.isdir(domains_dir):
        for f in os.listdir(domains_dir):
            if not f.endswith('.pddl'):
                continue
            number = _trailing_number(f)
            if number is not None:
                per_instance[number] = os.path.join(domains_dir, f)

    tasks: List[Task] = []
    domain_name = os.path.basename(dirpath)
    ipc = _ipc_from_path(dirpath)
    for problem_file in problems:
        number = _trailing_number(os.path.basename(problem_file))
        domain_file = per_instance.get(number or '', shared)
        if domain_file is None and len(per_instance) == 1:
            domain_file = next(iter(per_instance.values()))
        if domain_file is None or not os.path.isfile(domain_file):
            continue
        tasks.append(Task(
            suite=suite,
            domain=domain_name,
            instance=_stem(problem_file),
            domain_file=domain_file,
            problem_file=problem_file,
            track=classify_domain(domain_file),
            ipc=ipc,
        ))
    return tasks


def _shared_domain_file(dirpath: str) -> Optional[str]:
    """The one domain file all instances of `dirpath` share, if there is one.

    Usually ``domain.pddl``; some sets name it after the variant instead
    (``domain_snp.pddl``), and a domain directory holding exactly one PDDL file
    beside its ``instances/`` leaves no ambiguity either way.
    """
    default = os.path.join(dirpath, 'domain.pddl')
    if os.path.isfile(default):
        return default
    try:
        candidates = sorted(f for f in os.listdir(dirpath)
                            if f.endswith('.pddl') and os.path.isfile(os.path.join(dirpath, f)))
    except OSError:
        return None
    for pick in ([f for f in candidates if f.lower().startswith('domain')],
                 # `korf1_domain.pddl` beside `korf1_problem.pddl`: one instance
                 # per directory, the domain file named after it.
                 [f for f in candidates if 'domain' in f.lower()]):
        if len(pick) == 1:
            return os.path.join(dirpath, pick[0])
    if len(candidates) == 1:
        return os.path.join(dirpath, candidates[0])
    return None


def _skip_reason(dirpath: str, dirnames: Sequence[str], filenames: Sequence[str]) -> str:
    if 'api.py' in filenames:
        return 'api.py lists no existing (domain, problem) pair'
    if 'instances' in dirnames:
        return 'instances/ present but no domain file could be identified'
    return 'no problem files beside domain.pddl'


def _from_flat_dir(dirpath: str, filenames: Sequence[str], suite: str,
                   domain_file: Optional[str] = None) -> List[Task]:
    """A domain file with its problems as siblings."""
    domain_file = domain_file or os.path.join(dirpath, 'domain.pddl')
    if not os.path.isfile(domain_file):
        return []
    domain_basename = os.path.basename(domain_file)
    track = classify_domain(domain_file)
    ipc = _ipc_from_path(dirpath)
    domain, instance_prefix = _flat_names(dirpath)

    problems = sorted((f for f in filenames
                       if f.endswith('.pddl') and f != domain_basename), key=natural_key)
    tasks = []
    for name in problems:
        # One directory per instance (``instances/korf1/korf1_problem.pddl``):
        # the directory names the instance, not the file inside it.
        instance = (instance_prefix if instance_prefix and len(problems) == 1
                    else f'{instance_prefix}-{_stem(name)}' if instance_prefix
                    else _stem(name))
        tasks.append(Task(
            suite=suite,
            domain=domain,
            instance=instance,
            domain_file=domain_file,
            problem_file=os.path.join(dirpath, name),
            track=track,
            ipc=ipc,
        ))
    return tasks


def _flat_names(dirpath: str) -> Tuple[str, Optional[str]]:
    """``(domain name, instance prefix)`` for a flat directory.

    A directory sitting directly under ``instances/`` is one instance of the
    domain above it, not a domain of its own -- otherwise a set like 15-puzzle
    turns into a hundred one-instance "domains" called ``korf1``..``korf100``.
    """
    basename = os.path.basename(dirpath.rstrip(os.sep))
    parent = os.path.dirname(dirpath.rstrip(os.sep))
    if os.path.basename(parent) == 'instances':
        return os.path.basename(os.path.dirname(parent)), basename
    return basename, None


# ----------------------------------------------------------------------
# Filtering and selection
# ----------------------------------------------------------------------

def filter_tasks(tasks: Iterable[Task],
                 tracks: Optional[Sequence[str]] = None,
                 include: Optional[Sequence[str]] = None,
                 exclude: Optional[Sequence[str]] = None,
                 ipc_years: Optional[Sequence[str]] = None,
                 selected: Optional[Sequence[Sequence[str]]] = None) -> List[Task]:
    """Keep the tasks matching every given filter.

    `include`/`exclude` are glob patterns matched against both the bare domain
    name and ``suite:domain``, so ``--domains 'ipc-2002:*-time-*'`` and
    ``--domains counters`` both work.

    `selected` is the explicit ``[ipc-year, domain, instance]`` list of the
    older toolkit's ``selected-tasks``; when given, nothing outside it survives.
    """
    tracks = set(tracks) if tracks else None
    ipc_years = {str(y) for y in ipc_years} if ipc_years else None
    chosen = {tuple(str(part) for part in entry) for entry in selected} if selected else None
    kept = []
    for task in tasks:
        if tracks is not None and task.track not in tracks:
            continue
        if ipc_years is not None and str(task.ipc) not in ipc_years:
            continue
        if chosen is not None and (str(task.ipc), task.domain, task.instance) not in chosen:
            continue
        names = (task.domain, f'{task.suite}:{task.domain}')
        if include and not any(fnmatch(n, p) for p in include for n in names):
            continue
        if exclude and any(fnmatch(n, p) for p in exclude for n in names):
            continue
        kept.append(task)
    return kept


def limit_per_domain(tasks: Sequence[Task], limit: Optional[int],
                     strategy: str = 'even') -> List[Task]:
    """Cap each domain at `limit` instances.

    ``even`` samples across the instance range, which keeps the easy *and* the
    hard end of a domain in the experiment; ``first`` takes the N smallest by
    natural order, which is what you want for a smoke run.
    """
    if not limit or limit <= 0:
        return list(tasks)

    by_domain: Dict[str, List[Task]] = {}
    for task in tasks:
        by_domain.setdefault(f'{task.suite}:{task.domain}', []).append(task)

    selected: List[Task] = []
    for domain_tasks in by_domain.values():
        domain_tasks = sorted(domain_tasks, key=lambda t: natural_key(t.instance))
        if len(domain_tasks) <= limit:
            selected.extend(domain_tasks)
        elif strategy == 'first':
            selected.extend(domain_tasks[:limit])
        else:
            step = (len(domain_tasks) - 1) / float(limit - 1) if limit > 1 else 0.0
            picked = {int(round(i * step)) for i in range(limit)}
            selected.extend(domain_tasks[i] for i in sorted(picked))
    return sort_tasks(selected)


def sort_tasks(tasks: Iterable[Task]) -> List[Task]:
    return sorted(tasks, key=lambda t: (t.suite, t.track, t.domain, natural_key(t.instance)))


def summarize(tasks: Sequence[Task]) -> Dict[str, Dict[str, int]]:
    """Task counts per track and per suite, for the ``discover`` report."""
    per_track: Dict[str, int] = {}
    per_suite: Dict[str, int] = {}
    domains_per_track: Dict[str, set] = {}
    for task in tasks:
        per_track[task.track] = per_track.get(task.track, 0) + 1
        per_suite[task.suite] = per_suite.get(task.suite, 0) + 1
        domains_per_track.setdefault(task.track, set()).add(f'{task.suite}:{task.domain}')
    return {
        'instances_per_track': per_track,
        'instances_per_suite': per_suite,
        'domains_per_track': {k: len(v) for k, v in domains_per_track.items()},
    }


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def natural_key(text: str):
    """Sort key that orders ``instance-2`` before ``instance-10``."""
    return [int(part) if part.isdigit() else part.lower()
            for part in _NATURAL_RE.split(str(text))]


def _stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _trailing_number(name: str) -> Optional[str]:
    match = _TRAILING_NUM_RE.search(os.path.splitext(name)[0])
    return match.group(1) if match else None


def _ipc_from_path(path: str) -> Optional[str]:
    match = _IPC_YEAR_RE.search(path)
    return match.group(1) if match else None


def _clean_ipc(value) -> Optional[str]:
    if value in (None, '', 'None'):
        return None
    return str(value)


def _qualified_domain_name(name: str, dirpath: str) -> str:
    """Prefer the directory name when it is more specific than ``api.py``'s.

    ``classical-domains`` gives every IPC variant of a domain the same ``name``
    (``agricola`` for both ``agricola-opt18`` and ``agricola-sat18``), which
    would collapse distinct benchmark sets into one row of the coverage table.
    The directory name distinguishes them.
    """
    basename = os.path.basename(dirpath.rstrip(os.sep))
    if basename and name.lower() in basename.lower() and basename != name:
        return basename
    return name or basename
