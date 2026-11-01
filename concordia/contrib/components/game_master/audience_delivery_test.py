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

"""Tests for the audience_delivery component.

The delivery adapter is the story-specific replacement of the generic
``SendEventToRelevantPlayers`` sender: every check (contract version,
field presence and types, actor binding, engine-echo match, recipient
legality) must complete before the first queue write, so each failure
test asserts both the ``ValueError`` and a zero-call ``add_to_queue``.
"""

import inspect
from unittest import mock

from absl.testing import absltest
from concordia.contrib.components.game_master import audience_delivery
from concordia.contrib.components.game_master import drama_director
from concordia.typing import entity as entity_lib

_ACTOR = '甲'
_OTHERS = ('乙', '丙')
_PLAYERS = (_ACTOR, *_OTHERS, '场外角色')

_FULL_EVENT = '甲: 我去查底档。\n甲在档案里看到了落款日期。'
_PUBLIC_EVENT = '甲独自翻看了片刻档案。'


def _echo(resolved_event: str) -> str:
  """Returns the engine's canonical RESOLVE return value of an event."""
  return audience_delivery.engine_echo_of(resolved_event)


def _delivery_data(**overrides):
  """Returns one valid two-view delivery payload with overrides."""
  data = {
      'audience_delivery_version': drama_director.AUDIENCE_DELIVERY_VERSION,
      'actor': _ACTOR,
      'resolved_event': _FULL_EVENT,
      'stage_directions': '甲在档案里看到了落款日期。',
      'public_resolved_event': _PUBLIC_EVENT,
      'public_stage_directions': '甲独自翻看了片刻档案。',
  }
  data.update(overrides)
  return data


def _make_component(
    data,
    *,
    participants=(_ACTOR, '乙', '丙'),
    player_names=_PLAYERS,
    logging_channel=None,
):
  """Builds one wired-up component with fake entity and siblings.

  Args:
    data: the payload ``get_validated_delivery_data`` returns, or None.
    participants: names the player_filter callback reports.
    player_names: the configured cast.
    logging_channel: optional logging-channel mock.

  Returns:
    The component plus the mocked make-observation and director
    components, with ``add_to_queue`` calls recorded by MagicMock.
  """
  make_observation = mock.MagicMock()
  director = mock.MagicMock(spec=drama_director.DramaDirector)
  director.get_validated_delivery_data.return_value = data
  entity = mock.MagicMock()
  entity.get_component.side_effect = lambda key, type_: {
      '__make_observation__': make_observation,
      drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY: director,
  }[key]
  component = audience_delivery.AudienceDelivery(
      player_names=player_names,
      make_observation_component_key='__make_observation__',
      player_filter=lambda: participants,
      drama_director_component_key=(
          drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY
      ),
  )
  component.set_entity(entity)
  if logging_channel is not None:
    component.set_logging_channel(logging_channel)
  return component, make_observation


def _resolve(component, action_attempt):
  """Runs one RESOLVE act cycle and returns the post_act result."""
  component.pre_act(
      entity_lib.ActionSpec(
          call_to_action='test',
          output_type=entity_lib.OutputType.RESOLVE,
      )
  )
  return component.post_act(action_attempt)


def _queue_writes(make_observation):
  """Returns the recorded queue writes as a list of (name, event) pairs."""
  return [
      (call.args[0], call.args[1])
      for call in make_observation.add_to_queue.call_args_list
  ]


