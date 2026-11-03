"""Paper-ready tables and plots: ``pypmtevalcli report``.

Produces what a planning paper's evaluation section is made of — a per-domain
coverage table, the outcome breakdown (timeouts, memouts, errors), IPC-style
time and quality scores, the survival ("cactus") plots, plan-quality violins
over the commonly solved instances, and per-pair runtime scatters.

Tables are written as text *and* as LaTeX (booktabs) ready to ``\\input``.
Plots need matplotlib (``pip install "pypmt-eval-toolkit[plots]"``); without it the
tables are still produced and the plots are skipped with a note.

Design notes for the figures, since they end up in a paper:

* everything is black and white: a series is told apart by its marker shape,
  filled or hollow, and its line style, never by a hue, so the figures print
  on a monochrome printer and photocopy exactly as they appear on screen;
* the cactus plots follow the planning-literature convention of *instances
  solved* on the x axis and *time (or memory) on a log y axis*: a curve that
  stays low and reaches far right is a good planner;
* bars are told apart by hatch pattern and grey level, with outcomes
  (solved / timeout / memout / error ...) on one fixed scheme that is never
  reused for a planner;
* there are as many line series as there are distinct marker/line-style
  combinations; past that the data is faceted or truncated rather than drawn
  in a style that already means something else;
* one y-axis per plot, no dual scales.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from math import log10
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .analyzer import MISSING, is_solved, load_rows
from .runner import ERROR, EXHAUSTED, KILLED, MEMOUT, SOLVED, TIMEOUT, UNSOLVABLE, UNSUPPORTED

# -- monochrome styling --------------------------------------------------------
# A line series is identified by the triple (marker, filled-or-hollow, dashes).
# The eight entries are ordered so that neighbours differ in at least two of
# the three, which is what keeps adjacent curves apart in a dense cactus plot.
SERIES_MARKERS = ['o', 's', '^', 'D', 'v', 'P', 'X', '*']
SERIES_HOLLOW = [False, True, False, True, False, True, False, True]
SERIES_DASHES = [(None, None), (5, 2), (1.5, 1.5), (7, 2, 1.5, 2),
                 (3, 1.5), (9, 3), (1.5, 1.5, 5, 1.5), (4, 1.5, 1, 1.5, 1, 1.5)]
LINE_SERIES_CAP = len(SERIES_MARKERS)
# Runtime scatters: one marker per domain within a track. Fourteen shapes,
# black first and then mid-grey, before a shape repeats; hollow is reserved
# for "unsolved", so it never identifies a domain.
DOMAIN_MARKERS = ['o', 's', '^', 'D', 'v', 'P', 'X', '*', '<', '>', 'p', 'h', 'd', '8']
DOMAIN_FILLS = ['#000000', '#8c8c8c']
# Bars: a hatch per planner, white fill, black edge.
SERIES_HATCHES = ['', '////', '....', 'xxxx', '\\\\\\\\', '++++', 'oo', '**']

# Outcomes: a fixed (grey level, hatch) per status, darkest for solved so the
# useful part of a stacked bar reads first; the failure modes are light with a
# distinct hatch each, and the "nothing happened" statuses are plain greys.
STATUS_STYLE = {
    SOLVED: ('#1a1a1a', ''),
    TIMEOUT: ('#ffffff', '////'),
    MEMOUT: ('#ffffff', '....'),
    EXHAUSTED: ('#bdbdbd', ''),
    UNSOLVABLE: ('#8c8c8c', ''),
    UNSUPPORTED: ('#ffffff', ''),
    ERROR: ('#ffffff', '\\\\\\\\'),
    KILLED: ('#ffffff', 'xxxx'),
    MISSING: ('#e6e6e6', ''),
}
STATUS_ORDER = [SOLVED, TIMEOUT, MEMOUT, EXHAUSTED, UNSOLVABLE, UNSUPPORTED, ERROR, KILLED, MISSING]

INK = '#000000'
INK_MUTED = '#595959'
GRID = '#d9d9d9'
AXIS = '#8c8c8c'
SURFACE = '#ffffff'
INSTANCES_FILL = '#ededed'      # the instance-count backdrop behind coverage bars

MIN_TIME = 0.1                  # floor for log axes and for the IPC time score


def report(args) -> int:
    sandbox = os.path.abspath(os.path.expanduser(args.sandbox_dir))
    rows = load_rows(sandbox, args.results_dir)
    if not rows:
        print(f'No results found under {args.results_dir or os.path.join(sandbox, "results")}')
        return 1

    out_dir = args.output_dir or os.path.join(sandbox, 'report')
    os.makedirs(out_dir, exist_ok=True)

    planners = sorted({r['planner'] for r in rows if r['planner']})
    tracks = [t for t in ('classical', 'numeric', 'temporal')
              if t in {r['track'] for r in rows}]

    text_sections: List[str] = []
    text_sections.append(_coverage_table(rows, planners, tracks))
    text_sections.append(_outcome_table(rows, planners))
    text_sections.append(_score_table(rows, planners, tracks))
    text_sections.append(_per_domain_table(rows, planners))
    text = '\n'.join(text_sections)

    _write(os.path.join(out_dir, 'results.txt'), text)
    _write(os.path.join(out_dir, 'coverage.tex'),
           _coverage_latex(rows, planners, tracks))
    _write(os.path.join(out_dir, 'per-domain-coverage.tex'),
           _per_domain_latex(rows, planners))
    _write(os.path.join(out_dir, 'outcomes.tex'), _outcome_latex(rows, planners))
    with open(os.path.join(out_dir, 'report.json'), 'w') as handle:
        json.dump(_machine_summary(rows, planners, tracks), handle, indent=2)

    print(text)
    print(f'Tables  : {out_dir}')

    if args.no_plots:
        print('Plots   : skipped (--no-plots)')
        return 0
    figures = _plots(rows, planners, tracks, os.path.join(out_dir, 'plots'),
                     formats=[f.strip() for f in args.formats.split(',') if f.strip()])
    if figures is None:
        print('Plots   : skipped -- matplotlib not installed '
              '(pip install "pypmt-eval-toolkit[plots]")')
    else:
        print(f'Plots   : {len(figures)} figures in {os.path.join(out_dir, "plots")}')
        for name in figures:
            print(f'          {name}')
    return 0


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------

def _cost(row: Dict[str, Any]) -> Optional[float]:
    """Plan cost: makespan for a schedule, number of actions for a sequence."""
    if row.get('makespan') is not None:
        return float(row['makespan'])
    if row.get('plan_length') is not None:
        return float(row['plan_length'])
    return None


def _runtime(row: Dict[str, Any]) -> Optional[float]:
    value = row.get('total_seconds')
    return max(float(value), MIN_TIME) if value is not None else None


def _time_limit(rows: Sequence[Dict[str, Any]]) -> float:
    limits = [r['time_limit'] for r in rows if r.get('time_limit')]
    return float(max(limits)) if limits else 1800.0


def _solved_index(rows: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """``task_id -> planner -> row`` over solved rows only."""
    index: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        if is_solved(row) and row['task_id']:
            index[row['task_id']][row['planner']] = row
    return index


def _ipc_scores(rows: Sequence[Dict[str, Any]],
                planners: Sequence[str]) -> Dict[str, Dict[str, float]]:
    """IPC time and quality scores.

    Quality follows the satisficing-track rule (``best_cost / cost``, 0 when
    unsolved); time follows the agile-track rule
    (``1 / (1 + log10(t / t_best))``, 0 when unsolved). Both are summed over
    instances, so the maximum a planner can reach is the instance count.
    """
    index = _solved_index(rows)
    scores = {p: {'time': 0.0, 'quality': 0.0} for p in planners}
    for _task_id, by_planner in index.items():
        times = {p: _runtime(r) for p, r in by_planner.items() if _runtime(r) is not None}
        costs = {p: _cost(r) for p, r in by_planner.items() if _cost(r) not in (None, 0)}
        if times:
            best = min(times.values())
            for planner, value in times.items():
                scores[planner]['time'] += 1.0 / (1.0 + log10(value / best))
        if costs:
            best = min(costs.values())
            for planner, value in costs.items():
                scores[planner]['quality'] += best / value
    return scores


# ----------------------------------------------------------------------
# Text tables
# ----------------------------------------------------------------------

def _coverage_table(rows, planners, tracks) -> str:
    lines = ['=' * 78, 'Coverage (solved and validated / instances)', '=' * 78, '']
    header = f'{"planner":<26}' + ''.join(f'{t:>16}' for t in tracks) + f'{"total":>16}'
    lines += [header, '-' * len(header)]
    for planner in planners:
        subset = [r for r in rows if r['planner'] == planner]
        cells = [_cell(subset, track) for track in tracks] + [_cell(subset, None)]
        lines.append(f'{planner:<26}' + ''.join(f'{c:>16}' for c in cells))
    return '\n'.join(lines) + '\n'


def _cell(rows, track: Optional[str]) -> str:
    subset = [r for r in rows if track is None or r['track'] == track]
    if not subset:
        return '-'
    solved = sum(1 for r in subset if is_solved(r))
    return f'{solved}/{len(subset)} ({100.0 * solved / len(subset):.0f}%)'


def _outcome_table(rows, planners) -> str:
    statuses = [s for s in STATUS_ORDER if s in {r['status'] for r in rows}]
    lines = ['', 'Outcomes', '']
    header = f'{"planner":<26}' + ''.join(f'{s[:11]:>13}' for s in statuses)
    lines += [header, '-' * len(header)]
    for planner in planners:
        counts = Counter(r['status'] for r in rows if r['planner'] == planner)
        lines.append(f'{planner:<26}' + ''.join(f'{counts.get(s, 0):>13}' for s in statuses))
    return '\n'.join(lines) + '\n'


def _score_table(rows, planners, tracks) -> str:
    scores = _ipc_scores(rows, planners)
    instances = len({r['task_id'] for r in rows if r['task_id']})
    lines = ['', f'IPC scores (out of {instances} instances) and runtime on solved tasks', '']
    header = (f'{"planner":<26}{"quality":>10}{"time":>10}{"solved":>9}'
              f'{"mean s":>10}{"median s":>10}{"max s":>10}{"mean MB":>10}')
    lines += [header, '-' * len(header)]
    for planner in planners:
        solved = [r for r in rows if r['planner'] == planner and is_solved(r)]
        times = [_runtime(r) for r in solved if _runtime(r) is not None]
        memory = [r['peak_memory_mb'] for r in solved if r['peak_memory_mb'] is not None]
        lines.append(
            f'{planner:<26}{scores[planner]["quality"]:>10.2f}{scores[planner]["time"]:>10.2f}'
            f'{len(solved):>9}{_fmt(_mean(times)):>10}{_fmt(_median(times)):>10}'
            f'{_fmt(max(times) if times else None):>10}{_fmt(_mean(memory)):>10}')
    return '\n'.join(lines) + '\n'


def _per_domain_table(rows, planners) -> str:
    lines = ['', 'Per-domain coverage', '']
    header = f'{"track":<10}{"domain":<34}' + ''.join(f'{p[:14]:>16}' for p in planners)
    lines += [header, '-' * len(header)]
    for track, domain, per_planner, total in _domain_rows(rows, planners):
        cells = ''.join(f'{f"{n}/{total}":>16}' for n in per_planner)
        lines.append(f'{track:<10}{domain[:33]:<34}{cells}')
    totals = [sum(1 for r in rows if r['planner'] == p and is_solved(r)) for p in planners]
    attempted = [sum(1 for r in rows if r['planner'] == p) for p in planners]
    lines.append('-' * len(header))
    lines.append(f'{"":<10}{"total":<34}' +
                 ''.join(f'{f"{s}/{a}":>16}' for s, a in zip(totals, attempted)))
    return '\n'.join(lines) + '\n'


def _domain_rows(rows, planners) -> List[Tuple[str, str, List[int], int]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row['track'] or '', row['domain'] or '')].append(row)
    out = []
    for (track, domain), domain_rows in sorted(grouped.items()):
        per_planner, total = [], 0
        for planner in planners:
            subset = [r for r in domain_rows if r['planner'] == planner]
            per_planner.append(sum(1 for r in subset if is_solved(r)))
            total = max(total, len(subset))
        out.append((track, domain, per_planner, total))
    return out


# ----------------------------------------------------------------------
# LaTeX tables
# ----------------------------------------------------------------------

def _tex(text: str) -> str:
    for char, escaped in (('\\', r'\textbackslash{}'), ('_', r'\_'), ('&', r'\&'),
                          ('%', r'\%'), ('#', r'\#'), ('$', r'\$')):
        text = text.replace(char, escaped)
    return text


def _latex_table(caption: str, label: str, column_spec: str,
                 header: Sequence[str], body: Sequence[Sequence[str]],
                 footer: Optional[Sequence[str]] = None) -> str:
    lines = [
        '% Generated by `pypmtevalcli report`. Requires \\usepackage{booktabs}.',
        '\\begin{table}[t]', '  \\centering', '  \\small',
        f'  \\caption{{{caption}}}', f'  \\label{{{label}}}',
        f'  \\begin{{tabular}}{{{column_spec}}}', '    \\toprule',
        '    ' + ' & '.join(header) + ' \\\\', '    \\midrule',
    ]
    lines += ['    ' + ' & '.join(row) + ' \\\\' for row in body]
    if footer:
        lines += ['    \\midrule', '    ' + ' & '.join(footer) + ' \\\\']
    lines += ['    \\bottomrule', '  \\end{tabular}', '\\end{table}', '']
    return '\n'.join(lines)


def _coverage_latex(rows, planners, tracks) -> str:
    header = ['Planner'] + [t.capitalize() for t in tracks] + ['Total']
    body = []
    for planner in planners:
        subset = [r for r in rows if r['planner'] == planner]
        cells = []
        for track in list(tracks) + [None]:
            scoped = [r for r in subset if track is None or r['track'] == track]
            solved = sum(1 for r in scoped if is_solved(r))
            cells.append(f'{solved} / {len(scoped)}' if scoped else '--')
        body.append([_tex(planner)] + cells)
    return _latex_table('Coverage per track (solved and validated / instances).',
                        'tab:coverage', 'l' + 'r' * (len(tracks) + 1), header, body)


def _outcome_latex(rows, planners) -> str:
    statuses = [s for s in STATUS_ORDER if s in {r['status'] for r in rows}]
    header = ['Planner'] + [s.capitalize() for s in statuses]
    body = []
    for planner in planners:
        counts = Counter(r['status'] for r in rows if r['planner'] == planner)
        body.append([_tex(planner)] + [str(counts.get(s, 0)) for s in statuses])
    return _latex_table('Outcome breakdown per planner.', 'tab:outcomes',
                        'l' + 'r' * len(statuses), header, body)


def _per_domain_latex(rows, planners) -> str:
    header = ['Track', 'Domain', '\\#'] + [_tex(p) for p in planners]
    body = [[track, _tex(domain), str(total)] + [str(n) for n in per_planner]
            for track, domain, per_planner, total in _domain_rows(rows, planners)]
    totals = [str(sum(1 for r in rows if r['planner'] == p and is_solved(r))) for p in planners]
    attempted = max((sum(1 for r in rows if r['planner'] == p) for p in planners), default=0)
    footer = ['', '\\textbf{Total}', str(attempted)] + [f'\\textbf{{{t}}}' for t in totals]
    return _latex_table('Coverage per domain; \\# is the number of instances.',
                        'tab:per-domain', 'll r' + ' r' * len(planners), header, body, footer)


def _machine_summary(rows, planners, tracks) -> Dict[str, Any]:
    scores = _ipc_scores(rows, planners)
    summary: Dict[str, Any] = {'instances': len({r['task_id'] for r in rows if r['task_id']}),
                               'tracks': list(tracks), 'planners': {}}
    for planner in planners:
        subset = [r for r in rows if r['planner'] == planner]
        summary['planners'][planner] = {
            'attempted': len(subset),
            'solved': sum(1 for r in subset if is_solved(r)),
            'outcomes': dict(Counter(r['status'] for r in subset)),
            'ipc-quality-score': round(scores[planner]['quality'], 3),
            'ipc-time-score': round(scores[planner]['time'], 3),
            'per-track': {
                track: {
                    'attempted': len([r for r in subset if r['track'] == track]),
                    'solved': sum(1 for r in subset if r['track'] == track and is_solved(r)),
                } for track in tracks
            },
        }
    return summary


# ----------------------------------------------------------------------
# Plots
# ----------------------------------------------------------------------

def _plots(rows, planners, tracks, plots_dir: str, formats: Sequence[str]) -> Optional[List[str]]:
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    os.makedirs(plots_dir, exist_ok=True)
    _style(matplotlib)
    written: List[str] = []

    if len(planners) > LINE_SERIES_CAP:
        print(f'note: {len(planners)} planners, plotting the first {LINE_SERIES_CAP} '
              f'in the line figures ({", ".join(planners[LINE_SERIES_CAP:])} omitted)')
    line_planners = planners[:LINE_SERIES_CAP]

    written.append(_survival_plot(plt, rows, line_planners, plots_dir, formats))
    if len(tracks) > 1:
        written.append(_survival_facets(plt, rows, line_planners, tracks, plots_dir, formats))
    written.append(_memory_survival_plot(plt, rows, line_planners, plots_dir, formats))
    written.append(_coverage_bars(plt, rows, line_planners, tracks, plots_dir, formats))
    written.append(_outcome_bars(plt, rows, planners, plots_dir, formats))

    written.append(_quality_violins(plt, rows, planners, tracks, plots_dir, formats))

    pairs = [(a, b) for i, a in enumerate(planners) for b in planners[i + 1:]]
    if len(pairs) > 6:
        print(f'note: {len(pairs)} planner pairs, plotting the first 6 runtime scatters')
        pairs = pairs[:6]
    for left, right in pairs:
        written.append(_runtime_scatter(plt, rows, left, right, tracks, plots_dir, formats))

    return [w for w in written if w]


def _style(matplotlib) -> None:
    matplotlib.rcParams.update({
        'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE, 'savefig.facecolor': SURFACE,
        'font.family': 'sans-serif',
        'font.sans-serif': ['DejaVu Sans', 'Helvetica', 'Arial'],
        'font.size': 9, 'axes.titlesize': 10, 'axes.labelsize': 9, 'legend.fontsize': 8,
        'xtick.labelsize': 8, 'ytick.labelsize': 8,
        'axes.edgecolor': INK, 'axes.linewidth': 0.8,
        'axes.labelcolor': INK, 'text.color': INK,
        'xtick.color': INK, 'ytick.color': INK,
        'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.5,
        'axes.spines.top': False, 'axes.spines.right': False,
        'lines.linewidth': 1.3, 'lines.color': INK,
        'patch.edgecolor': INK, 'hatch.color': INK, 'hatch.linewidth': 0.6,
        'legend.frameon': False,
        'figure.dpi': 200, 'savefig.bbox': 'tight', 'savefig.pad_inches': 0.02,
        'pdf.fonttype': 42,          # editable text in the PDF, as venues ask
    })


def _save(fig, plots_dir: str, name: str, formats: Sequence[str]) -> str:
    for extension in formats or ['pdf', 'png']:
        fig.savefig(os.path.join(plots_dir, f'{name}.{extension}'))
    fig.clf()
    return f'{name}.{",".join(formats or ["pdf", "png"])}'


def _series_style(index: int, points: int) -> Dict[str, Any]:
    """Keyword arguments that draw series ``index`` in its own monochrome style.

    Black line, a marker shape, filled or hollow, and a dash pattern -- the
    three together identify the series; no two of the first eight share more
    than one of them with a neighbour.
    """
    hollow = SERIES_HOLLOW[index % len(SERIES_HOLLOW)]
    return {
        'color': INK,
        'dashes': SERIES_DASHES[index % len(SERIES_DASHES)],
        'marker': SERIES_MARKERS[index % len(SERIES_MARKERS)],
        'markersize': 4.5,
        'markerfacecolor': SURFACE if hollow else INK,
        'markeredgecolor': INK,
        'markeredgewidth': 0.9,
        # Markers on every point in a small run; thinned on a long one so the
        # line still shows through, but never fewer than ~10 per curve.
        'markevery': max(1, points // 10),
    }


def _sorted_solved(rows, planner: str, value) -> List[float]:
    """``value`` over this planner's solved rows, ascending -- a cactus curve."""
    return sorted(v for v in (value(r) for r in rows if r['planner'] == planner and is_solved(r))
                  if v is not None)


