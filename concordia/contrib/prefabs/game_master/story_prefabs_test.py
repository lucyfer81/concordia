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

"""Tests for the story-enactment game master and character prefabs."""

from typing import Any, cast

from absl.testing import absltest
from concordia.agents import entity_agent_with_logging
from concordia.associative_memory import basic_associative_memory as associative_memory
from concordia.components import agent as agent_components
from concordia.components import game_master as gm_components
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import drama_director as drama_director_lib
from concordia.contrib.components.game_master import story_terminator as story_terminator_lib
from concordia.contrib.prefabs.entity import story_character
from concordia.contrib.prefabs.game_master import story_dramaturgic
from concordia.environment.engines import sequential
from concordia.testing import mock_model
import numpy as np


def _embedder(text: str):
  del text
  return np.ones(16, dtype=np.float32)


_PLAYERS = ('Alice', 'Bob')
_PREMISE = 'The rehearsal is about to begin.'
_WORLD_CONSTRAINTS = ('The story must stay in the rehearsal room.',)
_IMMUTABLE_FACTS = ('The script has no final page.',)

_SCENE_DICTS = (
    {
        'scene_type': {
            'name': 'main scene',
            'game_master_name': None,
            'action_spec': {
                'call_to_action': 'What does {name} say next?',
                'output_type': 'free',
                'tag': 'speech',
            },
            'possible_participants': None,
        },
        'participants': list(_PLAYERS),
        'num_rounds': 1,
        'premise': {name: [_PREMISE] for name in _PLAYERS},
    },
)

# A single beat whose minimum turn count keeps the beat-completion
# question out of the one-step run below.
_BEAT_DICTS = (
    {
        'id': 'b1',
        'description': 'The rehearsal begins',
        'tension_range': [1, 3],
        'done_question': 'Has the rehearsal started?',
        'min_turns': 5,
        'max_turns': 10,
    },
)

# Valid dual-track JSON for the final resolution step: it preserves the
# mock player's speech verbatim (the mock model's default response).
_SPEECH = 'Quick brown fox jumps over a lazy dog'
_DUAL_TRACK_RESPONSE = (
    '{"resolved_event": "Alice: ' + _SPEECH + '", "stage_directions": ""}'
)


class _PromptRecordingModel(mock_model.MockModel):
  """Mock model recording every prompt it is asked to sample."""

  def __init__(self, response: str):
    super().__init__(response)
    self.prompts: list[str] = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.prompts.append(prompt)
    return self._response


def _character_params(name: str, secret: str = '') -> dict:
  return {
      'name': name,
      'goal': f'{name} wants the rehearsal to succeed.',
      'identity': f'{name} is a stage actor.',
      'hard_constraint': f'{name} never lies.',
      'secret': secret,
      'public_facts': (f'{name} joined the troupe last year.',),
      'style': f'{name} speaks concisely.',
      'initial_relationship_notes': f'{name} knows everyone in the troupe.',
  }


def _build_character(name: str, secret: str = ''):
  return story_character.Entity(params=_character_params(name, secret)).build(
      model=mock_model.MockModel(),
      memory_bank=associative_memory.AssociativeMemoryBank(
          sentence_embedder=_embedder
      ),
  )


def _build_game_master(model, entities, **overrides):
  params = {
      'name': 'story rules',
      'scene_dicts': _SCENE_DICTS,
      'beat_dicts': _BEAT_DICTS,
      'world_constraints': _WORLD_CONSTRAINTS,
      'immutable_facts': _IMMUTABLE_FACTS,
  }
  params.update(overrides)
  prefab = story_dramaturgic.GameMaster(params=params)
  prefab.entities = entities
  return prefab.build(
      model=model,
      memory_bank=associative_memory.AssociativeMemoryBank(
          sentence_embedder=_embedder
      ),
  )


