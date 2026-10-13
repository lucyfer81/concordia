# Copyright 2026 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Wrapper enforcing a hard budget on attempts to a language model."""

from collections.abc import Collection, Mapping, Sequence
import threading
from typing import Any, override

from concordia.language_model import language_model


class BudgetExhaustedError(Exception):
  """Raised when the attempt budget of a BudgetGuardLanguageModel is spent."""


class BudgetGuardLanguageModel(language_model.LanguageModel):
  """Wraps an underlying language model with a hard attempt budget.

  Each sample_text or sample_choice call consumes one unit of budget before
  the underlying model is invoked. Once the budget is exhausted no further
  calls reach the underlying model and BudgetExhaustedError is raised instead.
  Attempts that fail (i.e. the underlying model raises) still consume budget,
  while raising BudgetExhaustedError itself does not.
  """

  def __init__(
      self,
      model: language_model.LanguageModel,
      *,
      total_attempt_limit: int,
      used_attempts: int = 0,
  ) -> None:
    """Wrap the underlying language model with a hard attempt budget.

    Args:
      model: A language model to wrap.
      total_attempt_limit: the maximum total number of attempts allowed.
      used_attempts: budget already consumed, e.g. restored from a checkpoint.
    """
    self._model = model
    self._total_attempt_limit = total_attempt_limit
    self._used_attempts = used_attempts
    self._lock = threading.Lock()

  @property
  def used_attempts(self) -> int:
    """Number of budget units consumed so far."""
    return self._used_attempts

  @property
  def total_attempt_limit(self) -> int:
    """Maximum total number of attempts allowed."""
    return self._total_attempt_limit

  def restore(self, used_attempts: int, total_attempt_limit: int) -> None:
    """Restores the budget state, e.g. from a checkpoint.

    Args:
      used_attempts: budget consumed so far.
      total_attempt_limit: the maximum total number of attempts allowed.
    """
    with self._lock:
      self._used_attempts = used_attempts
      self._total_attempt_limit = total_attempt_limit

  def _acquire(self) -> None:
    """Consumes one unit of budget or raises if the budget is spent."""
    with self._lock:
      if self._used_attempts >= self._total_attempt_limit:
        raise BudgetExhaustedError(
            f'Attempt budget of {self._total_attempt_limit} exhausted with'
            f' {self._used_attempts} attempts already used.'
        )
      self._used_attempts += 1

  @override
  def sample_text(
      self,
      prompt: str,
      *,
      max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
      terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
      temperature: float = language_model.DEFAULT_TEMPERATURE,
      top_p: float = language_model.DEFAULT_TOP_P,
      top_k: int = language_model.DEFAULT_TOP_K,
      timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
      seed: int | None = None,
  ) -> str:
    """Samples text from the model.

    Args:
      prompt: the initial text to condition on.
      max_tokens: the maximum number of tokens in the response.
      terminators: the response will be terminated before any of these
        characters.
      temperature: temperature for the model.
      top_p: filters tokens based on cumulative probability, considering the
        most probable tokens until the sum of their probabilities reaches top_p.
      top_k: filters tokens by selecting the top_k most probable tokens.
      timeout: timeout for the request.
      seed: optional seed for the sampling. If None a random seed will be used.

    Returns:
      The sampled response (i.e. does not include the prompt).

    Raises:
      BudgetExhaustedError: if the attempt budget has been spent. The
        underlying model is not called in that case.
    """
    self._acquire()
    return self._model.sample_text(
        prompt,
        max_tokens=max_tokens,
        terminators=terminators,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        timeout=timeout,
        seed=seed,
    )

  @override
  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Mapping[str, Any]]:
    """Samples a response from those available.

    Args:
      prompt: the initial text to condition on.
      responses: the responses to score.
      seed: optional seed for the sampling. If None a random seed will be used.

    Returns:
      (index, response, info). The index of the sampled response, the sampled
      response, and some info about the sampling process.

    Raises:
      BudgetExhaustedError: if the attempt budget has been spent. The
        underlying model is not called in that case.
    """
    self._acquire()
    return self._model.sample_choice(prompt, responses, seed=seed)
