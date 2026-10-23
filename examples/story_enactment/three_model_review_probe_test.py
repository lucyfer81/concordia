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

"""Offline tests of the three-model review probe runner.

Every test runs without network and without API keys: forwarding slots
are answered by deterministic scripted models and the credential
lookup is patched. The suite verifies the frozen-protocol properties
the three-model comparison requires: exactly 144 unique real forward
slots with an R-only schedule, the calibrated F10 history anchoring,
byte-identical prompts across models and repetitions with no label or
model-name leakage, malformed responses never counting as correct
rejections, interruption never re-sending started entries, the
repaired summary accounting (planned/started/completed/not_started/
started_no_result separated), pass gating on three-model counts plus
the completed manual reason review, and no real client construction in
dry-run, freeze or summarize.
"""

import json
import tempfile
from typing import Collection
import unittest
from unittest import mock

from concordia.contrib.components.game_master import story_resolution
from concordia.language_model import language_model

from examples.story_enactment import fixed_model_probe as fp
from examples.story_enactment import three_model_review_probe as probe

_APPROVAL = '{"valid": true, "reason": "No violation."}'
_REJECTION = json.dumps(
    {'valid': False, 'reason': 'The narration invents a voluntary action.'},
    ensure_ascii=False,
)
_FAKE_NAMES = {'m1': 'fake-model-1', 'm2': 'fake-model-2', 'm4': 'fake-model-4'}


