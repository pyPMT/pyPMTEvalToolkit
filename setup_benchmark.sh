#!/usr/bin/env bash
#
# One-shot setup for a planner evaluation sweep:
#
#   1. ask for the per-task time and memory limits (and a few other knobs),
#   2. create a virtualenv and install this toolkit plus the engines the
#      experiment's planner configurations actually name,
#   3. fetch the benchmark repositories for the tracks you picked,
#   4. write the experiment configuration with those limits,
#   5. generate one run command per (planner, task) and the slurm job arrays.
#
# Everything is prompted with a default, and every prompt has a matching flag,
# so the same script drives an interactive setup and a scripted one (--yes).
# Re-running it is safe: an existing venv, clone or experiment is reused.
#
# Usage:
#   ./setup_benchmark.sh                      # interactive
#   ./setup_benchmark.sh --yes                # all defaults, no prompts
#   ./setup_benchmark.sh --time-limit 30m --memory-limit 8GB --tracks numeric --yes
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$SCRIPT_DIR"

# ---------------------------------------------------------------- defaults --
WORK_DIR="${REPO_DIR}/benchmark-run"
VENV_DIR=""
TASKS_DIR=""
SANDBOX_DIR=""
EXP_DIR=""
CONFIG_ROOT="${SCRIPT_DIR}/exp-configurations"
CONFIG=""                      # name under exp-configurations/, or a path
DEFAULT_CONFIG="default"
TIME_LIMIT="00:30:00"
MEMORY_LIMIT="8GB"
MAX_INSTANCES="0"
TRACKS="classical numeric temporal"
PARTITION=""
ACCOUNT=""
QOS=""
MAX_PARALLEL="50"
EXTRAS=""
SKIP_FETCH="no"
SKIP_INSTALL="no"
PER_TASK_SCRIPTS="no"
LIST_CONFIGS="no"
ASSUME_YES="no"
PYTHON_BIN="${PYTHON_BIN:-python3}"

CLASSICAL_REPO="https://github.com/AI-Planning/classical-domains.git"
NUMERIC_REPO="https://github.com/pyPMT/numeric-domains.git"
# The IPC archive is where the temporal benchmarks live: every `*-time*` and
# `*-temporal*` domain from IPC-2002 onwards, in one uniform layout. There is no
# equivalent of classical-domains/numeric-domains for the temporal track.
TEMPORAL_REPO="https://github.com/potassco/pddl-instances.git"

usage() {
    cat <<'EOF'
One-shot setup for a planner evaluation sweep.

Usage:
  ./setup_benchmark.sh                      # interactive; asks which configuration to run
  ./setup_benchmark.sh --yes                # all defaults, no prompts
  ./setup_benchmark.sh --config any-engine --time-limit 30m --tracks numeric --yes

Options:
  --config NAME|DIR       which exp-configurations/ entry to run; a bare name is
                          looked up under exp-configurations/, a path is used as
                          given. Asked interactively when omitted (default: default)
  --list-configs          print the available configurations and exit
  --work-dir DIR          root for venv/tasks/sandbox   (default: <repo>/benchmark-run)
  --venv-dir DIR          virtualenv location           (default: <work-dir>/venv)
  --tasks-dir DIR         where benchmarks are cloned   (default: <work-dir>/benchmark-tasks)
  --sandbox-dir DIR       commands, results, logs       (default: <work-dir>/sandbox/<config>)
  --exp-dir DIR           the experiment to run, seeded from the configuration
                          (default: <work-dir>/experiments/<config>)
  --time-limit VALUE      per task, e.g. 00:30:00 or 30m
  --memory-limit VALUE    per task, e.g. 8GB
  --max-instances N       instances per domain, 0 for all
  --tracks "a b c"        subset of: classical numeric temporal
  --partition NAME        slurm partition
  --account NAME          slurm account
  --qos NAME              slurm QOS
  --max-parallel N        cap on concurrently running array jobs
  --extras "a,b"          extras to install on top of the ones the planner
                          configurations imply (e.g. "plots,symk")
  --per-task-scripts      also emit one .sbatch per task (default: job arrays)
  --skip-fetch            do not clone/update the benchmark repositories
  --skip-install          do not create the venv or install anything
  -y, --yes               accept every default, never prompt
  -h, --help              this message
EOF
}

