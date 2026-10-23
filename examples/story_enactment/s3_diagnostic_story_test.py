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

"""Tests for the S3 diagnostic ledger runner (offline, no network)."""

from collections.abc import Callable
from concurrent import futures
import hashlib
import json
import os
import pathlib
import tempfile
import threading
import types
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.language_models import budget_wrapper
from concordia.contrib.language_models import retry_stats_wrapper
from concordia.contrib.language_models.openai import base_gpt_model
from concordia.language_model import language_model
import httpx
import openai

from examples.story_enactment import request_ledger as rl
from examples.story_enactment import run as run_lib
from examples.story_enactment import s3_diagnostic_story as s3


def _tmp_dir() -> tempfile.TemporaryDirectory:
  """Returns a fresh temporary directory as a context manager."""
  return tempfile.TemporaryDirectory()


def _fake_repo_root() -> tempfile.TemporaryDirectory:
  """Returns a tmp root holding a fake file per snapshot path."""
  tmp = tempfile.TemporaryDirectory()
  root = pathlib.Path(tmp.name)
  for relative in s3._SNAPSHOT_FILES:  # pylint: disable=protected-access
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f'fake {relative}', encoding='utf-8')
  return tmp


class _FakeInner(language_model.LanguageModel):
  """Scripted inner model; optional hook runs before each call."""

  def __init__(
      self,
      fail_on_call: int | None = None,
      hook: Callable[[], None] | None = None,
  ):
    self.calls = 0
    self._fail_on_call = fail_on_call
    self._hook = hook
    self.seen: list[str] = []

  def sample_text(self, prompt, **kwargs):
    self.calls += 1
    self.seen.append(prompt)
    if self._hook is not None:
      self._hook()
    if self.calls == self._fail_on_call:
      raise RuntimeError('transient provider error')
    return f'answer-{self.calls}'

  def sample_choice(self, prompt, responses, *, seed=None):
    self.calls += 1
    self.seen.append(prompt)
    if self._hook is not None:
      self._hook()
    return 0, responses[0], {}


class _FakeUsage:
  total_tokens = 11


class _FakeMessage:
  content = '{"valid": true}'


class _FakeResponse:
  model = 'gpt-6.1-solx'  # 故意与请求名不同，断言留证
  usage = _FakeUsage()
  choices = [type('Choice', (), {'message': _FakeMessage()})()]


class _FakeCompletions:

  def __init__(self):
    self.calls = 0

  def create(self, **kwargs):
    self.calls += 1
    return _FakeResponse()


class _FakeChat:

  def __init__(self, completions):
    self.completions = completions


class _FakeClient:
  """Minimal OpenAI-compatible client exposing chat.completions."""

  def __init__(self, completions):
    self.chat = _FakeChat(completions)


class _FakeClientModel(_FakeInner):
  """Inner exposing a _client so the observation boundary is exercised."""

  def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.completions = _FakeCompletions()
    self._client = _FakeClient(self.completions)

  def sample_text(self, prompt, **kwargs):
    # Route through the client, like the production adapter, so the
    # observing client sees the create() call inside the active slot.
    self._client.chat.completions.create(
        model='gpt-6.1-sol',
        messages=[{'role': 'user', 'content': prompt}],
    )
    return super().sample_text(prompt, **kwargs)


