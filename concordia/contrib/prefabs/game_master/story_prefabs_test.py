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

import json
from typing import Any, cast
from unittest import mock

from absl.testing import absltest
from concordia.agents import entity_agent_with_logging
from concordia.associative_memory import basic_associative_memory as associative_memory
from concordia.components import agent as agent_components
from concordia.components import game_master as gm_components
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.agent import emotional_stance
from concordia.contrib.components.agent import relationships as relationships_lib
from concordia.contrib.components.game_master import drama_director as drama_director_lib
from concordia.contrib.components.game_master import story_resolution
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

# Fingerprints of the core adjudication question (asked by the core
# `attempt_to_result` helper) and of the director-note fusion questions.
_ATTEMPT_QUESTION_MARK = 'What happens as a result of the attempted action?'
_ADJUDICATION_CONTEXT_MARK = '# Action adjudication (current turn only)'
_COMPLICATION_MARK = 'Suggest five plausible in-story complications'

# A valid speech-and-action JSON contract the mock player returns.
_ACTION_INTENT = 'Alice walks to the window and checks the latch.'
_CONTRACT_RESPONSE = json.dumps(
    {'speech': _SPEECH, 'action_intent': _ACTION_INTENT}, ensure_ascii=False
)
_CORE_ATTEMPT_ANSWER = 'The latch is loose; the window stays shut.'
# The core helper's true return value: its causal-statement glue plus
# the attempt answer.
_CORE_RESULT = f'{_ACTION_INTENT} Because of that, {_CORE_ATTEMPT_ANSWER}'
# Valid dual-track JSON for the speech-and-action chain: narrating the
# core-adjudicated result in the stage-directions track is required
# (the canonical event is assembled from speech and approved stage
# directions only), mirroring the example stub's evidence-driven draft.
_ACTION_DUAL_TRACK_RESPONSE = json.dumps(
    {
        'resolved_event': f'Alice: {_SPEECH}\n{_CORE_RESULT}',
        'stage_directions': _CORE_RESULT,
    },
    ensure_ascii=False,
)


def _last_block(prompt: str) -> str:
  """Returns the prompt from its last Question block on (routing scope)."""
  start = prompt.rfind('Question: ')
  return prompt[start:] if start >= 0 else ''


def _read_evidence(prompt: str) -> dict:
  """Reads the marked JSON resolution-evidence section of a prompt.

  Mirrors the decoder of the example's stub (contrib must not import
  examples, so the few lines live here): the section is a JSON object on
  the line after the mark, decoded with a JSON decoder so multi-line
  values survive verbatim.

  Args:
    prompt: the recorded final-generation or review prompt.

  Returns:
    The decoded evidence object.

  Raises:
    AssertionError: if the marked section holds no decodable object.
  """
  mark = '## Resolution evidence (data)'
  index = prompt.find(mark)
  assert index >= 0, 'prompt carries no evidence section'
  remainder = prompt[index + len(mark) :]
  start = remainder.find('{')
  payload, _ = json.JSONDecoder().raw_decode(remainder[start:])
  return payload


class _PromptRecordingModel(mock_model.MockModel):
  """Mock model recording every prompt it is asked to sample."""

  def __init__(
      self,
      response: str,
      *,
      review: str | None = None,
      attempt: str | None = None,
  ):
    super().__init__(response)
    self.prompts: list[str] = []
    self.review = review or '{"valid": true, "reason": "Only speech occurs."}'
    self.attempt = attempt

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.prompts.append(prompt)
    if '# Story resolution semantic review' in prompt:
      return self.review
    # Route on the last Question block: the resolution chain shares one
    # document, so earlier questions stay quoted inside later prompts.
    if self.attempt is not None and _ATTEMPT_QUESTION_MARK in _last_block(
        prompt
    ):
      return self.attempt
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


def _build_character(name: str, secret: str = '', response: str | None = None):
  model = (
      mock_model.MockModel(response)
      if response is not None
      else mock_model.MockModel()
  )
  return story_character.Entity(params=_character_params(name, secret)).build(
      model=model,
      memory_bank=associative_memory.AssociativeMemoryBank(
          sentence_embedder=_embedder
      ),
  )


