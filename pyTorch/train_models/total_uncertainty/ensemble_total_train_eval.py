import torch 
import torch.nn as nn
from torch.nn.functional import softplus
import torch.nn.functional as F
import matplotlib.pyplot as plt
import seaborn as sns
from torch.autograd import gradcheck
from torch.autograd import Variable
import torch
from torch.utils.data import random_split
from scipy.optimize import minimize, check_grad
import os
import sys
sys.path[0] = "/Users/joekadi/Documents/University/5thYear/Thesis/Code/MSci-Project/pyTorch/"
from maxent_irl.obj_functions.NLLFunction import *
from maxent_irl.obj_functions.NLLModel import *
from benchmarks.gridworld import *
from benchmarks.objectworld import *
from maxent_irl.linearvalueiteration import *
import pprint
from maxent_irl.sampleexamples import *
import random
import numpy as np
import pandas as pd
import time
from sklearn.preprocessing import MinMaxScaler
import math as math
import copy 
import torchvision
import torchvision.transforms as transforms
from clearml import Task


from clearml.automation import UniformParameterRange, UniformIntegerParameterRange
from clearml.automation import HyperParameterOptimizer
from clearml.automation.optuna import OptimizerOptuna

import pickle

from torch.utils.tensorboard import SummaryWriter
tensorboard_writer = SummaryWriter('./tensorboard_logs')

torch.set_printoptions(precision=5, sci_mode=False, threshold=100000)
torch.set_default_tensor_type(torch.DoubleTensor)
np.set_printoptions(precision=5, threshold=100000, suppress=False)
class LitModel(nn.Module):

    NLL = None
    F = None
    muE = None
    mu_sa = None
    initD = None
    mdp_data = None
    truep = None
    learned_feature_weights = None
    configuration_dict = None
    index_states_to_remove = None

    def __init__(self, no_features, activation, configuration_dict):
        
       
        super().__init__()
        self.model = nn.Sequential()
        self.model.add_module('input', nn.Linear(no_features, configuration_dict['no_neurons_in_hidden_layers']))
        if activation == 'relu':
            self.model.add_module('relu0', nn.ReLU())
        elif activation == 'tanh':
            self.model.add_module('tanh0', nn.Tanh())
        for i in range(configuration_dict['no_hidden_layers']):
            self.model.add_module('hidden'+str(i+1), nn.Linear(configuration_dict['no_neurons_in_hidden_layers'], configuration_dict['no_neurons_in_hidden_layers']))
            if activation == 'relu':
                self.model.add_module('relu'+str(i+1), nn.ReLU())
            elif activation == 'tanh':
                self.model.add_module('tanh'+str(i+1), nn.Tanh())
        self.model.add_module('final', nn.Linear(configuration_dict['no_neurons_in_hidden_layers'], no_features))

    def forward(self, x):
        return self.model(x)

def likelihood(r, initD, mu_sa, muE, F, mdp_data):
    #Returns NLL w.r.t input r

    '''
    if(torch.is_tensor(r) == False):
        r = torch.tensor(r) #cast to tensor
    if(r.shape != (mdp_data['states'],5)):
        #reformat to be in shape (states,actions)
        r = torch.reshape(r, (int(mdp_data['states']),1))
        r = r.repeat((1, 5))
    '''

    if(torch.is_tensor(r) == False):
        r = torch.tensor(r) #cast to tensor
    if(r.shape != (mdp_data['states'],5)):
        #convert to full reward
        r = torch.matmul(F, r)


    #Solve MDP with current reward
    v, q, logp, p = linearvalueiteration(mdp_data, r) 

    #Calculate likelihood from logp
    likelihood = torch.empty(mu_sa.shape, requires_grad=True)


    likelihood = torch.sum(torch.sum(logp*mu_sa)) #for scalar likelihood

    #LH for each state as tensor size (states,1)
    #mul = logp*mu_sa #hold
    #likelihood = torch.sum(mul, dim=1)
    #likelihood.requires_grad = True
    
    
    return -likelihood

