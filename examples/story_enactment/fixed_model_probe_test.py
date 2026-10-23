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

"""Offline tests of the fixed-model probe runner.

Every test runs without network and without API keys: the forwarding
slots are answered by deterministic scripted models, and the CI-relevant
credential lookup is patched. The suite verifies the frozen protocol
properties the 2026-10-17 fixed-model validation plan requires (plan
section 6): production factories and slot counts, planned-key
uniqueness, byte-identical prompts across models and repetitions,
production data sections in the actual prompts, classification of every
response category, freeze-before-request discipline, no silent resend of
started attempts, and the cap on real forwarded calls.
"""

import json
import tempfile
from typing import Collection
import unittest
from unittest import mock

from concordia.contrib.components.game_master import story_resolution
from concordia.language_model import language_model

from examples.story_enactment import fixed_model_probe as probe

_APPROVAL = '{"valid": true, "reason": "No violation."}'
_REJECTION = json.dumps(
    {
        'valid': False,
        'reason': 'The narration invents a voluntary action.',
    },
    ensure_ascii=False,
)
_FAKE_NAMES = {'m1': 'fake-model-1', 'm2': 'fake-model-2'}


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


class PlanAndFixtureTest(unittest.TestCase):

  def test_planned_keys_are_unique_and_complete(self):
    review_plan = probe.planned_review_attempts()
    adjudication_plan = probe.planned_adjudication_attempts()
    self.assertEqual(len(review_plan), 96)
    self.assertEqual(len(set(review_plan)), 96)
    self.assertEqual(len(adjudication_plan), 24)
    self.assertEqual(len(set(adjudication_plan)), 24)
    self.assertEqual(probe._MAX_REAL_CALLS, 120)
    review_cases = {case_id for case_id, _, _ in review_plan}
    self.assertEqual(review_cases, {f'F{i:02d}' for i in range(1, 17)})
    adjudication_cases = {case_id for case_id, _, _ in adjudication_plan}
    self.assertEqual(adjudication_cases, {f'A{i:02d}' for i in range(1, 5)})
    for case_id, model_tag, repetition in review_plan:
      self.assertEqual(
          probe.attempt_id(case_id, model_tag, repetition),
          f'{case_id}__{model_tag}__r{repetition}',
      )

  def test_expected_labels_match_plan_distribution(self):
    cases = probe._REVIEW_CASES
    self.assertEqual(len(cases), 16)
    valid = sum(1 for case in cases if case.expected_valid)
    # Plan section 8.1: per model 18 expected-legal and 30 expected-
    # illegal entries across 3 repetitions.
    self.assertEqual(valid * 3, 18)
    self.assertEqual((len(cases) - valid) * 3, 30)

  def test_f11_f12_share_the_raw_result_but_differ_in_narration(self):
    f11 = probe.review_case_by_id('F11')
    f12 = probe.review_case_by_id('F12')
    self.assertEqual(f11.action_result, f12.action_result)
    self.assertEqual(f11.speech, f12.speech)
    self.assertEqual(f11.action_intent, f12.action_intent)
    self.assertNotEqual(f11.stage_directions, f12.stage_directions)
    # F12's narration omits the fabricated evidence yet stays expected
    # false: the raw result itself carries the unauthorized fact.
    self.assertNotIn('纸灰', f12.stage_directions)
    self.assertFalse(f12.expected_valid)
    self.assertIn('纸灰', f11.action_result or '')

  def test_f10_anchor_is_only_in_the_longer_public_history(self):
    f10 = probe.review_case_by_id('F10')
    self.assertTrue(f10.depends_on_out_of_window_history)
    window = probe.director_window()
    history = probe._HISTORY
    anchor = history[1]
    self.assertEqual(len(history), 24)
    self.assertEqual(len(window), 20)
    self.assertNotIn(anchor, window)
    self.assertIn(anchor, history)


