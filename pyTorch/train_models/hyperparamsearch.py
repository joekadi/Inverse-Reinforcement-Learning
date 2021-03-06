import os
import sys

from clearml import Task
from clearml.automation import UniformParameterRange, UniformIntegerParameterRange
from clearml.automation import HyperParameterOptimizer
from clearml.automation.optimization import RandomSearch
from clearml.automation.optuna import OptimizerOptuna
TEMPLATE_TASK_ID = str(sys.argv[1])
task = Task.init(project_name='MSci-Project',
                task_name='Hyperparameter Search',
                task_type=Task.TaskTypes.optimizer)
optimizer = HyperParameterOptimizer(
    base_task_id=TEMPLATE_TASK_ID,  # This is the experiment we want to optimize
    # here we define the hyper-parameters to optimize
    hyper_parameters=[
        UniformIntegerParameterRange('number_of_epochs', min_value=1, max_value=4, step_size=1),
        UniformParameterRange('base_lr', min_value=0.005, max_value=0.1, step_size=0.05),
        UniformParameterRange('p', min_value=0.01, max_value=0.7, step_size=0.05),
        UniformIntegerParameterRange('no_hidden_layers', min_value=1, max_value=8, step_size=1),
        UniformIntegerParameterRange('no_neurons_in_hidden_layers', min_value=16, max_value=64, step_size=2),
    ],

    # setting the objective metric we want to maximize/minimize
    objective_metric_title='loss',
    objective_metric_series='loss',
    objective_metric_sign='min',  # maximize or minimize the objective metric

    # setting optimizer - clearml supports GridSearch, RandomSearch, OptimizerBOHB and OptimizerOptuna
    optimizer_class=RandomSearch,
    
    # Configuring optimization parameters
    execution_queue='default',  # queue to schedule the experiments for execution
    max_number_of_concurrent_tasks=4,  # number of concurrent experiments
    optimization_time_limit=60.,  # set the time limit for the optimization process
    compute_time_limit=60,  # set the compute time limit (sum of execution time on all machines)
    total_max_jobs=100,  # set the maximum number of experiments for the optimization. 
                        # Converted to total number of iteration for OptimizerBOHB
    min_iteration_per_job=15000,  # minimum number of iterations per experiment, till early stopping
    max_iteration_per_job=150000,  # maximum number of iterations per experiment
)
optimizer.set_report_period(1)  # setting the time gap between two consecutive reports
optimizer.start()  
optimizer.wait()  # wait until process is done
optimizer.stop()  # make sure background optimization stopped


# optimization is completed, print the top performing experiments id
k = 3
top_exp = optimizer.get_top_experiments(top_k=k)
print('Top {} experiments are:'.format(k))
for n, t in enumerate(top_exp, 1):
    print('Rank {}: task id={} |result={}'.format(n, t.id, t.get_last_scalar_metrics()['loss']))