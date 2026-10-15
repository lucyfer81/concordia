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

"""Tests for the action-adjudication thought step.

The step is exercised on a fresh adjudication document after the real
entity lifecycle opened the turn: observing a putative event in
``speech_and_action`` mode decodes the JSON action contract into the
director's transient state, mirroring the fixtures of
``drama_director_test``. The language model is a real ``MockModel``
whose ``sample_text`` is wrapped in a recording spy, so every assertion
reads the prompts the core helper actually produced; a second wraps spy
on ``event_resolution.attempt_to_result`` proves the core function
itself ran, without replacing its return value.
"""

import json
import unittest
from unittest import mock

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_action_adjudication
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.testing import mock_model
from concordia.typing import entity_component

BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=1,
        max_turns=5,
    ),
)

# Fingerprint of the core question `attempt_to_result` asks.
_CORE_QUESTION = 'What happens as a result of the attempted action?'
# Fingerprint of the authorization-boundaries block of the step context.
_BOUNDARIES_MARK = 'GM-only facts and authorization boundaries'

_INTENT = 'seal the letter in the envelope and hide it under the wardrobe.'
_OUTCOME = 'The envelope seals and slides far under the wardrobe.'

_GM_BOUNDARIES = dict(
    immutable_facts=('the house is old',),
    character_constraints={'Alice': 'Never confess touching the will.'},
    world_constraints=('Bob secretly holds a recording.',),
    player_names=('Alice', 'Bob'),
)


class _BudgetExhaustedModel(mock_model.MockModel):
  """Fails every text question by exhausting the attempt budget."""

  def __init__(self):
    super().__init__('unused')
    self.text_calls = 0

  def sample_text(self, prompt, **kwargs):
    del prompt, kwargs
    self.text_calls += 1
    raise budget_wrapper.BudgetExhaustedError('budget spent at adjudication')


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _NoopAct(entity_component.ActingComponent):
  """Never acts; the tests drive the thought step directly."""

  def get_action_attempt(self, context, action_spec):
    del context, action_spec
    return 'No'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_director(model, **director_params):
  director = drama_director.DramaDirector(
      model=model, beats=BEATS, eval_interval=0, **director_params
  )
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_NoopAct(),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
      },
  )
  return director, gm


def _open_turn(gm, speech, intent):
  """Observes a speech-and-action putative event, returning its raw JSON."""
  raw = json.dumps({'speech': speech, 'action_intent': intent})
  gm.observe(f'[putative_event] Alice: {raw}')
  return raw


def _spy_text(model):
  """Wraps a MockModel's bound sample_text in a recording spy."""
  spy = mock.Mock(wraps=model.sample_text)
  model.sample_text = spy
  return spy


def _step_call(model, director, candidate):
  document = interactive_document.InteractiveDocument(model)
  return story_action_adjudication.make_action_adjudication_step(director)(
      document, candidate, 'Alice'
  )


