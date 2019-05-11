import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
import torch.nn.functional as F
import matplotlib.pyplot as plt
import cv2
import utils
#import imageio
from statistics import Statistics
import scipy.misc
#imageio.plugins.ffmpeg.download()
import copy
from buffer import Buffer
import os
import pickle

"""
class Model:

    def __init__(self,policy,cric)
"""

lr_actor = 5e-5
lr_critic = 1e-5
num_epochs = 1000
batch_size = 256
minibatch_size = 32
MAX_GRAD_NORM = 0.5
PPO_EPSILON = 0.2
CRITIC_BALANCE = 0.5
ENTROPY_BETA = 0.01

LR_DECAY = 1



cuda = torch.cuda.is_available()
device = "cuda:0" if cuda else "cpu"

def train(algo):

    env = algo.env
    policy = algo.policy
    critic = algo.critic
    softmax = nn.Softmax(dim=-1)
    logsoftmax = nn.LogSoftmax(dim=-1)

    statistics = Statistics(scenario = algo.env_string,
                            method = algo.method,
                            epochs = num_epochs,
                            directory = 'stats/ppo_transfer_learn/defend_the_center/run1/')


    statistics.batch_size = batch_size
    statistics.mini_batch_size = minibatch_size

    if algo.pre_trained:

        #only last layer of classifier
        params = policy.state_dict()
        #params_to_update_actor = list(params['classifier.6.weight']) + list(params['classifier.6.bias'])
        params_to_update_actor = []
        for name,param in policy.named_parameters():
            if param.requires_grad == True:
                params_to_update_actor.append(param)            

        params_to_update_critic = []
        for name,param in critic.named_parameters():
            if param.requires_grad == True:
                params_to_update_critic.append(param)

        optimizer_actor = optim.Adam(params_to_update_actor, lr=lr_actor) 
        optimizer_critic = optim.Adam(params_to_update_critic, lr=lr_critic)


        #optimizer_actor = optim.Adam([
        #        {'params': policy.features.parameters()},
        #        {'params': policy.classifier.parameters(), 'lr': lr_actor}
        #    ], lr=0)

        #optimizer_critic = optim.Adam([
        #        {'params': critic.vgg.features.parameters()}, 'lr': 0}
        #        {'params': critic.vgg.classifier.parameters(), 'lr': lr_critic/3},
        #        {'params': critic.final_layer.parameters(), 'lr': lr_critic}
        #    ], lr=0)

    else:
        optimizer_actor = optim.Adam(policy.parameters(), lr=lr_actor)
        optimizer_critic = optim.Adam(critic.parameters(),lr = lr_critic) 


    lambda_lr = lambda epoch: LR_DECAY**epoch
    scheduler_actor = torch.optim.lr_scheduler.LambdaLR(optimizer_actor, lr_lambda=lambda_lr)
    scheduler_critic = torch.optim.lr_scheduler.LambdaLR(optimizer_critic, lr_lambda=lambda_lr)

    policy = policy.to(device)
    critic = critic.to(device)

    max_reward = -1e8
    loss_lsq = torch.nn.MSELoss()
    NLL = nn.NLLLoss(reduction='none') # cost

    training_rewards, losses_actor , losses_critic = [], [], []
    print('Start training')

    for epoch in range(num_epochs):

        #first initialize env

        s = env.reset() # now coming in (640, 640, 1, 12) because of tweaks in gdoom_wrappers to get colour
        s = stack_individual_color_channels(s) 
        states_human_size = [np.asarray(s,dtype = np.uint8)] #keep 640*640*3*4 frame for display purpose
        s = cropping(s,224) # all pretrained models are based on 224x224 input
        s = mean_rgb_stack(s) # classifier outputs a classification for each of the 4 images in stack
        # I chose to do the mean of each channel to choose the action, alternatively maybe the most "prevalent" category should decide the action
        #scipy.misc.imsave('videos/test_color/test_normalize.jpg', s)
        
        num_episode = 1
        #create empty buffer
        batch_buffer = Buffer(batch_size, minibatch_size)
        rewards_of_episode = []

        # build batch
        for idx_in_batch in range(batch_size):
            # generate rollout by iteratively evaluating the current policy on the environment
            with torch.no_grad():
                # s is 224*224*3*4 -> 4*3*224*224
                s_tensor = torch.from_numpy(normalize(s)).float().permute(2,0,1).view(1,3,224,224)
                s_tensor = s_tensor.to(device)
                classification = policy(s_tensor) 
                a_log_probs = logsoftmax(classification)
                estimated_value = critic(s_tensor)

            a = (np.cumsum(np.exp(a_log_probs.cpu().numpy())) > np.random.rand()).argmax() # sample action
            # if np.random.rand() < 0.2:
            #     a = np.random.randint(7)
            s1, r, done, info = env.step(int(a)) #s1 comes in 640x640x4
            s1 = stack_individual_color_channels(s1)
            states_human_size.append(np.asarray(s1,dtype = np.uint8))
            s1 = cropping(s1,224)
            s1 = mean_rgb_stack(s1)            
            batch_buffer.states.append(s) # s is at this point (264,264,3), a single RGB image result of the mean of the stack of 4
            batch_buffer.actions.append(a)
            batch_buffer.a_log_probs.append(a_log_probs.cpu().numpy())
            batch_buffer.rewards_of_batch.append(r)
            batch_buffer.value_observations.append(estimated_value) #y's of critic

            if not done:
                batch_buffer.masks.append(1)

            if done:
                batch_buffer.masks.append(0)
                statistics.rewards_per_episode.append(info['accumulated_reward']) #not with the death penalty
                statistics.length_episodes.append(info['time_alive'])
                statistics.kills_per_episode.append(info['kills'])
                rewards_of_episode = []
                num_episode += 1
                s = env.reset()
                s = stack_individual_color_channels(s) 
                s = cropping(s,224)
                s = mean_rgb_stack(s)

            else:
                s = s1

        print("Batch built")
        statistics.episode_per_epoch.append(num_episode)
        s_tensor = torch.from_numpy(normalize(s)).float().permute(2,0,1).view(1,3,224,224)
        s_tensor = s_tensor.to(device)
        next_value = critic(s_tensor)
        batch_buffer.next_value = next_value
        # prepare batch
        batch_buffer.prepare_batch()

        for states, actions, log_prob_old, advantages, returns in batch_buffer:
            print("optimizing batch")

            states = states.to(device)
            vgg_output = policy(states.permute(0,3,1,2)) # permute because of channel first in pytorch conv layer
            #a_probs = softmax(vgg_output)
            a_log_probs = logsoftmax(vgg_output)
            values = critic(states.permute(0,3,1,2))
            log_likelihood_new = NLL(a_log_probs, torch.LongTensor(actions).to(device))
            log_likelihood_old = NLL(log_prob_old.view(minibatch_size, -1).to(device), torch.LongTensor(actions).to(device))

            optimizer_actor.zero_grad()
            optimizer_critic.zero_grad()

            advantages = Variable(advantages, requires_grad=True).to(device)

            # A2C losses
            # loss_actor = torch.sum(advantages.detach()*(log_likelihood)) #mean vs sum
            # loss_critic = torch.sum(advantages.pow(2) )

            #PPO losses
            prob_ratio = torch.exp(log_likelihood_old - log_likelihood_new) #opposite sign cause computed by NLL
            surrogate_objective = prob_ratio * advantages
            surrogate_objective_clipped = torch.clamp(prob_ratio, 1 - PPO_EPSILON, 1 + PPO_EPSILON) * advantages

            entropy = - torch.exp(a_log_probs) * a_log_probs
            entropy = entropy.sum(-1)
            loss_actor = - torch.min(surrogate_objective, surrogate_objective_clipped).sum()
            advantages_new = returns.to(device) - values.view(-1)
            loss_critic = advantages_new.pow(2).sum()

            loss = loss_actor + CRITIC_BALANCE * loss_critic - ENTROPY_BETA * entropy.sum()

            # loss_actor.backward()
            # loss_critic.backward()

            loss.backward()

            nn.utils.clip_grad_norm_(policy.parameters(), MAX_GRAD_NORM)
            nn.utils.clip_grad_norm_(critic.parameters(), MAX_GRAD_NORM)
            optimizer_actor.step()
            optimizer_critic.step()
            # scheduler_actor.step()
            # scheduler_critic.step()

            losses_actor.append(loss_actor.item())
            losses_critic.append(loss_critic.item())


            statistics.loss_actor.append(loss_actor.item())
            statistics.loss_critic.append(loss_critic.item())


           # bookkeeping
        total_reward = batch_buffer.rewards_of_batch.sum()
        if max_reward < total_reward:
            max_reward = total_reward


        print("==========================================")
        print("Epoch: ",(epoch+1), "/", num_epochs)
        print("-----------")
        print("Number of training episodes: {}".format(num_episode))
        print("Total reward: {0:.2f}".format(total_reward))
        print("Max reward so far: {0:.2f}".format(max_reward))
        print("Mean Reward of that batch {0:.2f}".format(batch_buffer.rewards_of_batch.mean()))
        print("Training Loss for Actor: {0:.2f}".format(loss_actor.item()))
        print("Training Loss for Critic: {0:.2f}".format(loss_critic.item()))
        #print("Length of last episode: {0:.2f}".format(rewards_of_batch.shape[0]))

        #if epoch % 100 == 0:
            #directory = "states_human_size/transfer_learning_ppo/run1/"
            #if not os.path.exists(directory):
            #    os.makedirs(directory)           
            #write_pickle(states_human_size,"states_human_size/transfer_learning_ppo/run1/",'states_human_size_' + str(epoch))

            #format_frames = np.array(states_human_size)
            #directory = 'videos/transfer_learning_ppo/'
            #if not os.path.exists(directory):
            #    os.makedirs(directory)
            #imageio.mimwrite(directory+str(epoch)+'.mp4', format_frames[:,:,:,:,0], fps = 15)

    statistics.get_statistics()
    directory = 'saved_models/transfer_learning_ppo/run1/'
    if not os.path.exists(directory):
            os.makedirs(directory)
    torch.save(policy.state_dict(),directory + 'policiyParams.pickle')
    torch.save(critic.state_dict(), directory +'criticParams.pickle')
    print('done')

