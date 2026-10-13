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
from concordia.components.agent import memory as memory_component
from concordia.components.game_master import make_observation as make_observation_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.components.game_master import scene_tracker as scene_tracker_lib
from concordia.components.game_master import terminate as terminate_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_terminator
from concordia.language_model import no_language_model
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component
from concordia.typing import scene as scene_lib

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE
)
# NOTE: TERMINATE is a choice-type output, so options are mandatory
# (cf. drama_director_test.py).
_TERMINATE_SPEC = entity_lib.ActionSpec(
    call_to_action='terminate',
    output_type=entity_lib.OutputType.TERMINATE,
    options=('Yes', 'No'),
)
_FREE_SPEC = entity_lib.free_action_spec(call_to_action='act')

BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=1,
        max_turns=5,
    ),
    drama_director.Beat(
        id='b2',
        description='resolution',
        tension_range=(1, 3),
        done_question='Is the fallout addressed?',
        min_turns=1,
        max_turns=5,
    ),
)


class _BeatModel(mock_model.MockModel):
  """Selects Yes or No by semantic option text, despite shuffled letters."""

  def __init__(self, completed):
    super().__init__('unused')
    self.completed = completed
    self.choice_calls = 0

  def sample_choice(self, prompt, responses, *, seed=None):
    self.choice_calls += 1
    choices = dict(re.findall(r'^  \(([^)]+)\) (Yes|No)$', prompt, re.M))
    wanted = 'Yes' if self.completed else 'No'
    label = next(key for key, value in choices.items() if value == wanted)
    return responses.index(label), label, {}


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _PlainComponent(entity_component.ContextComponent):
  """A sibling component exposing none of the queried methods."""

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _StoryAct(entity_component.ActingComponent):
  """Model-free acting: echoes the terminator context, resolves turns."""

  def __init__(self, director, terminate_component_key):
    self._director = director
    self._terminate_component_key = terminate_component_key

  def get_action_attempt(self, contexts, action_spec):
    if action_spec.output_type == entity_lib.OutputType.TERMINATE:
      return contexts[self._terminate_component_key]
    if (
        action_spec.output_type == entity_lib.OutputType.RESOLVE
        and self._director is not None
    ):
      speech = self._director.get_current_original_speech() or 'acts'
      event = f'Alice: {speech}'
      self._director.set_validated_resolution(event, '')
      return f'Event: {event}\n'
    return 'No'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_scene(num_rounds):
  return scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='main',
          game_master_name=None,
          action_spec=entity_lib.free_action_spec(call_to_action='test'),
      ),
      participants=('Alice',),
      num_rounds=num_rounds,
      premise={'Alice': []},
  )


def _make_story_stack(*, director=None, scene_rounds=1):
  """Builds a real game master entity around the terminator under test."""
  terminator = story_terminator.SceneOrStoryTerminator()
  components: dict[str, entity_component.ContextComponent] = {
      terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY: terminator,
      memory_component.DEFAULT_MEMORY_COMPONENT_KEY: (
          memory_component.ListMemory(memory_bank=[])
      ),
      make_observation_lib.DEFAULT_MAKE_OBSERVATION_COMPONENT_KEY: (
          make_observation_lib.MakeObservation(
              model=no_language_model.NoLanguageModel(),
              player_names=('Alice',),
              allow_llm_fallback=False,
          )
      ),
      scene_tracker_lib.DEFAULT_SCENE_TRACKER_COMPONENT_KEY: (
          scene_tracker_lib.SceneTracker(
              model=no_language_model.NoLanguageModel(),
              scenes=(_make_scene(scene_rounds),),
          )
      ),
  }
  if director is not None:
    components[drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY] = director
    components[next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY] = (
        _FixedActor()
    )
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_StoryAct(
          director, terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY
      ),
      context_components=components,
  )
  return terminator, gm


def _make_bare_terminator():
  terminator = story_terminator.SceneOrStoryTerminator()
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_StoryAct(
          None, terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY
      ),
      context_components={
          terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY: terminator,
      },
  )
  return terminator, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


def _director_state(**overrides):
  state = {
      'beats': [beat.to_dict() for beat in BEATS],
      'beat_index': 0,
      'turns_in_beat': 0,
      'story_turn': 0,
      'recent_events': [],
      'beat_timeout': False,
      'budget_stopped': False,
      'last_story_turn': None,
  }
  state.update(overrides)
  return state


