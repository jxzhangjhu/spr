# -*- coding: utf-8 -*-
from __future__ import division
from collections import namedtuple
import numpy as np
import torch
from recordclass import recordclass

from rlpyt.replays.sequence.prioritized import SamplesFromReplayPri
from src.envs import get_example_outputs

from rlpyt.replays.non_sequence.frame import AsyncPrioritizedReplayFrameBuffer
from rlpyt.replays.sequence.n_step import SamplesFromReplay
from rlpyt.replays.sequence.frame import AsyncPrioritizedSequenceReplayFrameBuffer, \
    AsyncUniformSequenceReplayFrameBuffer, PrioritizedSequenceReplayFrameBuffer
from rlpyt.utils.buffer import torchify_buffer, numpify_buffer
from rlpyt.utils.collections import namedarraytuple
from rlpyt.utils.misc import extract_sequences
import traceback

Transition = recordclass('Transition', ('timestep', 'state', 'action', 'reward', 'value', 'policy', 'nonterminal'))
blank_trans = Transition(0, torch.zeros(84, 84, dtype=torch.uint8), 0, 0., 0., 0, False)  # TODO: Set appropriate default policy value
blank_batch_trans = Transition(0, torch.zeros(1, 84, 84, dtype=torch.uint8), 0, 0., 0., 0, False)

PrioritizedSamples = namedarraytuple("PrioritizedSamples",
                                  ["samples", "priorities"])
SamplesToBuffer = namedarraytuple("SamplesToBuffer",
                                  ["observation", "action", "reward", "done", "policy_probs", "value"])
SamplesFromReplayExt = namedarraytuple("SamplesFromReplayPriExt",
                                       SamplesFromReplay._fields + ("values", "age"))
SamplesFromReplayPriExt = namedarraytuple("SamplesFromReplayPriExt",
                                       SamplesFromReplayPri._fields + ("value", "policy"))
EPS = 1e-6


def samples_to_buffer(observation, action, reward, done, policy_probs, value, priorities=None):
    samples = SamplesToBuffer(
        observation=observation,
        action=action,
        reward=reward,
        done=done,
        policy_probs=policy_probs,
        value=value
        )
    if priorities is not None:
        return PrioritizedSamples(samples=samples,
                                  priorities=priorities)
    else:
        return samples

class AsyncUniformSequenceReplayFrameBufferExtended(AsyncUniformSequenceReplayFrameBuffer):
    """
    Extends AsyncPrioritizedSequenceReplayFrameBuffer to return policy_logits and values too during sampling.
    """
    def sample_batch(self, batch_B):
        while True:
            try:
                self._async_pull()  # Updates from writers.
                batch_T = self.batch_T
                T_idxs, B_idxs = self.sample_idxs(batch_B, batch_T)
                sampled_indices = True
                if self.rnn_state_interval > 1:
                    T_idxs = T_idxs * self.rnn_state_interval
                batch = self.extract_batch(T_idxs, B_idxs, self.batch_T)

            except Exception as _:
                print("FAILED TO LOAD BATCH")
                if sampled_indices:
                    print("B_idxs:", B_idxs, flush=True)
                    print("T_idxs:", T_idxs, flush=True)
                    print("Batch_T:", self.batch_T, flush=True)
                    print("Buffer T:", self.T, flush=True)

            values = torch.from_numpy(extract_sequences(self.samples.value, T_idxs, B_idxs, self.batch_T+self.n_step_return+1))
            policies = torch.from_numpy(extract_sequences(self.samples.policy, T_idxs, B_idxs, self.batch_T+self.n_step_return+1))
            batch = SamplesFromReplayPriExt(*batch,
                                            values=values,
                                            policies=policies)
            if self.batch_T > 1:
                batch = self.sanitize_batch(batch)
            return batch

    def sanitize_batch(self, batch):
        has_dones, inds = torch.max(batch.done, 0)
        for i, (has_done, ind) in enumerate(zip(has_dones, inds)):
            if not has_done:
                continue
            batch.all_observation[ind+1:, i] = batch.all_observation[ind, i]
            batch.all_reward[ind+1:, i] = 0
            batch.return_[ind+1:, i] = 0
            batch.done_n[ind+1:, i] = True
            batch.values[ind+1:, i] = 0
        return batch


class AsyncPrioritizedSequenceReplayFrameBufferExtended(AsyncPrioritizedSequenceReplayFrameBuffer):
    """
    Extends AsyncPrioritizedSequenceReplayFrameBuffer to return policy_logits and values too during sampling.
    """
    def sample_batch(self, batch_B):
        while True:
            try:
                self._async_pull()  # Updates from writers.
                (T_idxs, B_idxs), priorities = self.priority_tree.sample(
                    batch_B, unique=self.unique)
                sampled_indices = True
                if self.rnn_state_interval > 1:
                    T_idxs = T_idxs * self.rnn_state_interval

                batch = self.extract_batch(T_idxs, B_idxs, self.batch_T)

            except Exception as _:
                print("FAILED TO LOAD BATCH")
                traceback.print_exc()
                if sampled_indices:
                    print("B_idxs:", B_idxs, flush=True)
                    print("T_idxs:", T_idxs, flush=True)
                    print("Batch_T:", self.batch_T, flush=True)
                    print("Buffer T:", self.T, flush=True)

            is_weights = (1. / (priorities + 1e-5)) ** self.beta
            is_weights /= max(is_weights)  # Normalize.
            is_weights = torchify_buffer(is_weights).float()

            values = torch.from_numpy(extract_sequences(self.samples.value, T_idxs, B_idxs, self.batch_T+self.n_step_return+1))
            policies = torch.from_numpy(extract_sequences(self.samples.policy, T_idxs, B_idxs, self.batch_T+self.n_step_return+1))
            batch = SamplesFromReplayPriExt(*batch,
                                            value=values,
                                            policy=policies,
                                            is_weights=is_weights,)
            if self.batch_T > 1:
                batch = self.sanitize_batch(batch)
            return batch

    def sanitize_batch(self, batch):
        has_dones, inds = torch.max(batch.done, 0)
        for i, (has_done, ind) in enumerate(zip(has_dones, inds)):
            if not has_done:
                continue
            batch.all_observation[ind+1:, i] = batch.all_observation[ind, i]
            batch.all_reward[ind+1:, i] = 0
            batch.return_[ind+1:, i] = 0
            batch.done_n[ind+1:, i] = True
            batch.value[ind+1:, i] = 0
        return batch