class WrapperTest(unittest.TestCase):

  def _ledger(self, path, attempt_limit=400):
    return rl.RequestLedger(path, batch_id='t', **s3.ledger_caps(attempt_limit))

  def _sdk(self, path, attempt_limit=400):
    return s3._SdkCreateLedger(  # pylint: disable=protected-access
        path, attempt_limit=attempt_limit
    )

  def test_caps_mirror_per_run_budgets(self):
    caps = s3.ledger_caps(400)
    self.assertEqual(caps['global_cap'], 800)
    self.assertEqual(caps['group_caps'], {'R1': 400, 'R2': 400})
    self.assertEqual(caps['sample_caps'], {'R1': 400, 'R2': 400})

  def test_prompt_and_started_persist_before_inner_call(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    raw_dir = path / 'raw'

    def hook():
      # inner 调用发生时，prompt 与 started 必须已在磁盘上。
      self.assertTrue((raw_dir / 'R1__a000001.prompt.txt').exists())
      self.assertTrue((raw_dir / 'R1__a000001.started.json').exists())

    inner = _FakeInner(hook=hook)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    self.assertEqual(model.sample_text('a'), 'answer-1')

  def test_each_attempt_is_one_ledger_slot(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeInner()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    model.sample_text('a')
    model.sample_text('b')
    self.assertEqual(model.attempts, 2)
    self.assertEqual(ledger.counts['global'], 2)
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'started'],
        ['R1__a000001', 'R1__a000002'],
    )
    self.assertEqual(
        [e['status'] for e in events if e['event'] == 'finished'],
        ['completed', 'completed'],
    )

  def test_client_observation_archives_raw_response_and_identity(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeClientModel()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    answer = model.sample_text('p')
    self.assertEqual(answer, 'answer-1')
    raw = (path / 'raw' / 'R1__a000001.response.txt').read_text()
    self.assertEqual(raw, '{"valid": true}')
    events = rl.load_events(path / 'requests.jsonl')
    finished = [e for e in events if e['event'] == 'finished'][0]
    self.assertEqual(finished['boundary']['provider_model'], 'gpt-6.1-solx')
    self.assertEqual(finished['boundary']['usage']['total_tokens'], 11)

  def test_interleaved_sdk_calls_keep_their_own_slot(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger, sdk = self._ledger(path), self._sdk(path)
    a_entered, b_entered, release_b = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class Inner(_FakeClientModel):

      def sample_text(self, prompt, **kwargs):
        if prompt == 'A':
          a_entered.set()
          if not b_entered.wait(5):
            raise TimeoutError('B did not enter')
        else:
          b_entered.set()
          if not release_b.wait(5):
            raise TimeoutError('B was not released')
        return (
            self._client.chat.completions.create(
                model='gpt-6.1-sol',
                messages=[{'role': 'user', 'content': prompt}],
            )
            .choices[0]
            .message.content
        )

    inner = Inner()

    def create(**kwargs):
      return types.SimpleNamespace(
          model='fake-sol',
          usage=None,
          choices=[
              types.SimpleNamespace(
                  message=types.SimpleNamespace(
                      content='response:' + kwargs['messages'][-1]['content']
                  )
              )
          ],
      )

    inner.completions.create = create
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id='R1', inner=inner
    )
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
      a = pool.submit(model.sample_text, 'A')
      self.assertTrue(a_entered.wait(5))
      b = pool.submit(model.sample_text, 'B')
      try:
        self.assertEqual(a.result(timeout=5), 'response:A')
      finally:
        release_b.set()
      self.assertEqual(b.result(timeout=5), 'response:B')
    for number, prompt in ((1, 'A'), (2, 'B')):
      slot = f'R1__a{number:06d}'
      self.assertEqual(
          (path / 'raw' / f'{slot}.response.txt').read_text(),
          'response:' + prompt,
      )
    sdk_starts = [
        e
        for e in rl.load_events(path / 'sdk_ledger/requests.jsonl')
        if e['event'] == 'started'
    ]
    self.assertEqual(
        [e['attempt_id'] for e in sdk_starts], ['R1__a000001', 'R1__a000002']
    )

  def test_concurrent_caps_do_not_serialize_network_or_oversubscribe(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger, sdk = self._ledger(path, 4), self._sdk(path, 4)
    inner = _FakeClientModel()
    barrier = threading.Barrier(4)

    def create(**kwargs):
      del kwargs
      barrier.wait(timeout=5)  # All four provider calls must overlap.
      return _FakeResponse()

    inner.completions.create = create
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id='R1', inner=inner
    )

    def attempt(number):
      try:
        model.sample_text(str(number))
        return True
      except rl.LedgerBudgetError:
        return False

    with futures.ThreadPoolExecutor(max_workers=8) as pool:
      self.assertEqual(sum(pool.map(attempt, range(8))), 4)
    self.assertEqual(model.attempts, 4)
    for directory, counts in (
        (path, ledger.counts),
        (path / 'sdk_ledger', sdk.counts),
    ):
      events = rl.load_events(directory / 'requests.jsonl')
      self.assertEqual(rl.rebuild_counts(events), counts)
      self.assertEqual(counts['global'], 4)
      self.assertEqual(counts['by_group'], {'R1': 4})
      self.assertEqual(len(counts['finished_slots']), 4)

  def test_close_drains_admitted_calls_and_blocks_late_requests(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger, sdk = self._ledger(path), self._sdk(path)
    entered, release = threading.Event(), threading.Event()

    def hook():
      entered.set()
      if not release.wait(5):
        raise TimeoutError('provider was not released')

    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=sdk,
        run_id='R1',
        inner=_FakeInner(hook=hook),
    )
    with futures.ThreadPoolExecutor(max_workers=2) as pool:
      active = pool.submit(model.sample_text, 'admitted')
      self.assertTrue(entered.wait(5))
      closing = pool.submit(model.close)
      try:
        with model._condition:  # pylint: disable=protected-access
          self.assertTrue(
              model._condition.wait_for(  # pylint: disable=protected-access
                  lambda: model._closed, timeout=5
              )
          )  # pylint: disable=protected-access
        self.assertFalse(closing.done())
        with self.assertRaises(s3.BatchStop):
          model.sample_text('late')
      finally:
        release.set()
      self.assertEqual(active.result(timeout=5), 'answer-1')
      closing.result(timeout=5)
    self.assertEqual(ledger.counts['global'], 1)
    self.assertEqual(len(ledger.counts['finished_slots']), 1)

  def test_model_level_archive_when_no_client(self):
    # inner 无 _client（stub 形态）：response.txt 记返回文本，
    # boundary 记 observation_level='model_return'。
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeInner()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    answer = model.sample_text('p')
    self.assertEqual(answer, 'answer-1')
    raw = (path / 'raw' / 'R1__a000001.response.txt').read_text()
    self.assertEqual(raw, 'answer-1')
    events = rl.load_events(path / 'requests.jsonl')
    finished = [e for e in events if e['event'] == 'finished'][0]
    self.assertEqual(finished['boundary']['observation_level'], 'model_return')

  def test_error_occupies_budget_and_records_status(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeInner(fail_on_call=1)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    with self.assertRaises(RuntimeError):
      model.sample_text('p')
    self.assertEqual(ledger.counts['global'], 1)  # started 已预扣
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['status'] for e in events if e['event'] == 'finished'],
        ['RuntimeError'],
    )

  def test_slot_sequence_continues_after_ledger_rebuild(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger1 = self._ledger(path)
    s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger1,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=_FakeInner(),
    ).sample_text('a')
    ledger2 = self._ledger(path)  # 重建
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger2,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=_FakeInner(),
    )
    model.sample_text('b')
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'started'][-1],
        'R1__a000002',
    )

  def test_per_run_cap_blocks_before_request(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path, attempt_limit=1)
    inner = _FakeInner()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path, attempt_limit=1),
        run_id='R1',
        attempt_limit=1,
        inner=inner,
    )
    model.sample_text('a')
    with self.assertRaises(rl.LedgerBudgetError):
      model.sample_text('b')
    self.assertEqual(inner.calls, 1)  # 第二次从未到达 inner
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(events[-1]['event'], 'budget_blocked')
    self.assertEqual(events[-1]['level'], 'group')

  def test_second_run_uses_its_own_budget(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path, attempt_limit=1)
    s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path, attempt_limit=1),
        run_id='R1',
        attempt_limit=1,
        inner=_FakeInner(),
    ).sample_text('a')
    s3.LedgerLanguageModel(  # R2 预算独立，不受 R1 影响
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path, attempt_limit=1),
        run_id='R2',
        attempt_limit=1,
        inner=_FakeInner(),
    ).sample_text('a')
    self.assertEqual(ledger.counts['by_group'], {'R1': 1, 'R2': 1})

  def test_choice_params_are_archived(self):
    # sample_choice 的 passed_params 含 responses 与 seed；请求前落盘。
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    raw_dir = path / 'raw'

    def hook():
      sidecar = json.loads(
          (raw_dir / 'R1__a000001.started.json').read_text(encoding='utf-8')
      )
      self.assertEqual(
          sidecar['passed_params'], {'responses': ['yes', 'no'], 'seed': 7}
      )

    inner = _FakeInner(hook=hook)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol',
        ledger=ledger,
        sdk_ledger=self._sdk(path),
        run_id='R1',
        inner=inner,
    )
    index, choice, _ = model.sample_choice('p', ['yes', 'no'], seed=7)
    self.assertEqual((index, choice), (0, 'yes'))

  def test_create_outside_active_slot_is_rejected(self):
    # 观测客户端在无 active slot 时 create → BatchStop（布线错误），
    # 且 SDK 层不预扣（检查先于预扣）。
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    sdk = self._sdk(path)
    inner = _FakeClientModel()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id='R1', inner=inner
    )
    observing_client = inner._client
    with self.assertRaises(s3.BatchStop):
      observing_client.chat.completions.create(
          model='gpt-6.1-sol',
          messages=[{'role': 'user', 'content': 'x'}],
      )
    self.assertEqual(inner.completions.calls, 0)  # 从未到达真实 create
    self.assertEqual(sdk.counts['global'], 0)  # SDK 层零预扣