def _draw_cactus(ax, rows, planners, value, limit: Optional[float]) -> int:
    """One cactus curve per planner: instances solved (x) against the budget
    (y, log) needed to solve that many -- the k-th point is the k-th cheapest
    solved instance.

    Returns the largest instance count drawn, 0 when nothing was.
    """
    # Scale and range before any data: switching an empty axes to log while it
    # still holds the default (0, 1) range warns about the non-positive bound.
    ax.set_yscale('log')
    widest = 0
    for index, planner in enumerate(planners):
        values = _sorted_solved(rows, planner, value)
        if not values:
            continue
        counts = list(range(1, len(values) + 1))
        ax.plot(counts, values, label=planner, zorder=3, **_series_style(index, len(values)))
        widest = max(widest, len(values))
    if limit is not None:
        ax.axhline(limit, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=1)
        # Named at the right end: the legend lives in the upper left.
        ax.annotate('limit', xy=(1, limit), xytext=(-3, 2), textcoords='offset points',
                    xycoords=('axes fraction', 'data'), ha='right', fontsize=7,
                    color=INK_MUTED)
    # Start the count axis at 0 so the first solved instance stands off the
    # spine, and leave room on the right so the last marker is not clipped.
    ax.set_xlim(0, max(1, widest) * 1.04 + 0.5)
    _integer_xaxis(ax)
    return widest


