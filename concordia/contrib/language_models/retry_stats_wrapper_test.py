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

"""Tests for RetryStatsLanguageModel over the guard-and-retry chain."""

from collections import Counter
from concurrent import futures
import threading
import time
import unittest

from concordia.contrib.language_models import budget_wrapper
from concordia.contrib.language_models import retry_stats_wrapper
from concordia.testing import mock_model
import tenacity


class _FlakyModel(mock_model.MockModel):
  """Fails the first ``failures`` calls with a transient exception."""

  def __init__(self, response: str, failures: int, error: type[Exception]):
    super().__init__(response)
    self._remaining_failures = failures
    self._error = error
    self.calls = Counter()

  def _maybe_fail(self, name: str) -> None:
    self.calls[name] += 1
    if self._remaining_failures > 0:
      self._remaining_failures -= 1
      raise self._error('transient')

  def sample_text(self, prompt, **kwargs):
    self._maybe_fail('text')
    return super().sample_text(prompt, **kwargs)

  def sample_choice(self, prompt, responses, **kwargs):
    self._maybe_fail('choice')
    return super().sample_choice(prompt, responses, **kwargs)


class _AlwaysFailingModel(mock_model.MockModel):
  """Fails every call with a transient exception."""

  def __init__(self):
    super().__init__('unused')
    self.calls = 0

  def _fail(self):
    self.calls += 1
    raise RuntimeError('api down')

  def sample_text(self, prompt, **kwargs):
    self._fail()

  def sample_choice(self, prompt, responses, **kwargs):
    self._fail()


class _DelayedModel(mock_model.MockModel):
  """Slows every call down so concurrent requests genuinely overlap."""

  DELAY_SECONDS = 0.05
  RESPONSE = 'delayed-ok'

  def sample_text(self, prompt, **kwargs):
    time.sleep(self.DELAY_SECONDS)
    return super().sample_text(prompt, **kwargs)

  def sample_choice(self, prompt, responses, **kwargs):
    time.sleep(self.DELAY_SECONDS)
    return super().sample_choice(prompt, responses, **kwargs)


def _chain(model, *, limit: int = 10, retry_tries: int = 3):
  """Builds the story chain: the stats wrapper around guard and retry."""
  guard = budget_wrapper.BudgetGuardLanguageModel(
      model, total_attempt_limit=limit
  )
  stats = retry_stats_wrapper.RetryStatsLanguageModel(
      guard,
      retry_on_exceptions=(RuntimeError,),
      retry_tries=retry_tries,
  )
  return stats, guard


class RetryStatsTest(unittest.TestCase):

  def test_first_attempt_success_counts_zero_retries(self):
    stats, guard = _chain(mock_model.MockModel('ok'))
    self.assertEqual(stats.sample_text('p'), 'ok')
    self.assertEqual(stats.used_retries, 0)
    self.assertEqual(guard.used_attempts, 1)

  def test_one_failure_then_success_counts_one_retry(self):
    stats, guard = _chain(_FlakyModel('ok', failures=1, error=RuntimeError))
    self.assertEqual(stats.sample_text('p'), 'ok')
    self.assertEqual(stats.used_retries, 1)
    self.assertEqual(guard.used_attempts, 2)

  def test_all_three_attempts_failing_count_two_retries(self):
    stats, guard = _chain(_AlwaysFailingModel())
    # Tenacity wraps the exhaustion in a RetryError; the wrapper must
    # propagate it untouched while still accounting the two retries.
    with self.assertRaises(tenacity.RetryError):
      stats.sample_text('p')
    self.assertEqual(stats.used_retries, 2)
    self.assertEqual(guard.used_attempts, 3)

  def test_budget_blocked_request_counts_no_retry(self):
    stats, guard = _chain(
        _FlakyModel('ok', failures=99, error=RuntimeError), limit=1
    )
    # The first attempt fails transiently and the retry it would trigger
    # is blocked by the budget: one attempt, zero initiated retries.
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      stats.sample_text('p')
    self.assertEqual(stats.used_retries, 0)
    self.assertEqual(guard.used_attempts, 1)
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      stats.sample_text('p2')  # fully blocked: no provider attempt at all
    self.assertEqual(stats.used_retries, 0)

  def test_parse_reasks_are_new_requests_not_retries(self):
    # An output-parse re-ask issues a second logical request; each
    # succeeds on its first attempt, so no API retry is counted.
    stats, guard = _chain(mock_model.MockModel('ok'))
    stats.sample_text('p')
    stats.sample_text('p')
    self.assertEqual(stats.used_retries, 0)
    self.assertEqual(guard.used_attempts, 2)

  def test_choice_and_text_share_the_retry_counter(self):
    stats, _ = _chain(_FlakyModel('ok', failures=2, error=RuntimeError))
    _, response, _ = stats.sample_choice('p', ['a'])
    self.assertEqual(response, 'a')
    stats.sample_text('p')
    self.assertEqual(stats.used_retries, 2)

  def test_restore_seeds_cumulative_counter(self):
    stats, _ = _chain(mock_model.MockModel('ok'))
    stats.restore(used_retries=5)
    stats.sample_text('p')
    self.assertEqual(stats.used_retries, 5)


