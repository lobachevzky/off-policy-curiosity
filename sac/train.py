import itertools
import pickle
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Tuple, Union

import gym
import numpy as np
import tensorflow as tf
from gym import spaces

from environments.hindsight_wrapper import HindsightWrapper
from environments.unsupervised import UnsupervisedEnv
from sac.agent import AbstractAgent, PropagationAgent
from sac.policies import CategoricalPolicy, GaussianPolicy
from sac.replay_buffer import ReplayBuffer
from sac.utils import PropStep, State, Step

LOGGER_VALUES = """\
entropy
V loss
Q loss
pi loss
V grad
Q grad
pi grad\
""".split('\n')

class Trainer:
    def __init__(self, env: gym.Env, seed: Optional[int], buffer_size: int,
                 activation: Callable, n_layers: int, layer_size: int,
                 learning_rate: float, reward_scale: float, grad_clip: Optional[float],
                 batch_size: int, num_train_steps: int, mimic_dir: Optional[str],
                 logdir: str, save_path: str, load_path: str, render: bool):

        if seed is not None:
            np.random.seed(seed)
            tf.set_random_seed(seed)
            env.seed(seed)

        self.num_train_steps = num_train_steps
        self.batch_size = batch_size
        self.env = env
        self.buffer = ReplayBuffer(buffer_size)
        self.reward_scale = reward_scale

        if mimic_dir:
            for path in Path(mimic_dir).iterdir():
                if path.suffix == '.pkl':
                    with Path(path).open('rb') as f:
                        self.buffer.extend(pickle.load(f))
                print('Loaded mimic file {} into buffer.'.format(path))

        s1 = self.reset()

        self.agent = agent = self.build_agent(
            activation=activation,
            n_layers=n_layers,
            layer_size=layer_size,
            learning_rate=learning_rate,
            grad_clip=grad_clip)

        if isinstance(env.unwrapped, UnsupervisedEnv):
            # noinspection PyUnresolvedReferences
            env.unwrapped.initialize(agent.sess, self.buffer)

        saver = tf.train.Saver()
        tb_writer = None
        if load_path:
            saver.restore(agent.sess, load_path)
            print("Model restored from", load_path)
        if logdir:
            tb_writer = tf.summary.FileWriter(logdir=logdir, graph=agent.sess.graph)

        count = Counter(reward=0, episode=0)
        episode_count = Counter()
        info_counter = Counter()
        info_log_keys = set()
        evaluation_period = 10

        for time_steps in itertools.count():
            is_eval_period = count['episode'] % evaluation_period == evaluation_period - 1
            a = agent.get_actions([self.vectorize_state(s1)], sample=(not is_eval_period))
            if render:
                env.render()
            s2, r, t, info = self.step(a)
            if 'print' in info:
                print('time-step:', time_steps, info['print'])
            if 'log' in info:
                info_log_keys |= info['log'].keys()
                info_counter.update(Counter(info['log']))

            tick = time.time()

            episode_count.update(Counter(reward=r, timesteps=1))
            if save_path and time_steps % 5000 == 0:
                print("model saved in path:", saver.save(agent.sess, save_path=save_path))
            if not is_eval_period:
                self.add_to_buffer(Step(s1=s1, a=a, r=r * reward_scale, s2=s2, t=t))
                if self.buffer_full():
                    for i in range(self.num_train_steps):
                        sample_steps = self.sample_buffer()
                        # noinspection PyProtectedMember
                        step = self.agent.train_step(
                            sample_steps._replace(
                                s1=list(map(self.vectorize_state, sample_steps.s1)),
                                s2=list(map(self.vectorize_state, sample_steps.s2)),
                            ))
                        episode_count.update(
                            Counter({
                                k: getattr(step, k.replace(' ', '_'))
                                for k in LOGGER_VALUES
                            }))
            s1 = s2
            if t:
                s1 = self.reset()
                episode_reward = episode_count['reward']
                episode_timesteps = episode_count['timesteps']
                count.update(Counter(reward=episode_reward, episode=1))
                print('({}) Episode {}\t Time Steps: {}\t Reward: {}'.format(
                    'EVAL' if is_eval_period else 'TRAIN', count['episode'], time_steps,
                    episode_reward))
                fps = int(episode_timesteps / (time.time() - tick))
                if logdir:
                    summary = tf.Summary()
                    if is_eval_period:
                        summary.value.add(tag='eval reward', simple_value=episode_reward)
                    summary.value.add(
                        tag='average reward',
                        simple_value=(count['reward'] / float(count['episode'])))
                    summary.value.add(tag='time-steps', simple_value=episode_timesteps)
                    summary.value.add(tag='fps', simple_value=fps)
                    summary.value.add(tag='reward', simple_value=episode_reward)
                    for k in info_log_keys:
                        summary.value.add(tag=k, simple_value=info_counter[k])
                    for k in LOGGER_VALUES:
                        summary.value.add(
                            tag=k,
                            simple_value=episode_count[k] / float(episode_timesteps))
                    tb_writer.add_summary(summary, count['episode'])
                    tb_writer.flush()

                # zero out counters
                info_counter = Counter()
                episode_count = Counter()

    def build_agent(self,
                    activation: Callable,
                    n_layers: int,
                    layer_size: int,
                    learning_rate: float,
                    grad_clip: float,
                    base_agent: AbstractAgent = AbstractAgent) -> AbstractAgent:
        state_shape = self.env.observation_space.shape
        if isinstance(self.env.action_space, spaces.Discrete):
            action_shape = [self.env.action_space.n]
            policy_type = CategoricalPolicy
        else:
            action_shape = self.env.action_space.shape
            policy_type = GaussianPolicy

        class Agent(policy_type, base_agent):
            def __init__(self, s_shape, a_shape):
                super(Agent, self).__init__(
                    s_shape=s_shape,
                    a_shape=a_shape,
                    activation=activation,
                    n_layers=n_layers,
                    layer_size=layer_size,
                    learning_rate=learning_rate,
                    grad_clip=grad_clip)

        return Agent(state_shape, action_shape)

    def reset(self) -> State:
        return self.env.reset()

    def step(self, action: np.ndarray) -> Tuple[State, float, bool, dict]:
        """ Preprocess action before feeding to env """
        if type(self.env.action_space) is spaces.Discrete:
            # noinspection PyTypeChecker
            return self.env.step(np.argmax(action))
        else:
            action = np.tanh(action)
            hi, lo = self.env.action_space.high, self.env.action_space.low
            # noinspection PyTypeChecker
            return self.env.step((action + 1) / 2 * (hi - lo) + lo)

    def vectorize_state(self, state: State) -> np.ndarray:
        """ Preprocess state before feeding to network """
        return state

    def add_to_buffer(self, step: Step) -> None:
        assert isinstance(step, Step)
        self.buffer.append(step)

    def buffer_full(self):
        return len(self.buffer) >= self.batch_size

    def sample_buffer(self):
        return Step(*self.buffer.sample(self.batch_size))