def _integer_xaxis(ax) -> None:
    """Instances are countable; half an instance is never a tick."""
    from matplotlib.ticker import MaxNLocator
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))


def _integer_yaxis(ax) -> None:
    from matplotlib.ticker import MaxNLocator
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))


def _survival_plot(plt, rows, planners, plots_dir, formats) -> str:
    limit = _time_limit(rows)
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    # Headroom below the floor so a run clamped to MIN_TIME is not drawn on the
    # spine, and above the limit for the cutoff line and its label.
    ax.set_ylim(MIN_TIME * 0.7, limit * 1.6)
    _draw_cactus(ax, rows, planners, _runtime, limit)
    ax.set_xlabel('instances solved')
    ax.set_ylabel('time (s, log scale)')
    ax.set_title('Survival plot — time needed to solve the k cheapest instances')
    # The curves climb towards the right, which leaves the upper left empty.
    ax.legend(loc='upper left')
    fig.tight_layout()
    return _save(fig, plots_dir, 'survival', formats)


def _survival_facets(plt, rows, planners, tracks, plots_dir, formats) -> str:
    limit = _time_limit(rows)
    fig, axes = plt.subplots(1, len(tracks), figsize=(3.1 * len(tracks), 3.0), sharey=True)
    axes = axes if hasattr(axes, '__len__') else [axes]
    for ax, track in zip(axes, tracks):
        ax.set_ylim(MIN_TIME * 0.7, limit * 1.6)
        _draw_cactus(ax, [r for r in rows if r['track'] == track], planners, _runtime, limit)
        ax.set_title(track)
        ax.set_xlabel('instances solved')
    axes[0].set_ylabel('time (s, log scale)')
    axes[0].legend(loc='upper left')
    fig.suptitle('Survival plots per track', y=1.02)
    fig.tight_layout()
    return _save(fig, plots_dir, 'survival-per-track', formats)


