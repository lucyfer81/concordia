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

"""Tests for RelationshipsToOthers using the real entity lifecycle.

Adjudicated events are delivered through a real `MakeObservation`
component driven by a real game-master entity, so the observation text
under test is exactly the queue output players receive in a story run:
blocks joined by the triple-newline separator. The event prefix is the
real `EventResolution` pre-act label, the same string the resolution
chain queues for the players. The game master uses the story prefabs'
boundary-encoding `MakeObservation` subclass, so events that themselves
contain the triple-newline separator survive the join with their
content and boundaries intact.
"""

import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import make_observation as make_observation_lib
from concordia.contrib.components.agent import relationships
from concordia.contrib.components.game_master import observation_queue
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

# Key of the component under test inside the player entity.
_RELATIONSHIPS_KEY = 'relationships'

_PLAYER_ACTION_SPEC = entity_lib.DEFAULT_ACTION_SPEC


class _RelationshipModel(mock_model.MockModel):
  """Records every prompt and returns a fixed relationship summary."""

  def __init__(self, response: str = 'Bob: warm, high trust, shared a meal.'):
    super().__init__(response)
    self.text_prompts: list[str] = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    return self._response


class _PlayerAct(entity_component.ActingComponent):
  """Returns a fixed action, keeping the real lifecycle phases."""

  def get_action_attempt(self, contexts, action_spec):
    del contexts, action_spec
    return 'Alice waits.'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _ObservingAct(entity_component.ActingComponent):
  """Returns the MakeObservation context, like SwitchAct's observation path."""

  def get_action_attempt(self, contexts, action_spec):
    del action_spec
    return str(
        contexts[make_observation_lib.DEFAULT_MAKE_OBSERVATION_COMPONENT_KEY]
    )

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _event(text: str) -> str:
  """Builds the string the resolution chain queues for the players."""
  return f'{event_resolution_lib.DEFAULT_RESOLUTION_PRE_ACT_LABEL}: {text}\n'


def _observation_spec(player_name: str) -> entity_lib.ActionSpec:
  return entity_lib.ActionSpec(
      call_to_action=(
          make_observation_lib.DEFAULT_CALL_TO_MAKE_OBSERVATION.format(
              name=player_name
          )
      ),
      output_type=entity_lib.OutputType.MAKE_OBSERVATION,
  )


def _make_observation_gm(player_names):
  """Builds a real game master that delivers queued observations."""
  make_observation_component = (
      observation_queue.BoundaryEncodingMakeObservation(
          model=mock_model.MockModel('unused'),
          player_names=player_names,
          components=(),
          allow_llm_fallback=False,
      )
  )
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_ObservingAct(),
      context_components={
          make_observation_lib.DEFAULT_MAKE_OBSERVATION_COMPONENT_KEY: (
              make_observation_component
          ),
      },
  )
  return make_observation_component, gm


def _make_player(model, player_name, other_player_names, **component_params):
  component = relationships.RelationshipsToOthers(
      model=model,
      other_player_names=other_player_names,
      **component_params,
  )
  player = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name=player_name,
      act_component=_PlayerAct(),
      context_components={_RELATIONSHIPS_KEY: component},
  )
  return component, player


def _deliver(make_observation_component, gm, player, queued_text):
  """Queues one item and delivers it through a real GM observation cycle."""
  make_observation_component.add_to_queue(player.name, queued_text)
  player.observe(gm.act(_observation_spec(player.name)))


class RefreshSemanticsTest(unittest.TestCase):

  def test_first_act_refreshes_once(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    self.assertEqual(len(model.text_prompts), 0)
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 1)
    self.assertEqual(component.get_state()['last_refresh'], 0)

  def test_management_updates_and_background_observes_never_refresh(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 1)

    # Management acts and bare updates do not refresh.
    for _ in range(3):
      player.act(_PLAYER_ACTION_SPEC)
    for _ in range(3):
      component.update()

    # Ordinary background observations carry no reserved event prefix.
    _deliver(
        make_observation_component,
        gm,
        player,
        'The morning light filled the kitchen.',
    )
    player.act(_PLAYER_ACTION_SPEC)

    self.assertEqual(len(model.text_prompts), 1)
    state = component.get_state()
    self.assertEqual(state['observed_count'], 0)
    self.assertEqual(state['recent_events'], [])

  def test_two_events_reuse_cache_third_refreshes(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])
    player.act(_PLAYER_ACTION_SPEC)

    _deliver(
        make_observation_component, gm, player, _event('Alice found a key.')
    )
    player.act(_PLAYER_ACTION_SPEC)
    _deliver(
        make_observation_component, gm, player, _event('Bob waved goodbye.')
    )
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 1)

    _deliver(
        make_observation_component, gm, player, _event('Cleo locked the door.')
    )
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 2)
    state = component.get_state()
    self.assertEqual(state['observed_count'], 3)
    self.assertEqual(state['last_refresh'], 3)
    self.assertEqual(state['cache'], model._response)


