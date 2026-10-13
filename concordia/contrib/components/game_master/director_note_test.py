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

"""Tests for the director-note thought step and directive lifecycle.

The directive pipeline is exercised through the real entity lifecycle
(observe/act/update) with a resolution act that runs the actual story
thought chain (director note followed by the dual-track final output).
The language model is a semantic stub routing on the last question
block, mirroring ``examples.story_enactment.testing_stubs``.
"""

import json
import re
import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import director_note
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE
)

BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=1,
        max_turns=8,
    ),
    drama_director.Beat(
        id='b2',
        description='resolution',
        tension_range=(1, 3),
        done_question='Is the fallout addressed?',
        min_turns=1,
        max_turns=8,
    ),
)

_ALLOWED_EVENTS = ('a storm cuts the power', 'a courier arrives')
_IMMUTABLE_FACTS = ('the house is old', 'the will is sealed')
_PLAYER_NAMES = ('Alice', 'Bob')
_INSTRUCTION = 'Interrupt the scene with a sudden storm.'
_COMPLICATION_LINES = (
    'A storm cuts the power to the house.',
    'A courier knocks with an unexpected letter.',
    'A stray dog bursts into the yard.',
    'Thunder startles the candles out.',
    'A neighbor shouts outside the gate.',
)
_FUSED_EVENT = 'Alice: I hid the letter. while a storm cuts the power.'

# Fingerprints of the director prompts (plan appendix B, P3 and P4),
# matched against the last question block of each prompt.
_SCORING_MARK = 'Respond with a single integer from 1 to 10'
_DIRECTIVE_MARK = 'Write one short director instruction'
_COMPLICATION_MARK = 'Suggest five plausible in-story complications'
_MERGE_MARK = 'Merge the candidate event and the complication'
_DUAL_TRACK_MARK = 'Output a JSON object with exactly two string fields'
_CURRENT_SPEAKER = re.compile(r'## Current speaker\n(.+)')
_ORIGINAL_SPEECH = re.compile(r'## Original speech \(verbatim\)\n(.*)')
_LAST_BLOCK_OPENER = re.compile(r'^(?:Question|Exercise): ', re.M)
_CHOICE_LINE = re.compile(r'^  \(([^)]+)\) (Yes|No)$', re.M)


def _last_question_block(prompt: str) -> str:
  """Returns the prompt from its last Question/Exercise block on."""
  matches = list(_LAST_BLOCK_OPENER.finditer(prompt))
  if not matches:
    return ''
  return prompt[matches[-1].start() :]


class _DirectorFlowModel(mock_model.MockModel):
  """Semantic stub for the full director pipeline.

  Routes on the last question block: the beat-completion choice is
  answered by matching option text (never a fixed letter), the scoring
  question returns a configured integer, the directive-generation
  question a configured instruction, the complication question a
  configured list of lines, and the merge question a fixed fused event.
  Every call is recorded for later assertions.

  Attributes:
    text_prompts: every prompt passed to `sample_text`, in call order.
    choice_calls: number of `sample_choice` calls.
  """

  def __init__(
      self,
      *,
      completed: bool = False,
      score: str = '7',
      instruction: str = _INSTRUCTION,
      complications: str = '\n'.join(_COMPLICATION_LINES),
      fused_event: str = _FUSED_EVENT,
      complication_error: Exception | None = None,
      invalid_dual_track_from: int | None = None,
  ):
    super().__init__('unused')
    self._completed = completed
    self._score = score
    self._instruction = instruction
    self._complications = complications
    self._fused_event = fused_event
    self._complication_error = complication_error
    self._invalid_dual_track_from = invalid_dual_track_from
    self._dual_track_calls = 0
    self.text_prompts: list[str] = []
    self.choice_calls = 0

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    block = _last_question_block(prompt)
    if _COMPLICATION_MARK in block:
      if self._complication_error is not None:
        raise self._complication_error
      return self._complications
    if _MERGE_MARK in block:
      return self._fused_event
    if _DUAL_TRACK_MARK in block:
      call_index = self._dual_track_calls
      self._dual_track_calls += 1
      if (
          self._invalid_dual_track_from is not None
          and call_index >= self._invalid_dual_track_from
      ):
        return 'not valid json'
      return self._dual_track_output(prompt)
    if _DIRECTIVE_MARK in block:
      return self._instruction
    if _SCORING_MARK in block:
      return self._score
    return 'unused'

  def sample_choice(self, prompt, responses, *, seed=None):
    del seed
    self.choice_calls += 1
    choices = dict(_CHOICE_LINE.findall(_last_question_block(prompt)))
    wanted = 'Yes' if self._completed else 'No'
    letter = next(key for key, value in choices.items() if value == wanted)
    return responses.index(letter), letter, {}

  def _dual_track_output(self, prompt: str) -> str:
    speaker = _CURRENT_SPEAKER.search(prompt)
    speech = _ORIGINAL_SPEECH.search(prompt)
    speaker_name = speaker.group(1) if speaker else ''
    speech_text = speech.group(1) if speech else ''
    if not speech_text or speech_text == '(none)':
      resolved = f'{speaker_name} stays silent.'
    else:
      resolved = f'{speaker_name}: {speech_text}'
    return json.dumps(
        {'resolved_event': resolved, 'stage_directions': ''},
        ensure_ascii=False,
    )


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _ChainResolutionAct(entity_component.ActingComponent):
  """Runs the real story thought chain on RESOLVE acts.

  The chain mirrors the prefab wiring: the director-note step followed
  by the dual-track final-output step, both operating on one fresh
  interactive document per resolution.
  """

  def __init__(self, model, director):
    self._model = model
    self._director = director
    self._steps = (
        director_note.make_director_note_step(director),
        story_resolution.make_final_output_step(director),
    )

  def get_action_attempt(self, contexts, action_spec):
    if action_spec.output_type != entity_lib.OutputType.RESOLVE:
      return 'No'
    speech = self._director.get_current_original_speech()
    document = interactive_document.InteractiveDocument(self._model)
    candidate = f'Alice: {speech}'
    for step in self._steps:
      candidate = step(document, candidate, 'Alice')
    return f'Event: {candidate}\n'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_director(model, *, feedback_enabled=True, **director_params):
  director = drama_director.DramaDirector(
      model=model,
      beats=BEATS,
      eval_interval=1,
      feedback_enabled=feedback_enabled,
      allowed_environment_events=_ALLOWED_EVENTS,
      immutable_facts=_IMMUTABLE_FACTS,
      player_names=_PLAYER_NAMES,
      **director_params,
  )
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_ChainResolutionAct(model, director),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
      },
  )
  return director, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


