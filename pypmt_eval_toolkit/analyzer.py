"""Aggregate the per-task JSON results into a CSV and a coverage report.

Three things a naive aggregation gets wrong and this one does not:

* a pair whose result file never appeared is *missing*, not solved-or-not --
  it is counted and listed, so a coverage number is never quietly computed
  over a smaller denominator than you think;
* a leftover ``.running`` marker means the scheduler killed the job before it
  could write anything, which is a different failure from a clean TIMEOUT;
* coverage counts only plans that *validate*, since a returned plan that fails
  the validator is a bug report, not a solved instance.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Sequence

from .runner import KILLED, SOLVED

MISSING = 'MISSING'

_CSV_COLUMNS = [
    'planner', 'engine', 'suite', 'track', 'domain', 'instance', 'ipc', 'status',
    'up_status', 'plan_length', 'makespan', 'validated', 'parse_seconds',
    'solve_seconds', 'total_seconds', 'peak_memory_mb', 'time_limit', 'memory_limit',
    'task_id', 'domain_file', 'problem_file',
]


def analyze(args) -> int:
    sandbox = os.path.abspath(os.path.expanduser(args.sandbox_dir))
    results_dir = args.results_dir or os.path.join(sandbox, 'results')
    if not os.path.isdir(results_dir):
        print(f'No results directory at {results_dir}')
        return 1

    rows = load_rows(sandbox, results_dir)

    if not rows:
        print(f'No results found under {results_dir}')
        return 1

    out_dir = args.output_dir or os.path.join(sandbox, 'analysis')
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, 'results.csv')
    _write_csv(csv_path, rows)

    report = _report(rows, per_domain=args.per_domain)
    summary_path = os.path.join(out_dir, 'summary.txt')
    with open(summary_path, 'w') as handle:
        handle.write(report + '\n')
    with open(os.path.join(out_dir, 'summary.json'), 'w') as handle:
        json.dump(_summary_dict(rows), handle, indent=2)

    print(report)
    print()
    print(f'CSV     : {csv_path}')
    print(f'Summary : {summary_path}')
    _list_errors(sandbox, rows)
    return 0


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_rows(sandbox: str, results_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every (planner, task) pair of a sandbox as a flat row.

    Result files first, then the pairs that produced no result file: a leftover
    ``.running`` marker becomes ``KILLED``, a task in ``tasks.json`` with
    nothing at all becomes ``MISSING``. Shared by ``analyze`` and ``report`` so
    both compute coverage over the same denominator.
    """
    results_dir = results_dir or os.path.join(sandbox, 'results')
    rows = _load_results(results_dir)
    rows += _killed_rows(results_dir, {(r['planner'], r['task_id']) for r in rows})
    expected = _expected_pairs(os.path.join(sandbox, 'tasks.json'), results_dir)
    rows += _missing_rows(expected, {(r['planner'], r['task_id']) for r in rows})
    return rows


def is_solved(row: Dict[str, Any]) -> bool:
    """Public alias of the coverage predicate, for :mod:`pypmt_eval_toolkit.report`."""
    return _is_solved(row)


def _load_results(results_dir: str) -> List[Dict[str, Any]]:
    rows = []
    for dirpath, _dirnames, filenames in os.walk(results_dir):
        for name in sorted(filenames):
            if not name.endswith('.json'):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, 'r') as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            if 'status' not in payload:      # a marker file, handled separately
                continue
            rows.append(_row(payload))
    return rows


def _row(payload: Dict[str, Any]) -> Dict[str, Any]:
    task = payload.get('task') or {}
    planner = payload.get('planner') or {}
    metrics = payload.get('metrics') or {}
    timings = payload.get('timings') or {}
    limits = payload.get('limits') or {}
    return {
        'planner': planner.get('tag'),
        'engine': planner.get('engine'),
        'suite': task.get('suite'),
        'track': task.get('track'),
        'domain': task.get('domain'),
        'instance': task.get('instance'),
        'ipc': task.get('ipc'),
        'status': payload.get('status'),
        'up_status': metrics.get('up-status'),
        'plan_length': metrics.get('plan-length'),
        'makespan': metrics.get('makespan'),
        'validated': metrics.get('validated'),
        'parse_seconds': _round(timings.get('parse-seconds')),
        'solve_seconds': _round(timings.get('solve-seconds')),
        'total_seconds': _round(timings.get('total-seconds')),
        'peak_memory_mb': metrics.get('peak-memory-mb'),
        'time_limit': limits.get('time-seconds'),
        'memory_limit': limits.get('memory-mb'),
        'task_id': task.get('task-id'),
        'domain_file': task.get('domain-file'),
        'problem_file': task.get('problem-file'),
    }


