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

"""Tests for request_ledger (offline, stdlib only)."""

import hashlib
import json
import pathlib
import tempfile
import unittest

from examples.story_enactment import request_ledger as rl


def _tmp_dir():
  """Returns a context manager yielding a fresh temporary directory path."""
  return tempfile.TemporaryDirectory()


def _started(slot_id, model_tag='m4', model_name='gpt-6.1-sol'):
  sample_id = slot_id.split('__', 1)[0]
  group = ''.join(c for c in sample_id if c.isalpha())
  return {
      'event': 'started',
      'slot_id': slot_id,
      'sample_id': sample_id,
      'group': group,
      'model_tag': model_tag,
      'model_name': model_name,
      'stage': 'real_request',
      'request_started_at_utc': '2026-10-20T09:00:00Z',
      'budgets_before': {'global': 0, 'model': 0, 'group': 0, 'sample': 0},
      'budget_caps': {'global': 94, 'model': 47, 'group': 24, 'sample': 4},
      'prompt_sha256': hashlib.sha256(b'prompt-text').hexdigest(),
      'passed_params': {},
  }


def _finished(slot_id, status='completed', model_tag='m4'):
  e = _started(slot_id, model_tag=model_tag)
  return {
      'event': 'finished',
      'slot_id': slot_id,
      'sample_id': e['sample_id'],
      'group': e['group'],
      'model_tag': model_tag,
      'status': status,
      'error': None,
      'boundary': {},
      'finished_at_utc': '2026-10-20T09:00:01Z',
  }


def _write_ledger(path: pathlib.Path, events):
  path.write_text(
      ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events),
      encoding='utf-8',
  )


_STAGES = {
    'ACT': (
        'situation_perception',
        'self_perception',
        'person_by_situation',
        'action_output',
    ),
    'CHAIN': ('adjudication', 'generation', 'review'),
    'REV': ('generation', 'review'),
    'REP': ('judgment', 'suggestion', 'merge'),
}
_NO_BRANCH_STOPPED = {('REP02', 'm4'), ('REP03', 'm4'), ('REP03', 'm1')}
_SRB_SLOTS = {'REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'}
_MODEL_NAMES = {'m4': 'gpt-6.1-sol', 'm1': 'gemini-3.8-flash-high'}


def _batch_shaped_events():
  """Replicates the 2026-10-20 batch execution shape.

  m4 runs before m1 per sample; ACT03__m1 stops after s2 is persisted as
  started only (pending, later slots never ran); the two REP m1 judgment
  slots hit SecondRequestBlocked and their downstream stages never
  started; REP "No" branch s2/s3 never started; REV generation slots
  were only ever frozen_stub.
  """
  events = []
  order = [
      sid
      for sid in (
          'ACT01',
          'ACT02',
          'ACT03',
          'CHAIN01',
          'CHAIN02',
          'CHAIN03',
          'CHAIN04',
          'CHAIN05',
          'CHAIN06',
          'REV01',
          'REV02',
          'REV03',
          'REV04',
          'REV05',
          'REV06',
          'REV07',
          'REV08',
          'REP01',
          'REP02',
          'REP03',
      )
  ]
  for sample_id in order:
    group = ''.join(c for c in sample_id if c.isalpha())
    for tag in ('m4', 'm1'):
      stages = _STAGES[group]
      for i, stage in enumerate(stages, start=1):
        slot = f'{sample_id}__{tag}__s{i}_{stage}'
        if group == 'REV' and stage == 'generation':
          events.append({
              'event': 'frozen_stub',
              'slot_id': slot,
              'sample_id': sample_id,
              'status': 'frozen_stub',
          })
          continue
        if (sample_id, tag) in _NO_BRANCH_STOPPED and i > 1:
          continue
        events.append(_started(slot, tag, _MODEL_NAMES[tag]))
        if slot == 'ACT03__m1__s2_self_perception':
          # Crash after persisting started: later slots of this arm never
          # ran, leaving this slot pending.
          break
        if slot in _SRB_SLOTS:
          events.append({
              'event': 'second_request_blocked',
              'slot_id': slot,
              'first_content_sha256': '1' * 64,
          })
          events.append(
              _finished(slot, status='SecondRequestBlocked', model_tag=tag)
          )
          # A blocked judgment stops the sample's remaining stages.
          break
        events.append(_finished(slot, model_tag=tag))
  return events


