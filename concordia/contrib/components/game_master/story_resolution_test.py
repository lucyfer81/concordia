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
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.testing import mock_model

_EMPTY_EVIDENCE = {'action_result': None, 'intervention': None}
_ACTION_INTENT = 'force the lock of the strongbox.'
_ACTION_RESULT = (
    'Alice tries to force the lock of the strongbox. Because of that,'
    ' the lock holds.'
)
_INTERVENTION = {
    'reason': 'tension',
    'candidate_before': 'Alice: I hid the letter under the stairs.',
    'candidate_after': (
        'Alice: I hid the letter under the stairs. A storm cuts the'
        ' power to the house.'
    ),
}
_APPROVAL = '{"valid": true, "reason": "No violation."}'
_EVIDENCE_MARK = '## Resolution evidence (data)'


def _read_evidence_section(prompt: str) -> dict[str, Any]:
  """Decodes the marked JSON evidence data section of a prompt.

  Mirrors the decoder of the example stub (contrib must not import
  examples): locate the marked heading, then raw-decode the JSON object
  starting at the first brace after it, so multi-paragraph values
  inside the JSON survive verbatim.

  Args:
    prompt: a prompt carrying the marked evidence data section.

  Returns:
    The decoded evidence object.
  """
  mark_start = prompt.index(_EVIDENCE_MARK) + len(_EVIDENCE_MARK)
  payload, _ = json.JSONDecoder().raw_decode(
      prompt[prompt.index('{', mark_start) :]
  )
  return payload


