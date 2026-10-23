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

"""Information isolation and dialogue propagation tests for the story.

Two properties of the story enactment stack are verified here against
the real prefabs and the real act lifecycle (no READY-phase peeking at
component internals):

1. Information isolation. Social simulation credibility rests on
   differential knowledge: each agent acts only on what their character
   observes, the game master alone conditions its resolutions on the
   hidden ground truth. This mirrors epistemic perspective taking: an
   agent's prompt must reflect their character's knowledge, not the
   experimenter's omniscience.

2. Dialogue propagation. Spoken lines are the channel through which
   private knowledge becomes shared: a player's verbatim speech must
   reach the next player's observation and action prompt via the
   resolved event, updating the conversational common ground, while the
   narrator track (stage directions) must not restate it.

All model calls go through the deterministic semantic stub in
`testing_stubs`; no real API is contacted.
"""

import dataclasses
from typing import Any
import unittest

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.prefabs.entity import story_character
from concordia.contrib.prefabs.game_master import story_dramaturgic
from concordia.contrib.prefabs.game_master import story_scene
from concordia.prefabs.simulation import generic as simulation_lib
from concordia.typing import prefab as prefab_lib
from concordia.typing import scene as scene_lib
import numpy as np

from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs

_CAST = ('林雪', '陈默', '周律师', '王婶', '陈母')

# Distinctive substrings of the two hidden knowledge items used in the
# isolation test: 林雪's private secret and a game-master-only truth.
_SECRET_MARK = '火漆印'
_GM_TRUTH_MARK = '调包'

# Marker spoken by 林雪 in the propagation test.
_MARKER = 'MARKER_SECRET_42'


def _embedder(_: str) -> np.ndarray:
  return np.ones(16, dtype=np.float32)


def _character_params(character: story_config.CharacterInfo) -> dict[str, Any]:
  """Maps a CharacterInfo onto the story character prefab params."""
  return {
      'name': character.name,
      'identity': character.identity,
      'goal': character.goal,
      'hard_constraint': character.hard_constraint,
      'secret': character.secret,
      'public_facts': tuple(character.public_facts),
      'style': character.style,
      'initial_relationship_notes': character.initial_relationship_notes,
      'randomize_choices': False,
  }


def _gm_params(
    config: story_config.StoryConfig,
    participants: tuple[str, ...],
) -> dict[str, Any]:
  """Maps the story config onto the story game master prefab params.

  The game master conditions every resolution on the hidden truth
  (`gm_only_facts`) and on the private secrets of the participating
  characters; only the public conflict start enters scene premises.

  Args:
    config: the story configuration.
    participants: names of the characters present in the scene.

  Returns:
    The prefab params of the story game master.
  """
  scene = scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='故事主线',
          action_spec=story_config.make_speech_action_spec(),
      ),
      participants=participants,
      num_rounds=4,
      premise={name: (config.conflict_start,) for name in participants},
  )
  beat = drama_director.Beat(
      id='b1',
      description='遗嘱宣读后的第一轮争执：各方表态，旧怨浮出水面',
      tension_range=(3, 5),
      done_question='各方都已经表明立场了吗？',
      min_turns=5,
      max_turns=24,
  )
  return {
      **run_lib.make_gm_params(config),
      'scene_dicts': (story_scene.scene_to_dict(scene),),
      'beat_dicts': (beat.to_dict(),),
  }


def _build_simulation(
    config: story_config.StoryConfig,
    participants: tuple[str, ...],
    *,
    dialogue_lines: dict[str, str] | None = None,
) -> tuple[
    simulation_lib.Simulation,
    testing_stubs.PromptStubModel,
    testing_stubs.PromptStubModel,
]:
  """Builds a two-character story simulation from the real prefabs.

  Args:
    config: the story configuration.
    participants: names of the two characters present in the scene.
    dialogue_lines: per-character lines for the agent stub.

  Returns:
    The simulation plus the agent and game master stub models, so tests
    can inspect the prompts each side actually saw.
  """
  agent_model = testing_stubs.PromptStubModel(dialogue_lines=dialogue_lines)
  gm_model = testing_stubs.PromptStubModel()
  by_name = {character.name: character for character in config.characters}
  instances = [
      prefab_lib.InstanceConfig(
          prefab='story_character',
          role=prefab_lib.Role.ENTITY,
          params=_character_params(by_name[name]),
      )
      for name in participants
  ]
  instances.append(
      prefab_lib.InstanceConfig(
          prefab='story_gm',
          role=prefab_lib.Role.GAME_MASTER,
          params=_gm_params(config, participants),
      )
  )
  sim_config = prefab_lib.Config(
      prefabs={
          'story_character': story_character.Entity(),
          'story_gm': story_dramaturgic.GameMaster(),
      },
      instances=instances,
      default_premise='',
      default_max_steps=2,
  )
  sim = simulation_lib.Simulation(
      config=sim_config,
      model=gm_model,
      embedder=_embedder,
      override_agent_model=agent_model,
  )
  return sim, agent_model, gm_model


