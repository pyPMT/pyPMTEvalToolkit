"""Run one (planner, task) pair and dump a JSON result.

This is the only stage that imports ``unified_planning``, and the only one that
runs on a compute node. The engine it runs is whatever the planner
configuration names -- see :mod:`pypmt_eval_toolkit.engines`; nothing here is
tied to a particular planner.

It enforces its *own* time and memory limits, a little under the ones the
scheduler was given. That is the point of the headroom in the experiment
config: a task that runs out of time or memory should come back as a
``TIMEOUT``/``MEMOUT`` row, not as a missing file and a line in the slurm
accounting log. The scheduler's limits stay in place as the backstop for the
cases the process cannot catch itself -- notably a solve that dies inside a
native solver, where no Python handler gets to run.
"""

from __future__ import annotations

import json
import os
import platform
import re
import resource
import signal
import socket
import sys
import time
import traceback
from typing import Any, Dict, List, Optional

# Statuses this harness reports, independent of the UP vocabulary.
SOLVED = 'SOLVED'
UNSOLVABLE = 'UNSOLVABLE'
EXHAUSTED = 'EXHAUSTED'        # search bound reached, task may still be solvable
TIMEOUT = 'TIMEOUT'
MEMOUT = 'MEMOUT'
UNSUPPORTED = 'UNSUPPORTED'    # the engine does not support this ProblemKind
ERROR = 'ERROR'
KILLED = 'KILLED'              # marker left behind by a job the scheduler killed

_UP_STATUS_MAP = {
    'SOLVED_SATISFICING': SOLVED,
    'SOLVED_OPTIMALLY': SOLVED,
    'UNSOLVABLE_PROVEN': UNSOLVABLE,
    'UNSOLVABLE_INCOMPLETELY': EXHAUSTED,
    'TIMEOUT': TIMEOUT,
    'MEMOUT': MEMOUT,
    'INTERNAL_ERROR': ERROR,
    'UNSUPPORTED_PROBLEM': UNSUPPORTED,
}


