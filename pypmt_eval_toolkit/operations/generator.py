import os

from .utilities import parse_planning_tasks, parse_experiment_details, construct_run_cmd, warpCommand, getkeyvalue, createVEnv

def generate(args):
    # create the experiment folders.
    generated_cmds_dir = os.path.join(args.sandbox_dir, 'generated_cmds')
    planners_run_dir = os.path.join(args.sandbox_dir, 'planners_run')
    dump_results_dir = os.path.join(args.sandbox_dir, 'dump_results')
    slurm_scripts_dir = os.path.join(generated_cmds_dir, 'slurm_scripts')
    for dir_ in  [args.sandbox_dir, planners_run_dir, dump_results_dir, generated_cmds_dir, slurm_scripts_dir]:
        os.makedirs(dir_, exist_ok=True)
    # Read the experiment details.
    expdetails = parse_experiment_details(args.exp_details_dir)
    # Parse the planning tasks dir.
    planning_tasks = parse_planning_tasks(args.planning_tasks_dir)
    # Filter planning tasks based on the experiment details.
    if len(expdetails['exp-details']['selected-tasks']) > 0:
        planning_tasks = list(filter(lambda p: (p['ipc_year'], p['domainname'], p['instanceno']) in set(map(tuple, expdetails['exp-details']['selected-tasks'])), planning_tasks))
    
    # Now for every planner configuration, generate the cmd for each planning task.
    expdetails_jsonfile = os.path.join(args.exp_details_dir, "exp-details.json")
    generated_cmds = set()
    for planning_task in planning_tasks:
        for plannername, plannercfg in expdetails['planners'].items():
            cmd = construct_run_cmd(expdetails, expdetails_jsonfile, plannercfg, planning_task, planners_run_dir, dump_results_dir)
            # get the script running script.
            main_entry = 'pypmtevalcli'
            if args.venv_dir:
                generated_cmds.add(f'source {args.venv_dir}/bin/activate && {main_entry} {cmd} && deactivate')
            elif args.apptainer_image:
                generated_cmds.add(f'apptainer run --cleanenv --bind {args.sandbox_dir}:/app/sandbox_dir {args.apptainer_image} {cmd}')
            else:
                generated_cmds.add(f'python {main_entry} {cmd}')
    # dump those commands to a file.
    with open(os.path.join(generated_cmds_dir, 'generated_cmds.sh'), 'w') as f:
        for cmd in generated_cmds:
            f.write(f'{cmd}\n')
    # No split the commands in to strum batch script files.
    for i, cmd in enumerate(generated_cmds):
        slurmcmd = warpCommand(cmd, getkeyvalue(expdetails, 'timelimit'), getkeyvalue(expdetails, 'memorylimit'), slurm_scripts_dir)
        with open(os.path.join(slurm_scripts_dir, f'slurm_batch_task_{i}.txt'), 'w') as f:
            f.write(slurmcmd)