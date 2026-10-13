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

"""Codec mapping scene specifications to and from plain dictionaries."""

from collections.abc import Mapping
from typing import Any

from concordia.typing import entity as entity_lib
from concordia.typing import scene as scene_lib


def _action_spec_to_dict(
    action_spec: entity_lib.ActionSpec | Mapping[str, entity_lib.ActionSpec],
) -> dict[str, Any]:
  """Encodes an action spec, or a per-participant mapping of them.

  FREE-type specs are stored as their ``call_to_action``, ``output_type``,
  and ``tag`` fields, mirroring the subset of ``ActionSpec`` fields that is
  meaningful for free-form speech.

  Args:
    action_spec: an action spec or a mapping from participant name to one.

  Returns:
    A dictionary encoding of the action spec.
  """
  if isinstance(action_spec, Mapping):
    return {
        name: _action_spec_to_dict(spec) for name, spec in action_spec.items()
    }
  return {
      'call_to_action': action_spec.call_to_action,
      'output_type': action_spec.output_type.value,
      'tag': action_spec.tag,
  }


def _action_spec_from_dict(
    encoded: dict[str, Any] | None,
) -> entity_lib.ActionSpec | Mapping[str, entity_lib.ActionSpec] | None:
  """Decodes an action spec, or a per-participant mapping of them.

  A dictionary whose keys are exactly ``call_to_action``, ``output_type``,
  and ``tag`` decodes as a single action spec; any other dictionary decodes
  as a mapping from participant name to action spec.

  Args:
    encoded: a dictionary produced by ``_action_spec_to_dict``, or None.

  Returns:
    The decoded action spec, mapping of them, or None.
  """
  if encoded is None:
    return None
  if set(encoded) == {'call_to_action', 'output_type', 'tag'}:
    return entity_lib.action_spec_from_dict(encoded)
  return {
      name: entity_lib.action_spec_from_dict(spec)
      for name, spec in encoded.items()
  }


def scene_to_dict(scene: scene_lib.SceneSpec) -> dict[str, Any]:
  """Converts a scene specification into a plain dictionary.

  Args:
    scene: the scene specification to encode.

  Returns:
    A JSON-friendly dictionary with the scene type's name, game master name,
    action spec, and possible participants, plus the scene's participants,
    number of rounds, and per-participant premise.
  """
  kind = scene.scene_type
  premise = scene.premise
  return {
      'scene_type': {
          'name': kind.name,
          'game_master_name': kind.game_master_name,
          'action_spec': (
              None
              if kind.action_spec is None
              else _action_spec_to_dict(kind.action_spec)
          ),
          'possible_participants': (
              None
              if kind.possible_participants is None
              else list(kind.possible_participants)
          ),
      },
      'participants': list(scene.participants),
      'num_rounds': scene.num_rounds,
      'premise': (
          None
          if premise is None
          else {name: list(lines) for name, lines in premise.items()}
      ),
  }


def scene_from_dict(d: dict[str, Any]) -> scene_lib.SceneSpec:
  """Rebuilds a scene specification from its dictionary encoding.

  Args:
    d: a dictionary produced by ``scene_to_dict``.

  Returns:
    The decoded scene specification.
  """
  kind = d['scene_type']
  possible_participants = kind.get('possible_participants')
  premise = d.get('premise')
  return scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name=kind['name'],
          game_master_name=kind.get('game_master_name'),
          action_spec=_action_spec_from_dict(kind.get('action_spec')),
          possible_participants=(
              None
              if possible_participants is None
              else tuple(possible_participants)
          ),
      ),
      participants=tuple(d['participants']),
      num_rounds=d['num_rounds'],
      premise=(
          None
          if premise is None
          else {name: tuple(lines) for name, lines in premise.items()}
      ),
  )
