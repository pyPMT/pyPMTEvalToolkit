from collections import defaultdict
from copy import deepcopy
import os
import json
import importlib.util
import subprocess

def parse_experiment_details(expdetailsdir:str):
    """
    Parse the experiment details json file.
    """
    expdetails = defaultdict(dict)
    expdetailsjson = os.path.join(expdetailsdir, "exp-details.json")
    # First read the experiment details file.
    assert os.path.exists(expdetailsjson), f"Experiment details file not found: {expdetailsjson}"
    with open(expdetailsjson, "r") as f:
        expdetails['exp-details'] = json.load(f)
    # Now read the planners configurations from the planners dir under the expdetailsdir.
    plannersdir = os.path.join(expdetailsdir, "planners")
    assert os.path.exists(plannersdir), f"Planners directory not found: {plannersdir}"
    for planner in os.listdir(plannersdir):
        plannerjson = os.path.join(plannersdir, planner)
        assert os.path.exists(plannerjson), f"Planner json file not found: {plannerjson}"
        expdetails['planners'][planner.replace('.json', '')] = plannerjson
    return expdetails

def parse_planning_tasks(planningtasksdir:str):
    # First collect all the planning tasks from the planning tasks directory.
    planning_domains = _get_planning_domains(planningtasksdir)

    planning_problems = []
    covered_domains = set()

    for domain in planning_domains:
        for domainsroot, _dir, _files in os.walk(domain):
            domainapi = os.path.join(domainsroot, 'api.py')
            _domainbasename = os.path.basename(domainsroot)
            # This is not a valid domain directory.
            if not os.path.exists(domainapi): continue
            # Ignore already processed domains.
            if _domainbasename in covered_domains: continue
            # Process only strips domains.
            if 'adl' in _domainbasename: continue
            _modulename = f'{os.path.basename(domainsroot)}_module'
            module_spec = importlib.util.spec_from_file_location(_modulename, domainapi)
            domain_module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(domain_module)
            domainsproblems = deepcopy(domain_module.domains)
            _domainname = domainsproblems[0]['name']
            _ipc_year   = domainsproblems[0]['ipc']
            for no, problem in enumerate(domainsproblems[0]['problems']):
                _instanceno = no+1
                planning_problem                = defaultdict(dict)
                planning_problem['domainname']  = _domainname
                planning_problem['instanceno']  = _instanceno
                planning_problem['ipc_year']    = _ipc_year
                planning_problem['domainfile']  = os.path.join(os.path.dirname(domainsroot), problem[0])
                planning_problem['problemfile'] = os.path.join(os.path.dirname(domainsroot), problem[1])

                # Ignore if the domain or problem file does not exist.
                if not (os.path.exists(planning_problem['domainfile']) and os.path.exists(planning_problem['problemfile'])): continue
                planning_problems.append(planning_problem)
                covered_domains.add(os.path.basename(domainsroot))
    return sorted(planning_problems, key=lambda e:e['domainname'])

def _get_planning_domains(directory_path):
    planning_domains = []
    for root, dirs, files in os.walk(directory_path):
        for dir_name in dirs:
            if not os.path.exists(os.path.join(root, dir_name, 'api.py')): continue
            planning_domains.append(os.path.join(root, dir_name))
    return planning_domains

def construct_run_cmd(expdetails, expdetailsfile, plannercfg, planningtask, rundir, dump_results_dir):
    cmd = "solve "
    cmd += f"--domainname {planningtask['domainname']} "
    cmd += f"--instanceno {planningtask['instanceno']} "
    cmd += f"--ipc-year {planningtask['ipc_year']} "
    cmd += f"--planner-cfg-file {plannercfg} "
    cmd += f"--exp-details-dir {expdetailsfile} "
    cmd += f"--run-dir {rundir} "
    cmd += f"--domain {planningtask['domainfile']} "
    cmd += f"--problem {planningtask['problemfile']} "
    cmd += f"--results-dump-dir {dump_results_dir} "
    return cmd

def getkeyvalue(data, target_key):
    if isinstance(data, dict):
        if target_key in data:
            return data[target_key]
        for value in data.values():
            result = getkeyvalue(value, target_key)
            if result is not None:
                return result
    elif isinstance(data, list):
        for item in data:
            result = getkeyvalue(item, target_key)
            if result is not None:
                return result
    return None

def warpCommand(cmd, timelimt, memorylimit, slurmdumpdir):
    args = cmd.split(' ')
    domainname = args[args.index('--domainname') + 1]
    instanceno = args[args.index('--instanceno') + 1]
    plannercfg = os.path.basename(args[args.index('--planner-cfg-file') + 1]).replace('.json', '')
    taskname = f'{plannercfg}_{domainname}_{instanceno}'
    return f"""#!/bin/bash
#SBATCH --job-name={taskname}
#SBATCH -e {slurmdumpdir}/{taskname}.error
#SBATCH -o {slurmdumpdir}/{taskname}.output
#SBATCH --cpus-per-task=1
#SBATCH --mem={memorylimit}
#SBATCH --time={timelimt}

{cmd}
"""

def createVEnv(basedir, requirements_file):
    venv_dir = os.path.join(basedir, 'venv')
    os.makedirs(venv_dir, exist_ok=True)
    ## start a venv and install the required packages.
    os.system(f'python3 -m venv {venv_dir}')
    os.system(f'{venv_dir}/bin/python3 -m pip install -r {requirements_file}')
    return venv_dir