class TrajectoryTrainer(Trainer):
    def __init__(self, **kwargs):
        self.trajectory = []
        super().__init__(**kwargs)
        self.s1 = self.reset()

    def step(self, action: np.ndarray) -> Tuple[State, float, bool, dict]:
        s2, r, t, i = super().step(action)
        self.trajectory.append(Step(s1=self.s1, a=action, r=r, s2=s2, t=t))
        self.s1 = s2
        return s2, r, t, i

    def reset(self) -> State:
        self.trajectory = []
        self.s1 = super().reset()
        return self.s1


class HindsightTrainer(TrajectoryTrainer):
    def __init__(self, env, **kwargs):
        assert isinstance(env, HindsightWrapper)
        super().__init__(env=env, **kwargs)

    def reset(self) -> State:
        assert isinstance(self.env, HindsightWrapper)
        for s1, a, r, s2, t in self.env.recompute_trajectory(self.trajectory):
            self.buffer.append((s1, a, r * self.reward_scale, s2, t))
        return super().reset()

    def vectorize_state(self, state: State) -> np.ndarray:
        assert isinstance(self.env, HindsightWrapper)
        return self.env.vectorize_state(state)


class PropagationTrainer(TrajectoryTrainer):
    def add_to_buffer(self, _):
        pass

    def build_agent(self, **kwargs) -> AbstractAgent:
        return super().build_agent(base_agent=PropagationAgent, **kwargs)

    def reset(self) -> State:
        self.buffer.extend(self.step_generator(self.trajectory))
        return super().reset()

    def step_generator(self, trajectory: Iterable[Step]) -> Iterator[PropStep]:
        v2 = 0
        for step in reversed(trajectory):
            v2 = .99 * v2 + step.r
            # noinspection PyProtectedMember
            prop_step = PropStep(v2=v2, **step._asdict())
            # noinspection PyProtectedMember
            yield prop_step._replace(r=step.r * self.reward_scale)

    def sample_buffer(self):
        return PropStep(*self.buffer.sample(self.batch_size))


class HindsightPropagationTrainer(HindsightTrainer, PropagationTrainer):
    def reset(self) -> State:
        assert isinstance(self.env, HindsightWrapper)
        trajectory = list(self.env.recompute_trajectory(self.trajectory))
        self.buffer.extend(self.step_generator(trajectory))
        return PropagationTrainer.reset(self)
