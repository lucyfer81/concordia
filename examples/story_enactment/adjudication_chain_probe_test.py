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

"""Offline tests of the adjudication-chain probe runner.

Every test runs without network and without API keys: forwarding slots
are answered by deterministic scripted models and the credential lookup
is patched. The suite verifies the frozen-protocol properties the
chain batch requires: the 24+24 slot plans in frozen order with caps
summing to 120, the calibrated three-model history reuse, byte-for-byte
provenance of the E03 archived sample, deterministic stub validation of
call counts, routing, exception classification (a malformed generation
stops at the production path with no compensating review) and
interruption behavior (started entries never re-sent, caps enforced
before requests, upstream failures recorded as skipped with zero
calls), plus the label-derived summary accounting of all three stages.
"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from examples.story_enactment import adjudication_chain_probe as probe
from examples.story_enactment import fixed_model_probe as fp
from examples.story_enactment import three_model_review_probe as tm

_APPROVAL = json.dumps(
    {'valid': True, 'reason': 'No violation.'}, ensure_ascii=False
)
_REJECTION = json.dumps(
    {
        'valid': False,
        'reason': 'The action result invents another player\u2019s consent.',
    },
    ensure_ascii=False,
)
_GENERATION = json.dumps(
    {'stage_directions': '旁白占位：裁决结果被如实叙述。'}, ensure_ascii=False
)
_GENERATION_WITH_SELFTALK = json.dumps(
    {
        'stage_directions': (
            '周律师当众展示遗嘱文本并念出落款日期。'
            'seal remains intact? We can provide.'
        )
    },
    ensure_ascii=False,
)
_FAKE_NAMES = {'m1': 'fake-model-1', 'm4': 'fake-model-4'}
_BATCH2_A02_M2_R2 = (
    fp._REPO_ROOT  # pylint: disable=protected-access
    / 'run_results'
    / 'story_enactment'
    / 'action_authorization_fixed_probe'
    / '20261017T112000Z'
    / 'adjudication'
    / 'A02__m2__r2_result.json'
)


class _ScriptedModel:
  """Deterministic forward-slot stand-in with scripted responses."""

  def __init__(self, responses, *, on_call=None):
    self._responses = list(responses)
    self._on_call = on_call
    self.calls = 0

  def sample_text(self, prompt, **kwargs):
    del prompt, kwargs
    self.calls += 1
    if self._on_call is not None:
      self._on_call(self.calls)
    if self.calls > len(self._responses):
      raise AssertionError('scripted model received more calls than planned')
    return self._responses[self.calls - 1]


def _patched_settings():
  """Patches the credential lookup with offline fake settings."""
  return mock.patch.object(
      probe,
      '_load_settings',
      side_effect=lambda: (_FAKE_NAMES, 'fake-key', 'http://fake', ['k']),
  )


def _patched_live(responses, calls=None):
  """Patches the live-model factory with one shared scripted model."""
  scripted = _ScriptedModel(responses)
  if calls is not None:
    calls.append(scripted)
  return scripted, mock.patch.object(
      probe,
      '_create_live_model',
      side_effect=lambda name, **_: (scripted, []),
  )


def _read_json(path):
  return json.loads(path.read_text(encoding='utf-8'))


def _make_frozen_batch(parent, name='batch'):
  parent = pathlib.Path(parent)
  batch_dir = parent / name
  with _patched_settings():
    probe._freeze(batch_dir)  # pylint: disable=protected-access
  return batch_dir


def _write_a_results(batch_dir, *, verdict_outcome='approved'):
  adjudication = batch_dir / 'adjudication'
  for case_id, model_tag, repetition in probe.planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    record = {
        'attempt_id': stem,
        'status': 'completed',
        'core_action_result': f'{stem} 占位。 Because of that, 结果。',
    }
    (adjudication / f'{stem}_result.json').write_text(
        json.dumps(record, ensure_ascii=False), encoding='utf-8'
    )
    (adjudication / f'{stem}_started.json').write_text('{}', encoding='utf-8')


def _write_labels(batch_dir, *, pass_value=True):
  rows = []
  for case_id, model_tag, repetition in probe.planned_adjudication_attempts():
    rows.append(
        json.dumps(
            {
                'attempt_id': f'{case_id}__{model_tag}__r{repetition}',
                'status': 'labeled',
                'pass': pass_value,
                'definite_grounded_result': True,
            },
            ensure_ascii=False,
        )
    )
  (batch_dir / 'human_labels.jsonl').write_text(
      '\n'.join(rows) + '\n', encoding='utf-8'
  )


def _write_final_results(batch_dir, *, verdict=True):
  final = batch_dir / 'final'
  for case_id, model_tag, repetition in probe.planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    record = {
        'attempt_id': stem,
        'source_attempt_id': stem,
        'status': 'completed',
        'stage_outcome': 'approved' if verdict else 'semantic_rejected',
        'classification': None,
        'verdict': verdict,
        'review_executed': True,
        'real_api_calls': 2,
    }
    (final / f'{stem}_result.json').write_text(
        json.dumps(record, ensure_ascii=False), encoding='utf-8'
    )
    (final / f'{stem}_generation_started.json').write_text(
        '{}', encoding='utf-8'
    )


def _write_final_human_labels(batch_dir):
  rows = []
  for case_id, model_tag, repetition in probe.planned_adjudication_attempts():
    rows.append(
        json.dumps(
            {
                'attempt_id': f'{case_id}__{model_tag}__r{repetition}',
                'status': 'labeled',
                'legal_result_retained': True,
                'other_player_new_choice': False,
                'canonical_nonstory_text': False,
                'review_reason_matches_verdict': True,
            },
            ensure_ascii=False,
        )
    )
  (batch_dir / 'human_final_labels.jsonl').write_text(
      '\n'.join(rows) + '\n', encoding='utf-8'
  )


def _write_e_results_and_labels(batch_dir):
  injection = batch_dir / 'injection'
  rows = []
  for case_id, model_tag, repetition in probe.planned_injection_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    expected = probe.injection_case_by_id(case_id).expected_valid
    record = {
        'attempt_id': stem,
        'status': 'completed',
        'classification': 'correct',
        'verdict': expected,
        'stage_outcome': 'approved' if expected else 'semantic_rejected',
        'review_executed': True,
        'canonical_known_nonstory': False,
        'case_id': case_id,
        'real_api_calls': 2,
    }
    (injection / f'{stem}_result.json').write_text(
        json.dumps(record, ensure_ascii=False), encoding='utf-8'
    )
    (injection / f'{stem}_generation_started.json').write_text(
        '{}', encoding='utf-8'
    )
    label = {
        'attempt_id': stem,
        'status': 'labeled',
        'legal_substance_retained': True,
        'canonical_nonstory_text': False,
        'english_only_rejection': False,
        'violation_named_in_reason': True,
    }
    rows.append(json.dumps(label, ensure_ascii=False))
  (batch_dir / 'human_e_labels.jsonl').write_text(
      '\n'.join(rows) + '\n', encoding='utf-8'
  )


def _build_passing_batch(parent, name='batch'):
  batch_dir = _make_frozen_batch(parent, name)
  _write_a_results(batch_dir)
  _write_labels(batch_dir)
  _write_final_results(batch_dir, verdict=True)
  _write_final_human_labels(batch_dir)
  _write_e_results_and_labels(batch_dir)
  report = _read_json(batch_dir / 'report.json')
  report['counts'].update(
      real_api_calls=120,
      real_api_calls_stage_a=24,
      real_api_calls_stage_final=48,
      real_api_calls_stage_e=48,
  )
  (batch_dir / 'report.json').write_text(
      json.dumps(report, ensure_ascii=False), encoding='utf-8'
  )
  return batch_dir


def _flip_e_label(batch_dir, attempt_id, key, value, *, remove=False):
  path = batch_dir / 'human_e_labels.jsonl'
  rows = [
      json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
  ]
  for row in rows:
    if row['attempt_id'] == attempt_id:
      if remove:
        row.pop(key, None)
      else:
        row[key] = value
  path.write_text(
      '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n',
      encoding='utf-8',
  )


class PlanAndFixturesTest(unittest.TestCase):

  def test_plans_are_24_plus_24_unique_slots_in_frozen_order(self):
    a_plan = probe.planned_adjudication_attempts()
    e_plan = probe.planned_injection_attempts()
    self.assertEqual(len(set(a_plan)), 24)
    self.assertEqual(len(set(e_plan)), 24)
    self.assertEqual(a_plan[:2], [('A01', 'm1', 1), ('A02', 'm1', 1)])
    self.assertEqual(a_plan[4:6], [('A01', 'm4', 1), ('A02', 'm4', 1)])
    self.assertEqual(e_plan[:2], [('E01', 'm1', 1), ('E02', 'm1', 1)])
    self.assertEqual(e_plan[-1], ('E04', 'm4', 3))

  def test_model_mapping_is_m1_gemini_and_m4_sol_only(self):
    self.assertEqual(
        probe._EXPECTED_MODEL_NAMES,  # pylint: disable=protected-access
        {'m1': 'gemini-3.8-flash-high', 'm4': 'gpt-6.1-sol'},
    )
    self.assertNotIn('AI_MODEL_2', str(probe._ENV_MODEL_KEYS))  # pylint: disable=protected-access
    self.assertNotIn('AI_MODEL_3', str(probe._ENV_MODEL_KEYS))  # pylint: disable=protected-access

  def test_caps_sum_to_the_global_120_cap(self):
    self.assertEqual(probe._CAP_STAGE_A, 24)  # pylint: disable=protected-access
    self.assertEqual(probe._CAP_STAGE_FINAL, 48)  # pylint: disable=protected-access
    self.assertEqual(probe._CAP_STAGE_E, 48)  # pylint: disable=protected-access
    self.assertEqual(
        probe._CAP_STAGE_A  # pylint: disable=protected-access
        + probe._CAP_STAGE_FINAL  # pylint: disable=protected-access
        + probe._CAP_STAGE_E,  # pylint: disable=protected-access
        probe._MAX_REAL_CALLS,  # pylint: disable=protected-access
    )

  def test_history_is_the_three_model_calibrated_history(self):
    self.assertEqual(probe.public_history_text(), tm.public_history_text())
    self.assertNotEqual(probe.public_history_text(), fp.public_history_text())
    self.assertIn(
        '周律师当众展示已启封的遗嘱信封及其上的残留火漆',
        probe.public_history_text(),
    )
    # The calibrated event 1 stays outside the director window.
    self.assertNotIn(
        '周律师当众展示已启封的遗嘱信封及其上的残留火漆',
        '\n'.join(probe.director_window()),
    )

  def test_e03_is_the_archived_batch2_sample_byte_for_byte(self):
    record = json.loads(_BATCH2_A02_M2_R2.read_text(encoding='utf-8'))
    e03 = probe.injection_case_by_id('E03')
    self.assertEqual(e03.injected_action_result, record['core_action_result'])

  def test_e04_differs_from_e03_only_in_the_english_tail(self):
    e03 = probe.injection_case_by_id('E03')
    e04 = probe.injection_case_by_id('E04')
    chinese_base = e03.injected_action_result.split('seal remains intact')[0]
    self.assertTrue(e04.injected_action_result.startswith(chinese_base))
    self.assertIn('陈母 nods and promises', e04.injected_action_result)
    self.assertNotIn('nods and promises', e03.injected_action_result)
    self.assertNotIn('seal remains intact', e04.injected_action_result)

  def test_injection_labels_and_fragments(self):
    expected = {'E01': True, 'E02': False, 'E03': True, 'E04': False}
    for case_id, valid in expected.items():
      case = probe.injection_case_by_id(case_id)
      self.assertEqual(case.expected_valid, valid, case_id)
    self.assertEqual(probe.injection_case_by_id('E01').nonstory_fragments, ())
    self.assertTrue(probe.injection_case_by_id('E02').nonstory_fragments)
    self.assertTrue(probe.injection_case_by_id('E03').nonstory_fragments)
    self.assertTrue(probe.injection_case_by_id('E04').nonstory_fragments)

  def test_e01_and_e02_reuse_the_a04_input_with_f14_shape(self):
    a04 = fp.adjudication_case_by_id('A04')
    e01 = probe.injection_case_by_id('E01')
    e02 = probe.injection_case_by_id('E02')
    self.assertEqual(e01.base_case_id, 'A04')
    self.assertTrue(e01.injected_action_result.startswith(a04.action_intent))
    self.assertIn(' Because of that, ', e01.injected_action_result)
    self.assertTrue(
        e02.injected_action_result.startswith(e01.injected_action_result)
    )

  def test_dispute_registry_lists_the_old_a04_line_separately(self):
    registry_text = json.dumps(
        probe._DISPUTE_REGISTRY,  # pylint: disable=protected-access
        ensure_ascii=False,
    )
    self.assertIn('众人先暂歇片刻', registry_text)
    for case in probe._INJECTION_CASES:  # pylint: disable=protected-access
      self.assertNotIn('众人先暂歇片刻', case.injected_action_result)


class FinalStageAttemptTest(unittest.TestCase):

  def _run(self, scripted, *, expected_valid=None, fragments=()):
    out_dir = pathlib.Path(tempfile.mkdtemp())
    gm = fp.gm_params()
    case = fp.adjudication_case_by_id('A02')
    result = probe._run_final_stage_attempt(  # pylint: disable=protected-access
        stage='injection',
        case_id='X01',
        player=case.player,
        speech=case.speech,
        action_intent=case.action_intent,
        injected_action_result='占位意图。 Because of that, 占位结果。',
        real_model=scripted,
        model_tag='m1',
        model_name='fake-model-1',
        repetition=1,
        gm=gm,
        out_dir=out_dir,
        secrets=['k'],
        expected_valid=expected_valid,
        nonstory_fragments=fragments,
    )
    return out_dir, result

  def test_approved_path_routes_exactly_two_calls(self):
    scripted = _ScriptedModel([_GENERATION, _APPROVAL])
    out_dir, result = self._run(scripted)
    self.assertEqual(scripted.calls, 2)
    self.assertEqual(result['real_api_calls'], 2)
    self.assertTrue(result['review_executed'])
    self.assertEqual(result['stage_outcome'], 'approved')
    self.assertEqual(result['status'], 'completed')
    case = fp.adjudication_case_by_id('A02')
    expected_canonical = (
        f'{case.player}: {case.speech}\n' + '旁白占位：裁决结果被如实叙述。'
    )
    self.assertEqual(result['resolved_event'], expected_canonical)
    # The injected evidence travels as-is into both production prompts.
    injected_json = json.dumps(
        '占位意图。 Because of that, 占位结果。', ensure_ascii=False
    )
    generation_prompt = (
        out_dir / 'X01__m1__r1_generation_prompt.txt'
    ).read_text(encoding='utf-8')
    review_prompt = (out_dir / 'X01__m1__r1_review_prompt.txt').read_text(
        encoding='utf-8'
    )
    self.assertIn(injected_json, generation_prompt)
    self.assertIn(injected_json, review_prompt)

  def test_expected_labels_classify_the_verdict(self):
    approved = _ScriptedModel([_GENERATION, _APPROVAL])
    _, result = self._run(approved, expected_valid=True)
    self.assertEqual(result['classification'], 'correct')
    rejected = _ScriptedModel([_GENERATION, _REJECTION])
    _, result = self._run(rejected, expected_valid=False)
    self.assertEqual(result['classification'], 'correct')
    self.assertEqual(result['stage_outcome'], 'semantic_rejected')
    false_rejection = _ScriptedModel([_GENERATION, _REJECTION])
    _, result = self._run(false_rejection, expected_valid=True)
    self.assertEqual(result['classification'], 'false_rejection')
    missed = _ScriptedModel([_GENERATION, _APPROVAL])
    _, result = self._run(missed, expected_valid=False)
    self.assertEqual(result['classification'], 'missed_detection')

  def test_malformed_generation_stops_before_any_review(self):
    scripted = _ScriptedModel(['not json at all'])
    _, result = self._run(scripted, expected_valid=True)
    self.assertEqual(scripted.calls, 1)
    self.assertEqual(result['real_api_calls'], 1)
    self.assertFalse(result['review_executed'])
    self.assertEqual(result['stage_outcome'], 'generation_structural_failure')
    self.assertEqual(result['classification'], 'generation_malformed')

  def test_generation_api_error_is_recorded_without_a_review(self):
    class _Failing:

      def sample_text(self, prompt, **kwargs):
        del prompt, kwargs
        raise RuntimeError('simulated provider timeout')

    _, result = self._run(_Failing(), expected_valid=True)
    self.assertEqual(result['stage_outcome'], 'api_error')
    self.assertEqual(result['status'], 'api_error')
    self.assertFalse(result['review_executed'])
    # The request was started, so it consumed one real call exactly
    # like the predecessor probes count it (indeterminate billing).
    self.assertEqual(result['real_api_calls'], 1)

  def test_malformed_review_is_classified_malformed(self):
    scripted = _ScriptedModel([_GENERATION, 'definitely not json'])
    _, result = self._run(scripted, expected_valid=True)
    self.assertEqual(scripted.calls, 2)
    self.assertEqual(result['classification'], 'malformed')
    self.assertEqual(result['stage_outcome'], 'review_malformed')

  def test_canonical_nonstory_fragments_are_detected(self):
    scripted = _ScriptedModel([_GENERATION_WITH_SELFTALK, _APPROVAL])
    _, result = self._run(
        scripted,
        expected_valid=True,
        fragments=('seal remains intact', 'We can provide'),
    )
    self.assertTrue(result['canonical_known_nonstory'])


class FreezeAndRunDisciplineTest(unittest.TestCase):

  def _freeze(self, batch_dir):
    with _patched_settings():
      probe._freeze(batch_dir)  # pylint: disable=protected-access

  def test_freeze_refuses_existing_directory_and_writes_artefacts(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      with self.assertRaises(ValueError):
        self._freeze(batch_dir)
      protocol = json.loads(
          (batch_dir / 'protocol.json').read_text(encoding='utf-8')
      )
      self.assertEqual(protocol['models'], _FAKE_NAMES)
      schedule = protocol['schedule']
      self.assertEqual(schedule['planned_adjudication_calls'], 24)
      self.assertEqual(schedule['planned_final_calls_max'], 48)
      self.assertEqual(schedule['planned_injection_calls_max'], 48)
      self.assertEqual(schedule['max_real_calls'], 120)
      self.assertIn('m1, m4', schedule['execution_order'])
      self.assertIn('skipped_upstream', schedule['execution_order'])
      boundary = protocol['request_settings']['call_boundary_note']
      self.assertIn('timeout=60s', boundary)
      self.assertIn('does NOT send top_p or top_k', boundary)
      self.assertIn(
          'routing-identity limitation',
          protocol['model_config_source']['routing_identity_limitation'],
      )
      fixtures = json.loads(
          (batch_dir / 'fixtures.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(fixtures['adjudication_cases']), 4)
      self.assertEqual(len(fixtures['injection_cases']), 4)
      self.assertIn('final_stage_expected_verdict_rule', fixtures)
      self.assertTrue(fixtures['dispute_registry'])

  def test_freeze_constructs_no_real_client(self):
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch('openai.OpenAI') as fake_client:
        self._freeze(pathlib.Path(tmp) / 'batch')
        fake_client.assert_not_called()

  def test_run_group_requires_frozen_artefacts(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      batch_dir.mkdir()
      with self.assertRaises(ValueError):
        probe._run_adjudication_group(batch_dir)  # pylint: disable=protected-access

  def test_started_attempt_is_never_resent(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      adjudication = batch_dir / 'adjudication'
      stem = 'A01__m1__r1'
      (adjudication / f'{stem}_started.json').write_text('{}', encoding='utf-8')
      scripted, patch = _patched_live([_GENERATION] * 300)
      with _patched_settings(), patch:
        probe._run_adjudication_group(batch_dir)  # pylint: disable=protected-access
      self.assertFalse((adjudication / f'{stem}_result.json').exists())
      self.assertEqual(scripted.calls, 23, 'only the 23 unstarted entries')
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(report['counts']['real_api_calls'], 23)
      self.assertEqual(report['counts']['real_api_calls_stage_a'], 23)

  def test_cap_stops_the_group(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      scripted, patch = _patched_live([_GENERATION] * 300)
      with _patched_settings(), patch:
        with self.assertRaises(ValueError):
          probe._run_adjudication_group(  # pylint: disable=protected-access
              batch_dir, real_call_cap=3
          )
      self.assertEqual(scripted.calls, 3)

  def test_run_final_skips_all_slots_without_upstream_results(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      scripted, patch = _patched_live([_GENERATION] * 300)
      with _patched_settings(), patch:
        probe._run_final_group(batch_dir)  # pylint: disable=protected-access
      self.assertEqual(scripted.calls, 0)
      final_dir = batch_dir / 'final'
      skipped = 0
      for path in final_dir.glob('*_result.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(record['status'], 'skipped_upstream')
        self.assertEqual(record['real_api_calls'], 0)
        skipped += 1
      self.assertEqual(skipped, 24)

  def test_run_final_injects_archived_results_and_respects_caps(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      adjudication = batch_dir / 'adjudication'
      for (
          case_id,
          model_tag,
          repetition,
      ) in probe.planned_adjudication_attempts():
        stem = f'{case_id}__{model_tag}__r{repetition}'
        record = {
            'attempt_id': stem,
            'status': 'completed',
            'core_action_result': (
                f'{stem} 的占位裁决。 Because of that, 占位结果。'
            ),
        }
        (adjudication / f'{stem}_result.json').write_text(
            json.dumps(record, ensure_ascii=False), encoding='utf-8'
        )
      responses = [_GENERATION, _APPROVAL] * 24
      scripted, patch = _patched_live(responses)
      with _patched_settings(), patch:
        probe._run_final_group(batch_dir)  # pylint: disable=protected-access
      self.assertEqual(scripted.calls, 48)
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(report['counts']['real_api_calls_stage_final'], 48)
      stem = 'A01__m1__r1'
      result = json.loads(
          (batch_dir / 'final' / f'{stem}_result.json').read_text(
              encoding='utf-8'
          )
      )
      self.assertEqual(result['stage_outcome'], 'approved')
      self.assertEqual(
          result['injected_action_result'],
          f'{stem} 的占位裁决。 Because of that, 占位结果。',
      )
      prompt = (
          batch_dir / 'final' / f'{stem}_generation_prompt.txt'
      ).read_text(encoding='utf-8')
      self.assertIn(
          json.dumps(
              f'{stem} 的占位裁决。 Because of that, 占位结果。',
              ensure_ascii=False,
          ),
          prompt,
      )

  def test_run_e_classifies_against_frozen_labels(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      responses = []
      for _, model_tag, _ in probe.planned_injection_attempts():
        del model_tag
        responses.extend([_GENERATION, _APPROVAL])
      # Reject the E02 and E04 slots instead (every second attempt).
      responses[2 * 1 + 1] = _REJECTION
      responses[2 * 3 + 1] = _REJECTION
      scripted, patch = _patched_live(responses)
      with _patched_settings(), patch:
        probe._run_injection_group(batch_dir)  # pylint: disable=protected-access
      self.assertEqual(scripted.calls, 48)
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(report['counts']['real_api_calls_stage_e'], 48)
      self.assertEqual(report['counts']['real_api_calls'], 48)
      for case_id, expected_class in (
          ('E01', 'correct'),
          ('E02', 'correct'),
          ('E03', 'correct'),
          ('E04', 'correct'),
      ):
        record = json.loads(
            (
                batch_dir / 'injection' / f'{case_id}__m1__r1_result.json'
            ).read_text(encoding='utf-8')
        )
        self.assertEqual(record['classification'], expected_class, case_id)
      e02 = json.loads(
          (batch_dir / 'injection' / 'E02__m1__r1_result.json').read_text(
              encoding='utf-8'
          )
      )
      self.assertEqual(e02['stage_outcome'], 'semantic_rejected')


class SummarizeAccountingTest(unittest.TestCase):

  def test_summary_passes_when_all_gates_hold(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _build_passing_batch(pathlib.Path(tmp))
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      self.assertTrue(report['summary']['stage_a_pass'])
      self.assertTrue(report['summary']['stage_final_pass'])
      self.assertTrue(report['summary']['stage_e_pass'])
      self.assertTrue(report['overall_pass'])
      for tag in ('m1', 'm4'):
        self.assertEqual(
            report['summary']['stage_e'][tag]['human_canonical_nonstory'], 0
        )
        self.assertEqual(
            report['summary']['stage_e'][tag]['human_label_incomplete'], 0
        )

  def test_e_gate_fails_on_one_human_canonical_nonstory_label(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _build_passing_batch(pathlib.Path(tmp))
      _flip_e_label(batch_dir, 'E03__m4__r1', 'canonical_nonstory_text', True)
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      stage_e = report['summary']['stage_e']
      self.assertEqual(stage_e['m4']['human_canonical_nonstory'], 1)
      self.assertEqual(stage_e['m4']['human_label_incomplete'], 0)
      self.assertFalse(stage_e['m4']['pass'])
      self.assertTrue(stage_e['m1']['pass'])
      self.assertFalse(report['summary']['stage_e_pass'])
      self.assertFalse(report['summary']['overall_pass'])
      self.assertFalse(report['overall_pass'])
      # Restoring the label restores the verdict (temp data only).
      _flip_e_label(batch_dir, 'E03__m4__r1', 'canonical_nonstory_text', False)
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      self.assertTrue(report['summary']['stage_e_pass'])
      self.assertTrue(report['overall_pass'])

  def test_make_frozen_batch_accepts_string_and_path_parent(self):
    for use_path in (False, True):
      with self.subTest(use_path=use_path):
        with tempfile.TemporaryDirectory() as tmp:
          parent = pathlib.Path(tmp) if use_path else tmp
          batch_dir = _make_frozen_batch(parent)
          self.assertIsInstance(batch_dir, pathlib.Path)
          self.assertTrue((batch_dir / 'protocol.json').is_file())

  def test_e_gate_human_nonstory_covers_models_and_cases(self):
    for model_tag in ('m1', 'm4'):
      for case_id in ('E01', 'E02', 'E03', 'E04'):
        with self.subTest(model=model_tag, case=case_id):
          with tempfile.TemporaryDirectory() as tmp:
            batch_dir = _build_passing_batch(pathlib.Path(tmp))
            _flip_e_label(
                batch_dir,
                f'{case_id}__{model_tag}__r1',
                'canonical_nonstory_text',
                True,
            )
            with _patched_settings():
              probe._summarize(batch_dir)  # pylint: disable=protected-access
            report = _read_json(batch_dir / 'report.json')
            stage_e = report['summary']['stage_e']
            self.assertEqual(stage_e[model_tag]['human_canonical_nonstory'], 1)
            self.assertFalse(stage_e[model_tag]['pass'])
            other = 'm4' if model_tag == 'm1' else 'm1'
            self.assertTrue(stage_e[other]['pass'])
            self.assertTrue(report['summary']['stage_a_pass'])
            self.assertTrue(report['summary']['stage_final_pass'])
            self.assertFalse(report['summary']['stage_e_pass'])
            self.assertFalse(report['overall_pass'])

  def test_e_gate_rejects_missing_or_non_boolean_nonstory_label(self):
    for name, value, remove in (
        ('missing', None, True),
        ('null', None, False),
        ('string', 'false', False),
        ('number', 1, False),
    ):
      with self.subTest(label_value=name):
        with tempfile.TemporaryDirectory() as tmp:
          batch_dir = _build_passing_batch(pathlib.Path(tmp))
          _flip_e_label(
              batch_dir,
              'E01__m1__r1',
              'canonical_nonstory_text',
              value,
              remove=remove,
          )
          with _patched_settings():
            probe._summarize(batch_dir)  # pylint: disable=protected-access
          report = _read_json(batch_dir / 'report.json')
          m1 = report['summary']['stage_e']['m1']
          self.assertEqual(m1['human_label_incomplete'], 1)
          self.assertEqual(m1['human_canonical_nonstory'], 0)
          self.assertFalse(m1['pass'])
          self.assertTrue(report['summary']['stage_e']['m4']['pass'])
          self.assertFalse(report['summary']['stage_e_pass'])
          self.assertFalse(report['overall_pass'])

  def test_e_gate_machine_fragment_scan_still_blocks_pass(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _build_passing_batch(pathlib.Path(tmp))
      result_path = batch_dir / 'injection' / 'E03__m1__r1_result.json'
      record = _read_json(result_path)
      record['canonical_known_nonstory'] = True
      result_path.write_text(
          json.dumps(record, ensure_ascii=False), encoding='utf-8'
      )
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = _read_json(batch_dir / 'report.json')
      m1 = report['summary']['stage_e']['m1']
      self.assertEqual(m1['canonical_nonstory_leaks'], 1)
      self.assertEqual(m1['human_canonical_nonstory'], 0)
      self.assertFalse(m1['pass'])
      self.assertFalse(report['summary']['stage_e_pass'])
      self.assertFalse(report['overall_pass'])

  def test_final_gate_uses_label_derived_expectations(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _make_frozen_batch(tmp)
      _write_a_results(batch_dir)
      _write_labels(batch_dir, pass_value=True)
      # Legal adjudications must be approved: a rejection is a
      # false_rejection.
      _write_final_results(batch_dir, verdict=False)
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      summary = report['summary']['stage_final']['m1']
      self.assertEqual(summary['false_rejection'], 12)
      self.assertFalse(report['summary']['stage_final_pass'])

  def test_a_gate_fails_on_violation_labels(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = _make_frozen_batch(tmp)
      _write_a_results(batch_dir)
      _write_labels(batch_dir)
      labels_path = batch_dir / 'human_labels.jsonl'
      labels = [
          json.loads(line)
          for line in labels_path.read_text(encoding='utf-8').splitlines()
      ]
      labels[0]['unauthorized_player_action'] = True
      labels_path.write_text(
          '\n'.join(json.dumps(x, ensure_ascii=False) for x in labels) + '\n',
          encoding='utf-8',
      )
      with _patched_settings():
        probe._summarize(batch_dir)  # pylint: disable=protected-access
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertFalse(report['summary']['stage_a']['m1']['pass'])
      self.assertFalse(report['summary']['stage_a_pass'])


class OfflineResummarizeTest(unittest.TestCase):

  def _manifest_for(self, batch_dir):
    manifest_path = batch_dir.parent / f'{batch_dir.name}_manifest.json'
    probe._write_json(  # pylint: disable=protected-access
        manifest_path,
        probe._input_manifest_payload(batch_dir),  # pylint: disable=protected-access
    )
    return manifest_path

  def _run_offline(self, batch_dir, output_dir, manifest_path):
    def _forbid(name):
      def _guard(*args, **kwargs):
        raise AssertionError(f'{name} must not run during offline resummary')

      return _guard

    with mock.patch.object(
        probe, '_load_settings', side_effect=_forbid('_load_settings')
    ), mock.patch.object(
        probe,
        '_create_live_model',
        side_effect=_forbid('_create_live_model'),
    ), mock.patch.object(
        probe,
        '_run_adjudication_group',
        side_effect=_forbid('_run_adjudication_group'),
    ), mock.patch.object(
        probe, '_run_final_group', side_effect=_forbid('_run_final_group')
    ), mock.patch.object(
        probe,
        '_run_injection_group',
        side_effect=_forbid('_run_injection_group'),
    ):
      probe._resummarize_offline(  # pylint: disable=protected-access
          batch_dir, output_dir, manifest_path
      )

  def test_offline_resummary_reuses_summary_and_adds_no_calls(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      strict_dir = _build_passing_batch(root, 'strict')
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      with _patched_settings():
        probe._summarize(strict_dir)  # pylint: disable=protected-access
      self._run_offline(offline_dir, root / 'out', manifest_path)
      strict_report = _read_json(strict_dir / 'report.json')
      out_report = _read_json(root / 'out' / 'report.json')
      del strict_report['summary']['generated_at_utc']
      del out_report['summary']['generated_at_utc']
      self.assertEqual(out_report['summary'], strict_report['summary'])
      self.assertTrue(out_report['overall_pass'])
      self.assertEqual(out_report['counts']['real_api_calls'], 120)
      self.assertEqual(out_report['counts']['real_api_calls_stage_e'], 48)
      provenance = out_report['offline_resummarize']
      self.assertEqual(provenance['new_real_api_calls'], 0)
      self.assertEqual(provenance['source_batch_dir'], str(offline_dir))
      self.assertEqual(
          provenance['original_report_sha256'],
          probe.sha256_file(offline_dir / 'report.json'),
      )
      self.assertEqual(
          provenance['input_manifest_sha256'],
          probe.sha256_file(manifest_path),
      )
      self.assertEqual(provenance['source_diff_vs_experiment'], {})
      self.assertIn('summarizer_source_snapshot', provenance)
      self.assertIn('resummarized_at_utc', provenance)

  def test_offline_resummary_leaves_batch_files_unchanged(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      before = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      self._run_offline(offline_dir, root / 'out', manifest_path)
      after = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      self.assertEqual(after, before)
      self.assertFalse((offline_dir / 'report.json.tmp').exists())

  def test_offline_resummary_refuses_existing_output_dir(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      out = root / 'out'
      out.mkdir()
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, out, manifest_path)
      self._run_offline(offline_dir, root / 'out2', manifest_path)
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, root / 'out2', manifest_path)

  def test_offline_resummary_refuses_output_inside_original_batch(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      manifest_path = self._manifest_for(offline_dir)
      before = sorted(offline_dir.rglob('*'))
      before_inputs = probe._input_manifest_payload(offline_dir)  # pylint: disable=protected-access
      alias = root / 'batch_alias'
      alias.symlink_to(offline_dir, target_is_directory=True)
      outputs = (
          offline_dir,
          offline_dir / 'resummarized',
          offline_dir / 'nested' / 'resummarized',
          alias / 'via_alias',
      )
      for output_dir in outputs:
        with self.subTest(output=str(output_dir)):
          with self.assertRaises(ValueError):
            self._run_offline(offline_dir, output_dir, manifest_path)
          self.assertEqual(sorted(offline_dir.rglob('*')), before)
          self.assertEqual(
              probe._input_manifest_payload(offline_dir),  # pylint: disable=protected-access
              before_inputs,
          )

  def test_offline_resummary_refuses_changed_inputs(self):
    for kind in ('changed', 'missing'):
      with self.subTest(tamper=kind):
        with tempfile.TemporaryDirectory() as tmp:
          root = pathlib.Path(tmp)
          offline_dir = _build_passing_batch(root, 'offline')
          target = (
              offline_dir / 'injection' / 'E03__m4__r1_generation_response.txt'
          )
          # The shared summary fixture creates result/started files,
          # but no provider response: create it before the baseline.
          target.write_text(
              '{"stage_directions":"离线模拟响应。"}', encoding='utf-8'
          )
          manifest_path = self._manifest_for(offline_dir)
          if kind == 'changed':
            target.write_text(
                target.read_text(encoding='utf-8') + 'tampered',
                encoding='utf-8',
            )
          else:
            target.unlink()
          with self.assertRaises(ValueError):
            self._run_offline(offline_dir, root / 'out', manifest_path)

  def test_offline_resummary_refuses_tampered_frozen_hash(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      protocol = offline_dir / 'protocol.json'
      protocol.write_text(
          protocol.read_text(encoding='utf-8') + '\n', encoding='utf-8'
      )
      manifest_path = self._manifest_for(offline_dir)
      with self.assertRaises(ValueError):
        self._run_offline(offline_dir, root / 'out', manifest_path)

  def test_source_diff_allows_only_approved_tool_changes(self):
    current = probe.source_snapshot_payload()
    experiment = {'files': [dict(e) for e in current['files']]}
    diff = probe._source_diff_vs_experiment(  # pylint: disable=protected-access
        experiment, current
    )
    self.assertEqual(diff, {})
    tool_path = 'examples/story_enactment/adjudication_chain_probe.py'
    for entry in experiment['files']:
      if entry['path'] == tool_path:
        entry['sha256'] = '0' * 64
    diff = probe._source_diff_vs_experiment(  # pylint: disable=protected-access
        experiment, current
    )
    self.assertEqual(diff[tool_path]['experiment_sha256'], '0' * 64)
    self.assertEqual(
        diff[tool_path]['current_sha256'],
        probe.sha256_file(probe._REPO_ROOT / tool_path),  # pylint: disable=protected-access
    )
    production_path = 'concordia/document/document.py'
    for entry in experiment['files']:
      if entry['path'] == production_path:
        entry['sha256'] = '0' * 64
    with self.assertRaises(ValueError):
      probe._source_diff_vs_experiment(  # pylint: disable=protected-access
          experiment, current
      )
    # Reset all previous hash mutations so the next rejection proves
    # file-list validation independently of production-source drift.
    experiment = {'files': [dict(e) for e in current['files']]}
    experiment['files'].append({'path': 'extra/file.py', 'sha256': '0' * 64})
    with self.assertRaises(ValueError):
      probe._source_diff_vs_experiment(  # pylint: disable=protected-access
          experiment, current
      )

  def test_offline_resummary_records_tool_source_diff(self):
    with tempfile.TemporaryDirectory() as tmp:
      root = pathlib.Path(tmp)
      offline_dir = _build_passing_batch(root, 'offline')
      snapshot_path = offline_dir / 'source_snapshot.json'
      snapshot = _read_json(snapshot_path)
      tool_path = 'examples/story_enactment/adjudication_chain_probe.py'
      for entry in snapshot['files']:
        if entry['path'] == tool_path:
          entry['sha256'] = '0' * 64
      probe._write_json(snapshot_path, snapshot)  # pylint: disable=protected-access
      report = _read_json(offline_dir / 'report.json')
      report['source_snapshot_sha256'] = probe.sha256_file(  # pylint: disable=protected-access
          snapshot_path
      )
      (offline_dir / 'report.json').write_text(
          json.dumps(report, ensure_ascii=False), encoding='utf-8'
      )
      manifest_path = self._manifest_for(offline_dir)
      self._run_offline(offline_dir, root / 'out', manifest_path)
      out_report = _read_json(root / 'out' / 'report.json')
      diff = out_report['offline_resummarize']['source_diff_vs_experiment']
      self.assertEqual(list(diff), [tool_path])
      self.assertEqual(diff[tool_path]['experiment_sha256'], '0' * 64)
      self.assertEqual(
          diff[tool_path]['current_sha256'],
          probe.sha256_file(probe._REPO_ROOT / tool_path),  # pylint: disable=protected-access
      )


class DryRunTest(unittest.TestCase):

  def test_dry_run_reports_zero_findings_without_real_clients(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = pathlib.Path(tmp) / 'dry'
      with mock.patch('openai.OpenAI') as fake_client:
        probe._dry_run(dry_run_dir)  # pylint: disable=protected-access
        fake_client.assert_not_called()
      summary = json.loads(
          (dry_run_dir / 'dry_run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(summary['findings'], [])
      self.assertEqual(summary['real_api_calls'], 0)
      self.assertEqual(summary['planned_adjudication_attempts'], 24)
      self.assertEqual(summary['planned_injection_attempts'], 24)


if __name__ == '__main__':
  unittest.main()
