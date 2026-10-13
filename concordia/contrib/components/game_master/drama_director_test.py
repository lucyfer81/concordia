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

"""Tests using the actual entity component lifecycle."""
import dataclasses
import re
import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.language_models import budget_wrapper
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE)
# NOTE: TERMINATE is a choice-type output, so options are mandatory
# (cf. scene_tracker_dynamic_test.py); the brief omitted them.
_TERMINATE_SPEC = entity_lib.ActionSpec(
    call_to_action='terminate', output_type=entity_lib.OutputType.TERMINATE,
    options=('Yes', 'No'))

BEATS = (
    drama_director.Beat(
        id='b1', description='setup', tension_range=(1, 3),
        done_question='Is the evidence revealed?', min_turns=1, max_turns=5),
    drama_director.Beat(
        id='b2', description='resolution', tension_range=(1, 3),
        done_question='Is the fallout addressed?', min_turns=1, max_turns=5),
)


class _BeatModel(mock_model.MockModel):
  """Selects Yes or No by semantic option text, despite shuffled letters."""

  def __init__(self, completed):
    super().__init__('unused')
    self.completed = completed
    self.choice_calls = 0

  def sample_choice(self, prompt, responses, *, seed=None):
    self.choice_calls += 1
    choices = dict(re.findall(
        r'^  \(([^)]+)\) (Yes|No)$', prompt, re.MULTILINE))
    wanted = 'Yes' if self.completed else 'No'
    label = next(key for key, value in choices.items() if value == wanted)
    return responses.index(label), label, {}


class _ApiErrorModel(mock_model.MockModel):
  """Fails completion queries with a generic API error."""

  def sample_choice(self, prompt, responses, *, seed=None):
    del prompt, responses, seed
    raise RuntimeError('API unavailable')


class _BudgetExhaustedModel(mock_model.MockModel):
  """Fails completion queries by exhausting the attempt budget."""

  def sample_choice(self, prompt, responses, *, seed=None):
    del prompt, responses, seed
    raise budget_wrapper.BudgetExhaustedError('budget spent')


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _ResolutionAct(entity_component.ActingComponent):
  """Replaces only resolution generation; lifecycle remains real."""

  def __init__(self, director):
    self._director = director

  def get_action_attempt(self, contexts, action_spec):
    if action_spec.output_type != entity_lib.OutputType.RESOLVE:
      return 'No'
    speech = self._director.get_current_original_speech()
    event = f'Alice: {speech}'
    self._director.set_validated_resolution(event, '')
    return f'Event: {event}\n'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_director(model, beats=BEATS):
  director = drama_director.DramaDirector(model=model, beats=beats)
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules', act_component=_ResolutionAct(director),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
      })
  return director, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


class LifecycleTest(unittest.TestCase):

  def test_premise_event_not_counted(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    gm.observe('[event] the initial premise')
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_management_queries_do_not_evaluate(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    for _ in range(3):
      gm.act(_TERMINATE_SPEC)
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_full_turn_commits_once(self):
    model = _BeatModel(True)
    director, gm = _make_director(model)
    gm.observe('[putative_event] Alice: I hid the letter.')
    # observe 已完成 post_observe/update；原始对白仍须保留。
    self.assertEqual(
        director.get_current_original_speech(), 'I hid the letter.')
    self.assertEqual(model.choice_calls, 0)
    result = gm.act(_RESOLVE_SPEC)
    # act 已完成 update；等待匹配事件的状态仍须保留。
    self.assertIsNone(director.get_last_story_turn())
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertEqual(turn['actor'], 'Alice')
    self.assertEqual(turn['speech'], 'I hid the letter.')
    self.assertEqual(turn['beat_id_at_action'], 'b1')
    self.assertEqual(turn['beat_id_after_event'], 'b2')
    gm.observe(f'[event] {result}')
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)
    self.assertEqual(model.choice_calls, 1)

  def test_min_turns_blocks_advancement(self):
    model = _BeatModel(True)
    director, gm = _make_director(
        model, beats=(dataclasses.replace(BEATS[0], min_turns=2), BEATS[1]))
    _complete_turn(gm, 'one')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b1')
    self.assertEqual(model.choice_calls, 0)
    _complete_turn(gm, 'two')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b2')

  def test_max_turns_marks_timeout_without_advancing(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    director, gm = _make_director(_BeatModel(False), beats=(beat,))
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    self.assertFalse(director.is_beats_done())
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)

  def test_state_roundtrip(self):
    director, gm = _make_director(_BeatModel(False))
    _complete_turn(gm, 'hi')
    state = director.get_state()
    fresh, _ = _make_director(_BeatModel(False))
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)

  def test_api_error_propagates_from_completion_query(self):
    director, gm = _make_director(_ApiErrorModel())
    gm.observe('[putative_event] Alice: I show the letter.')
    result = gm.act(_RESOLVE_SPEC)
    with self.assertRaises(RuntimeError):
      gm.observe(f'[event] {result}')
    self.assertIsNone(director.get_last_story_turn())

  def test_budget_exhaustion_keeps_timeout_and_budget_flag(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    director, gm = _make_director(_BudgetExhaustedModel(), beats=(beat,))
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    self.assertTrue(director.get_state()['budget_stopped'])
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertTrue(turn['budget_stopped'])


if __name__ == '__main__':
  unittest.main()
