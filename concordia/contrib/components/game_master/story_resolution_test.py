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

"""Tests for the final dual-track output step."""

import json
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.testing import mock_model


class FinalOutputStepTest(unittest.TestCase):

  def _run(self, response: str, speech: str | None):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    director.get_committed_events.return_value = ()
    model = mock_model.MockModel(response)
    model.sample_text = mock.Mock(
        side_effect=[response, '{"valid": true, "reason": "No violation."}']
    )
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(
        director, allowed_environment_events=('Thunder rattles the windows.',)
    )
    result = step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    return result, director

  def test_returns_string_and_stores_dual_track(self):
    payload = json.dumps({
        'resolved_event': (
            'Alice: "I hid the letter under the stairs." '
            'Thunder rattles the windows.'
        ),
        'stage_directions': 'Thunder rattles the windows.',
    })
    result, director = self._run(
        payload, speech='I hid the letter under the stairs.'
    )
    self.assertEqual(
        result,
        'Alice: I hid the letter under the stairs.\n'
        'Thunder rattles the windows.',
    )
    director.set_validated_resolution.assert_called_once_with(
        'Alice: I hid the letter under the stairs.\nThunder rattles the'
        ' windows.',
        'Thunder rattles the windows.',
    )

  def test_invalid_json_raises_story_output_error(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run('not json at all', speech=None)

  def test_whitespace_only_rendering_difference_is_not_a_rewrite(self):
    # Models may flatten paragraph breaks ('\n\n') inside the embedded
    # speech into plain spaces. That is a whitespace rendering difference,
    # not a rewrite: the recorded event is assembled from the true
    # original speech anyway, so only word-level changes must be rejected.
    speech = 'First paragraph.\n\nSecond paragraph.'
    payload = json.dumps({
        'resolved_event': (
            'Alice: First paragraph.  Second paragraph. '
            'Thunder rattles the windows.'
        ),
        'stage_directions': 'Thunder rattles the windows.',
    })
    result, director = self._run(payload, speech=speech)
    self.assertIn('Thunder rattles the windows.', result)
    director.set_validated_resolution.assert_called_once()

  def test_fenced_json_parses(self):
    # Models sporadically wrap the requested JSON object in markdown code
    # fences despite an "output only the JSON object" instruction.
    payload = json.dumps({
        'resolved_event': (
            'Alice: I hid the letter under the stairs.\n'
            'Thunder rattles the windows.'
        ),
        'stage_directions': 'Thunder rattles the windows.',
    })
    for fenced in (
        f'```json\n{payload}\n```',
        f'```\n{payload}\n```',
        f'```json {payload}```',
    ):
      with self.subTest(fenced=fenced):
        result, director = self._run(
            fenced, speech='I hid the letter under the stairs.'
        )
        self.assertEqual(
            result,
            'Alice: I hid the letter under the stairs.\n'
            'Thunder rattles the windows.',
        )
        director.set_validated_resolution.assert_called_once_with(
            'Alice: I hid the letter under the stairs.\nThunder rattles the'
            ' windows.',
            'Thunder rattles the windows.',
        )

  def test_missing_fields_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(json.dumps({'resolved_event': 'x'}), speech=None)

  def test_rewritten_dialogue_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice claims she burned the letter.',
              'stage_directions': '',
          }),
          speech='I hid the letter under the stairs.',
      )

  def test_non_string_field_values_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 123,
              'stage_directions': 'Thunder rattles the windows.',
          }),
          speech=None,
      )
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice slams the door.',
              'stage_directions': ['Thunder rattles the windows.'],
          }),
          speech=None,
      )

  def test_extra_fields_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps(
              {'resolved_event': 'x', 'stage_directions': 'y', 'tension': 5}
          ),
          speech=None,
      )

  def test_paraphrased_dialogue_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': (
                  'Alice: "I had hidden the letter beneath the staircase."'
              ),
              'stage_directions': '',
          }),
          speech='I hid the letter under the stairs.',
      )

  def test_missing_speaker_name_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': '"I hid the letter under the stairs."',
              'stage_directions': '',
          }),
          speech='I hid the letter under the stairs.',
      )

  def test_invalid_output_is_not_stored_in_director(self):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = (
        'I hid the letter under the stairs.'
    )
    model = mock_model.MockModel(
        json.dumps({
            'resolved_event': 'Alice claims she burned the letter.',
            'stage_directions': '',
        })
    )
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(director)
    with self.assertRaises(story_resolution.StoryOutputError):
      step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    director.set_validated_resolution.assert_not_called()

  def test_speechless_turn_returns_only_approved_stage_directions(self):
    result, director = self._run(
        json.dumps({
            'resolved_event': 'The door slams shut.',
            'stage_directions': 'The door slams shut.',
        }),
        speech=None,
    )
    self.assertEqual(result, 'The door slams shut.')
    director.set_validated_resolution.assert_called_once_with(
        'The door slams shut.', 'The door slams shut.'
    )