class SdkLedgerTest(unittest.TestCase):
  """SDK 第二层账本（协议 §4）：逐次预扣、上限先阻断、按运行独立、镜像。"""

  def _ledgers(self, sdk_attempt_limit=400):
    """A tmp batch dir with the Concordia ledger and the SDK gate."""
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    # Concordia 层保持默认 400：SDK 层阻断必须先于 Concordia 上限，
    # 否则测不到 SDK 层自身的硬停止。
    ledger = rl.RequestLedger(path, batch_id='t', **s3.ledger_caps())
    sdk = s3._SdkCreateLedger(  # pylint: disable=protected-access
        path, attempt_limit=sdk_attempt_limit
    )
    return path, ledger, sdk

  def _model(self, ledger, sdk, run_id='R1'):
    inner = _FakeClientModel()
    return inner, s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id=run_id, inner=inner
    )

  def test_each_create_books_its_own_sdk_slot(self):
    path, ledger, sdk = self._ledgers()
    _, model = self._model(ledger, sdk)
    model.sample_text('a')
    model.sample_text('b')
    self.assertEqual(ledger.counts['global'], 2)  # Concordia 层照常记账
    self.assertEqual(sdk.counts['global'], 2)  # 每次 create 一个 SDK slot
    self.assertEqual(sdk.counts['by_group'], {'R1': 2})
    events = rl.load_events(path / 'sdk_ledger' / 'requests.jsonl')
    started = [e for e in events if e['event'] == 'started']
    self.assertEqual(
        [e['slot_id'] for e in started],
        ['R1__sdk000001', 'R1__sdk000002'],
    )
    # 关联不混加：SDK slot 以 attempt_id 指向其 Concordia slot。
    self.assertEqual(
        [e['attempt_id'] for e in started],
        ['R1__a000001', 'R1__a000002'],
    )
    self.assertEqual({e['stage'] for e in started}, {'sdk_create'})

  def test_real_choice_adapter_can_reask_within_one_attempt(self):
    _, ledger, sdk = self._ledgers()
    replies = iter(('invalid', 'b'))

    def create(**kwargs):
      del kwargs
      return types.SimpleNamespace(
          model='fake-sol',
          usage=None,
          choices=[
              types.SimpleNamespace(
                  message=types.SimpleNamespace(content=next(replies))
              )
          ],
      )

    client: Any = _FakeClient(types.SimpleNamespace(create=create))
    inner = base_gpt_model.BaseGPTModel(model_name='gpt-6.1-sol', client=client)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id='R1', inner=inner
    )
    self.assertEqual(model.sample_choice('choose', ['a', 'b'])[:2], (1, 'b'))
    self.assertEqual(ledger.counts['global'], 1)
    self.assertEqual(sdk.counts['global'], 2)

  def test_production_api_retry_chain_keeps_separate_attempts(self):
    _, ledger, sdk = self._ledgers()
    calls = []

    def create(**kwargs):
      del kwargs
      calls.append(1)
      if len(calls) == 1:
        response: Any = httpx.Response(
            429,
            request=httpx.Request(
                'POST', 'https://offline.invalid/chat/completions'
            ),
        )
        raise openai.RateLimitError('try again', response=response, body=None)
      return _FakeResponse()

    client: Any = _FakeClient(types.SimpleNamespace(create=create))
    inner = base_gpt_model.BaseGPTModel(model_name='gpt-6.1-sol', client=client)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, sdk_ledger=sdk, run_id='R1', inner=inner
    )
    guard = budget_wrapper.BudgetGuardLanguageModel(
        model, total_attempt_limit=400
    )
    retry = retry_stats_wrapper.RetryStatsLanguageModel(
        guard, retry_on_exceptions=(openai.RateLimitError,)
    )
    self.assertEqual(retry.sample_text('prompt'), '{"valid": true}')
    self.assertEqual(guard.used_attempts, 2)
    self.assertEqual(retry.used_retries, 1)
    self.assertEqual(ledger.counts['global'], 2)
    self.assertEqual(sdk.counts['global'], 2)

  def test_sdk_prompt_persisted_before_forward(self):
    path, ledger, sdk = self._ledgers()
    _, model = self._model(ledger, sdk)
    model.sample_text('p')
    sidecar = json.loads(
        (path / 'sdk_ledger' / 'raw' / 'R1__sdk000001.started.json').read_text(
            encoding='utf-8'
        )
    )
    self.assertEqual(sidecar['sample_id'], 'R1')
    self.assertEqual(sidecar['group'], 'R1')
    self.assertIn('prompt_sha256', sidecar)

  def test_cap_blocks_create_before_forwarding(self):
    # SDK 400/run 硬上限：满额后的 create 在转发前被拒——真实 create
    # 未被触达，被阻断调用不写 started slot、不计预算、不落 raw 留证；
    # 停止以生产 BudgetExhaustedError 语义出现，并点名 SDK 停止层。
    path, ledger, sdk = self._ledgers(sdk_attempt_limit=1)
    inner, model = self._model(ledger, sdk)
    model.sample_text('a')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError) as ctx:
      model.sample_text('b')
    self.assertIn('sdk_ledger', str(ctx.exception))
    self.assertEqual(inner.completions.calls, 1)  # 第二次从未转发
    self.assertEqual(sdk.counts['global'], 1)  # 阻断不占 SDK 预算
    self.assertEqual(ledger.counts['global'], 2)  # Concordia attempt 已预扣
    events = rl.load_events(path / 'sdk_ledger' / 'requests.jsonl')
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'started'],
        ['R1__sdk000001'],  # 被阻断调用无 started slot
    )
    self.assertEqual(events[-1]['event'], 'budget_blocked')
    self.assertFalse(
        (path / 'sdk_ledger' / 'raw' / 'R1__sdk000002.prompt.txt').exists()
    )

  def test_sdk_budget_is_per_run(self):
    # R1/R2 各自 400：R1 耗尽不占用 R2 额度，R2 照常转发。
    path, ledger, sdk = self._ledgers(sdk_attempt_limit=1)
    inner_r1, model_r1 = self._model(ledger, sdk, run_id='R1')
    inner_r2, model_r2 = self._model(ledger, sdk, run_id='R2')
    model_r1.sample_text('a')
    model_r2.sample_text('a')  # R2 不受 R1 影响
    self.assertEqual(sdk.counts['by_group'], {'R1': 1, 'R2': 1})
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      model_r1.sample_text('b')  # R1 已满
    self.assertEqual(inner_r1.completions.calls, 1)
    self.assertEqual(inner_r2.completions.calls, 1)

  def test_sdk_counts_mirror_rebuilt_ledger(self):
    # 报告可从磁盘事件重建同一计数：不依赖运行时状态。
    path, ledger, sdk = self._ledgers()
    self._model(ledger, sdk, run_id='R1')[1].sample_text('a')
    self._model(ledger, sdk, run_id='R2')[1].sample_text('a')
    rebuilt = rl.rebuild_counts(
        rl.load_events(path / 'sdk_ledger' / 'requests.jsonl')
    )
    self.assertEqual(rebuilt, sdk.counts)


