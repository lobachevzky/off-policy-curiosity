from collections import Iterable, Sequence, Callable
from typing import Tuple, Optional, Any

import numpy as np
import tensorflow as tf
from sac.agent import AbstractAgent, NetworkOutput
from sac.utils import Step, TrainStep, TRAIN_VALUES, ArrayLike
from tensorflow.contrib.rnn import LSTMBlockCell, LSTMStateTuple, BasicLSTMCell


def mlp(inputs, layer_size, n_layers, activation):
    for i in range(n_layers):
        inputs = tf.layers.dense(inputs, layer_size, activation, name='fc' + str(i))
    return inputs


class MlpAgent(AbstractAgent):
    @property
    def seq_len(self):
        return None

    def network(self, inputs: tf.Tensor) -> NetworkOutput:
        return NetworkOutput(output=mlp(inputs=inputs,
                                        layer_size=self.layer_size,
                                        n_layers=self.n_layers,
                                        activation=self.activation),
                             state=None)


class LstmAgent(AbstractAgent):
    @property
    def seq_len(self):
        if self._seq_len:
            return self._seq_len
        return 8

    def __init__(self, layer_size: int, device_num: int, **kwargs):
        with tf.device('/gpu:' + str(device_num)):
            state_args = tf.float32, [None, layer_size]
            self.S = LSTMStateTuple(c=tf.placeholder(*state_args, name='C'),
                                    h=tf.placeholder(*state_args, name='H'))
            self.lstm = BasicLSTMCell(layer_size)
        super().__init__(layer_size=layer_size, device_num=device_num, **kwargs)
        self.initial_state = self.sess.run(self.lstm.zero_state(1, tf.float32))

    def network(self, inputs: tf.Tensor, reuse=False) -> tf.Tensor:
        # inputs = tf.layers.dense(inputs, self.layer_size)  # TODO: this should be unnecessary
        # inputs = tf.reshape(inputs, [-1, self.seq_len, self.layer_size])
        inputs = tf.split(inputs, self.seq_len, axis=1)
        s = self.S
        for x in inputs:
            x = tf.squeeze(x, axis=1)
            outputs = NetworkOutput(*self.lstm(x, s))
        return outputs

    def state_feed(self, states):
        if not isinstance(states, LSTMStateTuple):
            assert isinstance(states, list)
            states = LSTMStateTuple(*states)
        return dict(zip(self.S, states))

    def train_step(self, step: Step, feed_dict: dict = None) -> TrainStep:
        assert np.shape(step.s)[1:] == np.shape(self.initial_state)
        if feed_dict is None:
            feed_dict = {**self.state_feed(step.s),
                         **{self.O1: step.o1,
                            self.A: step.a,
                            self.R: np.array(step.r) * self.reward_scale,
                            self.O2: step.o2,
                            self.T: step.t}}
        return super().train_step(step, feed_dict)

    def get_actions(self, o: ArrayLike, s: ArrayLike, sample: bool = True) -> \
            Tuple[np.ndarray, LSTMStateTuple]:
        assert np.shape(o)[0] == 1
        assert np.shape(s) == np.shape(self.initial_state)
        feed_dict = {**{self.O1: [o]}, **self.state_feed(s)}
        A = self.A_sampled1 if sample else self.A_max_likelihood
        return self.sess.run([A[0], self.S_new], feed_dict)