def write_pickle (f, path, fname):
        with open(path + fname, 'wb') as handle:
            pickle.dump(f, handle, protocol = pickle.HIGHEST_PROTOCOL)

def normalize(img):
    return (img)/255.0

def cropping(s, size):
    frame = np.asarray(s)
    if len(frame.shape) == 3:
        frame = np.asarray(s)
        frame = cv2.resize(frame, (size,size))
        return frame
    elif len(frame.shape) == 4:
        new_s = np.zeros((size,size,3,4))
        for i in range(frame.shape[-1]):
            # resize each color channel, must be a better way but oh well 
            r,g,b = cv2.resize(frame[:,:,0,i], (size,size)), cv2.resize(frame[:,:,1,i], (size,size)),cv2.resize(frame[:,:,2,i], (size,size))
            new_s[:,:,0,i] = r
            new_s[:,:,1,i] = g
            new_s[:,:,2,i] = b
        
        return new_s
    
def stack_individual_color_channels(s):
    lin_space = [0,3,6,9]
    new_s = np.zeros((640,640,3,4))
    for index,i in enumerate(lin_space):
        r,g,b = s[:,:,0,0 + i] , s[:,:,0, 1 + i] , s[:,:,0,2 + i]
        stacked = np.dstack((r,g,b))
        new_s[:,:,:,index] = stacked
    return new_s

def mean_rgb_stack(s):
    #last dimension is size of stack
    h,w,colors,stack = s.shape
    new_s = np.zeros((h,w,colors))
    for i in range(stack):
        new_s[:,:,0] += s[:,:,0,i]
        new_s[:,:,1] += s[:,:,1,i]
        new_s[:,:,2] += s[:,:,2,i]

    new_s /= stack
    return new_s