class EventBlockCountingTest(unittest.TestCase):

  def test_batch_with_two_event_blocks_counts_two(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    make_observation_component.add_to_queue(
        'Alice', _event('Alice found a key.')
    )
    make_observation_component.add_to_queue(
        'Alice', _event('Bob waved goodbye.')
    )
    observation = gm.act(_observation_spec('Alice'))
    # One real MakeObservation batch carrying two event blocks.
    self.assertEqual(observation.count('Event: '), 2)
    player.observe(observation)

    state = component.get_state()
    self.assertEqual(state['observed_count'], 2)
    self.assertEqual(
        state['recent_events'],
        ['Event: Alice found a key.', 'Event: Bob waved goodbye.'],
    )

  def test_background_block_sharing_batch_with_event_counts_once(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    make_observation_component.add_to_queue(
        'Alice', 'The morning light filled the kitchen.'
    )
    make_observation_component.add_to_queue(
        'Alice', _event('Alice found a key.')
    )
    player.observe(gm.act(_observation_spec('Alice')))

    state = component.get_state()
    self.assertEqual(state['observed_count'], 1)
    self.assertEqual(state['recent_events'], ['Event: Alice found a key.'])


class MultiParagraphEventTest(unittest.TestCase):
  """Acceptance F2: events containing the queue separator stay whole."""

  def test_one_event_with_triple_newline_counts_once_with_full_content(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    _deliver(
        make_observation_component,
        gm,
        player,
        _event('Bob 放下信封。\n\n\nBob 承认信封里的信是自己伪造的。'),
    )
    state = component.get_state()
    self.assertEqual(state['observed_count'], 1)
    self.assertEqual(
        state['recent_events'],
        ['Event: Bob 放下信封。\n\n\nBob 承认信封里的信是自己伪造的。'],
    )

    # The refresh prompt carries the full event, admission included.
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 1)
    self.assertIn('Bob 放下信封。', model.text_prompts[0])
    self.assertIn('Bob 承认信封里的信是自己伪造的。', model.text_prompts[0])

  def test_inner_paragraph_starting_with_event_prefix_still_one_event(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    _deliver(
        make_observation_component,
        gm,
        player,
        _event('Bob 清了清嗓子。\n\n\nEvent: Bob 承认信是伪造的。'),
    )
    state = component.get_state()
    self.assertEqual(state['observed_count'], 1)
    self.assertEqual(len(state['recent_events']), 1)

  def test_two_events_delivered_in_one_batch_count_two(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    make_observation_component.add_to_queue(
        'Alice', _event('Bob 放下信封。\n\n\nBob 承认信是伪造的。')
    )
    make_observation_component.add_to_queue(
        'Alice', _event('Cleo 转身离开。\n\n\nCleo 摔上了门。')
    )
    player.observe(gm.act(_observation_spec('Alice')))

    state = component.get_state()
    self.assertEqual(state['observed_count'], 2)
    self.assertEqual(
        state['recent_events'],
        [
            'Event: Bob 放下信封。\n\n\nBob 承认信是伪造的。',
            'Event: Cleo 转身离开。\n\n\nCleo 摔上了门。',
        ],
    )

  def test_background_and_premise_mixed_in_batch_count_events_only(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    make_observation_component.add_to_queue(
        'Alice', '堂外的雷雨下了一整夜。\n\n\n雨势直到清晨才停。'
    )
    make_observation_component.add_to_queue(
        'Alice', _event('Bob 放下信封。\n\n\nBob 承认信是伪造的。')
    )
    make_observation_component.add_to_queue(
        'Alice', '开场：众人聚在老宅堂屋。\n'
    )
    player.observe(gm.act(_observation_spec('Alice')))

    state = component.get_state()
    self.assertEqual(state['observed_count'], 1)
    self.assertEqual(
        state['recent_events'],
        ['Event: Bob 放下信封。\n\n\nBob 承认信是伪造的。'],
    )

  def test_state_round_trip_preserves_counts_and_full_content(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])

    _deliver(
        make_observation_component,
        gm,
        player,
        _event('Bob 放下信封。\n\n\nBob 承认信是伪造的。'),
    )
    state = component.get_state()

    restored_model = _RelationshipModel()
    restored_component, restored_player = _make_player(
        restored_model, 'Alice', ['Bob', 'Cleo']
    )
    restored_component.set_state(state)
    restored_state = restored_component.get_state()
    self.assertEqual(restored_state['observed_count'], 1)
    self.assertEqual(
        restored_state['recent_events'],
        ['Event: Bob 放下信封。\n\n\nBob 承认信是伪造的。'],
    )
    # The restored window still refreshes on the original schedule: one
    # more event reaches the interval of three and the full stored
    # content conditions the next prompt.
    _deliver(make_observation_component, gm, restored_player, _event('二。'))
    _deliver(make_observation_component, gm, restored_player, _event('三。'))
    restored_player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(restored_model.text_prompts), 1)
    self.assertIn('Bob 承认信是伪造的。', restored_model.text_prompts[0])


class StateRoundTripTest(unittest.TestCase):

  def test_restored_state_refreshes_at_original_threshold(self):
    model = _RelationshipModel()
    component, player = _make_player(model, 'Alice', ['Bob', 'Cleo'])
    make_observation_component, gm = _make_observation_gm(['Alice'])
    player.act(_PLAYER_ACTION_SPEC)
    for text in ('one.', 'two.', 'three.'):
      _deliver(make_observation_component, gm, player, _event(text))
    player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(model.text_prompts), 2)
    state = component.get_state()

    restored_model = _RelationshipModel()
    restored_component, restored_player = _make_player(
        restored_model, 'Alice', ['Bob', 'Cleo']
    )
    restored_component.set_state(state)
    restored_player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(restored_model.text_prompts), 0)

    _deliver(make_observation_component, gm, restored_player, _event('four.'))
    _deliver(make_observation_component, gm, restored_player, _event('five.'))
    restored_player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(restored_model.text_prompts), 0)

    _deliver(make_observation_component, gm, restored_player, _event('six.'))
    restored_player.act(_PLAYER_ACTION_SPEC)
    self.assertEqual(len(restored_model.text_prompts), 1)
    self.assertEqual(
        restored_component.get_state()['last_refresh'],
        state['observed_count'] + 3,
    )


