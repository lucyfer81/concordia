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

"""Tests for BudgetGuardLanguageModel."""

import unittest
from unittest import mock

from concordia.contrib.language_models import budget_wrapper
from concordia.language_model import language_model
from concordia.testing import mock_model


def _inner():
  m = mock.MagicMock(spec=language_model.LanguageModel)
  m.sample_text.return_value = 'ok'
  m.sample_choice.return_value = (0, 'a', {})
  return m


class BudgetGuardTest(unittest.TestCase):

  def test_under_limit_passes_and_counts(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=2
    )
    self.assertEqual(guard.sample_text('p'), 'ok')
    self.assertEqual(guard.used_attempts, 1)

  def test_over_limit_raises_before_calling_model(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=1
    )
    guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p2')
    self.assertEqual(inner.sample_text.call_count, 1)
    self.assertEqual(guard.used_attempts, 1)

  def test_choice_and_text_share_budget(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=2
    )
    self.assertEqual(guard.sample_choice('p', ['a'])[1], 'a')
    guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_choice('p', ['a'])
    self.assertEqual(inner.sample_choice.call_count, 1)
    self.assertEqual(guard.used_attempts, 2)

  def test_failed_attempt_consumes_budget(self):
    inner = _inner()
    inner.sample_text.side_effect = RuntimeError('api down')
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=1
    )
    with self.assertRaises(RuntimeError):
      guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p')

  def test_thread_safe_no_oversubscribe(self):
    from concurrent import futures
    import threading

    calls = []
    lock = threading.Lock()

    class Counting(mock_model.MockModel):

      def sample_text(self, prompt, **kwargs):
        with lock:
          calls.append(1)
        return 'ok'

    guard = budget_wrapper.BudgetGuardLanguageModel(
        Counting(), total_attempt_limit=5
    )

    def attempt(_):
      try:
        guard.sample_text('p')
        return True
      except budget_wrapper.BudgetExhaustedError:
        return False

    with futures.ThreadPoolExecutor(max_workers=20) as executor:
      results = list(executor.map(attempt, range(20)))
    self.assertEqual(len(calls), 5)
    self.assertEqual(results.count(True), 5)
    self.assertEqual(results.count(False), 15)
    self.assertEqual(guard.used_attempts, 5)

  def test_restore(self):
    guard = budget_wrapper.BudgetGuardLanguageModel(
        _inner(), total_attempt_limit=10, used_attempts=9
    )
    guard.sample_text('p')  # 10th
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p')
    guard.restore(used_attempts=3, total_attempt_limit=10)
    self.assertEqual(guard.sample_text('p'), 'ok')


if __name__ == '__main__':
  unittest.main()