def _step_call(model, director, candidate='Alice: I hid the letter.'):
  document = interactive_document.InteractiveDocument(model)
  return director_note.make_director_note_step(director)(
      document, candidate, 'Alice'
  )


class NoPendingDirectiveTest(unittest.TestCase):
  """Without a pending directive the step is the identity (P4 gate)."""

  def test_returns_candidate_with_zero_model_calls(self):
    # Scoring runs (eval_interval=1) and deviates, but feedback is
    # disabled, so the tension_observe mode never creates a directive.
    model = _DirectorFlowModel()
    director, gm = _make_director(model, feedback_enabled=False)
    _complete_turn(gm, 'I hid the letter.')
    self.assertEqual(director.get_last_story_turn()['tension'], 7)
    self.assertIsNone(director.get_state()['directive'])
    text_calls = len(model.text_prompts)
    choice_calls = model.choice_calls
    self.assertEqual(_step_call(model, director), 'Alice: I hid the letter.')
    self.assertEqual(len(model.text_prompts), text_calls)
    self.assertEqual(model.choice_calls, choice_calls)


class FusionStepTest(unittest.TestCase):
  """With a pending directive the step runs the two-stage fusion (P4)."""

  def test_fuses_first_candidate_with_constrained_prompts(self):
    model = _DirectorFlowModel()
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    before = len(model.text_prompts)
    self.assertEqual(_step_call(model, director), _FUSED_EVENT)
    complication_prompt, merge_prompt = model.text_prompts[before:]
    # The complication question carries the directive, the allowed
    # means, the immutable facts and the player-agency constraints.
    self.assertIn(_INSTRUCTION, complication_prompt)
    self.assertIn('a storm cuts the power', complication_prompt)
    self.assertIn('a courier arrives', complication_prompt)
    self.assertIn('the house is old', complication_prompt)
    self.assertIn('the will is sealed', complication_prompt)
    self.assertIn('never assume any player character', complication_prompt)
    self.assertIn('Alice, Bob', complication_prompt)
    # The merge question keeps the same constraints, weaves in the
    # candidate event and uses the FIRST complication only (no
    # unsaved random choice): the chosen-complication section holds
    # exactly the first proposed line.
    self.assertIn('Alice: I hid the letter.', merge_prompt)
    chosen_section = merge_prompt.split(
        '## Chosen complication to weave in\n', 1
    )[1].split('\n\n##', 1)[0]
    self.assertEqual(chosen_section, _COMPLICATION_LINES[0])
    self.assertIn('verbatim', merge_prompt)
    self.assertIn('a storm cuts the power', merge_prompt)

  def test_step_marks_directive_integrating(self):
    model = _DirectorFlowModel()
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    self.assertEqual(_step_call(model, director), _FUSED_EVENT)
    # The fused candidate awaits the final-output validation, so the
    # directive is integrating, not yet consumed.
    self.assertEqual(director.get_state()['directive']['status'], 'integrating')

  def test_no_valid_candidate_degrades_and_marks_failed(self):
    model = _DirectorFlowModel(complications='   \n\n  \n')
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    before = len(model.text_prompts)
    self.assertEqual(_step_call(model, director), 'Alice: I hid the letter.')
    # Only the complication question was asked; no merge happened.
    self.assertEqual(len(model.text_prompts), before + 1)
    directive = director.get_state()['directive']
    self.assertEqual(directive['status'], 'failed')
    self.assertTrue(directive.get('reason'))

  def test_budget_exhausted_at_fusion_propagates_path_a(self):
    # Fusion happens during the RESOLVE act, before the resolution
    # commits, so budget exhaustion takes path A: it propagates and the
    # step must not catch it (spec 11.3-1).
    model = _DirectorFlowModel(
        complication_error=budget_wrapper.BudgetExhaustedError('spent')
    )
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    gm.observe('[putative_event] Alice: What was that noise?')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      gm.act(_RESOLVE_SPEC)
    # The directive is neither consumed nor failed, and the second turn
    # never committed.
    self.assertEqual(director.get_state()['directive']['status'], 'integrating')
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)

  def test_api_error_at_fusion_propagates(self):
    model = _DirectorFlowModel(
        complication_error=RuntimeError('API unavailable')
    )
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    gm.observe('[putative_event] Alice: What was that noise?')
    with self.assertRaises(RuntimeError):
      gm.act(_RESOLVE_SPEC)
    self.assertEqual(director.get_state()['directive']['status'], 'integrating')