class LoadEventsTest(unittest.TestCase):

  def test_loads_jsonl_events_and_skips_blank_lines(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    events = [_started('A01__m4__s1_x'), _finished('A01__m4__s1_x')]
    _write_ledger(path / 'requests.jsonl', events)
    with (path / 'requests.jsonl').open('a', encoding='utf-8') as handle:
      handle.write('\n')
    self.assertEqual(rl.load_events(path / 'requests.jsonl'), events)

  def test_truncated_tail_line_is_rejected_with_line_number(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    (path / 'requests.jsonl').write_text(
        json.dumps(_started('A01__m4__s1_x')) + '\n{"event": "star',
        encoding='utf-8',
    )
    with self.assertRaisesRegex(rl.LedgerAuditError, 'line 2'):
      rl.load_events(path / 'requests.jsonl')


class AuditEventsTest(unittest.TestCase):

  def test_unknown_event_type_is_rejected(self):
    events = [{'event': 'mystery', 'slot_id': 'A01__m4__s1_x'}]
    with self.assertRaisesRegex(rl.LedgerAuditError, 'unknown'):
      rl.audit_events(events)

  def test_duplicate_started_of_same_slot_is_rejected(self):
    events = [_started('A01__m4__s1_x'), _started('A01__m4__s1_x')]
    with self.assertRaisesRegex(rl.LedgerAuditError, 'duplicate'):
      rl.audit_events(events)

  def test_finished_without_started_is_rejected(self):
    with self.assertRaisesRegex(rl.LedgerAuditError, 'prior started'):
      rl.audit_events([_finished('A01__m4__s1_x')])

  def test_second_request_blocked_without_started_is_rejected(self):
    events = [{
        'event': 'second_request_blocked',
        'slot_id': 'P01__m1__s1_judgment',
        'first_content_sha256': '1' * 64,
    }]
    with self.assertRaisesRegex(rl.LedgerAuditError, 'prior started'):
      rl.audit_events(events)

  def test_same_slot_started_under_two_model_names_is_rejected(self):
    events = [
        _started('A01__m4__s1_x', model_name='model-a'),
        _started('A01__m4__s1_x', model_name='model-b'),
    ]
    with self.assertRaisesRegex(rl.LedgerAuditError, 'model_name'):
      rl.audit_events(events)

  def test_started_missing_required_field_is_rejected(self):
    event = _started('A01__m4__s1_x')
    del event['model_name']
    with self.assertRaisesRegex(rl.LedgerAuditError, 'model_name'):
      rl.audit_events([event])

  def test_orphan_sidecar_started_file_is_reported(self):
    # raw/<slot>.started.json 存在但账本无对应 started 事件 → LedgerAuditError
    raw_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    (raw_dir / 'A01__m4__s1_x.started.json').write_text('{}', encoding='utf-8')
    with self.assertRaisesRegex(rl.LedgerAuditError, 'orphan'):
      rl.audit_events([], raw_dir=raw_dir)

  def test_missing_sidecar_for_started_is_rejected_when_raw_dir_given(self):
    raw_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    with self.assertRaisesRegex(rl.LedgerAuditError, 'no raw sidecar'):
      rl.audit_events([_started('A01__m4__s1_x')], raw_dir=raw_dir)

  def test_matching_sidecar_passes_audit(self):
    raw_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    event = _started('A01__m4__s1_x')
    (raw_dir / 'A01__m4__s1_x.started.json').write_text(
        json.dumps(
            {key: value for key, value in event.items() if key != 'event'}
        ),
        encoding='utf-8',
    )
    (raw_dir / 'A01__m4__s1_x.prompt.txt').write_text(
        'prompt-text', encoding='utf-8'
    )
    result = rl.audit_events([event], raw_dir=raw_dir)
    self.assertEqual(result['pending_slots'], ['A01__m4__s1_x'])
    self.assertEqual(result['warnings'], [])

  def test_pending_slot_is_reported_not_rejected(self):
    result = rl.audit_events([_started('A01__m4__s1_x')])
    self.assertEqual(result['pending_slots'], ['A01__m4__s1_x'])

  def test_only_offline_closeout_can_report_missing_pending_prompt(self):
    raw_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    event = _started('A01__m4__s1_x')
    (raw_dir / 'A01__m4__s1_x.started.json').write_text(
        json.dumps(
            {key: value for key, value in event.items() if key != 'event'}
        ),
        encoding='utf-8',
    )
    with self.assertRaisesRegex(rl.LedgerAuditError, 'prompt'):
      rl.audit_events([event], raw_dir=raw_dir)
    audit = rl.audit_events(
        [event], raw_dir=raw_dir, allow_missing_failure_prompts=True
    )
    self.assertEqual(audit['pending_slots'], [event['slot_id']])
    self.assertEqual(len(audit['warnings']), 1)
    with self.assertRaisesRegex(rl.LedgerAuditError, 'prompt'):
      rl.audit_events(
          [event, _finished(event['slot_id'])],
          raw_dir=raw_dir,
          allow_missing_failure_prompts=True,
      )


class RebuildCountsTest(unittest.TestCase):

  def test_started_without_finish_still_counts_and_finish_never_doubles(self):
    events = [
        _started('A01__m4__s1_x'),
        _finished('A01__m4__s1_x'),
        _started('A02__m4__s1_x'),
    ]
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['global'], 2)
    self.assertEqual(counts['by_model']['m4'], 2)
    self.assertEqual(
        counts['started_slots'], ['A01__m4__s1_x', 'A02__m4__s1_x']
    )

  def test_by_sample_keys_use_sample_and_model_tag(self):
    events = [
        _started('A01__m4__s1_x'),
        _started('A01__m4__s2_y'),
        _started('A01__m1__s1_x', model_tag='m1'),
    ]
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['by_sample'], {'A01__m4': 2, 'A01__m1': 1})

  def test_frozen_skipped_and_blocked_events_consume_nothing(self):
    events = [
        _started('R01__m4__s2_review'),
        _finished('R01__m4__s2_review'),
        {
            'event': 'frozen_stub',
            'slot_id': 'R01__m4__s1_generation',
            'status': 'frozen_stub',
        },
        {
            'event': 'skipped',
            'slot_id': 'R02__m4__s2_review',
            'status': 'skipped_by_upstream_failure',
        },
        {'event': 'second_request_blocked', 'slot_id': 'P01__m1__s1_judgment'},
        {
            'event': 'budget_blocked',
            'slot_id': 'P02__m1__s1_judgment',
            'level': 'global',
            'used': 94,
            'cap': 94,
        },
    ]
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['global'], 1)
    self.assertEqual(counts['finished_slots'], ['R01__m4__s2_review'])
    self.assertEqual(counts['frozen_slots'], ['R01__m4__s1_generation'])
    self.assertEqual(counts['skipped_slots'], ['R02__m4__s2_review'])
    self.assertEqual(counts['second_request_blocks'], 1)