def ensemble_selector(loss_function, optim_for_loss, y_hats, X, init_size=1,
                      replacement=True, max_iter=100):

    """Implementation of the algorithm of Caruana et al. (2004) 'Ensemble
    Selection from Libraries of Models'. Given a loss function mapping
    predicted and ground truth values to a scalar along with a dictionary of
    models with predicted and ground truth values, constructs an optimal
    ensemble minimizing ensemble loss, by default allowing models to appear
    several times in the ensemble.

    Parameters
    ----------
    loss_function: function
        accepting two arguments - numpy arrays of predictions and true values - 
        and returning a scalar
    y_hats: dict
        with keys being model names and values being numpy arrays of predicted
        values
    init_size: int
        number of models in the initial ensemble, picked by the best loss.
        Default is 1
    replacement: bool
        whether the models should be returned back to the pool of models once
        added to the ensemble. Default is True
    max_iter: int
        number of iterations for selection with replacement to perform. Only
        relevant if 'replacement' is True, otherwise iterations continue until
        the dataset is exhausted i.e.
        min(len(y_hats.keys())-init_size, max_iter). Default is 100

    Returns
    -------
    ensemble_loss: pd.Series
        with loss of the ensemble over iterations
    model_weights: pd.DataFrame
        with model names across columns and ensemble selection iterations
        across rows. Each value is the weight of a model in the ensemble

    """
    # Step 1: compute losses
    losses = dict()
    for model, y_hat in y_hats.items():
        if optim_for_loss:
            losses[model] = loss_function.apply(y_hat, initD, mu_sa, muE, X, mdp_data)
        else:
            losses[model] = loss_function.calculate_EVD(truep, torch.matmul(X, y_hat))

    # Get the initial ensemble comprised of the best models
    losses = pd.Series(losses).sort_values()
    init_ensemble = losses.iloc[:init_size].index.tolist()

    # Compute its loss
    if init_size == 1:
        # Take the best loss
        init_loss = losses.loc[init_ensemble].values[0]
        y_hat_avg = y_hats[init_ensemble[0]].detach().clone()
    else:
        # Average the predictions over several models
        y_hat_avg = np.array(
            [y_hats[mod] for mod in init_ensemble]).mean(axis=0)
        if optim_for_loss:
            init_loss = loss_function.apply(
                y_hat, initD, mu_sa, muE, F, mdp_data)
        else:
            init_loss = loss_function.calculate_EVD(truep, torch.matmul(X, y_hat))

    # Define the set of available models
    if replacement:
        available_models = list(y_hats.keys())
    else:
        available_models = losses.index.difference(init_ensemble).tolist()
        # Redefine maximum number of iterations
        max_iter = min(len(available_models), max_iter)

    # Sift through the available models keeping track of the ensemble loss
    # Redefine variables for the clarity of exposition
    current_loss = init_loss
    current_size = init_size

    loss_progress = [current_loss.detach().numpy()]
    ensemble_members = [init_ensemble]
    for i in range(max_iter):
        # Compute weights for predictions
        w_current = current_size / (current_size + 1)
        w_new = 1 / (current_size + 1)

        # Try all models one by one
        tmp_losses = dict()
        tmp_y_avg = dict()
        for mod in available_models:
            tmp_y_avg[mod] = w_current * y_hat_avg + w_new * y_hats[mod]
            if optim_for_loss:
                tmp_losses[mod] = loss_function.apply(tmp_y_avg[mod], initD, mu_sa, muE, X, mdp_data)
            else:
                tmp_losses[mod] = loss_function.calculate_EVD(
                    truep, torch.matmul(X, tmp_y_avg[mod]))

        # Locate the best trial
        best_model = pd.Series(tmp_losses).sort_values().index[0]

        # Update the loop variables and record progress
        current_loss = tmp_losses[best_model]
        loss_progress.append(current_loss.detach().numpy())
        y_hat_avg = tmp_y_avg[best_model]
        current_size += 1
        ensemble_members.append(ensemble_members[-1] + [best_model])

        if not replacement:
            available_models.remove(best_model)
    # Organize the output
    ensemble_loss = pd.Series(loss_progress, name="loss")
    model_weights = pd.DataFrame(index=ensemble_loss.index,
                                 columns=y_hats.keys())
    for ix, row in model_weights.iterrows():
        weights = pd.Series(ensemble_members[ix]).value_counts()
        weights = weights / weights.sum()
        model_weights.loc[ix, weights.index] = weights

    return ensemble_loss, model_weights.fillna(0).astype(float)