class FreezeStageTest(unittest.TestCase):
  """Freeze stage: offline capture of protocol, snapshot and report."""

  def _fake_root(self) -> pathlib.Path:
    return pathlib.Path(self.enterContext(_fake_repo_root()))

  def _frozen_fake_batch(self, root: pathlib.Path) -> pathlib.Path:
    """Builds a frozen batch dir against a tmp fake repo root."""
    batch = self._batch_path()
    batch.mkdir()
    s3._write_json_file(  # pylint: disable=protected-access
        batch / 'protocol.json', s3.protocol_payload()
    )
    s3._write_json_file(  # pylint: disable=protected-access
        batch / 'source_snapshot.json', s3.source_snapshot_payload(root=root)
    )
    s3._save_report(  # pylint: disable=protected-access
        batch,
        {
            'protocol_sha256': (
                hashlib.sha256(
                    (batch / 'protocol.json').read_bytes()
                ).hexdigest()
            ),
            'source_snapshot_sha256': (
                hashlib.sha256(
                    (batch / 'source_snapshot.json').read_bytes()
                ).hexdigest()
            ),
            'protocol_document_sha256': hashlib.sha256(
                (root / s3._PROTOCOL_DOCUMENT).read_bytes()  # pylint: disable=protected-access
            ).hexdigest(),
        },
    )
    return batch

  def _batch_path(self) -> pathlib.Path:
    """A fresh non-existent batch dir inside a managed tmp dir."""
    return pathlib.Path(self.enterContext(_tmp_dir())) / 'batch'

  def test_freeze_refuses_existing_dir(self):
    path = self._batch_path()
    s3.freeze_stage(path)
    with self.assertRaises(s3.BatchStop):
      s3.freeze_stage(path)

  def test_freeze_writes_hashed_payloads_and_initial_report(self):
    path = self._batch_path()
    s3.freeze_stage(path)
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    for name, key in (
        ('protocol.json', 'protocol_sha256'),
        ('source_snapshot.json', 'source_snapshot_sha256'),
    ):
      digest = hashlib.sha256((path / name).read_bytes()).hexdigest()
      self.assertEqual(report[key], digest)
    self.assertEqual(
        report['protocol_document_sha256'],
        hashlib.sha256(
            (s3._REPO_ROOT / s3._PROTOCOL_DOCUMENT).read_bytes()  # pylint: disable=protected-access
        ).hexdigest(),
    )
    self.assertEqual(
        report['counts'],
        {
            'planned_runs': 2,
            'finished_runs': 0,
            'ledger_attempts': 0,
            'by_run': {},
        },
    )
    self.assertEqual(report['runs'], {})
    self.assertIsNone(report['stop'])
    self.assertEqual(report['deviations'], [])
    s3._verify_frozen(path)  # pylint: disable=protected-access

  def test_protocol_declares_confirmed_numbers(self):
    protocol = s3.protocol_payload()
    self.assertEqual(protocol['model_scope'], 'Sol-only')
    self.assertEqual(protocol['runs'], ['R1', 'R2'])
    self.assertEqual(protocol['budgets']['turns_per_run'], 24)
    self.assertEqual(protocol['budgets']['attempts_per_run'], 400)
    self.assertEqual(protocol['budgets']['sdk_create_cap_per_run'], 400)
    self.assertIn('beat_timeout 不是完成结局', protocol['evaluation_criteria'])
    self.assertIn('诊断无验收门', protocol['conclusion_semantics'])

  def test_protocol_declares_frozen_director_table(self):
    # 协议 §3 七参数冻结表逐键照实转写进 payload；runner 以 run_config
    # 显式传入，不依赖生产默认值（dry-run 核对 checkpoint 实际生效值）。
    self.assertEqual(
        s3.protocol_payload()['director_params']['values'],  # pylint: disable=protected-access
        {
            'feedback_enabled': True,
            'eval_interval': 2,
            'scoring_window': 8,
            'tolerance': 1,
            'repetition_push_enabled': True,
            'repetition_check_interval': 3,
            'cooldown_turns': 3,
        },
    )
    self.assertEqual(
        s3._FROZEN_DIRECTOR_PARAMS,  # pylint: disable=protected-access
        s3.protocol_payload()['director_params']['values'],  # pylint: disable=protected-access
    )

  def test_verify_frozen_refuses_drift(self):
    path = self._batch_path()
    s3.freeze_stage(path)
    target = path / 'protocol.json'
    target.write_text(
        target.read_text(encoding='utf-8') + ' ', encoding='utf-8'
    )
    with self.assertRaises(s3.BatchStop):
      s3._verify_frozen(path)  # pylint: disable=protected-access

  def test_verify_frozen_refuses_source_drift(self):
    # 改动快照清单内任一文件：通过 tmp 假仓库注入 root 验证漂移拒绝。
    root = self._fake_root()
    batch = self._frozen_fake_batch(root)
    target = root / s3._SNAPSHOT_FILES[0]  # pylint: disable=protected-access
    target.write_text(
        target.read_text(encoding='utf-8') + ' drifted', encoding='utf-8'
    )
    with self.assertRaises(s3.BatchStop):
      s3._verify_frozen(batch, root=root)  # pylint: disable=protected-access

  def test_snapshot_lists_all_files_with_hashes(self):
    root = self._fake_root()
    snapshot = s3.source_snapshot_payload(root=root)
    self.assertEqual(
        [entry['path'] for entry in snapshot['files']],
        list(s3._SNAPSHOT_FILES),  # pylint: disable=protected-access
    )
    for entry in snapshot['files']:
      content = (root / entry['path']).read_bytes()
      self.assertEqual(entry['sha256'], hashlib.sha256(content).hexdigest())
      self.assertEqual(entry['bytes'], len(content))

  def test_snapshot_refuses_missing_file(self):
    # 任一快照文件缺失即 BatchStop，不做静默收缩。
    root = pathlib.Path(self.enterContext(_tmp_dir()))
    only = root / s3._SNAPSHOT_FILES[0]  # pylint: disable=protected-access
    only.parent.mkdir(parents=True)
    only.write_text('only one', encoding='utf-8')
    with self.assertRaises(s3.BatchStop):
      s3.source_snapshot_payload(root=root)