class BatchShapeReproductionTest(unittest.TestCase):
  """设计的 §8：真实重现本批形状，重建得到 ACT=22、Gemini=39、总数=82。"""

  def test_rebuild_reproduces_the_20261020_batch_counts(self):
    events = _batch_shaped_events()
    counts = rl.rebuild_counts(events)
    self.assertEqual(counts['global'], 82)
    self.assertEqual(counts['by_model'], {'m4': 43, 'm1': 39})
    self.assertEqual(
        counts['by_group'], {'ACT': 22, 'CHAIN': 36, 'REV': 16, 'REP': 8}
    )
    audit = rl.audit_events(events)
    self.assertEqual(audit['pending_slots'], ['ACT03__m1__s2_self_perception'])
    self.assertEqual(
        [
            e['slot_id']
            for e in events
            if e['event'] == 'second_request_blocked'
        ],
        ['REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'],
    )
    self.assertEqual(
        len([e for e in events if e['event'] == 'frozen_stub']), 16
    )


class RequestLedgerTest(unittest.TestCase):
  """Writer behavior: budget pre-reservation, evidence timing, resume."""

  def _make(self, path, **caps):
    return rl.RequestLedger(
        path,
        batch_id='test_batch',
        global_cap=caps.get('global', 94),
        model_caps={'m4': caps.get('model', 47), 'm1': caps.get('model', 47)},
        group_caps={'REV': caps.get('group', 16), 'ACT': 24},
        sample_caps={'REV': 1, 'ACT': 4},
    )

  def _begin(
      self,
      ledger,
      slot_id='R01__m4__s2_review',
      sample_id='R01',
      model_tag='m4',
  ):
    ledger.begin_request(
        slot_id=slot_id,
        sample_id=sample_id,
        group='REV',
        model_tag=model_tag,
        model_name='gpt-6.1-sol',
        attempt_id=f'{sample_id}__{model_tag}',
        prompt='prompt-text',
        passed_params={'max_tokens': 1000},
    )

  def test_budget_check_and_persist_happen_before_any_request(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir)
    ledger.begin_request(  # 计数 0→1，prompt 与 started 先落盘
        slot_id='R01__m4__s2_review',
        sample_id='R01',
        group='REV',
        model_tag='m4',
        model_name='gpt-6.1-sol',
        attempt_id='R01__m4',
        prompt='prompt-text',
        passed_params={'max_tokens': 1000},
    )
    self.assertTrue((raw / 'R01__m4__s2_review.prompt.txt').exists())
    self.assertTrue((raw / 'R01__m4__s2_review.started.json').exists())
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[0]['event'], 'started')
    self.assertEqual(events[0]['batch_id'], 'test_batch')
    self.assertEqual(events[0]['attempt_id'], 'R01__m4')
    self.assertRegex(
        events[0]['request_started_at_utc'],
        r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$',
    )
    sidecar = json.loads(
        (raw / 'R01__m4__s2_review.started.json').read_text(encoding='utf-8')
    )
    self.assertEqual(
        sidecar, {k: v for k, v in events[0].items() if k != 'event'}
    )
    self.assertEqual(ledger.counts['global'], 1)

  def test_over_cap_refuses_before_writing_anything_but_blocked_event(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir, group=1)
    self._begin(ledger)
    with self.assertRaises(rl.LedgerBudgetError):
      self._begin(ledger, slot_id='R02__m4__s2_review', sample_id='R02')
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[-1]['event'], 'budget_blocked')
    self.assertEqual(events[-1]['level'], 'group')
    self.assertFalse((raw / 'R02__m4__s2_review.started.json').exists())
    self.assertFalse((raw / 'R02__m4__s2_review.prompt.txt').exists())
    self.assertEqual(ledger.counts['global'], 1)

  def test_started_without_finish_occupies_budget_across_restart(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._make(batch_dir)
    self._begin(ledger)  # 不 finish（模拟崩溃后重入）
    rebuilt = self._make(batch_dir)
    self.assertEqual(rebuilt.counts['global'], 1)
    self.assertEqual(rebuilt.pending_slots(), ['R01__m4__s2_review'])
    self._begin(rebuilt, slot_id='R02__m4__s2_review', sample_id='R02')
    events = rl.load_events(batch_dir / 'requests.jsonl')
    started2 = [e for e in events if e['event'] == 'started'][1]
    self.assertEqual(started2['budgets_before']['global'], 1)

  def test_missing_ledger_with_started_sidecar_refuses_restart(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    self._begin(self._make(batch_dir))
    (batch_dir / 'requests.jsonl').unlink()
    with self.assertRaisesRegex(rl.LedgerAuditError, 'orphan'):
      self._make(batch_dir)

  def test_resume_rejects_changed_started_metadata(self):
    for field, value in (
        ('model_name', 'another-model'),
        ('sample_id', 'another-sample'),
        ('passed_params', {'max_tokens': 999}),
    ):
      with self.subTest(field=field):
        batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
        self._begin(self._make(batch_dir))
        path = batch_dir / 'raw/R01__m4__s2_review.started.json'
        payload = json.loads(path.read_text(encoding='utf-8'))
        payload[field] = value
        path.write_text(json.dumps(payload), encoding='utf-8')
        with self.assertRaisesRegex(rl.LedgerAuditError, 'sidecar'):
          self._make(batch_dir)

  def test_resume_rejects_missing_or_changed_prompt(self):
    for missing in (False, True):
      with self.subTest(missing=missing):
        batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
        self._begin(self._make(batch_dir))
        path = batch_dir / 'raw/R01__m4__s2_review.prompt.txt'
        if missing:
          path.unlink()
        else:
          path.write_text('different prompt', encoding='utf-8')
        with self.assertRaisesRegex(rl.LedgerAuditError, 'prompt'):
          self._make(batch_dir)

  def test_finish_never_adds_budget(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir)
    self._begin(ledger)
    ledger.record_raw_response('R01__m4__s2_review', 'answer')
    ledger.finish_request(
        slot_id='R01__m4__s2_review',
        status='completed',
        boundary={'provider_model': 'gpt-6.1-sol'},
    )
    self.assertEqual(ledger.counts['global'], 1)
    self.assertEqual(ledger.pending_slots(), [])
    self.assertEqual(
        (raw / 'R01__m4__s2_review.response.txt').read_text(encoding='utf-8'),
        'answer',
    )
    record = json.loads(
        (raw / 'R01__m4__s2_review.record.json').read_text(encoding='utf-8')
    )
    self.assertEqual(record['status'], 'completed')
    self.assertEqual(record['boundary'], {'provider_model': 'gpt-6.1-sol'})

  def test_record_raw_response_json_encodes_non_str_payloads(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir)
    self._begin(ledger)
    ledger.record_raw_response('R01__m4__s2_review', {'choice': 1})
    self.assertEqual(
        (raw / 'R01__m4__s2_review.response.txt').read_text(encoding='utf-8'),
        '{"choice": 1}',
    )

  def test_frozen_and_skipped_and_blocked_add_nothing(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir)
    self._begin(ledger)
    ledger.finish_request(slot_id='R01__m4__s2_review', status='completed')
    ledger.record_frozen_slot(
        slot_id='R01__m4__s1_generation',
        sample_id='R01',
        model_tag='m4',
        attempt_id='R01__m4',
        prompt='frozen prompt',
        frozen_text='{"stage_directions": "stub"}',
    )
    ledger.record_skipped(
        slot_id='R02__m4__s2_review',
        sample_id='R02',
        reason='started_no_result upstream',
    )
    # sample cap REV=1 for (R01, m4) already spent by the started slot.
    with self.assertRaises(rl.LedgerBudgetError):
      self._begin(ledger, slot_id='R01__m4__s3_extra')
    self.assertEqual(ledger.counts['global'], 1)
    self.assertEqual(ledger.counts['frozen_slots'], ['R01__m4__s1_generation'])
    self.assertEqual(ledger.counts['skipped_slots'], ['R02__m4__s2_review'])
    self.assertTrue((raw / 'R01__m4__s1_generation.prompt.txt').exists())
    self.assertTrue((raw / 'R01__m4__s1_generation.response.txt').exists())
    self.assertTrue((raw / 'R01__m4__s1_generation.record.json').exists())
    self.assertFalse((raw / 'R01__m4__s3_extra.started.json').exists())
    events = rl.load_events(batch_dir / 'requests.jsonl')
    frozen = [e for e in events if e['event'] == 'frozen_stub'][0]
    self.assertEqual(frozen['attempt_id'], 'R01__m4')
    self.assertEqual(frozen['status'], 'frozen_stub')
    # The skipped slot keeps a raw record shaped like the old batch's.
    skipped_record = json.loads(
        (raw / 'R02__m4__s2_review.record.json').read_text(encoding='utf-8')
    )
    skipped_event = [e for e in events if e['event'] == 'skipped'][0]
    self.assertEqual(skipped_record['status'], 'skipped_by_upstream_failure')
    self.assertEqual(
        skipped_record, {k: v for k, v in skipped_event.items() if k != 'event'}
    )

  def test_resume_rebuild_matches_ledger_at_all_levels(self):
    # 三条 started（两条 finished、一条 pending）后重建：global=3,
    # by_model m4=2/m1=1, by_group, by_sample 与账本一致；不读
    # report.json，也不接受任何手写恢复值。
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._make(batch_dir)
    self._begin(ledger)
    ledger.finish_request(slot_id='R01__m4__s2_review', status='completed')
    self._begin(ledger, slot_id='R02__m4__s2_review', sample_id='R02')
    ledger.finish_request(slot_id='R02__m4__s2_review', status='parse_error')
    self._begin(
        ledger, slot_id='R03__m1__s2_review', sample_id='R03', model_tag='m1'
    )
    # A decoy report.json with wrong counts must be ignored on resume.
    (batch_dir / 'report.json').write_text(
        json.dumps({'global': 999}), encoding='utf-8'
    )
    rebuilt = self._make(batch_dir)
    self.assertEqual(rebuilt.counts, ledger.counts)
    self.assertEqual(rebuilt.counts['global'], 3)
    self.assertEqual(rebuilt.counts['by_model'], {'m4': 2, 'm1': 1})
    self.assertEqual(rebuilt.counts['by_group'], {'REV': 3})
    self.assertEqual(
        rebuilt.counts['by_sample'],
        {'R01__m4': 1, 'R02__m4': 1, 'R03__m1': 1},
    )
    self.assertEqual(rebuilt.pending_slots(), ['R03__m1__s2_review'])

  def test_second_request_block_keeps_first_billed_only_once(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = self._make(batch_dir)
    self._begin(ledger)
    ledger.record_second_request_block('R01__m4__s2_review', 'b.')
    ledger.finish_request(
        slot_id='R01__m4__s2_review', status='SecondRequestBlocked'
    )
    self.assertEqual(ledger.counts['global'], 1)
    self.assertEqual(ledger.counts['second_request_blocks'], 1)
    sidecar_path = raw / 'R01__m4__s2_review.second_request_blocked.json'
    self.assertTrue(sidecar_path.exists())
    sidecar = json.loads(sidecar_path.read_text(encoding='utf-8'))
    self.assertEqual(sidecar['first_content'], 'b.')
    self.assertIn('blocked before the provider', sidecar['note'])
    events = rl.load_events(batch_dir / 'requests.jsonl')
    self.assertEqual(events[1]['event'], 'second_request_blocked')
    self.assertEqual(
        events[1]['first_content_sha256'],
        hashlib.sha256(b'b.').hexdigest(),
    )

  def test_secrets_are_redacted_from_every_written_artifact(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    raw = batch_dir / 'raw'
    ledger = rl.RequestLedger(
        batch_dir,
        batch_id='test_batch',
        global_cap=94,
        model_caps={'m4': 47},
        group_caps={'REV': 16},
        sample_caps={'REV': 1},
        secrets=('sk-SECRET',),
    )
    ledger.begin_request(
        slot_id='R01__m4__s2_review',
        sample_id='R01',
        group='REV',
        model_tag='m4',
        model_name='gpt-6.1-sol',
        attempt_id='R01__m4',
        prompt='prompt with sk-SECRET inside',
        passed_params={'model': 'x sk-SECRET'},
    )
    ledger.record_raw_response('R01__m4__s2_review', 'answer sk-SECRET')
    ledger.record_second_request_block('R01__m4__s2_review', 'b sk-SECRET .')
    ledger.finish_request(
        slot_id='R01__m4__s2_review',
        status='error',
        error='provider failed: sk-SECRET',
    )
    for path in sorted(raw.iterdir()):
      content = path.read_text(encoding='utf-8')
      self.assertNotIn('sk-SECRET', content, msg=path.name)
      self.assertIn('[REDACTED]', content, msg=path.name)
    ledger_text = (batch_dir / 'requests.jsonl').read_text(encoding='utf-8')
    self.assertNotIn('sk-SECRET', ledger_text)
    self.assertIn('[REDACTED]', ledger_text)

  def test_corrupt_ledger_refuses_resume(self):
    batch_dir = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._make(batch_dir)
    self._begin(ledger)
    with (batch_dir / 'requests.jsonl').open('a', encoding='utf-8') as handle:
      handle.write('{"event": "star')
    with self.assertRaises(rl.LedgerAuditError):
      self._make(batch_dir)


if __name__ == '__main__':
  unittest.main()