def _memory_survival_plot(plt, rows, planners, plots_dir, formats) -> str:
    def memory(row):
        value = row.get('peak_memory_mb')
        return max(float(value), 1.0) if value is not None else None

    observed = [m for m in (memory(r) for r in rows if is_solved(r)) if m is not None]
    if not observed:
        return ''
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    ax.set_ylim(min(observed) * 0.7, max(observed) * 1.6)
    _draw_cactus(ax, rows, planners, memory, limit=None)
    ax.set_xlabel('instances solved')
    ax.set_ylabel('peak memory (MB, log scale)')
    ax.set_title('Memory profile — memory needed to solve the k cheapest instances')
    ax.legend(loc='upper left')
    fig.tight_layout()
    return _save(fig, plots_dir, 'memory-survival', formats)


def _coverage_bars(plt, rows, planners, tracks, plots_dir, formats) -> str:
    fig, ax = plt.subplots(figsize=(5.8, 3.2))
    width = 0.8 / max(1, len(planners))
    # Percent labels sit above adjacent bars, so they shrink as the bars do.
    label_size = 7 if len(planners) <= 3 else 6
    for index, planner in enumerate(planners):
        heights, totals = [], []
        for track in tracks:
            subset = [r for r in rows if r['planner'] == planner and r['track'] == track]
            heights.append(sum(1 for r in subset if is_solved(r)))
            totals.append(len(subset))
        positions = [i + index * width - 0.4 + width / 2 for i in range(len(tracks))]
        # The instance count behind each bar: without it, 4/4 on a 4-instance
        # track draws exactly as tall as 4/88 on a numeric one.
        ax.bar(positions, totals, width=width * 0.92, color=INSTANCES_FILL, zorder=1,
               edgecolor='none', label='instances' if index == 0 else None)
        bars = ax.bar(positions, heights, width=width * 0.92, zorder=2, label=planner,
                      color=SURFACE, edgecolor=INK, linewidth=0.8,
                      hatch=SERIES_HATCHES[index % len(SERIES_HATCHES)])
        for bar, height, total in zip(bars, heights, totals):
            if total:
                ax.annotate(f'{100.0 * height / total:.0f}%',
                            xy=(bar.get_x() + bar.get_width() / 2, height),
                            xytext=(0, 2), textcoords='offset points',
                            ha='center', fontsize=label_size, color=INK)
    ax.set_xticks(range(len(tracks)))
    # Instance count per track, not row count: planners may cover different tracks.
    per_track = {t: len({r['task_id'] for r in rows if r['track'] == t}) for t in tracks}
    ax.set_xticklabels([f'{t}\n({per_track[t]} instances)' for t in tracks])
    ax.set_ylabel('instances solved')
    ax.set_title('Coverage per track')
    ax.grid(axis='x', visible=False)
    ax.set_axisbelow(True)
    _integer_yaxis(ax)
    ax.legend(loc='upper left', bbox_to_anchor=(1.01, 1.0))
    fig.tight_layout()
    return _save(fig, plots_dir, 'coverage-per-track', formats)