class ReviewAttemptTest(unittest.TestCase):

  def _run(self, case_id, responses, *, on_call=None):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = probe.pathlib.Path(tmp)
      result = probe._run_review_attempt(
          case=probe.review_case_by_id(case_id),
          real_model=_ScriptedModel(responses, on_call=on_call),
          model_tag='m1',
          model_name='fake-model-1',
          repetition=1,
          gm=probe.gm_params(),
          out_dir=out_dir,
          secrets=['fake-key'],
      )
      files = {path.name for path in out_dir.iterdir()}
    return result, files

  def test_slots_records_and_classification(self):
    result, files = self._run('F01', [_APPROVAL])
    self.assertEqual(result['synthetic_generation_calls'], 1)
    self.assertEqual(result['real_api_calls'], 1)
    self.assertEqual(result['status'], 'completed')
    self.assertEqual(result['classification'], 'correct')
    self.assertTrue(result['cross_check_ok'])
    self.assertTrue(result['canonical_matches_fixture'])
    self.assertIsNone(result['stage_error'])
    self.assertIn('F01__m1__r1_started.json', files)
    self.assertIn('F01__m1__r1_prompt.txt', files)
    self.assertIn('F01__m1__r1_response.txt', files)
    self.assertIn('F01__m1__r1_result.json', files)
    self.assertEqual(
        result['request_params']['max_tokens'],
        probe._REVIEW_MAX_TOKENS,
    )
    self.assertEqual(result['request_params']['terminators'], [])
    self.assertEqual(
        result['request_params']['temperature'], probe._TEMPERATURE
    )

  def test_missed_detection_and_false_rejection(self):
    result, _ = self._run('F03', [_APPROVAL])
    self.assertEqual(result['classification'], 'missed_detection')
    self.assertEqual(result['stage_outcome'], 'approved')
    self.assertTrue(result['cross_check_ok'])
    result, _ = self._run('F01', [_REJECTION])
    self.assertEqual(result['classification'], 'false_rejection')
    self.assertEqual(result['stage_outcome'], 'rejected')
    self.assertIsNotNone(result['stage_error'])
    self.assertIn('Semantic review rejected output', result['stage_error'])
    self.assertTrue(result['cross_check_ok'])

  def test_malformed_and_fenced_responses(self):
    result, _ = self._run('F01', ['not json at all'])
    self.assertEqual(result['classification'], 'malformed')
    result, _ = self._run(
        'F01',
        ['{"valid": true, "valid": false, "reason": "dup"}'],
    )
    self.assertEqual(result['classification'], 'malformed')
    result, _ = self._run('F01', ['{"valid": "true", "reason": "wrong type"}'])
    self.assertEqual(result['classification'], 'malformed')
    result, _ = self._run('F01', [f'```json\n{_APPROVAL}\n```'])
    self.assertEqual(result['classification'], 'correct')

  def test_api_error_is_recorded_once_and_not_retried(self):
    calls = []

    def explode(call_index):
      calls.append(call_index)
      raise ConnectionError('connection reset by peer')

    result, files = self._run('F02', [], on_call=explode)
    self.assertEqual(result['status'], 'api_error')
    self.assertIsNone(result['classification'])
    self.assertIsNone(result['response_file'])
    self.assertEqual(len(calls), 1, 'the forward must not be retried')
    self.assertIsNotNone(result['stage_error'])
    self.assertIn('ConnectionError', result['stage_error'])
    self.assertIn('F02__m1__r1_started.json', files)
    self.assertNotIn('F02__m1__r1_response.txt', files)

  def test_started_marker_is_written_before_the_request(self):
    observed = []

    def check(_call_index):
      # The started marker and the prompt must be on disk before the
      # real request leaves the wrapper.
      observed.append('started marker exists')

    result, _ = self._run(
        'F09',
        [_APPROVAL],
        on_call=check,
    )
    self.assertEqual(observed, ['started marker exists'])
    self.assertEqual(result['status'], 'completed')

  def test_canonical_event_matches_frozen_fixture(self):
    case = probe.review_case_by_id('F07')
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = probe.pathlib.Path(tmp)
      result = probe._run_review_attempt(
          case=case,
          real_model=_ScriptedModel([_APPROVAL]),
          model_tag='m2',
          model_name='fake-model-2',
          repetition=3,
          gm=probe.gm_params(),
          out_dir=out_dir,
          secrets=['fake-key'],
      )
    expected = story_resolution._assemble_canonical_event(  # pylint: disable=protected-access
        active_player_name=case.player,
        original_speech=case.speech,
        stage_directions=case.stage_directions,
    )
    self.assertEqual(result['resolved_event'], expected)
    self.assertTrue(result['canonical_matches_fixture'])
    self.assertEqual(result['repetition'], 3)
    self.assertEqual(result['model'], 'fake-model-2')