class ResolveFinalReasonTest(unittest.TestCase):

  def test_single_reasons(self):
    cases = (
        ('error', ('error', None, 0, 10)),
        ('budget_limit', ('budget_limit', None, 0, 10)),
        ('beats_completed', (None, 'beats_completed', 0, 10)),
        ('beat_timeout', (None, 'beat_timeout', 0, 10)),
        ('scene_limit', (None, 'scene_limit', 0, 10)),
        ('step_limit', (None, None, 10, 10)),
        ('budget_limit', (None, 'budget_limit', 0, 10)),
    )
    for expected, args in cases:
      with self.subTest(expected=expected):
        self.assertEqual(story_terminator.resolve_final_reason(*args), expected)

  def test_no_candidates_default_to_error(self):
    self.assertEqual(
        story_terminator.resolve_final_reason(None, None, 0, 10), 'error'
    )

  def test_empty_component_reason_is_not_a_candidate(self):
    self.assertEqual(
        story_terminator.resolve_final_reason(None, '', 0, 10), 'error'
    )

  def test_step_limit_boundary_uses_committed_turns(self):
    resolve = story_terminator.resolve_final_reason
    self.assertEqual(resolve(None, None, 4, 5), 'error')
    self.assertEqual(resolve(None, None, 5, 5), 'step_limit')
    self.assertEqual(resolve(None, None, 6, 5), 'step_limit')

  def test_adjacent_reasons_are_ordered_by_priority(self):
    pairs = (
        ('error', 'beats_completed'),
        ('beats_completed', 'beat_timeout'),
        ('beat_timeout', 'scene_limit'),
        ('scene_limit', 'step_limit'),
        ('step_limit', 'budget_limit'),
    )
    resolve = story_terminator.resolve_final_reason
    for higher, lower in pairs:
      with self.subTest(higher=higher, lower=lower):
        self.assertEqual(resolve(higher, lower, 0, 10), higher)
        self.assertEqual(resolve(lower, higher, 0, 10), higher)

  def test_budget_exception_loses_to_component_reasons(self):
    resolve = story_terminator.resolve_final_reason
    self.assertEqual(
        resolve('budget_limit', 'scene_limit', 0, 10), 'scene_limit'
    )
    self.assertEqual(
        resolve('budget_limit', 'beats_completed', 1, 1), 'beats_completed'
    )

  def test_step_limit_loses_to_scene_limit_but_beats_budget(self):
    resolve = story_terminator.resolve_final_reason
    self.assertEqual(resolve(None, 'scene_limit', 9, 9), 'scene_limit')
    self.assertEqual(resolve(None, 'budget_limit', 9, 9), 'step_limit')


class TerminatorPreActTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.terminator, self.gm = _make_bare_terminator()

  def test_answers_no_before_termination(self):
    self.assertEqual(self.terminator.pre_act(_TERMINATE_SPEC), 'No')

  def test_answers_yes_after_terminate(self):
    self.terminator.terminate()
    self.assertEqual(self.terminator.pre_act(_TERMINATE_SPEC), 'Yes')

  def test_non_terminate_specs_answer_no(self):
    self.terminator.terminate()
    self.assertEqual(self.terminator.pre_act(_FREE_SPEC), 'No')
    self.assertEqual(self.terminator.pre_act(_RESOLVE_SPEC), 'No')

  def test_terminate_flag_state_round_trip(self):
    self.assertEqual(self.terminator.get_state(), {'terminate_now': False})
    self.terminator.terminate()
    state = self.terminator.get_state()
    self.assertEqual(state, {'terminate_now': True})
    fresh, _ = _make_bare_terminator()
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)
    self.assertEqual(fresh.pre_act(_TERMINATE_SPEC), 'Yes')
    fresh.set_state({'terminate_now': False})
    self.assertEqual(fresh.pre_act(_TERMINATE_SPEC), 'No')

  def test_siblings_without_methods_are_tolerated(self):
    terminator = story_terminator.SceneOrStoryTerminator()
    entity_agent_with_logging.EntityAgentWithLogging(
        agent_name='story rules',
        act_component=_StoryAct(
            None, terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY
        ),
        context_components={
            terminate_lib.DEFAULT_TERMINATE_COMPONENT_KEY: terminator,
            scene_tracker_lib.DEFAULT_SCENE_TRACKER_COMPONENT_KEY: (
                _PlainComponent()
            ),
            drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY: (
                _PlainComponent()
            ),
        },
    )
    self.assertEqual(terminator.pre_act(_TERMINATE_SPEC), 'No')
    self.assertEqual(terminator.termination_reason(), '')
    terminator.terminate()
    self.assertEqual(terminator.pre_act(_TERMINATE_SPEC), 'Yes')