class AgentIsolationTest(unittest.TestCase):

  def test_windows_and_prompts_contain_only_own_events(self):
    alice_model = _RelationshipModel()
    bob_model = _RelationshipModel()
    alice_component, alice = _make_player(alice_model, 'Alice', ['Bob'])
    bob_component, bob = _make_player(bob_model, 'Bob', ['Alice'])
    make_observation_component, gm = _make_observation_gm(['Alice', 'Bob'])

    _deliver(
        make_observation_component, gm, alice, _event('Alice found a key.')
    )
    _deliver(
        make_observation_component,
        gm,
        bob,
        'The morning light filled the kitchen.',
    )
    alice.act(_PLAYER_ACTION_SPEC)
    bob.act(_PLAYER_ACTION_SPEC)

    self.assertEqual(len(alice_model.text_prompts), 1)
    self.assertEqual(len(bob_model.text_prompts), 1)
    alice_prompt = alice_model.text_prompts[0]
    bob_prompt = bob_model.text_prompts[0]

    # P6 structure (plan appendix B) on the refreshing side.
    self.assertIn(
        '# Observed story events of Alice (oldest first)', alice_prompt
    )
    self.assertIn('## People present', alice_prompt)
    self.assertIn('- Bob', alice_prompt)
    self.assertIn('## Output', alice_prompt)
    self.assertIn('warmth', alice_prompt)
    self.assertIn('trust', alice_prompt)
    self.assertIn('Alice found a key.', alice_prompt)

    # The other agent's prompt carries only its own observations.
    self.assertIn('# Observed story events of Bob (oldest first)', bob_prompt)
    self.assertIn('## People present', bob_prompt)
    self.assertIn('- Alice', bob_prompt)
    self.assertNotIn('found a key', bob_prompt)

    self.assertEqual(
        alice_component.get_state()['recent_events'],
        ['Event: Alice found a key.'],
    )
    self.assertEqual(bob_component.get_state()['recent_events'], [])


if __name__ == '__main__':
  unittest.main()
