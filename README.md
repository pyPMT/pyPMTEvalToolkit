# pyPMTEvalToolkit — benchmark harness for Unified Planning engines

Runs **any planner reachable through the [Unified Planning](https://github.com/aiplan4eu/unified-planning) API**
over the classical, numeric and temporal benchmark sets, one job per
(planner, instance) pair, and turns the results into coverage tables, a CSV and
paper-ready figures.

Nothing here imports a planner by name. A planner configuration names a UP
engine; the engine is resolved at run time from whatever is installed — or from
a module or class you point at. Adding a planner to a sweep is one JSON file.

The whole thing is one script away:

```bash
./setup_benchmark.sh          # asks which configuration and which limits, then does everything
```

It starts by asking which experiment configuration to run:

```
Available experiment configurations:
  1) any-engine     3 planner(s): installed-plugin, my-own-engine, self-registering-module
  2) default        4 planner(s): enhsp, fast-downward, smt-par-planner, smt-seq-planner
  3) demo           2 planner(s): smt-par-planner, smt-seq-planner
  (or the path to a configuration directory of your own)

Experiment configuration [default]:
```

then where everything should live (one root directory holds the venv, the
benchmark tasks, and the generated commands and results), the time/memory
limits, the slurm knobs, and any extra python packages the venv should get —
planners published on PyPI, or a local checkout given as a directory, which
is installed editable so a planner under development stays live. From there
it creates a
virtualenv, installs this toolkit plus the engines that configuration's
planners actually name plus those packages, clones the benchmark
repositories, writes an experiment with the limits you gave, and generates
the slurm job arrays. It ends by printing the `sbatch` command that starts
the sweep.

Everything it asks for also has a flag, so a scripted run is the same script:

```bash
./setup_benchmark.sh --list-configs                       # just show the choices
./setup_benchmark.sh --config any-engine --yes            # by name, no prompts
./setup_benchmark.sh --config /path/to/my-experiment --time-limit 30m \
                     --tracks "numeric temporal" --max-instances 20 --yes
./setup_benchmark.sh --work-dir /scratch/$USER/pypmt-sweep \
                     --packages "up-enhsp ~/dev/my-planner" --yes
```

The chosen configuration is a *template*: it is copied into a working
experiment (`<work-dir>/experiments/<config>`, alongside
`<work-dir>/sandbox/<config>`) whose limits the script then rewrites, so the
configurations under `exp-configurations/` stay as you committed them. One
experiment and one sandbox per configuration, since seeding a second
configuration into the same directory would add its planners to the first
one's — the script records which configuration an experiment came from and
warns if you do.

## The stages

```
pypmtevalcli init      → an experiment directory (limits + planner configurations)
pypmtevalcli engines   → which UP engines this environment can actually run
pypmtevalcli discover  → what tasks a benchmark repository holds
pypmtevalcli generate  → one run command per (planner, task), plus slurm arrays
pypmtevalcli solve     → run ONE pair under its limits, dump a JSON result   (slurm calls this)
pypmtevalcli analyze   → results.csv + a coverage report
pypmtevalcli report    → paper-ready tables (text + LaTeX) and figures
```

Everything except `solve` and `engines` is stdlib-only — none of it imports
`unified_planning`, so you can generate a sweep on a laptop and let only the
compute nodes carry the planners. (`report`'s *figures* want matplotlib; its
tables do not.)

## Benchmarking any UP engine

An engine is named by `up-planner-name` and made available in one of four ways,
tried in order:

| in the planner configuration | when to use it |
|---|---|
| nothing extra | the engine is a UP builtin, or an installed plugin UP already knows |
| nothing extra (auto-discovery) | an installed plugin that registers itself on import (`up_enhsp`, `up_pypmt`, `up_symk`, …) — every distribution that publishes a `unified_planning` entry point or is named `up-*` is imported when the engine is not found otherwise |
| `"up-planner-module": "my_plugin"` | a plugin that self-registers on import but ships no entry point |
| `"up-planner-class": "my_pkg.my_module:MyEngine"` | an engine class that is not a packaged plugin at all — registered with the UP factory under `up-planner-name` |

Check before you submit ten thousand jobs:

```bash
pypmtevalcli engines --exp-dir experiment
```

lists the engines this environment can instantiate and resolves every planner
of the experiment, exiting non-zero if one of them names an engine that is not
available (`setup_benchmark.sh` runs this for you).

Engines are **optional extras** of this package, so a venv only carries the
ones you benchmark:

```bash
pip install ".[pypmt]"                    # SMTPlanner (pyPMT)
pip install ".[enhsp,fast-downward]"      # a couple of externals
pip install ".[engines,plots]"            # everything we know about, plus figures
```

Anything not listed there is fine too — install it yourself and name it in a
planner configuration.

## Where the benchmarks come from

| track | repository | what is taken |
|---|---|---|
| classical | [AI-Planning/classical-domains](https://github.com/AI-Planning/classical-domains) | every domain with an `api.py` |
| numeric | [pyPMT/numeric-domains](https://github.com/pyPMT/numeric-domains) | every domain with an `api.py` |
| temporal | [potassco/pddl-instances](https://github.com/potassco/pddl-instances) | the `*-time*` / `*-temporal*` IPC domains |

A task's track is decided by **reading its domain file**, not by which
repository it came from: `:durative-action` (or a PDDL+ `:process`/`:event`)
makes it temporal, a `(:functions ...)` block makes it numeric, anything else
is classical. That keeps the labels honest whatever you point the tool at —
including your own task directory, which needs no `api.py`:

```bash
pypmtevalcli discover --tasks-dir mine=/path/to/my-domains
```

Four repository layouts are recognised: an `api.py` domain directory, an IPC
`instances/` directory (with a shared domain file or one `domains/domain-N.pddl`
per instance), one sub-directory per instance under `instances/`, and a plain
domain file with its problems as siblings.

Discovery is defensive about the state these repositories are actually in. An
`api.py` is authoritative *for its own directory only* — a few are copy-pasted
from a sibling domain and resolve to that domain's files, which would file its
instances under the wrong name and hide the ones actually present; those are
ignored and the directory is read directly instead. The domain file does not
have to be called `domain.pddl` (`domain_snp.pddl` and `korf1_domain.pddl` are
found too). And a directory that looks like a domain but yields nothing is
reported on stderr rather than dropped:

```
note: no tasks from .../numeric-domains/foo (api.py lists no existing (domain, problem) pair)
```

so a benchmark set never contributes fewer instances than it holds without
saying so.

## Experiment configuration

An experiment is a directory:

```
experiment/
├── exp-details.json        limits + task selection
└── planners/
    ├── smt-seq-planner.json
    └── enhsp.json
```

```jsonc
// exp-details.json
{
    "cfgs": {
        "timelimit": "00:30:00",        // also accepts "30m" or 1800
        "memorylimit": "8GB",           // also accepts 8192 (MB)
        "slurm-time-headroom": "00:05:00",
        "slurm-memory-headroom": "1GB",
        "validate-plans": true,
        "slurm": {
            "cpus-per-task": 1,
            "partition": null,
            "account": null,
            "max-parallel-jobs": 50,    // --array=...%50
            "max-array-size": 1000,     // split arrays larger than this
            "extra-directives": []      // verbatim #SBATCH lines
        }
    },
    "tasks": {
        "tracks": ["classical", "numeric", "temporal"],
        "max-instances-per-domain": 10,
        "selection": "even",            // "even" spreads across sizes, "first" takes the smallest
        "include-domains": [],          // glob patterns
        "exclude-domains": [],
        "ipc-years": [],
        "selected-tasks": []            // explicit [ipc-year, domain, instance] triples
    }
}
```

```jsonc
// planners/smt-seq-planner.json
{
    "planner-tag": "SequentialSMT",        // names the result directory and the CSV column
    "up-planner-name": "SMTPlanner",       // the UP engine to run
    "planner-params": {                    // passed to OneshotPlanner(params=...) verbatim
        "encoder": "EncoderSequentialSMT",
        "upper-bound": 1000,
        "configuration": "seq",
        "compilationlist": [
            ["up_quantifiers_remover", "QUANTIFIERS_REMOVING"],
            ["fast-downward-reachability-grounder", "GROUNDING"]
        ]
    },
    "tracks": ["classical", "numeric"]     // optional: restrict this planner to some tracks
}
```

**Every `.json` in `planners/` is benchmarked.** Nothing filters the set — to
leave a planner out, delete its file.

`planner-params` reaches the engine as written, with three conveniences JSON
cannot express itself: a `compilationlist` names its `CompilationKind` as a
string and the runner resolves it (the older `compliation-list` spelling still
works), a `stats_file` is redirected into *this run's* directory and read back
into the result JSON, and `"true"`/`"false"` strings become booleans.

`exp-configurations/` holds the ready-made experiments `setup_benchmark.sh`
offers: `default/` (the four planners a normal sweep runs), `any-engine/` (one
configuration per engine-registration style) and `demo/` (the two SMT
configurations). Add a directory there — `exp-details.json` plus `planners/` —
and it shows up in the menu; `--config` also takes a path to one kept
elsewhere.

## Sandbox layout

```
sandbox/
├── tasks.json                  the resolved task list and the pairs each planner is expected to run
├── cmds/<planner>.txt          one pypmtevalcli-solve command per line
├── slurm/pypmteval-<planner>.sbatch    job array, one index per line of that file
├── slurm/submit_all.sh
├── run_local.sh                the same commands through GNU parallel, no scheduler
├── results/<planner>/<task>.json
├── errors/                     tracebacks of crashed tasks
├── analysis/                   results.csv, summary.txt, summary.json
└── report/                     paper tables, LaTeX, plots/
```

## What a run records

Each result JSON carries the task and planner identity, the limits it ran
under, `parse`/`solve`/`total` seconds, peak memory, the plan and its length
(makespan for temporal plans), the `ProblemKind` features of the task, the
engine's log messages and statistics file, and a status:

| status | meaning |
|---|---|
| `SOLVED` | a plan came back and it validated |
| `UNSOLVABLE` | proven unsolvable |
| `EXHAUSTED` | the search hit its own bound (UP's `UNSOLVABLE_INCOMPLETELY`) |
| `TIMEOUT` / `MEMOUT` | the task's own limit fired |
| `UNSUPPORTED` | the engine does not support this `ProblemKind` |
| `ERROR` | it crashed; the traceback is in `errors/` |
| `KILLED` | the scheduler killed the job before it could report |
| `MISSING` | the pair never produced a result at all |

Some deliberate choices behind those:

* **Limits are enforced twice.** The runner arms its own alarm and address-space
  limit, and slurm gets those limits *plus* headroom. A task that runs out of
  time comes back as a `TIMEOUT` row rather than as a missing file and a line
  in the accounting log. Slurm stays the backstop for what the process cannot
  catch itself — a solve that dies inside a native solver, where no Python
  handler gets to run, or a platform whose `RLIMIT_AS` is advisory (macOS).
* **`UNSUPPORTED` is not a failure.** The engine's `ProblemKind` is checked
  before solving, so a domain the engine cannot express is recorded as out of
  scope instead of counted as a miss (or a crash).
* **Coverage counts validated plans only.** Plans are re-checked with whichever
  UP validator fits the problem and plan kind — engine-agnostic, and independent
  of whatever the engine checked internally. A returned plan that fails the
  validator is a bug, and `analyze` lists those separately rather than adding
  them to a coverage number. (`--no-validate`, or `"validate-plans": false`,
  turns it off; an unavailable validator is recorded as unknown, not as failure.)
* **`MISSING` is counted, and only where it means something.** `tasks.json`
  records the pairs each planner was asked to run, so a coverage percentage is
  never computed over a quietly smaller denominator — and a planner restricted
  to some tracks is not charged for the tracks it never ran.
* **Every run gets its own working directory.** The runner `chdir`s into
  `runs/<planner>__<task>.<pid>` before solving and removes it afterwards.
  Fast Downward's translator writes `output.sas` into the *working* directory,
  SMT backends drop `.smt2` files there, and on a cluster that directory would
  otherwise be shared by every task in the array. `--keep-run-dir` leaves it
  behind for inspection, and the path is recorded as `run.work-dir`.

## Running the sweep

```bash
bash sandbox/slurm/submit_all.sh              # one job array per planner
squeue -u $USER                               # watch it
bash sandbox/run_local.sh 8                   # or run it locally, 8 at a time
```

One job array per planner rather than one `sbatch` file per task: a full sweep
is tens of thousands of pairs, which is slow to submit one at a time and
unfriendly to schedulers with a submission-rate limit. `--per-task-scripts`
still emits the one-file-per-task form for sites that need it. Arrays longer
than `max-array-size` are split automatically, each chunk reading its own slice
of the command file.

## Collecting the results

```bash
pypmtevalcli analyze --sandbox-dir sandbox --per-domain
```

prints (and writes to `analysis/`) coverage per planner and track, a status
breakdown, runtime statistics over solved instances, and — when more than one
planner ran — a head-to-head restricted to the tasks *every* planner attempted,
with the count of instances only that planner solved.

## Paper-ready tables and figures

```bash
pip install ".[plots]"                      # matplotlib, for the figures only
pypmtevalcli report --sandbox-dir sandbox
```

Writes to `sandbox/report/`:

| file | what |
|---|---|
| `results.txt` | coverage, outcomes, IPC scores and per-domain coverage as text |
| `coverage.tex`, `per-domain-coverage.tex`, `outcomes.tex` | the same tables as booktabs LaTeX, ready to `\input` |
| `report.json` | every number above, for scripting |
| `plots/survival.*` | **survival (cactus) plot** — instances solved within a time budget, log x |
| `plots/survival-per-track.*` | the same, faceted by track |
| `plots/memory-survival.*` | instances solved within a memory budget |
| `plots/coverage-per-track.*` | grouped coverage bars, labelled with percentages |
| `plots/outcomes.*` | stacked outcome bars (solved / timeout / memout / error / …) |
| `plots/runtime-A-vs-B.*` | log-log runtime scatter per planner pair, colored by track |
| `plots/cost-A-vs-B.*` | the same for plan cost (length, or makespan for schedules) |

Both PDF (vector, `pdf.fonttype 42` so venues can edit the text) and PNG, via
`--formats`. `--no-plots` produces the tables alone, with no matplotlib needed.

**IPC scores.** `results.txt` reports the two standard ones, summed over
instances so the maximum is the instance count: *quality* is the satisficing
rule `best_cost / cost` (0 when unsolved), *time* is the agile rule
`1 / (1 + log10(t / t_best))`. Cost is the plan length, or the makespan for a
temporal plan.

**About the figures.** Colors come from a colorblind-validated categorical set,
every series also carries its own marker and dash pattern so nothing depends on
color alone (greyscale printing survives), outcome colors are reserved for
outcomes and never reused as a series color, and there are no dual axes. In the
runtime scatters, an instance one planner failed is pinned to the cutoff line
and drawn **hollow**, so a point on the border reads as "did not finish" rather
than "took exactly the limit".

## Resuming and iterating

`generate --skip-existing` drops the pairs that already have a result, so after
a partial sweep (or after adding a planner configuration) you regenerate and
submit again without redoing finished work:

```bash
pypmtevalcli generate --exp-dir experiment --sandbox-dir sandbox \
    --tasks-dir numeric-domains=benchmark-tasks/numeric-domains \
    --venv-dir venv --skip-existing
bash sandbox/slurm/submit_all.sh
```

## Upgrading from the older CLI

The stages are the same; the layout and the flag names changed. Old flags are
still accepted, so command files and scripts written against the previous
version keep working:

| old | new |
|---|---|
| `--exp-details-dir` | `--exp-dir` |
| `--planning-tasks-dir` | `--tasks-dir` (repeatable, `LABEL=PATH`) |
| `--planner-cfg-file` | `--planner-cfg` |
| `--domainname` / `--instanceno` / `--ipc-year` | `--domain-name` / `--instance` / `--ipc` |
| `--results-dump-dir` | `--results-dir` |
| `--dump-results-dir` (analyze) | `--sandbox-dir` (the results directory's parent) |

Results now live in `results/<planner>/<task>.json` instead of one flat
directory, and `analyze` reads the whole sandbox rather than a results
directory alone.