class _ScriptedModel(language_model.LanguageModel):
  """Deterministic forward-slot stand-in with scripted responses."""

  def __init__(self, responses, *, on_call=None):
    self._responses = list(responses)
    self._on_call = on_call
    self.calls = 0

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
    del prompt, max_tokens, terminators, temperature, top_p, top_k
    del timeout, seed
    self.calls += 1
    if self._on_call is not None:
      self._on_call(self.calls)
    if self.calls > len(self._responses):
      raise AssertionError('scripted model received more calls than planned')
    return self._responses[self.calls - 1]

  def sample_choice(
      self,
      prompt: str,
      responses: Collection[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    del prompt, seed
    return 0, list(responses)[0], {}


def _patched_settings():
  """Patches the credential lookup with offline fake settings."""
  return mock.patch.object(
      probe,
      '_load_settings',
      side_effect=lambda: (
          _FAKE_NAMES,
          'fake-key',
          'http://fake',
          ['fake-key'],
      ),
  )


class PlanAndCalibrationTest(unittest.TestCase):

  def test_plan_is_144_unique_slots_in_frozen_order(self):
    plan = probe.planned_review_attempts()
    self.assertEqual(len(plan), 144)
    self.assertEqual(len(set(plan)), 144)
    self.assertEqual(probe._MAX_REAL_CALLS, 144)
    self.assertEqual(
        {case_id for case_id, _, _ in plan}, {f'F{i:02d}' for i in range(1, 17)}
    )
    self.assertEqual({tag for _, tag, _ in plan}, {'m1', 'm2', 'm4'})
    # Frozen order: repetition -> case -> m1/m2/m4.
    self.assertEqual(
        plan[:6],
        [
            ('F01', 'm1', 1),
            ('F01', 'm2', 1),
            ('F01', 'm4', 1),
            ('F02', 'm1', 1),
            ('F02', 'm2', 1),
            ('F02', 'm4', 1),
        ],
    )
    self.assertEqual(plan[-1], ('F16', 'm4', 3))

  def test_model_mapping_is_fixed_and_excludes_model_3(self):
    self.assertEqual(
        probe._EXPECTED_MODEL_NAMES,
        {
            'm1': 'gemini-3.8-flash-high',
            'm2': 'gpt-6-luna',
            'm4': 'gpt-6.1-sol',
        },
    )
    self.assertEqual(
        probe._ENV_MODEL_KEYS,
        {'m1': 'AI_MODEL_1', 'm2': 'AI_MODEL_2', 'm4': 'AI_MODEL_4'},
    )
    self.assertNotIn('AI_MODEL_3', probe._ENV_MODEL_KEYS.values())
    # The original two-model probe keeps its own config untouched.
    self.assertEqual(fp._MODEL_TAGS, ('m1', 'm2'))
    self.assertEqual(fp._MAX_REAL_CALLS, 120)

  def test_history_differs_from_batch2_only_at_event_1(self):
    self.assertEqual(len(probe._HISTORY), 24)
    batch2 = fp._shared_history()
    for index, (new, old) in enumerate(zip(probe._HISTORY, batch2)):
      if index == 1:
        self.assertNotEqual(new, old)
      else:
        self.assertEqual(new, old)

  def test_revised_event_1_is_a_canonical_record_with_constraints(self):
    event = probe._REVISED_EVENT_1
    speech, _, narration = event.partition('\n')
    # Contains the lawyer's speech and a committed own-action record.
    self.assertTrue(speech.startswith('周律师: '))
    self.assertIn('周律师当众展示已启封的遗嘱信封及其上的残留火漆', narration)
    self.assertIn('随后收回信封与遗嘱文本，自行保管', narration)
    # No other player's viewing/agreement/endorsement choice, no seal
    # authentication, no new secret (no date, no authenticity claim).
    for forbidden in ('众人', '同意', '认可', '为真', '真伪', '落款'):
      self.assertNotIn(forbidden, narration)

  def test_f10_fixture_is_the_calibrated_pure_restatement(self):
    f10 = probe.review_case_by_id('F10')
    self.assertEqual(
        f10.stage_directions,
        '遗嘱信封及其上的残留火漆仍由周律师保管，遗嘱文本亦由其收执。',
    )
    self.assertEqual(f10.action_intent, '')
    self.assertIsNone(f10.action_result)
    self.assertIsNone(f10.intervention)
    self.assertTrue(f10.expected_valid)
    self.assertTrue(f10.depends_on_out_of_window_history)

  def test_f10_anchor_stays_outside_the_director_window(self):
    window = probe.director_window()
    self.assertEqual(len(window), 20)
    self.assertNotIn(probe._REVISED_EVENT_1, window)
    self.assertIn(probe._REVISED_EVENT_1, probe._HISTORY)

  def test_other_fifteen_fixtures_are_byte_identical_to_batch2(self):
    for case in fp._REVIEW_CASES:
      if case.case_id == 'F10':
        continue
      self.assertEqual(
          probe.review_case_by_id(case.case_id), case, case.case_id
      )

  def test_label_distribution_is_18_legal_30_illegal_per_model(self):
    cases = probe._REVIEW_CASES
    valid = sum(1 for case in cases if case.expected_valid)
    self.assertEqual(valid * 3, 18)
    self.assertEqual((len(cases) - valid) * 3, 30)


class ReviewAttemptTest(unittest.TestCase):

  def _run(self, case_id, responses, *, on_call=None, model_tag='m1'):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = fp.pathlib.Path(tmp)
      result = fp._run_review_attempt(
          case=probe.review_case_by_id(case_id),
          real_model=_ScriptedModel(responses, on_call=on_call),
          model_tag=model_tag,
          model_name=_FAKE_NAMES[model_tag],
          repetition=1,
          gm=fp.gm_params(),
          out_dir=out_dir,
          secrets=['fake-key'],
          history_text=probe.public_history_text(),
      )
      files = {path.name for path in out_dir.iterdir()}
    return result, files

  def test_slots_records_and_classification(self):
    result, files = self._run('F01', [_APPROVAL])
    self.assertEqual(result['synthetic_generation_calls'], 1)
    self.assertEqual(result['real_api_calls'], 1)
    self.assertEqual(result['status'], 'completed')
    self.assertEqual(result['classification'], 'correct')
    self.assertTrue(result['canonical_matches_fixture'])
    self.assertIn('F01__m1__r1_started.json', files)
    self.assertIn('F01__m1__r1_response.txt', files)
    self.assertEqual(result['request_params']['max_tokens'], 1000)

  def test_calibrated_f10_prompt_carries_the_revised_event(self):
    with tempfile.TemporaryDirectory() as tmp:
      generation, review = probe._capture_review_prompts(
          probe.review_case_by_id('F10'), fp.pathlib.Path(tmp)
      )
    history_json = fp.json.dumps(
        probe.public_history_text(), ensure_ascii=False
    )
    for prompt in (generation, review):
      self.assertIn(probe._REVISED_EVENT_1.split('\n')[1][:20], prompt)
      self.assertIn(history_json, prompt)
      self.assertNotIn('gemini-3.8-flash-high', prompt)
      self.assertNotIn('gpt-6-luna', prompt)
      self.assertNotIn('gpt-6.1-sol', prompt)

  def test_malformed_is_never_counted_as_a_correct_rejection(self):
    result, _ = self._run('F03', ['not json at all'])
    self.assertEqual(result['classification'], 'malformed')
    self.assertNotEqual(result['classification'], 'correct')
    result, _ = self._run('F03', ['{"valid": "false", "reason": "x"}'])
    self.assertEqual(result['classification'], 'malformed')

  def test_semantic_outcomes_classify_against_labels(self):
    result, _ = self._run('F01', [_REJECTION])
    self.assertEqual(result['classification'], 'false_rejection')
    self.assertEqual(result['stage_outcome'], 'rejected')
    result, _ = self._run('F03', [_APPROVAL])
    self.assertEqual(result['classification'], 'missed_detection')

  def test_api_error_is_recorded_once_without_response(self):
    calls = []

    def explode(call_index):
      calls.append(call_index)
      raise ConnectionError('connection reset by peer')

    result, files = self._run('F02', [], on_call=explode)
    self.assertEqual(result['status'], 'api_error')
    self.assertEqual(len(calls), 1, 'the forward must not be retried')
    self.assertNotIn('F02__m1__r1_response.txt', files)
    self.assertIn('F02__m1__r1_started.json', files)

  def test_enrich_result_hook_attaches_provider_fields(self):
    observed = [{
        'requested_model': 'req-name',
        'provider_model': 'provider-name',
        'usage': {'total_tokens': 12},
        'observed_at_utc': 'now',
    }]

    def enrich(result):
      result['provider_model'] = observed[-1]['provider_model']
      result['token_usage'] = observed[-1]['usage']
      return result

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = fp.pathlib.Path(tmp)
      result = fp._run_review_attempt(
          case=probe.review_case_by_id('F01'),
          real_model=_ScriptedModel([_APPROVAL]),
          model_tag='m4',
          model_name='fake-model-4',
          repetition=2,
          gm=fp.gm_params(),
          out_dir=out_dir,
          secrets=['fake-key'],
          history_text=probe.public_history_text(),
          enrich_result=enrich,
      )
      written = json.loads(
          (out_dir / 'F01__m4__r2_result.json').read_text(encoding='utf-8')
      )
    self.assertEqual(result['provider_model'], 'provider-name')
    self.assertEqual(written['provider_model'], 'provider-name')
    self.assertEqual(written['token_usage'], {'total_tokens': 12})


class ProviderObservationTest(unittest.TestCase):

  def test_proxy_archives_requested_and_provider_model_and_usage(self):
    class _Usage:

      def model_dump(self):
        return {'total_tokens': 7}

    class _Response:
      model = 'provider-returned-name'
      usage = _Usage()

    class _Completions:

      def create(self, **kwargs):
        return _Response()

    class _Chat:
      completions = _Completions()

    class _Client:
      chat = _Chat()
      api_key = 'secret-key'

      def with_options(self, **kwargs):
        del kwargs
        return self

    records = []
    client = probe._ObservingClient(_Client(), records)
    # with_options must keep the observation path.
    tuned = client.with_options(max_retries=0)
    response = tuned.chat.completions.create(
        model='requested-name', messages=[]
    )
    self.assertEqual(response.model, 'provider-returned-name')
    self.assertEqual(len(records), 1)
    self.assertEqual(records[0]['requested_model'], 'requested-name')
    self.assertEqual(records[0]['provider_model'], 'provider-returned-name')
    self.assertEqual(records[0]['usage'], {'total_tokens': 7})
    # Non-chat attributes delegate to the real client.
    self.assertEqual(client.api_key, 'secret-key')

  def test_proxy_never_masks_a_provider_exception(self):
    class _Completions:

      def create(self, **kwargs):
        raise ConnectionError('upstream gone')

    class _Chat:
      completions = _Completions()

    class _Client:
      chat = _Chat()

    records = []
    client = probe._ObservingClient(_Client(), records)
    with self.assertRaises(ConnectionError):
      client.chat.completions.create(model='x')
    self.assertEqual(records, [])


class FreezeAndRunDisciplineTest(unittest.TestCase):

  def _freeze(self, batch_dir):
    with _patched_settings():
      probe._freeze(batch_dir)

  def test_freeze_refuses_existing_directory_and_writes_artefacts(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = fp.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      with self.assertRaises(ValueError):
        self._freeze(batch_dir)
      protocol = json.loads(
          (batch_dir / 'protocol.json').read_text(encoding='utf-8')
      )
      self.assertEqual(protocol['models'], _FAKE_NAMES)
      self.assertEqual(len(protocol['models']), 3)
      self.assertEqual(protocol['schedule']['planned_review_calls'], 144)
      self.assertEqual(protocol['schedule']['planned_adjudication_calls'], 0)
      self.assertEqual(protocol['schedule']['max_real_calls'], 144)
      self.assertIn('m1/m2/m4', protocol['schedule']['execution_order'])
      self.assertIn(
          'not evaluated', protocol['pass_criteria']['ra_dual_model_gate']
      )
      fixtures = json.loads(
          (batch_dir / 'fixtures.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(fixtures['review_cases']), 16)
      self.assertNotIn('adjudication_cases', fixtures)
      self.assertIn('f10_calibration', fixtures)
      self.assertEqual(
          fixtures['f10_calibration']['history_event_1_before'],
          probe._BATCH2_EVENT_1,
      )
      self.assertEqual(
          fixtures['f10_calibration']['f10_stage_directions_after'],
          probe._F10_STAGE_DIRECTIONS,
      )
      self.assertEqual(
          fixtures['shared_context']['history_events'][1],
          probe._REVISED_EVENT_1,
      )

  def test_freeze_constructs_no_real_client(self):
    with tempfile.TemporaryDirectory() as tmp:
      with mock.patch('openai.OpenAI') as fake_client:
        self._freeze(fp.pathlib.Path(tmp) / 'batch')
        fake_client.assert_not_called()

  def test_verify_frozen_detects_fixture_tampering(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = fp.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      fixtures_path = batch_dir / 'fixtures.json'
      fixtures = json.loads(fixtures_path.read_text(encoding='utf-8'))
      fixtures['review_cases'][0]['expected_valid'] = not fixtures[
          'review_cases'
      ][0]['expected_valid']
      fixtures_path.write_text(
          json.dumps(fixtures, ensure_ascii=False, indent=2) + '\n',
          encoding='utf-8',
      )
      with _patched_settings():
        with self.assertRaises(ValueError):
          probe._verify_frozen(batch_dir)

  def test_run_group_requires_frozen_artefacts(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = fp.pathlib.Path(tmp) / 'batch'
      batch_dir.mkdir()
      with self.assertRaises(ValueError):
        probe._run_review_group(batch_dir)

  def test_started_attempt_is_never_resent(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = fp.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      review_dir = batch_dir / 'review'
      # Simulate an interrupted attempt: started, no response, no result.
      stem = 'F01__m1__r1'
      (review_dir / f'{stem}_started.json').write_text('{}', encoding='utf-8')
      scripted = _ScriptedModel([_APPROVAL] * 300)
      with _patched_settings(), mock.patch.object(
          probe,
          '_create_live_model',
          side_effect=lambda name, **_: (scripted, []),
      ):
        probe._run_review_group(batch_dir)
      self.assertFalse((review_dir / f'{stem}_result.json').exists())
      self.assertEqual(scripted.calls, 143, 'only the 143 unstarted entries')
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(report['counts']['real_api_calls'], 143)
      self.assertEqual(report['counts']['synthetic_generation_calls'], 143)

  def test_cap_stops_the_group(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = fp.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      scripted = _ScriptedModel([_APPROVAL] * 300)
      with _patched_settings(), mock.patch.object(
          probe,
          '_create_live_model',
          side_effect=lambda name, **_: (scripted, []),
      ):
        with self.assertRaises(ValueError):
          probe._run_review_group(batch_dir, real_call_cap=3)
      self.assertEqual(scripted.calls, 3)


class SummarizeAccountingTest(unittest.TestCase):

  def _make_batch(self, tmp):
    batch_dir = fp.pathlib.Path(tmp) / 'batch'
    with _patched_settings():
      probe._freeze(batch_dir)
    return batch_dir

  def _write_result(self, batch_dir, stem, *, status, classification=None):
    record = {
        'attempt_id': stem,
        'stage': 'review',
        'status': status,
        'classification': classification,
        'cross_check_ok': True,
        'canonical_matches_fixture': True,
        'real_api_calls': 1,
        'synthetic_generation_calls': 1,
    }
    (batch_dir / 'review' / f'{stem}_result.json').write_text(
        json.dumps(record, ensure_ascii=False) + '\n', encoding='utf-8'
    )
    if status == 'completed':
      (batch_dir / 'review' / f'{stem}_response.txt').write_text(
          _APPROVAL, encoding='utf-8'
      )

  def _write_review_file(self, batch_dir, entries):
    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    (batch_dir / 'human_reason_review.jsonl').write_text(
        '\n'.join(lines) + '\n', encoding='utf-8'
    )

  def _summarize_with_report(self, batch_dir, real_report):
    """Runs _summarize against a controlled report under fake settings."""
    with _patched_settings(), mock.patch.object(
        probe, '_load_report', side_effect=lambda _dir: real_report
    ), mock.patch('openai.OpenAI') as fake_client:
      probe._summarize(batch_dir)
      fake_client.assert_not_called()

  def _fill_all_results(self, batch_dir, *, classification='correct'):
    plan = probe.planned_review_attempts()
    for case_id, tag, rep in plan:
      stem = f'{case_id}__{tag}__r{rep}'
      (batch_dir / 'review' / f'{stem}_started.json').write_text(
          '{}', encoding='utf-8'
      )
      self._write_result(
          batch_dir, stem, status='completed', classification=classification
      )
    real_report = json.loads(
        (batch_dir / 'report.json').read_text(encoding='utf-8')
    )
    real_report['counts']['real_api_calls'] = 144
    return real_report

  def _default_reviews(self):
    return [
        {
            'attempt_id': f'{case_id}__{tag}__r{rep}',
            'status': 'reviewed',
            'reason_fact_based': True,
            'reason_conclusion_matches_verdict': True,
            'fixture_or_label_dispute': False,
        }
        for case_id, tag, rep in probe.planned_review_attempts()
    ]

  def test_counts_separate_not_started_started_no_result_and_api_error(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._make_batch(tmp)
      # m1: 46 correct + 1 malformed + 1 api_error; one started-no-result;
      # one not-started.
      stems = [
          f'F{case:02d}__m1__r{rep}'
          for rep in (1, 2, 3)
          for case in range(1, 17)
      ]
      for stem in stems:
        if stem == 'F16__m1__r3':
          continue  # never started
        (batch_dir / 'review' / f'{stem}_started.json').write_text(
            '{}', encoding='utf-8'
        )
        if stem == 'F15__m1__r3':
          continue  # started, no result -> started_no_result
        if stem == 'F14__m1__r3':
          self._write_result(
              batch_dir, stem, status='api_error', classification=None
          )
        elif stem == 'F13__m1__r3':
          self._write_result(
              batch_dir, stem, status='completed', classification='malformed'
          )
        else:
          self._write_result(
              batch_dir, stem, status='completed', classification='correct'
          )
      real_report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      real_report['counts']['real_api_calls'] = 48
      self._summarize_with_report(batch_dir, real_report)
      summary = real_report['summary']
      m1 = summary['review']['m1']
      self.assertEqual(m1['planned'], 48)
      self.assertEqual(m1['started'], 47)
      self.assertEqual(m1['completed'], 45)
      self.assertEqual(m1['not_started'], 1)
      self.assertEqual(m1['started_no_result'], 1)
      self.assertEqual(m1['responses_archived'], 45)
      self.assertEqual(m1['correct'], 44)
      self.assertEqual(m1['malformed'], 1)
      self.assertEqual(m1['statuses'], {'api_error': 1})
      self.assertFalse(m1['pass'])
      self.assertFalse(summary['review_pass'])
      self.assertFalse(real_report['overall_pass'])
      self.assertIn('not evaluated', summary['ra_dual_model_gate'])

  def test_pass_requires_all_three_models_and_the_full_review(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._make_batch(tmp)
      real_report = self._fill_all_results(batch_dir)
      # All three models complete, but the manual reason review is
      # missing: overall_pass must stay False.
      self._summarize_with_report(batch_dir, real_report)
      self.assertTrue(real_report['summary']['review_pass'])
      self.assertFalse(
          real_report['summary']['human_reason_review']['complete']
      )
      self.assertFalse(real_report['overall_pass'])
      # Completing the review for all 144 flips the round to pass.
      self._write_review_file(batch_dir, self._default_reviews())
      self._summarize_with_report(batch_dir, real_report)
      self.assertTrue(real_report['summary']['human_reason_review']['complete'])
      self.assertTrue(real_report['overall_pass'])
      self.assertTrue(real_report['summary']['semantic_validation_established'])

  def test_one_missing_review_or_defective_reason_blocks_pass(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._make_batch(tmp)
      real_report = self._fill_all_results(batch_dir)
      entries = self._default_reviews()
      # One reason reviewed as fact-free: label stays correct but the
      # round cannot be described as semantic validation passed.
      entries[0]['reason_fact_based'] = False
      self._write_review_file(batch_dir, entries)
      self._summarize_with_report(batch_dir, real_report)
      self.assertTrue(real_report['summary']['review_pass'])
      self.assertFalse(real_report['overall_pass'])
      self.assertFalse(
          real_report['summary']['semantic_validation_established']
      )

  def test_dispute_is_listed_but_keeps_original_scoring(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = self._make_batch(tmp)
      real_report = self._fill_all_results(batch_dir)
      entries = self._default_reviews()
      for entry in entries:
        if entry['attempt_id'].startswith('F13'):
          entry['fixture_or_label_dispute'] = True
          entry['dispute_case_id'] = 'F13'
      self._write_review_file(batch_dir, entries)
      self._summarize_with_report(batch_dir, real_report)
      review = real_report['summary']['human_reason_review']
      self.assertEqual(review['fixture_or_label_disputes'], ['F13'])
      # Machine scoring is unchanged by the dispute; overall passes but
      # semantic validation is not claimed while a dispute is open.
      self.assertTrue(real_report['overall_pass'])
      self.assertFalse(
          real_report['summary']['semantic_validation_established']
      )


class DryRunTest(unittest.TestCase):

  def test_dry_run_captures_prompts_without_real_clients(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = fp.pathlib.Path(tmp) / 'dry'
      with mock.patch('openai.OpenAI') as fake_client:
        probe._dry_run(dry_run_dir)
        fake_client.assert_not_called()
      summary = json.loads(
          (dry_run_dir / 'dry_run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(summary['findings'], [])
      self.assertEqual(summary['planned_review_attempts'], 144)
      self.assertEqual(summary['real_client_constructions'], 0)
      prompt_dir = dry_run_dir / 'prompts'
      names = {path.name for path in prompt_dir.iterdir()}
      self.assertIn('F01_generation_prompt.txt', names)
      self.assertIn('F10_review_prompt.txt', names)
      self.assertEqual(len(names), 16 * 2)

  def test_dry_run_refuses_existing_directory(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = fp.pathlib.Path(tmp) / 'dry'
      dry_run_dir.mkdir()
      with self.assertRaises(ValueError):
        probe._dry_run(dry_run_dir)


class PromptBindingTest(unittest.TestCase):
  """Byte-identical prompts across the three models and repetitions."""

  def test_all_prompts_are_byte_identical_and_free_of_metadata(self):
    gm = fp.gm_params()
    history = probe.public_history_text()
    for case in probe._REVIEW_CASES:
      prompts = set()
      for model_tag in probe._MODEL_TAGS:
        for repetition in (1, 2, 3):
          director, entity, _ = fp._make_director(player=case.player, gm=gm)
          fp._open_turn(entity, case.player, case.speech, case.action_intent)
          if case.action_result is not None:
            director.record_action_result(case.action_result)
          if case.intervention is not None:
            director.record_intervention_candidate(
                reason=case.intervention['reason'],
                candidate_before=case.intervention['candidate_before'],
                candidate_after=case.intervention['candidate_after'],
                trace=fp._INTERVENTION_TRACE,
            )
          wrapper = fp._FinalOutputProbeModel(
              fp._DryRunReviewer(), case.stage_directions
          )
          chain = fp.interactive_document.InteractiveDocument(wrapper)
          step = story_resolution.make_final_output_step(
              director,
              immutable_facts=gm['immutable_facts'],
              character_constraints=gm['character_constraints'],
              player_names=gm['player_names'],
              world_constraints=gm['world_constraints'],
              allowed_environment_events=gm['allowed_environment_events'],
              public_facts=gm['public_facts'],
              public_history=lambda: history,
          )
          step(chain, fp._premise_of_review_case(case), case.player)
          self.assertEqual(wrapper.generation_calls, 1)
          self.assertEqual(wrapper.review_calls, 1)
          prompts.add(wrapper.captured[0]['prompt'])
          prompts.add(wrapper.captured[1]['prompt'])
      self.assertEqual(
          len(prompts), 2, f'{case.case_id} prompts are not byte-identical'
      )
      for prompt in prompts:
        for model_name in probe._EXPECTED_MODEL_NAMES.values():
          self.assertNotIn(model_name, prompt)
        self.assertNotIn(case.human_rationale, prompt)
        self.assertNotIn(case.fact_sources, prompt)
        self.assertNotIn(str(case.expected_valid).lower() + '\n', prompt)


if __name__ == '__main__':
  unittest.main()