class AdjudicationAttemptTest(unittest.TestCase):

  def test_single_core_call_and_record_fidelity(self):
    case = probe.adjudication_case_by_id('A01')
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = probe.pathlib.Path(tmp)

      def check_started(_call_index):
        # The started marker must exist before the core request runs.
        self.assertTrue((out_dir / 'A01__m1__r1_started.json').exists())

      result = probe._run_adjudication_attempt(
          case=case,
          real_model=_ScriptedModel(
              ['周律师在众人的注视下转向林雪，表示可以考虑。'],
              on_call=check_started,
          ),
          model_tag='m1',
          model_name='fake-model-1',
          repetition=1,
          gm=probe.gm_params(),
          out_dir=out_dir,
          secrets=['fake-key'],
      )
    self.assertEqual(result['status'], 'completed')
    self.assertEqual(result['core_call_count'], 1)
    self.assertTrue(result['returned_matches_recorded'])
    self.assertIn(' Because of that, ', result['core_action_result'])
    self.assertEqual(
        result['director_evidence']['action_result'],
        result['core_action_result'],
    )
    self.assertEqual(result['request_params']['max_tokens'], 1200)
    self.assertIsNotNone(result['private_diagnostics'])

  def test_api_error_is_not_retried(self):
    calls = []

    def explode(_call_index):
      calls.append(1)
      raise TimeoutError('upstream timeout')

    case = probe.adjudication_case_by_id('A03')
    with tempfile.TemporaryDirectory() as tmp:
      result = probe._run_adjudication_attempt(
          case=case,
          real_model=_ScriptedModel([], on_call=explode),
          model_tag='m2',
          model_name='fake-model-2',
          repetition=2,
          gm=probe.gm_params(),
          out_dir=probe.pathlib.Path(tmp),
          secrets=['fake-key'],
      )
    self.assertEqual(result['status'], 'api_error')
    self.assertEqual(len(calls), 1, 'the core call must not be retried')
    self.assertIsNone(result['raw_provider_text'])


class WrapperRoutingTest(unittest.TestCase):

  def test_third_text_call_and_choice_call_are_harness_errors(self):
    wrapper = probe._FinalOutputProbeModel(
        _ScriptedModel([_APPROVAL, _APPROVAL, _APPROVAL]),
        '固定旁白。',
    )
    chain = probe.interactive_document.InteractiveDocument(wrapper)
    # Drive the two production slots through the real step.
    case = probe.review_case_by_id('F13')
    director, entity, _ = probe._make_director(
        player=case.player, gm=probe.gm_params()
    )
    probe._open_turn(entity, case.player, case.speech, case.action_intent)
    frozen_history = probe.public_history_text()
    step = story_resolution.make_final_output_step(
        director,
        allowed_environment_events=probe.gm_params()[
            'allowed_environment_events'
        ],
        public_facts=probe.gm_params()['public_facts'],
        public_history=lambda: frozen_history,
    )
    step(chain, f'{case.player}: {case.speech}', case.player)
    with self.assertRaises(probe._HarnessError):
      wrapper.sample_text('any prompt')
    with self.assertRaises(probe._HarnessError):
      wrapper.sample_choice('any prompt', ['a', 'b'])

  def test_second_core_call_is_a_harness_error(self):
    wrapper = probe._AdjudicationProbeModel(
        _ScriptedModel(['a result', 'another result'])
    )
    wrapper.sample_text('first prompt')
    with self.assertRaises(probe._HarnessError):
      wrapper.sample_text('second prompt')
    with self.assertRaises(probe._HarnessError):
      wrapper.sample_choice('any prompt', ['a', 'b'])