def _killed_rows(results_dir: str, known: set) -> List[Dict[str, Any]]:
    """``.running`` markers with no result file beside them."""
    rows = []
    for dirpath, _dirnames, filenames in os.walk(results_dir):
        for name in filenames:
            if not name.endswith('.running'):
                continue
            if name[: -len('.running')] + '.json' in filenames:
                continue
            try:
                with open(os.path.join(dirpath, name), 'r') as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            payload['status'] = KILLED
            row = _row(payload)
            if (row['planner'], row['task_id']) not in known:
                rows.append(row)
    return rows


def _expected_pairs(tasks_file: str, results_dir: str) -> List[Dict[str, Any]]:
    """Every (planner, task) the generator planned for, from ``tasks.json``.

    ``expected`` names the pairs per planner, which is what a planner
    restricted to some tracks actually ran. Older sandboxes have no such
    section; there the full cross product is the best available guess.
    """
    if not os.path.isfile(tasks_file):
        return []
    try:
        with open(tasks_file, 'r') as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    tasks = payload.get('tasks', [])
    expected = payload.get('expected')
    if isinstance(expected, dict):
        by_id = {task.get('task_id'): task for task in tasks}
        return [{'planner': tag, 'task': by_id[task_id]}
                for tag, task_ids in sorted(expected.items())
                for task_id in task_ids if task_id in by_id]
    planner_tags = sorted(name for name in os.listdir(results_dir)
                          if os.path.isdir(os.path.join(results_dir, name)))
    return [{'planner': tag, 'task': task} for tag in planner_tags for task in tasks]


def _missing_rows(expected: Sequence[Dict[str, Any]], known: set) -> List[Dict[str, Any]]:
    rows = []
    for entry in expected:
        key = (entry['planner'], entry['task'].get('task_id'))
        if key in known:
            continue
        task = entry['task']
        rows.append({
            **{column: None for column in _CSV_COLUMNS},
            'planner': entry['planner'],
            'suite': task.get('suite'),
            'track': task.get('track'),
            'domain': task.get('domain'),
            'instance': task.get('instance'),
            'ipc': task.get('ipc'),
            'status': MISSING,
            'task_id': task.get('task_id'),
            'domain_file': task.get('domain_file'),
            'problem_file': task.get('problem_file'),
        })
    return rows


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def _is_solved(row: Dict[str, Any]) -> bool:
    """Solved means: a plan came back *and* it validated (or validation was
    unavailable, in which case the engine's own answer stands)."""
    return row['status'] == SOLVED and row['validated'] is not False


def _report(rows: Sequence[Dict[str, Any]], per_domain: bool = False) -> str:
    lines = ['=' * 78, 'pyPMT evaluation summary', '=' * 78, '']

    planners = sorted({r['planner'] for r in rows if r['planner']})
    tracks = sorted({r['track'] for r in rows if r['track']})

    lines.append('Coverage (solved & validated / attempted)')
    lines.append('')
    header = f'{"planner":<26}' + ''.join(f'{t:>16}' for t in tracks) + f'{"total":>16}'
    lines.append(header)
    lines.append('-' * len(header))
    for planner in planners:
        cells = []
        planner_rows = [r for r in rows if r['planner'] == planner]
        for track in tracks:
            track_rows = [r for r in planner_rows if r['track'] == track]
            cells.append(_coverage_cell(track_rows))
        cells.append(_coverage_cell(planner_rows))
        lines.append(f'{planner:<26}' + ''.join(f'{c:>16}' for c in cells))
    lines.append('')

    lines.append('Status breakdown')
    lines.append('')
    statuses = sorted({r['status'] for r in rows if r['status']})
    header = f'{"planner":<26}' + ''.join(f'{s[:12]:>14}' for s in statuses)
    lines.append(header)
    lines.append('-' * len(header))
    for planner in planners:
        counts = Counter(r['status'] for r in rows if r['planner'] == planner)
        lines.append(f'{planner:<26}' + ''.join(f'{counts.get(s, 0):>14}' for s in statuses))
    lines.append('')

    lines.append('Runtime on solved instances (seconds)')
    lines.append('')
    header = f'{"planner":<26}{"solved":>10}{"mean":>10}{"median":>10}{"max":>10}{"mean MB":>10}'
    lines.append(header)
    lines.append('-' * len(header))
    for planner in planners:
        solved = [r for r in rows if r['planner'] == planner and _is_solved(r)]
        times = [r['total_seconds'] for r in solved if r['total_seconds'] is not None]
        memory = [r['peak_memory_mb'] for r in solved if r['peak_memory_mb'] is not None]
        lines.append(
            f'{planner:<26}{len(solved):>10}'
            f'{_fmt(_mean(times)):>10}{_fmt(_median(times)):>10}'
            f'{_fmt(max(times) if times else None):>10}{_fmt(_mean(memory)):>10}'
        )
    lines.append('')

    if len(planners) > 1:
        lines.extend(_pairwise_section(rows, planners))

    if per_domain:
        lines.extend(_per_domain_section(rows, planners))

    return '\n'.join(lines)


