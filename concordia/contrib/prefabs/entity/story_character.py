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

"""A story character prefab built on the basic three-question actor.

The prefab keeps the basic entity's reasoning chain ("What situation am I
in right now?", "What kind of person am I?", "What would a person like me
do in a situation like this?") by delegating to
`concordia.prefabs.entity.basic.Entity`. On top of that chain it adds a
`CharacterBrief` constant summarizing the character's public data, and,
when the character has one, a `PrivateSecret` constant that belongs only
to this character. All components are created fresh inside `build`, so
the prefab's params only ever hold JSON-safe values.
"""

from collections.abc import Mapping, Sequence
import dataclasses
from typing import Any

from concordia.agents import entity_agent_with_logging
from concordia.associative_memory import basic_associative_memory as associative_memory
from concordia.components import agent as agent_components
from concordia.language_model import language_model
from concordia.prefabs.entity import basic
from concordia.typing import prefab as prefab_lib

_DEFAULT_NAME = 'Alice'

# Context keys of the story components layered on the basic entity, and
# their positions in the basic component order.
_CHARACTER_BRIEF_KEY = 'CharacterBrief'
_CHARACTER_BRIEF_INDEX = 1  # Right after the Instructions component.
_PRIVATE_SECRET_KEY = 'PrivateSecret'
_PRIVATE_SECRET_INDEX = 2  # Right after the character brief.

# The basic prefab requires one index per extra component; user-provided
# components without an index are appended at the end of the order.
_APPEND_AT_END_INDEX = 999


def _compose_brief(
    identity: str,
    style: str,
    public_facts: Sequence[str],
    hard_constraint: str,
    initial_relationship_notes: str,
) -> str:
  """Composes the character brief from the character's data.

  Args:
    identity: public one-line description of who the character is.
    style: description of the character's speech and mannerisms.
    public_facts: baseline information about the character that is public
      knowledge among the cast.
    hard_constraint: behavior the character must never exhibit.
    initial_relationship_notes: how the character initially relates to the
      other characters.

  Returns:
    The text of the character brief constant.
  """
  lines = []
  if identity:
    lines.append(f'Who: {identity}')
  if style:
    lines.append(f'How they speak: {style}')
  if public_facts:
    lines.append('Public facts:')
    lines.extend(f'- {fact}' for fact in public_facts)
  if hard_constraint:
    lines.append(f'Must never: {hard_constraint}')
  if initial_relationship_notes:
    lines.append(f'Relationships: {initial_relationship_notes}')
  return '\n'.join(lines)


@dataclasses.dataclass
class Entity(prefab_lib.Prefab):
  """A prefab implementing a story character."""

  supports_extra_components = True

  description: str = (  # pyrefly: ignore[bad-override]
      'A story character built on the basic three-question actor. A '
      "character brief summarizes the character's public data, and a "
      'private secret, if any, belongs only to this character.'
  )
  params: Mapping[str, Any] = dataclasses.field(
      default_factory=lambda: {
          'name': _DEFAULT_NAME,
          'goal': '',
          'identity': '',
          'hard_constraint': '',
          'secret': '',
          'public_facts': (),
          'style': '',
          'initial_relationship_notes': '',
          'randomize_choices': True,
          # Story characters speak in their own words; the transcript adds
          # speaker names, so the entity must not prefix them itself.
          'prefix_entity_name': False,
          'extra_components': {},
          'extra_components_index': {},
          # Reserved for a later stage (emotional stance and relationship
          # refresh); stored but not used yet.
          'emotion_options': (),
          'relationships_refresh_interval': 0,
      }
  )

  def build(
      self,
      model: language_model.LanguageModel,
      memory_bank: associative_memory.AssociativeMemoryBank,
  ) -> entity_agent_with_logging.EntityAgentWithLogging:
    """Build a story character by delegating to the basic entity prefab.

    Args:
      model: The language model to use.
      memory_bank: The memory bank to use.

    Returns:
      An entity.
    """
    self.check_extra_components()

    # Create fresh components on every build: runtime component objects
    # never enter the prefab's params, which stay JSON-safe.
    extra_components = dict(self.params.get('extra_components', {}))
    extra_components_index = dict(self.params.get('extra_components_index', {}))
    for key in extra_components:
      extra_components_index.setdefault(key, _APPEND_AT_END_INDEX)

    brief = agent_components.constant.Constant(
        state=_compose_brief(
            identity=self.params.get('identity', ''),
            style=self.params.get('style', ''),
            public_facts=self.params.get('public_facts', ()),
            hard_constraint=self.params.get('hard_constraint', ''),
            initial_relationship_notes=self.params.get(
                'initial_relationship_notes', ''
            ),
        ),
        pre_act_label='\nCharacter brief',
    )
    extra_components[_CHARACTER_BRIEF_KEY] = brief
    extra_components_index[_CHARACTER_BRIEF_KEY] = _CHARACTER_BRIEF_INDEX

    secret = self.params.get('secret', '')
    if secret:
      extra_components[_PRIVATE_SECRET_KEY] = (
          agent_components.constant.Constant(
              state=secret, pre_act_label='\nPrivate secret'
          )
      )
      extra_components_index[_PRIVATE_SECRET_KEY] = _PRIVATE_SECRET_INDEX

    basic_params: dict[str, Any] = {
        'name': self.params.get('name', _DEFAULT_NAME),
        'goal': self.params.get('goal', ''),
        'randomize_choices': self.params.get('randomize_choices', True),
        'prefix_entity_name': self.params.get('prefix_entity_name', False),
        'extra_components': extra_components,
        'extra_components_index': extra_components_index,
    }
    for passthrough in (
        'measurements',
        'observation_history_length',
        'situation_perception_history_length',
        'self_perception_history_length',
        'person_by_situation_history_length',
    ):
      if passthrough in self.params:
        basic_params[passthrough] = self.params[passthrough]

    return basic.Entity(params=basic_params).build(model, memory_bank)
