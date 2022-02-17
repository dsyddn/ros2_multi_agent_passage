import rclpy
from typing import Dict, List
from evaluation_infrastructure.agent_util import get_uuids_fast
from evaluation_infrastructure.agent_start_goal import AgentStartGoal
from rclpy.publisher import Publisher
from rclpy.subscription import Subscription
from freyja_msgs.msg import ReferenceState, CurrentState
from functools import partial
import torch
import torch_geometric
from torch_geometric.nn.conv import MessagePassing
from scipy.spatial.transform import Rotation as R
import numpy as np


class AgentCentralizedGNNPassage(AgentStartGoal):

    _vel_pubs: Dict[str, Publisher] = {}
    _state_subs: Dict[str, Subscription] = {}
    _current_states: Dict[str, CurrentState] = {}
    model = None
    _current_side: R = R.from_euler("z", 0.0)

    def __init__(self):
        super().__init__()

        self.declare_parameter("model_path")
        self.model = torch.jit.load(self.get_parameter("model_path").value)
        self.declare_parameter("comm_range", 2.0)
        self.declare_parameter("max_v", 1.5)
        self.declare_parameter("max_a", 1.0)
        self.declare_parameter("goal_reached_dist", 0.25)

    def _update_current_state(self, uuid, msg):
        self._current_states[uuid] = msg

    def update_pubs_and_subs(self, controllable_agents: List[str]):
        for uuid in controllable_agents:
            if uuid not in self._vel_pubs:
                self._vel_pubs[uuid] = self.create_publisher(
                    ReferenceState,
                    f"/{uuid}/reference_state",
                    1,
                )
            if uuid not in self._state_subs:
                self._state_subs[uuid] = self.create_subscription(
                    CurrentState,
                    f"/{uuid}/current_state",
                    partial(self._update_current_state, uuid),
                    1,
                )

    def run_model(self, obs):
        comm_range = self.get_parameter("comm_range").value

        x = torch.cat(
            [obs["goal"] - obs["pos"], obs["pos"], obs["pos"] + obs["vel"]], dim=2
        )
        logits = self.model(obs["pos"], x, torch.tensor([comm_range]))

        actions = []
        for i in range(logits.shape[1]):
            action = self.sample_action_from_logit(logits[:, i])
            actions.append(action[0].numpy())

        return actions

    def sample_action_from_logit(self, logit):
        max_v = self.get_parameter("max_v").value

        mean, log_std = torch.chunk(logit, 2, dim=1)
        dist = torch.distributions.normal.Normal(mean, torch.exp(log_std))
        action = dist.sample()
        return torch.clamp(action, -max_v, max_v)

    def action_constrain_vel_acc(self, action, velocity):
        max_v = self.get_parameter("max_v").value
        max_a = self.get_parameter("max_a").value

        f = 4.0
        clipped_v = np.clip(action, -max_v, max_v)
        desired_a = (clipped_v - velocity) / (1 / f)
        possible_a = np.clip(desired_a, -max_a, max_a)
        possible_v = velocity + possible_a * (1 / f)
        return np.clip(possible_v, -max_v, max_v)

    def update_current_side(self):
        if list(self.start_poses.values())[0].position.y > 0.0:
            self._current_side = R.from_euler("z", np.pi)
        else:
            self._current_side = R.from_euler("z", 0.0)

    def step(
        self, controllable_agents: List[str], state_dict: Dict[str, Dict]
    ) -> Dict[str, bool]:
        self.update_pubs_and_subs(controllable_agents)
        self.update_current_side()

        if len(self._current_states) != len(controllable_agents):
            return {agent: False for agent in controllable_agents}

        obs = {"pos": [[]], "vel": [[]], "goal": [[]]}
        for uuid in controllable_agents:
            if uuid not in self._current_states:
                continue

            obs["goal"][0].append(
                self._current_side.apply(
                    [
                        self.goal_poses[uuid].position.x,
                        self.goal_poses[uuid].position.y,
                        0.0,
                    ]
                )[:2]
            )
            obs["pos"][0].append(
                self._current_side.apply(
                    [
                        self._current_states[uuid].state_vector[0],
                        self._current_states[uuid].state_vector[1],
                        0.0,
                    ]
                )[:2]
            )
            obs["vel"][0].append(
                self._current_side.apply(
                    [
                        self._current_states[uuid].state_vector[3],
                        self._current_states[uuid].state_vector[4],
                        0.0,
                    ]
                )[:2]
            )

        actions = self.run_model({k: torch.Tensor(v) for k, v in obs.items()})
        for uuid, action, vel in zip(controllable_agents, actions, obs["vel"][0]):
            if uuid not in self._vel_pubs:
                continue

            proc_action = self.action_constrain_vel_acc(action, vel)
            action_rot = self._current_side.apply(np.hstack([proc_action, [0]]))[
                :2
            ].astype(float)

            ref_state = ReferenceState()
            ref_state.vn = action_rot[0]
            ref_state.ve = action_rot[1]
            ref_state.yaw = np.pi / 2

            self._vel_pubs[uuid].publish(ref_state)

        dones = {}
        for i, agent in enumerate(controllable_agents):
            dist_goal = np.linalg.norm(obs["pos"][0][i] - obs["goal"][0][i])
            dones[agent] = dist_goal < self.get_parameter("goal_reached_dist").value
        return dones

    def get_controllable_agents(self) -> List[str]:
        """Override me. Return uuids of the agents we control"""
        return get_uuids_fast(self)


def main() -> None:
    rclpy.init()
    g = AgentCentralizedGNNPassage()
    rclpy.spin(g)
