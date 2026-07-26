"""Experiment configuration: resource limits, task selection, planner set.

An experiment directory holds

    <exp-dir>/exp-details.json      limits + task selection
    <exp-dir>/planners/*.json       one planner configuration per file

Every ``.json`` under ``planners/`` is benchmarked; to leave a planner out,
delete its file. A planner configuration names a UP engine and, optionally, how
to make that engine available -- which is what keeps the harness engine-agnostic
(see :mod:`pypmt_eval_toolkit.engines`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

DEFAULT_EXP_DETAILS: Dict[str, Any] = {
    'name': 'default',
    'cfgs': {
        'timelimit': '00:30:00',
        'memorylimit': '8GB',
        # Head-room the scheduler gets on top of the task's own limits, so the
        # runner hits its limit first and records TIMEOUT/MEMOUT rather than
        # the job vanishing into a slurm cancellation with no result file.
        'slurm-time-headroom': '00:05:00',
        'slurm-memory-headroom': '1GB',
        'slurm': {
            'cpus-per-task': 1,
            'partition': None,
            'account': None,
            'qos': None,
            'max-parallel-jobs': 50,
            'max-array-size': 1000,
            'extra-directives': [],
        },
    },
    'tasks': {
        'tracks': ['classical', 'numeric', 'temporal'],
        # 0 means every instance of every domain. Set a cap for a smoke run.
        'max-instances-per-domain': 0,
        'selection': 'even',
        'include-domains': [],
        'exclude-domains': [],
        'ipc-years': [],
        # Explicit [ipc-year, domain, instance] triples; empty means "no
        # explicit selection", which is how the older toolkit spelled it too.
        'selected-tasks': [],
    },
}


@dataclass
class PlannerConfig:
    """One planner configuration: a UP engine name plus its parameters.

    ``modules`` and ``engine_class`` are the two escape hatches that let an
    arbitrary engine be benchmarked without the harness knowing about it:

    * ``up-planner-module``: module(s) to import before solving, for a plugin
      that registers itself on import but ships no UP entry point;
    * ``up-planner-class``: ``"package.module:ClassName"`` (or a
      ``{"module": ..., "class": ...}`` object), registered with the UP factory
      under ``up-planner-name`` -- an engine class that is not a packaged
      plugin at all.
    """

    tag: str
    engine: str
    params: Dict[str, Any] = field(default_factory=dict)
    tracks: Optional[List[str]] = None      # restrict this planner to some tracks
    modules: List[str] = field(default_factory=list)
    engine_class: Optional[Union[str, Dict[str, str]]] = None
    path: Optional[str] = None

    @classmethod
    def from_file(cls, path: str) -> 'PlannerConfig':
        with open(path, 'r') as handle:
            raw = json.load(handle)
        tag = raw.get('planner-tag') or os.path.splitext(os.path.basename(path))[0]
        engine = raw.get('up-planner-name') or raw.get('engine')
        if not engine:
            raise ValueError(f'{path}: "up-planner-name" is required')
        return cls(
            tag=tag,
            engine=engine,
            params=raw.get('planner-params') or {},
            tracks=raw.get('tracks'),
            modules=_as_list(raw.get('up-planner-module') or raw.get('modules')),
            engine_class=raw.get('up-planner-class') or raw.get('engine-class'),
            path=os.path.abspath(path),
        )

    def runs_track(self, track: str) -> bool:
        return not self.tracks or track in self.tracks


@dataclass
class Experiment:
    """Parsed ``exp-details.json`` plus the planner configurations beside it."""

    name: str
    details: Dict[str, Any]
    planners: List[PlannerConfig]
    path: str

    # -- resource limits, normalised ------------------------------------
    @property
    def time_limit_seconds(self) -> int:
        return parse_time(self._cfg('timelimit', '00:30:00'))

    @property
    def memory_limit_mb(self) -> int:
        return parse_memory(self._cfg('memorylimit', '8GB'))

    @property
    def slurm_time(self) -> str:
        head = parse_time(self._cfg('slurm-time-headroom', '00:05:00'))
        return format_time(self.time_limit_seconds + head)

    @property
    def slurm_memory(self) -> str:
        head = parse_memory(self._cfg('slurm-memory-headroom', '1GB'))
        return f'{self.memory_limit_mb + head}M'

    @property
    def slurm(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_EXP_DETAILS['cfgs']['slurm'])
        merged.update(self.details.get('cfgs', {}).get('slurm') or {})
        return merged

    @property
    def task_selection(self) -> Dict[str, Any]:
        merged = dict(DEFAULT_EXP_DETAILS['tasks'])
        # `tasks` is the current spelling; the older toolkit put the same keys
        # at the top level of exp-details.json, so those are still honoured.
        for key in ('selected-tasks', 'include-domains', 'exclude-domains',
                    'ipc-years', 'tracks', 'max-instances-per-domain', 'selection'):
            if key in self.details:
                merged[key] = self.details[key]
        merged.update(self.details.get('tasks') or {})
        return merged

    def _cfg(self, key: str, fallback):
        return (self.details.get('cfgs') or {}).get(key, fallback)

    @classmethod
    def load(cls, exp_dir: str) -> 'Experiment':
        """Load an experiment from its directory (or from its details file)."""
        exp_dir = os.path.abspath(os.path.expanduser(exp_dir))
        details_file = exp_dir if os.path.isfile(exp_dir) else os.path.join(exp_dir, 'exp-details.json')
        if os.path.isfile(exp_dir):
            exp_dir = os.path.dirname(exp_dir)
        if not os.path.isfile(details_file):
            raise FileNotFoundError(f'experiment details not found: {details_file}')
        with open(details_file, 'r') as handle:
            details = json.load(handle)

        planners_dir = os.path.join(exp_dir, 'planners')
        if not os.path.isdir(planners_dir):
            raise FileNotFoundError(f'planner configurations not found: {planners_dir}')
        planners = [PlannerConfig.from_file(os.path.join(planners_dir, name))
                    for name in sorted(os.listdir(planners_dir)) if name.endswith('.json')]
        if not planners:
            raise ValueError(f'no planner configuration (*.json) in {planners_dir}')

        tags = [p.tag for p in planners]
        duplicates = {t for t in tags if tags.count(t) > 1}
        if duplicates:
            raise ValueError(f'duplicate planner tags would overwrite each other: {sorted(duplicates)}')

        return cls(name=details.get('name', os.path.basename(exp_dir)),
                   details=details, planners=planners, path=exp_dir)


# ----------------------------------------------------------------------
# Limit parsing
# ----------------------------------------------------------------------

_MEMORY_RE = re.compile(r'^\s*([0-9]*\.?[0-9]+)\s*([kmgt]?b?)\s*$', re.IGNORECASE)
_MEMORY_UNITS = {'': 1, 'b': 1 / (1024 ** 2), 'k': 1 / 1024, 'kb': 1 / 1024,
                 'm': 1, 'mb': 1, 'g': 1024, 'gb': 1024, 't': 1024 ** 2, 'tb': 1024 ** 2}


def parse_memory(value) -> int:
    """``"8GB"`` / ``"8192"`` / ``8192`` -> mebibytes. A bare number is MiB."""
    if isinstance(value, (int, float)):
        return int(value)
    match = _MEMORY_RE.match(str(value))
    if not match:
        raise ValueError(f'cannot parse memory limit: {value!r} (try "8GB" or "8192MB")')
    amount, unit = float(match.group(1)), match.group(2).lower()
    return max(1, int(round(amount * _MEMORY_UNITS[unit])))


def parse_time(value) -> int:
    """``"00:30:00"`` / ``"30m"`` / ``"1800"`` / ``1800`` -> seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower()
    if ':' in text:
        parts = [float(p) for p in text.split(':')]
        if len(parts) == 2:            # MM:SS
            return int(parts[0] * 60 + parts[1])
        if len(parts) == 3:            # HH:MM:SS
            return int(parts[0] * 3600 + parts[1] * 60 + parts[2])
        if len(parts) == 4:            # D-HH:MM:SS written with colons
            return int(parts[0] * 86400 + parts[1] * 3600 + parts[2] * 60 + parts[3])
        raise ValueError(f'cannot parse time limit: {value!r}')
    match = re.match(r'^([0-9]*\.?[0-9]+)\s*([smhd]?)$', text)
    if not match:
        raise ValueError(f'cannot parse time limit: {value!r} (try "00:30:00" or "30m")')
    amount = float(match.group(1))
    return int(amount * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match.group(2)])