class RunStageTest(unittest.TestCase):
  """Run stage: injection, interruption pausing, failure mapping, pending."""

  def _frozen_batch(self) -> pathlib.Path:
    path = pathlib.Path(self.enterContext(_tmp_dir())) / 'batch'
    s3.freeze_stage(path)
    return path

  def test_run_stage_offline_executes_both_runs_with_stub(self):
    batch = self._frozen_batch()
    s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(sorted(report['runs']), ['R1', 'R2'])
    for entry in report['runs'].values():
      self.assertIn(entry['reason'], s3._TERMINAL_REASONS)  # pylint: disable=protected-access
      self.assertEqual(entry['used_model_attempts'], entry['ledger_attempts'])
    self.assertEqual(report['counts']['finished_runs'], 2)
    self.assertLessEqual(report['counts']['ledger_attempts'], 800)

  def test_production_caught_auth_error_stops_before_r2(self):
    batch = self._frozen_batch()

    class AuthFail(_FakeInner):

      def sample_text(self, prompt, **kwargs):
        response: Any = httpx.Response(
            401,
            request=httpx.Request(
                'POST', 'https://offline.invalid/chat/completions'
            ),
        )
        raise openai.AuthenticationError(
            'offline auth failure', response=response, body=None
        )

    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(AuthFail(), ())
    ):
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(report['stop']['run'], 'R1')
    self.assertIn('AuthenticationError', report['stop']['error'])
    self.assertFalse((batch / 'R2').exists())
    summary = json.loads((batch / 'R1/summary.json').read_text())
    self.assertEqual(summary['reason'], 'error')
    self.assertIn('AuthenticationError', summary['error_message'])

  def test_semantic_review_rejection_still_continues_to_r2(self):
    batch = self._frozen_batch()
    original = run_lib.make_language_model

    def factory(*args, **kwargs):
      provider, retryable = original(*args, **kwargs)
      sample = provider.sample_text

      def text(prompt, **params):
        if '# Story resolution semantic review' in prompt:
          return '{"valid": false, "reason": "scripted semantic rejection"}'
        return sample(prompt, **params)

      provider.sample_text = text
      return provider, retryable

    with mock.patch.object(run_lib, 'make_language_model', side_effect=factory):
      s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNone(report['stop'])
    for run_id in ('R1', 'R2'):
      self.assertIn(
          'StoryOutputError: Semantic review rejected output:',
          report['runs'][run_id]['error_message'],
      )

  def test_drift_before_sdk_forward_stops_without_sdk_request(self):
    for target in ('protocol', 'prompt'):
      with self.subTest(target=target):
        batch = self._frozen_batch()

        class Inner(_FakeClientModel):

          def sample_text(self, prompt, **kwargs):
            path = (
                batch / 'protocol.json'
                if target == 'protocol'
                else next((batch / 'raw').glob('*.prompt.txt'))
            )
            path.write_text(path.read_text() + '\n')
            return super().sample_text(prompt, **kwargs)

        inner = Inner()
        with mock.patch.object(
            run_lib, 'make_language_model', return_value=(inner, ())
        ):
          with self.assertRaises(SystemExit):
            s3.run_stage(batch, offline=True)
        self.assertEqual(inner.completions.calls, 0)
        self.assertFalse((batch / 'R2').exists())

  def test_sdk_archive_failure_is_not_silently_accepted(self):
    batch = self._frozen_batch()
    with mock.patch.object(
        run_lib,
        'make_language_model',
        side_effect=lambda *a, **k: (_FakeClientModel(), ()),
    ), mock.patch.object(
        s3._SdkCreateLedger,
        'record_response',
        side_effect=OSError('cannot archive raw response'),
    ):
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertIn('cannot archive raw response', report['stop']['error'])
    self.assertFalse((batch / 'R2').exists())

  def test_drift_between_runs_prevents_r2(self):
    batch = self._frozen_batch()
    original = s3._execute_run  # pylint: disable=protected-access

    def execute(*args, **kwargs):
      result = original(*args, **kwargs)
      if args[0] == 'R1':
        path = batch / 'protocol.json'
        path.write_text(path.read_text() + '\n')
      return result

    with mock.patch.object(s3, '_execute_run', side_effect=execute):
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True)
    self.assertFalse((batch / 'R2').exists())
    self.assertIsNotNone(
        json.loads((batch / 'report.json').read_text())['stop']
    )

  def test_run_stage_is_idempotent_after_terminal(self):
    batch = self._frozen_batch()
    s3.run_stage(batch, offline=True)
    events_before = (batch / 'requests.jsonl').read_text()
    s3.run_stage(batch, offline=True)  # 重入：全部 finished，零新请求
    self.assertEqual((batch / 'requests.jsonl').read_text(), events_before)

  def test_summary_without_checkpoint_reports_finished_entry(self):
    # 幂等回填：删 report 中 runs 后重入，仍从 summary.json 回填。
    batch = self._frozen_batch()
    s3.run_stage(batch, offline=True)
    events_before = (batch / 'requests.jsonl').read_text()
    report = json.loads((batch / 'report.json').read_text())
    report['runs'] = {}
    (batch / 'report.json').write_text(json.dumps(report, ensure_ascii=False))
    s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(sorted(report['runs']), ['R1', 'R2'])
    for entry in report['runs'].values():
      self.assertEqual(entry['status'], 'finished')
      self.assertIn(entry['reason'], s3._TERMINAL_REASONS)  # pylint: disable=protected-access
    self.assertEqual((batch / 'requests.jsonl').read_text(), events_before)

  def test_interrupted_run_pauses_batch_for_review(self):
    # 协议 §5：裸 checkpoint（有 checkpoint 无 summary）= 运行中断：
    # 标记 interrupted、暂停批次交用户审阅；不自动 resume，后续运行
    # 不启动；重入因批次已停仍被拒。
    batch = self._frozen_batch()
    state = batch / 'R1'
    s3.run_stage(batch, offline=True, runs=('R1',))
    (state / 'summary.json').unlink()
    # 清 report 里 R1 条目，模拟「该 run 未完成」的重入。
    report = json.loads((batch / 'report.json').read_text())
    report['runs'].pop('R1')
    (batch / 'report.json').write_text(json.dumps(report, ensure_ascii=False))
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(report['runs']['R1']['status'], 'interrupted')
    self.assertIsNotNone(report['stop'])
    self.assertEqual(report['stop']['run'], 'R1')
    self.assertIn('interrupted', report['stop']['error'])
    self.assertNotIn('R2', report['runs'])  # 批次已暂停，R2 未启动
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True)  # 已停批：无自动恢复路径

  def test_pending_slot_refuses_continuation(self):
    # started 无 finished 是中断证据：拒绝继续并暂停批次。S2 式
    # 跳过授权已按协议 §5 移除——无参数可绕过，重入仍拒，不记
    # deviations，被拒 slot 的预算保持已预扣状态。
    batch = self._frozen_batch()
    ledger = rl.RequestLedger(  # pylint: disable=protected-access
        batch, batch_id=s3._BATCH_ID, **s3.ledger_caps()  # pylint: disable=protected-access
    )
    ledger.begin_request(
        slot_id='R1__a000001',
        sample_id='R1',
        group='R1',
        model_tag='m4',
        model_name='gpt-6.1-sol',
        attempt_id='R1__m4',
        stage='real_request',
        prompt='p',
        passed_params={},
    )  # started 无 finished → pending
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNotNone(report['stop'])
    self.assertIn('R1__a000001', report['stop']['error'])
    self.assertEqual(report['deviations'], [])
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True)  # 无授权路径：重入仍然拒绝

  def test_fresh_run_refuses_preexisting_ledger_slots(self):
    # 无 checkpoint/summary 的 run 目录却已有该 run 的账本 slot：
    # 拒绝执行（不烧预算于无故事状态锚点的消费），批次停下留场。
    batch = self._frozen_batch()
    ledger = rl.RequestLedger(  # pylint: disable=protected-access
        batch, batch_id=s3._BATCH_ID, **s3.ledger_caps()  # pylint: disable=protected-access
    )
    ledger.begin_request(
        slot_id='R1__a000001',
        sample_id='R1',
        group='R1',
        model_tag='m4',
        model_name='gpt-6.1-sol',
        attempt_id='R1__m4',
        stage='real_request',
        prompt='p',
        passed_params={},
    )
    ledger.finish_request(slot_id='R1__a000001', status='completed')
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True, runs=('R1',))
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNotNone(report['stop'])
    self.assertIn('unanchored', report['stop']['error'])

  def test_run_exception_stops_batch(self):
    batch = self._frozen_batch()
    original = run_lib.run

    def exploding(*args, **kwargs):
      raise ConnectionError('auth failed')

    run_lib.run = exploding
    try:
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True, runs=('R1',))
    finally:
      run_lib.run = original
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNotNone(report['stop'])
    self.assertEqual(report['stop']['run'], 'R1')
    self.assertIn('auth failed', report['stop']['error'])

  def test_ledger_production_mismatch_stops_batch(self):
    # 账本与生产计数不一致是审计异常：伪造 RunResult 形状的返回值
    # （used_model_attempts 多记一次）触发不一致 → BatchStop 停批留场。
    batch = self._frozen_batch()
    fake = types.SimpleNamespace(
        reason='step_limit',
        committed_turn_id=3,
        budget_exhausted=False,
        budget_error=None,
        error_message=None,
        used_model_attempts=1,  # 账本为 0，故意多一
        used_api_retries=0,
        used_scoring_parse_retries=0,
        wall_time_seconds=0.0,
    )

    def fake_run(*args, **kwargs):
      return fake

    original = run_lib.run
    run_lib.run = fake_run
    try:
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True, runs=('R1',))
    finally:
      run_lib.run = original
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNotNone(report['stop'])
    self.assertEqual(report['stop']['run'], 'R1')
    self.assertIn('mismatch', report['stop']['error'])

  def test_offline_stage_never_reads_env(self):
    # offline 分支绝不调用 _load_settings：将其替换为抛错，仍须成功。
    batch = self._frozen_batch()

    def forbidden(*args, **kwargs):
      raise AssertionError('_load_settings must not run offline')

    original = s3._load_settings  # pylint: disable=protected-access
    s3._load_settings = forbidden  # pylint: disable=protected-access
    try:
      s3.run_stage(batch, offline=True)
    finally:
      s3._load_settings = original  # pylint: disable=protected-access
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(report['counts']['finished_runs'], 2)

  def test_load_settings_validates_model_and_sets_openai_env(self):
    # _load_settings 从给定路径读 .env（测试永不读真实 .env）：
    # AI_MODEL_4 与冻结模型名不符 → BatchStop；相符 → 返回凭据并
    # setdefault OPENAI_* 环境变量。
    env = pathlib.Path(self.enterContext(_tmp_dir())) / '.env'
    env.write_text(
        'AI_MODEL_4=wrong-model\n'
        'AI_API_KEY=k-secret\n'
        'AI_BASE_URL=https://example.invalid/v1\n',
        encoding='utf-8',
    )
    with self.assertRaises(s3.BatchStop):
      s3._load_settings(env)  # pylint: disable=protected-access
    env.write_text(
        'AI_MODEL_4=gpt-6.1-sol\n'
        'AI_API_KEY=k-secret\n'
        'AI_BASE_URL=https://example.invalid/v1\n',
        encoding='utf-8',
    )
    with mock.patch.dict(os.environ):
      os.environ.pop('OPENAI_API_KEY', None)
      os.environ.pop('OPENAI_BASE_URL', None)
      api_key, api_base, secrets = s3._load_settings(env)  # pylint: disable=protected-access
      self.assertEqual(api_key, 'k-secret')
      self.assertEqual(api_base, 'https://example.invalid/v1')
      self.assertIn('k-secret', secrets)
      self.assertEqual(os.environ['OPENAI_API_KEY'], 'k-secret')
      self.assertEqual(
          os.environ['OPENAI_BASE_URL'], 'https://example.invalid/v1'
      )


