"""Benchmark harness for planners exposed through the Unified Planning API.

Any engine registered with UP can be benchmarked: the toolkit never imports a
planner by name. A planner configuration names a UP engine, and the engine is
resolved at run time from the plugins installed in the environment (or from a
module / class the configuration points at). See :mod:`pypmt_eval_toolkit.engines`.

Six stages, one per CLI subcommand:

``init``      write a starter experiment directory (limits + planner configs);
``engines``   list the UP engines this environment can actually run;
``discover``  walk a benchmark repository and classify every (domain, instance)
              pair into a track (classical / numeric / temporal);
``generate``  cross the discovered tasks with the planner configurations of an
              experiment and emit one run command per pair, plus the slurm job
              arrays that run them;
``solve``     run *one* (planner, task) pair under its own time/memory limits
              and dump a JSON result;
``analyze``   aggregate those JSONs into a CSV and a coverage table;
``report``    paper-ready tables (text + LaTeX) and figures.

Everything except ``solve``/``engines`` is import-light on purpose: they never
touch ``unified_planning``, so a sweep can be generated on a laptop and only the
compute nodes need the planners installed.
"""

__version__ = "0.2.0"
