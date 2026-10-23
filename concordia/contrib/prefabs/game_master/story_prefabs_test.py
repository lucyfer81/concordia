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
import re
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
from concordia.contrib.components.game_master import story_action_adjudication
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

_SPEECH = 'Quick brown fox jumps over a lazy dog'
# Valid single-field JSON for the final resolution step: the mock
# player's speech is assembled by the production step, not the model.
_STAGE_RESPONSE = json.dumps({'stage_directions': ''})

# Fingerprints of the core adjudication question (asked by the core
# `attempt_to_result` helper) and of the director-note fusion questions.
_ATTEMPT_QUESTION_MARK = 'What happens as a result of the attempted action?'
# Fingerprint of the final output step's single-field JSON instruction.
_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'
_ADJUDICATION_CONTEXT_MARK = '# Action adjudication (current turn only)'
_COMPLICATION_MARK = 'Suggest five plausible in-story complications'

# Fingerprints of the core repetition helper's questions and of the
# repetition adapter's independent-document context.
_REPETITION_MARK = (
    'Is the story traced out by the above list of events repetitive?'
)
_SUGGEST_MARK = 'Suggest five plausible events'
_MERGE_MARK = 'How does 2 modify'
_REPETITION_CONTEXT_MARK = '# Repetition check (current turn candidate)'
_HISTORY_MARK = '## Committed public events (oldest first)'
_CANDIDATE_MARK = '## Current candidate (not yet committed)'
_MERGE_GIVEN_RE = re.compile(r'Given:\n(.*?)\n\nHow does 2 modify', re.S)
_EVIDENCE_MARK = '## Resolution evidence (data)'