class SummarizeStageTest(unittest.TestCase):
  """Summarize stage: offline cross-check, disclosure, no pass gate."""

  def _prepared_batch(self) -> pathlib.Path:
    """A frozen batch whose two runs executed offline to completion."""
    path = pathlib.Path(self.enterContext(_tmp_dir())) / 'batch'
    s3.freeze_stage(path)
    s3.run_stage(path, offline=True)
    return path

  def test_summary_cross_checks_ledger_against_production(self):
    batch = self._prepared_batch()
    summary = s3.summarize_stage(batch)
    self.assertEqual(sorted(summary['runs']), ['R1', 'R2'])
    for entry in summary['runs'].values():
      self.assertEqual(entry['ledger_attempts'], entry['production_attempts'])
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(summary['integrity']['by_run'], report['counts']['by_run'])
    self.assertEqual(
        summary['integrity']['ledger_attempts'],
        report['counts']['ledger_attempts'],
    )
    self.assertEqual(summary['integrity']['pending_slots'], [])
    # SDK 层分列重建：离线 stub 无 SDK 面，零 create，上限在册。
    sdk = summary['integrity']['sdk_ledger']
    self.assertEqual(sdk['ledger_attempts'], 0)
    self.assertEqual(sdk['by_run'], {'R1': 0, 'R2': 0})
    self.assertEqual(sdk['pending_slots'], [])
    self.assertEqual(sdk['cap_per_run'], 400)
    self.assertIsNone(summary['integrity']['stop'])

  def test_tampered_summary_fails_audit(self):
    batch = self._prepared_batch()
    summary_path = batch / 'R1' / 'summary.json'
    data = json.loads(summary_path.read_text())
    data['used_model_attempts'] += 1
    summary_path.write_text(json.dumps(data, ensure_ascii=False))
    with self.assertRaises(rl.LedgerAuditError):
      s3.summarize_stage(batch)
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNone(report['summary'])
    self.assertIsNotNone(report.get('summary_error'))

  def test_missing_summary_records_no_summary(self):
    # 未执行的 run 不是错误：runs 记 no_summary；已执行 run 照常核对。
    batch = self._prepared_batch()
    (batch / 'R2' / 'summary.json').unlink()
    summary = s3.summarize_stage(batch)
    self.assertEqual(summary['runs']['R2'], {'status': 'no_summary'})
    self.assertEqual(
        summary['runs']['R1']['ledger_attempts'],
        summary['runs']['R1']['production_attempts'],
    )

  def test_dependency_drift_lists_intersection_differences(self):
    # S2 同型：合成 reference snapshot，交集文件改一字节 → 清单含之。
    batch = self._prepared_batch()
    no_reference = s3.summarize_stage(batch)
    self.assertEqual(
        no_reference['dependency_drift_vs_reference'], 'not compared'
    )
    current = s3.source_snapshot_payload()['files']
    reference = {'files': [dict(entry) for entry in current]}
    reference['files'][0]['sha256'] = '0' * 64  # 模拟一字节之差
    reference_path = batch.parent / 'reference_snapshot.json'
    reference_path.write_text(
        json.dumps(reference, ensure_ascii=False), encoding='utf-8'
    )
    summary = s3.summarize_stage(batch, reference_snapshot=reference_path)
    drift = summary['dependency_drift_vs_reference']
    self.assertEqual([entry['path'] for entry in drift], [current[0]['path']])
    self.assertEqual(drift[0]['reference_sha256'], '0' * 64)
    self.assertEqual(drift[0]['current_sha256'], current[0]['sha256'])

  def test_summary_has_no_pass_gate(self):
    batch = self._prepared_batch()
    summary = s3.summarize_stage(batch)
    self.assertNotIn('rev_gate', summary)
    self.assertNotIn(
        'overall_pass', json.loads((batch / 'report.json').read_text())
    )


