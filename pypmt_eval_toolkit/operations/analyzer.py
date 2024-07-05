
import os
import json
from collections import defaultdict

from .utilities import getkeyvalue

def analyze(args):
    assert args.dump_results_dir is not None, "--dump-results-dir is required"
    assert args.error_logs_dir is not None,   "--error-logs-dir is required"

    assert os.path.exists(args.dump_results_dir), f"Path {args.dump_results_dir} does not exist"
    assert os.path.exists(args.error_logs_dir),   f"Path {args.error_logs_dir} does not exist"

    run_summary = analyze_run(args.dump_results_dir)
    error_summary = analyze_errors(args.error_logs_dir)

    dump_dir = os.path.join(os.path.dirname(args.dump_results_dir), "analysis")
    os.makedirs(dump_dir, exist_ok=True)
    with open(os.path.join(dump_dir, "run_summary.json"), "w") as f:
        json.dump(run_summary, f, indent=4)
    with open(os.path.join(dump_dir, "error_summary.json"), "w") as f:
        json.dump(error_summary, f, indent=4)

def analyze_run(results_dir):
    # we want to have per domain per instance the planners results.
    planners_details = defaultdict(list)
    for file in os.listdir(results_dir):
        if file.endswith(".json"):
            results_file = os.path.join(results_dir, file)
            with open(results_file, "r") as f:
                data = json.load(f)

            domain   = getkeyvalue(data, "domain")
            instance = getkeyvalue(data, "instance")
            year     = getkeyvalue(data, "year")
            year     = 'None' if year is None else year
            planner_tag = getkeyvalue(data, "planner-tag")

            planning_time = getkeyvalue(data, "planning-time")
            status = getkeyvalue(data, "status")
            log_messages = getkeyvalue(data, "log-messages")
            log_messages = [] if log_messages is None else log_messages

            assert domain is not None,      f"Domain not found in {results_file}"
            assert instance is not None,    f"Instance not found in {results_file}"
            assert planner_tag is not None, f"Planner tag not found in {results_file}"
            assert status is not None,      f"Status not found in {results_file}"

            planners_details[planner_tag].append({'domain': domain, 'instance': instance, 'year': year, 'status': status, 'planning-time': planning_time, 'reason': ''.join(log_messages)})
    
    planners_summary = defaultdict(dict)
    for planner_tag, details in planners_details.items():

        instances_count = len(set(map(lambda l: f'{l["year"]}-{l["domain"]}-{l["instance"]}', details)))
        unsolvable_planning_time = list(map(lambda l: l['planning-time'], (filter(lambda x: x['status'] == 'UNSOLVABLE_INCOMPLETELY', details))))
        solvable_planning_time   = list(map(lambda l: l['planning-time'], (filter(lambda x: x['status'] == 'SOLVED_SATISFICING', details)))) 

        planners_summary[planner_tag] = {
            'instances-count': instances_count,
            'unsolvable': len(unsolvable_planning_time),
            'solvable': len(solvable_planning_time),
            'total-unsolvable-planning-time':   round(sum(unsolvable_planning_time), 3),
            'total-solvable-planning-time':     round(sum(solvable_planning_time), 3),
            'average-unsolvable-planning-time': round(sum(unsolvable_planning_time) / len(unsolvable_planning_time), 3) if len(unsolvable_planning_time) > 0 else -1,
            'average-solvable-planning-time':   round(sum(solvable_planning_time)   / len(solvable_planning_time), 3)   if len(solvable_planning_time) > 0 else -1
        }
    
    # do we need the common coverage instances?!
    return planners_summary

def analyze_errors(error_logs_dir):
    errors_summary = defaultdict(list)
    for file in os.listdir(error_logs_dir):
        if file.endswith(".error"):
            error_file = os.path.join(error_logs_dir, file)
            with open(error_file, "r") as f:
                data = f.readlines()
            
            for l in data:
                errors_summary[l].append(file)

    return errors_summary