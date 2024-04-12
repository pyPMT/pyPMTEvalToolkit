import os

from .utilities import parse_planning_tasks, parse_experiment_details, construct_run_cmd, warpCommand, getkeyvalue, createVEnv

def generate(args):
    # Read the experiment details.
    expdetails = parse_experiment_details(args.exp_details_dir)
    # Parse the planning tasks dir.
    planning_tasks = parse_planning_tasks(args.planning_tasks_dir)
    # Create a venv for to install the required packages.
    venv_dir = createVEnv(args.sandbox_dir, os.path.join(os.path.dirname(__file__), 'requirements.txt'))
    
    # Now for every planner configuration, generate the cmd for each planning task.
    planners_run_dir = os.path.join(args.sandbox_dir, 'planners_run')
    expdetails_jsonfile = os.path.join(args.exp_details_dir, "exp-details.json")
    generated_cmds = set()
    for planning_task in planning_tasks:
        for plannername, plannercfg in expdetails['planners'].items():
            cmd = construct_run_cmd(expdetails, expdetails_jsonfile, plannercfg, planning_task, planners_run_dir)
            # get the script running script.
            main_entry = os.path.join(os.path.dirname(__file__), '..', 'main.py')
            generated_cmds.add(f'source {venv_dir}/bin/activate && {main_entry} {cmd} && deactivate')

    # dump those commands to a file.
    os.makedirs(args.sandbox_dir, exist_ok=True)
    generated_cmds_dir = os.path.join(args.sandbox_dir, 'generated_cmds')
    os.makedirs(generated_cmds_dir, exist_ok=True)
    with open(os.path.join(generated_cmds_dir, 'generated_cmds.sh'), 'w') as f:
        for cmd in generated_cmds:
            f.write(f'{cmd}\n')
    # No split the commands in to strum batch script files.
    slurm_scripts_dir = os.path.join(generated_cmds_dir, 'slurm_scripts')
    os.makedirs(slurm_scripts_dir, exist_ok=True)
    for i, cmd in enumerate(generated_cmds):
        slurmcmd = warpCommand(cmd, getkeyvalue(expdetails, 'timelimit'), getkeyvalue(expdetails, 'memorylimit'), slurm_scripts_dir)
        with open(os.path.join(slurm_scripts_dir, f'slurm_batch_task_{i}.txt'), 'w') as f:
            f.write(slurmcmd)