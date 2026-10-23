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

"""Tests for world_response_probe_closeout (offline, stdlib only).

Task 3 covered the I/O discipline (input manifest hashing and
re-verification, output-directory disjointness, explicit failure on
missing required inputs, transparent failure on a truncated ledger).
Task 4 adds the semantic errata stage: corrected ledger counts, the
three-layer attempt status, the offline Gemini option-intent analysis,
the human-label audit and preservation of the source conclusions.

The semantic tests build a synthetic mini batch reproducing the shape of
the frozen 2026-10-20 batch (82 unique started slots, one pending slot,
two second-request blocks, 16 frozen REV generation slots, six skipped
slots and the legacy wrong counts), so nothing depends on run_results
or a real batch; CI never touches run_results.
"""

from collections import Counter
import hashlib
import json
import pathlib
import tempfile
from typing import Any
import unittest

from examples.story_enactment import world_response_probe_closeout as cop


def _tmp_dir():
  """Returns a context manager yielding a fresh temporary directory path."""
  return tempfile.TemporaryDirectory()


def _write_minimal_batch(batch_dir: pathlib.Path) -> None:
  """Writes a minimal but structurally valid probe batch."""
  batch_dir.mkdir(parents=True)
  (batch_dir / 'protocol.json').write_text('{}\n', encoding='utf-8')
  (batch_dir / 'fixtures.json').write_text('{}\n', encoding='utf-8')
  (batch_dir / 'report.json').write_text('{}\n', encoding='utf-8')
  (batch_dir / 'human_labels.jsonl').write_text('\n', encoding='utf-8')
  (batch_dir / 'README.md').write_text('# batch\n', encoding='utf-8')
  (batch_dir / 'source_snapshot.json').write_text(
      json.dumps({'files': []}) + '\n', encoding='utf-8'
  )
  events = [
      {
          'event': 'started',
          'slot_id': 's1__ACT1__a',
          'sample_id': 's1__ACT1',
          'group': 'ACT',
          'model_tag': 'm4',
          'model_name': 'test-model',
          'prompt_sha256': hashlib.sha256(b'minimal prompt').hexdigest(),
      },
      {'event': 'finished', 'slot_id': 's1__ACT1__a'},
  ]
  (batch_dir / 'requests.jsonl').write_text(
      ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events),
      encoding='utf-8',
  )
  # Task 4 wires raw_dir into the ledger audit: every started slot needs
  # its raw sidecar.
  raw = batch_dir / 'raw'
  raw.mkdir()
  (raw / 's1__ACT1__a.started.json').write_text(
      json.dumps(
          {key: value for key, value in events[0].items() if key != 'event'}
      ),
      encoding='utf-8',
  )
  (raw / 's1__ACT1__a.prompt.txt').write_text(
      'minimal prompt', encoding='utf-8'
  )
  nested = batch_dir / 'nested'
  nested.mkdir()
  (nested / 'deep.txt').write_bytes(b'deep\n')


def _main_args(batch_dir: pathlib.Path, output_dir: pathlib.Path) -> list[str]:
  """Returns the CLI argv pair for main()."""
  return ['--source_batch_dir', str(batch_dir), '--output_dir', str(output_dir)]