class TerminationReasonTest(unittest.TestCase):
  """Reasons from a real SceneTracker and a state-driven DramaDirector."""

  def _make_stack(self):
    director = drama_director.DramaDirector(
        model=mock_model.MockModel('unused'), beats=BEATS
    )
    terminator, gm = _make_story_stack(director=director)
    return director, terminator, gm

  def test_beats_completed_wins_over_scene_limit(self):
    director, terminator, gm = self._make_stack()
    gm.act(_RESOLVE_SPEC)
    director.set_state(_director_state(beat_index=len(BEATS)))
    self.assertEqual(terminator.termination_reason(), 'beats_completed')

  def test_beat_timeout(self):
    director, terminator, _ = self._make_stack()
    director.set_state(_director_state(beat_timeout=True))
    self.assertEqual(terminator.termination_reason(), 'beat_timeout')

  def test_scene_limit(self):
    director, terminator, gm = self._make_stack()
    gm.act(_RESOLVE_SPEC)
    self.assertEqual(terminator.termination_reason(), 'scene_limit')

  def test_budget_limit(self):
    director, terminator, _ = self._make_stack()
    director.set_state(_director_state(budget_stopped=True))
    self.assertEqual(terminator.termination_reason(), 'budget_limit')

  def test_scene_limit_wins_over_budget_limit(self):
    director, terminator, gm = self._make_stack()
    director.set_state(_director_state(budget_stopped=True))
    gm.act(_RESOLVE_SPEC)
    self.assertEqual(terminator.termination_reason(), 'scene_limit')

  def test_no_reason_while_running(self):
    director, terminator, _ = self._make_stack()
    director.set_state(_director_state())
    self.assertEqual(terminator.termination_reason(), '')


class StoryRunIntegrationTest(unittest.TestCase):

  def test_scene_budget_exhaustion_records_scene_limit(self):
    model = _BeatModel(False)
    beats = (
        dataclasses.replace(BEATS[0], min_turns=5, max_turns=10),
        BEATS[1],
    )
    director = drama_director.DramaDirector(model=model, beats=beats)
    terminator, gm = _make_story_stack(director=director, scene_rounds=2)
    _complete_turn(gm, 'one')
    self.assertEqual(gm.act(_TERMINATE_SPEC), 'No')
    _complete_turn(gm, 'two')
    # The scene schedule is exhausted: the real SceneTracker flags the
    # terminator and the terminator answers Yes on its own conditions.
    self.assertEqual(gm.act(_TERMINATE_SPEC), 'Yes')
    self.assertTrue(terminator.get_state()['terminate_now'])
    tracker = gm.get_component(
        scene_tracker_lib.DEFAULT_SCENE_TRACKER_COMPONENT_KEY,
        type_=scene_tracker_lib.SceneTracker,
    )
    self.assertTrue(tracker.is_done())
    self.assertFalse(director.is_beats_done())
    self.assertEqual(model.choice_calls, 0)
    self.assertEqual(terminator.termination_reason(), 'scene_limit')
    self.assertEqual(
        story_terminator.resolve_final_reason(
            None, terminator.termination_reason(), 2, 10
        ),
        'scene_limit',
    )

  def test_last_beat_completing_with_scene_limit_records_beats_completed(self):
    model = _BeatModel(True)
    beats = (
        drama_director.Beat(
            id='only',
            description='climax',
            tension_range=(1, 3),
            done_question='Is the conflict resolved?',
            min_turns=1,
            max_turns=5,
        ),
    )
    director = drama_director.DramaDirector(model=model, beats=beats)
    terminator, gm = _make_story_stack(director=director, scene_rounds=1)
    _complete_turn(gm, 'I end the feud.')
    self.assertTrue(director.is_beats_done())
    self.assertEqual(gm.act(_TERMINATE_SPEC), 'Yes')
    self.assertTrue(terminator.get_state()['terminate_now'])
    reason = terminator.termination_reason()
    self.assertEqual(reason, 'beats_completed')
    turn = director.get_last_story_turn()
    self.assertIsNotNone(turn)
    # The story-turn limit is reached simultaneously; completing the story
    # still outranks both the scene and the global step limit.
    self.assertEqual(turn['turn_id'], 1)
    self.assertEqual(
        story_terminator.resolve_final_reason(None, reason, turn['turn_id'], 1),
        'beats_completed',
    )


if __name__ == '__main__':
  unittest.main()