class ActionAdjudicationStepTest(unittest.TestCase):

  def test_no_intent_makes_no_model_call_and_cleans_premise(self):
    # speech_and_action turn with an empty action_intent: the raw JSON
    # premise must be replaced by the clean attributed dialogue with
    # zero model calls. No action is inferred from the spoken claims
    # (a mentioned recording is not an established recording), so
    # nothing is recorded as an adjudicated result.
    model = mock_model.MockModel()
    spy = _spy_text(model)
    director, gm = _make_director(model, action_mode='speech_and_action')
    raw = _open_turn(gm, 'I hid the letter.', '')
    self.assertEqual(
        _step_call(model, director, raw), 'Alice: I hid the letter.'
    )
    spy.assert_not_called()
    self.assertIsNone(
        director.get_current_resolution_evidence()['action_result']
    )
    # The default speech_only mode always takes the same branch: the
    # raw speech premise becomes attributed dialogue, zero calls.
    model = mock_model.MockModel()
    spy = _spy_text(model)
    director, gm = _make_director(model)
    gm.observe('[putative_event] Alice: I hid the letter.')
    self.assertEqual(
        _step_call(model, director, 'I hid the letter.'),
        'Alice: I hid the letter.',
    )
    spy.assert_not_called()
    self.assertIsNone(
        director.get_current_resolution_evidence()['action_result']
    )

  def test_core_return_is_recorded_and_returned_unchanged(self):
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(
        model, action_mode='speech_and_action', **_GM_BOUNDARIES
    )
    raw = _open_turn(gm, 'I will tidy the study.', _INTENT)
    # Wraps spies: the real core helper and the real recording API run,
    # only their calls are observed. The recording wrapper captures the
    # core's true return value so the recorded argument can be checked
    # against it directly.
    real_core = event_resolution_lib.attempt_to_result
    core_returns: list[str] = []

    def _recording_core(*args, **kwargs):
      value = real_core(*args, **kwargs)
      core_returns.append(value)
      return value

    core = mock.Mock(wraps=_recording_core)
    record = mock.Mock(wraps=director.record_action_result)
    with mock.patch.object(event_resolution_lib, 'attempt_to_result', core):
      with mock.patch.object(director, 'record_action_result', record):
        returned = _step_call(model, director, raw)
    expected = f'{_INTENT} Because of that, {_OUTCOME}'
    core.assert_called_once()
    self.assertEqual(core_returns, [expected])
    record.assert_called_once_with(expected)
    self.assertEqual(returned, expected)
    self.assertEqual(
        director.get_current_resolution_evidence()['action_result'], expected
    )
    # Exactly one model call: the step itself adds only statements and
    # the core asks only its single question.
    spy.assert_called_once()
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_CORE_QUESTION, prompt)
    self.assertIn(
        'Take into account the location and status of each player.', prompt
    )
    self.assertIn(_BOUNDARIES_MARK, prompt)
    self.assertIn('the house is old', prompt)
    self.assertIn('Never confess touching the will.', prompt)
    self.assertIn('Bob secretly holds a recording.', prompt)
    self.assertIn('I will tidy the study.', prompt)
    self.assertIn(_INTENT, prompt)
    self.assertLess(
        prompt.index('## Adjudication rules'),
        prompt.index(f'Question: {_CORE_QUESTION}'),
    )

  def test_action_only_prompt_has_no_fabricated_dialogue(self):
    # A turn with an intent and no speech: the adjudication prompt must
    # not fabricate attributed dialogue for the silent speaker.
    model = mock_model.MockModel('The study drawers stand empty.')
    spy = _spy_text(model)
    director, gm = _make_director(model, action_mode='speech_and_action')
    raw = _open_turn(gm, '', 'search the study for the will.')
    returned = _step_call(model, director, raw)
    self.assertIn('search the study for the will.', returned)
    spy.assert_called_once()
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_CORE_QUESTION, prompt)
    self.assertIn('## Original speech (verbatim)\n(none)', prompt)
    self.assertNotIn('Alice:', prompt)
    self.assertNotIn('Bob:', prompt)

  def test_budget_exhaustion_propagates_without_result_or_commit(self):
    model = _BudgetExhaustedModel()
    director, gm = _make_director(model, action_mode='speech_and_action')
    _open_turn(gm, 'Here goes nothing.', 'flip the breaker to restore light.')
    record = mock.Mock(wraps=director.record_action_result)
    commit = mock.Mock(wraps=director.set_validated_resolution)
    with mock.patch.object(director, 'record_action_result', record):
      with mock.patch.object(director, 'set_validated_resolution', commit):
        with self.assertRaises(budget_wrapper.BudgetExhaustedError):
          _step_call(model, director, 'premise')
    # The error propagates unwrapped and unretried; no result is
    # recorded and nothing commits (budget path A).
    self.assertEqual(model.text_calls, 1)
    record.assert_not_called()
    commit.assert_not_called()
    self.assertIsNone(
        director.get_current_resolution_evidence()['action_result']
    )
    self.assertIsNone(director.get_last_story_turn())


if __name__ == '__main__':
  unittest.main()
