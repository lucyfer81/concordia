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
import json
import os
import tempfile
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.components.game_master import audience_delivery
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import observation_queue
from concordia.contrib.components.game_master import story_action
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.prefabs.entity import story_character
from concordia.contrib.prefabs.game_master import story_dramaturgic
from concordia.contrib.prefabs.game_master import story_scene
from concordia.prefabs.simulation import generic as simulation_lib
from concordia.typing import entity as entity_lib
from concordia.typing import prefab as prefab_lib
from concordia.typing import scene as scene_lib
import numpy as np

from examples.story_enactment import collector
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
    *,
    action_mode: str = 'speech_only',
) -> dict[str, Any]:
  """Maps the story config onto the story game master prefab params.

  The game master conditions every resolution on the hidden truth
  (`gm_only_facts`) and on the private secrets of the participating
  characters; only the public conflict start enters scene premises.

  Args:
    config: the story configuration.
    participants: names of the characters present in the scene.
    action_mode: the action contract the scene elicits and the game
      master decodes (`speech_only` or `speech_and_action`).

  Returns:
    The prefab params of the story game master.
  """
  scene = scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='故事主线',
          action_spec=story_config.make_scene_action_spec(action_mode),
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
      'action_mode': action_mode,
      'scene_dicts': (story_scene.scene_to_dict(scene),),
      'beat_dicts': (beat.to_dict(),),
  }