# ------------------------------------------------------------------- args ---
while [ $# -gt 0 ]; do
    case "$1" in
        --work-dir)         WORK_DIR="$2"; shift 2 ;;
        --venv-dir)         VENV_DIR="$2"; shift 2 ;;
        --tasks-dir)        TASKS_DIR="$2"; shift 2 ;;
        --sandbox-dir)      SANDBOX_DIR="$2"; shift 2 ;;
        --exp-dir)          EXP_DIR="$2"; shift 2 ;;
        --config)           CONFIG="$2"; shift 2 ;;
        --template-dir)     CONFIG="$2"; shift 2 ;;      # the older name for --config
        --list-configs)     LIST_CONFIGS="yes"; shift ;;
        --time-limit)       TIME_LIMIT="$2"; shift 2 ;;
        --memory-limit)     MEMORY_LIMIT="$2"; shift 2 ;;
        --max-instances)    MAX_INSTANCES="$2"; shift 2 ;;
        --tracks)           TRACKS="$2"; shift 2 ;;
        --partition)        PARTITION="$2"; shift 2 ;;
        --account)          ACCOUNT="$2"; shift 2 ;;
        --qos)              QOS="$2"; shift 2 ;;
        --max-parallel)     MAX_PARALLEL="$2"; shift 2 ;;
        --extras)           EXTRAS="$2"; shift 2 ;;
        --per-task-scripts) PER_TASK_SCRIPTS="yes"; shift ;;
        --skip-fetch)       SKIP_FETCH="yes"; shift ;;
        --skip-install)     SKIP_INSTALL="yes"; shift ;;
        -y|--yes)           ASSUME_YES="yes"; shift ;;
        -h|--help)          usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

VENV_DIR="${VENV_DIR:-${WORK_DIR}/venv}"
TASKS_DIR="${TASKS_DIR:-${WORK_DIR}/benchmark-tasks}"
# SANDBOX_DIR and EXP_DIR default per configuration, so they are resolved once
# the configuration is known (below).

# ---------------------------------------------------------------- helpers ---
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m error:\033[0m %s\n' "$*" >&2; exit 1; }

# ask <prompt> <default> <variable-name>
ask() {
    local prompt="$1" default="$2" varname="$3" answer=""
    if [ "$ASSUME_YES" = "yes" ] || [ ! -t 0 ]; then
        answer="$default"
    else
        read -r -p "$(printf '%s [%s]: ' "$prompt" "$default")" answer || answer=""
        answer="${answer:-$default}"
    fi
    printf -v "$varname" '%s' "$answer"
}

has_track() {
    case " $TRACKS " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# ------------------------------------------------------------ configurations --
# A configuration is a directory holding exp-details.json and planners/*.json --
# the same shape the toolkit runs. The ones shipped in exp-configurations/ are
# offered by name; any other directory works too, given as a path.

# Names of the shipped configurations, one per line.
config_names() {
    local dir
    for dir in "${CONFIG_ROOT}"/*/; do
        [ -f "${dir}exp-details.json" ] || continue
        basename "${dir%/}"
    done
}

# "3 planners: ENHSP, ParallelSMT, SequentialSMT" for a configuration directory.
config_summary() {
    local dir="$1" count names
    count=$(find "${dir}/planners" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l | tr -d ' ')
    names=$(ls "${dir}/planners" 2>/dev/null | sed 's/\.json$//' | paste -sd, - | sed 's/,/, /g')
    printf '%s planner(s)%s' "$count" "${names:+: $names}"
}

# A name resolves under exp-configurations/, anything with a slash is a path.
config_dir() {
    case "$1" in
        */*|.|..) printf '%s' "$1" ;;
        *)        printf '%s' "${CONFIG_ROOT}/$1" ;;
    esac
}