def run_NN_ensemble(models_to_train, X, configuration_dict):

    # use ensemble selector to generate ensemble of NN's optimised by min loss or min evd depending on "opitim_for_loss" variable


    models_to_train = models_to_train  # train this many models
    max_epochs = configuration_dict['number_of_epochs']  # for this many epochs
    learning_rate = configuration_dict['base_lr']

    # Define model names
    model_names = ["M" + str(m) for m in range(models_to_train)]

    # Create paths
    TRAINING_PATH = "./noisey_paths/models/ensembles/"
    for path in [TRAINING_PATH]:
        try:
            os.mkdir(path)
        except FileExistsError:
            pass

    # Train a pool of ensemble candidates
    print("... training pool of ensemble candidates ... \n")

    for model_name in model_names:
        # Define the model and optimiser
        net = LitModel(len(X[0]), 'relu', configuration_dict)
        optimizer = torch.optim.Adam(net.parameters(), lr=configuration_dict['base_lr'], weight_decay=1e-2)

        train_loss = []
        train_evd = []

        i = 0

        for epoch in range(max_epochs):

            # Training loop
            net.train()
            epoch_loss = []
            epoch_evd = []
            for f in X:
                # Compute predicted R
                yhat = net(f.view(-1))
                yhat = yhat.reshape(len(yhat), 1)
                # compute loss and EVD
                loss = NLL.apply(yhat, initD, mu_sa, muE, X, mdp_data)
                evd = NLL.calculate_EVD(truep, torch.matmul(X, yhat))

                print('{} | EVD: {} | loss: {} '.format(
                    i, evd, loss ))

                # Backpropogate and update weights
                loss.backward()

                optimizer.step()

                # Append loss and EVD estimates
                epoch_loss.append(loss)
                epoch_evd.append(evd)
                i += 1

            # Compute metrics for this epoch
            train_loss.append(sum(epoch_loss)/len(epoch_loss))
            train_evd.append(sum(epoch_evd)/len(epoch_evd))

            print("\nModel Name", model_name, "Epoch", epoch,
                  "Loss", train_loss[-1], "EVD", train_evd[-1], "\n")

            # Save the checkpoint
            torch.save({
                "epoch": epoch,
                "model_state_dict": net.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
                "history": pd.DataFrame({"train_loss": train_loss, "train_evd": train_evd}).astype(float),
                "train_loss": train_loss[-1],
                "train_evd": train_evd[-1]
            }, TRAINING_PATH + model_name + "_epoch_" + str(epoch) + ".p")

    # print(os.listdir(TRAINING_PATH))

    print("\n... done ...\n")

    # For each model pick the checkpoint with the lowest validation loss, then:
    # 1. compute losses and accuracies on the validation and test set
    # 2. get predictions on the validation set

    trained_models = {}
    metrics = {}
    y_hats_test = {}

    x_test = configuration_dict['regular_features']  # features for test

    for model_name in model_names:
        # Load the last checkpoint
        last_checkpoint = torch.load(
            TRAINING_PATH + model_name + "_epoch_" + str(max_epochs-1) + ".p")

        # Find the best checkpoint by train loss
        best_by_train_loss = last_checkpoint["history"].sort_values(
            "train_loss").index[0]
        best_checkpoint = torch.load(
            TRAINING_PATH + model_name + "_epoch_" + str(best_by_train_loss) + ".p")

        # Restore the best checkpoint
        net = LitModel(len(X[0]), 'relu', configuration_dict)
        net.load_state_dict(best_checkpoint["model_state_dict"])
        net.eval()

        # Compute predictions on the validation and test sets, compute the
        # metrics for the latter (validation stuff has already been saved)



        # Compute predicted R
        y_hat_test = net(x_test[0,:].view(-1))
        y_hat_test = y_hat_test.reshape(len(y_hat_test), 1)

        # compute evd & loss
        test_loss = testNLL.apply(y_hat_test, initD, mu_sa, muE, x_test, mdp_data)
        test_evd = testNLL.calculate_EVD(truep, torch.matmul(x_test, y_hat_test))

        # Store the outputs
        trained_models[model_name] = net
        metrics[model_name] = {
            "train_loss": best_checkpoint["train_loss"],
            "train_evd": best_checkpoint["train_evd"],
            "test_loss": test_loss,
            "test_evd": test_evd}

        # Store models loss in dict
        y_hats_test[model_name] = y_hat_test

    # Convert the metrics dict to a dataframe
    metrics = pd.DataFrame(metrics).T.astype(float)
    print(metrics)

    # Separate dataframes for losses and accuracies
    metrics_loss = metrics.filter(like="loss").stack().reset_index()
    metrics_loss.columns = ["model", "train/test", "loss"]

    metrics_evd = metrics.filter(like="evd").stack().reset_index()
    metrics_evd.columns = ["model", "train/test", "evd"]

    # Plot losses and accuracies
    fig, ax = plt.subplots(1, 2, figsize=(15, 7))
    sns.barplot(x="model", y="loss", hue="train/test", data=metrics_loss,
                alpha=0.75, saturation=0.90, palette=["#1f77b4", "#ff7f0e"],
                ax=ax[0])
    sns.barplot(x="model", y="evd", hue="train/test", data=metrics_evd,
                alpha=0.75, saturation=0.90, palette=["#1f77b4", "#ff7f0e"],
                ax=ax[1])

    ax[0].set_ylim(metrics_loss["loss"].min() - 1e-2,
                   metrics_loss["loss"].max() + 1e-2)
    ax[1].set_ylim(metrics_evd["evd"].min()-3e-3,
                   metrics_evd["evd"].max()+3e-3)

    ax[0].set_title("Loss", fontsize=17)
    ax[1].set_title("Expected Value Difference", fontsize=17)

    for x in ax:
        x.xaxis.set_tick_params(rotation=0, labelsize=15)
        x.yaxis.set_tick_params(rotation=0, labelsize=15)
        x.set_xlabel("Model", visible=True, fontsize=15)
        x.set_ylabel("", visible=False)

        handles, labels = x.get_legend_handles_labels()
        x.legend(handles=handles, labels=labels, fontsize=15)

    fig.tight_layout(w_pad=5)

    ensemble_loss, model_weights = ensemble_selector(
        loss_function=testNLL, optim_for_loss=True, y_hats=y_hats_test, X=x_test, init_size=1, replacement=True, max_iter=10)

    print("\nEnsemble Loss:")
    print(ensemble_loss)
    print("Ensemble Model Weight:")
    print(model_weights)

    # Locate non-zero weights and sort models by their average weight
    weights_to_plot = model_weights.loc[:, (model_weights != 0).any()]
    weights_to_plot = weights_to_plot[
        weights_to_plot.mean().sort_values(ascending=False).index]

    # A palette corresponding to the number of models with non-zero weights
    palette = sns.cubehelix_palette(weights_to_plot.shape[1], reverse=True)

    # Plot
    fig, ax = plt.subplots(1, 1, figsize=(15, 7))
    weights_to_plot.plot(kind="bar", stacked=True, color=palette, ax=ax,
                         alpha=0.85)
    ax.margins(x=0.0)
    ax.set_xlabel("Optimization Step", fontsize=15, visible=True)
    ax.set_ylabel("Ensemble Weight", fontsize=15, visible=True)
    ax.yaxis.set_tick_params(rotation=0, labelsize=15)
    ax.xaxis.set_tick_params(rotation=0, labelsize=15)
    ax.legend(loc="best", bbox_to_anchor=(1, 0.92),
              frameon=True, edgecolor="k", fancybox=False,
              framealpha=0.7, shadow=False, ncol=1, fontsize=15)
    fig.tight_layout()

    # Compute the test loss for each ensemble iteration
    ensemble_loss_test = []
    for _, row in model_weights.iterrows():
        # Compute test prediction for this iteration of ensemble weights
        tmp_y_hat = np.array(
            [y_hats_test[model_name] * weight
                for model_name, weight in row.items()]
        ).sum(axis=0)

        ensemble_loss_test.append(testNLL.apply(tmp_y_hat, initD, mu_sa, muE, x_test, mdp_data).detach().numpy())
    ensemble_loss_test = pd.Series(ensemble_loss_test)

    # Compute loss of an ensemble which equally weights each model in the pool
    losses = []
    for model, predictedR in y_hats_test.items():losses.append(testNLL.apply(predictedR, initD, mu_sa, muE, x_test, mdp_data).detach().numpy())
    ens_loss_test_avg = sum(losses) / len(losses)
    ens_loss_test_avg = ens_loss_test_avg 



    # plot
    fig, ax = plt.subplots(1, 1, figsize=(15, 7), sharey=False)

    ax.plot(ensemble_loss_test, color="#1f77b4", lw=2.75,
            label="ensemble loss")

    ax.plot(pd.Series(ensemble_loss_test[0], ensemble_loss_test.index),
            color="k", lw=1.75, ls="--", dashes=(5, 5),
            label="baseline 1: best model on validation set")

    ax.plot(pd.Series(ens_loss_test_avg, ensemble_loss.index),
            color="r", lw=1.75, ls="--", dashes=(5, 5),
            label="baseline 2: average of all models")
    ax.set_title("Test Loss", fontsize=17)

    ax.margins(x=0.0)
    ax.set_xlabel("Optimization Step", fontsize=15, visible=True)
    ax.set_ylabel("", fontsize=15, visible=False)
    ax.yaxis.set_tick_params(labelsize=15)
    ax.xaxis.set_tick_params(labelsize=15)
    ax.legend(loc="upper right", bbox_to_anchor=(1, 0.92),
              frameon=True, edgecolor="k", fancybox=False,
              framealpha=0.7, shadow=False, ncol=1, fontsize=15)
    fig.tight_layout(w_pad=3.14)



    # EVD-minimising ensemble on the test set
    ensemble_acc, model_weights = ensemble_selector(
        loss_function=testNLL, optim_for_loss=False, y_hats=y_hats_test, X=x_test, init_size=1, replacement=True, max_iter=10)

    # Compute evd of the equally weighted ensemble
    evds = []
    for model, predictedR in y_hats_test.items():
        evds.append(testNLL.calculate_EVD(truep, torch.matmul(x_test, predictedR)).detach().numpy())
    ens_acc_test_avg = sum(evds) / len(evds)
    ens_acc_test_avg = ens_acc_test_avg 

    # plot
    fig, ax = plt.subplots(1, 1, figsize=(15, 7), sharey=False)

    ax.plot(ensemble_acc, color="#1f77b4", lw=2.75, label="ensemble EVD")
    ax.plot(pd.Series(ensemble_acc[0], ensemble_acc.index), color="k",
            lw=1.75, ls="--", dashes=(5, 5), label="baseline 1: best model")
    ax.plot(pd.Series(ens_acc_test_avg, ensemble_loss.index), color="r", lw=1.75,
            ls="--", dashes=(5, 5), label="baseline 2: average of all models")
    ax.set_title("Test EVD", fontsize=17)

    ax.margins(x=0.0)
    ax.set_xlabel("Optimization Step", fontsize=15, visible=True)
    ax.set_ylabel("", fontsize=15, visible=False)
    ax.yaxis.set_tick_params(labelsize=15)
    ax.xaxis.set_tick_params(labelsize=15)
    ax.legend(loc="upper right", bbox_to_anchor=(1, 0.72),
              frameon=True, edgecolor="k", fancybox=False,
              framealpha=0.7, shadow=False, ncol=1, fontsize=15)
    fig.tight_layout()

    #plt.show()

    return y_hats_test, model_weights