def _build_simulation(
    config: story_config.StoryConfig,
    participants: tuple[str, ...],
    *,
    dialogue_lines: dict[str, str] | None = None,
    action_mode: str = 'speech_only',
    agent_model: testing_stubs.PromptStubModel | None = None,
    gm_model: testing_stubs.PromptStubModel | None = None,
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
    action_mode: the action contract the scene elicits.
    agent_model: optional preconfigured agent stub; when omitted, one
      is created from `dialogue_lines`.
    gm_model: optional preconfigured game master stub.

  Returns:
    The simulation plus the agent and game master stub models, so tests
    can inspect the prompts each side actually saw.
  """
  if agent_model is None:
    agent_model = testing_stubs.PromptStubModel(dialogue_lines=dialogue_lines)
  if gm_model is None:
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
          params=_gm_params(config, participants, action_mode=action_mode),
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
    # 2026-10-24 事件准入修复：默认白名单缩减为五项（来电与邮差旧信
    # 不再由默认导演引入），下限与标记同步收紧。
    self.assertGreaterEqual(len(events), 5)
    joined = ' '.join(events)
    for marker in ('雷雨', '花瓶', '邻居', '收音机', '白烛'):
      self.assertIn(marker, joined)
    for removed in ('手机', '邮差'):
      self.assertNotIn(removed, joined)

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


class _SequencedStubModel(testing_stubs.PromptStubModel):
  """A stub whose characters speak a configured sequence of lines.

  The audience-delivery scenarios need one character to act silently on
  one turn and disclose on a later one, while the base stub returns one
  fixed line per character. Each sequence entry is a
  ``(speech, action_intent)`` pair consumed in order by the
  speech-and-action contract call; exhausted sequences and unlisted
  characters fall back to the base stub's per-character mapping.
  """

  def __init__(self, *, speech_sequences=None, **kwargs):
    super().__init__(**kwargs)
    self._speech_sequences = {
        name: [(speech, intent) for speech, intent in lines]
        for name, lines in (speech_sequences or {}).items()
    }

  def _action_contract_output(self, prompt: str) -> str:
    match = testing_stubs._SPEAKER_IN_CALL.search(prompt)  # pylint: disable=protected-access
    name = match.group(1) if match else ''
    lines = self._speech_sequences.get(name)
    if lines:
      speech, intent = lines.pop(0)
    else:
      speech = self._dialogue_lines.get(name, self._default_line)
      intent = self._action_intents.get(name, '')
    payload = json.dumps(
        {'speech': speech, 'action_intent': intent}, ensure_ascii=False
    )
    story_action.decode_action(payload)
    return payload


# Distinctive private fact of the R1-t3-style lookup turn: a concrete
# date the acting character learns privately (design section 8).
_DATE_MARK = '二〇一九年三月十二日'
_LOOKUP_INTENT = '周律师借口复核，独自抽出遗嘱底档查看落款日期。'
_LOOKUP_RESULT = (
    f'档案室没有别人。周律师翻开底档，看清落款日期是{_DATE_MARK}，'
    '与公示文本不一致。'
)
# The public narration of the same turn: the visible act of consulting
# the file, explicitly WITHOUT the private date.
_PUBLIC_STAGE = '周律师独自在档案桌前翻看了片刻底档。'
_DISCLOSURE_LINE = f'我看过了：底档的落档日期是{_DATE_MARK}。'
_LAWYER, _AUNT = '周律师', '王婶'


def _build_delivery_scenario(
    *,
    speech_sequences=None,
    action_result: str = _LOOKUP_RESULT,
    public_stage_directions: str | None = _PUBLIC_STAGE,
    stage_directions: str = '',
    review_response: str = '{"valid": true, "reason": "scripted approval"}',
    participants: tuple[str, ...] = (_LAWYER, _AUNT),
):
  """Builds the two-character speech-and-action delivery scenario.

  The lawyer's first turn is the R1-t3-style private lookup: a
  speech-and-action contract turn whose core-adjudicated result carries
  a concrete date, while the explicitly configured public narrator
  track describes only the visible consulting act.

  Args:
    speech_sequences: per-character sequence of ``(speech,
      action_intent)`` pairs spoken by the agent stub.
    action_result: the core attempt-to-result answer of the game
      master stub.
    public_stage_directions: the public narrator track; None mirrors
      the full track (a fully public turn).
    stage_directions: the fixed background narration of the full track.
    review_response: scripted semantic-review verdict.
    participants: the scene participants, in acting order.

  Returns:
    The simulation plus the agent and game master stub models.
  """
  config = story_config.make_example_config()
  agent_model = _SequencedStubModel(speech_sequences=speech_sequences)
  gm_model = testing_stubs.PromptStubModel(
      action_result=action_result,
      public_stage_directions=public_stage_directions,
      stage_directions=stage_directions,
      review_response=review_response,
      # A clean integer keeps the scheduled tension scoring to exactly
      # one call (no parse re-ask), so call counts stay deterministic.
      scoring_answer='5',
  )
  return _build_simulation(
      config,
      participants,
      action_mode='speech_and_action',
      agent_model=agent_model,
      gm_model=gm_model,
  )


def _director_of(
    sim: simulation_lib.Simulation,
) -> drama_director.DramaDirector:
  """Returns the drama director of the scenario's game master."""
  return sim.game_masters[0].get_component(
      drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
      type_=drama_director.DramaDirector,
  )


def _queue_state(sim: simulation_lib.Simulation) -> dict[str, list[str]]:
  """Returns the game master's undelivered per-player observation queue."""
  return (
      sim.game_masters[0]
      .get_component('__make_observation__')
      .get_state()['queue']
  )


def _queued_items(sim: simulation_lib.Simulation, name: str) -> list[str]:
  """Returns one player's undelivered queue items, boundary-decoded."""
  return [
      observation_queue.decode_observation(item)[0] or item
      for item in _queue_state(sim).get(name, [])
  ]


class AudienceDeliveryIsolationTest(unittest.TestCase):
  """Design section 8: prove WHO received which view, not just the text.

  Every test drives the real story prefabs through the real act
  lifecycle and asserts on the next player's actual reasoning input
  (the recorded action prompt of the entity's own act call), never on
  final dialogue alone.
  """

  def test_private_lookup_reaches_only_actor_inputs(self):
    sim, agent_model, _ = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [('', _LOOKUP_INTENT), ('这件事我要再想想。', '')]
        }
    )
    sim.play(max_steps=1)
    turn = _director_of(sim).get_last_story_turn()
    self.assertIsNotNone(turn)
    self.assertIn(_DATE_MARK, turn['resolved_event'])
    self.assertNotIn(_DATE_MARK, turn['public_resolved_event'])
    # The undelivered queue holds the full view for the actor and the
    # public view (no date) for the other participant.
    self.assertEqual(_queued_items(sim, _LAWYER), [turn['resolved_event']])
    self.assertEqual(_queued_items(sim, _AUNT), [turn['public_resolved_event']])
    # The next actual character inputs: the actor's own next prompt
    # carries the date; the other participant's does not, and neither
    # does her whole memory.
    sim.play(max_steps=2)
    lawyer_prompts = _speech_prompts(agent_model, _LAWYER)
    self.assertEqual(len(lawyer_prompts), 2)
    self.assertIn(_DATE_MARK, lawyer_prompts[1])
    aunt_prompts = _speech_prompts(agent_model, _AUNT)
    self.assertEqual(len(aunt_prompts), 1)
    self.assertNotIn(_DATE_MARK, aunt_prompts[0])
    self.assertNotIn(_DATE_MARK, _memories_text(sim, _AUNT))
    self.assertIn(_DATE_MARK, _memories_text(sim, _LAWYER))

  def test_later_verbal_disclosure_propagates_verbatim(self):
    sim, agent_model, gm_model = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [('', _LOOKUP_INTENT), (_DISCLOSURE_LINE, '')]
        }
    )
    sim.play(max_steps=4)
    aunt_prompts = _speech_prompts(agent_model, _AUNT)
    self.assertEqual(len(aunt_prompts), 2)
    # The private date stayed private until the lawyer spoke it.
    self.assertNotIn(_DATE_MARK, aunt_prompts[0])
    # After the disclosure the other participant's actual input carries
    # the date exactly as the real dialogue spoke it, character for
    # character.
    self.assertIn(_DISCLOSURE_LINE, aunt_prompts[1])
    self.assertIn(_DISCLOSURE_LINE, _memories_text(sim, _AUNT))
    # The original speech of the disclosure turn entered the committed
    # public history verbatim.
    director = _director_of(sim)
    self.assertTrue(
        any(_DISCLOSURE_LINE in event for event in director.get_public_events())
    )

  def test_public_event_and_intervention_survive_public_view(self):
    public_result = '管家上前把堂屋的灯拧亮，雷声顺着门缝滚进来。'
    sim, agent_model, _ = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [('请大家看这份底档。', '周律师当众把底档摊开在桌上。')]
        },
        action_result=public_result,
        # No explicit public track: the two views are allowed to be
        # identical for a fully public turn.
        public_stage_directions=None,
    )
    sim.play(max_steps=2)
    director = _director_of(sim)
    # The public action turn is the first committed turn: its two views
    # are allowed to be identical, and the whitelisted environmental
    # response survives in both.
    committed = director.get_committed_events()
    public_events = director.get_public_events()
    self.assertEqual(public_events[0], committed[0])
    self.assertIn(public_result, public_events[0])
    # The bystander's next actual input receives the public success and
    # the environmental response.
    aunt_prompts = _speech_prompts(agent_model, _AUNT)
    self.assertEqual(len(aunt_prompts), 1)
    self.assertIn(public_result, aunt_prompts[0])
    self.assertIn(public_result, _memories_text(sim, _AUNT))

  def test_pure_private_action_queues_nothing_for_others(self):
    sim, agent_model, _ = _build_delivery_scenario(
        speech_sequences={_LAWYER: [('', _LOOKUP_INTENT)]},
        public_stage_directions='',
    )
    sim.play(max_steps=1)
    turn = _director_of(sim).get_last_story_turn()
    self.assertIsNotNone(turn)
    # The full result is legal and non-empty; the public event is
    # empty and no visible action is fabricated to fill it.
    self.assertTrue(turn['resolved_event'].strip())
    self.assertIn(_DATE_MARK, turn['resolved_event'])
    self.assertEqual(turn['public_resolved_event'], '')
    self.assertEqual(_director_of(sim).get_public_events(), ())
    queue = _queue_state(sim)
    # No queue item for the other participant — a drained key may hold
    # an empty list, but no item and never an empty-string view.
    self.assertEqual(queue.get(_AUNT, []), [])
    self.assertEqual(_queued_items(sim, _LAWYER), [turn['resolved_event']])
    # And the other participant's next actual input stays date-free.
    sim.play(max_steps=1)
    self.assertEqual(len(_speech_prompts(agent_model, _AUNT)), 1)
    self.assertNotIn(_DATE_MARK, _speech_prompts(agent_model, _AUNT)[0])
    self.assertNotIn(_DATE_MARK, _memories_text(sim, _AUNT))

  def test_speech_turn_public_view_still_carries_speech(self):
    marker_line = '王婶当众说：这份底档要当众念一遍。'
    sim, agent_model, _ = _build_delivery_scenario(
        speech_sequences={_AUNT: [(marker_line, '')]},
        participants=(_AUNT, _LAWYER),
        action_result='',
        public_stage_directions='',
        stage_directions='窗外的雷声滚过，烛火摇曳。',
    )
    sim.play(max_steps=2)
    director = _director_of(sim)
    # The speech turn is the first committed turn: with an empty public
    # narrator track the public event still carries the spoken line
    # verbatim, and only the full event carries the background
    # narration (Review Focus 1).
    self.assertEqual(director.get_public_events()[0], f'{_AUNT}: {marker_line}')
    self.assertIn('窗外的雷声滚过', director.get_committed_events()[0])
    self.assertNotIn('窗外的雷声滚过', director.get_public_events()[0])
    # The bystander's next actual input received the verbatim speech.
    lawyer_prompts = _speech_prompts(agent_model, _LAWYER)
    self.assertEqual(len(lawyer_prompts), 1)
    self.assertIn(marker_line, lawyer_prompts[0])
    self.assertIn(marker_line, _memories_text(sim, _LAWYER))

  def test_review_rejection_delivers_nothing(self):
    sim, _, _ = _build_delivery_scenario(
        review_response='{"valid": false, "reason": "scripted rejection"}'
    )
    make_observation = sim.game_masters[0].get_component('__make_observation__')
    with mock.patch.object(
        make_observation, 'add_to_queue', wraps=make_observation.add_to_queue
    ) as add:
      with self.assertRaisesRegex(
          story_resolution.StoryOutputError, 'scripted rejection'
      ):
        sim.play(max_steps=1)
    # Exact-write pinning: the only queue writes of the aborted step are
    # the scene premises the scene tracker queued at scene start — one
    # per participant, in participant order, each carrying exactly the
    # configured conflict start. NOTHING new was enqueued for anyone:
    # not the wrapped full view, and not the bare public view either —
    # the date-free, unwrapped payload of a partial-delivery bug would
    # fail here just as loudly. (The premise contains neither the
    # 'Event:' marker nor the private date, so this pinning subsumes the
    # earlier marker filters.)
    premise = story_config.make_example_config().conflict_start
    self.assertEqual(
        add.call_args_list,
        [mock.call(_LAWYER, premise), mock.call(_AUNT, premise)],
    )
    director = _director_of(sim)
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(director.get_state()['story_turn'], 0)
    self.assertEqual(director.get_public_events(), ())

  def test_full_broadcast_mutation_is_red(self):
    # 变异测试（设计 8）：临时把投递换成“向所有场景参与者投递完整
    # 事件”的行为，核心隔离断言（其他角色输入无日期）必须在此变异
    # 下失败；恢复真实组件后同一断言通过。变异只存在于本测试内。
    def _broadcast_mutation(component, action_attempt):
      if (
          component._last_action_spec is None  # pylint: disable=protected-access
          or component._last_action_spec.output_type  # pylint: disable=protected-access
          != entity_lib.OutputType.RESOLVE
      ):
        return ''
      director = component.get_entity().get_component(
          component._drama_director_component_key  # pylint: disable=protected-access
      )
      data = director.get_validated_delivery_data()
      make_observation = component.get_entity().get_component(
          component._make_observation_component_key  # pylint: disable=protected-access
      )
      for name in component._player_filter():  # pylint: disable=protected-access
        make_observation.add_to_queue(name, data['resolved_event'])
      return ''

    with mock.patch.object(
        audience_delivery.AudienceDelivery, 'post_act', _broadcast_mutation
    ):
      sim, agent_model, _ = _build_delivery_scenario(
          speech_sequences={
              _LAWYER: [('', _LOOKUP_INTENT), ('这件事我要再想想。', '')]
          }
      )
      sim.play(max_steps=2)
      leaked_prompt = _speech_prompts(agent_model, _AUNT)[0]
      self.assertIn(_DATE_MARK, leaked_prompt)
      with self.assertRaises(AssertionError):
        # The core assertion of test_private_lookup_reaches_only_
        # actor_inputs must FAIL under the full-broadcast mutation —
        # even though the bystander's stub never speaks the date.
        self.assertNotIn(_DATE_MARK, leaked_prompt)

    sim, agent_model, _ = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [('', _LOOKUP_INTENT), ('这件事我要再想想。', '')]
        }
    )
    sim.play(max_steps=2)
    self.assertNotIn(_DATE_MARK, _speech_prompts(agent_model, _AUNT)[0])

  def test_pending_candidate_not_in_any_history(self):
    first_line = '第一句：先把底档收好。'
    second_line = '第二句：王婶的当众发言。'
    sim, _, gm_model = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [(first_line, '')],
            _AUNT: [(second_line, '')],
        },
        action_result='',
        public_stage_directions='',
    )
    sim.play(max_steps=2)
    finals = [
        p for p in gm_model.text_prompts if testing_stubs._FINAL_OUTPUT_MARK in p  # pylint: disable=protected-access
    ]
    self.assertEqual(len(finals), 2)
    # The second final-generation prompt is turn 2's: it carries the
    # current candidate (王婶's pending line)...
    self.assertIn(second_line, finals[1])
    section_marks = (
        '## Public history (data)',
        (
            '## GM complete committed history'
            ' (GM-only, not common knowledge, data)'
        ),
    )

    def read_history(prompt: str, mark: str) -> str:
      index = prompt.index(mark) + len(mark)
      value, _ = json.JSONDecoder().raw_decode(
          prompt[prompt.index('"', index) :]
      )
      return value

    # ...but neither history segment contains the pending candidate,
    # while the genuinely committed first turn does appear publicly.
    public_history = read_history(finals[1], section_marks[0])
    gm_history = read_history(finals[1], section_marks[1])
    self.assertNotIn(second_line, public_history)
    self.assertNotIn(second_line, gm_history)
    self.assertIn(first_line, public_history)
    self.assertIn(first_line, gm_history)

  def test_delivery_adds_no_model_calls(self):
    # 投递本身不产生任何模型调用：把一轮内全部 text 调用按既有
    # 指纹分类，任何未知类别的调用都会让本测试失败；各类别的计数
    # 恰好等于既有裁决/评审/角色推理的模式，无投递额外调用。
    sim, agent_model, gm_model = _build_delivery_scenario(
        speech_sequences={
            _LAWYER: [('', _LOOKUP_INTENT), ('这件事我要再想想。', '')]
        }
    )
    sim.play(max_steps=2)
    known_agent_marks = (
        '现在说什么',  # the action-contract call to action
        'What kind of situation is',  # situation perception
        'What kind of person is',  # self perception
        'What would a person like',  # action tendency
    )
    known_gm_marks = (
        testing_stubs._FINAL_OUTPUT_MARK,  # pylint: disable=protected-access
        '# Story resolution semantic review',
        'What happens as a result of the attempted action?',
        'Respond with a single integer from 1 to 10',  # tension scoring
    )
    for prompt in agent_model.text_prompts:
      self.assertTrue(
          any(mark in prompt for mark in known_agent_marks),
          f'unknown agent call: {prompt[-200:]!r}',
      )
    for prompt in gm_model.text_prompts:
      self.assertTrue(
          any(mark in prompt for mark in known_gm_marks),
          f'unknown game master call: {prompt[-200:]!r}',
      )
    # Exact per-category counts freeze the existing call pattern: the
    # lawyer's lookup turn runs adjudication + final + review, the
    # aunt's speech turn runs final + review + the scheduled tension
    # scoring (eval_interval=2); each acting character runs three
    # perception questions and one contract call. Delivery adds none.
    self.assertEqual(len(gm_model.text_prompts), 6)
    self.assertEqual(
        sum(
            1 for p in gm_model.text_prompts if 'What happens as a result' in p
        ),
        1,
    )
    self.assertEqual(
        sum(1 for p in gm_model.text_prompts if 'semantic review' in p), 2
    )
    self.assertEqual(len(agent_model.text_prompts), 8)
    self.assertEqual(
        sum(1 for p in agent_model.text_prompts if '现在说什么' in p), 2
    )


