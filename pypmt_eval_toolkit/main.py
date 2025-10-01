"""
Program entry point for experiments on the command line.
"""

import argparse
from importlib import metadata
import os
import sys
from copy import deepcopy
import cProfile

from pypmt_eval_toolkit.operations import (
    runner,
    generator,
    analyzer
)

def main():
    """
    Entry point function.
    """
    profilecode = True
    ARG_PARSER = _create_arg_parser()
    args = ARG_PARSER.parse_args()
    exitcode = args.func(args)
    return 0

def _create_arg_parser():
    cmd_list = "commands:\n"
    max_cmd_length = max(len(cmd) for cmd in _COMMANDS)
    col_width = max_cmd_length + 2
    for (cmd, cmd_attributes) in _COMMANDS.items():
        cmd_list += cmd.ljust(col_width) + cmd_attributes["desc"] + "\n"
    cmd_list += "Run `main <COMMAND> --help` for command-specific help."
 
    parser = argparse.ArgumentParser(
        prog="exp",
        description="Experiment runner for the behaviour space implementation.",
        epilog=cmd_list,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="command")
    subparser = {}
    for (cmd, cmd_attributes) in _COMMANDS.items():
        subparser[cmd] = subparsers.add_parser(cmd, description=cmd_attributes["desc"])
        subparser[cmd].set_defaults(func=cmd_attributes["fn"])

    for cmd in ["solve"]:
        subparser[cmd].add_argument( "--planner-cfg-file", type=str, help="Planner configuration file.")
        subparser[cmd].add_argument( "--exp-details-dir", type=str, help="Path to the directory containing all results.")

        subparser[cmd].add_argument( "--domain",      type=str, help="Path to a domain file.")
        subparser[cmd].add_argument( "--problem",     type=str, help="Path to a problem file.")
        
        subparser[cmd].add_argument( "--domainname",  type=str, help="Domain name ")
        subparser[cmd].add_argument( "--instanceno",  type=str, help="Instance number ")
        subparser[cmd].add_argument( "--ipc-year",    type=str, help="Path to the directory containing all results.")

        subparser[cmd].add_argument( "--run-dir")
        subparser[cmd].add_argument( "--results-dump-dir", type=str, help="Path to a directory to dump the results.")
    
    for cmd in ["generate"]:
        subparser[cmd].add_argument( "--venv-dir" , type=str, help="Path to the virtual environment directory.")
        subparser[cmd].add_argument( "--apptainer-image", type=str, help="Path to the Apptainer image.")
        subparser[cmd].add_argument( "--exp-details-dir", type=str, help="Path to the directory containing all results.")

        subparser[cmd].add_argument( "--sandbox-dir", type=str, help="Path to a sandbox directory to contain all processed files and generated plans.")
        subparser[cmd].add_argument( "--planning-tasks-dir", type=str, help="Path to the directory containing all planning tasks.")

    for cmd in ["analyze"]:
        subparser[cmd].add_argument( "--dump-results-dir", type=str, help="Path to the directory containing all results.")
        subparser[cmd].add_argument( "--error-logs-dir", type=str, help="Path to the directory containing all error logs.")

    return parser

_COMMANDS = {
    "solve": {
        "fn": runner.solve,
        "desc": "Solve the diverse planning task."
    },
    "generate": {
        "fn": generator.generate,
        "desc": "Generate the planning task cmds."
    },
    "analyze": {
        "fn": analyzer.analyze,
        "desc": "Analyze the results."
    },
}

if __name__ == "__main__":
    sys.exit(main())