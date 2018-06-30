from pathlib import Path

import click
import numpy as np
import tensorflow as tf
from gym.wrappers import TimeLimit

from environments.hindsight_wrapper import PickAndPlaceHindsightWrapper
from environments.multi_task import MultiTaskEnv
from sac.networks import LstmAgent
from sac.networks import MlpAgent
from sac.train import MultiTaskHindsightTrainer
from scripts.pick_and_place import mutate_xml, put_in_xml_setter, parse_double


@click.command()
@click.option('--seed', default=0, type=int)
@click.option('--device-num', default=0, type=int)
@click.option('--relu', 'activation', flag_value=tf.nn.relu, default=True)
@click.option('--n-layers', default=3, type=int)
@click.option('--layer-size', default=256, type=int)
@click.option('--learning-rate', default=1e-4, type=float)
@click.option('--buffer-size', default=1e5, type=int)
@click.option('--num-train-steps', default=4, type=int)
@click.option('--steps-per-action', default=200, type=int)
@click.option('--batch-size', default=32, type=int)
@click.option('--reward-scale', default=7e3, type=float)
@click.option('--max-steps', default=200, type=int)
@click.option('--n-goals', default=1, type=int)
@click.option('--goal-scale', default=.1, type=float)
@click.option('--grad-clip', default=2e4, type=float)
@click.option('--logdir', default=None, type=str)
@click.option('--save-path', default=None, type=str)
@click.option('--load-path', default=None, type=str)
@click.option('--render-freq', default=0, type=int)
@click.option('--render', is_flag=True)
@click.option('--record-freq', type=int, default=0)
@click.option('--record-path', type=Path)
@click.option('--image-dims', type=str, callback=parse_double)
@click.option('--record', is_flag=True)
@click.option('--eval', is_flag=True)
@click.option('--no-qvel', 'obs_type', flag_value='no-qvel')
@click.option('--add-base-qvel', 'obs_type', flag_value='base-qvel', default=True)
@click.option('--set-xml', multiple=True, callback=put_in_xml_setter)
@click.option(
    '--use-dof',
    multiple=True,
    default=[
        'slide_x', 'slide_y', 'arm_lift_joint', 'arm_flex_joint', 'wrist_roll_joint',
        'hand_l_proximal_joint', 'hand_r_proximal_joint'
    ])
def cli(max_steps, seed, device_num, buffer_size, activation,
        n_layers, layer_size, learning_rate, reward_scale, grad_clip, batch_size,
        num_train_steps, steps_per_action, logdir, save_path, load_path,
        n_goals, eval, goal_scale, set_xml, use_dof, obs_type,
        render_freq, render, record, record_path, record_freq, image_dims):
    xml_filepath = Path(Path(__file__).parent.parent, 'environments', 'models', 'world.xml')
    if render and not render_freq:
        render_freq = 20
    with mutate_xml(
            changes=set_xml, dofs=use_dof, xml_filepath=xml_filepath) as temp_path:
        env = PickAndPlaceHindsightWrapper(
            env=TimeLimit(
                max_episode_steps=max_steps,
                env=MultiTaskEnv(
                    goal_scale=goal_scale,
                    xml_filepath=temp_path,
                    steps_per_action=steps_per_action,
                    obs_type=obs_type,
                    render_freq=render_freq,
                    record=record,
                    record_path=record_path,
                    record_freq=record_freq,
                    image_dimensions=image_dims,
                )))
    MultiTaskHindsightTrainer(
        env=env,
        base_agent=MlpAgent,
        seq_len=None,
        seed=seed,
        device_num=device_num,
        n_goals=n_goals,
        buffer_size=buffer_size,
        activation=activation,
        n_layers=n_layers,
        layer_size=layer_size,
        learning_rate=learning_rate,
        reward_scale=reward_scale,
        grad_clip=grad_clip if grad_clip > 0 else None,
        batch_size=batch_size,
        num_train_steps=num_train_steps,
        logdir=logdir,
        save_path=save_path,
        load_path=load_path,
        render=False,  # because render is handled inside env
        evaluation=eval,
    )


if __name__ == '__main__':
    cli()