class _StateDirTestCase(unittest.TestCase):
  """Base case providing a per-test state directory and file readers."""

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.state_dir = self._tmp.name

  def turn_log(self) -> collector.StoryTurnLog:
    return collector.StoryTurnLog(
        os.path.join(self.state_dir, 'story_turns.jsonl')
    )

  def envelope(self) -> dict[str, Any]:
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)


def _budget_beat(beat_id: str = 'b1'):
  """Returns one minimal beat that never completes under the stub."""
  return drama_director.Beat(
      id=beat_id,
      description=f'节拍 {beat_id}：出现可供角色质疑的新情况',
      tension_range=(3, 6),
      done_question=f'节拍 {beat_id} 的完成条件是否已经出现？',
      min_turns=2,
      max_turns=99,
  )


def _budget_run_config(
    *, total_model_attempt_limit: int, prompt_stub: dict[str, Any]
) -> dict[str, Any]:
  """Builds one budget-boundary run config of the stub-driven example."""
  config = run_lib.make_run_config(
      story_config.make_example_config(),
      total_turn_limit=4,
      total_model_attempt_limit=total_model_attempt_limit,
      action_mode='speech_only',
      model_config={'api_type': 'stub', 'prompt_stub': dict(prompt_stub)},
  )
  config['beat_dicts'] = [_budget_beat().to_dict()]
  return config