def _build_prefab(prefab):
  """Builds the same prefab instance with a fresh model and bank."""
  return prefab.build(
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
    # One prefab instance (one params dict) built twice must produce fresh
    # components each time and must never cache them back into its params:
    # the params stay JSON-safe across builds.
    prefab = story_character.Entity(
        params=_character_params('Alice', secret='kept secret')
    )
    first = _build_prefab(prefab)
    second = _build_prefab(prefab)
    self.assertIsNot(
        first.get_component('CharacterBrief'),
        second.get_component('CharacterBrief'),
    )
    self.assertIsNot(
        first.get_component('PrivateSecret'),
        second.get_component('PrivateSecret'),
    )
    json.dumps(prefab.params)

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


class StoryCharacterDepthWiringTest(absltest.TestCase):
  """Stage-3 params wire emotion and relationship components per build."""

  def _build(self, **extra):
    params = _character_params('Alice')
    params.update(extra)
    return story_character.Entity(params=params).build(
        model=mock_model.MockModel(),
        memory_bank=associative_memory.AssociativeMemoryBank(
            sentence_embedder=_embedder
        ),
    )

  def test_emotion_options_wire_emotional_stance(self):
    entity = self._build(emotion_options=('calm', 'anxious'))
    stance = entity.get_component(
        story_character.EMOTIONAL_STANCE_COMPONENT_KEY,
        type_=emotional_stance.EmotionalStance,
    )
    self.assertIsInstance(stance, emotional_stance.EmotionalStance)
    # The component carries the character's name and the palette verbatim
    # (protected access is the only way to inspect the wiring).
    self.assertEqual(stance._name, 'Alice')  # pylint: disable=protected-access
    self.assertEqual(  # pylint: disable=protected-access
        stance._emotion_options,
        ['calm', 'anxious'],
    )

  def test_no_emotional_stance_without_emotion_options(self):
    entity = self._build()
    with self.assertRaises(KeyError):
      entity.get_component(story_character.EMOTIONAL_STANCE_COMPONENT_KEY)

  def test_relationships_wire_public_participant_names(self):
    entity = self._build(
        relationships_refresh_interval=3, other_player_names=('Bob', 'Carol')
    )
    component = entity.get_component(
        story_character.RELATIONSHIPS_COMPONENT_KEY,
        type_=relationships_lib.RelationshipsToOthers,
    )
    self.assertIsInstance(component, relationships_lib.RelationshipsToOthers)
    # The relationship view names exactly the other public participants.
    self.assertEqual(  # pylint: disable=protected-access
        component._other_player_names,
        ('Bob', 'Carol'),
    )
    self.assertEqual(component.get_state()['refresh_interval'], 3)

  def test_no_relationships_without_refresh_interval(self):
    # Participant names alone must not create the component: only a
    # positive refresh interval turns relationship perception on.
    entity = self._build(other_player_names=('Bob',))
    with self.assertRaises(KeyError):
      entity.get_component(story_character.RELATIONSHIPS_COMPONENT_KEY)

  def test_rebuild_creates_new_depth_components(self):
    # One prefab instance (one params dict) built twice must produce fresh
    # depth components each time and keep its params JSON-safe.
    prefab = story_character.Entity(
        params={
            **_character_params('Alice'),
            'emotion_options': ('calm',),
            'relationships_refresh_interval': 2,
            'other_player_names': ('Bob',),
        }
    )
    first = _build_prefab(prefab)
    second = _build_prefab(prefab)
    self.assertIsNot(
        first.get_component(story_character.EMOTIONAL_STANCE_COMPONENT_KEY),
        second.get_component(story_character.EMOTIONAL_STANCE_COMPONENT_KEY),
    )
    self.assertIsNot(
        first.get_component(story_character.RELATIONSHIPS_COMPONENT_KEY),
        second.get_component(story_character.RELATIONSHIPS_COMPONENT_KEY),
    )
    json.dumps(prefab.params)


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
    # One prefab instance (one params dict) built twice must produce fresh
    # components each time and must never cache them back into its params:
    # the params stay JSON-safe across builds.
    prefab = story_dramaturgic.GameMaster(
        params={
            'name': 'story rules',
            'scene_dicts': _SCENE_DICTS,
            'beat_dicts': _BEAT_DICTS,
            'world_constraints': _WORLD_CONSTRAINTS,
            'immutable_facts': _IMMUTABLE_FACTS,
        }
    )
    prefab.entities = [_build_character(name) for name in _PLAYERS]
    first = _build_prefab(prefab)
    second = _build_prefab(prefab)
    self.assertIsNot(
        first.get_component('__drama_director__'),
        second.get_component('__drama_director__'),
    )
    self.assertIsNot(
        first.get_component('world_constraints'),
        second.get_component('world_constraints'),
    )
    json.dumps(prefab.params)

  def test_real_act_resolution_prompt_contains_beat_and_constraints(self):
    players = [_build_character(name) for name in _PLAYERS]
    model = _PromptRecordingModel(_DUAL_TRACK_RESPONSE)
    gm = _build_game_master(
        model,
        players,
        player_names=list(_PLAYERS),
        character_constraints={
            'Alice': 'Alice never lies.',
            'Bob': 'Bob never signs.',
        },
        public_facts=('Both players saw the sealed envelope.',),
    )
    environment = sequential.Sequential()
    environment.run_loop(game_masters=[gm], entities=players, max_steps=1)
    # The final dual-track output step must see the current beat and the
    # world constraints in its prompt, proving the resolution chain is
    # conditioned on the drama director and the constraints component.
    resolution_prompts = [
        p
        for p in model.prompts
        if 'Output a JSON object with exactly two string fields' in p
    ]
    self.assertTrue(resolution_prompts)
    prompt = resolution_prompts[-1]
    self.assertIn('The rehearsal begins (beat b1)', prompt)
    self.assertIn('\n'.join(_WORLD_CONSTRAINTS), prompt)
    self.assertIn('\n'.join(_IMMUTABLE_FACTS), prompt)
    reviews = [
        p for p in model.prompts if '# Story resolution semantic review' in p
    ]
    self.assertLen(reviews, 1)
    for text in (prompt, reviews[0]):
      self.assertIn('Alice never lies.', text)
      self.assertIn('Bob never signs.', text)
      self.assertIn('Both players saw the sealed envelope.', text)
    director = gm.get_component('__drama_director__')
    committed = director.get_last_story_turn()
    self.assertEqual(committed['resolved_event'], f'Alice: {_SPEECH}')

  def test_semantic_rejection_never_broadcasts_or_commits_or_consumes(self):
    players = [_build_character(name) for name in _PLAYERS]
    model = _PromptRecordingModel(
        _DUAL_TRACK_RESPONSE,
        review='{"valid": false, "reason": "Invented joint agreement."}',
    )
    gm = _build_game_master(model, players)
    director = gm.get_component('__drama_director__')
    state = director.get_state()
    state['directive'] = {
        'beat_id': 'b1',
        'generated_at_turn': 0,
        'content': 'Thunder outside.',
        'status': 'integrating',
    }
    director.set_state(state)
    queue = gm.get_component('__make_observation__')
    with (
        mock.patch.object(
            queue, 'add_to_queue', wraps=queue.add_to_queue
        ) as add,
        mock.patch.object(
            director,
            'set_validated_resolution',
            wraps=director.set_validated_resolution,
        ) as validate,
    ):
      with self.assertRaisesRegex(
          story_resolution.StoryOutputError, 'Invented joint agreement'
      ):
        sequential.Sequential().run_loop(
            game_masters=[gm], entities=players, max_steps=1
        )
    validate.assert_not_called()
    self.assertFalse(any('Event:' in str(call) for call in add.call_args_list))
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(director.get_state()['story_turn'], 0)
    self.assertEqual(director.get_state()['directive']['status'], 'integrating')
    self.assertIsNone(director.get_state()['last_directive_executed_turn'])


class StoryActionModeWiringTest(absltest.TestCase):
  """The prefab validates and forwards action_mode through the chain.

  The action contract is observable only through behavior: with
  ``speech_and_action`` the built game master decodes the player's JSON
  contract, adjudicates the character's own action attempt through the
  core helper before the director note and the final dual-track output,
  and the core's true return value reaches both later prompts through
  the resolution evidence. The default build stays pure speech.
  """

  def test_build_rejects_unknown_action_mode(self):
    players = [_build_character(name) for name in _PLAYERS]
    with self.assertRaises(ValueError):
      _build_game_master(
          _PromptRecordingModel(_DUAL_TRACK_RESPONSE),
          players,
          action_mode='telepathy',
      )

  def test_default_build_is_speech_only_without_adjudication_calls(self):
    players = [_build_character(name) for name in _PLAYERS]
    model = _PromptRecordingModel(_DUAL_TRACK_RESPONSE)
    gm = _build_game_master(model, players)
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    self.assertFalse(
        any(_ADJUDICATION_CONTEXT_MARK in p for p in model.prompts)
    )
    self.assertFalse(any(_ATTEMPT_QUESTION_MARK in p for p in model.prompts))
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    turn = director.get_last_story_turn()
    assert turn is not None
    self.assertEqual(turn['speech'], _SPEECH)
    self.assertEqual(turn['action_intent'], '')
    self.assertEqual(turn['raw_action'], _SPEECH)

  def test_speech_and_action_decodes_and_adjudicates_before_final(self):
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_DUAL_TRACK_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        character_constraints={
            'Alice': 'Alice never lies.',
            'Bob': 'Bob never signs.',
        },
    )
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    turn = director.get_last_story_turn()
    assert turn is not None
    # The director received the contract mode: the raw JSON action is
    # decoded into speech and action intent for the committed turn.
    self.assertEqual(turn['speech'], _SPEECH)
    self.assertEqual(turn['action_intent'], _ACTION_INTENT)
    self.assertEqual(turn['raw_action'], _CONTRACT_RESPONSE)

    adjudication_prompts = [
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    ]
    self.assertLen(adjudication_prompts, 1)
    adjudication = adjudication_prompts[0]
    self.assertIn('## Current speaker\nAlice', adjudication)
    self.assertIn(f'## Original speech (verbatim)\n{_SPEECH}', adjudication)
    self.assertIn(
        "## The speaker's own attempted action (adjudicate only this)\n"
        + _ACTION_INTENT,
        adjudication,
    )
    self.assertIn('## Adjudication rules', adjudication)
    self.assertIn(
        'Never decide a new voluntary action for any other player character',
        adjudication,
    )
    self.assertIn(f'- immutable fact: {_IMMUTABLE_FACTS[0]}', adjudication)
    self.assertIn('- Alice: Alice never lies.', adjudication)
    self.assertIn('- Bob: Bob never signs.', adjudication)
    self.assertIn(
        'Player characters whose voluntary actions only they can choose:'
        ' Alice, Bob',
        adjudication,
    )

    final_prompts = [
        p
        for p in model.prompts
        if 'Output a JSON object with exactly two string fields' in p
    ]
    reviews = [
        p for p in model.prompts if '# Story resolution semantic review' in p
    ]
    self.assertLen(final_prompts, 1)
    self.assertLen(reviews, 1)
    # Chain order: adjudication, then the final dual-track generation,
    # then the review (the director note is the identity without a
    # pending directive, so it contributes no prompt).
    self.assertLess(
        model.prompts.index(adjudication), model.prompts.index(final_prompts[0])
    )
    self.assertLess(
        model.prompts.index(final_prompts[0]), model.prompts.index(reviews[0])
    )
    self.assertFalse(any(_COMPLICATION_MARK in p for p in model.prompts))
    # The core helper's true return value (its causal-statement glue
    # included) is the evidence both later prompts consume.
    core_result = f'{_ACTION_INTENT} Because of that, {_CORE_ATTEMPT_ANSWER}'
    for prompt in (final_prompts[0], reviews[0]):
      evidence = _read_evidence(prompt)
      self.assertEqual(evidence['action_intent'], _ACTION_INTENT)
      self.assertEqual(evidence['action_result'], core_result)
      self.assertIsNone(evidence['intervention'])

  def test_director_note_sits_between_adjudication_and_final_output(self):
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_DUAL_TRACK_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(model, players, action_mode='speech_and_action')
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    state = director.get_state()
    state['directive'] = {
        'beat_id': 'b1',
        'generated_at_turn': 0,
        'content': 'Thunder outside.',
        'status': 'pending',
    }
    director.set_state(state)
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ADJUDICATION_CONTEXT_MARK in p
    )
    complication = next(p for p in model.prompts if _COMPLICATION_MARK in p)
    merge = next(
        p for p in model.prompts if '## Chosen complication to weave in' in p
    )
    final = next(
        p
        for p in model.prompts
        if 'Output a JSON object with exactly two string fields' in p
    )
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )
    order = [adjudication, complication, merge, final, review]
    positions = [model.prompts.index(p) for p in order]
    self.assertEqual(positions, sorted(positions))
    # The directive fused by the note is committed as consumed with the
    # validated resolution of the same turn.
    self.assertEqual(director.get_state()['directive']['status'], 'consumed')


if __name__ == '__main__':
  absltest.main()