class SemanticReviewTest(unittest.TestCase):
  """Tests review plumbing with scripted verdicts, not model accuracy."""

  def _setup(self, *, speech='Please check the letter.', review=None):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    director.get_committed_events.return_value = (
        'Bob: The envelope is still sealed.',
    )
    payload = json.dumps({
        'resolved_event': f'Alice: {speech}',
        'stage_directions': '',
    })
    model = mock_model.MockModel()
    model.sample_text = mock.Mock(
        side_effect=[
            payload,
            review
            if review is not None
            else '{"valid": true, "reason": "Only attributed speech occurs."}',
        ]
    )
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(
        director,
        player_names=('Alice', 'Bob'),
        character_constraints={'Bob': 'Bob never confesses to forgery.'},
        world_constraints=('GM-only: Bob forged the letter.',),
        immutable_facts=('The original letter was destroyed.',),
        allowed_environment_events=('Thunder rattles the windows.',),
        public_facts=('The rehearsal room contains a sealed envelope.',),
    )
    return director, model, chain, step

  def test_review_is_independent_and_contains_all_constraints_and_history(self):
    director, model, chain, step = self._setup()
    chain.statement('UNTRUSTED GENERATION TRACE')
    step(chain, 'UNTRUSTED CANDIDATE', 'Alice')
    self.assertEqual(model.sample_text.call_count, 2)
    generation, review = [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]
    for prompt in (generation, review):
      self.assertIn('Bob never confesses to forgery.', prompt)
      self.assertIn('GM-only: Bob forged the letter.', prompt)
      self.assertIn('The original letter was destroyed.', prompt)
      self.assertIn('Thunder rattles the windows.', prompt)
      self.assertIn('The rehearsal room contains a sealed envelope.', prompt)
      self.assertIn('"Alice"', prompt)
      self.assertIn('"Bob"', prompt)
    self.assertIn('# Story resolution semantic review', review)
    self.assertIn('Bob: The envelope is still sealed.', review)
    self.assertNotIn('UNTRUSTED GENERATION TRACE', review)
    self.assertNotIn('UNTRUSTED CANDIDATE', review)
    self.assertIn('proposal', review)
    self.assertIn('unverified accusations', review)
    director.set_validated_resolution.assert_called_once()

  def test_scripted_rejection_of_live_failure_patterns_does_not_commit(self):
    # Reduced fixtures from Luna T7 and Gemini T18 respectively. The verdict
    # is deliberately scripted: this proves rejection handling, not that a
    # particular model reliably identifies these violations.
    fixtures = (
        (
            '陈默',
            '查清之前，谁也别动老宅，也别急着下结论。',
            '陈母承认焚毁新遗嘱。周律师披露私藏录音。众人同意林雪的权益。',
            '陈母低头认罪，众人同意林雪的权益。',
            'Invented confession, private disclosure and joint agreement.',
        ),
        (
            '周律师',
            '请各位依序在现场执行笔录上签字确认。',
            '',
            '各方在封口及现场执行笔录上捺印签字。',
            'A request became executed signatures in the narrator track only.',
        ),
    )
    for actor, speech, added, stage, reason in fixtures:
      with self.subTest(actor=actor):
        director, model, chain, step = self._setup(speech=speech)
        model.sample_text.side_effect = [
            json.dumps({
                'resolved_event': f'{actor}: {speech}{added}',
                'stage_directions': stage,
            }),
            json.dumps({'valid': False, 'reason': reason}),
        ]
        with self.assertRaisesRegex(story_resolution.StoryOutputError, reason):
          step(chain, added, actor)
        self.assertEqual(model.sample_text.call_count, 2)
        director.set_validated_resolution.assert_not_called()
        director.commit_directive.assert_not_called()

  def test_invalid_review_is_rejected_without_regeneration(self):
    for verdict in (
        'not json',
        '[]',
        '{}',
        '{"valid": 1, "reason": "ok"}',
        '{"valid": "true", "reason": "ok"}',
        '{"valid": true, "reason": null}',
        '{"valid": true, "reason": "ok", "extra": false}',
        '{"valid": false, "valid": true, "reason": "ok"}',
    ):
      with self.subTest(verdict=verdict):
        director, model, chain, step = self._setup(review=verdict)
        with self.assertRaises(story_resolution.StoryOutputError):
          step(chain, 'candidate', 'Alice')
        self.assertEqual(model.sample_text.call_count, 2)
        director.set_validated_resolution.assert_not_called()

  def test_approved_self_disclosure_proposal_and_accusation_stay_speech(self):
    for speech in (
        'I hid the letter under the stairs.',
        'Please check the letter before deciding.',
        'Bob, I think you forged this letter.',
        '  I hid it.\n\n\nDo you hear me?  ',
    ):
      with self.subTest(speech=speech):
        director, _, chain, step = self._setup(speech=speech)
        result = step(chain, 'candidate', 'Alice')
        self.assertEqual(result, f'Alice: {speech}')
        director.set_validated_resolution.assert_called_once_with(result, '')

  def test_review_api_error_propagates_without_commit(self):
    director, model, chain, step = self._setup()
    first = model.sample_text.side_effect
    model.sample_text.side_effect = [
        next(first),
        RuntimeError('API unavailable'),
    ]
    with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
      step(chain, 'candidate', 'Alice')
    self.assertEqual(model.sample_text.call_count, 2)
    director.set_validated_resolution.assert_not_called()

  def test_review_uses_same_budget_and_exhaustion_prevents_commit(self):
    director, model, _, step = self._setup()
    guarded = budget_wrapper.BudgetGuardLanguageModel(
        model, total_attempt_limit=1
    )
    chain = interactive_document.InteractiveDocument(guarded)
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      step(chain, 'candidate', 'Alice')
    self.assertEqual(guarded.used_attempts, 1)
    self.assertEqual(model.sample_text.call_count, 1)
    director.set_validated_resolution.assert_not_called()


if __name__ == '__main__':
  unittest.main()
