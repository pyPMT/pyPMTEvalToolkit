"""Command line entry point: ``pypmtevalcli <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from . import __version__
from .tasks import TRACKS


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return 1
    return args.func(args) or 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='pypmtevalcli',
        description='Benchmark harness for planners exposed through the '
                    'Unified Planning API (classical, numeric, temporal).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'typical flow:\n'
            '  pypmtevalcli init      --exp-dir exp-1\n'
            '  pypmtevalcli engines\n'
            '  pypmtevalcli discover  --tasks-dir classical-domains\n'
            '  pypmtevalcli generate  --exp-dir exp-1 --sandbox-dir sandbox '
            '--tasks-dir classical-domains --venv-dir venv\n'
            '  bash sandbox/slurm/submit_all.sh\n'
            '  pypmtevalcli analyze   --sandbox-dir sandbox\n'
            '  pypmtevalcli report    --sandbox-dir sandbox\n'
        ),
    )
    parser.add_argument('--version', action='version', version=f'pypmtevalcli {__version__}')
    subparsers = parser.add_subparsers(dest='command')

    # -- init ----------------------------------------------------------
    init = subparsers.add_parser('init', help='write a starter experiment directory')
    init.add_argument('--exp-dir', '--exp-details-dir', required=True)
    init.add_argument('--time-limit', help='e.g. 00:30:00 or 30m')
    init.add_argument('--memory-limit', help='e.g. 8GB')
    init.set_defaults(func=_init)

    # -- engines -------------------------------------------------------
    engines = subparsers.add_parser(
        'engines', help='list the UP engines this environment can run')
    engines.add_argument('--json', dest='as_json', action='store_true')
    engines.add_argument('--exp-dir', '--exp-details-dir', default=None,
                         help='also check that every planner of this experiment '
                              'resolves to an available engine')
    engines.set_defaults(func=_engines)

    # -- discover ------------------------------------------------------
    discover = subparsers.add_parser(
        'discover', help='list the tasks found in a benchmark repository')
    _add_task_arguments(discover)
    discover.add_argument('--json', dest='as_json', action='store_true',
                          help='dump the task list as JSON instead of a summary')
    discover.set_defaults(func=_discover)

    # -- generate ------------------------------------------------------
    generate = subparsers.add_parser(
        'generate', help='generate one run command per (planner, task) plus slurm arrays')
    generate.add_argument('--exp-dir', '--exp-details-dir', required=True,
                          help='experiment directory (exp-details.json + planners/)')
    generate.add_argument('--sandbox-dir', required=True,
                          help='where commands, results and logs are written')
    _add_task_arguments(generate)
    generate.add_argument('--venv-dir', help='virtualenv the commands should activate')
    generate.add_argument('--apptainer-image', help='run each command inside this image instead')
    generate.add_argument('--skip-existing', action='store_true',
                          help='skip pairs that already have a result (resume a sweep)')
    generate.add_argument('--per-task-scripts', action='store_true',
                          help='also emit one .sbatch per task (default is job arrays)')
    generate.add_argument('--no-validate', action='store_true',
                          help='do not validate returned plans (default: validate)')
    generate.add_argument('--local-jobs', type=int, default=4,
                          help='default parallelism baked into run_local.sh (default: 4)')
    generate.set_defaults(func=_generate)

    # -- solve ---------------------------------------------------------
    solve = subparsers.add_parser('solve', help='run ONE (planner, task) pair (called by slurm)')
    solve.add_argument('--planner-cfg', '--planner-cfg-file', required=True)
    solve.add_argument('--domain', required=True)
    solve.add_argument('--problem', required=True)
    solve.add_argument('--results-dir', '--results-dump-dir', required=True)
    solve.add_argument('--task-id', default=None,
                       help='defaults to <suite>:<domain>:<instance>')
    solve.add_argument('--suite', default='')
    solve.add_argument('--domain-name', '--domainname', default='')
    solve.add_argument('--instance', '--instanceno', default='')
    solve.add_argument('--track', default='')
    solve.add_argument('--ipc', '--ipc-year', default=None)
    solve.add_argument('--errors-dir', '--error-logs-dir', default=None)
    solve.add_argument('--run-dir', default=None,
                       help='parent for this run\'s private working directory')
    solve.add_argument('--keep-run-dir', action='store_true',
                       help='do not delete the working directory afterwards '
                            '(keeps output.sas, .smt2 and friends for debugging)')
    solve.add_argument('--no-validate', dest='validate', action='store_false',
                       help='skip plan validation')
    solve.add_argument('--time-limit', type=int, default=1800,
                       help='seconds; 0 disables the runner-side limit')
    solve.add_argument('--memory-limit', type=int, default=8192,
                       help='MB; 0 disables the runner-side limit')
    # Accepted and ignored: the older toolkit passed it, and old command files
    # must keep running against the new CLI.
    solve.add_argument('--exp-details-dir', default=None, help=argparse.SUPPRESS)
    solve.set_defaults(func=_solve, validate=True)

    # -- analyze -------------------------------------------------------
    analyze = subparsers.add_parser('analyze', help='aggregate results into a CSV and a report')
    analyze.add_argument('--sandbox-dir', default=None)
    analyze.add_argument('--results-dir', '--dump-results-dir', default=None,
                         help='default: <sandbox>/results')
    analyze.add_argument('--output-dir', default=None, help='default: <sandbox>/analysis')
    analyze.add_argument('--error-logs-dir', default=None, help=argparse.SUPPRESS)
    analyze.add_argument('--per-domain', action='store_true', help='add a per-domain table')
    analyze.set_defaults(func=_analyze)

    # -- report --------------------------------------------------------
    report = subparsers.add_parser(
        'report', help='paper-ready tables (text + LaTeX) and plots')
    report.add_argument('--sandbox-dir', required=True)
    report.add_argument('--results-dir', '--dump-results-dir', default=None,
                        help='default: <sandbox>/results')
    report.add_argument('--output-dir', default=None, help='default: <sandbox>/report')
    report.add_argument('--formats', default='pdf,png',
                        help='figure formats, comma separated (default: pdf,png)')
    report.add_argument('--no-plots', action='store_true',
                        help='tables only; skip the figures (no matplotlib needed)')
    report.set_defaults(func=_report)

    return parser


def _add_task_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--tasks-dir', '--planning-tasks-dir', action='append', required=True,
                        metavar='[LABEL=]PATH',
                        help='benchmark repository to scan; repeatable. '
                             'Prefix with LABEL= to name the suite.')
    parser.add_argument('--suite-tracks', action='append', default=None, metavar='LABEL=TRACKS',
                        help='restrict one repository to some tracks, e.g. '
                             'pddl-instances=temporal; repeatable')
    parser.add_argument('--tracks', nargs='+', choices=list(TRACKS), default=None,
                        help='restrict to these tracks (default: the experiment setting)')
    parser.add_argument('--domains', nargs='+', default=None,
                        help='glob patterns of domains to include')
    parser.add_argument('--exclude-domains', nargs='+', default=None,
                        help='glob patterns of domains to drop')
    parser.add_argument('--ipc-years', nargs='+', default=None, help='restrict to these IPC years')
    parser.add_argument('--max-instances-per-domain', type=int, default=None,
                        help='cap per domain (0 or negative: no cap)')
    parser.add_argument('--selection', choices=('even', 'first'), default=None,
                        help='"even" spreads the cap across the instance range, '
                             '"first" takes the smallest')


# ----------------------------------------------------------------------
# Dispatch (imports stay inside, so `solve`/`engines` are the only paths
# that need unified_planning)
# ----------------------------------------------------------------------

def _init(args) -> int:
    from .config import write_default_experiment
    path = write_default_experiment(args.exp_dir, args.time_limit, args.memory_limit)
    print(f'Wrote {path} and starter planner configurations in '
          f'{args.exp_dir}/planners')
    return 0


def _engines(args) -> int:
    from .engines import available_engines, engine_report, resolve_engine, EngineNotFound

    try:
        report = engine_report()
    except ImportError as error:
        # The only stage besides `solve` that needs UP; on a machine that only
        # generates commands, say so plainly instead of dumping a traceback.
        print(f'unified-planning is not importable here ({error}). '
              f'Install it (pip install unified-planning) to list engines.',
              file=sys.stderr)
        return 2
    if args.as_json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print(f'{len(report["engines"])} UP engine(s) available:')
        for name in report['engines']:
            print(f'  {name}')
        if report['plugins-imported']:
            print(f'\nplugins imported: {", ".join(report["plugins-imported"])}')
        for failure in report['plugins-failed']:
            print(f'plugin {failure["module"]} failed to import: {failure["error"]}',
                  file=sys.stderr)

    if not args.exp_dir:
        return 0

    # Resolving each configured planner here is the cheap way to find out that a
    # sweep is going to fail on every one of its ten thousand tasks.
    from .config import Experiment
    experiment = Experiment.load(args.exp_dir)
    print(f'\nPlanners of {experiment.name}:')
    missing = 0
    for planner in experiment.planners:
        try:
            resolve_engine(planner.engine, modules=planner.modules,
                           engine_class=planner.engine_class)
            print(f'  {planner.tag:<26} {planner.engine:<24} ok')
        except EngineNotFound as error:
            missing += 1
            print(f'  {planner.tag:<26} {planner.engine:<24} MISSING')
            print(f'    {str(error).splitlines()[0]}')
    if missing:
        print(f'\n{missing} planner configuration(s) name an engine that is not available.')
        print(f'available: {", ".join(available_engines())}')
    return 1 if missing else 0


def _discover(args) -> int:
    from .generator import collect_tasks
    from .tasks import summarize

    tasks = collect_tasks(args.tasks_dir, None, args)
    if args.as_json:
        json.dump([t.to_dict() for t in tasks], sys.stdout, indent=2)
        print()
        return 0

    counts = summarize(tasks)
    print(f'{len(tasks)} tasks')
    for track, count in sorted(counts['instances_per_track'].items()):
        print(f'  {track:<10} {count:>6} instances, '
              f'{counts["domains_per_track"].get(track, 0)} domains')
    print()
    print(f'{"track":<10}{"suite":<22}{"domain":<40}{"instances":>10}')
    print('-' * 82)
    grouped = {}
    for task in tasks:
        grouped.setdefault((task.track, task.suite, task.domain), 0)
        grouped[(task.track, task.suite, task.domain)] += 1
    for (track, suite, domain), count in sorted(grouped.items()):
        print(f'{track:<10}{suite[:21]:<22}{domain[:39]:<40}{count:>10}')
    return 0


def _generate(args) -> int:
    from .generator import generate
    return generate(args)


def _solve(args) -> int:
    from .runner import solve
    if not args.task_id:
        args.task_id = ':'.join(part for part in (args.suite, args.domain_name, args.instance)
                                if part) or os.path.splitext(os.path.basename(args.problem))[0]
    return solve(args)


def _analyze(args) -> int:
    from .analyzer import analyze
    if not args.sandbox_dir:
        if not args.results_dir:
            print('one of --sandbox-dir or --results-dir is required', file=sys.stderr)
            return 2
        # The older CLI only knew about the results directory; the sandbox is
        # its parent, which is where tasks.json and errors/ live.
        args.sandbox_dir = os.path.dirname(os.path.abspath(os.path.expanduser(args.results_dir)))
    return analyze(args)


def _report(args) -> int:
    from .report import report
    return report(args)


if __name__ == '__main__':
    sys.exit(main())
