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

"""Offline tests for world_response_rev_probe (Tasks 5-7).

The fixture tests cover the eight ``RevCase`` entries, the field-level
``REVISION_RECORD`` and the ``FIXTURE_EVENT_SOURCES`` provenance notes
(Task 5). The slot-discipline tests cover the Task 6 runtime machinery:
the frozen generation slot spends nothing, the one real review slot is
guarded against the adapter's hidden retry loop, the prompt is
persisted before the call and the raw response before parsing, and the
provider boundary is archived through the request ledger.

The run-stage tests cover the Task 7 resume, stop and failure
classification rules: hash re-verification of the frozen artifacts,
completed/sample_failure attempts skipped on resume, pending
started-no-result slots refusing re-entry unless the user authorizes
the exact slot list (then booked as a scoped deviation, never resent),
budget-full stopping before any provider call, and the failure split
(review rejection / malformed generation are sample failures; provider,
budget, plan and unknown errors stop the batch).

Everything here is deterministic and offline: the inner model is the
real OpenAI-compatible adapter mounted on a scripted fake client (the
old batch's ``_Fake*`` pattern), so no network, API keys, ``.env``
reads or ``run_results`` access ever happen in CI. The byte-exact
inheritance from the frozen 2026-10-20 batch is verified by a separate
local one-off script, not by these tests.
"""

import hashlib
import json
import pathlib
import tempfile
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.language_models.openai import base_gpt_model
from concordia.language_model import language_model

from examples.story_enactment import request_ledger as rl
from examples.story_enactment import world_response_rev_probe as probe


class FixturesTest(unittest.TestCase):

  def test_eight_cases_in_order_with_expected_split(self):
    ids = [c.sample_id for c in probe.CASES]
    self.assertEqual(ids, [f'REV0{i}' for i in range(1, 9)])
    self.assertEqual(
        [c.expected for c in probe.CASES], ['true', 'true'] + ['false'] * 6
    )

  def test_revision_record_lists_field_level_diffs(self):
    rev01 = probe.REVISION_RECORD['REV01']
    self.assertIn('action_result', rev01['revised'])
    self.assertIn('old', rev01['revised']['action_result'])
    self.assertIn('reason', rev01['revised']['action_result'])
    self.assertIn('speech', rev01['inherited'])
    rev02 = probe.REVISION_RECORD['REV02']
    self.assertIn('stage_directions', rev02['revised'])
    for sid in ('REV03', 'REV04', 'REV05', 'REV06', 'REV07', 'REV08'):
      self.assertEqual(probe.REVISION_RECORD[sid]['revised'], {})
      self.assertIn('action_result', probe.REVISION_RECORD[sid]['inherited'])

  def test_rev01_revision_requirements_hold(self):
    case = probe.sample_by_id('REV01')
    self.assertNotIn('没有别的动静', case.action_result)
    for fragment in ('隔门', '邻居', '远去'):
      self.assertIn(fragment, case.action_result)
    for fragment in ('周律师走到门边', '把话说完', '邻居', '远去'):
      self.assertIn(fragment, case.stage_directions)
    revision = probe.REVISION_RECORD['REV01']
    self.assertNotIn('stage_directions', revision['inherited'])
    self.assertEqual(
        revision['revised']['stage_directions']['new'], case.stage_directions
    )

  def test_rev02_revision_requirements_hold(self):
    case = probe.sample_by_id('REV02')
    for banned in ('吹干', '折', '收进'):
      self.assertNotIn(banned, case.stage_directions)
    self.assertIn('自称', case.stage_directions)  # 保留归属、不认证

  def test_fixture_sources_are_documented(self):
    self.assertIn('敲门', probe.FIXTURE_EVENT_SOURCES['knock'])


class _FakeResponse:
  """Minimal provider response object (old batch probe.py:506)."""

  def __init__(self, content, model):
    self.model = model
    self.usage = None
    self.choices = [
        type(
            'Choice',
            (),
            {'message': type('Message', (), {'content': content})()},
        )()
    ]


class _FakeCompletions:
  """Scripted ``create`` capturing every received call."""

  def __init__(self, answer, model, on_create=None):
    self._answer = answer
    self._model = model
    self._on_create = on_create
    self.calls = []

  def create(self, *args, **kwargs):
    if self._on_create is not None:
      self._on_create()
    self.calls.append({k: v for k, v in kwargs.items() if k != 'messages'})
    prompt = kwargs['messages'][-1]['content']
    answer = self._answer(prompt) if callable(self._answer) else self._answer
    return _FakeResponse(answer, self._model)


class _FakeChat:
  """Fake ``chat`` facet exposing only ``completions``."""

  def __init__(self, completions):
    self.completions = completions


class _FakeClient:
  """Fake provider client; ``with_options`` is recorded, not applied."""

  def __init__(self, completions):
    self.chat = _FakeChat(completions)
    self.with_options_calls = []

  def with_options(self, **kwargs):
    self.with_options_calls.append(dict(kwargs))
    return self


def _fake_inner(
    answer: str | None = '{"valid": true, "reason": "ok"}',
    on_create: Any = None,
):
  """Builds the real adapter around an offline scripted fake client.

  The inner model is the production ``BaseGPTModel``, so the tests
  exercise its true parsing and its hidden multiple-choice retry loop;
  only the provider client is a fake.
  """
  completions = _FakeCompletions(answer, 'gpt-6.1-sol', on_create=on_create)
  client: Any = _FakeClient(completions)
  inner = base_gpt_model.BaseGPTModel(model_name='gpt-6.1-sol', client=client)
  return inner, completions


