# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 philanthrope

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.


import torch
import numpy as np
import bittensor as bt
from bittensor import Synapse
from typing import Union, List
from functools import partial

from storage.validator.verify import (
    verify_store_with_seed,
    verify_challenge_with_seed,
    verify_retrieve_with_seed,
)
from storage.validator.bonding import update_statistics, get_tier_factor
from storage.validator.event import EventSchema

from storage.constants import (
    STORE_FAILURE_REWARD,
    RETRIEVAL_FAILURE_REWARD,
    CHALLENGE_FAILURE_REWARD,
)
from storage.protocol import Store, Retrieve, Challenge


def adjusted_sigmoid(x, steepness=1, shift=0):
    """
    Adjusted sigmoid function.

    This function is a modified version of the sigmoid function that is shifted
    to the right by a certain amount.
    """
    return 1 / (1 + np.exp(-steepness * (x - shift)))


def adjusted_sigmoid_inverse(x, steepness=1, shift=0):
    """
    Inverse of the adjusted sigmoid function.

    This function is a modified version of the sigmoid function that is shifted to
    the right by a certain amount but inverted such that low completion times are
    rewarded and high completions dimes are punished.
    """
    return 1 / (1 + np.exp(steepness * (x - shift)))


def calculate_sigmoid_params(timeout):
    """
    Calculate sigmoid parameters based on the timeout value.

    Args:
    - timeout (float): The current timeout value.

    Returns:
    - tuple: A tuple containing the 'steepness' and 'shift' values for the current timeout.
    """
    base_timeout = 1
    base_steepness = 5
    base_shift = 0.6

    # Calculate the ratio of the current timeout to the base timeout
    ratio = timeout / base_timeout

    # Calculate steepness and shift based on the pattern
    steepness = base_steepness / ratio
    shift = base_shift * ratio

    return steepness, shift


def get_sorted_response_times(uids, responses, timeout: float):
    """
    Sorts a list of axons based on their response times.

    This function pairs each uid with its corresponding axon's response time,
    and then sorts this list in ascending order. Lower response times are considered better.

    Args:
        uids (List[int]): List of unique identifiers for each axon.
        responses (List[Response]): List of Response objects corresponding to each axon.

    Returns:
        List[Tuple[int, float]]: A sorted list of tuples, where each tuple contains an axon's uid and its response time.

    Example:
        >>> get_sorted_response_times([1, 2, 3], [response1, response2, response3])
        [(2, 0.1), (1, 0.2), (3, 0.3)]
    """
    axon_times = [
        (
            uids[idx],
            response.dendrite.process_time
            if response.dendrite.process_time is not None
            else timeout,
        )
        for idx, response in enumerate(responses)
    ]
    # Sorting in ascending order since lower process time is better
    sorted_axon_times = sorted(axon_times, key=lambda x: x[1])
    bt.logging.debug(f"sorted_axon_times: {sorted_axon_times}")
    return sorted_axon_times


def sigmoid_normalize(process_times, timeout):
    # Center the completion times around 0 for effective sigmoid scaling
    centered_times = process_times - np.mean(process_times)

    # Calculate steepness and shift based on timeout
    steepness, shift = calculate_sigmoid_params(timeout)

    # Apply adjusted sigmoid function to scale the times
    return adjusted_sigmoid_inverse(centered_times, steepness, shift)


def min_max_normalize(times):
    """
    Normalizes the response times using Min-Max scaling.
    Args:
        times (List[float]): A list of response times.
    Returns:
        List[float]: Normalized response times scaled between 0 and 1.
    """
    if times == []:
        return []
    min_time = min(times)
    max_time = max(times)
    range_time = max_time - min_time
    if range_time == 0:
        # Avoid division by zero in case all times are the same
        return [0.5 for _ in times]
    return [(time - max_time) / range_time for time in times]


def scale_rewards(uids, responses, rewards, timeout: float, mode: str):
    """
    Scales the rewards for each axon based on their response times using `mode` normalization.
    Args:
        uids (List[int]): A list of unique identifiers for each axon.
        responses (List[Response]): A list of Response objects corresponding to each axon.
        rewards (List[float]): A list of initial reward values for each axon.
        timeout (float): The timeout value used for response time calculations.
        mode (str): The normalization mode to use. Can be either 'sigmoid' or 'minmax'.
    Returns:
        List[float]: A list of scaled rewards for each axon.
    """
    sorted_axon_times = get_sorted_response_times(uids, responses, timeout=timeout)

    # Extract only the process times
    process_times = [proc_time for _, proc_time in sorted_axon_times]

    # Normalize the response times
    normalized_times = (
        min_max_normalize(process_times)
        if mode == "minmax"
        else sigmoid_normalize(process_times, timeout)
    )

    # Create a dictionary mapping UIDs to normalized times
    uid_to_normalized_time = {
        uid: normalized_time
        for (uid, _), normalized_time in zip(sorted_axon_times, normalized_times)
    }
    bt.logging.debug(
        f"scale_rewards_by_{mode}() uid_to_normalized_time: {uid_to_normalized_time}"
    )
    # Scale the rewards with normalized times
    for i, uid in enumerate(uids):
        normalized_time_for_uid = uid_to_normalized_time[uid]
        rewards[i] += rewards[i] * normalized_time_for_uid
    bt.logging.debug(f"scale_rewards_by_{mode}() rewards: {rewards}")
    return rewards