class StoryCharacterPrefabTest(absltest.TestCase):

  def test_build_returns_logging_agent_with_story_wiring(self):
    entity = _build_character('Alice', secret='Alice hid the last page.')
    self.assertIsInstance(
        entity, entity_agent_with_logging.EntityAgentWithLogging
    )
    brief = entity.get_component('CharacterBrief')
    self.assertIsInstance(brief, agent_components.constant.Constant)
    brief_text = cast('str', brief.get_state()['state'])
    self.assertIn('Alice is a stage actor.', brief_text)
    self.assertIn('Alice joined the troupe last year.', brief_text)
    self.assertIn('Alice never lies.', brief_text)
    # The secret lives in its own component on this character only.
    secret = entity.get_component('PrivateSecret')
    self.assertEqual(secret.get_state()['state'], 'Alice hid the last page.')
    # Story characters speak in their own words: no automatic prefixing.
    act_state = entity.get_act_component().get_state()
    self.assertFalse(act_state['prefix_entity_name'])

  def test_build_without_secret_has_no_secret_component(self):
    entity = _build_character('Bob')
    with self.assertRaises(KeyError):
      entity.get_component('PrivateSecret')

  def test_rebuild_creates_new_extension_components(self):
    first = _build_character('Alice', secret='kept secret')
    second = _build_character('Alice', secret='kept secret')
    self.assertIsNot(
        first.get_component('CharacterBrief'),
        second.get_component('CharacterBrief'),
    )
    self.assertIsNot(
        first.get_component('PrivateSecret'),
        second.get_component('PrivateSecret'),
    )

  def test_supports_extra_components(self):
    note = agent_components.constant.Constant(
        state='extra note', pre_act_label='\nExtra note'
    )
    params = _character_params('Alice')
    params['extra_components'] = {'user_note': note}
    entity = story_character.Entity(params=params).build(
        model=mock_model.MockModel(),
        memory_bank=associative_memory.AssociativeMemoryBank(
            sentence_embedder=_embedder
        ),
    )
    self.assertIs(entity.get_component('user_note'), note)
    self.assertIsInstance(
        entity.get_component('CharacterBrief'),
        agent_components.constant.Constant,
    )


class StoryGameMasterPrefabTest(absltest.TestCase):

  def test_build_returns_logging_agent_with_story_wiring(self):
    players = [_build_character(name) for name in _PLAYERS]
    gm = _build_game_master(mock_model.MockModel(), players)
    self.assertIsInstance(gm, entity_agent_with_logging.EntityAgentWithLogging)
    director = gm.get_component('__drama_director__')
    self.assertIsInstance(director, drama_director_lib.DramaDirector)
    beats = cast('list[dict[str, Any]]', director.get_state()['beats'])
    self.assertEqual(beats[0]['id'], 'b1')
    terminator = gm.get_component(
        gm_components.terminate.DEFAULT_TERMINATE_COMPONENT_KEY
    )
    self.assertIsInstance(
        terminator, story_terminator_lib.SceneOrStoryTerminator
    )
    constraints = gm.get_component('world_constraints')
    self.assertIsInstance(constraints, agent_components.constant.Constant)
    self.assertEqual(
        constraints.get_state()['state'], '\n'.join(_WORLD_CONSTRAINTS)
    )
    resolution = gm.get_component(
        '__resolution__', type_=event_resolution_lib.EventResolution
    )
    # The resolution chain must be conditioned on the current beat and the
    # world constraints (protected access is the only way to inspect the
    # configured dependency keys).
    self.assertEqual(
        set(resolution._components),  # pylint: disable=protected-access
        {'display_events', '__drama_director__', 'world_constraints'},
    )

  def test_supports_extra_components(self):
    players = [_build_character(name) for name in _PLAYERS]
    note = agent_components.constant.Constant(
        state='extra note', pre_act_label='\nExtra note'
    )
    gm = _build_game_master(
        mock_model.MockModel(), players, extra_components={'user_note': note}
    )
    self.assertIs(gm.get_component('user_note'), note)

  def test_rebuild_creates_new_components(self):
    players = [_build_character(name) for name in _PLAYERS]
    first = _build_game_master(mock_model.MockModel(), players)
    second = _build_game_master(mock_model.MockModel(), players)
    self.assertIsNot(
        first.get_component('__drama_director__'),
        second.get_component('__drama_director__'),
    )

  def test_real_act_resolution_prompt_contains_beat_and_constraints(self):
    players = [_build_character(name) for name in _PLAYERS]
    model = _PromptRecordingModel(_DUAL_TRACK_RESPONSE)
    gm = _build_game_master(model, players)
    environment = sequential.Sequential()
    environment.run_loop(game_masters=[gm], entities=players, max_steps=1)
    # The final dual-track output step must see the current beat and the
    # world constraints in its prompt, proving the resolution chain is
    # conditioned on the drama director and the constraints component.
    resolution_prompts = [p for p in model.prompts if 'resolved_event' in p]
    self.assertTrue(resolution_prompts)
    prompt = resolution_prompts[-1]
    self.assertIn('The rehearsal begins (beat b1)', prompt)
    self.assertIn('\n'.join(_WORLD_CONSTRAINTS), prompt)
    self.assertIn('\n'.join(_IMMUTABLE_FACTS), prompt)


if __name__ == '__main__':
  absltest.main()