# A valid speech-and-action JSON contract the mock player returns.
_ACTION_INTENT = 'Alice walks to the window and checks the latch.'
_CONTRACT_RESPONSE = json.dumps(
    {'speech': _SPEECH, 'action_intent': _ACTION_INTENT}, ensure_ascii=False
)
_CORE_ATTEMPT_ANSWER = 'The latch is loose; the window stays shut.'
# The core helper's true return value: its causal-statement glue plus
# the attempt answer.
_CORE_RESULT = f'{_ACTION_INTENT} Because of that, {_CORE_ATTEMPT_ANSWER}'
# Valid single-field JSON for the speech-and-action chain: narrating
# the core-adjudicated result in the stage-directions track is required
# (the canonical event is assembled from speech and approved stage
# directions only), mirroring the example stub's evidence-driven draft.
_ACTION_STAGE_RESPONSE = json.dumps(
    {'stage_directions': _CORE_RESULT}, ensure_ascii=False
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


def _intervention_fact(intervention: dict[str, Any]) -> str:
  """Extracts only the new fact of an intervention candidate.

  A test instrument modeling legal narration, never production
  parsing (it mirrors the example stub's helper locally because
  contrib must not import examples): the recorded `candidate_after`
  may embed the original event (the speech line) as a numbered merge
  line, so the narration fact is what remains after removing the
  original-event portion and the stub's narrator track never repeats
  the dialogue.

  Args:
    intervention: the decoded intervention candidate.

  Returns:
    The new-fact text, or '' when the candidate carries no text.
  """
  candidate_after = intervention.get('candidate_after')
  if not isinstance(candidate_after, str) or not candidate_after:
    return ''
  candidate_before = intervention.get('candidate_before')
  fact = candidate_after
  if isinstance(candidate_before, str) and candidate_before:
    if fact.startswith(candidate_before):
      fact = fact[len(candidate_before) :]
    else:
      before_lines = {line.strip() for line in candidate_before.split('\n')}
      kept = []
      for line in fact.split('\n'):
        unnumbered = re.sub(r'^\d+\.\s*', '', line)
        if unnumbered.strip() not in before_lines:
          kept.append(unnumbered)
      fact = '\n'.join(kept)
  return fact.strip()


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
    model = _PromptRecordingModel(_STAGE_RESPONSE)
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
    # The final single-field output step must see the current beat and the
    # world constraints in its prompt, proving the resolution chain is
    # conditioned on the drama director and the constraints component.
    resolution_prompts = [p for p in model.prompts if _FINAL_OUTPUT_MARK in p]
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
        _STAGE_RESPONSE,
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
  core helper before the director note and the final stage-directions
  output,
  and the core's true return value reaches both later prompts through
  the resolution evidence. The default build stays pure speech.
  """

  def test_build_rejects_unknown_action_mode(self):
    players = [_build_character(name) for name in _PLAYERS]
    with self.assertRaises(ValueError):
      _build_game_master(
          _PromptRecordingModel(_STAGE_RESPONSE),
          players,
          action_mode='telepathy',
      )

  def test_default_build_is_speech_only_without_adjudication_calls(self):
    players = [_build_character(name) for name in _PLAYERS]
    model = _PromptRecordingModel(_STAGE_RESPONSE)
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
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
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
    self.assertIn(
        '## Original speech (verbatim, data)\n'
        + json.dumps(_SPEECH, ensure_ascii=False),
        adjudication,
    )
    self.assertIn(
        "## The speaker's own attempted action (adjudicate only this, data)\n"
        + json.dumps(_ACTION_INTENT, ensure_ascii=False),
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

    final_prompts = [p for p in model.prompts if _FINAL_OUTPUT_MARK in p]
    reviews = [
        p for p in model.prompts if '# Story resolution semantic review' in p
    ]
    self.assertLen(final_prompts, 1)
    self.assertLen(reviews, 1)
    # Chain order: adjudication, then the final stage-directions generation,
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
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
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
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )
    order = [adjudication, complication, merge, final, review]
    positions = [model.prompts.index(p) for p in order]
    self.assertEqual(positions, sorted(positions))
    # The directive fused by the note is committed as consumed with the
    # validated resolution of the same turn.
    self.assertEqual(director.get_state()['directive']['status'], 'consumed')

  _OLD_SHOWN_FACT = '周律师早在众人面前展示过遗嘱封套。'

  def test_adjudication_isolated_from_upstream_beat_and_conversation(self):
    # The isolated adjudication document carries no upstream beat
    # instruction, no conversation window and no world-constraints
    # component section, while its own conditioning — speaker, initial
    # public facts, GM-only boundaries, the shared authorization rules
    # and the DisplayEvents history — stays complete (design 4.1). The
    # final generation keeps its existing shared-document context.
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        player_names=list(_PLAYERS),
        public_facts=('Both players saw the sealed envelope.',),
    )
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    self.assertNotIn('(beat b1)', adjudication)
    self.assertNotIn('Conversation', adjudication)
    self.assertNotIn('\nWorld constraints', adjudication)
    self.assertIn('## Current speaker\nAlice', adjudication)
    self.assertIn('## Initial public facts', adjudication)
    self.assertIn('Both players saw the sealed envelope.', adjudication)
    self.assertIn('## Public history (data)', adjudication)
    self.assertIn(
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES, adjudication
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    self.assertIn('(beat b1)', final)

  def test_display_history_beyond_director_window_reaches_all_prompts(self):
    # Design 8.1: a committed display fact outside the director's
    # stored window but within DisplayEvents' retrieval range must
    # reach the actual adjudication, generation and review prompts —
    # via the marked history data section, not the window bullets —
    # and generation with review must read the identical history
    # frozen at step entry.
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    scoring_window = 8
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        player_names=list(_PLAYERS),
        scoring_window=scoring_window,
        public_facts=('Both players saw the sealed envelope.',),
    )
    # Seed the GM memory with 24 committed events: the shown-envelope
    # fact sits at position 1, outside the director's stored window
    # (the last max(scoring_window, 20) events) but well within
    # DisplayEvents' retrieval. Use the actual configured window.
    memory = gm.get_component('__memory__')
    for i in range(24):
      text = self._OLD_SHOWN_FACT if i == 1 else f'第{i}轮排练照常进行。'
      memory.add(f'[event] {text}')
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    state = dict(director.get_state())
    window = max(scoring_window, 20)
    state['recent_events'] = [
        f'第{i}轮排练照常进行。' for i in range(max(0, 24 - window), 24)
    ]
    director.set_state(state)
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )

    def read_history(prompt: str) -> str:
      mark = '## Public history (data)'
      index = prompt.index(mark) + len(mark)
      start = prompt.index('"', index)
      value, _ = json.JSONDecoder().raw_decode(prompt[start:])
      return value

    for prompt in (adjudication, final, review):
      self.assertIn(self._OLD_SHOWN_FACT, prompt)
      self.assertIn('## Public history (data)', prompt)
    # The old fact arrives via the history data section, never as a
    # director-window entry, and the director window itself stays.
    mark = '## Committed public events (oldest first)'
    window_data = adjudication.split(mark, 1)[1].lstrip()
    decoded_window, _ = json.JSONDecoder().raw_decode(window_data)
    self.assertNotIn(self._OLD_SHOWN_FACT, decoded_window)
    self.assertIn('第16轮排练照常进行。', adjudication)
    self.assertIn(self._OLD_SHOWN_FACT, read_history(final))
    self.assertEqual(read_history(adjudication), read_history(final))
    self.assertEqual(read_history(final), read_history(review))

  def test_history_getter_is_wired_in_build_not_params(self):
    # The public-history getter is bound at build time to the built
    # DisplayEvents instance; the serializable params keep no callable
    # and no new config field exists (design 4.1).
    params = {
        'name': 'story rules',
        'scene_dicts': _SCENE_DICTS,
        'beat_dicts': _BEAT_DICTS,
        'public_facts': ('Both players saw the sealed envelope.',),
    }
    prefab = story_dramaturgic.GameMaster(params=params)
    prefab.entities = [_build_character(name) for name in _PLAYERS]
    _build_prefab(prefab)
    json.dumps(prefab.params)
    self.assertNotIn('public_history', prefab.params)


class NpcResponseRulesWiringTest(absltest.TestCase):
  """Design 5.2: rules validated at build and forwarded to both factories."""

  _RULE = (
      'Neighbor: after the whitelisted knock, may publicly acknowledge'
      ' a through-the-door announcement; no secrets, no certification,'
      " no consent on players' behalf, no evidence."
  )

  def test_bad_structure_rejected_at_build_before_any_model_call(self):
    players = [_build_character(name) for name in _PLAYERS]
    for bad in (('   ',), (42,), '一条被误写成裸字符串的邻居规则'):
      with self.subTest(bad=bad):
        prefab = story_dramaturgic.GameMaster(
            params={
                'scene_dicts': _SCENE_DICTS,
                'beat_dicts': _BEAT_DICTS,
                'action_mode': 'speech_and_action',
                'npc_response_rules': bad,
            }
        )
        prefab.entities = players
        with self.assertRaisesRegex(ValueError, 'npc_response_rules'):
          prefab.build(
              model=mock_model.MockModel(),
              memory_bank=associative_memory.AssociativeMemoryBank(
                  sentence_embedder=_embedder
              ),
          )

  def test_rules_reach_adjudication_generation_and_review_prompts(self):
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        npc_response_rules=(self._RULE,),
    )
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )
    for prompt in (adjudication, final, review):
      self.assertIn(self._RULE, prompt)
      self.assertIn(
          story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS, prompt
      )
    # Compare the GM model's call counts per task type against an
    # independent control run with the same config and empty rules, not
    # only the totals; the characters use their own models, so their
    # calls never touch this model. The control must build fresh
    # players, model and GM — no reuse of committed component state.
    # With equal totals and exactly one call of each of the three task
    # types, there is no room for any independent NPC call; this wiring
    # probe does not infer from the scripted reviewer whether empty
    # rules allow an NPC response.
    control_players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    control_model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    control_gm = _build_game_master(
        control_model,
        control_players,
        action_mode='speech_and_action',
        npc_response_rules=(),
    )
    sequential.Sequential().run_loop(
        game_masters=[control_gm], entities=control_players, max_steps=1
    )
    self.assertEqual(len(model.prompts), len(control_model.prompts))

    def task_type_counts(prompts):
      return (
          sum(1 for p in prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)),
          sum(1 for p in prompts if _FINAL_OUTPUT_MARK in p),
          sum(1 for p in prompts if '# Story resolution semantic review' in p),
      )

    counts = task_type_counts(model.prompts)
    self.assertEqual(counts, (1, 1, 1))
    self.assertEqual(counts, task_type_counts(control_model.prompts))

  def test_default_empty_rules_keep_old_boundary_and_json_safe_params(self):
    players = [_build_character(name) for name in _PLAYERS]
    prefab = story_dramaturgic.GameMaster(
        params={
            'scene_dicts': _SCENE_DICTS,
            'beat_dicts': _BEAT_DICTS,
        }
    )
    prefab.entities = players
    _build_prefab(prefab)
    json.dumps(prefab.params)
    self.assertEqual(prefab.params.get('npc_response_rules', ()), ())


class StoryRepetitionWiringTest(absltest.TestCase):
  """The repetition-push step wired into the real RESOLVE chain.

  The director state is restored to the start of turn three (the
  interval's check turn) before one real engine step, so the chain
  order — director note (identity without a directive), repetition
  push, final stage-directions output, review — is observable on the actual
  prompts, and the committed turn records the executed intervention.
  """

  _PUSH_EVENT = 'A courier knocks with an unexpected letter.'
  _MERGE_SUFFIX = 'Thunder startles the candles out.'

  class _RepetitionModel(_PromptRecordingModel):
    """Routes the real core repetition and final prompts."""

    def __init__(self):
      super().__init__(_STAGE_RESPONSE)
      self.choice_prompts: list[str] = []

    def sample_choice(self, prompt, responses, *, seed=None):
      del seed
      self.choice_prompts.append(prompt)
      block = _last_block(prompt)
      if _REPETITION_MARK in block:
        wanted = 'Yes'
      else:
        wanted = 'No'
      pairs = re.findall(r'^  \(([^)]+)\) (.+)$', block, re.M)
      letter = next(k for k, option in pairs if option.strip() == wanted)
      return responses.index(letter), letter, {}

    def sample_text(self, prompt, **kwargs):
      del kwargs
      self.prompts.append(prompt)
      if '# Story resolution semantic review' in prompt:
        return self.review
      block = _last_block(prompt)
      if _SUGGEST_MARK in block:
        # One suggestion keeps the core's random.choice deterministic.
        return StoryRepetitionWiringTest._PUSH_EVENT
      if _MERGE_MARK in block:
        given = _MERGE_GIVEN_RE.search(block)
        assert given is not None
        return f'{given.group(1)}\n{StoryRepetitionWiringTest._MERGE_SUFFIX}'
      if _FINAL_OUTPUT_MARK in block:
        return self._evidence_stage_directions(prompt)
      return self._response

    def _evidence_stage_directions(self, prompt: str) -> str:
      evidence = _read_evidence(prompt)
      intervention = evidence.get('intervention')
      stage = _intervention_fact(intervention) if intervention else ''
      return json.dumps({'stage_directions': stage}, ensure_ascii=False)

  def _build_with_repetition(self, model, **overrides):
    params = {
        'scene_dicts': _SCENE_DICTS,
        'beat_dicts': _BEAT_DICTS,
        'world_constraints': _WORLD_CONSTRAINTS,
        'immutable_facts': _IMMUTABLE_FACTS,
        'player_names': list(_PLAYERS),
        'eval_interval': 0,
        'feedback_enabled': True,
        'repetition_push_enabled': True,
        'repetition_check_interval': 3,
    }
    params.update(overrides)
    prefab = story_dramaturgic.GameMaster(params=params)
    prefab.entities = [_build_character(name) for name in _PLAYERS]
    gm = prefab.build(
        model=model,
        memory_bank=associative_memory.AssociativeMemoryBank(
            sentence_embedder=_embedder
        ),
    )
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    state = dict(director.get_state())
    state['story_turn'] = 2
    state['recent_events'] = ['Alice: one', 'Alice: two']
    director.set_state(state)
    return gm, director

  def test_repetition_sits_between_director_note_and_final_output(self):
    model = self._RepetitionModel()
    gm, director = self._build_with_repetition(model)
    players = [_build_character(name) for name in _PLAYERS]
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    # The core repetition judgment ran once on its own document.
    repetition = [p for p in model.choice_prompts if _REPETITION_MARK in p]
    self.assertLen(repetition, 1)
    self.assertIn(_REPETITION_CONTEXT_MARK, repetition[0])
    self.assertIn(_HISTORY_MARK, repetition[0])
    self.assertIn('- Alice: one', repetition[0])
    self.assertIn(_CANDIDATE_MARK, repetition[0])
    suggestion = next(p for p in model.prompts if _SUGGEST_MARK in p)
    merge = next(p for p in model.prompts if _MERGE_MARK in p)
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(p for p in model.prompts if '# Story resolution' in p)
    # The repetition questions precede the final generation; the
    # director note contributed nothing (no pending directive).
    self.assertLess(model.prompts.index(suggestion), model.prompts.index(final))
    self.assertFalse(any(_COMPLICATION_MARK in p for p in model.prompts))
    # The merged candidate traveled through the evidence into the final
    # generation and the review, and the committed turn records the
    # executed repetition intervention with the shared baseline.
    merged = f'1. Alice: {_SPEECH}\n2. {self._PUSH_EVENT}\n{self._MERGE_SUFFIX}'
    for prompt in (final, review):
      evidence = _read_evidence(prompt)
      self.assertEqual(evidence['intervention']['reason'], 'repetition')
      self.assertEqual(evidence['intervention']['candidate_after'], merged)
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(state['repetition_execution_count'], 1)
    self.assertEqual(
        state['last_intervention'], {'turn_id': 3, 'reason': 'repetition'}
    )
    turn = director.get_last_story_turn()
    self.assertTrue(turn['director_diagnostics']['repetition']['executed'])
    self.assertIn(self._PUSH_EVENT, turn['stage_directions'])
    # Review note 2: the stub extracts only the candidate's new fact, so
    # the narrator track never repeats the dialogue and the canonical
    # event carries the speech exactly once — a violation no
    # fixed-approving reviewer could catch.
    self.assertNotIn(_SPEECH, turn['stage_directions'])
    self.assertEqual(turn['resolved_event'].count(f'Alice: {_SPEECH}'), 1)

  def test_disabled_switch_runs_no_repetition_calls(self):
    model = self._RepetitionModel()
    gm, director = self._build_with_repetition(
        model, repetition_push_enabled=False
    )
    players = [_build_character(name) for name in _PLAYERS]
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    self.assertFalse(any(_REPETITION_MARK in p for p in model.choice_prompts))
    self.assertFalse(any(_SUGGEST_MARK in p for p in model.prompts))
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 0)
    self.assertIsNone(state['last_intervention'])

  def test_non_positive_interval_rejected_at_build(self):
    players = [_build_character(name) for name in _PLAYERS]
    for interval in (0, -1):
      with self.subTest(interval=interval):
        with self.assertRaises(ValueError):
          _build_game_master(
              mock_model.MockModel(),
              players,
              feedback_enabled=True,
              repetition_push_enabled=True,
              repetition_check_interval=interval,
          )


if __name__ == '__main__':
  absltest.main()