def format_time(seconds: int) -> str:
    """Seconds -> the ``HH:MM:SS`` (or ``D-HH:MM:SS``) slurm wants."""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f'{days}-{hours:02d}:{minutes:02d}:{secs:02d}'
    return f'{hours:02d}:{minutes:02d}:{secs:02d}'


# ----------------------------------------------------------------------
# Starter experiment
# ----------------------------------------------------------------------

STARTER_PLANNERS: Dict[str, Dict[str, Any]] = {
    'smt-seq-planner.json': {
        'planner-tag': 'SequentialSMT',
        'up-planner-name': 'SMTPlanner',
        'planner-params': {
            'encoder': 'EncoderSequentialSMT',
            'upper-bound': 1000,
            'search-strategy': 'SMTSearch',
            'configuration': 'seq',
            'run-validation': False,
            'compilationlist': [
                ['up_quantifiers_remover', 'QUANTIFIERS_REMOVING'],
                ['fast-downward-reachability-grounder', 'GROUNDING'],
            ],
        },
    },
    'smt-par-planner.json': {
        'planner-tag': 'ParallelSMT',
        'up-planner-name': 'SMTPlanner',
        'planner-params': {
            'encoder': 'EncoderParallelSMT',
            'upper-bound': 1000,
            'search-strategy': 'SMTSearch',
            'configuration': 'forall',
            'run-validation': False,
            'compilationlist': [
                ['up_quantifiers_remover', 'QUANTIFIERS_REMOVING'],
                ['fast-downward-reachability-grounder', 'GROUNDING'],
            ],
        },
    },
    'enhsp.json': {
        'planner-tag': 'ENHSP',
        'up-planner-name': 'enhsp',
        'planner-params': {},
        'tracks': ['numeric', 'classical'],
    },
}


def write_default_experiment(exp_dir: str, time_limit: Optional[str] = None,
                             memory_limit: Optional[str] = None) -> str:
    """Write a starter experiment directory; used by ``pypmtevalcli init``.

    Existing files are never overwritten, so re-running it on an experiment you
    have edited only fills in what is missing.
    """
    os.makedirs(os.path.join(exp_dir, 'planners'), exist_ok=True)
    details = json.loads(json.dumps(DEFAULT_EXP_DETAILS))
    if time_limit:
        details['cfgs']['timelimit'] = time_limit
    if memory_limit:
        details['cfgs']['memorylimit'] = memory_limit
    details['name'] = os.path.basename(os.path.abspath(exp_dir))
    details_file = os.path.join(exp_dir, 'exp-details.json')
    if not os.path.exists(details_file):
        _write_json(details_file, details)

    for name, body in STARTER_PLANNERS.items():
        path = os.path.join(exp_dir, 'planners', name)
        if not os.path.exists(path):
            _write_json(path, body)
    return details_file


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    with open(path, 'w') as handle:
        json.dump(payload, handle, indent=4)
        handle.write('\n')


def _as_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Sequence):
        return [str(v) for v in value]
    return [str(value)]
