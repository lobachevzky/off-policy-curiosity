from abc import abstractmethod
from collections import namedtuple
from copy import deepcopy
from typing import Optional

import gym
import numpy as np
from gym import spaces
from gym.spaces import Box

from environments.frozen_lake import FrozenLakeEnv
from environments.lift import LiftEnv
from environments.mujoco import distance_between, MujocoEnv
from environments.shift import ShiftEnv
from sac.array_group import ArrayGroup
from sac.utils import Step, unwrap_env, vectorize

Goal = namedtuple('Goal', 'gripper block')


class Observation(namedtuple('Obs', 'observation achieved_goal desired_goal')):
    def replace(self, **kwargs):
        return super()._replace(**kwargs)


class HindsightWrapper(gym.Wrapper):
    @abstractmethod
    def _achieved_goal(self):
        raise NotImplementedError

    @abstractmethod
    def _is_success(self, achieved_goal, desired_goal):
        raise NotImplementedError

    @abstractmethod
    def _desired_goal(self):
        raise NotImplementedError

    def _add_goals(self, env_obs):
        observation = Observation(observation=env_obs,
                                  desired_goal=self._desired_goal(),
                                  achieved_goal=self._achieved_goal())
        return observation

    def step(self, action):
        o2, r, t, info = self.env.step(action)
        return self._add_goals(o2), r, t, info

    def reset(self):
        return self._add_goals(super().reset())

    def recompute_trajectory(self, trajectory: Step):
        trajectory = Step(*deepcopy(trajectory))

        # get values
        o1 = Observation(*trajectory.o1)
        o2 = Observation(*trajectory.o2)
        achieved_goal = ArrayGroup(o2.achieved_goal)[-1]

        # perform assignment
        ArrayGroup(o1.desired_goal)[:] = achieved_goal
        ArrayGroup(o2.desired_goal)[:] = achieved_goal
        trajectory.r[:] = self._is_success(o2.achieved_goal, o2.desired_goal)
        trajectory.t[:] = np.logical_or(trajectory.t, trajectory.r)

        first_terminal = np.flatnonzero(trajectory.t)[0]
        return ArrayGroup(trajectory)[:first_terminal + 1]  # include first terminal

    def preprocess_obs(self, obs, shape: tuple = None):
        obs = Observation(*obs)
        obs = [obs.observation, obs.desired_goal]
        return vectorize(obs, shape=shape)


class MountaincarHindsightWrapper(HindsightWrapper):
    """
    new obs is [pos, vel, goal_pos]
    """

    def __init__(self, env):
        super().__init__(env)
        self.observation_space = Box(
            low=vectorize([self.observation_space.low, env.unwrapped.min_position]),
            high=vectorize([self.observation_space.high, env.unwrapped.max_position]))

    def step(self, action):
        o2, r, t, info = super().step(action)
        is_success = self._is_success(o2.achieved_goal, o2.desired_goal)
        new_t = is_success or t
        new_r = float(is_success)
        info['base_reward'] = r
        return o2, new_r, new_t, info

    def _achieved_goal(self):
        return self.env.unwrapped.state[0]

    def _desired_goal(self):
        return 0.45

    def _is_success(self, achieved_goal, desired_goal):
        return achieved_goal >= desired_goal


class MujocoHindsightWrapper(HindsightWrapper):
    def __init__(self, env, geofence):
        super().__init__(env)
        self.mujoco_env = unwrap_env(env, lambda e: isinstance(e, MujocoEnv))
        self._geofence = geofence
        self.observation_space = Box(
            low=vectorize(
                Observation(
                    observation=env.observation_space.low,
                    desired_goal=Goal(self.goal_space.low, self.goal_space.low),
                    achieved_goal=None)),
            high=vectorize(
                Observation(
                    observation=env.observation_space.high,
                    desired_goal=Goal(self.goal_space.high, self.goal_space.high),
                    achieved_goal=None)))

    def _is_success(self, achieved_goal, desired_goal):
        achieved_goal = Goal(*achieved_goal).block
        desired_goal = Goal(*desired_goal).block
        return distance_between(achieved_goal, desired_goal) < self._geofence

    def _achieved_goal(self):
        return Goal(gripper=self.mujoco_env.gripper_pos(), block=self.mujoco_env.block_pos())

    @property
    def goal_space(self):
        return Box(low=np.array([-.14, -.2240, .4]), high=np.array([.11, .2241, .921]))


class LiftHindsightWrapper(MujocoHindsightWrapper):
    def __init__(self, env, geofence):
        super().__init__(env, geofence)
        self.lift_env = unwrap_env(env, lambda e: isinstance(e, LiftEnv))

    def _desired_goal(self):
        assert isinstance(self.lift_env, LiftEnv)
        goal = self.lift_env.initial_block_pos.copy()
        goal[2] += self.lift_env.min_lift_height
        return Goal(gripper=goal, block=goal)


class ShiftHindsightWrapper(MujocoHindsightWrapper):
    def __init__(self, env, geofence):
        self.shift_env = unwrap_env(env, lambda e: isinstance(e, ShiftEnv))
        super().__init__(env, geofence=geofence)

    def _desired_goal(self):
        assert isinstance(self.shift_env, ShiftEnv)
        block_height = self.shift_env.initial_block_pos[2]
        goal = np.append(self.shift_env.goal, block_height)
        return Goal(goal, goal)

    def step(self, action):
        o2, r, t, info = self.env.step(action)
        new_o2 = Observation(
            observation=o2.observation,
            desired_goal=self._desired_goal(),
            achieved_goal=self._achieved_goal())
        return new_o2, r, t, info

    def reset(self):
        return Observation(
            observation=self.env.reset().observation,
            desired_goal=self._desired_goal(),
            achieved_goal=self._achieved_goal())


class FrozenLakeHindsightWrapper(HindsightWrapper):
    def __init__(self, env):
        self.frozen_lake_env = unwrap_env(env, lambda e: isinstance(e, FrozenLakeEnv))
        super().__init__(env)

    def _achieved_goal(self):
        fl_env = self.frozen_lake_env
        return np.array([fl_env.s // fl_env.nrow, fl_env.s % fl_env.ncol])

    def _is_success(self, achieved_goal, desired_goal):
        return (achieved_goal == desired_goal).prod(axis=-1)

    def _desired_goal(self):
        return self.frozen_lake_env.goal_vector()

    def step(self, action):
        o2, r, t, info = self.env.step(action)
        new_o2 = Observation(
            observation=np.array(o2.observation),
            desired_goal=self._desired_goal(),
            achieved_goal=self._achieved_goal())
        return new_o2, r, t, info

    def reset(self):
        return Observation(
            observation=np.array(self.env.reset().observation),
            desired_goal=self._desired_goal(),
            achieved_goal=self._achieved_goal())