def run_single_NN():

    task = Task.init(project_name='MSci-Project', task_name='Gridworld, n=32, b=4, normal') #init task on ClearML
    

    #load variables from file
    open_file = open("NNIRL_param_list.pkl", "rb")
    NNIRL_param_list = pickle.load(open_file)
    open_file.close()
    threshold = NNIRL_param_list[0]
    optim_type = NNIRL_param_list[1]
    net = NNIRL_param_list[2]
    X = NNIRL_param_list[3]
    initD = NNIRL_param_list[4]
    mu_sa = NNIRL_param_list[5]
    muE = NNIRL_param_list[6]
    F = NNIRL_param_list[7]
    #F = F.type(torch.DoubleTensor)
    mdp_data = NNIRL_param_list[8]
    configuration_dict = NNIRL_param_list[9]
    truep = NNIRL_param_list[10] 
    NLL_EVD_plots = NNIRL_param_list[11]
    example_samples = NNIRL_param_list[12]
    noisey_features = NNIRL_param_list[13]

    NLL = NLLFunction()  # initialise NLL
    #assign constants
    NLL.F = F
    NLL.muE = muE
    NLL.mu_sa = mu_sa
    NLL.initD = initD
    NLL.mdp_data = mdp_data

    configuration_dict = task.connect(configuration_dict)  #enabling configuration override by clearml

    start_time = time.time() #to time execution
    #tester = testers() #to use testing functions

    # lists for printing
    NLList = []
    iterations = []
    evdList = []

    i = 0 #track iterations
    finalOutput = None #store final est R
    loss = 1000 #init loss 
    diff = 1000 #init diff
    evd = 10 #init val

    #noisey_features=True
    if noisey_features:
        #add noise to features at states 12, 34 and 64 (when mdp_params.n=8)
        #set each states features to all 0
        print('\n... adding noise to features at states 12, 34 and 64 ...\n')
        X[11,:] = torch.zeros(X.size()[1])
        X[33,:] = torch.zeros(X.size()[1])
        X[63,:] = torch.zeros(X.size()[1])

    #if noisey_paths:
       # print('\n... adding noise to paths at states 12, 34 and 64 ...\n')


    if (optim_type == 'Adam'):
        print('\nOptimising with torch.Adam\n')
        optimizer = torch.optim.Adam(
            net.parameters(), lr=configuration_dict.get('base_lr'), weight_decay=1e-2) #weight decay for l2 regularisation
        #while(evd > threshold): #termination criteria: evd threshold
        #for p in range(configuration_dict.get('number_of_epochs')): #termination criteria: no of iters in config dict
        while diff >= threshold: #termination criteria: loss diff
        #for p in range(1): #for testing
            prevLoss = loss
            
            #net.zero_grad()
            net.zero_grad(set_to_none=True)
            output = torch.empty(len(X[0]), 1, dtype=torch.double)

            indexer = 0
            for j in range(len(X[0])):
                thisR = net(X[:, j].view(-1, len(X[:, j])))
                output[indexer] = thisR
                indexer += 1
            finalOutput = output

            loss = NLL.apply(output, initD, mu_sa, muE, F, mdp_data) #use this line for custom gradient
            #loss = likelihood(output, initD, mu_sa, muE, F, mdp_data) #use this line for auto gradient
            #tester.checkgradients_NN(output, NLL) # check gradients
            loss.backward()  # propagate grad through network
            #nn.utils.clip_grad_norm_(net.parameters(), max_norm=2.0, norm_type=2)
            evd = NLL.calculate_EVD(truep, torch.matmul(X, output))  # calc EVD
            optimizer.step()

            #printline to show est R
            #print('{}: output:\n {} | EVD: {} | loss: {} '.format(i, torch.matmul(X, output).repeat(1, 5).detach() , evd, loss.detach() ))

            #printline to hide est R
            print('{}: | EVD: {} | loss: {} | diff {}'.format(i, evd, loss  , diff))
            # store metrics for printing
            NLList.append(loss )
            iterations.append(i)
            evdList.append(evd )
            finaloutput = output
            tensorboard_writer.add_scalar('loss', loss  , i)
            tensorboard_writer.add_scalar('evd', evd, i)
            tensorboard_writer.add_scalar('diff', diff, i)

            i += 1
            diff = abs(prevLoss-loss)

    else:
        print('\implement LBFGS\n')
        
        
    PATH = './NN_IRL.pth'
    torch.save(net.state_dict(), PATH)
    tensorboard_writer.close()

    if NLL_EVD_plots:
        # plot
        f, (ax1, ax2) = plt.subplots(1, 2, sharex=True)
        ax1.plot(iterations, NLList)
        ax1.plot(iterations, NLList, 'r+')
        ax1.set_title('NLL')

        ax2.plot(iterations, evdList)
        ax2.plot(iterations, evdList, 'r+')
        ax2.set_title('Expected Value Diff')
        #plt.show()
    
    
    print("\nruntime: --- %s seconds ---\n" % (time.time() - start_time) )
    return net, finalOutput, (time.time() - start_time)