class AudienceDeliveryTest(absltest.TestCase):
  """Unit tests of the two-view delivery adapter."""

  def test_actor_gets_full_others_get_public(self):
    component, make_observation = _make_component(_delivery_data())
    self.assertEqual(_resolve(component, _echo(_FULL_EVENT)), '')
    self.assertEqual(
        _queue_writes(make_observation),
        [(_ACTOR, _FULL_EVENT), ('乙', _PUBLIC_EVENT), ('丙', _PUBLIC_EVENT)],
    )

  def test_empty_public_event_skips_others_but_actor_still_gets_full(self):
    component, make_observation = _make_component(
        _delivery_data(public_resolved_event='')
    )
    self.assertEqual(_resolve(component, _echo(_FULL_EVENT)), '')
    self.assertEqual(_queue_writes(make_observation), [(_ACTOR, _FULL_EVENT)])

  def test_off_scene_players_receive_nothing(self):
    component, make_observation = _make_component(
        _delivery_data(), participants=(_ACTOR, '乙')
    )
    self.assertEqual(_resolve(component, _echo(_FULL_EVENT)), '')
    written = dict(_queue_writes(make_observation))
    self.assertEqual(set(written), {_ACTOR, '乙'})
    self.assertNotIn('场外角色', written)
    self.assertNotIn('丙', written)

  def test_mismatch_fails_before_any_queue_write(self):
    cases = {
        'engine_echo_differs': (
            _delivery_data(),
            _echo('甲: 别的字符串。'),
        ),
        'no_validated_data': (None, _echo(_FULL_EVENT)),
        'missing_field': (
            {
                key: value
                for key, value in _delivery_data().items()
                if key != 'public_resolved_event'
            },
            _echo(_FULL_EVENT),
        ),
        'wrong_version': (
            _delivery_data(audience_delivery_version=2),
            _echo(_FULL_EVENT),
        ),
        'actor_not_configured': (
            _delivery_data(actor='陌生人'),
            _echo(_FULL_EVENT),
        ),
        'actor_not_in_scene': (
            _delivery_data(),
            _echo(_FULL_EVENT),
        ),
    }
    for name, (data, attempt) in cases.items():
      with self.subTest(name):
        participants = [_ACTOR, '乙', '丙']
        if name == 'actor_not_in_scene':
          participants = ['乙', '丙']
        component, make_observation = _make_component(
            data, participants=participants
        )
        with self.assertRaises(ValueError):
          _resolve(component, attempt)
        make_observation.add_to_queue.assert_not_called()

  def test_non_string_field_fails_before_any_queue_write(self):
    component, make_observation = _make_component(
        _delivery_data(resolved_event=123)
    )
    with self.assertRaises(ValueError):
      _resolve(component, _echo(_FULL_EVENT))
    make_observation.add_to_queue.assert_not_called()

  def test_invalid_recipients_fail_before_any_queue_write(self):
    cases = {
        'unconfigured_participant': [_ACTOR, '乙', '路人'],
        'duplicate_participant': [_ACTOR, '乙', '乙'],
        'all_broadcast_alias': [_ACTOR, '乙', 'all'],
        'all_broadcast_alias_case_folded': [_ACTOR, '乙', ' ALL '],
    }
    for name, participants in cases.items():
      with self.subTest(name):
        component, make_observation = _make_component(
            _delivery_data(), participants=participants
        )
        with self.assertRaises(ValueError):
          _resolve(component, _echo(_FULL_EVENT))
        make_observation.add_to_queue.assert_not_called()

  def test_no_model_and_no_string_replace_dedup(self):
    # The constructor signature carries no model parameter at all, and
    # a turn whose view repeats the previous turn's text verbatim is
    # still delivered whole: no string-replace dedup of the kind the
    # generic sender performed.
    parameters = inspect.signature(
        audience_delivery.AudienceDelivery.__init__
    ).parameters
    self.assertNotIn('model', parameters)
    component, make_observation = _make_component(_delivery_data())
    self.assertEqual(_resolve(component, _echo(_FULL_EVENT)), '')
    second_event = _FULL_EVENT + '\n甲又看了一遍日期。'
    second = _delivery_data(resolved_event=second_event)
    component.pre_act(
        entity_lib.ActionSpec(
            call_to_action='test',
            output_type=entity_lib.OutputType.RESOLVE,
        )
    )
    director = component.get_entity().get_component(
        drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director.DramaDirector,
    )
    director.get_validated_delivery_data.return_value = second
    component.post_act(_echo(second_event))
    # The second view repeats the first turn's text verbatim and is
    # still delivered whole — no string-replace dedup happened.
    self.assertIn((_ACTOR, second_event), _queue_writes(make_observation))
    self.assertIn(('乙', _PUBLIC_EVENT), _queue_writes(make_observation))

  def test_non_resolve_post_act_does_not_deliver(self):
    component, make_observation = _make_component(_delivery_data())
    component.pre_act(
        entity_lib.ActionSpec(
            call_to_action='test',
            output_type=entity_lib.OutputType.FREE,
        )
    )
    self.assertEqual(component.post_act(_echo(_FULL_EVENT)), '')
    make_observation.add_to_queue.assert_not_called()

  def test_logging_channel_records_per_recipient_views(self):
    logging_channel = mock.MagicMock()
    component, _unused = _make_component(
        _delivery_data(), logging_channel=logging_channel
    )
    _resolve(component, _echo(_FULL_EVENT))
    logging_channel.assert_called_once()
    entry = logging_channel.call_args.args[0]
    self.assertEqual(entry['Key'], 'Audience delivery')
    recipients = entry['Recipients']
    self.assertEqual(recipients[_ACTOR]['view'], 'full')
    self.assertEqual(recipients['乙']['view'], 'public')
    self.assertEqual(recipients['丙']['view'], 'public')


if __name__ == '__main__':
  absltest.main()