class AudienceDeliveryBudgetTest(_StateDirTestCase):
  """Budget paths A/B with the delivery adapter wired in the prefab.

  Follows the injection pattern of the path A/B tests of
  `story_enactment_test.py`: the stub model is injected through
  `make_language_model` and the built simulation is captured through a
  wrapping `build_simulation` so the undelivered queue state can be
  inspected after the run.
  """

  def _run_with_capture(self, run_config, prompt_stub):
    # The stub is injected through `make_language_model` (the same
    # pattern as the path A/B tests of `story_enactment_test.py`), and
    # the built simulation is captured through a wrapping
    # `build_simulation` so the undelivered queue state can be
    # inspected after the run.
    stub = testing_stubs.PromptStubModel(**prompt_stub)
    captured: dict[str, Any] = {}
    real_build = run_lib.build_simulation

    def capturing_build(**kwargs):
      built = real_build(**kwargs)
      captured['sim'] = built[0]
      return built

    with (
        mock.patch.object(
            run_lib, 'make_language_model', return_value=(stub, ())
        ),
        mock.patch.object(run_lib, 'build_simulation', capturing_build),
    ):
      result = run_lib.run(self.state_dir, run_config=run_config)
    return result, stub, captured['sim']

  def test_path_a_exhaustion_delivers_neither_view(self):
    # Path A: the attempt budget dies on turn 2's final-generation
    # request, before the review and before any approval exists.
    prompt_stub = {
        'stage_directions': '窗外的雷声滚过，烛火摇曳。',
        'public_stage_directions': '堂屋里安静了片刻。',
    }
    config = _budget_run_config(
        total_model_attempt_limit=10, prompt_stub=prompt_stub
    )
    result, _, sim = self._run_with_capture(config, prompt_stub)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    # Neither view was queued for anyone (drained keys may hold empty
    # lists from turn 1's delivery, but no item anywhere).
    for items in _queue_state(sim).values():
      self.assertEqual(items, [])
    # Nothing committed and no checkpoint advance past turn 1.
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    director = _director_of(sim)
    self.assertEqual(len(director.get_public_events()), 1)

  def test_path_b_exhaustion_commits_once_with_both_views(self):
    # Path B: the budget dies at turn 2's completion judgment, after
    # the approval; the turn commits exactly once and both views are
    # queued exactly once each.
    prompt_stub = {
        'stage_directions': '窗外的雷声滚过，烛火摇曳。',
        'public_stage_directions': '堂屋里安静了片刻。',
    }
    config = _budget_run_config(
        total_model_attempt_limit=12, prompt_stub=prompt_stub
    )
    result, _, sim = self._run_with_capture(config, prompt_stub)
    self.assertEqual(result.reason, 'budget_limit')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    self.assertTrue(records[1]['budget_stopped'])
    # The checkpoint advanced exactly one step and the run stopped on
    # the attempt boundary.
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    self.assertEqual(self.envelope()['used_model_attempts'], 12)
    # Exactly one queue item per participant: the actor holds the full
    # event, every other participant holds the public event.
    actor = records[1]['actor']
    queue = _queue_state(sim)
    director = _director_of(sim)
    participants = list(config['gm_params']['player_names'])
    self.assertEqual(set(queue), set(participants))
    for name in participants:
      self.assertEqual(len(queue[name]), 1, f'{name} queued exactly once')
    self.assertIn('窗外的雷声滚过', queue[actor][0])
    self.assertNotIn('堂屋里安静了片刻', queue[actor][0])
    for name in participants:
      if name != actor:
        self.assertIn('堂屋里安静了片刻', queue[name][0])
        self.assertNotIn('窗外的雷声滚过', queue[name][0])
    # The public history gained exactly this turn's public item.
    public_events = director.get_public_events()
    self.assertEqual(len(public_events), 2)
    self.assertIn('堂屋里安静了片刻', public_events[-1])


if __name__ == '__main__':
  unittest.main()