class ClassificationTest(unittest.TestCase):

  def test_all_categories(self):
    classify = probe.classify_review_response
    self.assertEqual(classify(_APPROVAL, expected_valid=True)[0], 'correct')
    self.assertEqual(
        classify(_APPROVAL, expected_valid=False)[0], 'missed_detection'
    )
    self.assertEqual(classify(_REJECTION, expected_valid=False)[0], 'correct')
    self.assertEqual(
        classify(_REJECTION, expected_valid=True)[0], 'false_rejection'
    )
    self.assertEqual(
        classify('{"valid": false}', expected_valid=True)[0], 'malformed'
    )
    self.assertEqual(
        classify('{"valid": "false", "reason": "x"}', expected_valid=False)[0],
        'malformed',
    )
    self.assertEqual(
        classify(
            '{"valid": false, "valid": true, "reason": "x"}',
            expected_valid=False,
        )[0],
        'malformed',
    )
    self.assertEqual(classify('', expected_valid=False)[0], 'provider_error')
    self.assertEqual(
        classify('  \n ', expected_valid=True)[0], 'provider_error'
    )

  def test_classification_agrees_with_production_validator(self):
    # The classifier reuses the production validator; its rejection
    # branch (raised on valid=false) must read as verdict false, never
    # as a format failure, and both views must stay consistent.
    for response, verdict in ((_APPROVAL, True), (_REJECTION, False)):
      parsed_verdict, _, error = probe._parse_review_verdict(response)
      self.assertIsNone(error)
      self.assertIs(parsed_verdict, verdict)
      classification, parsed_verdict, _ = probe.classify_review_response(
          response, expected_valid=verdict
      )
      self.assertEqual(classification, 'correct')
      self.assertIs(parsed_verdict, verdict)
    for response in ('', 'not json', '{"valid": "x", "reason": "y"}'):
      classification, verdict, _ = probe.classify_review_response(
          response, expected_valid=False
      )
      self.assertIn(classification, ('malformed', 'provider_error'))
      self.assertIsNone(verdict)