list_configs() {
    local name
    echo "Configurations in ${CONFIG_ROOT}:"
    for name in $(config_names); do
        printf '  %-14s %s\n' "$name" "$(config_summary "${CONFIG_ROOT}/${name}")"
    done
}

# Ask which configuration to run and set CONFIG. Accepts its number, its name,
# or a path. Sets the global rather than printing it: the menu goes to stdout,
# so a $(...) capture would swallow it.
choose_config() {
    # `reply`, not `answer`: `ask` has a local of that name, and dynamic scoping
    # would have it assign to its own copy instead of this one.
    local names name count reply index=1
    names="$(config_names)"
    count=$(printf '%s\n' "$names" | sed '/^$/d' | wc -l | tr -d ' ')
    [ "$count" -gt 0 ] || die "no configuration in ${CONFIG_ROOT}"

    echo "Available experiment configurations:"
    for name in $names; do
        printf '  %d) %-14s %s\n' "$index" "$name" "$(config_summary "${CONFIG_ROOT}/${name}")"
        index=$((index + 1))
    done
    echo "  (or the path to a configuration directory of your own)"
    echo

    ask "Experiment configuration" "$DEFAULT_CONFIG" reply
    # A number picks from the list; anything else is a name or a path.
    case "$reply" in
        ''|*[!0-9]*) CONFIG="$reply" ;;
        *)           CONFIG="$(printf '%s\n' "$names" | sed -n "${reply}p")"
                     [ -n "$CONFIG" ] || die "no configuration number ${reply}" ;;
    esac
}

clone_or_update() {
    local url="$1" dest="$2"
    if [ -d "${dest}/.git" ]; then
        say "updating $(basename "$dest")"
        git -C "$dest" fetch --depth 1 origin HEAD --quiet || warn "could not update ${dest}"
        git -C "$dest" reset --hard FETCH_HEAD --quiet || warn "could not fast-forward ${dest}"
    elif [ -d "$dest" ]; then
        say "reusing $(basename "$dest") (not a git checkout)"
    else
        say "cloning $(basename "$dest")"
        git clone --depth 1 --quiet "$url" "$dest"
    fi
}

if [ "$LIST_CONFIGS" = "yes" ]; then
    list_configs
    exit 0
fi

# --------------------------------------------------------------- prompting --
if [ "$ASSUME_YES" != "yes" ] && [ -t 0 ]; then
    echo
    echo "pyPMT evaluation setup -- press enter to accept a default."
    echo
fi

# The configuration comes first: it decides which planners run, and therefore
# which engines have to be installed. Asked only when --config was not given;
# --yes and a non-interactive shell take the default.
if [ -z "$CONFIG" ]; then
    if [ "$ASSUME_YES" = "yes" ] || [ ! -t 0 ]; then
        CONFIG="$DEFAULT_CONFIG"
    else
        choose_config
    fi
fi

TEMPLATE_DIR="$(config_dir "$CONFIG")"
CONFIG_NAME="$(basename "${TEMPLATE_DIR%/}")"
[ -d "$TEMPLATE_DIR" ] || die "no such configuration: ${CONFIG} (tried ${TEMPLATE_DIR}); \
run with --list-configs to see the shipped ones"
[ -f "${TEMPLATE_DIR}/exp-details.json" ] || \
    die "${TEMPLATE_DIR} holds no exp-details.json, so it is not a configuration"

# One experiment and one sandbox per configuration: seeding a second
# configuration into a directory that already holds another would union their
# planner sets and mix their results.
SANDBOX_DIR="${SANDBOX_DIR:-${WORK_DIR}/sandbox/${CONFIG_NAME}}"
EXP_DIR="${EXP_DIR:-${WORK_DIR}/experiments/${CONFIG_NAME}}"