class SlotDisciplineTest(unittest.TestCase):
  """Slot-plan discipline of the guarded Sol model (offline, Task 6)."""

  def _ledger(self, batch_dir):
    return rl.RequestLedger(
        batch_dir,
        batch_id='rev-probe-test',
        global_cap=8,
        model_caps={'m4': 8},
        group_caps={'REV': 8},
        sample_caps={'REV': 1},
    )

  def _model(self, ledger, inner):
    return probe.SolSlotModel('gpt-6.1-sol', ledger=ledger, inner=inner)

  def test_slot_plan_and_attempt_id_shape(self):
    case = probe.sample_by_id('REV01')
    s1, s2 = probe.slot_plan(case)
    self.assertEqual(s1.slot_id, 'REV01__m4__s1_generation')
    self.assertEqual(s1.kind, 'frozen')
    self.assertIsNotNone(s1.frozen_text)
    self.assertEqual(
        json.loads(s1.frozen_text or ''),
        {'stage_directions': case.stage_directions},
    )
    self.assertEqual(s2.slot_id, 'REV01__m4__s2_review')
    self.assertEqual(s2.kind, 'real')
    self.assertIsNone(s2.frozen_text)
    self.assertEqual(probe.attempt_id('REV01'), 'REV01__m4')

  def test_frozen_generation_slot_spends_nothing(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      inner, completions = _fake_inner()
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      frozen = model.sample_text('# Candidate event\n', max_tokens=2000)
      self.assertEqual(
          json.loads(frozen)['stage_directions'],
          probe.sample_by_id('REV01').stage_directions,
      )
      self.assertEqual(ledger.counts['global'], 0)
      self.assertEqual(len(completions.calls), 0)
      events = rl.load_events(batch_dir / 'requests.jsonl')
      self.assertEqual(events[0]['event'], 'frozen_stub')
      self.assertEqual(events[0]['slot_id'], 'REV01__m4__s1_generation')

  def test_review_slot_forwards_exactly_once(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      inner, completions = _fake_inner()
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      model.sample_text('# Candidate event\n', max_tokens=2000)
      raw = model.sample_text('# Story resolution semantic review\n')
      self.assertEqual(raw, '{"valid": true, "reason": "ok"}')
      # Generation stayed frozen: exactly one real (review) call.
      self.assertEqual(len(completions.calls), 1)
      self.assertEqual(ledger.counts['global'], 1)
      raw_dir = batch_dir / 'raw'
      self.assertTrue((raw_dir / 'REV01__m4__s2_review.prompt.txt').exists())
      self.assertTrue((raw_dir / 'REV01__m4__s2_review.response.txt').exists())

  def test_prompt_persisted_before_call_raw_response_before_parsing(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      slot = 'REV01__m4__s2_review'
      seen = {}

      def on_create():
        raw_dir = batch_dir / 'raw'
        seen['prompt'] = (raw_dir / f'{slot}.prompt.txt').exists()
        seen['started'] = (raw_dir / f'{slot}.started.json').exists()
        seen['response_yet'] = (raw_dir / f'{slot}.response.txt').exists()

      # A None content makes the real adapter raise InvalidResponseError
      # AFTER create() returned; the guard must have written the raw
      # response file by then (design section 4.2).
      inner, completions = _fake_inner(answer=None, on_create=on_create)
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      model.sample_text('# Candidate event\n', max_tokens=2000)
      with self.assertRaises(language_model.InvalidResponseError):
        model.sample_text('# Story resolution semantic review\n')
      # At provider time the prompt and started marker were on disk,
      # and the response was not yet (it does not exist).
      self.assertTrue(seen['prompt'])
      self.assertTrue(seen['started'])
      self.assertFalse(seen['response_yet'])
      response = batch_dir / 'raw' / f'{slot}.response.txt'
      self.assertTrue(response.exists())
      self.assertEqual(len(completions.calls), 1)
      # The parse failure keeps the slot's unit spent and is recorded.
      self.assertEqual(ledger.counts['global'], 1)
      events = rl.load_events(batch_dir / 'requests.jsonl')
      finished = [e for e in events if e['event'] == 'finished']
      self.assertEqual(finished[0]['status'], 'InvalidResponseError')
      self.assertIn('InvalidResponseError', finished[0]['error'])

  def test_second_request_of_slot_is_blocked_before_provider(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      # The scripted answer never matches an option, so the production
      # sample_choice loop attempts a second request in the same slot.
      inner, completions = _fake_inner(answer='NEITHER_OPTION')
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      model.sample_text('# Candidate event\n', max_tokens=2000)
      with self.assertRaises(probe.SecondRequestBlocked):
        model.sample_choice(
            '# Story resolution semantic review\n', ('true', 'false')
        )
      # Exactly one request ever reached the provider.
      self.assertEqual(len(completions.calls), 1)
      blocked_path = (
          batch_dir / 'raw' / 'REV01__m4__s2_review.second_request_blocked.json'
      )
      self.assertTrue(blocked_path.exists())
      blocked = json.loads(blocked_path.read_text(encoding='utf-8'))
      self.assertEqual(blocked['first_content'], 'NEITHER_OPTION')
      # The slot's one unit stays spent; the block adds no budget.
      self.assertEqual(ledger.counts['global'], 1)
      self.assertEqual(ledger.counts['second_request_blocks'], 1)
      events = rl.load_events(batch_dir / 'requests.jsonl')
      finished = [e for e in events if e['event'] == 'finished']
      self.assertEqual(finished[-1]['status'], 'SecondRequestBlocked')

  def test_boundary_kwargs_are_archived(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      inner, completions = _fake_inner()
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      model.sample_text('# Candidate event\n', max_tokens=2000)
      model.sample_text('# Story resolution semantic review\n')
      self.assertEqual(len(completions.calls), 1)
      events = rl.load_events(batch_dir / 'requests.jsonl')
      finished = [e for e in events if e['event'] == 'finished'][-1]
      boundary = finished['boundary']
      self.assertEqual(boundary['requested_model'], 'gpt-6.1-sol')
      self.assertEqual(boundary['provider_model'], 'gpt-6.1-sol')
      self.assertEqual(boundary['messages_count'], 6)
      self.assertIn('max_completion_tokens', boundary['boundary_kwargs'])
      self.assertNotIn('messages', boundary['boundary_kwargs'])
      self.assertIn('boundary_kwargs_note', boundary)
      self.assertEqual(
          boundary['first_content'], '{"valid": true, "reason": "ok"}'
      )

  def test_calls_beyond_the_plan_raise_plan_exhausted(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      ledger = self._ledger(batch_dir)
      inner, completions = _fake_inner()
      model = self._model(ledger, inner)
      model.begin_attempt(probe.slot_plan(probe.sample_by_id('REV01')))
      model.sample_text('# Candidate event\n', max_tokens=2000)
      model.sample_text('# Story resolution semantic review\n')
      with self.assertRaises(probe.PlanExhausted):
        model.sample_text('# one call beyond the two-slot plan\n')
      # The extra call never reached the provider or the ledger.
      self.assertEqual(len(completions.calls), 1)
      self.assertEqual(ledger.counts['global'], 1)


def _write_json_file(path: pathlib.Path, payload: Any) -> None:
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
  )


def _file_sha256(path: pathlib.Path) -> str:
  return hashlib.sha256(path.read_bytes()).hexdigest()


_APPROVE = '{"valid": true, "reason": "ok"}'
# Deterministic rejection reason for REV03 (no entry history for the
# neighbor, yet the narration brings her inside).
_REJECT_REV03 = json.dumps(
    {
        'valid': False,
        'reason': '邻居没有任何登场历史，旁白却让她进门回应，无来源。',
    },
    ensure_ascii=False,
)


def _scripted_factory(answers: list[str]):
  """Builds an inner-model factory popping scripted review answers.

  Each attempt gets a fresh fake client; exactly one provider ``create``
  happens per attempt (the frozen generation slot never reaches the
  provider), so the shared answer queue is consumed in CASES order.
  """
  completions_log: list[_FakeCompletions] = []

  def factory(model_name: str):
    def answer(prompt):
      del prompt
      return answers.pop(0)

    completions = _FakeCompletions(answer, model_name)
    completions_log.append(completions)
    client: Any = _FakeClient(completions)
    return base_gpt_model.BaseGPTModel(model_name=model_name, client=client)

  return factory, completions_log


def _provider_calls(completions_log: list[Any]) -> int:
  return sum(len(calls.calls) for calls in completions_log)


class RunStageTest(unittest.TestCase):
  """Resume, stop rules and failure classification of run_stage."""

  def _prepared_batch(self, tmp: str) -> pathlib.Path:
    """Writes the minimal frozen-batch equivalent (Task 8 owns freeze)."""
    batch_dir = pathlib.Path(tmp) / 'batch'
    batch_dir.mkdir()
    _write_json_file(batch_dir / 'protocol.json', {'batch': 'minimal'})
    _write_json_file(
        batch_dir / 'fixtures.json',
        {'cases': [case.sample_id for case in probe.CASES]},
    )
    _write_json_file(
        batch_dir / 'source_snapshot.json',
        {
            'files': [
                {
                    'path': str(batch_dir / name),
                    'sha256': _file_sha256(batch_dir / name),
                    'bytes': (batch_dir / name).stat().st_size,
                }
                for name in ('protocol.json', 'fixtures.json')
            ]
        },
    )
    _write_json_file(
        batch_dir / 'report.json',
        {
            'counts': {
                'planned_attempts': 8,
                'executed_attempts': 0,
                'real_calls': 0,
                'by_model': {},
                'by_group': {},
                'second_request_blocks': 0,
            },
            'attempts': {},
            'stop': None,
            'deviations': [],
            'protocol_sha256': _file_sha256(batch_dir / 'protocol.json'),
            'fixtures_sha256': _file_sha256(batch_dir / 'fixtures.json'),
            'source_snapshot_sha256': _file_sha256(
                batch_dir / 'source_snapshot.json'
            ),
        },
    )
    return batch_dir

  def _report(self, batch_dir: pathlib.Path) -> dict[str, Any]:
    return json.loads((batch_dir / 'report.json').read_text(encoding='utf-8'))

  def _seed_ledger(self, batch_dir: pathlib.Path) -> rl.RequestLedger:
    return rl.RequestLedger(
        batch_dir,
        batch_id='rev-probe-test',
        global_cap=probe._GLOBAL_CAP,
        model_caps=probe._MODEL_CAPS,
        group_caps=probe._GROUP_CAPS,
        sample_caps=probe._SAMPLE_CAPS,
    )

  def test_positive_and_negative_verdicts_classify(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      answers = [_APPROVE, _APPROVE, _REJECT_REV03] + [_APPROVE] * 5
      factory, log = _scripted_factory(answers)
      probe.run_stage(batch_dir, inner_factory=factory)
      report = self._report(batch_dir)
      rev01 = report['attempts']['REV01__m4']
      self.assertEqual(rev01['status'], 'completed')
      self.assertEqual(rev01['outputs']['review_outcome'], 'approved')
      rev03 = report['attempts']['REV03__m4']
      self.assertEqual(rev03['status'], 'sample_failure')
      self.assertEqual(rev03['outputs']['failure_kind'], 'review_rejected')
      # Exactly one provider call per sample; no retries.
      self.assertEqual([len(c.calls) for c in log], [1] * 8)
      self.assertEqual(report['counts']['real_calls'], 8)
      self.assertEqual(report['counts']['by_model'], {'m4': 8})
      self.assertIsNone(report['stop'])
      self.assertEqual(report['deviations'], [])

  def test_format_error_is_sample_failure_not_batch_stop(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      answers = ['not a json verdict'] + [_APPROVE] * 7
      factory, _ = _scripted_factory(answers)
      probe.run_stage(batch_dir, inner_factory=factory)
      report = self._report(batch_dir)
      rev01 = report['attempts']['REV01__m4']
      self.assertEqual(rev01['status'], 'sample_failure')
      self.assertEqual(rev01['outputs']['failure_kind'], 'malformed_generation')
      self.assertIsNone(report['stop'])
      # The malformed review was still started, billed and never resent.
      events = rl.load_events(batch_dir / 'requests.jsonl')
      counts = rl.rebuild_counts(events)
      self.assertEqual(counts['by_sample']['REV01__m4'], 1)
      self.assertEqual(counts['global'], 8)

  def test_api_error_books_budget_and_stops_batch(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)

      def raising_factory(model_name: str):
        def on_create():
          raise RuntimeError('provider unavailable')

        completions = _FakeCompletions(
            'unused', model_name, on_create=on_create
        )
        client: Any = _FakeClient(completions)
        return base_gpt_model.BaseGPTModel(model_name=model_name, client=client)

      with self.assertRaises(SystemExit):
        probe.run_stage(batch_dir, inner_factory=raising_factory)
      report = self._report(batch_dir)
      self.assertEqual(report['attempts']['REV01__m4']['status'], 'batch_stop')
      self.assertIsNotNone(report['stop'])
      self.assertEqual(report['stop']['attempt'], 'REV01__m4')
      events = rl.load_events(batch_dir / 'requests.jsonl')
      started = [e for e in events if e['event'] == 'started']
      self.assertEqual(
          [e['slot_id'] for e in started], ['REV01__m4__s2_review']
      )
      # The observed provider error is archived on the started slot and
      # its unit stays billed (never refunded).
      finished = [e for e in events if e['event'] == 'finished']
      self.assertEqual(finished[-1]['status'], 'RuntimeError')
      self.assertEqual(rl.rebuild_counts(events)['global'], 1)
      # Re-entry after a recorded batch_stop is refused outright.
      before = len(rl.load_events(batch_dir / 'requests.jsonl'))
      with self.assertRaises(SystemExit):
        probe.run_stage(batch_dir, inner_factory=raising_factory)
      self.assertEqual(
          len(rl.load_events(batch_dir / 'requests.jsonl')), before
      )

  def test_pending_slot_blocks_resume_until_authorized_skip(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      seed = self._seed_ledger(batch_dir)
      seed.begin_request(
          slot_id='REV05__m4__s2_review',
          sample_id='REV05',
          group='REV',
          model_tag='m4',
          model_name='gpt-6.1-sol',
          attempt_id='REV05__m4',
          prompt='seeded started-no-result review',
          passed_params={},
      )
      factory, log = _scripted_factory([_APPROVE] * 8)
      with self.assertRaises(SystemExit):
        probe.run_stage(batch_dir, inner_factory=factory)
      # Refused before any new ledger entry or provider call.
      self.assertEqual(len(rl.load_events(batch_dir / 'requests.jsonl')), 1)
      self.assertEqual(_provider_calls(log), 0)
      # Authorized skip: the pending sample becomes a scoped deviation.
      probe.run_stage(
          batch_dir,
          authorize_skip_pending=['REV05__m4__s2_review'],
          inner_factory=factory,
      )
      report = self._report(batch_dir)
      rev05 = report['attempts']['REV05__m4']
      self.assertEqual(rev05['status'], 'sample_failure')
      self.assertIn('user-authorized deviation', rev05['failure'])
      self.assertEqual(
          report['deviations'][0]['slots'], ['REV05__m4__s2_review']
      )
      # The pending slot was never resent: still exactly one started.
      events = rl.load_events(batch_dir / 'requests.jsonl')
      resent = [
          e
          for e in events
          if e['event'] == 'started' and e['slot_id'] == 'REV05__m4__s2_review'
      ]
      self.assertEqual(len(resent), 1)
      # The other seven samples continued, one call each.
      self.assertEqual(_provider_calls(log), 7)
      self.assertEqual(len(report['attempts']), 8)
      completed = [
          key
          for key, attempt in report['attempts'].items()
          if attempt['status'] == 'completed'
      ]
      self.assertEqual(len(completed), 7)
      self.assertEqual(report['counts']['real_calls'], 8)

  def test_budget_full_stops_before_sending(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      seed = rl.RequestLedger(
          batch_dir,
          batch_id='rev-probe-test',
          global_cap=8,
          model_caps={'m4': 8},
          group_caps={},
          sample_caps={},
      )
      for index in range(1, 9):
        slot = f'FILL{index:02d}__m4__s2_review'
        seed.begin_request(
            slot_id=slot,
            sample_id=f'FILL{index:02d}',
            group='FILL',
            model_tag='m4',
            model_name='gpt-6.1-sol',
            attempt_id=f'FILL{index:02d}__m4',
            prompt='filler',
            passed_params={},
        )
        seed.finish_request(slot_id=slot, status='completed')
      answers = [_APPROVE] * 8
      factory, log = _scripted_factory(answers)
      with self.assertRaises(SystemExit):
        probe.run_stage(batch_dir, inner_factory=factory)
      # Stopped before any provider call; no scripted answer consumed.
      self.assertEqual(_provider_calls(log), 0)
      self.assertEqual(answers, [_APPROVE] * 8)
      events = rl.load_events(batch_dir / 'requests.jsonl')
      blocked = [e for e in events if e['event'] == 'budget_blocked']
      self.assertEqual(len(blocked), 1)
      self.assertEqual(blocked[0]['level'], 'global')
      report = self._report(batch_dir)
      self.assertEqual(report['attempts']['REV01__m4']['status'], 'batch_stop')
      self.assertIsNotNone(report['stop'])

  def test_completed_attempts_are_skipped_on_resume(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      report = self._report(batch_dir)
      report['attempts']['REV01__m4'] = {
          'sample_id': 'REV01',
          'model_tag': 'm4',
          'group': 'REV',
          'planned_slots': [
              'REV01__m4__s1_generation',
              'REV01__m4__s2_review',
          ],
          'real_slots': ['REV01__m4__s2_review'],
          'status': 'completed',
          'outputs': {'review_outcome': 'approved'},
          'skipped_slots': [],
          'failure': None,
          'wall_seconds': 0.0,
      }
      _write_json_file(batch_dir / 'report.json', report)
      factory, log = _scripted_factory([_APPROVE] * 7)
      probe.run_stage(batch_dir, inner_factory=factory)
      # Only REV02-08 executed, one provider call each.
      self.assertEqual(len(log), 7)
      self.assertEqual([len(c.calls) for c in log], [1] * 7)
      final = self._report(batch_dir)
      self.assertEqual(final['attempts']['REV01__m4']['status'], 'completed')
      self.assertEqual(len(final['attempts']), 8)
      self.assertEqual(final['counts']['real_calls'], 7)
      self.assertEqual(final['counts']['executed_attempts'], 8)

  def test_frozen_verify_refuses_drift(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._prepared_batch(tmp)
      fixtures = batch_dir / 'fixtures.json'
      fixtures.write_text(
          fixtures.read_text(encoding='utf-8') + ' ', encoding='utf-8'
      )
      factory, log = _scripted_factory([_APPROVE] * 8)
      with self.assertRaises(SystemExit):
        probe.run_stage(batch_dir, inner_factory=factory)
      # Refused before any ledger entry or provider call.
      self.assertFalse((batch_dir / 'requests.jsonl').exists())
      self.assertEqual(_provider_calls(log), 0)

  def test_load_settings_validates_model_name_and_credentials(self):
    with mock.patch.object(probe, 'dotenv_values', return_value={}):
      with self.assertRaises(probe.BatchStop):
        probe._load_settings()
    wrong = {
        'AI_MODEL_4': 'some-other-model',
        'AI_API_KEY': 'k',
        'AI_BASE_URL': 'u',
    }
    with mock.patch.object(probe, 'dotenv_values', return_value=wrong):
      with self.assertRaises(probe.BatchStop):
        probe._load_settings()
    good = {
        'AI_MODEL_4': 'gpt-6.1-sol',
        'AI_API_KEY': 'k',
        'AI_BASE_URL': 'u',
    }
    with mock.patch.object(probe, 'dotenv_values', return_value=good) as get:
      names, api_key, base_url, secrets = probe._load_settings()
      (env_path,) = get.call_args.args
      self.assertEqual(pathlib.Path(env_path).name, '.env')
    self.assertEqual(names, {'m4': 'gpt-6.1-sol'})
    self.assertEqual((api_key, base_url), ('k', 'u'))
    self.assertEqual(secrets, ['k', 'u'])


class FreezeStageTest(unittest.TestCase):
  """Frozen payloads and the freeze stage (Task 8)."""

  def _load(self, path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))

  def test_freeze_refuses_existing_dir_and_writes_payloads(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      with mock.patch('openai.OpenAI') as fake_client:
        probe.freeze_stage(batch_dir)
        fake_client.assert_not_called()
      for name in (
          'protocol.json',
          'fixtures.json',
          'source_snapshot.json',
          'report.json',
      ):
        self.assertTrue((batch_dir / name).exists(), name)
      protocol = self._load(batch_dir / 'protocol.json')
      self.assertEqual(protocol['batch'], 'sol_rev_revalidation')
      self.assertTrue(protocol['created_utc'])
      fixtures = self._load(batch_dir / 'fixtures.json')
      self.assertEqual(len(fixtures['samples']), 8)
      self.assertIn('revision_record', fixtures)
      self.assertIn('fixture_event_sources', fixtures['shared'])
      report = self._load(batch_dir / 'report.json')
      self.assertEqual(report['models'], {'m4': 'gpt-6.1-sol'})
      self.assertEqual(report['counts']['planned_attempts'], 8)
      self.assertEqual(report['counts']['real_calls'], 0)
      self.assertEqual(report['attempts'], {})
      self.assertIsNone(report['summary'])
      self.assertIsNone(report['overall_pass'])
      for name, key in (
          ('protocol.json', 'protocol_sha256'),
          ('fixtures.json', 'fixtures_sha256'),
          ('source_snapshot.json', 'source_snapshot_sha256'),
      ):
        self.assertEqual(report[key], _file_sha256(batch_dir / name), name)
      # The frozen payloads satisfy the run stage's hash verification and
      # the real run path works on top of them.
      factory, log = _scripted_factory([_APPROVE] * 8)
      probe.run_stage(batch_dir, inner_factory=factory)
      self.assertEqual(_provider_calls(log), 8)
      final = self._load(batch_dir / 'report.json')
      self.assertEqual(final['counts']['real_calls'], 8)
      # Refreeze over the existing directory is refused.
      with self.assertRaises(probe.BatchStop):
        probe.freeze_stage(batch_dir)

  def test_protocol_declares_sol_only_scope_and_caps(self):
    protocol = probe.protocol_payload()
    self.assertEqual(protocol['model_scope'], 'Sol-only')
    self.assertEqual(protocol['batch'], 'sol_rev_revalidation')
    self.assertEqual(protocol['budgets']['global_requests'], 8)
    self.assertEqual(protocol['budgets']['per_model'], {'m4': 8})
    self.assertEqual(protocol['budgets']['per_group'], {'REV': 8})
    self.assertEqual(protocol['budgets']['per_sample'], {'REV': 1})
    self.assertIn('caps', protocol['budgets']['note'].lower())
    self.assertIn('合法两例接受，非法六例拒绝', protocol['pass_criteria'])
    self.assertIn('格式错误与未决不算通过', protocol['pass_criteria'])
    self.assertEqual(
        protocol['execution_order'],
        [f'REV0{index}__m4' for index in range(1, 9)],
    )
    model = protocol['models']['m4']
    self.assertEqual(model['env_key'], 'AI_MODEL_4')
    self.assertEqual(model['expected_name'], 'gpt-6.1-sol')
    self.assertIn('identity_disclosure', protocol)
    slots = protocol['slots']
    self.assertEqual(sorted(slots), [f'REV0{index}' for index in range(1, 9)])
    for case in probe.CASES:
      entry = slots[case.sample_id]
      self.assertEqual(len(entry['slot_ids']), 2)
      self.assertEqual(
          entry['slot_ids'][0], f'{case.sample_id}__m4__s1_generation'
      )
      self.assertEqual(entry['frozen_slots'], [entry['slot_ids'][0]])
      self.assertEqual(entry['slot_ids'][1], f'{case.sample_id}__m4__s2_review')

  def test_snapshot_covers_probe_and_ledger_and_production(self):
    payload = probe.source_snapshot_payload()
    paths = [entry['path'] for entry in payload['files']]
    for required in (
        'examples/story_enactment/world_response_rev_probe.py',
        'examples/story_enactment/world_response_rev_probe_test.py',
        'examples/story_enactment/request_ledger.py',
        'examples/story_enactment/request_ledger_test.py',
        'examples/story_enactment/fixed_model_probe.py',
        'examples/story_enactment/run.py',
        'examples/story_enactment/story_config.py',
        'concordia/contrib/components/game_master/drama_director.py',
        'concordia/contrib/components/game_master/story_resolution.py',
        'concordia/contrib/language_models/openai/gpt_model.py',
        'concordia/contrib/language_models/openai/base_gpt_model.py',
        'concordia/document/interactive_document.py',
        'concordia/language_model/language_model.py',
        'concordia/agents/entity_agent_with_logging.py',
        (
            'docs/superpowers/specs/'
            '2026-10-21-story-enactment-probe-accounting-and-sol-validation'
            '-design.md'
        ),
        (
            'docs/superpowers/plans/'
            '2026-10-21-story-enactment-probe-accounting-and-sol-validation.md'
        ),
    ):
      self.assertIn(required, paths)
    # Every entry hashes the actual repo bytes at call time.
    for entry in payload['files']:
      path = probe._REPO_ROOT / entry['path']
      self.assertTrue(path.exists(), entry['path'])
      self.assertEqual(entry['sha256'], _file_sha256(path), entry['path'])
      self.assertEqual(entry['bytes'], path.stat().st_size, entry['path'])


class DryRunStageTest(unittest.TestCase):
  """The offline dry run proves paths with zero real clients (Task 8)."""

  def test_dry_run_refuses_existing_dir(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = pathlib.Path(tmp) / 'dry_run'
      dry_run_dir.mkdir()
      with self.assertRaises(probe.BatchStop):
        probe.dry_run_stage(dry_run_dir)

  def test_dry_run_proves_paths_with_zero_real_clients(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = pathlib.Path(tmp) / 'dry_run'
      with mock.patch('openai.OpenAI') as fake_client:
        probe.dry_run_stage(dry_run_dir)
        fake_client.assert_not_called()
      report = json.loads(
          (dry_run_dir / 'dry_run_report.json').read_text(encoding='utf-8')
      )
      self.assertTrue(report['ok'])
      self.assertEqual(report['findings'], [])
      self.assertEqual(len(report['attempts']), 8)
      self.assertEqual(report['provider_calls_total'], 8)
      self.assertEqual(report['runtime_real_calls'], 8)
      # Exactly one frozen generation slot and one real review slot per
      # sample; budgets held at the caps.
      events = rl.load_events(dry_run_dir / 'requests.jsonl')
      frozen = [e['slot_id'] for e in events if e['event'] == 'frozen_stub']
      started = [e['slot_id'] for e in events if e['event'] == 'started']
      self.assertEqual(len(frozen), 8)
      self.assertEqual(len(started), 8)
      for case in probe.CASES:
        self.assertIn(f'{case.sample_id}__m4__s1_generation', frozen)
        self.assertIn(f'{case.sample_id}__m4__s2_review', started)
        attempt = next(
            entry
            for entry in report['attempts']
            if entry['attempt'] == probe.attempt_id(case.sample_id)
        )
        self.assertEqual(attempt['status'], 'completed')
        self.assertEqual(attempt['provider_calls'], 1)
      # Sixteen archived prompts; no label metadata enters any of them.
      prompts = sorted((dry_run_dir / 'raw').glob('*.prompt.txt'))
      self.assertEqual(len(prompts), 16)
      forbidden = ['expected_valid']
      forbidden += [case.sample_id for case in probe.CASES]
      forbidden += [case.title for case in probe.CASES]
      for path in prompts:
        text = path.read_text(encoding='utf-8')
        for needle in forbidden:
          self.assertNotIn(needle, text, f'{path.name}: {needle}')


class SummarizeStageTest(unittest.TestCase):
  """Offline summarization, REV classification and gates (Task 8)."""

  _REJECT = json.dumps(
      {'valid': False, 'reason': '评审dry run拒绝：无来源的越权内容。'},
      ensure_ascii=False,
  )

  def _freeze(self, tmp: str) -> pathlib.Path:
    batch_dir = pathlib.Path(tmp) / 'batch'
    probe.freeze_stage(batch_dir)
    return batch_dir

  def _run(self, batch_dir: pathlib.Path, answers: list[str]) -> None:
    factory, _ = _scripted_factory(answers)
    probe.run_stage(batch_dir, inner_factory=factory)

  def _summarize(
      self, batch_dir: pathlib.Path, *, reference: pathlib.Path | None = None
  ) -> dict[str, Any]:
    with mock.patch('openai.OpenAI') as fake_client:
      probe.summarize_stage(batch_dir, reference_snapshot=reference)
      fake_client.assert_not_called()
    return json.loads((batch_dir / 'report.json').read_text(encoding='utf-8'))

  def _passing_batch(self, tmp: str) -> pathlib.Path:
    batch_dir = self._freeze(tmp)
    self._run(batch_dir, [_APPROVE] * 2 + [self._REJECT] * 6)
    labels = [
        {
            'attempt_id': probe.attempt_id(case.sample_id),
            'annotator_type': 'human',
            'status': 'labeled',
        }
        for case in probe.CASES
    ]
    (batch_dir / 'human_labels.jsonl').write_text(
        ''.join(json.dumps(label) + '\n' for label in labels),
        encoding='utf-8',
    )
    self.assertTrue(self._summarize(batch_dir)['overall_pass'])
    return batch_dir

  def _assert_audit_failure(self, batch_dir: pathlib.Path) -> None:
    with self.assertRaises((rl.LedgerAuditError, probe.BatchStop)):
      self._summarize(batch_dir)
    report = json.loads((batch_dir / 'report.json').read_text(encoding='utf-8'))
    self.assertFalse(report['overall_pass'])  # Never retain a stale pass.
    self.assertIsNone(report['summary'])

  def test_summary_rejects_missing_empty_or_duplicate_ledger(self):
    for change in ('missing', 'empty', 'duplicate'):
      with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
        batch_dir = self._passing_batch(tmp)
        path = batch_dir / 'requests.jsonl'
        lines = path.read_text(encoding='utf-8').splitlines()
        if change == 'missing':
          path.unlink()
        elif change == 'empty':
          path.write_text('', encoding='utf-8')
        else:
          started = next(
              line for line in lines if json.loads(line)['event'] == 'started'
          )
          with path.open('a', encoding='utf-8') as handle:
            handle.write(started + '\n')
        self._assert_audit_failure(batch_dir)

  def test_summary_requires_request_evidence_for_every_result(self):
    for change in ('remove_slot', 'response', 'record', 'frozen'):
      with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
        batch_dir = self._passing_batch(tmp)
        slot = 'REV01__m4__s2_review'
        path = batch_dir / 'requests.jsonl'
        events = rl.load_events(path)
        if change == 'remove_slot':
          events = [event for event in events if event['slot_id'] != slot]
          for evidence in (batch_dir / 'raw').glob(f'{slot}.*'):
            evidence.unlink()
        elif change == 'frozen':
          events = [
              event
              for event in events
              if event['slot_id'] != 'REV01__m4__s1_generation'
          ]
        else:
          suffix = 'response.txt' if change == 'response' else 'record.json'
          (batch_dir / 'raw' / f'{slot}.{suffix}').unlink()
        path.write_text(
            ''.join(json.dumps(event) + '\n' for event in events),
            encoding='utf-8',
        )
        self._assert_audit_failure(batch_dir)

  def test_summary_rejects_duplicate_or_unknown_human_labels(self):
    for change in ('duplicate', 'unknown'):
      with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
        batch_dir = self._passing_batch(tmp)
        path = batch_dir / 'human_labels.jsonl'
        entry = json.loads(path.read_text(encoding='utf-8').splitlines()[0])
        if change == 'unknown':
          entry['attempt_id'] = 'UNKNOWN__m4'
        with path.open('a', encoding='utf-8') as handle:
          handle.write(json.dumps(entry) + '\n')
        self._assert_audit_failure(batch_dir)

  def test_summary_rejects_report_disagreeing_with_raw_verdict(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._passing_batch(tmp)
      path = batch_dir / 'raw/REV01__m4__s2_review.response.txt'
      path.write_text(self._REJECT, encoding='utf-8')
      self._assert_audit_failure(batch_dir)

  def test_expected_rejection_is_passing_negative_and_defects_are_separate(
      self,
  ):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(batch_dir, [_APPROVE, _APPROVE] + [self._REJECT] * 6)
      report = self._summarize(batch_dir)
      summary = report['summary']
      self.assertEqual(summary['rev_classification']['approved'], 2)
      self.assertEqual(summary['rev_classification']['rejected_expectedly'], 6)
      self.assertTrue(summary['rev_gate'])
      self.assertEqual(summary['human_confirmation'], 'pending')
      self.assertFalse(report['overall_pass'])
      self.assertEqual(summary['dependency_drift_vs_reference'], 'not compared')

    # A negative case passing review is a missed detection, not a pass.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(batch_dir, [_APPROVE] * 3 + [self._REJECT] * 5)
      summary = self._summarize(batch_dir)['summary']
      self.assertEqual(summary['rev_classification']['missed_detection'], 1)
      self.assertFalse(summary['rev_gate'])

    # A malformed review is a format error and never passes.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(
          batch_dir,
          [_APPROVE, _APPROVE, self._REJECT, 'not a json verdict']
          + [self._REJECT] * 4,
      )
      summary = self._summarize(batch_dir)['summary']
      self.assertEqual(summary['rev_classification']['format_error'], 1)
      self.assertFalse(summary['rev_gate'])

    # One pending started-no-result slot keeps the gate closed.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      seed = rl.RequestLedger(
          batch_dir,
          batch_id='rev-probe-test',
          global_cap=probe._GLOBAL_CAP,
          model_caps=probe._MODEL_CAPS,
          group_caps=probe._GROUP_CAPS,
          sample_caps=probe._SAMPLE_CAPS,
      )
      seed.begin_request(
          slot_id='REV05__m4__s2_review',
          sample_id='REV05',
          group='REV',
          model_tag='m4',
          model_name='gpt-6.1-sol',
          attempt_id='REV05__m4',
          prompt='seeded started-no-result review',
          passed_params={},
      )
      # REV05 is booked as an authorized deviation; the other seven
      # samples run one call each, in CASES order.
      factory, _ = _scripted_factory([_APPROVE, _APPROVE] + [self._REJECT] * 5)
      probe.run_stage(
          batch_dir,
          authorize_skip_pending=['REV05__m4__s2_review'],
          inner_factory=factory,
      )
      summary = self._summarize(batch_dir)['summary']
      self.assertEqual(summary['rev_classification']['pending'], 1)
      self.assertEqual(summary['rev_classification']['rejected_expectedly'], 5)
      self.assertFalse(summary['rev_gate'])

  def test_integrity_rebuilt_from_ledger(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(batch_dir, [_APPROVE, _APPROVE] + [self._REJECT] * 6)
      # Tamper the recorded counts: summarization must rebuild every
      # number from the ledger, never restate the report's old values.
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      report['counts']['real_calls'] = 999
      report['counts']['by_model'] = {'m4': 999}
      report['counts']['by_group'] = {'REV': 999}
      _write_json_file(batch_dir / 'report.json', report)
      integrity = self._summarize(batch_dir)['summary']['integrity']
      self.assertEqual(integrity['planned_attempts'], 8)
      self.assertEqual(integrity['executed_attempts'], 8)
      self.assertEqual(integrity['real_calls'], 8)
      self.assertEqual(integrity['by_model'], {'m4': 8})
      self.assertEqual(integrity['by_group'], {'REV': 8})
      self.assertEqual(len(integrity['frozen_slots']), 8)
      self.assertEqual(integrity['skipped_slots'], [])
      self.assertEqual(integrity['second_request_blocks'], 0)
      self.assertEqual(integrity['started_no_result'], [])
      self.assertEqual(integrity['retries'], 0)
      self.assertIsNone(integrity['stop_reason'])

  def test_human_confirmation_requires_human_labels(self):
    def label_lines(annotator_type: str) -> str:
      return (
          '\n'.join(
              json.dumps(
                  {
                      'attempt_id': probe.attempt_id(case.sample_id),
                      'annotator_type': annotator_type,
                      'status': 'labeled',
                  },
                  ensure_ascii=False,
              )
              for case in probe.CASES
          )
          + '\n'
      )

    # Agent-authored labels never confirm.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(batch_dir, [_APPROVE, _APPROVE] + [self._REJECT] * 6)
      (batch_dir / 'human_labels.jsonl').write_text(
          label_lines('agent'), encoding='utf-8'
      )
      report = self._summarize(batch_dir)
      self.assertEqual(report['summary']['human_confirmation'], 'pending')
      self.assertFalse(report['overall_pass'])

    # All eight human labels confirm a passing gate.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      self._run(batch_dir, [_APPROVE, _APPROVE] + [self._REJECT] * 6)
      (batch_dir / 'human_labels.jsonl').write_text(
          label_lines('human'), encoding='utf-8'
      )
      report = self._summarize(batch_dir)
      self.assertEqual(report['summary']['human_confirmation'], 'confirmed')
      self.assertTrue(report['summary']['rev_gate'])
      self.assertTrue(report['overall_pass'])

  def test_dependency_drift_lists_changed_intersection(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._freeze(tmp)
      current = probe.source_snapshot_payload()
      target = 'examples/story_enactment/request_ledger.py'
      reference = {
          'files': [
              dict(entry, sha256='0' * 64)
              if entry['path'] == target
              else dict(entry)
              for entry in current['files']
          ]
      }
      reference_path = pathlib.Path(tmp) / 'reference_snapshot.json'
      _write_json_file(reference_path, reference)
      summary = self._summarize(batch_dir, reference=reference_path)['summary']
      drift = summary['dependency_drift_vs_reference']
      self.assertEqual([entry['path'] for entry in drift], [target])
      self.assertEqual(drift[0]['reference_sha256'], '0' * 64)
      real = next(
          entry for entry in current['files'] if entry['path'] == target
      )
      self.assertEqual(drift[0]['current_sha256'], real['sha256'])
      # Without a reference snapshot the comparison is recorded as not
      # performed, never as an empty (all-equal) list.
      other = self._summarize(batch_dir)['summary']
      self.assertEqual(other['dependency_drift_vs_reference'], 'not compared')


if __name__ == '__main__':
  unittest.main()
