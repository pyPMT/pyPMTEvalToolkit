"""Command generation: tasks x planners -> one run command each, plus slurm.

The unit of work is a single (planner, task) pair, run by ``pypmtevalcli solve``.
Everything else here is packaging: writing those commands to files and wrapping
them in job arrays.

Job arrays rather than one ``sbatch`` file per task: a full sweep is tens of
thousands of pairs, and submitting that many jobs individually is slow enough
to matter and hostile to schedulers with a submission-rate limit. One array per
planner keeps ``squeue`` readable and lets you cancel a planner's whole sweep
with a single ``scancel``. ``--per-task-scripts`` still emits the one-file-per-
task form when a site needs it.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .config import Experiment, PlannerConfig
from .tasks import Task, discover, filter_tasks, limit_per_domain, summarize

CLI = 'pypmtevalcli'


class Sandbox:
    """The directory layout every stage agrees on."""

    def __init__(self, root: str):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.tasks_file = os.path.join(self.root, 'tasks.json')
        self.cmds_dir = os.path.join(self.root, 'cmds')
        self.slurm_dir = os.path.join(self.root, 'slurm')
        self.slurm_logs_dir = os.path.join(self.slurm_dir, 'logs')
        self.results_dir = os.path.join(self.root, 'results')
        self.errors_dir = os.path.join(self.root, 'errors')
        self.runs_dir = os.path.join(self.root, 'runs')

    def create(self) -> None:
        for path in (self.root, self.cmds_dir, self.slurm_dir, self.slurm_logs_dir,
                     self.results_dir, self.errors_dir, self.runs_dir):
            os.makedirs(path, exist_ok=True)

    def planner_results_dir(self, tag: str) -> str:
        return os.path.join(self.results_dir, tag)

    def result_file(self, tag: str, slug: str) -> str:
        return os.path.join(self.planner_results_dir(tag), f'{slug}.json')


def generate(args) -> int:
    experiment = Experiment.load(args.exp_dir)
    sandbox = Sandbox(args.sandbox_dir)
    sandbox.create()

    tasks = collect_tasks(args.tasks_dir, experiment, args)
    if not tasks:
        print('No tasks matched the experiment selection; nothing to generate.')
        return 1

    # `expected` is what "attempted" means to `analyze`: the pairs this
    # experiment calls for, which is *not* the full cross product -- a planner
    # restricted to some tracks never runs the others, and counting those as
    # missing would charge it for instances it was never asked to solve.
    # Recorded before --skip-existing narrows the work, so re-generating a
    # partial sweep does not shrink the denominator.
    expected = {planner.tag: [task.task_id for task in tasks if planner.runs_track(task.track)]
                for planner in experiment.planners}
    with open(sandbox.tasks_file, 'w') as handle:
        json.dump({'summary': summarize(tasks), 'expected': expected,
                   'tasks': [t.to_dict() for t in tasks]}, handle, indent=2)

    prefix = _launcher_prefix(args)
    time_limit = experiment.time_limit_seconds
    memory_limit = experiment.memory_limit_mb
    validate = _validate_plans(experiment, args)

    per_planner: Dict[str, List[str]] = {}
    skipped_done = 0
    for planner in experiment.planners:
        commands: List[str] = []
        os.makedirs(sandbox.planner_results_dir(planner.tag), exist_ok=True)
        for task in tasks:
            if not planner.runs_track(task.track):
                continue
            if args.skip_existing and os.path.exists(sandbox.result_file(planner.tag, task.slug)):
                skipped_done += 1
                continue
            commands.append(prefix + solve_command(planner, task, sandbox, time_limit,
                                                   memory_limit, validate))
        per_planner[planner.tag] = commands

    total = sum(len(c) for c in per_planner.values())
    if total == 0:
        print('Every (planner, task) pair already has a result; nothing to generate.')
        return 0

    written = _write_command_files(sandbox, per_planner)
    _write_slurm_arrays(sandbox, experiment, per_planner)
    _write_local_runner(sandbox, per_planner, args.local_jobs)
    if args.per_task_scripts:
        _write_per_task_scripts(sandbox, experiment, per_planner)

    _report(sandbox, experiment, tasks, per_planner, total, skipped_done, written)
    return 0


# ----------------------------------------------------------------------
# Tasks
# ----------------------------------------------------------------------

def collect_tasks(tasks_dirs: Sequence[str], experiment: Optional[Experiment], args) -> List[Task]:
    """Discover, then filter and cap, according to the experiment and the CLI.

    A ``--tasks-dir`` may be given as ``label=path`` to name the suite; without
    a label the directory name is used.

    ``--suite-tracks LABEL=track,...`` narrows one repository to some tracks,
    which is how an IPC archive contributes its temporal domains without also
    re-contributing classical ones another repository already covers.
    """
    selection = experiment.task_selection if experiment else {}
    suite_tracks: Dict[str, List[str]] = {}
    for entry in getattr(args, 'suite_tracks', None) or []:
        label, _, tracks = entry.partition('=')
        suite_tracks[label.strip()] = [t.strip() for t in tracks.split(',') if t.strip()]

    tasks: List[Task] = []
    skipped: List[Tuple[str, str]] = []
    for entry in tasks_dirs:
        label, _, path = entry.partition('=')
        if not path:
            label, path = None, entry
        suite = label or os.path.basename(os.path.abspath(os.path.expanduser(path)).rstrip(os.sep))
        found = discover(path, suite=suite, skipped=skipped)
        allowed = suite_tracks.get(suite)
        if allowed:
            found = [t for t in found if t.track in allowed]
        tasks.extend(found)

    # Never silent: a directory that looks like a domain but produced nothing is
    # reported, not dropped -- otherwise the coverage denominator quietly shrinks.
    # On stderr, so `discover --json` stays machine-readable.
    for directory, reason in skipped:
        print(f'note: no tasks from {directory} ({reason})', file=sys.stderr)

    tracks = getattr(args, 'tracks', None) or selection.get('tracks')
    include = getattr(args, 'domains', None) or selection.get('include-domains')
    exclude = getattr(args, 'exclude_domains', None) or selection.get('exclude-domains')
    ipc_years = getattr(args, 'ipc_years', None) or selection.get('ipc-years')
    tasks = filter_tasks(tasks, tracks=tracks, include=include, exclude=exclude,
                         ipc_years=ipc_years, selected=selection.get('selected-tasks'))

    limit = getattr(args, 'max_instances_per_domain', None)
    if limit is None:
        limit = selection.get('max-instances-per-domain')
    strategy = getattr(args, 'selection', None) or selection.get('selection', 'even')
    return limit_per_domain(tasks, limit, strategy)


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------

def solve_command(planner: PlannerConfig, task: Task, sandbox: Sandbox,
                  time_limit: int, memory_limit: int, validate: bool = True) -> str:
    """The ``pypmtevalcli solve`` invocation for one (planner, task) pair.

    Paths are absolute and quoted: the command has to be runnable from any
    working directory, which is what a compute node gives you.
    """
    parts = [
        CLI, 'solve',
        '--planner-cfg', planner.path or '',
        '--task-id', task.task_id,
        '--suite', task.suite,
        '--domain-name', task.domain,
        '--instance', task.instance,
        '--track', task.track,
        '--domain', task.domain_file,
        '--problem', task.problem_file,
        '--results-dir', sandbox.planner_results_dir(planner.tag),
        '--errors-dir', sandbox.errors_dir,
        '--run-dir', sandbox.runs_dir,
        '--time-limit', str(time_limit),
        '--memory-limit', str(memory_limit),
    ]
    if task.ipc:
        parts += ['--ipc', str(task.ipc)]
    if not validate:
        parts.append('--no-validate')
    return ' '.join(shlex.quote(p) for p in parts)


def _validate_plans(experiment: Experiment, args) -> bool:
    if getattr(args, 'no_validate', False):
        return False
    return bool((experiment.details.get('cfgs') or {}).get('validate-plans', True))


def _launcher_prefix(args) -> str:
    """What has to happen before ``pypmtevalcli`` is on PATH on the compute node."""
    if getattr(args, 'apptainer_image', None):
        image = shlex.quote(os.path.abspath(args.apptainer_image))
        return f'apptainer run --cleanenv {image} '
    if getattr(args, 'venv_dir', None):
        activate = shlex.quote(os.path.join(os.path.abspath(args.venv_dir), 'bin', 'activate'))
        return f'source {activate} && '
    return ''


def _write_command_files(sandbox: Sandbox, per_planner: Dict[str, List[str]]) -> Dict[str, str]:
    written = {}
    all_lines: List[str] = []
    for tag, commands in per_planner.items():
        if not commands:
            continue
        path = os.path.join(sandbox.cmds_dir, f'{tag}.txt')
        with open(path, 'w') as handle:
            handle.write('\n'.join(commands) + '\n')
        written[tag] = path
        all_lines.extend(commands)

    combined = os.path.join(sandbox.cmds_dir, 'generated_cmds.sh')
    with open(combined, 'w') as handle:
        handle.write('#!/bin/bash\n')
        handle.write('\n'.join(all_lines) + '\n')
    _make_executable(combined)
    written['__all__'] = combined
    return written


# ----------------------------------------------------------------------
# Slurm
# ----------------------------------------------------------------------

def _slurm_directives(experiment: Experiment, job_name: str, sandbox: Sandbox) -> List[str]:
    slurm = experiment.slurm
    lines = [
        f'#SBATCH --job-name={job_name}',
        f'#SBATCH --output={sandbox.slurm_logs_dir}/%x_%A_%a.out',
        f'#SBATCH --error={sandbox.slurm_logs_dir}/%x_%A_%a.err',
        f'#SBATCH --cpus-per-task={slurm.get("cpus-per-task", 1)}',
        f'#SBATCH --mem={experiment.slurm_memory}',
        f'#SBATCH --time={experiment.slurm_time}',
    ]
    for key, flag in (('partition', '--partition'), ('account', '--account'), ('qos', '--qos')):
        value = slurm.get(key)
        if value:
            lines.append(f'#SBATCH {flag}={value}')
    for directive in slurm.get('extra-directives') or []:
        directive = str(directive).strip()
        lines.append(directive if directive.startswith('#SBATCH') else f'#SBATCH {directive}')
    return lines


def _write_slurm_arrays(sandbox: Sandbox, experiment: Experiment,
                        per_planner: Dict[str, List[str]]) -> List[str]:
    """One job array per planner, split so no array exceeds ``MaxArraySize``."""
    slurm = experiment.slurm
    chunk_size = int(slurm.get('max-array-size') or 1000)
    throttle = int(slurm.get('max-parallel-jobs') or 0)
    scripts: List[str] = []

    for tag, commands in per_planner.items():
        if not commands:
            continue
        cmd_file = os.path.join(sandbox.cmds_dir, f'{tag}.txt')
        chunks = [(i, min(i + chunk_size, len(commands)))
                  for i in range(0, len(commands), chunk_size)]
        for number, (start, end) in enumerate(chunks, start=1):
            suffix = f'.{number}' if len(chunks) > 1 else ''
            job_name = f'pypmteval-{tag}{suffix}'
            array = f'0-{end - start - 1}'
            if throttle > 0:
                array += f'%{throttle}'
            body = '\n'.join([
                '#!/bin/bash',
                *_slurm_directives(experiment, job_name, sandbox),
                f'#SBATCH --array={array}',
                '',
                '# Each array index picks its own line out of the command file, so the',
                '# whole sweep is one submission instead of one per task.',
                'set -uo pipefail',
                f'CMD_FILE={shlex.quote(cmd_file)}',
                f'OFFSET={start}',
                'LINE=$((OFFSET + SLURM_ARRAY_TASK_ID + 1))',
                'CMD=$(sed -n "${LINE}p" "$CMD_FILE")',
                'if [ -z "$CMD" ]; then',
                '    echo "no command at line $LINE of $CMD_FILE" >&2',
                '    exit 1',
                'fi',
                'echo "[$(date -Is)] host=$(hostname) line=$LINE"',
                'echo "$CMD"',
                'eval "$CMD"',
                'status=$?',
                'echo "[$(date -Is)] exit=$status"',
                '# The runner records timeouts and crashes itself, so a non-zero exit here',
                '# is reported but not propagated: it must not fail the rest of the array.',
                'exit 0',
                '',
            ])
            path = os.path.join(sandbox.slurm_dir, f'{job_name}.sbatch')
            with open(path, 'w') as handle:
                handle.write(body)
            _make_executable(path)
            scripts.append(path)

    submit = os.path.join(sandbox.slurm_dir, 'submit_all.sh')
    with open(submit, 'w') as handle:
        handle.write('#!/bin/bash\n# Submit every generated job array.\nset -euo pipefail\n')
        for path in scripts:
            handle.write(f'sbatch {shlex.quote(path)}\n')
    _make_executable(submit)
    return scripts


def _write_per_task_scripts(sandbox: Sandbox, experiment: Experiment,
                            per_planner: Dict[str, List[str]]) -> None:
    """One ``.sbatch`` per (planner, task), for sites without job arrays."""
    scripts_dir = os.path.join(sandbox.slurm_dir, 'per_task')
    os.makedirs(scripts_dir, exist_ok=True)
    submit = os.path.join(scripts_dir, 'submit_all.sh')
    with open(submit, 'w') as submit_handle:
        submit_handle.write('#!/bin/bash\nset -euo pipefail\n')
        for tag, commands in per_planner.items():
            for index, command in enumerate(commands):
                job_name = f'pypmteval-{tag}-{index}'
                path = os.path.join(scripts_dir, f'{job_name}.sbatch')
                with open(path, 'w') as handle:
                    handle.write('\n'.join([
                        '#!/bin/bash',
                        *_slurm_directives(experiment, job_name, sandbox),
                        '', command, '',
                    ]))
                submit_handle.write(f'sbatch {shlex.quote(path)}\n')
    _make_executable(submit)


def _write_local_runner(sandbox: Sandbox, per_planner: Dict[str, List[str]], jobs: int) -> None:
    """A no-slurm fallback: the same commands through GNU parallel or bash jobs."""
    path = os.path.join(sandbox.root, 'run_local.sh')
    combined = os.path.join(sandbox.cmds_dir, 'generated_cmds.sh')
    with open(path, 'w') as handle:
        handle.write('\n'.join([
            '#!/bin/bash',
            '# Run the whole sweep locally, N tasks at a time. Usage: ./run_local.sh [jobs]',
            'set -uo pipefail',
            f'JOBS=${{1:-{max(1, jobs)}}}',
            f'CMDS={shlex.quote(combined)}',
            '',
            'if command -v parallel >/dev/null 2>&1; then',
            '    grep -v "^#" "$CMDS" | grep -v "^[[:space:]]*$" | parallel -j "$JOBS" --halt never',
            '    exit 0',
            'fi',
            '',
            '# No GNU parallel: keep at most $JOBS children alive. (xargs -I is not an',
            '# option here -- BSD xargs caps the replacement at 255 bytes and these',
            '# commands are far longer than that.)',
            'while IFS= read -r cmd; do',
            '    case "$cmd" in ""|"#"*) continue ;; esac',
            '    while [ "$(jobs -rp | wc -l | tr -d " ")" -ge "$JOBS" ]; do sleep 0.5; done',
            '    bash -c "$cmd" &',
            'done < "$CMDS"',
            'wait',
            '',
        ]))
    _make_executable(path)


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def _report(sandbox: Sandbox, experiment: Experiment, tasks: Sequence[Task],
            per_planner: Dict[str, List[str]], total: int, skipped_done: int,
            written: Dict[str, str]) -> None:
    counts = summarize(tasks)
    print(f'Experiment      : {experiment.name} ({experiment.path})')
    print(f'Sandbox         : {sandbox.root}')
    print(f'Time limit      : {experiment.time_limit_seconds}s per task '
          f'(slurm --time={experiment.slurm_time})')
    print(f'Memory limit    : {experiment.memory_limit_mb}MB per task '
          f'(slurm --mem={experiment.slurm_memory})')
    print(f'Tasks           : {len(tasks)}')
    for track, count in sorted(counts['instances_per_track'].items()):
        domains = counts['domains_per_track'].get(track, 0)
        print(f'  {track:<10} {count:>6} instances across {domains} domains')
    print(f'Planners        : {len(experiment.planners)}')
    for planner in experiment.planners:
        print(f'  {planner.tag:<24} {len(per_planner.get(planner.tag, [])):>6} commands '
              f'(engine: {planner.engine})')
    if skipped_done:
        print(f'Skipped         : {skipped_done} pairs that already have results')
    print(f'Total commands  : {total}')
    print()
    print(f'Commands        : {written.get("__all__")}')
    print(f'Slurm arrays    : {sandbox.slurm_dir}')
    print(f'Submit with     : bash {os.path.join(sandbox.slurm_dir, "submit_all.sh")}')
    print(f'Or run locally  : bash {os.path.join(sandbox.root, "run_local.sh")} 8')


def _make_executable(path: str) -> None:
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
