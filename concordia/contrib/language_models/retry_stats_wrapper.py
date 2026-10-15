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

"""Wrapper counting API retries around a guard-and-retry model chain.

Spec section 13.3 requires the run summary to record retry counts in
addition to model-call attempts. The story enactment chain composes the
core ``RetryLanguageModel`` around a ``BudgetGuardLanguageModel``
(``Retry(Recorder(BudgetGuard(provider)))``), and neither core wrapper
exposes how many attempts were retries. This module assembles that chain
around the guard and adds the retry accounting: a per-request recorder
between the retry wrapper and the guard counts the attempts of the
logical request currently executing on the calling thread, and a request
that made ``n >= 1`` provider attempts accounts for ``n - 1`` API
retries.

Attempts are attributed through a thread-local counter, not through the
guard's shared cumulative total: ``EntityAgent`` executes component
``pre_act`` hooks concurrently, so several logical requests share one
guard, and the delta of a shared counter cannot attribute attempts to
the request that caused them. Tenacity retries synchronously inside the
request's own thread, so the thread-local count of one request never
includes another request's attempts.

The metric's contract (acceptance F5, recheck R3):

- a request that succeeds on its first attempt contributes 0 retries,
  also when other requests execute concurrently;
- a request that fails once and then succeeds contributes 1 retry, and
  a request that exhausts three attempts contributes 2;
- a request blocked by the budget before any attempt is made does not
  count as an initiated retry, and a retry that the budget blocks before
  it reaches the provider does not count either;
- output-parse re-asks (e.g. the tension-scoring retry issued by the
  drama director after an unparseable answer) are NEW logical requests,
  not API retries: they are counted under the model-attempt budget but
  never under this wrapper's retry count.

The counter is cumulative across resumes: ``restore`` seeds it with the
value persisted in the run's checkpoint envelope, mirroring
``BudgetGuardLanguageModel.restore``.
"""

from collections.abc import Collection
import threading
from typing import Any, Mapping, override, Sequence

from concordia.contrib.language_models import budget_wrapper
from concordia.language_model import language_model
from concordia.language_model import retry_wrapper


class _RequestAttemptRecorder(language_model.LanguageModel):
  """Counts the provider attempts of the request on the calling thread.

  Sits between the retry wrapper and the budget guard. Every call
  optimistically counts one attempt of the logical request running on
  the calling thread; a ``BudgetExhaustedError`` rolls the count back,
  because the guard raised it before any provider call was made.
  """

  def __init__(self, guard: budget_wrapper.BudgetGuardLanguageModel):
    self._guard = guard
    self._local = threading.local()

  def start_request(self) -> None:
    """Begins a new logical request with a fresh attempt count."""
    self._local.attempts = 0

  def take_attempts(self) -> int:
    """Returns the current request's attempt count and resets it."""
    attempts = getattr(self._local, 'attempts', 0)
    self._local.attempts = 0
    return attempts

  def count_attempt(self) -> None:
    self._local.attempts = getattr(self._local, 'attempts', 0) + 1

  def uncount_attempt(self) -> None:
    self._local.attempts = getattr(self._local, 'attempts', 1) - 1

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
    """Delegates to the guard, counting one provider attempt."""
    self.count_attempt()
    try:
      return self._guard.sample_text(
          prompt,
          max_tokens=max_tokens,
          terminators=terminators,
          temperature=temperature,
          top_p=top_p,
          top_k=top_k,
          timeout=timeout,
          seed=seed,
      )
    except budget_wrapper.BudgetExhaustedError:
      self.uncount_attempt()
      raise

  @override
  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Mapping[str, Any]]:
    """Delegates to the guard, counting one provider attempt."""
    self.count_attempt()
    try:
      return self._guard.sample_choice(prompt, responses, seed=seed)
    except budget_wrapper.BudgetExhaustedError:
      self.uncount_attempt()
      raise


class RetryStatsLanguageModel(language_model.LanguageModel):
  """Guard-and-retry chain that counts its own API retries.

  The wrapper composes the chain around the given budget guard
  (``Retry(Recorder(guard))``) and exposes it as one model, so the
  recording layer cannot be assembled in the wrong position. Each
  ``sample_text``/``sample_choice`` call on this wrapper is one logical
  request: it starts a fresh thread-local attempt count, runs the retry
  chain, and accounts the request's own attempts minus one as its API
  retries.

  Attributes:
    used_retries: cumulative count of API retries that reached the
      provider, including the count restored from a checkpoint.
  """

  def __init__(
      self,
      guard: budget_wrapper.BudgetGuardLanguageModel,
      *,
      retry_on_exceptions: Collection[type[Exception]] = (),
      used_retries: int = 0,
      retry_tries: int = 3,
      retry_delay: float = 0.0,
  ):
    """Initializes the wrapper.

    Args:
      guard: the budget guard every real attempt must pass; the guard
        (and not this wrapper) is what the run's attempt accounting
        reads.
      retry_on_exceptions: transient API exception whitelist forwarded
        to the core retry wrapper.
      used_retries: retry count already accounted for, e.g. restored
        from a checkpoint envelope.
      retry_tries: attempts per logical request before giving up.
      retry_delay: fixed delay between retries; the default of zero
        keeps deterministic tests fast and does not change the contract.
    """
    self._recorder = _RequestAttemptRecorder(guard)
    self._model = retry_wrapper.RetryLanguageModel(
        self._recorder,
        retry_on_exceptions=tuple(retry_on_exceptions),
        retry_tries=retry_tries,
        retry_delay=retry_delay,
        jitter=(0.0, 0.0),
        exponential_backoff=False,
    )
    self._used_retries = used_retries
    self._lock = threading.Lock()

  @property
  def used_retries(self) -> int:
    """Cumulative count of provider-reached API retries."""
    return self._used_retries

  def restore(self, used_retries: int) -> None:
    """Restores the cumulative retry count, e.g. from a checkpoint.

    Args:
      used_retries: retry count accounted for so far.
    """
    with self._lock:
      self._used_retries = used_retries

  def _account_request(self) -> None:
    """Accounts the finished request's own attempts as API retries."""
    attempts = self._recorder.take_attempts()
    if attempts > 1:
      with self._lock:
        self._used_retries += attempts - 1

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
    """Samples text, counting the request's provider retries.

    Args:
      prompt: the initial text to condition on.
      max_tokens: the maximum number of tokens in the response.
      terminators: the response will be terminated before any of these
        characters.
      temperature: temperature for the model.
      top_p: filters tokens based on cumulative probability.
      top_k: filters tokens by selecting the top_k most probable tokens.
      timeout: timeout for the request.
      seed: optional seed for the sampling.

    Returns:
      The sampled response.

    Raises:
      BudgetExhaustedError: propagated untouched when the request made
        no provider attempt; retries blocked by the budget never count.
    """
    self._recorder.start_request()
    try:
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
    finally:
      self._account_request()

  @override
  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Mapping[str, Any]]:
    """Samples a choice, counting the request's provider retries.

    Args:
      prompt: the initial text to condition on.
      responses: the responses to score.
      seed: optional seed for the sampling.

    Returns:
      (index, response, info) of the sampled response.

    Raises:
      BudgetExhaustedError: propagated untouched when the request made
        no provider attempt; retries blocked by the budget never count.
    """
    self._recorder.start_request()
    try:
      return self._model.sample_choice(prompt, responses, seed=seed)
    finally:
      self._account_request()