class DryRunStageTest(unittest.TestCase):
  """Dry run: the full freeze → run → summarize pipeline, zero network."""

  def test_dry_run_end_to_end_with_zero_network(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.dry_run_stage(path)
    report = json.loads((path / 'dry_run_report.json').read_text())
    self.assertTrue(report['ok'])
    self.assertEqual(sorted(report['runs']), ['R1', 'R2'])
    for entry in report['runs'].values():
      self.assertEqual(entry['ledger_attempts'], entry['production_attempts'])
      self.assertLessEqual(entry['ledger_attempts'], 400)
    # 每个账本 attempt 都有留证 prompt。
    ledger_lines = (path / 'requests.jsonl').read_text().splitlines()
    started = [
        json.loads(l)
        for l in ledger_lines
        if json.loads(l)['event'] == 'started'
    ]
    for event in started:
      self.assertTrue(
          (path / 'raw' / f"{event['slot_id']}.prompt.txt").exists()
      )
    # 冻结 §3 表实际生效：两运行的 checkpoint artifact 中
    # director_params 逐键等于冻结表（显式 run_config，非默认值）。
    for run_id in ('R1', 'R2'):
      envelope = json.loads(
          (path / run_id / 'checkpoint.json').read_text(encoding='utf-8')
      )
      effective = envelope['run_config']['director_params']
      self.assertEqual(
          {key: effective[key] for key in s3._FROZEN_DIRECTOR_PARAMS},  # pylint: disable=protected-access
          dict(s3._FROZEN_DIRECTOR_PARAMS),  # pylint: disable=protected-access
      )
    # SDK 层证据：离线零 create（stub 无 SDK 面）、上限在册且未破。
    self.assertEqual(report['sdk']['by_run'], {'R1': 0, 'R2': 0})
    self.assertEqual(report['sdk']['attempts'], 0)
    self.assertEqual(report['sdk']['cap_per_run'], 400)
    passed = {entry['check']: entry['passed'] for entry in report['invariants']}
    self.assertTrue(passed['director_params_match_freeze'])
    self.assertTrue(passed['sdk_ledger_within_caps'])
    self.assertTrue(passed['offline_sdk_zero_creates'])
    self.assertTrue(passed['offline_stub_requests_only'])

  def test_dry_run_refuses_existing_dir(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.dry_run_stage(path)
    with self.assertRaises(s3.BatchStop):
      s3.dry_run_stage(path)


if __name__ == '__main__':
  unittest.main()