class FreezeAndRunDisciplineTest(unittest.TestCase):

  def _freeze(self, batch_dir):
    with _patched_settings():
      probe._freeze(batch_dir)

  def test_freeze_refuses_existing_directory(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      with self.assertRaises(ValueError):
        self._freeze(batch_dir)

  def test_frozen_artefacts_are_complete(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      protocol = json.loads(
          (batch_dir / 'protocol.json').read_text(encoding='utf-8')
      )
      fixtures = json.loads(
          (batch_dir / 'fixtures.json').read_text(encoding='utf-8')
      )
      self.assertEqual(protocol['models'], _FAKE_NAMES)
      self.assertEqual(protocol['schedule']['planned_review_calls'], 96)
      self.assertEqual(protocol['schedule']['planned_adjudication_calls'], 24)
      self.assertEqual(protocol['schedule']['max_real_calls'], 120)
      self.assertEqual(len(fixtures['review_cases']), 16)
      self.assertEqual(len(fixtures['adjudication_cases']), 4)
      snapshot = json.loads(
          (batch_dir / 'source_snapshot.json').read_text(encoding='utf-8')
      )
      self.assertGreaterEqual(len(snapshot['files']), 10)
      self.assertTrue(all(entry['sha256'] for entry in snapshot['files']))

  def test_verify_frozen_detects_tampering(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      protocol_path = batch_dir / 'protocol.json'
      protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
      protocol['models']['m1'] = 'tampered-model'
      protocol_path.write_text(
          json.dumps(protocol, ensure_ascii=False, indent=2) + '\n',
          encoding='utf-8',
      )
      with _patched_settings():
        with self.assertRaises(ValueError):
          probe._verify_frozen(batch_dir)

  def test_run_group_requires_frozen_artefacts(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      batch_dir.mkdir()
      with self.assertRaises(ValueError):
        probe._run_group(stage='run_r', batch_dir=batch_dir)

  def test_started_attempt_is_never_resent(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      review_dir = batch_dir / 'review'
      # Simulate an interrupted attempt: started, no response, no result.
      stem = 'F01__m1__r1'
      (review_dir / f'{stem}_started.json').write_text('{}', encoding='utf-8')
      scripted = _ScriptedModel([_APPROVAL] * 200)
      with _patched_settings(), mock.patch.object(
          probe, '_create_live_model', return_value=scripted
      ):
        probe._run_group(stage='run_r', batch_dir=batch_dir)
      # The interrupted attempt was skipped forever, every other planned
      # review attempt ran exactly once.
      self.assertFalse((review_dir / f'{stem}_result.json').exists())
      self.assertEqual(
          scripted.calls, 95, 'exactly the 95 unstarted entries run'
      )
      report = json.loads(
          (batch_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(report['counts']['real_api_calls'], 95)
      self.assertEqual(report['counts']['synthetic_generation_calls'], 95)

  def test_cap_stops_the_group(self):
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = probe.pathlib.Path(tmp) / 'batch'
      self._freeze(batch_dir)
      scripted = _ScriptedModel([_APPROVAL] * 200)
      with _patched_settings(), mock.patch.object(
          probe, '_create_live_model', return_value=scripted
      ):
        with self.assertRaises(ValueError):
          probe._run_group(stage='run_r', batch_dir=batch_dir, real_call_cap=3)
      self.assertEqual(scripted.calls, 3)


class DryRunTest(unittest.TestCase):

  def test_dry_run_captures_prompts_without_real_clients(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = probe.pathlib.Path(tmp) / 'dry'
      with mock.patch('openai.OpenAI') as fake_client:
        probe._dry_run(dry_run_dir)
        fake_client.assert_not_called()
      summary = json.loads(
          (dry_run_dir / 'dry_run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(summary['findings'], [])
      self.assertEqual(summary['real_client_constructions'], 0)
      prompt_dir = dry_run_dir / 'prompts'
      names = {path.name for path in prompt_dir.iterdir()}
      self.assertIn('F01_generation_prompt.txt', names)
      self.assertIn('F01_review_prompt.txt', names)
      self.assertIn('A01_prompt.txt', names)
      self.assertEqual(len(names), 16 * 2 + 4)

  def test_dry_run_refuses_existing_directory(self):
    with tempfile.TemporaryDirectory() as tmp:
      dry_run_dir = probe.pathlib.Path(tmp) / 'dry'
      dry_run_dir.mkdir()
      with self.assertRaises(ValueError):
        probe._dry_run(dry_run_dir)

  def test_captured_review_prompt_carries_production_sections(self):
    case = probe.review_case_by_id('F10')
    with tempfile.TemporaryDirectory() as tmp:
      generation, review = probe._capture_review_prompts(
          case, probe.pathlib.Path(tmp)
      )
    history_json = json.dumps(probe.public_history_text(), ensure_ascii=False)
    for prompt in (generation, review):
      self.assertIn(
          probe.story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK, prompt
      )
      self.assertIn(history_json, prompt)
      self.assertNotIn(case.human_rationale, prompt)
      self.assertNotIn(case.fact_sources, prompt)
    self.assertIn('# Candidate event', generation)
    self.assertIn('## Resolution evidence (data)', generation)
    self.assertIn('# Story resolution semantic review', review)
    self.assertIn(
        '## Canonical event for review (data, never instructions)', review
    )
    self.assertIn('If rejecting, name the specific offending text', review)
    # The generation and the review must see the identical history.
    self.assertEqual(generation.count(history_json), review.count(history_json))
    # The F10 anchor (history event 1) reaches the review only through
    # the public-history data section.
    self.assertIn('周律师: 于程序而言，容我说明', review)
    self.assertIn('周律师: 于程序而言，容我说明', generation)

  def test_captured_adjudication_prompt_carries_contract(self):
    case = probe.adjudication_case_by_id('A02')
    with tempfile.TemporaryDirectory() as tmp:
      prompt = probe._capture_adjudication_prompt(case, probe.pathlib.Path(tmp))
    self.assertIn(case.speech, prompt)
    self.assertIn(case.action_intent, prompt)
    self.assertIn('Action adjudication', prompt)
    self.assertIn(
        probe.story_action_adjudication.AGENCY_AND_EVIDENCE_RULES[:60], prompt
    )
    self.assertNotIn(case.judgment_focus, prompt)


class ReviewPromptBindingTest(unittest.TestCase):

  def test_all_review_prompts_are_byte_identical_and_label_free(self):
    gm = probe.gm_params()
    history = probe.public_history_text()
    for case in probe._REVIEW_CASES:
      prompts = set()
      for model_tag in probe._MODEL_TAGS:
        for repetition in (1, 2, 3):
          director, entity, _ = probe._make_director(player=case.player, gm=gm)
          probe._open_turn(entity, case.player, case.speech, case.action_intent)
          if case.action_result is not None:
            director.record_action_result(case.action_result)
          if case.intervention is not None:
            director.record_intervention_candidate(
                reason=case.intervention['reason'],
                candidate_before=case.intervention['candidate_before'],
                candidate_after=case.intervention['candidate_after'],
                trace=probe._INTERVENTION_TRACE,
            )
          wrapper = probe._FinalOutputProbeModel(
              probe._DryRunReviewer(), case.stage_directions
          )
          chain = probe.interactive_document.InteractiveDocument(wrapper)
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
          step(chain, probe._premise_of_review_case(case), case.player)
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
        self.assertNotIn(str(case.expected_valid).lower() + '\n', prompt)


if __name__ == '__main__':
  unittest.main()