class _LimitReached(Exception):
    """Raised from a signal handler when the task's own limit fires."""

    def __init__(self, status: str, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def solve(args) -> int:
    """Entry point for ``pypmtevalcli solve``. Always writes a result file."""
    started_wall = time.time()
    started = time.monotonic()

    # Resolved before anything else: the run happens inside a scratch directory,
    # after which a relative path would point somewhere else entirely.
    for attribute in ('planner_cfg', 'domain', 'problem', 'results_dir', 'errors_dir', 'run_dir'):
        value = getattr(args, attribute, None)
        if value:
            setattr(args, attribute, os.path.abspath(os.path.expanduser(value)))

    with open(args.planner_cfg, 'r') as handle:
        planner_cfg = json.load(handle)
    tag = planner_cfg.get('planner-tag') or os.path.splitext(os.path.basename(args.planner_cfg))[0]
    engine = planner_cfg.get('up-planner-name') or planner_cfg.get('engine')
    params = dict(planner_cfg.get('planner-params') or {})
    modules = planner_cfg.get('up-planner-module') or planner_cfg.get('modules')
    modules = [modules] if isinstance(modules, str) else list(modules or [])
    engine_class = planner_cfg.get('up-planner-class') or planner_cfg.get('engine-class')

    slug = _slug(args.task_id)
    os.makedirs(args.results_dir, exist_ok=True)
    result_file = os.path.join(args.results_dir, f'{slug}.json')
    marker_file = os.path.join(args.results_dir, f'{slug}.running')

    result: Dict[str, Any] = {
        'task': {
            'task-id': args.task_id,
            'suite': args.suite,
            'domain': args.domain_name,
            'instance': args.instance,
            'track': args.track,
            'ipc': args.ipc,
            'domain-file': os.path.abspath(args.domain),
            'problem-file': os.path.abspath(args.problem),
        },
        'planner': {
            'tag': tag,
            'engine': engine,
            'params': _jsonable(params),
            'config-file': os.path.abspath(args.planner_cfg),
        },
        'limits': {'time-seconds': args.time_limit, 'memory-mb': args.memory_limit},
        'run': {
            'host': socket.gethostname(),
            'pid': os.getpid(),
            'started': started_wall,
            'slurm-job-id': os.environ.get('SLURM_JOB_ID'),
            'slurm-array-task-id': os.environ.get('SLURM_ARRAY_TASK_ID'),
            'python': platform.python_version(),
        },
        'status': ERROR,
        'timings': {},
        'plan': None,
        'metrics': {},
        'stats': {},
        'logs': [],
    }

    if not engine:
        result['logs'].append(f'{args.planner_cfg}: "up-planner-name" is required')
        return _finish(result, result_file, marker_file, started)

    _write_marker(marker_file, result)
    _install_limits(args.time_limit, args.memory_limit)
    workdir = _enter_workdir(args.run_dir, tag, slug)
    result['run']['work-dir'] = workdir

    try:
        import unified_planning.shortcuts as up_shortcuts
        from unified_planning.io import PDDLReader

        from .engines import resolve_engine

        environment = up_shortcuts.get_environment()
        # Every engine UP touches prints a credits block; across a sweep that is
        # tens of thousands of lines of slurm log saying the same thing.
        environment.credits_stream = None
        # Some IPC instances reuse one name across categories (an object named
        # like a domain predicate, say); UP rejects those by default.
        environment.error_used_name = False

        # Resolve the engine before parsing: an unavailable engine is a
        # configuration error, and there is no point parsing a problem for it.
        engine_class_obj = resolve_engine(engine, modules=modules,
                                          engine_class=engine_class,
                                          environment=environment)

        parse_start = time.monotonic()
        problem = PDDLReader().parse_problem(args.domain, args.problem)
        result['timings']['parse-seconds'] = time.monotonic() - parse_start
        result['metrics']['problem-kind'] = sorted(problem.kind.features)

        params = _resolve_params(params, workdir)
        result['planner']['params'] = _jsonable(params)

        if hasattr(engine_class_obj, 'supports') and not engine_class_obj.supports(problem.kind):
            result['status'] = UNSUPPORTED
            result['logs'].append(
                f'{engine} does not support this ProblemKind: '
                f'{sorted(problem.kind.features)}')
            return _finish(result, result_file, marker_file, started)

        remaining = _remaining_seconds(args.time_limit, started)
        solve_start = time.monotonic()
        with up_shortcuts.OneshotPlanner(name=engine, params=params) as planner:
            outcome = _run(planner, problem, remaining, result)
        result['timings']['solve-seconds'] = time.monotonic() - solve_start

        result['status'] = _UP_STATUS_MAP.get(outcome.status.name, ERROR)
        result['metrics']['up-status'] = outcome.status.name
        result['logs'] = _log_messages(outcome)
        result['stats'] = _engine_stats(params)

        if outcome.plan is not None and result['status'] == SOLVED:
            result['plan'] = _plan_lines(problem, outcome.plan)
            result['metrics'].update(_plan_metrics(outcome.plan))
            if getattr(args, 'validate', True):
                result['metrics']['validated'] = _validate(problem, outcome.plan, result)

    except _LimitReached as limit:
        result['status'] = limit.status
        result['logs'].append(limit.detail)
    except MemoryError:
        result['status'] = MEMOUT
        result['logs'].append(f'MemoryError under a {args.memory_limit}MB limit.')
    except Exception as error:                              # noqa: BLE001 -- reported, not swallowed
        result['status'] = ERROR
        result['error'] = {'type': type(error).__name__, 'message': str(error)}
        _dump_traceback(args, tag, slug, error)
        result['logs'].append(f'{type(error).__name__}: {error}')
    finally:
        _leave_workdir(workdir, keep=getattr(args, 'keep_run_dir', False))

    return _finish(result, result_file, marker_file, started)


def _run(planner, problem, remaining: Optional[float], result: Dict[str, Any]):
    """``planner.solve``, tolerating engines that do not take a timeout.

    Not every UP engine accepts ``timeout=``; the ones that do get the budget
    left after parsing, the ones that do not are left to the harness alarm.
    """
    if remaining is None:
        return planner.solve(problem)
    try:
        return planner.solve(problem, timeout=remaining)
    except TypeError as error:
        if 'timeout' not in str(error):
            raise
        result['logs'].append(f'{type(planner).__name__} takes no timeout; '
                              f'relying on the harness limit instead.')
        return planner.solve(problem)


def _enter_workdir(run_dir: Optional[str], tag: str, slug: str) -> Optional[str]:
    """Run inside a scratch directory of this run's own, and nothing else's.

    Fast Downward's translator writes ``output.sas`` into the *working*
    directory, SMT backends drop ``.smt2`` files there, and they are not the
    only ones. On a cluster the working directory is wherever ``sbatch`` was
    called from -- shared by every task in the array -- so two concurrent runs
    would read and overwrite each other's intermediate files. The directory
    name carries the planner tag, the task and the pid, so it is unique per run
    even when the same pair is re-run or two planners hit the same instance at
    the same moment.
    """
    if not run_dir:
        return None
    try:
        workdir = os.path.join(run_dir, f'{_slug(tag)}__{slug}.{os.getpid()}')
        os.makedirs(workdir, exist_ok=True)
        os.chdir(workdir)
        return workdir
    except OSError:
        return None


def _leave_workdir(workdir: Optional[str], keep: bool = False) -> None:
    """Leave the scratch directory and, unless asked to keep it, delete it."""
    if not workdir:
        return
    import shutil
    try:
        os.chdir(os.path.dirname(workdir) or '/')
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
    except OSError:
        pass


# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------

def _install_limits(time_limit: Optional[int], memory_limit: Optional[int]) -> None:
    """Arm the task's own limits.

    The alarm covers the search, and the address-space limit turns a runaway
    encoding into a ``MemoryError`` we can report. Signals the scheduler sends
    on the way to a kill are caught too, so a job that is about to be cancelled
    still leaves a result behind.
    """
    for signame, status, detail in (
        ('SIGALRM', TIMEOUT, 'Hit the task time limit.'),
        ('SIGTERM', KILLED, 'Received SIGTERM (scheduler cancellation).'),
        ('SIGXCPU', TIMEOUT, 'Received SIGXCPU (CPU limit).'),
        ('SIGUSR1', KILLED, 'Received SIGUSR1 (slurm --signal warning).'),
    ):
        signum = getattr(signal, signame, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _make_handler(status, detail))
        except (ValueError, OSError):
            pass

    if time_limit and time_limit > 0:
        signal.alarm(int(time_limit))

    if memory_limit and memory_limit > 0:
        limit_bytes = int(memory_limit) * 1024 * 1024
        for limit_name in ('RLIMIT_AS', 'RLIMIT_DATA'):
            rlimit = getattr(resource, limit_name, None)
            if rlimit is None:
                continue
            try:
                soft, hard = resource.getrlimit(rlimit)
                capped = limit_bytes if hard in (resource.RLIM_INFINITY, -1) else min(limit_bytes, hard)
                resource.setrlimit(rlimit, (capped, hard))
                break
            except (ValueError, OSError):
                continue


def _make_handler(status: str, detail: str):
    def handler(signum, frame):                              # noqa: ARG001
        raise _LimitReached(status, detail)
    return handler


def _remaining_seconds(time_limit: Optional[int], started: float) -> Optional[float]:
    """What is left of the budget after parsing, for the engine's own timeout."""
    if not time_limit or time_limit <= 0:
        return None
    return max(1.0, time_limit - (time.monotonic() - started))


def _peak_memory_mb() -> float:
    """Peak RSS of this process. ``ru_maxrss`` is KiB on Linux, bytes on macOS."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024.0 * 1024.0 if sys.platform == 'darwin' else 1024.0
    return round(peak / divisor, 2)


# ----------------------------------------------------------------------
# Planner parameters
# ----------------------------------------------------------------------

# Both spellings of the compilation pipeline key, including the one the older
# configurations shipped with, so an experiment written for either is read.
_COMPILATION_KEYS = ('compilationlist', 'compilation-list', 'compliation-list')


def _resolve_params(params: Dict[str, Any], workdir: Optional[str]) -> Dict[str, Any]:
    """Turn JSON-friendly parameter values into what the engine expects.

    Three things JSON cannot express directly:

    * a compilation pipeline names its ``CompilationKind`` as a string;
    * ``stats_file`` must land inside *this run's* directory, or concurrent
      runs of the same planner overwrite each other's statistics;
    * a boolean written as ``"true"`` stays a string through ``json.load``.
    """
    resolved = dict(params)

    pipeline_entries = None
    for key in _COMPILATION_KEYS:
        if key in resolved:
            pipeline_entries = resolved.pop(key)
    if pipeline_entries is not None:
        from unified_planning.shortcuts import CompilationKind
        pipeline = []
        for entry in pipeline_entries:
            name, kind = entry[0], entry[1]
            if isinstance(kind, str):
                kind = getattr(CompilationKind, kind.upper())
            pipeline.append([name, kind])
        resolved['compilationlist'] = pipeline

    stats_file = resolved.get('stats_file')
    if stats_file:
        resolved['stats_file'] = os.path.join(workdir or os.getcwd(),
                                              os.path.basename(str(stats_file)))

    for key, value in list(resolved.items()):
        if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
            resolved[key] = value.strip().lower() == 'true'

    return resolved


def _engine_stats(params: Dict[str, Any]) -> Dict[str, Any]:
    """Read back the ``stats_file`` an engine was asked to write, if any.

    The format is the ``key: value`` one pyPMT writes; values are parsed as
    JSON where possible and kept as text otherwise (never ``eval``'d).
    """
    path = params.get('stats_file')
    if not path or not os.path.exists(path):
        return {}
    stats: Dict[str, Any] = {}
    try:
        with open(path, 'r') as handle:
            for line in handle:
                key, separator, value = line.strip().partition(':')
                if not separator:
                    continue
                stats[key.strip()] = _coerce(value.strip())
    except OSError:
        return stats
    return stats


def _coerce(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


# ----------------------------------------------------------------------
# Plan reporting
# ----------------------------------------------------------------------

def _plan_metrics(plan) -> Dict[str, Any]:
    from unified_planning.plans import TimeTriggeredPlan

    if isinstance(plan, TimeTriggeredPlan):
        timed = list(plan.timed_actions)
        makespan = 0.0
        for start, _action, duration in timed:
            end = float(start) + (float(duration) if duration is not None else 0.0)
            makespan = max(makespan, end)
        return {'plan-length': len(timed), 'makespan': makespan, 'plan-type': 'time-triggered'}
    actions = getattr(plan, 'actions', [])
    return {'plan-length': len(actions), 'plan-type': 'sequential'}


def _plan_lines(problem, plan) -> List[str]:
    """The plan as text; the PDDL writer when it can, ``str`` otherwise."""
    try:
        from unified_planning.io import PDDLWriter
        text = PDDLWriter(problem).get_plan(plan)
        return [line for line in text.splitlines() if line.strip()]
    except Exception:
        return [line for line in str(plan).splitlines() if line.strip()]


def _rebind_plan(problem, plan, result: Dict[str, Any]):
    """Point a plan's action instances back at *this* problem's actions.

    An engine is free to solve a rewritten copy of the problem and hand back a
    plan over that copy's actions -- pyPMT strips redundant delete-then-set
    effects before it encodes anything, so a rovers ``communicate_rock_data``
    comes back one effect lighter than the one we parsed. UP identifies an
    action by its whole definition rather than by its name, so the validator
    rejects such a plan outright ("does not belong to the given problem")
    without checking a single precondition.

    Re-binding by name and arity restores the link without weakening the check:
    the actions come from the original problem, so it is still the original
    preconditions and effects the plan is validated against. ``None`` means
    some action could not be matched at all -- a grounded plan that was never
    lifted back, say -- and the caller reports that as "no validation" rather
    than as a bad plan.
    """
    from unified_planning.plans import ActionInstance

    unmatched: List[str] = []

    def rebind(instance):
        action = instance.action
        original = problem.action(action.name) if problem.has_action(action.name) else None
        if original is None or len(original.parameters) != len(instance.actual_parameters):
            unmatched.append(action.name)
            return instance
        if original is action:
            return instance
        return ActionInstance(original, instance.actual_parameters,
                              instance.agent, instance.motion_paths)

    rebound = plan.replace_action_instances(rebind)
    if unmatched:
        result['logs'].append(
            'Validation unavailable: the plan names actions this problem does '
            f'not have: {", ".join(sorted(set(unmatched)))}')
        return None
    return rebound


def _validate(problem, plan, result: Dict[str, Any]) -> Optional[bool]:
    """Re-check the plan against the *original* problem with a UP validator.

    Engine-agnostic on purpose: whichever validator UP can supply for this
    problem and plan kind is used. ``None`` means no validator was available,
    not that the plan is bad -- and coverage treats it as such.
    """
    try:
        import unified_planning.shortcuts as up_shortcuts
        plan = _rebind_plan(problem, plan, result)
        if plan is None:
            return None
        with up_shortcuts.PlanValidator(problem_kind=problem.kind,
                                        plan_kind=plan.kind) as validator:
            outcome = validator.validate(problem, plan)
        status = getattr(getattr(outcome, 'status', None), 'name', str(outcome))
        if status != 'VALID':
            reason = '; '.join(_log_messages(outcome)) or status
            result['logs'].append(f'Validation failed: {reason}')
        return status == 'VALID'
    except Exception as error:                               # noqa: BLE001
        result['logs'].append(f'Validation unavailable: {type(error).__name__}: {error}')
        return None


def _log_messages(outcome) -> List[str]:
    messages = getattr(outcome, 'log_messages', None) or []
    return [getattr(m, 'message', str(m)) for m in messages]


# ----------------------------------------------------------------------
# Result files
# ----------------------------------------------------------------------

def _finish(result: Dict[str, Any], result_file: str, marker_file: str, started: float) -> int:
    signal.alarm(0)
    result['timings']['total-seconds'] = time.monotonic() - started
    result['metrics']['peak-memory-mb'] = _peak_memory_mb()
    _write_json(result_file, result)
    if os.path.exists(marker_file):
        try:
            os.remove(marker_file)
        except OSError:
            pass
    print(f"{result['status']:<12} {result['task']['task-id']} "
          f"[{result['timings']['total-seconds']:.1f}s, "
          f"{result['metrics']['peak-memory-mb']:.0f}MB] -> {result_file}")
    # Exit 0 whatever happened: a recorded TIMEOUT is a successful measurement.
    return 0


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    """Write atomically, so a kill mid-write cannot leave truncated JSON."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = f'{path}.tmp.{os.getpid()}'
    with open(tmp, 'w') as handle:
        json.dump(payload, handle, indent=2, default=str)
    os.replace(tmp, path)


def _write_marker(path: str, result: Dict[str, Any]) -> None:
    """Leave a breadcrumb for the analyzer.

    If the scheduler kills the job hard enough that nothing else runs, the
    marker survives without a result file next to it, and ``analyze`` counts
    that pair as KILLED instead of silently dropping it.
    """
    try:
        _write_json(path, {'task': result['task'], 'planner': result['planner'],
                           'run': result['run'], 'limits': result['limits']})
    except OSError:
        pass


def _dump_traceback(args, tag: str, slug: str, error: Exception) -> None:
    errors_dir = getattr(args, 'errors_dir', None)
    if not errors_dir:
        return
    try:
        os.makedirs(errors_dir, exist_ok=True)
        with open(os.path.join(errors_dir, f'{_slug(tag)}__{slug}.log'), 'w') as handle:
            handle.write(f'{args.task_id}\n{type(error).__name__}: {error}\n\n')
            handle.write(traceback.format_exc())
    except OSError:
        pass


def _slug(task_id: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', str(task_id))