ask "Per-task time limit (HH:MM:SS or 30m)"      "$TIME_LIMIT"    TIME_LIMIT
ask "Per-task memory limit (e.g. 8GB)"           "$MEMORY_LIMIT"  MEMORY_LIMIT
ask "Tracks to benchmark"                        "$TRACKS"        TRACKS
ask "Instances per domain (0 = all)"             "$MAX_INSTANCES" MAX_INSTANCES
ask "Slurm partition (blank = site default)"     "$PARTITION"     PARTITION
ask "Slurm account (blank = site default)"       "$ACCOUNT"       ACCOUNT
ask "Slurm QOS (blank = site default)"           "$QOS"           QOS
ask "Max array jobs running at once"             "$MAX_PARALLEL"  MAX_PARALLEL

for track in $TRACKS; do
    case "$track" in
        classical|numeric|temporal) ;;
        *) die "unknown track '${track}' (expected: classical, numeric, temporal)" ;;
    esac
done

echo
say "config       ${CONFIG_NAME} (${TEMPLATE_DIR})"
say "             $(config_summary "$TEMPLATE_DIR")"
say "work dir     ${WORK_DIR}"
say "venv         ${VENV_DIR}"
say "tasks        ${TASKS_DIR}"
say "sandbox      ${SANDBOX_DIR}"
say "experiment   ${EXP_DIR}"
say "limits       ${TIME_LIMIT} / ${MEMORY_LIMIT} per task"
say "tracks       ${TRACKS}"
echo

mkdir -p "$WORK_DIR" "$TASKS_DIR"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || die "python interpreter not found: ${PYTHON_BIN}"

# ------------------------------------------------------------- experiment --
# Set up before installing: the planner configurations decide which engines have
# to be installed, and every one of them is benchmarked.
mkdir -p "${EXP_DIR}/planners"
if [ ! -f "${EXP_DIR}/exp-details.json" ]; then
    say "creating experiment at ${EXP_DIR} from configuration ${CONFIG_NAME}"
    cp "${TEMPLATE_DIR}/exp-details.json" "${EXP_DIR}/"
else
    say "reusing experiment at ${EXP_DIR}"
    # Which configuration an experiment came from, so that pointing a second one
    # at it -- which would add that one's planners to this one's -- is noticed.
    if [ -f "${EXP_DIR}/.configuration" ] && \
       [ "$(cat "${EXP_DIR}/.configuration")" != "$CONFIG_NAME" ]; then
        warn "${EXP_DIR} was created from configuration '$(cat "${EXP_DIR}/.configuration")';"
        warn "seeding '${CONFIG_NAME}' into it adds its planners to the ones already there."
    fi
fi
printf '%s\n' "$CONFIG_NAME" > "${EXP_DIR}/.configuration"

