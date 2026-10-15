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

"""Tests for the boundary-preserving observation queue encoding."""

import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import make_observation as make_observation_lib
from concordia.contrib.components.game_master import observation_queue
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component


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


def _observation_spec(player_name: str) -> entity_lib.ActionSpec:
  return entity_lib.ActionSpec(
      call_to_action=(
          make_observation_lib.DEFAULT_CALL_TO_MAKE_OBSERVATION.format(
              name=player_name
          )
      ),
      output_type=entity_lib.OutputType.MAKE_OBSERVATION,
  )


def _make_gm(player_names):
  component = observation_queue.BoundaryEncodingMakeObservation(
      model=mock_model.MockModel('unused'),
      player_names=player_names,
      components=(),
      allow_llm_fallback=False,
  )
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_ObservingAct(),
      context_components={
          make_observation_lib.DEFAULT_MAKE_OBSERVATION_COMPONENT_KEY: (
              component
          ),
      },
  )
  return component, gm


class CodecTest(unittest.TestCase):

  def test_round_trips_plain_item_unchanged(self):
    item = 'Event: Bob 放下信封。\n'
    self.assertEqual(observation_queue.encode_queue_item(item), item)

  def test_round_trips_newline_runs_of_every_length(self):
    for run in range(1, 9):
      item = f'Event: 第一段。{"\n" * run}第二段。\n'
      observation = (
          observation_queue.OBSERVATION_SEPARATOR
          + observation_queue.encode_queue_item(item)
          + observation_queue.OBSERVATION_SEPARATOR
      )
      self.assertEqual(
          observation_queue.decode_observation(observation),
          [item.strip()],
          msg=f'run of {run} newlines',
      )

  def test_paragraph_starting_with_digits_round_trips_verbatim(self):
    # A digit opening the paragraph after a newline run must stay part of
    # the content: without an end terminator on the length field, the
    # greedy digit match absorbs it into the newline count.
    for lead in ('0', '3', '2026', '$100', '12.5万', '1、2、3'):
      item = f'Event: 第一段。\n\n\n{lead}，Bob 承认伪造。\n'
      encoded = observation_queue.encode_queue_item(item)
      self.assertNotIn('\n\n\n', encoded, msg=lead)
      observation = (
          observation_queue.OBSERVATION_SEPARATOR
          + encoded
          + observation_queue.OBSERVATION_SEPARATOR
      )
      decoded = observation_queue.decode_observation(observation)
      self.assertEqual(decoded, [item.strip()], msg=lead)
      self.assertIn(lead, decoded[0], msg=lead)
      self.assertEqual(decoded[0].count('\n'), 3, msg=lead)

  def test_round_trips_literal_escape_characters(self):
    item = f'Event: a{observation_queue._ESCAPE}3b\n\n\n尾段。\n'  # pylint: disable=protected-access
    encoded = observation_queue.encode_queue_item(item)
    self.assertNotIn('\n\n\n', encoded)
    observation = (
        observation_queue.OBSERVATION_SEPARATOR
        + encoded
        + observation_queue.OBSERVATION_SEPARATOR
    )
    self.assertEqual(
        observation_queue.decode_observation(observation), [item.strip()]
    )

  def test_encoded_item_never_contains_the_separator(self):
    items = (
        'Event: a\n\n\n\n\n\nb\n',
        'Event: \n\n\n\nEvent: 不是新事件\n',
        'x' * 5 + '\n\n\n',
    )
    for item in items:
      self.assertNotIn(
          observation_queue.OBSERVATION_SEPARATOR,
          observation_queue.encode_queue_item(item),
      )


class EncodedQueueDeliveryTest(unittest.TestCase):
  """Delivers encoded items through a real MakeObservation cycle."""

  def test_multi_paragraph_event_keeps_boundaries_and_content(self):
    component, gm = _make_gm(['Alice'])
    first = 'Event: Bob 放下信封。\n\n\nBob 承认信封里的信是自己伪造的。\n'
    second = 'Event: Alice 沉默了很久。\n'
    background = '背景：窗外下着雨。\n'
    for item in (background, first, second):
      component.add_to_queue('Alice', item)
    observation = gm.act(_observation_spec('Alice'))

    self.assertEqual(
        observation_queue.decode_observation(observation),
        [item.strip() for item in (background, first, second)],
    )

  def test_queue_state_round_trip_keeps_encoded_items(self):
    component, _ = _make_gm(['Alice'])
    item = 'Event: Bob 放下信封。\n\n\nBob 承认信是伪造的。\n'
    component.add_to_queue('Alice', item)
    state = component.get_state()

    restored, restored_gm = _make_gm(['Alice'])
    restored.set_state(state)
    restored.add_to_queue('Alice', 'Event: 第二条。\n')
    observation = restored_gm.act(_observation_spec('Alice'))
    self.assertEqual(
        observation_queue.decode_observation(observation),
        [item.strip(), 'Event: 第二条。'],
    )

  def test_digit_leading_events_survive_delivery_and_state_round_trip(self):
    # Two multi-paragraph adjudicated events whose following paragraphs
    # open with digits (a year and a numbering), delivered through a real
    # MakeObservation cycle and restored from the saved queue state: the
    # decoded items must match the originals verbatim, one item each.
    component, gm = _make_gm(['Alice'])
    first = (
        'Event: Bob 放下信封。\n\n\n2026年，Bob 承认信封里的信是自己伪造的。\n'
    )
    second = 'Event: 编号复核。\n\n\n3 号文件与 12、13 号记录对不上。\n'
    component.add_to_queue('Alice', first)
    component.add_to_queue('Alice', second)
    state = component.get_state()

    restored, restored_gm = _make_gm(['Alice'])
    restored.set_state(state)
    observation = restored_gm.act(_observation_spec('Alice'))
    decoded = observation_queue.decode_observation(observation)
    self.assertEqual(decoded, [first.strip(), second.strip()])
    self.assertIn('2026年', decoded[0])
    self.assertIn('3 号文件', decoded[1])


if __name__ == '__main__':
  unittest.main()