class ConsumptionTest(unittest.TestCase):
  """The directive is consumed once, on validated final output only."""

  def test_consumed_after_validation_and_not_reconsumed(self):
    model = _DirectorFlowModel()
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    self.assertEqual(director.get_state()['directive']['status'], 'pending')
    _complete_turn(gm, 'What was that noise?')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'consumed')
    self.assertEqual(state['last_directive_executed_turn'], 2)
    # A repeated RESOLVE makes no directive calls and consumes nothing.
    before = len(model.text_prompts)
    gm.act(_RESOLVE_SPEC)
    new_prompts = model.text_prompts[before:]
    self.assertEqual(len(new_prompts), 1)  # only the final-output question
    self.assertNotIn(_COMPLICATION_MARK, new_prompts[0])
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'consumed')
    self.assertEqual(state['last_directive_executed_turn'], 2)

  def test_story_output_error_leaves_directive_unconsumed(self):
    # The first resolution validates; the second (which fuses the
    # directive) returns an invalid final output.
    model = _DirectorFlowModel(invalid_dual_track_from=1)
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    gm.observe('[putative_event] Alice: What was that noise?')
    with self.assertRaises(story_resolution.StoryOutputError):
      gm.act(_RESOLVE_SPEC)
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'integrating')
    self.assertIsNone(state['last_directive_executed_turn'])

  def test_consumed_directive_not_reexecuted_after_roundtrip(self):
    model = _DirectorFlowModel()
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    _complete_turn(gm, 'What was that noise?')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'consumed')
    fresh_model = _DirectorFlowModel()
    fresh, _ = _make_director(fresh_model)
    fresh.set_state(state)
    self.assertIsNone(fresh.get_pending_directive())
    text_calls = len(fresh_model.text_prompts)
    self.assertEqual(_step_call(fresh_model, fresh), 'Alice: I hid the letter.')
    self.assertEqual(len(fresh_model.text_prompts), text_calls)


class CooldownTest(unittest.TestCase):
  """Generation is blocked for cooldown turns after an execution."""

  def test_cooldown_blocks_generation_until_window_passes(self):
    model = _DirectorFlowModel()
    director, gm = _make_director(model, cooldown_turns=2)
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_state()['directive']['generated_at_turn'], 1)
    # Turn 2's resolution consumes the directive: from then on, no new
    # directive may be generated for cooldown_turns more turns.
    _complete_turn(gm, 'two')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'consumed')
    self.assertEqual(state['last_directive_executed_turn'], 2)
    for speech in ('three', 'four'):
      _complete_turn(gm, speech)
      state = director.get_state()
      self.assertEqual(state['last_directive_executed_turn'], 2)
      self.assertEqual(state['directive']['status'], 'consumed')
      self.assertEqual(state['directive']['generated_at_turn'], 1)
    # The turns inside the cooldown window still deviate (scores of 7)
    # and the story keeps running, so only the cooldown blocks.
    self.assertTrue(
        all(entry['deviated'] for entry in state['tension_history'])
    )
    # The turn after the window generates a fresh pending directive.
    _complete_turn(gm, 'five')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'pending')
    self.assertEqual(state['directive']['generated_at_turn'], 5)
    self.assertEqual(state['last_directive_executed_turn'], 2)


if __name__ == '__main__':
  unittest.main()