def apply_reward_scores(
    self, uids, responses, rewards, timeout: float, mode: str = "sigmoid"
):
    """
    Adjusts the moving average scores for a set of UIDs based on their response times and reward values.

    This should reflect the distribution of axon response times (minmax norm)

    Parameters:
        uids (List[int]): A list of UIDs for which rewards are being applied.
        responses (List[Response]): A list of response objects received from the nodes.
        rewards (torch.FloatTensor): A tensor containing the computed reward values.
    """
    if mode not in ["sigmoid", "minmax"]:
        raise ValueError(f"Invalid mode: {mode}")

    if self.config.neuron.verbose:
        bt.logging.debug(f"Applying rewards: {rewards}")
        bt.logging.debug(f"Reward shape: {rewards.shape}")
        bt.logging.debug(f"UIDs: {uids}")

    scaled_rewards = scale_rewards(uids, responses, rewards, timeout=timeout, mode=mode)
    bt.logging.debug(f"apply_reward_scores() Scaled rewards: {scaled_rewards}")

    # Compute forward pass rewards
    # shape: [ metagraph.n ]
    scattered_rewards: torch.FloatTensor = self.moving_averaged_scores.scatter(
        0, torch.tensor(uids).to(self.device), scaled_rewards
    ).to(self.device)
    bt.logging.trace(f"Scattered rewards: {scattered_rewards}")

    # Update moving_averaged_scores with rewards produced by this step.
    # shape: [ metagraph.n ]
    alpha: float = self.config.neuron.moving_average_alpha
    self.moving_averaged_scores: torch.FloatTensor = alpha * scattered_rewards + (
        1 - alpha
    ) * self.moving_averaged_scores.to(self.device)
    bt.logging.trace(f"Updated moving avg scores: {self.moving_averaged_scores}")


async def create_reward_vector(
    self,
    synapse: Union[Store, Retrieve, Challenge],
    rewards: torch.FloatTensor,
    uids: List[int],
    responses: List[Synapse],
    event: EventSchema,
    callback: callable,
    fail_callback: callable,
):
    # Determine if the commitment is valid
    success = False
    if isinstance(synapse, Store):
        verify_fn = partial(
            verify_store_with_seed,
            b64_encrypted_data=synapse.encrypted_data,
            seed=synapse.seed,
        )
        task_type = "store"
        failure_reward = STORE_FAILURE_REWARD
    elif isinstance(synapse, Retrieve):
        verify_fn = partial(verify_retrieve_with_seed, seed=synapse.seed)
        task_type = "retrieve"
        failure_reward = RETRIEVAL_FAILURE_REWARD
    elif isinstance(synapse, Challenge):
        verify_fn = partial(verify_challenge_with_seed, seed=synapse.seed)
        task_type = "challenge"
        failure_reward = CHALLENGE_FAILURE_REWARD
    else:
        raise ValueError(f"Invalid synapse type: {type(synapse)}")

    for idx, (uid, response) in enumerate(zip(uids, responses)):
        # Verify the commitment
        hotkey = self.metagraph.hotkeys[uid]

        # Determine if the commitment is valid
        success = verify_fn(synapse=response)
        if success:
            bt.logging.debug(
                f"Successfully verified {synapse.__class__} commitment from UID: {uid} | hotkey: {hotkey}"
            )
            await callback(hotkey, idx, uid, response)
        else:
            bt.logging.error(
                f"create_reward_vector() Failed to verify store commitment from UID: {uid} | hotkey: {hotkey}"
            )
            fail_callback(uid)

        # Update the storage statistics
        await update_statistics(
            ss58_address=hotkey,
            success=success,
            task_type=task_type,
            database=self.database,
        )

        # Apply reward for this task
        tier_factor = await get_tier_factor(hotkey, self.database)
        rewards[idx] = 1.0 * tier_factor if success else failure_reward * tier_factor

        event.successful.append(success)
        event.uids.append(uid)
        event.completion_times.append(response.dendrite.process_time)
        event.task_status_messages.append(response.dendrite.status_message)
        event.task_status_codes.append(response.dendrite.status_code)