def _speech_prompts(
    model: testing_stubs.PromptStubModel, name: str
) -> list[str]:
  """Returns the recorded action prompts of one character's speeches."""
  return [p for p in model.text_prompts if f'{name} 现在说什么' in p]


def _memories_text(sim: simulation_lib.Simulation, name: str) -> str:
  """Returns everything one character's memory holds, joined as text."""
  entities_by_name = {entity.name: entity for entity in sim.entities}
  memory = entities_by_name[name].get_component('__memory__')
  return '\n'.join(memory.get_all_memories_as_text())


class AppendixScenarioContentTest(unittest.TestCase):
  """The example config must carry the appendix-C estate-dispute story."""

  def setUp(self):
    super().setUp()
    self.config = story_config.make_example_config()

  def test_validate_accepts_the_config(self):
    self.assertEqual(story_config.validate(self.config), [])

  def test_five_character_cast(self):
    self.assertEqual([c.name for c in self.config.characters], list(_CAST))

  def test_every_character_has_mandated_fields(self):
    for character in self.config.characters:
      with self.subTest(name=character.name):
        for field in ('identity', 'goal', 'hard_constraint', 'style'):
          self.assertTrue(getattr(character, field).strip())
        self.assertTrue(character.public_facts)

  def test_lawyer_is_marked_as_the_player_character(self):
    lawyer = next(c for c in self.config.characters if c.name == '周律师')
    self.assertIn('玩家角色', lawyer.identity)

  def test_gm_only_facts_hold_the_swapped_will_truth(self):
    self.assertGreaterEqual(len(self.config.gm_only_facts), 2)
    self.assertTrue(any('调包' in fact for fact in self.config.gm_only_facts))

  def test_environment_events_catalogue(self):
    events = self.config.allowed_environment_events
    self.assertGreaterEqual(len(events), 6)
    joined = ' '.join(events)
    for marker in ('雷雨', '手机', '邮差', '花瓶', '邻居', '收音机'):
      self.assertIn(marker, joined)

  def test_immutable_facts_cover_house_and_kinship(self):
    joined = ' '.join(self.config.immutable_facts)
    self.assertIn('老宅', joined)
    self.assertIn('遗孀', joined)

  def test_private_secrets_of_lin_xue_and_chen_mother(self):
    by_name = {c.name: c for c in self.config.characters}
    lin_xue = by_name['林雪'].secret
    chen_mother = by_name['陈母'].secret
    # 林雪 witnessed 陈母 burning a letter; 陈母 holds her own secret.
    self.assertIn('陈母', lin_xue)
    self.assertIn('烧', lin_xue)
    self.assertIn('遗嘱', chen_mother)
    self.assertNotEqual(lin_xue, chen_mother)

  def test_conflict_start_and_endings(self):
    self.assertTrue(self.config.conflict_start.strip())
    self.assertGreaterEqual(len(self.config.allowed_endings), 2)