class ConcurrentRetryAttributionTest(unittest.TestCase):
  """Recheck R3: overlapping requests must not count each other."""

  def test_overlapping_zero_failure_requests_count_zero_retries(self):
    # Two threads hold their requests open simultaneously through a
    # barrier; the provider always succeeds, so neither request retried,
    # even though the shared guard's counter advances by two.
    stats, guard = _chain(_DelayedModel(_DelayedModel.RESPONSE), limit=10)
    barrier = threading.Barrier(2)

    def request(prompt):
      barrier.wait()  # both requests are now in flight together
      return stats.sample_text(prompt)

    with futures.ThreadPoolExecutor(max_workers=2) as executor:
      results = list(executor.map(request, ('p1', 'p2')))
    self.assertEqual(results, [_DelayedModel.RESPONSE] * 2)
    self.assertEqual(guard.used_attempts, 2)
    self.assertEqual(stats.used_retries, 0)

  def test_one_flaky_request_attributes_only_its_own_retry(self):
    # One request fails once and then succeeds while an always-clean
    # request overlaps it: exactly one retry is counted, attributed to
    # the flaky request rather than smeared over both.
    flaky = _FlakyModel('ok', failures=1, error=RuntimeError)
    barrier = threading.Barrier(2)

    class _RoutingModel(mock_model.MockModel):
      """Routes half the prompts through the flaky model."""

      def sample_text(self, prompt, **kwargs):
        if 'flaky' in prompt:
          return flaky.sample_text(prompt, **kwargs)
        return super().sample_text(prompt, **kwargs)

    stats, guard = _chain(_RoutingModel('clean'), limit=10)

    def request(prompt):
      barrier.wait()
      return stats.sample_text(prompt)

    with futures.ThreadPoolExecutor(max_workers=2) as executor:
      list(executor.map(request, ('flaky-prompt', 'clean-prompt')))
    self.assertEqual(flaky.calls['text'], 2)
    self.assertEqual(guard.used_attempts, 3)
    self.assertEqual(stats.used_retries, 1)

  def test_mixed_text_and_choice_concurrency_counts_zero_retries(self):
    stats, guard = _chain(_DelayedModel(_DelayedModel.RESPONSE), limit=10)
    barrier = threading.Barrier(2)

    def text_request(_):
      barrier.wait()
      return stats.sample_text('p')

    def choice_request(_):
      barrier.wait()
      return stats.sample_choice('p', ['a'])

    with futures.ThreadPoolExecutor(max_workers=2) as executor:
      text_future = executor.submit(text_request, None)
      choice_future = executor.submit(choice_request, None)
      self.assertEqual(text_future.result(), _DelayedModel.RESPONSE)
      self.assertEqual(choice_future.result()[1], 'a')
    self.assertEqual(guard.used_attempts, 2)
    self.assertEqual(stats.used_retries, 0)

  def test_concurrent_budget_exhaustion_counts_no_retries(self):
    # Two requests race for the last unit of budget: one succeeds, the
    # other is fully blocked by the guard (no provider attempt), so no
    # retry is initiated by either of them.
    stats, guard = _chain(_DelayedModel(), limit=1)
    barrier = threading.Barrier(2)

    def request(prompt):
      barrier.wait()
      try:
        stats.sample_text(prompt)
        return True
      except budget_wrapper.BudgetExhaustedError:
        return False

    with futures.ThreadPoolExecutor(max_workers=2) as executor:
      results = list(executor.map(request, ('p1', 'p2')))
    self.assertEqual(results.count(True), 1)
    self.assertEqual(guard.used_attempts, 1)
    self.assertEqual(stats.used_retries, 0)


if __name__ == '__main__':
  unittest.main()