def _outcome_bars(plt, rows, planners, plots_dir, formats) -> str:
    statuses = [s for s in STATUS_ORDER if s in {r['status'] for r in rows}]
    fig, ax = plt.subplots(figsize=(5.6, 0.55 * len(planners) + 1.9))
    left = [0.0] * len(planners)
    positions = list(range(len(planners)))
    for status in statuses:
        face, hatch = STATUS_STYLE.get(status, ('#ffffff', ''))
        widths = [sum(1 for r in rows if r['planner'] == p and r['status'] == status)
                  for p in planners]
        ax.barh(positions, widths, left=left, height=0.62, label=status.capitalize(),
                color=face, hatch=hatch, edgecolor=INK, linewidth=0.8)
        for index, width in enumerate(widths):
            # Selective labels: only segments wide enough to hold one, in the
            # ink that contrasts with the segment's fill.
            attempted = max(1, sum(1 for r in rows if r['planner'] == planners[index]))
            if width and width / attempted > 0.06:
                ax.annotate(str(width), xy=(left[index] + width / 2, positions[index]),
                            ha='center', va='center', fontsize=7,
                            color=SURFACE if _is_dark(face) else INK,
                            bbox=None if not hatch else dict(
                                boxstyle='square,pad=0.15', facecolor=SURFACE,
                                edgecolor='none'))
        left = [l + w for l, w in zip(left, widths)]
    ax.set_yticks(positions)
    ax.set_yticklabels(planners)
    ax.invert_yaxis()
    ax.set_xlabel('instances')
    ax.set_title('Outcome breakdown')
    ax.grid(axis='y', visible=False)
    ax.set_axisbelow(True)
    ax.legend(loc='lower center', bbox_to_anchor=(0.5, -0.42), ncol=min(5, len(statuses)))
    fig.tight_layout()
    return _save(fig, plots_dir, 'outcomes', formats)