class IoDisciplineTest(unittest.TestCase):

  def test_output_inside_source_is_rejected_including_symlink(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      # A direct subdirectory of the source is rejected.
      with self.assertRaisesRegex(ValueError, 'outside the source batch'):
        cop.assert_disjoint_output_dir(batch, batch / 'inside_out')
      # The reverse overlap (source inside output) is rejected too.
      with self.assertRaisesRegex(ValueError, 'outside the source batch'):
        cop.assert_disjoint_output_dir(batch, pathlib.Path(tmp))
      # A symlink pointing inside the source is rejected after resolve().
      inner = batch / 'inner'
      inner.mkdir()
      link = pathlib.Path(tmp) / 'out_link'
      link.symlink_to(inner)
      with self.assertRaisesRegex(ValueError, 'outside the source batch'):
        cop.assert_disjoint_output_dir(batch, link)

  def test_existing_output_dir_is_rejected(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      out = pathlib.Path(tmp) / 'out'
      out.mkdir()
      with self.assertRaisesRegex(ValueError, 'existing'):
        cop.assert_disjoint_output_dir(batch, out)

  def test_manifest_covers_all_files_sorted_with_hashes(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      manifest = cop.input_manifest_payload(batch)
      paths = [f['path'] for f in manifest['files']]
      self.assertEqual(paths, sorted(paths))
      self.assertIn('requests.jsonl', paths)
      self.assertIn('nested/deep.txt', paths)
      by_path = {f['path']: f['sha256'] for f in manifest['files']}
      self.assertEqual(
          by_path['nested/deep.txt'], hashlib.sha256(b'deep\n').hexdigest()
      )

  def test_post_work_drift_is_detected(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      manifest = cop.input_manifest_payload(batch)
      cop.verify_input_manifest(batch, manifest)  # Clean baseline passes.
      (batch / 'protocol.json').unlink()  # Now missing from the batch.
      (batch / 'extra.txt').write_text('extra\n', encoding='utf-8')  # Extra.
      (batch / 'report.json').write_text('{"changed": true}\n')  # Changed.
      with self.assertRaises(ValueError) as context:
        cop.verify_input_manifest(batch, manifest)
      message = str(context.exception)
      self.assertIn('missing', message)
      self.assertIn('protocol.json', message)
      self.assertIn('extra', message)
      self.assertIn('extra.txt', message)
      self.assertIn('changed', message)
      self.assertIn('report.json', message)

  def test_missing_required_inputs_fail_explicitly(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      (batch / 'fixtures.json').unlink()
      out = pathlib.Path(tmp) / 'out'
      with self.assertRaises(SystemExit) as context:
        cop.main(_main_args(batch, out))
      self.assertIn('fixtures.json', str(context.exception))
      # Nothing is written and no zeros are guessed.
      self.assertFalse(out.exists())
      self.assertFalse((out / 'corrected_report.json').exists())

  def test_truncated_ledger_fails_without_guessing(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      ledger = batch / 'requests.jsonl'
      ledger.write_text(
          ledger.read_text(encoding='utf-8')[:-8], encoding='utf-8'
      )  # Truncate the last line mid-JSON.
      out = pathlib.Path(tmp) / 'out'
      with self.assertRaises(SystemExit) as context:
        cop.main(_main_args(batch, out))
      self.assertIn('truncated or invalid JSON', str(context.exception))
      self.assertFalse((out / 'corrected_report.json').exists())

  def test_main_happy_path_writes_manifest_and_reverifies(self):
    with _tmp_dir() as tmp:
      batch = pathlib.Path(tmp) / 'batch'
      _write_minimal_batch(batch)
      out = pathlib.Path(tmp) / 'out'
      cop.main(_main_args(batch, out))
      manifest_path = out / 'input_manifest.json'
      self.assertTrue(manifest_path.is_file())
      manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
      cop.verify_input_manifest(batch, manifest)
      # Task 4 completes main: the semantic outputs are now written too.
      for name in ('corrected_report.json', 'corrections.json', 'README.md'):
        self.assertTrue((out / name).is_file(), name)


# --------------------------------------------------------------------------
# Task 4: synthetic mini batch shaped like the frozen 2026-10-20 batch.
# --------------------------------------------------------------------------

_SAMPLE_IDS = (
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
_MODELS = {'m4': 'gpt-6.1-sol', 'm1': 'gemini-3.8-flash-high'}
# REP attempts whose judgment said 'No' never run their s2/s3 stages.
_NO_BRANCH_STOPPED = {('REP02', 'm4'), ('REP03', 'm4'), ('REP03', 'm1')}
_SRB_FIRST_CONTENT = {
    'REP01__m1__s1_judgment': 'b.',
    'REP02__m1__s1_judgment': 'b)',
}
_SRB_ATTEMPT_IDS = {
    '__'.join(slot.split('__')[:2]) for slot in _SRB_FIRST_CONTENT
}
_REP_JUDGMENT_PROMPTS = {
    'REP01': (
        'Mini judgment prompt for REP01.\n'
        'Question: Is the story traced out by the above list of events'
        ' repetitive?\n'
        '  (a) No\n'
        '  (b) Yes\n'
        'Answer: ('
    ),
    'REP02': (
        'Mini judgment prompt for REP02.\n'
        'Question: Is the story traced out by the above list of events'
        ' repetitive?\n'
        '  (a) Yes\n'
        '  (b) No\n'
        'Answer: ('
    ),
}
_REP_JUDGMENT_BY_ATTEMPT = {
    'REP01__m4': 'Yes',
    'REP02__m4': 'No',
    'REP03__m4': 'No',
    'REP03__m1': 'No',
}
_REV_TRUE_SAMPLES = ('REV01', 'REV02')
_REP_EXPECTED = {'REP01': 'Yes', 'REP02': 'No', 'REP03': 'No'}
_README_CLAIM = '冻结生成槽（REV generation）12 次：0 成本。'


def _group_of(sample_id: str) -> str:
  """Derives the group tag from a sample id, as the ledger does."""
  return ''.join(char for char in sample_id if char.isalpha())


def _sha256_text(text: str) -> str:
  """Returns the hex SHA-256 of a UTF-8 string."""
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _expected_of(sample_id: str) -> str:
  """Returns the frozen expected label of a mini-batch sample."""
  group = _group_of(sample_id)
  if group == 'REV':
    return 'true' if sample_id in _REV_TRUE_SAMPLES else 'false'
  if group == 'REP':
    return _REP_EXPECTED[sample_id]
  return ''


def _skipped_events(sample_id, tag, stages, after_index):
  """Builds skipped events for the stages after a stopped one."""
  return [
      {
          'event': 'skipped',
          'slot_id': f'{sample_id}__{tag}__s{j}_{stages[j - 1]}',
          'sample_id': sample_id,
          'status': 'skipped_by_upstream_failure',
          'reason': 'upstream failure',
      }
      for j in range(after_index + 1, len(stages) + 1)
  ]


def _finished_event(slot_id, sample_id, group, tag, status):
  """Builds a finished ledger event."""
  return {
      'event': 'finished',
      'slot_id': slot_id,
      'sample_id': sample_id,
      'group': group,
      'model_tag': tag,
      'status': status,
      'error': None,
      'boundary': {},
  }


def _write_mini_protocol(batch_dir, execution_order, slots, frozen_slots):
  """Writes the frozen protocol (budgets, order, slots, frozen markers)."""
  protocol = {
      'batch': 'mini',
      'budgets': {
          'global_requests': 94,
          'per_model': 47,  # Scalar cap, as in the real protocol.
          'per_group_both_models': {
              'ACT': 24,
              'CHAIN': 36,
              'REV': 16,
              'REP': 18,
          },
          'per_sample': {'ACT': 4, 'CHAIN': 3, 'REV': 1, 'REP': 3},
      },
      'execution_order': execution_order,
      'slots': slots,
      'frozen_slots': frozen_slots,
  }
  (batch_dir / 'protocol.json').write_text(
      json.dumps(protocol, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


def _write_mini_fixtures(batch_dir):
  """Writes the frozen fixtures with per-sample expected labels."""
  samples = [
      {
          'sample_id': sample_id,
          'group': _group_of(sample_id),
          'expected': _expected_of(sample_id),
      }
      for sample_id in _SAMPLE_IDS
  ]
  (batch_dir / 'fixtures.json').write_text(
      json.dumps(
          {'batch': 'mini', 'samples': samples}, ensure_ascii=False, indent=2
      )
      + '\n',
      encoding='utf-8',
  )


def _write_mini_report(batch_dir, execution_order, slots, frozen_slots):
  """Writes the legacy report with the wrong counts (80/37/20, SRB 0)."""
  attempts = {}
  for attempt_id in execution_order:
    sample_id, tag = attempt_id.split('__')
    group = _group_of(sample_id)
    planned = slots[sample_id][tag]
    attempt: dict[str, Any] = {
        'sample_id': sample_id,
        'model_tag': tag,
        'group': group,
        'planned_slots': planned,
        'real_slots': [s for s in planned if s not in frozen_slots],
        'status': 'completed',
        'outputs': {},
        'skipped_slots': [],
        'failure': None,
    }
    if attempt_id in _REP_JUDGMENT_BY_ATTEMPT:
      attempt['outputs'] = {'judgment': _REP_JUDGMENT_BY_ATTEMPT[attempt_id]}
    if attempt_id == 'ACT03__m1':
      attempt['status'] = 'sample_failure'
      attempt['failure'] = (
          'started_no_result: ACT03__m1__s2_self_perception (request sent,'
          ' result lost to an external process kill)'
      )
      attempt['skipped_slots'] = [
          'ACT03__m1__s3_person_by_situation',
          'ACT03__m1__s4_action_output',
      ]
    elif attempt_id in _SRB_ATTEMPT_IDS:
      attempt['status'] = 'sample_failure'
      attempt['failure'] = (
          f'SecondRequestBlocked: slot {attempt_id}__s1_judgment already'
          ' served its one request; the hidden retry attempt was blocked'
          ' before the provider.'
      )
      attempt['skipped_slots'] = [
          f'{attempt_id}__s2_suggestion',
          f'{attempt_id}__s3_merge',
      ]
    elif group == 'REV':
      attempt['status'] = 'sample_failure'
      attempt['outputs'] = {
          'failure_kind': 'review_rejected',
          'failure_detail': 'Semantic review rejected output.',
      }
      attempt['failure'] = 'review_rejected: Semantic review rejected output.'
    attempts[attempt_id] = attempt
  report = {
      'batch': 'mini',
      'counts': {
          'planned_attempts': 40,
          'executed_attempts': 40,
          'real_calls': 80,
          'by_model': {'m4': 43, 'm1': 37},
          'by_group': {'ACT': 20, 'CHAIN': 36, 'REV': 16, 'REP': 8},
          'second_request_blocks': 0,
      },
      'attempts': attempts,
      'overall_pass': False,
  }
  (batch_dir / 'report.json').write_text(
      json.dumps(report, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


def _make_mini_batch(batch_dir: pathlib.Path) -> None:
  """Writes a synthetic batch shaped like the frozen 2026-10-20 batch.

  The ledger reproduces the Task-1 verified shape (82 unique started, 81
  finished, one pending ACT03__m1 slot, two second-request blocks, 16
  frozen REV generation slots, six skipped slots) plus the real skipped
  events; report.json carries the legacy wrong counts. The two REP m1
  judgment started events carry the true sha256 of their m4 counterpart
  prompt texts, so the offline intent analysis can verify hash equality
  before mapping the letter.
  """
  batch_dir.mkdir(parents=True)
  raw = batch_dir / 'raw'
  raw.mkdir()
  slots = {}
  for sample_id in _SAMPLE_IDS:
    group = _group_of(sample_id)
    slots[sample_id] = {
        tag: [
            f'{sample_id}__{tag}__s{i}_{stage}'
            for i, stage in enumerate(_STAGES[group], start=1)
        ]
        for tag in ('m4', 'm1')
    }
  frozen_slots = [
      slot
      for sample_id in _SAMPLE_IDS
      if _group_of(sample_id) == 'REV'
      for slot in slots[sample_id]['m4'] + slots[sample_id]['m1']
      if slot.endswith('s1_generation')
  ]
  events = []
  for sample_id in _SAMPLE_IDS:
    group = _group_of(sample_id)
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
        started: dict[str, Any] = {
            'event': 'started',
            'slot_id': slot,
            'sample_id': sample_id,
            'group': group,
            'model_tag': tag,
            'model_name': _MODELS[tag],
            'prompt_sha256': '0' * 64,
        }
        prompt = _REP_JUDGMENT_PROMPTS.get(sample_id, 'synthetic prompt')
        started['prompt_sha256'] = _sha256_text(prompt)
        (raw / f'{slot}.prompt.txt').write_text(prompt, encoding='utf-8')
        events.append(started)
        (raw / f'{slot}.started.json').write_text(
            json.dumps({k: v for k, v in started.items() if k != 'event'}),
            encoding='utf-8',
        )
        if slot == 'ACT03__m1__s2_self_perception':
          # Crash after persisting started: later slots never ran and are
          # recorded as skipped; this slot stays pending.
          events.extend(_skipped_events(sample_id, tag, stages, i))
          break
        if slot in _SRB_FIRST_CONTENT:
          events.append({
              'event': 'second_request_blocked',
              'slot_id': slot,
              'first_content_sha256': _sha256_text(_SRB_FIRST_CONTENT[slot]),
          })
          (raw / f'{slot}.second_request_blocked.json').write_text(
              json.dumps({
                  'note': 'blocked before the provider',
                  'first_content': _SRB_FIRST_CONTENT[slot],
              }),
              encoding='utf-8',
          )
          events.append(
              _finished_event(
                  slot, sample_id, group, tag, 'SecondRequestBlocked'
              )
          )
          events.extend(_skipped_events(sample_id, tag, stages, i))
          break
        events.append(_finished_event(slot, sample_id, group, tag, 'completed'))
  for sample_id, prompt in _REP_JUDGMENT_PROMPTS.items():
    (raw / f'{sample_id}__m4__s1_judgment.prompt.txt').write_text(
        prompt, encoding='utf-8'
    )
  (batch_dir / 'requests.jsonl').write_text(
      ''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events),
      encoding='utf-8',
  )
  execution_order = [
      f'{sample_id}__{tag}' for sample_id in _SAMPLE_IDS for tag in ('m4', 'm1')
  ]
  _write_mini_protocol(batch_dir, execution_order, slots, frozen_slots)
  _write_mini_fixtures(batch_dir)
  _write_mini_report(batch_dir, execution_order, slots, frozen_slots)
  labels = [
      json.dumps({
          'attempt_id': attempt_id,
          'annotator_type': 'agent',
          'status': 'labeled',
          'pass': True,
      })
      for attempt_id in execution_order
  ]
  (batch_dir / 'human_labels.jsonl').write_text(
      '\n'.join(labels) + '\n', encoding='utf-8'
  )
  (batch_dir / 'README.md').write_text(
      f'# mini batch\n\n- {_README_CLAIM}\n', encoding='utf-8'
  )
  (batch_dir / 'source_snapshot.json').write_text(
      json.dumps({'files': []}) + '\n', encoding='utf-8'
  )


class MiniBatchCloseoutTest(unittest.TestCase):
  """Base class: runs the full closeout over the synthetic mini batch."""

  def _make_batch(self) -> pathlib.Path:
    root = pathlib.Path(self.enterContext(_tmp_dir()))
    batch = root / 'batch'
    _make_mini_batch(batch)
    return batch

  def _run_closeout(self, batch: pathlib.Path) -> pathlib.Path:
    out = batch.parent / 'out'
    cop.main(_main_args(batch, out))
    return out

  def _corrected_report(self, batch: pathlib.Path | None = None) -> dict:
    batch = batch if batch is not None else self._make_batch()
    out = self._run_closeout(batch)
    return json.loads(
        (out / 'corrected_report.json').read_text(encoding='utf-8')
    )


class CorrectedCountsTest(MiniBatchCloseoutTest):

  def test_missing_historical_failure_prompts_are_disclosed_not_recreated(self):
    batch = self._make_batch()
    slots = [
        'ACT03__m1__s2_self_perception',
        'REP01__m1__s1_judgment',
        'REP02__m1__s1_judgment',
    ]
    for slot in slots:
      (batch / 'raw' / f'{slot}.prompt.txt').unlink()
    before = cop.input_manifest_payload(batch)
    report = self._corrected_report(batch)
    self.assertEqual(report['ledger_counts']['requests_attempted']['total'], 82)
    self.assertFalse(report['overall_pass_preserved'])
    self.assertEqual(len(report['warnings']), 3)
    for slot in slots:
      self.assertTrue(any(slot in warning for warning in report['warnings']))
    self.assertEqual(cop.input_manifest_payload(batch), before)

  def test_corrected_counts_come_from_ledger_not_old_report(self):
    report = self._corrected_report()
    attempted = report['ledger_counts']['requests_attempted']
    self.assertEqual(attempted['total'], 82)
    self.assertEqual(attempted['by_model'], {'m4': 43, 'm1': 39})
    self.assertEqual(
        attempted['by_group'], {'ACT': 22, 'CHAIN': 36, 'REV': 16, 'REP': 8}
    )

  def test_responses_no_result_and_format_failures_are_listed_separately(self):
    report = self._corrected_report()
    counts = report['ledger_counts']
    self.assertEqual(counts['responses_observed'], 81)
    self.assertEqual(
        counts['no_result_requests'], ['ACT03__m1__s2_self_perception']
    )
    self.assertEqual(
        counts['format_failures'],
        ['REP01__m1__s1_judgment', 'REP02__m1__s1_judgment'],
    )
    self.assertEqual(counts['second_request_blocks'], 2)
    self.assertEqual(counts['frozen_generation_slots'], 16)

  def test_corrections_list_old_and_corrected_with_reasons(self):
    batch = self._make_batch()
    out = self._run_closeout(batch)
    corrections = json.loads(
        (out / 'corrections.json').read_text(encoding='utf-8')
    )['corrections']
    by_field = {c['field']: c for c in corrections}
    self.assertEqual(by_field['report.counts.real_calls']['old'], 80)
    self.assertEqual(by_field['report.counts.real_calls']['corrected'], 82)
    self.assertEqual(by_field['report.counts.by_model.m1']['old'], 37)
    self.assertEqual(by_field['report.counts.by_model.m1']['corrected'], 39)
    self.assertEqual(by_field['report.counts.by_group.ACT']['old'], 20)
    self.assertEqual(by_field['report.counts.by_group.ACT']['corrected'], 22)
    self.assertEqual(by_field['report.counts.second_request_blocks']['old'], 0)
    self.assertEqual(
        by_field['report.counts.second_request_blocks']['corrected'], 2
    )
    self.assertEqual(by_field['readme.frozen_generation_slots']['old'], 12)
    self.assertEqual(
        by_field['readme.frozen_generation_slots']['corrected'], 16
    )
    # Values the old report already had right are not "corrected".
    self.assertNotIn('report.counts.by_model.m4', by_field)
    self.assertNotIn('report.counts.by_group.CHAIN', by_field)
    for correction in corrections:
      self.assertTrue(correction['basis'], correction['field'])
      self.assertTrue(correction['reason'], correction['field'])

  def test_output_directory_holds_all_four_files(self):
    batch = self._make_batch()
    out = self._run_closeout(batch)
    for name in (
        'input_manifest.json',
        'corrected_report.json',
        'corrections.json',
        'README.md',
    ):
      self.assertTrue((out / name).is_file(), name)
    readme = (out / 'README.md').read_text(encoding='utf-8')
    self.assertIn('82', readme)
    self.assertIn('三层', readme)
    self.assertIn('零真实', readme)


class ThreeLayerStatusTest(MiniBatchCloseoutTest):

  def test_rev_three_layers_are_separated(self):
    report = self._corrected_report()
    layers = report['attempt_status_layers']
    rev_protocol = layers['protocol_match']['REV']
    self.assertEqual(rev_protocol['matched'], 12)
    self.assertEqual(rev_protocol['unmatched'], 4)
    self.assertTrue(rev_protocol['frozen_labels_unchanged'])
    self.assertEqual(
        {
            a['sample_id']
            for a in rev_protocol['unmatched_attempts']
            if a['fixture_defect']
        },
        {'REV01', 'REV02'},
    )
    self.assertEqual(
        [a['attempt_id'] for a in rev_protocol['unmatched_attempts']],
        ['REV01__m4', 'REV01__m1', 'REV02__m4', 'REV02__m1'],
    )
    semantic = layers['semantic_annotation']['REV']
    self.assertIn('合法正例接受能力缺少本组证据', semantic['summary'])
    self.assertEqual(semantic['fixture_defect_samples'], ['REV01', 'REV02'])
    self.assertTrue(semantic['frozen_expected_unchanged'])

  def test_execution_status_classification_rules(self):
    report = self._corrected_report()
    status = report['attempt_status_layers']['execution_status']
    # completed → completed.
    self.assertEqual(status['ACT01__m4'], 'completed')
    self.assertEqual(status['REP01__m4'], 'completed')
    # failure startswith 'SecondRequestBlocked' → format_error.
    self.assertEqual(status['REP01__m1'], 'format_error')
    self.assertEqual(status['REP02__m1'], 'format_error')
    # outputs.failure_kind == 'review_rejected' → review_rejected.
    self.assertEqual(status['REV03__m4'], 'review_rejected')
    self.assertEqual(status['REV08__m1'], 'review_rejected')
    # attempt contains a pending slot → started_no_result.
    self.assertEqual(status['ACT03__m1'], 'started_no_result')
    self.assertEqual(
        Counter(status.values()),
        {
            'completed': 21,
            'format_error': 2,
            'review_rejected': 16,
            'started_no_result': 1,
        },
    )

  def test_rep_no_branch_slots_are_not_needed_not_skipped(self):
    report = self._corrected_report()
    counts = report['ledger_counts']
    self.assertEqual(len(counts['not_needed_no_branch']), 6)
    self.assertIn('REP02__m4__s2_suggestion', counts['not_needed_no_branch'])
    self.assertEqual(len(counts['skipped_by_upstream_failure']), 6)
    self.assertNotIn(
        'REP02__m4__s2_suggestion', counts['skipped_by_upstream_failure']
    )
    self.assertEqual(
        set(counts['skipped_by_upstream_failure']),
        {
            'ACT03__m1__s3_person_by_situation',
            'ACT03__m1__s4_action_output',
            'REP01__m1__s2_suggestion',
            'REP01__m1__s3_merge',
            'REP02__m1__s2_suggestion',
            'REP02__m1__s3_merge',
        },
    )


class GeminiOfflineIntentTest(MiniBatchCloseoutTest):

  def test_offline_letter_analysis_maps_intent_but_keeps_verdict_failed(self):
    report = self._corrected_report()
    analysis = report['gemini_format_offline_analysis']
    by_slot = {a['slot_id']: a for a in analysis}
    rep01 = by_slot['REP01__m1__s1_judgment']
    self.assertEqual(rep01['raw_first_content'], 'b.')
    self.assertEqual(rep01['normalized_letter'], 'b')
    self.assertEqual(rep01['option_meaning'], 'Yes')
    self.assertEqual(rep01['expected'], 'Yes')
    self.assertTrue(rep01['intent_matches_expected'])
    self.assertEqual(rep01['protocol_verdict'], 'failed')
    self.assertIn('仅离线解读', rep01['note'])
    # REP02 answered 'b)' and its prompt maps b to 'No', which also
    # matches the frozen expectation — still a protocol failure.
    rep02 = by_slot['REP02__m1__s1_judgment']
    self.assertEqual(rep02['raw_first_content'], 'b)')
    self.assertEqual(rep02['normalized_letter'], 'b')
    self.assertEqual(rep02['option_meaning'], 'No')
    self.assertTrue(rep02['intent_matches_expected'])
    self.assertEqual(rep02['protocol_verdict'], 'failed')

  def test_prompt_hash_equality_is_verified_before_mapping(self):
    batch = self._make_batch()
    # Give m4 an internally consistent but different archived prompt.
    # The cross-model equality check must still refuse the mapping.
    prompt_path = batch / 'raw' / 'REP01__m4__s1_judgment.prompt.txt'
    prompt_path.write_text(
        prompt_path.read_text(encoding='utf-8') + 'tampered',
        encoding='utf-8',
    )
    ledger_path = batch / 'requests.jsonl'
    events = [
        json.loads(line)
        for line in ledger_path.read_text(encoding='utf-8').splitlines()
    ]
    for event in events:
      if (
          event['event'] == 'started'
          and event['slot_id'] == 'REP01__m4__s1_judgment'
      ):
        event['prompt_sha256'] = hashlib.sha256(
            prompt_path.read_bytes()
        ).hexdigest()
        (batch / 'raw/REP01__m4__s1_judgment.started.json').write_text(
            json.dumps(
                {key: value for key, value in event.items() if key != 'event'}
            ),
            encoding='utf-8',
        )
    ledger_path.write_text(
        ''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8'
    )
    report = self._corrected_report(batch)
    by_slot = {
        a['slot_id']: a for a in report['gemini_format_offline_analysis']
    }
    rep01 = by_slot['REP01__m1__s1_judgment']
    self.assertIn('error', rep01)
    self.assertNotIn('option_meaning', rep01)
    # The untouched REP02 entry is still mapped normally.
    rep02 = by_slot['REP02__m1__s1_judgment']
    self.assertNotIn('error', rep02)
    self.assertEqual(rep02['option_meaning'], 'No')


class HumanLabelAuditTest(MiniBatchCloseoutTest):

  def test_agent_labels_do_not_count_as_human_confirmation(self):
    report = self._corrected_report()
    audit = report['human_labels_audit']
    self.assertEqual(len(audit['planned']), 40)
    self.assertEqual(len(audit['labeled']), 40)
    self.assertEqual(audit['missing'], [])
    self.assertEqual(audit['duplicates'], [])
    self.assertEqual(audit['unknown_attempt_ids'], [])
    self.assertEqual(audit['not_labeled'], [])
    self.assertEqual(audit['annotator_type_counts'], {'agent': 40})
    self.assertEqual(report['human_confirmation'], 'pending')

  def test_missing_duplicate_and_unknown_labels_are_reported(self):
    batch = self._make_batch()
    labels_path = batch / 'human_labels.jsonl'
    lines = labels_path.read_text(encoding='utf-8').splitlines()
    kept = [
        line
        for line in lines
        if json.loads(line)['attempt_id'] != 'ACT01__m4'  # 缺一项
    ]
    kept.append(kept[0])  # 复制一项（ACT01__m1）。
    kept.append(
        json.dumps({
            'attempt_id': 'ZZ99__m9',  # 未知 attempt_id
            'annotator_type': 'agent',
            'status': 'labeled',
            'pass': True,
        })
    )
    labels_path.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    report = self._corrected_report(batch)
    audit = report['human_labels_audit']
    self.assertEqual(audit['missing'], ['ACT01__m4'])
    self.assertEqual(audit['duplicates'], ['ACT01__m1'])
    self.assertEqual(audit['unknown_attempt_ids'], ['ZZ99__m9'])
    self.assertEqual(report['human_confirmation'], 'pending')


class PreservationTest(MiniBatchCloseoutTest):

  def test_overall_pass_and_original_files_unchanged(self):
    batch = self._make_batch()
    before = {
        path.relative_to(batch).as_posix(): path.read_bytes()
        for path in sorted(batch.rglob('*'))
        if path.is_file()
    }
    out = self._run_closeout(batch)
    report = json.loads(
        (out / 'corrected_report.json').read_text(encoding='utf-8')
    )
    self.assertFalse(report['overall_pass_preserved'])
    self.assertEqual(report['provenance']['new_real_api_calls'], 0)
    # Every source file is byte-identical after the run.
    after = {
        path.relative_to(batch).as_posix(): path.read_bytes()
        for path in sorted(batch.rglob('*'))
        if path.is_file()
    }
    self.assertEqual(before, after)
    # ... and the written manifest re-verifies against the batch.
    manifest = json.loads(
        (out / 'input_manifest.json').read_text(encoding='utf-8')
    )
    cop.verify_input_manifest(batch, manifest)

  def test_budget_remaining_within_all_caps(self):
    report = self._corrected_report()
    check = report['ledger_counts']['budget_check']
    self.assertFalse(check['any_cap_exceeded'])
    self.assertEqual(check['global'], {'used': 82, 'cap': 94, 'remaining': 12})
    self.assertEqual(
        check['by_model']['m1'], {'used': 39, 'cap': 47, 'remaining': 8}
    )
    self.assertEqual(
        check['by_group']['ACT'], {'used': 22, 'cap': 24, 'remaining': 2}
    )
    self.assertEqual(
        check['by_sample']['ACT01__m4'], {'used': 4, 'cap': 4, 'remaining': 0}
    )


if __name__ == '__main__':
  unittest.main()