class ActionResolutionTest(unittest.TestCase):
  """Dual-track generation, structural validation and assembly."""

  def _make(
      self,
      *,
      speech: str | None,
      responses: list[str],
      action_intent: str | None = None,
      evidence: dict[str, Any] | None = None,
  ) -> tuple[mock.MagicMock, Any, Any, Any]:
    """Builds the director, model, chain and step for one resolution.

    Every new director evidence accessor is configured explicitly, so
    no MagicMock implicit truthiness can fabricate evidence data. The
    model element is typed Any: its `sample_text` method is replaced by
    a recording `mock.Mock`, and the tests assert on that recorder.

    Args:
      speech: the turn's original speech held by the director.
      responses: the model's answers, in call order (generation first,
        then the semantic review).
      action_intent: the turn's decoded action intent, if any.
      evidence: the turn's resolution evidence, if any.

    Returns:
      A (director, model, chain, step) tuple.
    """
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    director.get_current_action_intent.return_value = action_intent
    director.get_current_resolution_evidence.return_value = (
        evidence if evidence is not None else _EMPTY_EVIDENCE
    )
    director.get_committed_events.return_value = ()
    model = mock_model.MockModel(responses[0])
    model.sample_text = mock.Mock(side_effect=responses)
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(
        director, allowed_environment_events=('Thunder rattles the windows.',)
    )
    return director, model, chain, step

  def _run(
      self,
      response: str,
      speech: str | None,
      *,
      action_intent: str | None = None,
      evidence: dict[str, Any] | None = None,
      review: str | None = None,
  ):
    """Runs one full resolution and returns (result, director, model)."""
    director, model, chain, step = self._make(
        speech=speech,
        responses=[response, review if review is not None else _APPROVAL],
        action_intent=action_intent,
        evidence=evidence,
    )
    result = step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    return result, director, model

  def test_returns_string_and_stores_dual_track(self):
    payload = json.dumps({
        'resolved_event': (
            'Alice: "I hid the letter under the stairs." '
            'Thunder rattles the windows.'
        ),
        'stage_directions': 'Thunder rattles the windows.',
    })
    result, director, _ = self._run(
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
    result, director, _ = self._run(payload, speech=speech)
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
        result, director, _ = self._run(
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
    director.get_current_action_intent.return_value = None
    director.get_current_resolution_evidence.return_value = _EMPTY_EVIDENCE
    director.get_committed_events.return_value = ()
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
    result, director, _ = self._run(
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

  def test_build_prompt_initializes_local_speech(self):
    # The helper must stand on its own local `speech` variable: calling
    # it directly (with empty and multi-paragraph dialogue) must not
    # raise NameError by referencing the outer step's variables, and the
    # JSON data section must carry the complete values verbatim.
    multi = 'First paragraph.\n\n## not a heading\nThird "paragraph".'
    for speech, expected in ((None, ''), ('', ''), (multi, multi)):
      with self.subTest(speech=speech):
        prompt = story_resolution._build_prompt(  # pylint: disable=protected-access
            candidate_event='Alice: I hid the letter under the stairs.',
            active_player_name='Alice',
            original_speech=speech,
            constraints='{}',
            public_facts=('A sealed envelope is on the table.',),
            action_intent=_ACTION_INTENT,
            evidence={
                'action_result': _ACTION_RESULT,
                'intervention': _INTERVENTION,
            },
            committed_events=('Bob: The envelope is still sealed.',),
        )
        data = _read_evidence_section(prompt)
        self.assertEqual(data['speech'], expected)
        self.assertEqual(data['action_intent'], _ACTION_INTENT)
        self.assertEqual(data['action_result'], _ACTION_RESULT)
        self.assertEqual(data['intervention'], _INTERVENTION)
        # JSON serialization keeps the data inert: newlines and '##'
        # inside values cannot become prompt section headings, and the
        # empty-speech display placeholder stays out of the data.
        if expected:
          self.assertIn('## not a heading', data['speech'])
        else:
          self.assertIn('(none)', prompt)
          self.assertNotIn('(none)', data['speech'])
        self.assertIn('## Committed public events', prompt)
        self.assertIn('Bob: The envelope is still sealed.', prompt)

  def test_action_evidence_reaches_generation_and_review(self):
    # Both actual prompts must carry the adjudicated result, the
    # authorization rule for the speaker's own action, and the evidence
    # through the marked JSON data section (provenance), not just the
    # intent.
    payload = json.dumps({
        'resolved_event': (
            'Alice: I hid the letter under the stairs. The lock holds.'
            ' A storm cuts the power to the house.'
        ),
        'stage_directions': (
            'The lock holds. A storm cuts the power to the house.'
        ),
    })
    result, director, model = self._run(
        payload,
        speech='I hid the letter under the stairs.',
        action_intent=_ACTION_INTENT,
        evidence={
            'action_result': _ACTION_RESULT,
            'intervention': _INTERVENTION,
        },
    )
    generation, review = [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]
    for prompt in (generation, review):
      data = _read_evidence_section(prompt)
      self.assertEqual(data['action_result'], _ACTION_RESULT)
      self.assertEqual(data['action_intent'], _ACTION_INTENT)
      self.assertEqual(data['intervention'], _INTERVENTION)
      self.assertIn("current speaker's own attempted action", prompt)
    self.assertIn('candidate_before', review)
    self.assertIn('candidate_after', review)
    # The adjudicated result reaches the canonical event and the
    # director through the approved stage-directions track.
    self.assertIn('The lock holds.', result)
    director.set_validated_resolution.assert_called_once()
    stage = director.set_validated_resolution.call_args.args[1]
    self.assertIn('The lock holds.', stage)

  def test_missing_action_result_is_rejected(self):
    # The intent is a proposal, never an outcome: without the core's
    # adjudicated result the step must fail before generation. The
    # reviewer is scripted to approve, yet is never even consulted.
    payload = json.dumps({
        'resolved_event': 'Alice: I hid the letter. The lock opens.',
        'stage_directions': 'The lock opens.',
    })
    for missing in (None, ''):
      with self.subTest(missing=missing):
        director, model, chain, step = self._make(
            speech='I hid the letter under the stairs.',
            responses=[payload, _APPROVAL],
            action_intent=_ACTION_INTENT,
            evidence={'action_result': missing, 'intervention': None},
        )
        with self.assertRaisesRegex(
            story_resolution.StoryOutputError, 'adjudicated action result'
        ):
          step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
        model.sample_text.assert_not_called()
        director.set_validated_resolution.assert_not_called()
        director.commit_directive.assert_not_called()
        # The failure is archived as a transient diagnostic only.
        director.record_resolution_diagnostics.assert_called_once()
        self.assertEqual(
            director.record_resolution_diagnostics.call_args.kwargs['stage'],
            'final_output',
        )

  def test_empty_stage_for_action_is_rejected(self):
    # Deterministic guard: with an adjudicated action result or an
    # intervention candidate to integrate, an empty or whitespace-only
    # stage-directions track is a structural failure that fires before
    # the review, so a fixed approving reviewer cannot rescue it.
    cases = (
        (
            'action result',
            {'action_result': _ACTION_RESULT},
            '',
            _ACTION_INTENT,
        ),
        (
            'action result whitespace',
            {'action_result': _ACTION_RESULT},
            '   \n  ',
            _ACTION_INTENT,
        ),
        (
            'intervention candidate',
            {'action_result': None, 'intervention': _INTERVENTION},
            '',
            None,
        ),
    )
    for label, evidence_extra, stage, intent in cases:
      with self.subTest(label=label):
        evidence = {
            'intervention': None,
            **evidence_extra,
        }
        payload = json.dumps({
            'resolved_event': (
                'Alice: I hid the letter under the stairs. The lock holds.'
            ),
            'stage_directions': stage,
        })
        director, model, chain, step = self._make(
            speech='I hid the letter under the stairs.',
            responses=[payload, _APPROVAL],
            action_intent=intent,
            evidence=evidence,
        )
        with self.assertRaisesRegex(
            story_resolution.StoryOutputError, 'stage-directions track'
        ):
          step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
        self.assertEqual(model.sample_text.call_count, 1)
        director.set_validated_resolution.assert_not_called()

  def test_review_rejects_dropped_or_reversed_action_result(self):
    # The reviewer prompt must carry both the intervention candidate
    # and the core-adjudicated result; a scripted rejection leaves
    # nothing committed. The verdict is scripted: this proves rejection
    # handling, not that a particular model detects the violation.
    drafts = (
        (
            'dropped',
            (
                'Alice: I hid the letter under the stairs. A storm cuts'
                ' the power to the house.'
            ),
            'A storm cuts the power to the house.',
        ),
        (
            'reversed',
            'Alice: I hid the letter under the stairs. The lock breaks open.',
            'The lock breaks open.',
        ),
    )
    for label, resolved, stage in drafts:
      with self.subTest(label=label):
        director, model, chain, step = self._make(
            speech='I hid the letter under the stairs.',
            responses=[
                json.dumps({
                    'resolved_event': resolved,
                    'stage_directions': stage,
                }),
                json.dumps({
                    'valid': False,
                    'reason': 'The adjudicated action result was not kept.',
                }),
            ],
            action_intent=_ACTION_INTENT,
            evidence={
                'action_result': _ACTION_RESULT,
                'intervention': _INTERVENTION,
            },
        )
        with self.assertRaisesRegex(
            story_resolution.StoryOutputError, 'adjudicated action result'
        ):
          step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
        self.assertEqual(model.sample_text.call_count, 2)
        _, review = [
            call.kwargs['prompt'] for call in model.sample_text.call_args_list
        ]
        # Both the candidate and the core result are present for the
        # reviewer to compare the draft against.
        data = _read_evidence_section(review)
        self.assertEqual(data['action_result'], _ACTION_RESULT)
        self.assertEqual(data['intervention'], _INTERVENTION)
        self.assertIn('candidate_before', review)
        self.assertIn('candidate_after', review)
        director.set_validated_resolution.assert_not_called()
        director.commit_directive.assert_not_called()

  def test_review_rejects_silently_dropped_intervention(self):
    # A draft that simply omits the recorded candidate's fact is a
    # silent drop, not an honest resolution: the scripted rejection
    # must leave directive consumption and cooldown untouched.
    director, model, chain, step = self._make(
        speech='I hid the letter under the stairs.',
        responses=[
            json.dumps({
                'resolved_event': 'Alice: I hid the letter under the stairs.',
                'stage_directions': 'The candles flicker.',
            }),
            json.dumps({
                'valid': False,
                'reason': 'The intervention candidate was silently dropped.',
            }),
        ],
        evidence={'action_result': None, 'intervention': _INTERVENTION},
    )
    with self.assertRaisesRegex(
        story_resolution.StoryOutputError, 'silently dropped'
    ):
      step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    self.assertEqual(model.sample_text.call_count, 2)
    director.set_validated_resolution.assert_not_called()
    director.commit_directive.assert_not_called()

  def test_rephrasing_does_not_count_as_intervention(self):
    # A purely verbal restatement is not a successful intervention: the
    # same boundary holds as for the dropped candidate, with no
    # directive consumption or cooldown.
    director, model, chain, step = self._make(
        speech='I hid the letter under the stairs.',
        responses=[
            json.dumps({
                'resolved_event': (
                    'Alice: I hid the letter under the stairs. The hidden'
                    ' letter stays hidden.'
                ),
                'stage_directions': 'The hidden letter stays hidden.',
            }),
            json.dumps({
                'valid': False,
                'reason': 'Rephrasing is not intervention integration.',
            }),
        ],
        evidence={'action_result': None, 'intervention': _INTERVENTION},
    )
    with self.assertRaisesRegex(
        story_resolution.StoryOutputError, 'Rephrasing'
    ):
      step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    self.assertEqual(model.sample_text.call_count, 2)
    director.set_validated_resolution.assert_not_called()
    director.commit_directive.assert_not_called()

  def test_multiline_whitespace_tolerance_preserves_original_canonical(self):
    # Whitespace rendering differences in the model's draft are
    # tolerated, but the canonical event is always assembled from the
    # true original speech, paragraph breaks included.
    speech = 'First paragraph.\n\nSecond paragraph.'
    payload = json.dumps({
        'resolved_event': (
            'Alice: First paragraph.  Second paragraph. '
            'Thunder rattles the windows.'
        ),
        'stage_directions': 'Thunder rattles the windows.',
    })
    result, director, _ = self._run(payload, speech=speech)
    self.assertEqual(
        result,
        'Alice: First paragraph.\n\nSecond paragraph.\n'
        'Thunder rattles the windows.',
    )
    director.set_validated_resolution.assert_called_once_with(
        result, 'Thunder rattles the windows.'
    )

  def test_word_change_is_rejected(self):
    # Whitespace normalization must not bleed into word-level changes:
    # a single changed word in the draft's dialogue is a rewrite.
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice: I hid a letter under the stair.',
              'stage_directions': 'Thunder rattles the windows.',
          }),
          speech='I hid the letter under the stairs.',
      )

  def test_action_only_uses_action_track(self):
    # A pure action turn (no dialogue) resolves through the narrator
    # track alone: no empty speaker line, no fabricated dialogue.
    payload = json.dumps({
        'resolved_event': 'Alice tries to force the lock. The lock holds.',
        'stage_directions': 'The lock holds.',
    })
    result, director, _ = self._run(
        payload,
        speech='',
        action_intent=_ACTION_INTENT,
        evidence={'action_result': _ACTION_RESULT, 'intervention': None},
    )
    self.assertEqual(result, 'The lock holds.')
    self.assertNotIn('Alice: ', result)
    director.set_validated_resolution.assert_called_once_with(
        'The lock holds.', 'The lock holds.'
    )

  def test_empty_canonical_in_turn_is_rejected(self):
    # A story turn in progress must resolve to a non-empty canonical
    # event even when neither speech nor narration survives the draft.
    director, model, chain, step = self._make(
        speech='',
        responses=[
            json.dumps({'resolved_event': 'Alice: ', 'stage_directions': ''}),
            _APPROVAL,
        ],
    )
    with self.assertRaisesRegex(
        story_resolution.StoryOutputError, 'canonical event is empty'
    ):
      step(chain, 'Alice: ', 'Alice')
    self.assertEqual(model.sample_text.call_count, 2)
    director.set_validated_resolution.assert_not_called()


class SemanticReviewTest(unittest.TestCase):
  """Tests review plumbing with scripted verdicts, not model accuracy."""

  def _setup(self, *, speech='Please check the letter.', review=None):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    director.get_current_action_intent.return_value = None
    director.get_current_resolution_evidence.return_value = _EMPTY_EVIDENCE
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