def _is_dark(hex_color: str) -> bool:
    red, green, blue = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.299 * red + 0.587 * green + 0.114 * blue < 128


def _runtime_scatter(plt, rows, left_planner, right_planner, tracks, plots_dir, formats) -> str:
    """Instance-by-instance runtime comparison of two planners, log-log.

    Faceted per track, one panel each on shared axes, and within a panel every
    domain has its own marker shape -- the way the planning literature draws
    it. Unsolved instances are pinned to the cutoff line and drawn hollow, so a
    point on the border reads as "did not finish" rather than "took exactly
    the limit".
    """
    by_task = defaultdict(dict)
    for row in rows:
        if row['task_id']:
            by_task[row['task_id']][row['planner']] = row

    limit = _time_limit(rows)
    by_domain = _scatter_points(by_task, left_planner, right_planner, limit)
    if not by_domain:
        return ''

    # Anchor the range on the data rather than on the measurement floor: a decade
    # of empty axes below the fastest point wastes most of the plot. One range
    # for every panel, so the panels compare directly. Runs clamped to MIN_TIME
    # must still stand off the spine, hence the 0.6.
    everything = [v for p in by_domain.values()
                  for v in p['xs'] + p['ys'] + p['hollow_x'] + p['hollow_y']]
    low = max(MIN_TIME * 0.6, min(everything) * 0.6)
    high = limit * 1.6

    # A panel is (track, [(domain, marker, fill, points), ...]).
    panels = []
    for track in tracks:
        domains = sorted(d for t, d in by_domain if t == track)
        if domains:
            panels.append((track, [
                (domain, DOMAIN_MARKERS[i % len(DOMAIN_MARKERS)],
                 DOMAIN_FILLS[(i // len(DOMAIN_MARKERS)) % len(DOMAIN_FILLS)],
                 by_domain[(track, domain)]) for i, domain in enumerate(domains)]))

    # The legend goes under each panel, and tight_layout would otherwise shrink
    # the axes to fit it: give the figure the legend's height up front.
    def _legend_columns(count: int) -> int:
        return 3 if count > 4 else min(3, max(1, count))

    legend_rows = 0
    for _track, groups in panels:
        count = len(groups) + (1 if any(g[3]['hollow_x'] for g in groups) else 0)
        legend_rows = max(legend_rows, -(-count // _legend_columns(count)))
    fig, axes = plt.subplots(1, len(panels),
                             figsize=(3.6 * len(panels), 3.9 + 0.17 * legend_rows),
                             sharex=True, sharey=True, squeeze=False)
    axes = list(axes[0])
    for ax, (track, groups) in zip(axes, panels):
        hollow_seen = False
        for domain, marker, fill, points in groups:
            if points['xs']:
                # A thin white rim separates overlapping filled markers.
                ax.scatter(points['xs'], points['ys'], s=26, color=fill, marker=marker,
                           edgecolor=SURFACE, linewidth=0.6, zorder=3, label=domain)
            if points['hollow_x']:
                hollow_seen = True
                ax.scatter(points['hollow_x'], points['hollow_y'], s=26, facecolor=SURFACE,
                           edgecolor=fill, marker=marker, linewidth=1.0, zorder=3)
                if not points['xs']:
                    # Keep the domain in the legend even if nothing was solved.
                    ax.scatter([], [], s=26, color=fill, marker=marker, label=domain)
        ax.plot([low, high], [low, high], color=AXIS, linewidth=0.8, zorder=1)
        ax.axvline(limit, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=0)
        ax.axhline(limit, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=0)
        ax.set_xscale('log')
        ax.set_yscale('log')
        ax.set_xlim(low, high)
        ax.set_ylim(low, high)
        ax.set_aspect('equal')
        ax.set_xlabel(f'{left_planner} — runtime (s)')
        ax.set_title(track, fontsize=9)
        # Legend below the panel: both triangles carry a "which side means what"
        # note, and a corner legend would sit on top of one. Domains differ per
        # track, so every panel carries its own.
        handles, labels = ax.get_legend_handles_labels()
        if hollow_seen:
            from matplotlib.lines import Line2D
            handles.append(Line2D([], [], linestyle='none', marker='o', markersize=5,
                                  markerfacecolor=SURFACE, markeredgecolor=INK))
            labels.append('hollow: one unsolved (at the limit)')
        ax.legend(handles, labels, loc='upper center', bbox_to_anchor=(0.5, -0.17),
                  fontsize=6.5, ncol=_legend_columns(len(labels)),
                  handletextpad=0.3, columnspacing=1.0)
    axes[0].set_ylabel(f'{right_planner} — runtime (s)')
    # Which side of the diagonal means what, stated rather than left to the
    # reader -- once, in the first panel, and clear of the cutoff lines where
    # the hollow markers sit.
    axes[0].annotate(f'{right_planner} slower', xy=(0.04, 0.86), xycoords='axes fraction',
                     fontsize=7, color=INK_MUTED)
    axes[0].annotate(f'{left_planner} slower', xy=(0.88, 0.05), xycoords='axes fraction',
                     fontsize=7, color=INK_MUTED, ha='right')
    fig.tight_layout()
    # Title just above the panels, wherever tight_layout put them: a square
    # panel in a tall figure leaves slack above it, and the title would float.
    panel_top = max(ax.get_position().y1 for ax in axes)
    fig.suptitle('Runtime per track', x=0.01, y=min(0.995, panel_top + 0.26 / fig.get_figheight()),
                 ha='left', va='bottom', fontsize=10)
    name = f'runtime-{_slug(left_planner)}-vs-{_slug(right_planner)}'
    return _save(fig, plots_dir, name, formats)


def _scatter_points(by_task, left_planner, right_planner,
                    limit: float) -> Dict[Tuple[str, str], Dict[str, List[float]]]:
    """``(track, domain) -> points`` for a runtime scatter: ``xs``/``ys`` where
    both planners solved the instance, ``hollow_x``/``hollow_y`` where one did
    not (pinned to the limit). Only groups with at least one point are returned."""
    groups: Dict[Tuple[str, str], Dict[str, List[float]]] = defaultdict(
        lambda: {'xs': [], 'ys': [], 'hollow_x': [], 'hollow_y': []})
    for per_planner in by_task.values():
        left_row, right_row = per_planner.get(left_planner), per_planner.get(right_planner)
        if not (left_row and right_row):
            continue
        left_ok, right_ok = is_solved(left_row), is_solved(right_row)
        x = _runtime(left_row) if left_ok else None
        y = _runtime(right_row) if right_ok else None
        if x is None and y is None:
            continue
        key = (left_row['track'] or '', left_row['domain'] or '')
        if left_ok and right_ok:
            groups[key]['xs'].append(x)
            groups[key]['ys'].append(y)
        else:
            groups[key]['hollow_x'].append(x if x is not None else limit)
            groups[key]['hollow_y'].append(y if y is not None else limit)
    return dict(groups)


def _common_quality(rows, planners, track: str) -> Tuple[Dict[str, List[float]], int]:
    """Per planner, the plan cost relative to the best cost on that instance,
    over the instances of ``track`` that *every* planner that ran the track
    solved. Restricting to the common set is what makes the distributions
    comparable: a planner that only solves the easy half would otherwise look
    better on raw cost. Returns the ratios and the common instance count."""
    ran = [p for p in planners if any(r['planner'] == p and r['track'] == track for r in rows)]
    index = _solved_index([r for r in rows if r['track'] == track])
    ratios: Dict[str, List[float]] = {p: [] for p in ran}
    common = 0
    for by_planner in index.values():
        costs = {p: _cost(by_planner[p]) for p in ran if p in by_planner}
        if len(costs) < len(ran) or any(c in (None, 0) for c in costs.values()):
            continue
        common += 1
        best = min(costs.values())
        for planner, cost in costs.items():
            ratios[planner].append(cost / best)
    return ratios, common


def _quality_violins(plt, rows, planners, tracks, plots_dir, formats) -> str:
    """Plan quality as one violin per planner, per track, over the instances
    every planner solved. Cost is the plan length, or the makespan for a
    schedule, divided by the best cost found for that instance -- so 1 is
    "as good as the best planner", 1.5 is "half again as long", and domains
    with different cost scales can share one axis.
    """
    per_track = {t: _common_quality(rows, planners, t) for t in tracks}
    # A violin needs a few instances to say anything; a track with fewer is
    # reported, not drawn.
    drawn = [t for t, (ratios, common) in per_track.items()
             if common >= 3 and len(ratios) >= 2]
    for track, (ratios, common) in per_track.items():
        if track not in drawn:
            print(f'note: plan-quality violins skipped for {track} ({len(ratios)} planner(s), '
                  f'{common} instance(s) solved by every planner)')
    if not drawn:
        return ''

    # Width follows the number of violins, and each panel gets its share, so a
    # track that only two planners ran is not stretched to match the others.
    counts = [len(per_track[t][0]) for t in drawn]
    fig, axes = plt.subplots(1, len(drawn), figsize=(0.8 + 0.8 * sum(counts), 3.4),
                             sharey=True, squeeze=False,
                             gridspec_kw={'width_ratios': counts})
    axes = list(axes[0])
    top = 1.0
    for ax, track in zip(axes, drawn):
        ratios, common = per_track[track]
        names = list(ratios)
        positions = list(range(1, len(names) + 1))
        for position, name in zip(positions, names):
            values = ratios[name]
            top = max(top, max(values))
            if len(set(values)) < 2:
                # A degenerate distribution (every instance at the best cost,
                # typically) has no density to draw: show it as its one value.
                ax.plot([position - 0.3, position + 0.3], [values[0]] * 2, color=INK,
                        linewidth=1.5, solid_capstyle='butt', zorder=3)
                continue
            parts = ax.violinplot([values], positions=[position], widths=0.75,
                                  showmeans=False, showmedians=False, showextrema=False)
            for body in parts['bodies']:
                body.set_facecolor(INSTANCES_FILL)
                body.set_edgecolor(INK)
                body.set_linewidth(0.8)
                body.set_alpha(1.0)
            # Inner box: interquartile bar, median as a white dot, whiskers to
            # the extremes as a thin line -- the reading a reviewer expects.
            q1, median, q3 = _quantiles(values)
            ax.plot([position, position], [min(values), max(values)], color=INK,
                    linewidth=0.8, zorder=3)
            ax.plot([position, position], [q1, q3], color=INK, linewidth=4.0,
                    solid_capstyle='butt', zorder=4)
            ax.plot([position], [median], marker='o', markersize=3.5, color=SURFACE,
                    markeredgecolor=INK, markeredgewidth=0.8, linestyle='none', zorder=5)
        ax.axhline(1.0, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=0)
        ax.set_xticks(positions)
        ax.set_xticklabels(names, rotation=20, ha='right')
        ax.set_xlim(0.4, len(names) + 0.6)
        ax.set_title(f'{track} ({common} instances solved by all)', fontsize=9)
        ax.grid(axis='x', visible=False)
    axes[0].set_ylabel('plan cost / best cost (1 = best)')
    axes[0].set_ylim(0.97, top * 1.06)
    fig.suptitle('Plan quality on the commonly solved instances', x=0.01, ha='left',
                 fontsize=10, y=1.0)
    fig.tight_layout()
    return _save(fig, plots_dir, 'plan-quality', formats)


def _quantiles(values: Sequence[float]) -> Tuple[float, float, float]:
    """First quartile, median, third quartile by linear interpolation."""
    ordered = sorted(values)

    def at(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)

    return at(0.25), at(0.5), at(0.75)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _write(path: str, text: str) -> None:
    with open(path, 'w') as handle:
        handle.write(text if text.endswith('\n') else text + '\n')


def _slug(text: str) -> str:
    import re
    return re.sub(r'[^A-Za-z0-9._-]+', '_', text)


def _mean(values):
    return sum(values) / len(values) if values else None


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def _fmt(value) -> str:
    return '-' if value is None else f'{value:.2f}'