if __name__ == "__main__":

    if len(sys.argv) > 1:
        index_states_to_corrupt = int(str(sys.argv[1]))
        print('\n... got which noisey features from cmd line ...\n')
    else:
        raise Exception("Index States To Corrupt not supplied")

    if len(sys.argv) > 2:
        num_paths = int(str(sys.argv[2]))
        print('\n... got number of paths value from cmd line ...\n')
    else:
        raise Exception("Number of Paths not supplied")

    if index_states_to_corrupt < 0 or index_states_to_corrupt > 3:
        raise Exception("Index of features to corrupt must be within range 0 - 3")
    
    #Variants of states to add noise to
    states_to_remove = [np.arange(0, 32, 1), np.arange(0, 64, 1), np.arange(0, 128, 1)]

    
    # Load variables
    open_file = open(str(num_paths) + "_NNIRL_param_list.pkl", "rb")
    NNIRL_param_list = pickle.load(open_file)
    open_file.close()
    threshold = NNIRL_param_list[0]
    optim_type = NNIRL_param_list[1]
    net = NNIRL_param_list[2]
    initD = NNIRL_param_list[3]
    mu_sa = NNIRL_param_list[4]
    muE = NNIRL_param_list[5]
    mdp_data = NNIRL_param_list[6]
    truep = NNIRL_param_list[7] 
    NLL_EVD_plots = NNIRL_param_list[8]
    example_samples = NNIRL_param_list[9]
    mdp_params = NNIRL_param_list[10] 
    r = NNIRL_param_list[11] 
    mdp_solution = NNIRL_param_list[12] 
    feature_data = NNIRL_param_list[13] 
    trueNLL = NNIRL_param_list[14]
    normalise = NNIRL_param_list[15]
    user_input = NNIRL_param_list[16]
    worldtype = NNIRL_param_list[17]
        
    torch.manual_seed(mdp_params['seed'])
    np.random.seed(seed=mdp_params['seed'])
    random.seed(mdp_params['seed'])

    # Initalise tester loss function
    testNLL = NLLFunction()
    # Assign tester loss function constants
    testNLL.F = feature_data['splittable']
    testNLL.muE = muE
    testNLL.mu_sa = mu_sa
    testNLL.initD = initD
    testNLL.mdp_data = mdp_data


    #Print what benchmark
    if(user_input):
        if worldtype == "gridworld" or worldtype == "gw" or worldtype == "grid":
            print('\n... training on GridWorld benchmark ... \n')
        elif worldtype == "objectworld" or worldtype == "ow" or worldtype == "obj":
            print('\n... training on ObjectWorld benchmark ... \n')
    else:
        print('\n... training on GridWorld benchmark ... \n')

    #Print true R loss 
    print('\n... true reward loss is', trueNLL.item() ,'... \n')
    
    
    # Connect configuration dict
    configuration_dict = {'number_of_epochs': 3, 'base_lr': 0.1, 'no_hidden_layers': 3, 'no_neurons_in_hidden_layers': len(feature_data['splittable'][0])*2, 'regular_features': feature_data['splittable'] } #set config params for clearml
    #configuration_dict = task.connect(configuration_dict)

    
    # Remove chosen states from paths
    if states_to_remove[index_states_to_corrupt] is not None:
        N = len(example_samples)
        top_index = math.ceil(0.5*N)
        twenty_percent_example_samples = example_samples[0:top_index]
        for path in twenty_percent_example_samples:
            T = len(path)
            pathindex = twenty_percent_example_samples.index(path)
            for move in path:
                moveindex = twenty_percent_example_samples[pathindex].index(move)
                #remove state
                if move[0] in states_to_remove[index_states_to_corrupt]:
                    newmove = move
                    #get new state thats not in states to remove
                    newmove = ( random.randint(states_to_remove[index_states_to_corrupt][-1]+1, 255), move[1])
                    #assign new to state to curr step in paths
                    twenty_percent_example_samples[pathindex][moveindex] = newmove       
        example_samples[0:top_index] = twenty_percent_example_samples  
        initD, mu_sa, muE, F, mdp_data = testNLL.calc_var_values(mdp_data, N, T, example_samples, feature_data)  # calculate required variables
    
    # Add noise to features
    if states_to_remove[index_states_to_corrupt] is not None:
        for state in states_to_remove[index_states_to_corrupt]:
            if random.randint(0,100) < 3: #3% chance of NOT using this state
                break
            for i in range(len(feature_data['splittable'][state,:])):
                if random.randint(0,100) < 22: #22% chance of inverting the feature
                    #invert the feature, works since binary features
                    feature_data['splittable'][state,i] =  1-feature_data['splittable'][state,i]

    # Initalise loss function
    NLL = NLLFunction()
    # Assign loss function constants
    NLL.F = feature_data['splittable']
    NLL.muE = muE
    NLL.mu_sa = mu_sa
    NLL.initD = initD
    NLL.mdp_data = mdp_data

    
    # run NN ensemble
    models_to_train = 10
    models, model_weights =  run_NN_ensemble(models_to_train, feature_data['splittable'], configuration_dict)


    # get ensemble model predictions
    ensemble_predictions = []
    for _, row in model_weights.iterrows():
        # Compute test prediction for this iteration of ensemble weights

        tmp_y_hat = np.array([models[model_name] * weight for model_name, weight in row.items()]).sum(axis=0)


        ensemble_predictions.append(torch.matmul(configuration_dict['regular_features'], tmp_y_hat).flatten())


    #stack all predicted rewards
    predictions = torch.empty(len(ensemble_predictions), 256)


    #get average predicted reward and uncertainty of all models
    average_predictions = torch.empty(mdp_params['n']**2)
    predictions_uncertainty = torch.empty(mdp_params['n']**2)
    for column in range(predictions.size()[1]):
        average_predictions[column] = torch.mean(predictions[:, column]) #save avg predicted R for each state
        predictions_uncertainty[column] = torch.var(predictions[:, column]) #save variance for each states prediciton as uncertainty


    y_mc_relu = predictions_uncertainty.detach().numpy()
    y_mc_std_relu = average_predictions.detach().numpy()


    hold = y_mc_relu.reshape(len(y_mc_relu),1)
    y_mc_relu_reward = np.repeat(hold, 5, 1)

    y_mc_relu_reward = torch.from_numpy(y_mc_relu_reward)

    y_mc_relu_v, y_mc_relu_q, y_mc_relu_logp, y_mc_relu_P = linearvalueiteration(mdp_data, y_mc_relu_reward)

    total_results = [y_mc_relu, y_mc_std_relu, y_mc_relu_reward, y_mc_relu_v, y_mc_relu_P, y_mc_relu_q, NLL.calculate_EVD(truep, y_mc_relu_reward), 10, 5,000]

    #Save results
    print('\n... saving results ...\n')

    # Create path for trained models
    RESULTS_PATH = "./total_uncertainty/results/ensembles/"
    for path in [RESULTS_PATH]:
        try:
            os.makedirs(path)
        except FileExistsError:
            pass

    file_name = RESULTS_PATH+str(worldtype)+'_'+str(0.0)+'_'+ str(num_paths)+ '_results_'+str(index_states_to_corrupt)+'.pkl'
    open_file = open(file_name, "wb")
    pickle.dump(total_results, open_file)
    open_file.close()