class InformationIsolationTest(unittest.TestCase):
  """Hidden knowledge stays hidden until a character reveals it.

  林雪 (A) holds private secret S, 王婶 (B) holds none, and the game
  master additionally knows S and the ground truth G. All assertions
  read pre-act values through the real act lifecycle: the prompts below
  are those the entities' own act calls sent to the model.
  """

  def test_private_secret_reaches_only_its_owner(self):
    config = story_config.make_example_config()
    sim, agent_model, _ = _build_simulation(config, ('林雪', '王婶'))
    sim.play(max_steps=2)

    # A's recorded action prompt carries her own brief and secret.
    a_prompts = _speech_prompts(agent_model, '林雪')
    self.assertEqual(len(a_prompts), 1)
    self.assertIn('住家看护', a_prompts[0])
    self.assertIn('\nPrivate secret', a_prompts[0])
    self.assertIn(_SECRET_MARK, a_prompts[0])

    # B's recorded action prompt has no secret section and no leak of S.
    b_prompts = _speech_prompts(agent_model, '王婶')
    self.assertEqual(len(b_prompts), 1)
    self.assertIn('老邻居', b_prompts[0])
    self.assertNotIn('\nPrivate secret', b_prompts[0])
    self.assertNotIn(_SECRET_MARK, b_prompts[0])

    # B's whole memory is clean of S as well.
    self.assertNotIn(_SECRET_MARK, _memories_text(sim, '王婶'))

  def test_all_cast_constraints_reach_gm_without_entering_other_players(self):
    config = story_config.make_example_config()
    config = dataclasses.replace(
        config,
        characters=tuple(
            dataclasses.replace(c, hard_constraint=f'CONSTRAINT_{index}_ONLY')
            for index, c in enumerate(config.characters)
        ),
    )
    sim, agent_model, gm_model = _build_simulation(config, ('林雪', '王婶'))
    sim.play(max_steps=2)
    reviews = [
        prompt
        for prompt in gm_model.text_prompts
        if '# Story resolution semantic review' in prompt
    ]
    self.assertEqual(len(reviews), 2)
    for review in reviews:
      for character in config.characters:
        self.assertIn(character.hard_constraint, review)
    for name in ('林雪', '王婶'):
      prompt = _speech_prompts(agent_model, name)[0]
      for character in config.characters:
        if character.name == name:
          self.assertIn(character.hard_constraint, prompt)
        else:
          self.assertNotIn(character.hard_constraint, prompt)
          self.assertNotIn(character.hard_constraint, _memories_text(sim, name))

  def test_action_spec_carries_no_hidden_facts_or_endings(self):
    # 设计 4：角色只读自己的合法观察与私人设定；ActionSpec 是共享
    # 召唤语，不得携带导演节拍、张力、允许结局或他人秘密。
    prompt = story_config.make_scene_action_spec(
        'speech_and_action'
    ).call_to_action
    config = story_config.make_example_config()
    for character in config.characters:
      if character.secret:
        self.assertNotIn(character.secret, prompt)
    for fact in config.gm_only_facts:
      self.assertNotIn(fact, prompt)
    for ending in config.allowed_endings:
      self.assertNotIn(ending, prompt)
    self.assertNotIn('张力', prompt)
    self.assertNotIn('beat', prompt)

  def test_gm_truth_conditions_resolutions_only(self):
    config = story_config.make_example_config()
    sim, agent_model, gm_model = _build_simulation(config, ('林雪', '王婶'))
    sim.play(max_steps=2)

    # Neither player's action prompt ever sees the ground truth G.
    for prompt in _speech_prompts(agent_model, '林雪') + _speech_prompts(
        agent_model, '王婶'
    ):
      self.assertNotIn(_GM_TRUTH_MARK, prompt)
    self.assertNotIn(_GM_TRUTH_MARK, _memories_text(sim, '王婶'))

    # The game master's resolution prompts are conditioned on G and S.
    resolutions = [
        p for p in gm_model.text_prompts if 'Putative event to resolve' in p
    ]
    self.assertEqual(len(resolutions), 2)
    for prompt in resolutions:
      self.assertIn(_GM_TRUTH_MARK, prompt)
      self.assertIn(_SECRET_MARK, prompt)


class DialoguePropagationTest(unittest.TestCase):
  """Spoken dialogue reaches the next player; stage directions do not."""

  def test_marker_line_reaches_next_speaker(self):
    config = story_config.make_example_config()
    marker_line = f'我最后说一遍：{_MARKER}'
    sim, agent_model, gm_model = _build_simulation(
        config, ('林雪', '王婶'), dialogue_lines={'林雪': marker_line}
    )
    sim.play(max_steps=2)

    # 林雪 spoke the marker in step one: her one action prompt exists
    # and the game master resolved it with the speech verbatim.
    self.assertEqual(len(_speech_prompts(agent_model, '林雪')), 1)
    resolutions = [
        p for p in gm_model.text_prompts if 'Putative event to resolve' in p
    ]
    self.assertEqual(len(resolutions), 2)
    self.assertIn(_MARKER, resolutions[0])

    # B observed the resolved event and acted on it: the marker is in
    # both B's memory and B's actual action prompt.
    self.assertIn(_MARKER, _memories_text(sim, '王婶'))
    b_prompts = _speech_prompts(agent_model, '王婶')
    self.assertEqual(len(b_prompts), 1)
    self.assertIn(_MARKER, b_prompts[0])
    self.assertIn('窗外的雷声滚过，烛火摇曳。', b_prompts[0])
    self.assertIn('窗外的雷声滚过，烛火摇曳。', _memories_text(sim, '王婶'))

    # The narrator track never restates the spoken line, and B's own
    # authored speech is preserved verbatim in the committed turn.
    gm = sim.game_masters[0]
    director = gm.get_component(
        drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director.DramaDirector,
    )
    turn = director.get_last_story_turn()
    self.assertIsNotNone(turn)
    self.assertEqual(turn['actor'], '王婶')
    self.assertEqual(turn['speech'], '这宅子的事，急不得。')
    self.assertEqual(
        turn['resolved_event'],
        '王婶: 这宅子的事，急不得。\n' + turn['stage_directions'],
    )
    self.assertTrue(turn['stage_directions'])
    self.assertNotIn(_MARKER, turn['stage_directions'])


if __name__ == '__main__':
  unittest.main()
