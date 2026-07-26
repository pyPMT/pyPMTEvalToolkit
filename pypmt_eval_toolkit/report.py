"""Paper-ready tables and plots: ``pypmtevalcli report``.

Produces what a planning paper's evaluation section is made of — a per-domain
coverage table, the outcome breakdown (timeouts, memouts, errors), IPC-style
time and quality scores, and the survival ("cactus") plots plus per-planner
scatters.

Tables are written as text *and* as LaTeX (booktabs) ready to ``\\input``.
Plots need matplotlib (``pip install "pypmt-eval-toolkit[plots]"``); without it the
tables are still produced and the plots are skipped with a note.

Design notes for the figures, since they end up in a paper:

* colors come from a CVD-validated categorical set, and every series also
  carries a distinct marker and dash pattern, so the figures survive both
  colorblind readers and greyscale printing;
* series colors are capped at three in the scatters (where every pair of colors
  is compared at once) and at six in the line plots (where only adjacent pairs
  are); past that the data is faceted rather than given new hues;
* status colors are reserved for outcomes (solved/timeout/memout/error) and
  never reused as a series color;
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

# -- palette (validated: worst adjacent CVD dE 9.1, worst all-pairs 9.2) ------
SERIES = ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300']
SERIES_MARKERS = ['o', 's', '^', 'D', 'v', 'P']
SERIES_DASHES = [(None, None), (5, 2), (1.5, 1.5), (7, 2, 1.5, 2), (3, 1.5), (9, 3)]
SCATTER_SERIES_CAP = 3          # all pairs of colors are compared at once there
LINE_SERIES_CAP = 6

STATUS_COLORS = {
    SOLVED: '#0ca30c',          # good
    TIMEOUT: '#fab219',         # warning
    MEMOUT: '#ec835a',          # serious
    ERROR: '#d03b3b',           # critical
    KILLED: '#d03b3b',
    EXHAUSTED: '#898781',
    UNSOLVABLE: '#52514e',
    UNSUPPORTED: '#c3c2b7',
    MISSING: '#e1e0d9',
}
STATUS_ORDER = [SOLVED, TIMEOUT, MEMOUT, EXHAUSTED, UNSOLVABLE, UNSUPPORTED, ERROR, KILLED, MISSING]

INK = '#0b0b0b'
INK_MUTED = '#898781'
GRID = '#e1e0d9'
AXIS = '#c3c2b7'
SURFACE = '#fcfcfb'

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

    pairs = [(a, b) for i, a in enumerate(planners) for b in planners[i + 1:]]
    if len(pairs) > 6:
        print(f'note: {len(pairs)} planner pairs, plotting the first 6 scatters')
        pairs = pairs[:6]
    for left, right in pairs:
        written.append(_scatter(plt, rows, left, right, tracks, plots_dir, formats,
                                what='runtime'))
        quality = _scatter(plt, rows, left, right, tracks, plots_dir, formats, what='cost')
        if quality:
            written.append(quality)

    return [w for w in written if w]


def _style(matplotlib) -> None:
    matplotlib.rcParams.update({
        'figure.facecolor': SURFACE, 'axes.facecolor': SURFACE, 'savefig.facecolor': SURFACE,
        'font.family': 'sans-serif',
        'font.sans-serif': ['DejaVu Sans', 'Helvetica', 'Arial'],
        'font.size': 9, 'axes.titlesize': 10, 'axes.labelsize': 9, 'legend.fontsize': 8,
        'xtick.labelsize': 8, 'ytick.labelsize': 8,
        'axes.edgecolor': AXIS, 'axes.labelcolor': INK, 'text.color': INK,
        'xtick.color': INK_MUTED, 'ytick.color': INK_MUTED,
        'axes.grid': True, 'grid.color': GRID, 'grid.linewidth': 0.6,
        'axes.spines.top': False, 'axes.spines.right': False,
        'lines.linewidth': 2.0, 'legend.frameon': False,
        'figure.dpi': 200, 'savefig.bbox': 'tight', 'savefig.pad_inches': 0.02,
        'pdf.fonttype': 42,          # editable text in the PDF, as venues ask
    })


def _save(fig, plots_dir: str, name: str, formats: Sequence[str]) -> str:
    for extension in formats or ['pdf', 'png']:
        fig.savefig(os.path.join(plots_dir, f'{name}.{extension}'))
    fig.clf()
    return f'{name}.{",".join(formats or ["pdf", "png"])}'


def _survival_curve(rows) -> Tuple[List[float], List[int]]:
    """Sorted solve times and the running count of instances solved by then."""
    times = sorted(t for t in (_runtime(r) for r in rows if is_solved(r)) if t is not None)
    return times, list(range(1, len(times) + 1))


def _draw_survival(ax, rows, planners, limit: float, label_ends: bool = True) -> None:
    # Scale and range before any data: switching an empty axes to log while it
    # still holds the default (0, 1) range warns about the non-positive bound.
    ax.set_xscale('log')
    ax.set_xlim(MIN_TIME, limit * 1.05)
    ends = []
    for index, planner in enumerate(planners):
        times, counts = _survival_curve([r for r in rows if r['planner'] == planner])
        if not times:
            continue
        color = SERIES[index % len(SERIES)]
        # Step to the limit so the line ends at the cutoff rather than mid-air.
        ax.step(times + [limit], counts + [counts[-1]], where='post', color=color,
                dashes=SERIES_DASHES[index % len(SERIES_DASHES)],
                marker=SERIES_MARKERS[index % len(SERIES_MARKERS)],
                markevery=max(1, len(times) // 8), markersize=4.5, label=planner)
        ends.append((counts[-1], planner, color))

    if label_ends and ends:
        # Direct labels, so identity is never carried by color alone -- stacked
        # apart where two planners finish on the same count, which is the common
        # case and would otherwise print one label on top of the other.
        top = max(end for end, _, _ in ends)
        gap = max(1.0, top * 0.055)
        placed: List[float] = []
        for end, planner, color in sorted(ends, key=lambda e: -e[0]):
            target = end if not placed else min(end, placed[-1] - gap)
            placed.append(target)
            ax.annotate(planner, xy=(limit, target), xytext=(-4, 3),
                        textcoords='offset points', ha='right', fontsize=7.5, color=color)
    ax.axvline(limit, color=AXIS, linewidth=1.0, dashes=(2, 2), zorder=0)
    _integer_yaxis(ax)


def _integer_yaxis(ax) -> None:
    """Instances are countable; half an instance is never a tick."""
    from matplotlib.ticker import MaxNLocator
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))


def _survival_plot(plt, rows, planners, plots_dir, formats) -> str:
    limit = _time_limit(rows)
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    _draw_survival(ax, rows, planners, limit)
    ax.set_xlabel('time (s, log scale)')
    ax.set_ylabel('instances solved')
    ax.set_title('Survival plot — instances solved within a time budget')
    ax.legend(loc='upper left')
    fig.tight_layout()
    return _save(fig, plots_dir, 'survival', formats)


def _survival_facets(plt, rows, planners, tracks, plots_dir, formats) -> str:
    limit = _time_limit(rows)
    fig, axes = plt.subplots(1, len(tracks), figsize=(3.1 * len(tracks), 3.0), sharex=True)
    axes = axes if hasattr(axes, '__len__') else [axes]
    for ax, track in zip(axes, tracks):
        _draw_survival(ax, [r for r in rows if r['track'] == track], planners, limit,
                       label_ends=False)
        ax.set_title(track)
        ax.set_xlabel('time (s)')
    axes[0].set_ylabel('instances solved')
    axes[-1].legend(loc='lower right')
    fig.suptitle('Survival plots per track', y=1.02)
    fig.tight_layout()
    return _save(fig, plots_dir, 'survival-per-track', formats)


def _memory_survival_plot(plt, rows, planners, plots_dir, formats) -> str:
    fig, ax = plt.subplots(figsize=(5.2, 3.4))
    drawn = False
    for index, planner in enumerate(planners):
        memory = sorted(r['peak_memory_mb'] for r in rows
                        if r['planner'] == planner and is_solved(r)
                        and r['peak_memory_mb'] is not None)
        if not memory:
            continue
        drawn = True
        ax.step(memory, range(1, len(memory) + 1), where='post',
                color=SERIES[index % len(SERIES)],
                dashes=SERIES_DASHES[index % len(SERIES_DASHES)],
                marker=SERIES_MARKERS[index % len(SERIES_MARKERS)],
                markevery=max(1, len(memory) // 8), markersize=4.5, label=planner)
    if not drawn:
        fig.clf()
        return ''
    ax.set_xscale('log')
    ax.set_xlabel('peak memory (MB, log scale)')
    ax.set_ylabel('instances solved')
    ax.set_title('Memory profile — instances solved within a memory budget')
    _integer_yaxis(ax)
    ax.legend(loc='upper left')
    fig.tight_layout()
    return _save(fig, plots_dir, 'memory-survival', formats)


def _coverage_bars(plt, rows, planners, tracks, plots_dir, formats) -> str:
    fig, ax = plt.subplots(figsize=(5.4, 3.2))
    width = 0.8 / max(1, len(planners))
    for index, planner in enumerate(planners):
        heights, totals = [], []
        for track in tracks:
            subset = [r for r in rows if r['planner'] == planner and r['track'] == track]
            heights.append(sum(1 for r in subset if is_solved(r)))
            totals.append(len(subset))
        positions = [i + index * width - 0.4 + width / 2 for i in range(len(tracks))]
        # The instance count behind each bar: without it, 4/4 on a 4-instance
        # track draws exactly as tall as 4/88 on a numeric one.
        ax.bar(positions, totals, width=width * 0.92, color=GRID, zorder=1,
               label='instances' if index == 0 else None)
        # 2px surface gap between adjacent bars.
        bars = ax.bar(positions, heights, width=width * 0.92, zorder=2,
                      color=SERIES[index % len(SERIES)], label=planner,
                      edgecolor=SURFACE, linewidth=1.0)
        for bar, height, total in zip(bars, heights, totals):
            if total:
                ax.annotate(f'{100.0 * height / total:.0f}%',
                            xy=(bar.get_x() + bar.get_width() / 2, height),
                            xytext=(0, 2), textcoords='offset points',
                            ha='center', fontsize=7, color=INK_MUTED)
    ax.set_xticks(range(len(tracks)))
    # Instance count per track, not row count: planners may cover different tracks.
    per_track = {t: len({r['task_id'] for r in rows if r['track'] == t}) for t in tracks}
    ax.set_xticklabels([f'{t}\n({per_track[t]} instances)' for t in tracks])
    ax.set_ylabel('instances solved')
    ax.set_title('Coverage per track')
    ax.grid(axis='x', visible=False)
    _integer_yaxis(ax)
    ax.legend(loc='upper right')
    fig.tight_layout()
    return _save(fig, plots_dir, 'coverage-per-track', formats)


def _outcome_bars(plt, rows, planners, plots_dir, formats) -> str:
    statuses = [s for s in STATUS_ORDER if s in {r['status'] for r in rows}]
    fig, ax = plt.subplots(figsize=(5.6, 0.55 * len(planners) + 1.9))
    left = [0.0] * len(planners)
    positions = list(range(len(planners)))
    for status in statuses:
        widths = [sum(1 for r in rows if r['planner'] == p and r['status'] == status)
                  for p in planners]
        ax.barh(positions, widths, left=left, height=0.62, label=status.capitalize(),
                color=STATUS_COLORS.get(status, INK_MUTED),
                edgecolor=SURFACE, linewidth=1.0)
        for index, width in enumerate(widths):
            # Selective labels: only segments wide enough to hold one.
            if width and width / max(1, sum(1 for r in rows if r['planner'] == planners[index])) > 0.06:
                ax.annotate(str(width), xy=(left[index] + width / 2, positions[index]),
                            ha='center', va='center', fontsize=7, color='#ffffff')
        left = [l + w for l, w in zip(left, widths)]
    ax.set_yticks(positions)
    ax.set_yticklabels(planners)
    ax.invert_yaxis()
    ax.set_xlabel('instances')
    ax.set_title('Outcome breakdown')
    ax.grid(axis='y', visible=False)
    ax.legend(loc='lower center', bbox_to_anchor=(0.5, -0.42), ncol=min(5, len(statuses)))
    fig.tight_layout()
    return _save(fig, plots_dir, 'outcomes', formats)


def _scatter(plt, rows, left_planner, right_planner, tracks, plots_dir, formats,
             what: str) -> str:
    """Instance-by-instance comparison of two planners, log-log.

    Unsolved instances are pinned to the cutoff line and drawn hollow, so a
    point on the border reads as "did not finish" rather than "took exactly the
    limit" — the convention the planning literature uses.
    """
    by_task = defaultdict(dict)
    for row in rows:
        if row['task_id']:
            by_task[row['task_id']][row['planner']] = row

    value = _runtime if what == 'runtime' else _cost
    limit = _time_limit(rows)
    fig, ax = plt.subplots(figsize=(3.9, 3.9))
    # Color by track: at most three, which is the all-pairs-validated cap.
    palette = {track: SERIES[i % SCATTER_SERIES_CAP] for i, track in enumerate(tracks)}
    markers = {track: SERIES_MARKERS[i % SCATTER_SERIES_CAP] for i, track in enumerate(tracks)}

    plotted = 0
    top = MIN_TIME
    bottom = float('inf')
    for track in tracks:
        xs, ys, hollow_x, hollow_y = [], [], [], []
        for _task, per_planner in by_task.items():
            left_row, right_row = per_planner.get(left_planner), per_planner.get(right_planner)
            if not (left_row and right_row) or left_row['track'] != track:
                continue
            left_ok, right_ok = is_solved(left_row), is_solved(right_row)
            if what == 'cost' and not (left_ok and right_ok):
                continue            # cost is only defined where both found a plan
            x = value(left_row) if left_ok else None
            y = value(right_row) if right_ok else None
            if x is None and y is None:
                continue
            if what == 'runtime':
                target_x, target_y = (x if x is not None else limit), (y if y is not None else limit)
                (xs if (left_ok and right_ok) else hollow_x).append(target_x)
                (ys if (left_ok and right_ok) else hollow_y).append(target_y)
                top = max(top, target_x, target_y)
                bottom = min(bottom, target_x, target_y)
            else:
                xs.append(x)
                ys.append(y)
                top = max(top, x, y)
                bottom = min(bottom, x, y)
        if xs:
            ax.scatter(xs, ys, s=26, color=palette[track], marker=markers[track],
                       edgecolor=SURFACE, linewidth=0.6, label=track, zorder=3)
            plotted += len(xs)
        if hollow_x:
            ax.scatter(hollow_x, hollow_y, s=26, facecolor='none', edgecolor=palette[track],
                       marker=markers[track], linewidth=1.2, zorder=3,
                       label=f'{track} (unsolved)')
            plotted += len(hollow_x)

    if not plotted:
        fig.clf()
        return ''

    # Anchor the range on the data rather than on the measurement floor: a decade
    # of empty axes below the fastest point wastes most of the plot.
    low = max(MIN_TIME, bottom * 0.6) if what == 'runtime' else max(0.5, bottom * 0.6)
    high = (limit * 1.6) if what == 'runtime' else top * 1.4
    ax.plot([low, high], [low, high], color=AXIS, linewidth=1.0, zorder=1)
    if what == 'runtime':
        ax.axvline(limit, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=0)
        ax.axhline(limit, color=AXIS, linewidth=0.8, dashes=(2, 2), zorder=0)
    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlim(low, high)
    ax.set_ylim(low, high)
    unit = 'runtime (s)' if what == 'runtime' else 'plan cost (length / makespan)'
    ax.set_xlabel(f'{left_planner} — {unit}')
    ax.set_ylabel(f'{right_planner} — {unit}')
    ax.set_title('Runtime' if what == 'runtime' else 'Plan quality', loc='left')
    # Which side of the diagonal means what, stated rather than left to the reader.
    ax.annotate(f'{right_planner} slower' if what == 'runtime' else f'{right_planner} worse',
                xy=(0.04, 0.94), xycoords='axes fraction', fontsize=7, color=INK_MUTED)
    ax.annotate(f'{left_planner} slower' if what == 'runtime' else f'{left_planner} worse',
                xy=(0.96, 0.06), xycoords='axes fraction', fontsize=7, color=INK_MUTED,
                ha='right')
    # Legend below the axes: both triangles carry a "which side means what" note,
    # and a corner legend would sit on top of one of them.
    handles = ax.get_legend_handles_labels()[0]
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.17), fontsize=7,
              ncol=min(3, max(1, len(handles))))
    fig.tight_layout()
    name = f'{what}-{_slug(left_planner)}-vs-{_slug(right_planner)}'
    return _save(fig, plots_dir, name, formats)


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
