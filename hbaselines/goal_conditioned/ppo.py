"""Base goal-conditioned hierarchical policy."""
import tensorflow as tf
import numpy as np
from copy import deepcopy

from hbaselines.base_policies import ActorCriticPolicy
from hbaselines.fcnet.ppo import FeedForwardPolicy
from hbaselines.utils.reward_fns import negative_distance
from hbaselines.utils.env_util import get_meta_ac_space, get_state_indices


class GoalConditionedPolicy(ActorCriticPolicy):
    """Goal-conditioned hierarchical reinforcement learning model.

    Attributes
    ----------
    meta_period : int
        meta-policy action period
    intrinsic_reward_type : str
        the reward function to be used by the worker. Must be one of:

        * "negative_distance": the negative two norm between the states and
          desired absolute or relative goals.
        * "scaled_negative_distance": similar to the negative distance reward
          where the states, goals, and next states are scaled by the inverse of
          the action space of the manager policy
        * "non_negative_distance": the negative two norm between the states and
          desired absolute or relative goals offset by the maximum goal space
          (to ensure non-negativity)
        * "scaled_non_negative_distance": similar to the non-negative distance
          reward where the states, goals, and next states are scaled by the
          inverse of the action space of the manager policy
        * "exp_negative_distance": equal to exp(-negative_distance^2). The
          result is a reward between 0 and 1. This is useful for policies that
          terminate early.
        * "scaled_exp_negative_distance": similar to the previous worker reward
          type but with states, actions, and next states that are scaled.
    intrinsic_reward_scale : float
        the value that the intrinsic reward should be scaled by
    relative_goals : bool
        specifies whether the goal issued by the higher-level policies is meant
        to be a relative or absolute goal, i.e. specific state or change in
        state
    off_policy_corrections : bool
        whether to use off-policy corrections during the update procedure. See:
        https://arxiv.org/abs/1805.08296.
    hindsight : bool
        whether to use hindsight action and goal transitions, as well as
        subgoal testing. See: https://arxiv.org/abs/1712.00948
    subgoal_testing_rate : float
        rate at which the original (non-hindsight) sample is stored in the
        replay buffer as well. Used only if `hindsight` is set to True.
    connected_gradients : bool
        whether to use the connected gradient update actor update procedure
        to the higher-level policy. See: https://arxiv.org/abs/1912.02368v1
    cg_weights : float
        weights for the gradients of the loss of the lower-level policies with
        respect to the parameters of the higher-level policies. Only used if
        `connected_gradients` is set to True.
    policy : list of hbaselines.base_policies.ActorCriticPolicy
        a list of policy object for each level in the hierarchy, order from
        highest to lowest level policy
    goal_indices : list of int
        the state indices for the intrinsic rewards
    intrinsic_reward_fn : function
        reward function for the lower-level policies
    """

    def __init__(self,
                 sess,
                 ob_space,
                 ac_space,
                 co_space,
                 verbose,
                 model_params,
                 num_levels,
                 meta_period,
                 intrinsic_reward_type,
                 intrinsic_reward_scale,
                 relative_goals,
                 off_policy_corrections,
                 hindsight,
                 subgoal_testing_rate,
                 connected_gradients,
                 cg_weights,
                 scope=None,
                 env_name="",
                 num_envs=1,
                 additional_params=None):
        """Instantiate the goal-conditioned hierarchical policy.

        Parameters
        ----------
        sess : tf.compat.v1.Session
            the current TensorFlow session
        ob_space : gym.spaces.*
            the observation space of the environment
        ac_space : gym.spaces.*
            the action space of the environment
        co_space : gym.spaces.*
            the context space of the environment
        verbose : int
            the verbosity level: 0 none, 1 training information, 2 tensorflow
            debug
        num_levels : int
            number of levels within the hierarchy. Must be greater than 1. Two
            levels correspond to a Manager/Worker paradigm.
        meta_period : int
            meta-policy action period
        intrinsic_reward_type : str
            the reward function to be used by the worker. Must be one of:

            * "negative_distance": the negative two norm between the states and
              desired absolute or relative goals.
            * "scaled_negative_distance": similar to the negative distance
              reward where the states, goals, and next states are scaled by the
              inverse of the action space of the manager policy
            * "non_negative_distance": the negative two norm between the states
              and desired absolute or relative goals offset by the maximum goal
              space (to ensure non-negativity)
            * "scaled_non_negative_distance": similar to the non-negative
              distance reward where the states, goals, and next states are
              scaled by the inverse of the action space of the manager policy
            * "exp_negative_distance": equal to exp(-negative_distance^2). The
              result is a reward between 0 and 1. This is useful for policies
              that terminate early.
            * "scaled_exp_negative_distance": similar to the previous worker
              reward type but with states, actions, and next states that are
              scaled.
        intrinsic_reward_scale : float
            the value that the intrinsic reward should be scaled by
        relative_goals : bool
            specifies whether the goal issued by the higher-level policies is
            meant to be a relative or absolute goal, i.e. specific state or
            change in state
        off_policy_corrections : bool
            whether to use off-policy corrections during the update procedure.
            See: https://arxiv.org/abs/1805.08296
        hindsight : bool
            whether to include hindsight action and goal transitions in the
            replay buffer. See: https://arxiv.org/abs/1712.00948
        subgoal_testing_rate : float
            rate at which the original (non-hindsight) sample is stored in the
            replay buffer as well. Used only if `hindsight` is set to True.
        connected_gradients : bool
            whether to use the connected gradient update actor update procedure
            to the higher-level policy. See: https://arxiv.org/abs/1912.02368v1
        cg_weights : float
            weights for the gradients of the loss of the lower-level policies
            with respect to the parameters of the higher-level policies. Only
            used if `connected_gradients` is set to True.
        additional_params : dict
            additional algorithm-specific policy parameters. Used internally by
            the class when instantiating other (child) policies.
        """
        meta_policy = FeedForwardPolicy
        worker_policy = FeedForwardPolicy

        super(GoalConditionedPolicy, self).__init__(
            sess=sess,
            ob_space=ob_space,
            ac_space=ac_space,
            co_space=co_space,
            verbose=verbose,
            model_params=model_params,
        )

        assert num_levels >= 2, "num_levels must be greater than or equal to 2"

        self.num_levels = num_levels
        self.meta_period = meta_period
        self.intrinsic_reward_type = intrinsic_reward_type
        self.intrinsic_reward_scale = intrinsic_reward_scale
        self.relative_goals = relative_goals
        self.off_policy_corrections = off_policy_corrections
        self.hindsight = hindsight
        self.subgoal_testing_rate = subgoal_testing_rate
        self.connected_gradients = connected_gradients
        self.cg_weights = cg_weights

        # Get the observation and action space of the higher level policies.
        meta_ac_space = get_meta_ac_space(
            ob_space=ob_space,
            relative_goals=relative_goals,
            env_name=env_name,
        )

        # =================================================================== #
        # Step 1: Create the policies for the individual levels.              #
        # =================================================================== #

        self.policy = []

        # The policies are ordered from the highest level to lowest level
        # policies in the hierarchy.
        for i in range(num_levels):
            # Determine the appropriate parameters to use for the policy in the
            # current level.
            policy_fn = meta_policy if i < (num_levels - 1) else worker_policy
            ac_space_i = meta_ac_space if i < (num_levels - 1) else ac_space
            co_space_i = co_space if i == 0 else meta_ac_space
            ob_space_i = ob_space

            # The policies are ordered from the highest level to lowest level
            # policies in the hierarchy.
            with tf.compat.v1.variable_scope("level_{}".format(i)):
                # Compute the scope name based on any outer scope term.
                scope_i = "level_{}".format(i)
                if scope is not None:
                    scope_i = "{}/{}".format(scope, scope_i)

                # Create the next policy.
                self.policy.append(policy_fn(
                    sess=sess,
                    ob_space=ob_space_i,
                    ac_space=ac_space_i,
                    co_space=co_space_i,
                    verbose=verbose,
                    model_params=model_params,
                    scope=scope_i,
                    **(additional_params or {}),
                ))

        # =================================================================== #
        # Step 2: Create attributes for the replay buffer.                    #
        # =================================================================== #

        # current action by the meta-level policies
        self._meta_action = [[None for _ in range(num_levels - 1)]
                             for _ in range(num_envs)]

        # a list of all the actions performed by each level in the hierarchy,
        # ordered from highest to lowest level policy. A separate element is
        # used for each environment.
        self._actions = [[[] for _ in range(self.num_levels)]
                         for _ in range(num_envs)]

        # a list of the rewards (intrinsic or other) experienced by every level
        # in the hierarchy, ordered from highest to lowest level policy. A
        # separate element is used for each environment.
        self._rewards = [[[0]] + [[] for _ in range(self.num_levels - 1)]
                         for _ in range(num_envs)]

        # a list of observations that stretch as long as the dilated horizon
        # chosen for the highest level policy. A separate element is used for
        # each environment.
        self._observations = [[] for _ in range(num_envs)]

        # the first and last contextual term. A separate element is used for
        # each environment.
        self._contexts = [[] for _ in range(num_envs)]

        # a list of done masks at every time step. A separate element is used
        # for each environment.
        self._dones = [[] for _ in range(num_envs)]

        # Collect the state indices for the intrinsic rewards.
        self.goal_indices = get_state_indices(ob_space, env_name)

        # Define the intrinsic reward function.
        if intrinsic_reward_type in ["negative_distance",
                                     "scaled_negative_distance",
                                     "non_negative_distance",
                                     "scaled_non_negative_distance",
                                     "exp_negative_distance",
                                     "scaled_exp_negative_distance"]:
            # Offset the distance measure by the maximum possible distance to
            # ensure non-negativity.
            if "non_negative" in intrinsic_reward_type:
                offset = np.sqrt(np.sum(np.square(
                    meta_ac_space.high - meta_ac_space.low), -1))
            else:
                offset = 0

            # Scale the outputs from the state by the meta-action space if you
            # wish to scale the worker reward.
            if intrinsic_reward_type.startswith("scaled"):
                scale = 0.5 * (meta_ac_space.high - meta_ac_space.low)
            else:
                scale = 1

            def intrinsic_reward_fn(states, goals, next_states):
                return negative_distance(
                    states=states[self.goal_indices] / scale,
                    goals=goals / scale,
                    next_states=next_states[self.goal_indices] / scale,
                    relative_context=relative_goals,
                    offset=0.0
                ) + offset

            # Perform the exponential and squashing operations to keep the
            # intrinsic reward between 0 and 1.
            if "exp" in intrinsic_reward_type:
                def exp_intrinsic_reward_fn(states, goals, next_states):
                    return np.exp(
                        -intrinsic_reward_fn(states, goals, next_states) ** 2)
                self.intrinsic_reward_fn = exp_intrinsic_reward_fn
            else:
                self.intrinsic_reward_fn = intrinsic_reward_fn
        else:
            raise ValueError("Unknown intrinsic reward type: {}".format(
                intrinsic_reward_type))

        # =================================================================== #
        # Step 3: Create algorithm-specific features.                         #
        # =================================================================== #

        # a fixed goal transition function for the meta-actions in between meta
        # periods. This is used when relative_goals is set to True in order to
        # maintain a fixed absolute position of the goal.
        if relative_goals:
            def goal_transition_fn(obs0, goal, obs1):
                return obs0 + goal - obs1
        else:
            def goal_transition_fn(obs0, goal, obs1):
                return goal
        self.goal_transition_fn = goal_transition_fn

        if self.connected_gradients:
            with tf.compat.v1.variable_scope(scope):
                self._setup_connected_gradients()

    def initialize(self):
        """See parent class.

        This method calls the initialization methods of the policies at every
        level of the hierarchy.
        """
        for i in range(self.num_levels):
            self.policy[i].initialize()

    def update(self, update_actor=True, **kwargs):
        """Perform a gradient update step.

        This is done both at every level of the hierarchy.

        The kwargs argument for this method contains two additional terms:

        * update_meta (bool): specifies whether to perform a gradient update
          step for the meta-policies
        * update_meta_actor (bool): similar to the `update_policy` term, but
          for the meta-policy. Note that, if `update_meta` is set to False,
          this term is void.

        **Note**; The target update soft updates for all policies occur at the
        same frequency as their respective actor update frequencies.

        Parameters
        ----------
        update_actor : bool
            specifies whether to update the actor policy. The critic policy is
            still updated if this value is set to False.

        Returns
        -------
         ([float, float], [float, float])
            the critic loss for every policy in the hierarchy
        (float, float)
            the actor loss for every policy in the hierarchy
        """
        for i in range(self.num_levels):
            self.policy[i].update()

    def get_action(self, obs, context, apply_noise, random_actions, env_num=0):
        """See parent class."""
        # Loop through the policies in the hierarchy.
        for i in range(self.num_levels - 1):
            if self._update_meta(i, env_num):
                context_i = context if i == 0 \
                    else self._meta_action[env_num][i - 1]

                # Update the meta action based on the output from the policy if
                # the time period requires is.
                self._meta_action[env_num][i] = self.policy[i].get_action(
                    obs, context_i, apply_noise, random_actions)
            else:
                # Update the meta-action in accordance with a fixed transition
                # function.
                self._meta_action[env_num][i] = self.goal_transition_fn(
                    obs0=np.array(
                        [self._observations[env_num][-1][self.goal_indices]]),
                    goal=self._meta_action[env_num][i],
                    obs1=obs[:, self.goal_indices]
                )

        # Return the action to be performed within the environment (i.e. the
        # action by the lowest level policy).
        action = self.policy[-1].get_action(
            obs, self._meta_action[env_num][-1], apply_noise, random_actions)

        return action

    def store_transition(self, obs0, context0, action, reward, obs1, context1,
                         done, is_final_step, env_num=0, evaluate=False):
        """See parent class."""
        # the time since the most recent sample began collecting step samples
        t_start = len(self._observations[env_num])

        # Flatten the observations.
        obs0 = obs0.flatten()
        obs1 = obs1.flatten()

        for i in range(1, self.num_levels):
            # Actions and intrinsic rewards for the high-level policies are
            # only updated when the action is recomputed by the graph.
            if t_start % self.meta_period ** (i-1) == 0:
                self._rewards[env_num][-i].append(0)
                self._actions[env_num][-i-1].append(
                    self._meta_action[env_num][-i].flatten())

            # Compute the intrinsic rewards and append them to the list of
            # rewards.
            self._rewards[env_num][-i][-1] += \
                self.intrinsic_reward_scale / self.meta_period ** (i-1) * \
                self.intrinsic_reward_fn(
                    states=obs0,
                    goals=self._meta_action[env_num][-i].flatten(),
                    next_states=obs1
                )

        # The highest level policy receives the sum of environmental rewards.
        self._rewards[env_num][0][0] += reward

        # The lowest level policy's actions are received from the algorithm.
        self._actions[env_num][-1].append(action)

        # Add the environmental observations and contextual terms to their
        # respective lists.
        self._observations[env_num].append(obs0)
        if t_start == 0:
            self._contexts[env_num].append(context0)

        # Modify the done mask in accordance with the TD3 algorithm. Done masks
        # that correspond to the final step are set to False.
        self._dones[env_num].append(done and not is_final_step)

        # Add a sample to the replay buffer.
        if len(self._observations[env_num]) == \
                self.meta_period ** (self.num_levels - 1) or done:
            # Add the last observation and context.
            self._observations[env_num].append(obs1)
            self._contexts[env_num].append(context1)

            # Compute the current state goals to add to the final observation.
            for i in range(self.num_levels - 1):
                self._actions[env_num][i].append(self.goal_transition_fn(
                    obs0=obs0[self.goal_indices],
                    goal=self._meta_action[env_num][i],
                    obs1=obs1[self.goal_indices]
                ).flatten())

            # Store a sample in the replay buffer.
            self.replay_buffer.add(
                obs_t=self._observations[env_num],
                context_t=self._contexts[env_num],
                action_t=self._actions[env_num],
                reward_t=self._rewards[env_num],
                done_t=self._dones[env_num],
            )

            # Clear the memory that has been stored in the replay buffer.
            self.clear_memory(env_num)

    def _update_meta(self, level, env_num):
        """Determine whether a meta-policy should update its action.

        This is done by checking the length of the observation lists that are
        passed to the replay buffer, which are cleared whenever the highest
        level meta-period has been met or the environment has been reset.

        Parameters
        ----------
        level : int
            the level of the policy
        env_num : int
            the environment number. Used to handle situations when multiple
            parallel environments are being used.

        Returns
        -------
        bool
            True if the action should be updated by the meta-policy at the
            given level
        """
        return len(self._observations[env_num]) % \
            (self.meta_period ** (self.num_levels - level - 1)) == 0

    def clear_memory(self, env_num):
        """Clear internal memory that is used by the replay buffer."""
        self._actions[env_num] = [[] for _ in range(self.num_levels)]
        self._rewards[env_num] = \
            [[0]] + [[] for _ in range(self.num_levels - 1)]
        self._observations[env_num] = []
        self._contexts[env_num] = []
        self._dones[env_num] = []

    def get_td_map(self):
        """See parent class."""
        td_map = {}
        for i in range(self.num_levels):
            td_map.update(self.policy[i].get_td_map())

        return td_map

    def _negative_reward_fn(self):
        """Return True if the intrinsic reward returns negative values.

        Intrinsic reward functions with negative rewards incentivize early
        terminations, which we attempt to mitigate in the training operation by
        preventing early terminations from return an expected return of 0.
        """
        return "exp" not in self.intrinsic_reward_type \
            and "non" not in self.intrinsic_reward_type

    # ======================================================================= #
    #                       Auxiliary methods for HIRO                        #
    # ======================================================================= #

    def _sample_best_meta_action(self,
                                 meta_obs0,
                                 meta_obs1,
                                 meta_action,
                                 worker_obses,
                                 worker_actions,
                                 k=10):
        """Return meta-actions that approximately maximize low-level log-probs.

        Parameters
        ----------
        meta_obs0 : array_like
            (batch_size, m_obs_dim) matrix of meta observations
        meta_obs1 : array_like
            (batch_size, m_obs_dim) matrix of next time step meta observations
        meta_action : array_like
            (batch_size, m_ac_dim) matrix of meta actions
        worker_obses : array_like
            (batch_size, w_obs_dim, meta_period+1) matrix of current Worker
            state observations
        worker_actions : array_like
            (batch_size, w_ac_dim, meta_period) matrix of current Worker
            environmental actions
        k : int, optional
            number of goals returned, excluding the initial goal and the mean
            value

        Returns
        -------
        array_like
            (batch_size, m_ac_dim) matrix of most likely meta actions
        """
        batch_size, goal_dim = meta_action.shape

        # Collect several samples of potentially optimal goals.
        sampled_actions = self._sample(meta_obs0, meta_obs1, meta_action, k)
        assert sampled_actions.shape == (batch_size, goal_dim, k)

        # Compute the fitness of each candidate goal. The fitness is the sum of
        # the log-probabilities of each action for the given goal.
        fitness = self._log_probs(
            sampled_actions, worker_obses, worker_actions)
        assert fitness.shape == (batch_size, k)

        # For each sample, choose the meta action that maximizes the fitness.
        indx = np.argmax(fitness, 1)
        best_goals = np.asarray(
            [sampled_actions[i, :, indx[i]] for i in range(batch_size)])

        return best_goals

    def _sample(self, meta_obs0, meta_obs1, meta_action, num_samples, sc=0.5):
        """Sample different goals.

        The goals are sampled as follows:

        * The first num_samples-2 goals are acquired from a random Gaussian
          distribution centered at s_{t+c} - s_t.
        * The second to last goal is s_{t+c} - s_t.
        * The last goal is the originally sampled goal g_t.

        Parameters
        ----------
        meta_obs0 : array_like
            (batch_size, m_obs_dim) matrix of meta observations
        meta_obs1 : array_like
            (batch_size, m_obs_dim) matrix of next time step meta observations
        meta_action : array_like
            (batch_size, m_ac_dim) matrix of meta actions
        num_samples : int
            number of samples
        sc : float
            scaling factor for the normal distribution.

        Returns
        -------
        array_like
            (batch_size, goal_dim, num_samples) matrix of sampled goals

        Helps
        -----
        * _sample_best_meta_action(self)
        """
        batch_size, goal_dim = meta_action.shape
        goal_space = self.policy[0].ac_space
        spec_range = goal_space.high - goal_space.low
        random_samples = num_samples - 2

        # Compute the mean and std for the Gaussian distribution to sample
        # from, and well as the maxima and minima.
        loc = meta_obs1[:, self.goal_indices] - meta_obs0[:, self.goal_indices]
        scale = [sc * spec_range / 2]
        minimum, maximum = [goal_space.low], [goal_space.high]

        new_loc = np.zeros((batch_size, goal_dim, random_samples))
        new_scale = np.zeros((batch_size, goal_dim, random_samples))
        for i in range(random_samples):
            new_loc[:, :, i] = loc
            new_scale[:, :, i] = scale

        new_minimum = np.zeros((batch_size, goal_dim, num_samples))
        new_maximum = np.zeros((batch_size, goal_dim, num_samples))
        for i in range(num_samples):
            new_minimum[:, :, i] = minimum
            new_maximum[:, :, i] = maximum

        # Generate random samples for the above distribution.
        normal_samples = np.random.normal(
            size=(random_samples * batch_size * goal_dim))
        normal_samples = normal_samples.reshape(
            (batch_size, goal_dim, random_samples))

        samples = np.zeros((batch_size, goal_dim, num_samples))
        samples[:, :, :-2] = new_loc + normal_samples * new_scale
        samples[:, :, -2] = loc
        samples[:, :, -1] = meta_action

        # Clip the values based on the meta action space range.
        samples = np.minimum(np.maximum(samples, new_minimum), new_maximum)

        return samples

    def _log_probs(self, meta_actions, worker_obses, worker_actions):
        """Calculate the log probability of the next goal by the meta-policies.

        Parameters
        ----------
        meta_actions : array_like
            (batch_size, m_ac_dim, num_samples) matrix of candidate higher-
            level policy actions
        worker_obses : array_like
            (batch_size, w_obs_dim, meta_period + 1) matrix of lower-level
            policy observations
        worker_actions : array_like
            (batch_size, w_ac_dim, meta_period) list of lower-level policy
            actions

        Returns
        -------
        array_like
            (batch_size, num_samples) fitness associated with every state /
            action / goal pair

        Helps
        -----
        * _sample_best_meta_action(self):
        """
        raise NotImplementedError

    # ======================================================================= #
    #                       Auxiliary methods for HAC                         #
    # ======================================================================= #

    def _hindsight_actions_goals(self, initial_observations, initial_rewards):
        """Calculate hindsight goal and action transitions.

        These are then stored in the replay buffer along with the original
        (non-hindsight) sample.

        See the README at the front page of this repository for an in-depth
        description of this procedure.

        Parameters
        ----------
        initial_observations : array_like
            the original worker observations with the non-hindsight goals
            appended to them
        initial_rewards : array_like
            the original intrinsic rewards

        Returns
        -------
        array_like
            the goal at every step in hindsight
        array_like
            the modified intrinsic rewards taking into account the hindsight
            goals

        Helps
        -----
        * store_transition(self):
        """
        new_goals = []
        observations = deepcopy(initial_observations)
        rewards = deepcopy(initial_rewards)
        hindsight_goal = 0 if self.relative_goals \
            else observations[-1][self.goal_indices]
        obs_tp1 = observations[-1]

        for i in range(1, len(observations) + 1):
            obs_t = observations[-i]

            # Calculate the hindsight goal in using relative goals.
            # If not, the hindsight goal is simply a subset of the
            # final state observation.
            if self.relative_goals:
                hindsight_goal += \
                    obs_tp1[self.goal_indices] - obs_t[self.goal_indices]

            # Modify the Worker intrinsic rewards based on the new
            # hindsight goal.
            if i > 1:
                rewards[-(i - 1)] = self.intrinsic_reward_scale \
                    * self.intrinsic_reward_fn(obs_t, hindsight_goal, obs_tp1)

            obs_tp1 = deepcopy(obs_t)
            new_goals = [deepcopy(hindsight_goal)] + new_goals

        return new_goals, rewards

    # ======================================================================= #
    #                      Auxiliary methods for HRL-CG                       #
    # ======================================================================= #

    def _setup_connected_gradients(self):
        """Create the connected gradients meta-policy optimizer."""
        raise NotImplementedError

    def _connected_gradients_update(self,
                                    obs0,
                                    actions,
                                    rewards,
                                    obs1,
                                    terminals1,
                                    update_actor=True):
        """Perform the gradient update procedure for the HRL-CG algorithm.

        This procedure is similar to update_from_batch, expect it runs the
        self.cg_optimizer operation instead of the policy object's optimizer,
        and utilizes some information from the worker samples as well.

        Parameters
        ----------
        obs0 : list of array_like
            (batch_size, obs_dim) matrix of observations for every level in the
            hierarchy
        actions : list of array_like
            (batch_size, ac_dim) matrix of actions for every level in the
            hierarchy
        obs1 : list of array_like
            (batch_size, obs_dim) matrix of next step observations for every
            level in the hierarchy
        rewards : list of array_like
            (batch_size,) vector of rewards for every level in the hierarchy
        terminals1 : list of numpy bool
            (batch_size,) vector of done masks for every level in the hierarchy
        update_actor : bool
            specifies whether to update the actor policy of the meta policy.
            The critic policy is still updated if this value is set to False.

        Returns
        -------
        [float, float]
            meta-policy critic loss
        float
            meta-policy actor loss
        """
        raise NotImplementedError