def _coverage_cell(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return '-'
    solved = sum(1 for r in rows if _is_solved(r))
    return f'{solved}/{len(rows)} ({100.0 * solved / len(rows):.0f}%)'


def _pairwise_section(rows: Sequence[Dict[str, Any]], planners: Sequence[str]) -> List[str]:
    """Coverage restricted to the tasks *every* planner attempted.

    Without this, a planner that only ran the temporal track looks better or
    worse than one that ran everything for reasons that have nothing to do with
    the planner.
    """
    by_planner = defaultdict(dict)
    for row in rows:
        if row['task_id']:
            by_planner[row['planner']][row['task_id']] = row
    common = set.intersection(*(set(by_planner[p]) for p in planners)) if planners else set()
    if not common:
        return []
    lines = ['Head-to-head on the {} tasks every planner attempted'.format(len(common)), '']
    header = f'{"planner":<26}{"solved":>10}{"unique":>10}'
    lines.append(header)
    lines.append('-' * len(header))
    solved_sets = {p: {t for t in common if _is_solved(by_planner[p][t])} for p in planners}
    for planner in planners:
        others = set().union(*(solved_sets[p] for p in planners if p != planner)) if len(planners) > 1 else set()
        unique = solved_sets[planner] - others
        lines.append(f'{planner:<26}{len(solved_sets[planner]):>10}{len(unique):>10}')
    lines.append('')
    return lines


def _per_domain_section(rows: Sequence[Dict[str, Any]], planners: Sequence[str]) -> List[str]:
    lines = ['Per-domain coverage', '']
    header = f'{"track":<10}{"domain":<38}' + ''.join(f'{p[:14]:>16}' for p in planners)
    lines.append(header)
    lines.append('-' * len(header))
    domains = sorted({(r['track'] or '', r['domain'] or '') for r in rows})
    for track, domain in domains:
        cells = []
        for planner in planners:
            subset = [r for r in rows
                      if r['planner'] == planner and r['domain'] == domain and r['track'] == track]
            cells.append(_coverage_cell(subset))
        lines.append(f'{track:<10}{domain[:37]:<38}' + ''.join(f'{c:>16}' for c in cells))
    lines.append('')
    return lines


def _summary_dict(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {'planners': {}}
    for planner in sorted({r['planner'] for r in rows if r['planner']}):
        planner_rows = [r for r in rows if r['planner'] == planner]
        entry: Dict[str, Any] = {
            'engine': next((r['engine'] for r in planner_rows if r['engine']), None),
            'attempted': len(planner_rows),
            'solved': sum(1 for r in planner_rows if _is_solved(r)),
            'status-counts': dict(Counter(r['status'] for r in planner_rows)),
            'per-track': {},
        }
        for track in sorted({r['track'] for r in planner_rows if r['track']}):
            track_rows = [r for r in planner_rows if r['track'] == track]
            entry['per-track'][track] = {
                'attempted': len(track_rows),
                'solved': sum(1 for r in track_rows if _is_solved(r)),
            }
        summary['planners'][planner] = entry
    return summary


def _list_errors(sandbox: str, rows: Sequence[Dict[str, Any]]) -> None:
    errors = [r for r in rows if r['status'] == 'ERROR']
    invalid = [r for r in rows if r['validated'] is False]
    if invalid:
        print(f'\n{len(invalid)} plan(s) FAILED VALIDATION -- these are bugs, not misses:')
        for row in invalid[:10]:
            print(f"  {row['planner']:<20} {row['task_id']}")
    if errors:
        errors_dir = os.path.join(sandbox, 'errors')
        print(f'\n{len(errors)} task(s) crashed; tracebacks in {errors_dir}:')
        for row in errors[:10]:
            print(f"  {row['planner']:<20} {row['task_id']}")
        if len(errors) > 10:
            print(f'  ... and {len(errors) - 10} more')


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _write_csv(path: str, rows: Sequence[Dict[str, Any]]) -> None:
    with open(path, 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in sorted(rows, key=lambda r: (r['planner'] or '', r['track'] or '',
                                               r['domain'] or '', r['instance'] or '')):
            writer.writerow(row)


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _round(value, digits: int = 3):
    return round(value, digits) if isinstance(value, (int, float)) else value


def _fmt(value) -> str:
    return '-' if value is None else f'{value:.2f}'
