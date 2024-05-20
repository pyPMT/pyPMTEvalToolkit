# PlannerEvalToolkit
This repo makes it easy for us to compare planners.

# How to use
1. Step 1: Describe your experiment parameter.
```
mkdir -p exp-1/planners
touch exp-1/exp-details.json
```
The `exp-details.json` defines the following parameters:
    - [ ] time limit
    - [ ] memory limit
Here is a demo `exp-details.json`
```
{
    "name": "demo",
    "cfgs": {
        "timelimit" : "00:30:00",
        "memorylimit": "10GB"
    }
}
```

2. Step 2: Describe which planners are used inside this experiment, this is done by creating json file inside `planners` dir.
This is an example showing using an experiment with two planners.
```
touch exp-1/planners/smt-seq-planner.json
touch exp-1/planners/smt-par-planner.json
```

Here is the content for `smt-seq-planner.json`:
```
{
    "planner-tag": "SequentialSMT-tag",
    "up-planner-name": "SMTPlanner",
    "planner-params": {
        "encoder": "EncoderSequentialSMT",
        "upper-bound": 1000,
        "search-strategy": "SMTSearch",
        "configuration": "seq",
        "run-validation": false
    }
}
```

Here is the content for `smt-par-planner.json`:
```
{
    "planner-tag": "ParallelSMT-tag",
    "up-planner-name": "SMTPlanner",
    "planner-params": {
        "encoder": "EncoderParallelSMT",
        "upper-bound": 1000,
        "search-strategy": "SMTSearch",
        "configuration": "seqForall",
        "run-validation": false
    }
}
```

3. Step 3: Generate the experiment commands, such that each command runs a planning task with a planner. This is acheived by
```
python3 exp-runner/main.py generate --exp-details-dir $(pwd)/exp-1 --sandbox-dir $(pwd)/sandbox-dir --planning-tasks-dir $(pwd)/pkgs/classical-domains
```
After running this command, it generates `generated_cmds.sh` such that each line is a command aims to solve one of the planning tasks found in `--planning-tasks-dir`

4. Step 4: Running the experiment commands, which can be done by using gnu-parallels or slurm.

Note that the results are stored in `sandbox-dir/dump_results` dir in json file format.
- A quick start use:
```
python3 exp-runner/main.py generate --exp-details-dir $(pwd)/exp-configurations/exp-demo --sandbox-dir $(pwd)/sandbox-dir --planning-tasks-dir $(pwd)/pkgs/classical-domains
```
