import os
import tempfile
import json
import time
from collections import defaultdict

from .utilities import getkeyvalue

def solve(args):

    try:
        import unified_planning.shortcuts as up_shortcuts
        import unified_planning.engines.results as up_results
        import unified_planning.io as up_io
        import unified_planning.plans as up_plans
        # Register the planners.
        import up_pypmt 
        import up_enhsp
        import up_fast_downward
        import up_pyperplan
        import up_symk
        
        with tempfile.TemporaryDirectory(dir=args.run_dir) as tmpdirname:
            planner_cfg       = json.load(open(args.planner_cfg_file))
            planner_tag       = getkeyvalue(planner_cfg, 'planner-tag')
            up_planner_name   = getkeyvalue(planner_cfg, 'up-planner-name')
            up_planner_params = getkeyvalue(planner_cfg, 'planner-params')
            
            assert up_planner_name is not None, "up-planner-name is not defined in the planner configuration file."
            assert up_planner_params is not None, "planner-param is not defined in the planner configuration file."
            assert planner_tag is not None, "planner-tag is not defined in the planner configuration file."

            start_time = time.time()
            task = up_io.PDDLReader().parse_problem(args.domain, args.problem)
            end_time = time.time()
            pddl_parse_time = end_time - start_time
            
            start_time = time.time()
            with up_shortcuts.OneshotPlanner(name=up_planner_name,  params=up_planner_params) as planner:
                result = planner.solve(task)
                seedplan = result.plan if result.status in up_results.POSITIVE_OUTCOMES else up_plans.SequentialPlan([], task.environment)
            end_time = time.time()
            planning_time = end_time - start_time

            # Now we need to construct the task result.
            dumpresult = defaultdict(dict)
            dumpresult['task-info'] = defaultdict(dict)
            dumpresult['task-info']['domain'] = args.domainname
            dumpresult['task-info']['instance'] = args.instanceno
            dumpresult['task-info']['ipc-year'] = args.ipc_year

            dumpresult['planner-info'] = defaultdict(dict)  
            dumpresult['planner-info']['planner-tag'] = planner_tag
            dumpresult['planner-info']['planner-name'] = up_planner_name
            dumpresult['planner-info']['planner-params'] = up_planner_params

            dumpresult['task-result'] = defaultdict(dict)
            dumpresult['task-result']['timings'] = defaultdict(dict)
            dumpresult['task-result']['timings']['pddl-parse-time'] = pddl_parse_time
            dumpresult['task-result']['timings']['planning-time']   = planning_time

            dumpresult['task-result']['summary'] = defaultdict(dict)
            dumpresult['task-result']['summary']['status'] = result.status.name
            dumpresult['task-result']['summary']['log_messages'] = [] if result.log_messages is None else result.log_messages

            dumpresult['task-result']['plan'] = up_io.PDDLWriter(task).get_plan(seedplan).split('\n')[:-1]

            dumpresult['debug-info'] = defaultdict(dict)
            dumpresult['debug-info']['domain-file']  = args.domain
            dumpresult['debug-info']['problem-file'] = args.problem
            
            # Dump this to the output directory.
            dumpfile = os.path.join(args.results_dump_dir, f"{planner_tag}-{args.domainname}-{args.instanceno}-{args.ipc_year}.json")
            # make sure that the directory exists
            os.makedirs(os.path.dirname(dumpfile), exist_ok=True)
            with open(dumpfile, 'w') as dumpfilehandle:
                json.dump(dumpresult, dumpfilehandle, indent=4)
    except Exception as e:
        error_dir = os.path.join(args.results_dump_dir, '..', 'errors')
        os.makedirs(error_dir, exist_ok=True)
        error_file = os.path.join(error_dir, f"{planner_tag}-{args.domainname}-{args.instanceno}-{args.ipc_year}.error")
        with open(error_file, 'w') as f:
            f.write(str(e))