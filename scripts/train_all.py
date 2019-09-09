from collections import deque
from itertools import chain

import torch
import torch.nn as nn
import numpy as np
import os
import wandb

from src.agent import Agent
from src.memory import ReplayMemory
from src.encoders import NatureCNN, ImpalaCNN
from src.envs import make_vec_envs, Env
from src.eval import test
from src.forward_model import ForwardModel
from src.stdim import InfoNCESpatioTemporalTrainer
from src.utils import get_argparser, log
from src.episodes import get_random_agent_episodes, sample_state, Transition


def train_policy(args):
    env = Env(args)
    env.train()

    # get initial exploration data
    real_transitions = get_random_agent_episodes(args)
    model_transitions = ReplayMemory(args, args.fake_buffer_capacity)
    j, rollout_length = 0, args.rollout_length
    dqn = Agent(args, env)
    dqn.train()
    results_dir = os.path.join('results', args.id)
    metrics = {'steps': [], 'rewards': [], 'Qs': [], 'best_avg_reward': -float('inf')}

    state, done = env.reset(), False
    while j * args.env_steps_per_epoch < args.total_steps:
        # Train encoder and forward model on real data
        encoder = train_encoder(args, real_transitions)
        forward_model = train_model(args, encoder, real_transitions)

        timestep, done = 0, True
        for e in range(args.env_steps_per_epoch):
            if done:
                state, done = env.reset(), False
            # Take action in env acc. to current policy, and add to real_transitions
            real_z = encoder(state).view(-1)
            action = dqn.act(real_z)
            next_state, reward, done = env.step(action)
            state = state[-1].mul(255).to(dtype=torch.uint8, device=torch.device('cpu'))
            real_transitions.append(Transition(timestep, state, action, reward, not done))
            state = next_state
            timestep = 0 if done else timestep + 1

            for m in range(args.num_model_rollouts):
                # sample a state uniformly from real_transitions
                state_deque = sample_state(real_transitions, encoder, device=args.device)
                # Perform k-step model rollout starting from s using current policy
                # Add imagined data to model_transitions
                for k in range(rollout_length):
                    z = torch.stack(list(state_deque))
                    z = z.view(-1)
                    action = dqn.act(z)
                    with torch.no_grad():
                        next_z, reward = forward_model.predict(z, action)
                    # figure out what to do about terminal state here
                    z = z.view(4, -1)
                    model_transitions.append(z, action, reward, False)
                    state_deque.append(next_z)

            # Update policy parameters on model data
            for g in range(args.updates_per_step):
                dqn.learn(model_transitions)

        steps = (j+1) * args.env_steps_per_epoch
        if (j * args.env_steps_per_epoch) % args.evaluation_interval == 0:
            dqn.eval()  # Set DQN (online network) to evaluation mode
            avg_reward = test(args, steps, dqn, encoder, metrics, results_dir)  # Test
            log(steps, avg_reward)
            dqn.train()  # Set DQN (online network) back to training mode

        # Update target network
        if (j * args.env_steps_per_epoch) % args.target_update == 0:
            dqn.update_target_net()

        j += 1


def train_encoder(args, transitions, val_eps=None):

    observation_shape = transitions[0].state.shape
    if args.encoder_type == "Nature":
        encoder = NatureCNN(observation_shape[0], args)
    elif args.encoder_type == "Impala":
        encoder = ImpalaCNN(observation_shape[0], args)
    encoder.to(args.device)
    torch.set_num_threads(1)

    config = {}
    config.update(vars(args))
    config['obs_space'] = observation_shape  # weird hack
    if args.method == "infonce-stdim":
        trainer = InfoNCESpatioTemporalTrainer(encoder, config, device=args.device, wandb=wandb)
    else:
        assert False, "method {} has no trainer".format(args.method)

    trainer.train(transitions, val_eps)
    return encoder


def train_model(args, encoder, real_transitions, val_eps=None):
    forward_model = ForwardModel(args, encoder)
    forward_model.train(real_transitions)
    return forward_model


if __name__ == '__main__':
    wandb.init()
    parser = get_argparser()
    args = parser.parse_args()
    if torch.cuda.is_available() and not args.disable_cuda:
        args.device = torch.device('cuda')
        torch.cuda.manual_seed(np.random.randint(1, 10000))
        torch.backends.cudnn.enabled = args.enable_cudnn
    else:
        args.device = torch.device('cpu')
    tags = []
    wandb.init(project=args.wandb_proj, entity="abs-world-models", tags=tags)
    train_policy(args)