# Copy in any template configuration the experiment does not have yet, without
# touching the ones it does: a config added to the template after the experiment
# was first created would otherwise never reach it, while local edits survive.
for template in "${TEMPLATE_DIR}"/planners/*.json; do
    [ -e "$template" ] || continue
    name="$(basename "$template")"
    if [ ! -f "${EXP_DIR}/planners/${name}" ]; then
        cp "$template" "${EXP_DIR}/planners/"
        say "added planner configuration ${name}"
    fi
done

PLANNER_COUNT=$(find "${EXP_DIR}/planners" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')
if [ "$PLANNER_COUNT" -eq 0 ]; then
    die "no planner configuration in ${EXP_DIR}/planners"
fi
say "planners (${PLANNER_COUNT}): $(ls "${EXP_DIR}/planners" | tr '\n' ' ')"

# --------------------------------------------------------------- install ---
# Which extras to install is read off the planner configurations: an engine that
# a configuration names should be installed, and one nobody asks for should not
# drag its (large, native) dependencies into the venv.
# Redirected to a file rather than captured with $(...): a heredoc inside a
# command substitution is mis-parsed by the bash 3.2 macOS still ships.
EXTRAS_FILE="$(mktemp "${TMPDIR:-/tmp}/pypmteval-extras.XXXXXX")"
trap 'rm -f "$EXTRAS_FILE"' EXIT

"$PYTHON_BIN" - "${EXP_DIR}/planners" > "$EXTRAS_FILE" <<'PY'
import json, os, sys

# engine-name prefix -> extra of this package pyproject
EXTRA_FOR = {
    'smtplanner': 'pypmt', 'pypmt': 'pypmt',
    'enhsp': 'enhsp',
    'fast-downward': 'fast-downward', 'fast_downward': 'fast-downward',
    'symk': 'symk',
    'pyperplan': 'pyperplan',
    'patty': 'patty',
    'plasp': 'asp', 'aba': 'asp',
}

extras, unknown = set(), set()
directory = sys.argv[1]
for name in sorted(os.listdir(directory)):
    if not name.endswith('.json'):
        continue
    with open(os.path.join(directory, name)) as handle:
        cfg = json.load(handle)
    engine = str(cfg.get('up-planner-name') or cfg.get('engine') or '').lower()
    # An engine the configuration brings itself needs no extra from us.
    if cfg.get('up-planner-class') or cfg.get('up-planner-module'):
        continue
    match = next((extra for prefix, extra in EXTRA_FOR.items() if engine.startswith(prefix)), None)
    if match:
        extras.add(match)
    elif engine:
        unknown.add(engine)

if unknown:
    print('note: no extra known for engine(s): ' + ', '.join(sorted(unknown)) +
          ' -- install them yourself if they are not already available',
          file=sys.stderr)
print(','.join(sorted(extras)))
PY

IMPLIED_EXTRAS="$(cat "$EXTRAS_FILE")"
ALL_EXTRAS="$(printf '%s,%s' "$IMPLIED_EXTRAS" "$EXTRAS" | tr ',' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"

if [ "$SKIP_INSTALL" = "yes" ]; then
    say "skipping installation (--skip-install)"
    [ -x "${VENV_DIR}/bin/pypmtevalcli" ] || warn "no pypmtevalcli in ${VENV_DIR}; generation will fail"
else
    if [ ! -d "$VENV_DIR" ]; then
        say "creating virtualenv at ${VENV_DIR}"
        "$PYTHON_BIN" -m venv "$VENV_DIR"
    else
        say "reusing virtualenv at ${VENV_DIR}"
    fi
    # shellcheck disable=SC1091
    . "${VENV_DIR}/bin/activate"
    python -m pip install --quiet --upgrade pip setuptools wheel
    if [ -n "$ALL_EXTRAS" ]; then
        say "installing the toolkit with extras: ${ALL_EXTRAS}"
        python -m pip install --quiet -e "${REPO_DIR}[${ALL_EXTRAS}]"
    else
        say "installing the toolkit"
        python -m pip install --quiet -e "${REPO_DIR}"
    fi
    deactivate
fi

CLI="${VENV_DIR}/bin/pypmtevalcli"
VENV_PYTHON="${VENV_DIR}/bin/python"
[ -x "$CLI" ] || die "pypmtevalcli was not installed into ${VENV_DIR}"

# Fail here, with a list of what is available, rather than on ten thousand
# compute nodes each discovering the same missing engine.
say "checking that every configured engine is available"
"$CLI" engines --exp-dir "$EXP_DIR" || warn "some planner configurations name an engine this venv cannot run"

# ------------------------------------------------------------- benchmarks --
TASKS_ARGS=()
SUITE_TRACK_ARGS=()
if [ "$SKIP_FETCH" != "yes" ]; then
    if has_track classical; then clone_or_update "$CLASSICAL_REPO" "${TASKS_DIR}/classical-domains"; fi
    if has_track numeric;   then clone_or_update "$NUMERIC_REPO"   "${TASKS_DIR}/numeric-domains"; fi
    if has_track temporal;  then clone_or_update "$TEMPORAL_REPO"  "${TASKS_DIR}/pddl-instances"; fi
fi

if has_track classical && [ -d "${TASKS_DIR}/classical-domains" ]; then
    TASKS_ARGS+=(--tasks-dir "classical-domains=${TASKS_DIR}/classical-domains")
fi
if has_track numeric && [ -d "${TASKS_DIR}/numeric-domains" ]; then
    TASKS_ARGS+=(--tasks-dir "numeric-domains=${TASKS_DIR}/numeric-domains")
fi
if has_track temporal && [ -d "${TASKS_DIR}/pddl-instances" ]; then
    TASKS_ARGS+=(--tasks-dir "pddl-instances=${TASKS_DIR}/pddl-instances")
    # The IPC archive holds all three tracks; take only its temporal domains so
    # its classical and numeric ones do not duplicate the two repos above.
    SUITE_TRACK_ARGS+=(--suite-tracks "pddl-instances=temporal")
fi
if [ ${#TASKS_ARGS[@]} -eq 0 ]; then
    die "no benchmark repository available for tracks: ${TRACKS}"
fi

# ------------------------------------------------------- experiment limits --
say "writing the limits into ${EXP_DIR}/exp-details.json"
PYPMTEVAL_TIME="$TIME_LIMIT" PYPMTEVAL_MEM="$MEMORY_LIMIT" PYPMTEVAL_TRACKS="$TRACKS" \
PYPMTEVAL_MAXINST="$MAX_INSTANCES" PYPMTEVAL_PARTITION="$PARTITION" \
PYPMTEVAL_ACCOUNT="$ACCOUNT" PYPMTEVAL_QOS="$QOS" PYPMTEVAL_PARALLEL="$MAX_PARALLEL" \
"$VENV_PYTHON" - "$EXP_DIR" <<'PY'
import json, os, sys

exp_dir = sys.argv[1]
path = os.path.join(exp_dir, 'exp-details.json')
with open(path) as handle:
    details = json.load(handle)

cfgs = details.setdefault('cfgs', {})
cfgs['timelimit'] = os.environ['PYPMTEVAL_TIME']
cfgs['memorylimit'] = os.environ['PYPMTEVAL_MEM']
slurm = cfgs.setdefault('slurm', {})
slurm['partition'] = os.environ['PYPMTEVAL_PARTITION'] or None
slurm['account'] = os.environ['PYPMTEVAL_ACCOUNT'] or None
slurm['qos'] = os.environ['PYPMTEVAL_QOS'] or None
slurm['max-parallel-jobs'] = int(os.environ['PYPMTEVAL_PARALLEL'] or 0)

tasks = details.setdefault('tasks', {})
tasks['tracks'] = os.environ['PYPMTEVAL_TRACKS'].split()
tasks['max-instances-per-domain'] = int(os.environ['PYPMTEVAL_MAXINST'] or 0)
details['name'] = os.path.basename(os.path.abspath(exp_dir))

with open(path, 'w') as handle:
    json.dump(details, handle, indent=4)
    handle.write('\n')
print(f'  time={cfgs["timelimit"]} memory={cfgs["memorylimit"]} '
      f'tracks={tasks["tracks"]} instances/domain={tasks["max-instances-per-domain"]}')
PY

# --------------------------------------------------------------- generate --
GENERATE_ARGS=(
    generate
    --exp-dir "$EXP_DIR"
    --sandbox-dir "$SANDBOX_DIR"
    --venv-dir "$VENV_DIR"
    "${TASKS_ARGS[@]}"
)
if [ ${#SUITE_TRACK_ARGS[@]} -gt 0 ]; then
    GENERATE_ARGS+=("${SUITE_TRACK_ARGS[@]}")
fi
if [ "$PER_TASK_SCRIPTS" = "yes" ]; then
    GENERATE_ARGS+=(--per-task-scripts)
fi

echo
say "generating run commands"
"$CLI" "${GENERATE_ARGS[@]}"

cat <<EOF

Next steps
  submit the sweep      bash ${SANDBOX_DIR}/slurm/submit_all.sh
  or run it locally     bash ${SANDBOX_DIR}/run_local.sh 8
  watch it              squeue -u \$USER
  collect the results   ${CLI} analyze --sandbox-dir ${SANDBOX_DIR} --per-domain
  paper tables/figures  ${CLI} report  --sandbox-dir ${SANDBOX_DIR}

  re-generate after changing the experiment (skips finished tasks):
    ${CLI} generate --exp-dir ${EXP_DIR} --sandbox-dir ${SANDBOX_DIR} \\
        --venv-dir ${VENV_DIR} ${TASKS_ARGS[*]} --skip-existing
EOF
