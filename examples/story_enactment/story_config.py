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

"""Story configuration data model for the story enactment example.

The dataclasses here describe a story as a structured configuration: a
setting, world-level facts (some game-master-only), a cast of characters
with public facts, private goals, secrets, and constraints, plus the
conflict that starts the story and the endings the enactment may reach.
"""

import dataclasses
from typing import Any

from concordia.typing import entity as entity_lib
from concordia.typing import scene as scene_lib

# Speech prompt shared by all scenes. It asks for the character's own
# dialogue line only, with no role names, quotation wrapping, other
# characters' lines, stage directions, or reasoning, so that raw outputs
# can be concatenated directly into a transcript.
_SPEECH_CALL_TO_ACTION = (
    '根据你已知的信息，{name} 现在说什么？只输出自己的对白正文；'
    '不要角色名、引号包裹、他人台词、旁白或推理过程。'
)


@dataclasses.dataclass(frozen=True)
class CharacterInfo:
  """Configuration for one character in the story.

  Attributes:
    name: the character's display name, also used as the entity name.
    identity: public one-line description of who the character is.
    goal: the character's private goal in the story.
    hard_constraint: behavior the character must never exhibit.
    secret: information only this character and the game master know; may
      be an empty string when the character has no secret.
    public_facts: baseline information about this character that is public
      knowledge among the cast.
    style: description of the character's speech and mannerisms.
    initial_relationship_notes: how this character initially relates to the
      other characters.
  """

  name: str
  identity: str
  goal: str
  hard_constraint: str
  secret: str
  public_facts: tuple[str, ...]
  style: str
  initial_relationship_notes: str

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-friendly dictionary view of this character.

    Returns:
      A dictionary with all fields, sequences converted to lists.
    """
    return {
        'name': self.name,
        'identity': self.identity,
        'goal': self.goal,
        'hard_constraint': self.hard_constraint,
        'secret': self.secret,
        'public_facts': list(self.public_facts),
        'style': self.style,
        'initial_relationship_notes': self.initial_relationship_notes,
    }

  @classmethod
  def from_dict(cls, d: dict[str, Any]) -> 'CharacterInfo':
    """Rebuilds a character from its dictionary view.

    Args:
      d: a dictionary produced by ``to_dict``.

    Returns:
      The decoded character.
    """
    return cls(
        name=d['name'],
        identity=d['identity'],
        goal=d['goal'],
        hard_constraint=d['hard_constraint'],
        secret=d['secret'],
        public_facts=tuple(d['public_facts']),
        style=d['style'],
        initial_relationship_notes=d['initial_relationship_notes'],
    )


@dataclasses.dataclass(frozen=True)
class StoryConfig:
  """Configuration of a story to be enacted.

  Attributes:
    title: the story's title.
    location: where the story takes place.
    interior: whether the location is an interior space.
    time_label: natural-language description of the starting time.
    world_facts: public facts about the world of the story.
    gm_only_facts: facts only the game master knows.
    allowed_environment_events: environment events the director may inject;
      constrains what the director can do during enactment.
    immutable_facts: facts that must remain true throughout the enactment.
    characters: the cast of the story.
    conflict_start: the public conflict that opens the story.
    allowed_endings: the endings the enactment may reach.
  """

  title: str
  location: str
  interior: bool
  time_label: str
  world_facts: tuple[str, ...]
  gm_only_facts: tuple[str, ...]
  allowed_environment_events: tuple[str, ...]
  immutable_facts: tuple[str, ...]
  characters: tuple[CharacterInfo, ...]
  conflict_start: str
  allowed_endings: tuple[str, ...]

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-friendly dictionary view of this configuration.

    Returns:
      A dictionary with all fields, tuples converted to lists and
      characters to nested dictionaries.
    """
    return {
        'title': self.title,
        'location': self.location,
        'interior': self.interior,
        'time_label': self.time_label,
        'world_facts': list(self.world_facts),
        'gm_only_facts': list(self.gm_only_facts),
        'allowed_environment_events': list(self.allowed_environment_events),
        'immutable_facts': list(self.immutable_facts),
        'characters': [c.to_dict() for c in self.characters],
        'conflict_start': self.conflict_start,
        'allowed_endings': list(self.allowed_endings),
    }

  @classmethod
  def from_dict(cls, d: dict[str, Any]) -> 'StoryConfig':
    """Rebuilds a configuration from its dictionary view.

    Args:
      d: a dictionary produced by ``to_dict``.

    Returns:
      The decoded configuration.
    """
    return cls(
        title=d['title'],
        location=d['location'],
        interior=d['interior'],
        time_label=d['time_label'],
        world_facts=tuple(d['world_facts']),
        gm_only_facts=tuple(d['gm_only_facts']),
        allowed_environment_events=tuple(d['allowed_environment_events']),
        immutable_facts=tuple(d['immutable_facts']),
        characters=tuple(CharacterInfo.from_dict(c) for c in d['characters']),
        conflict_start=d['conflict_start'],
        allowed_endings=tuple(d['allowed_endings']),
    )


def make_speech_action_spec() -> entity_lib.ActionSpec:
  """Returns the FREE action spec used to elicit character speech.

  Returns:
    An action spec whose call to action requests only the character's own
    dialogue line.
  """
  return entity_lib.free_action_spec(call_to_action=_SPEECH_CALL_TO_ACTION)


def validate(config: StoryConfig) -> list[str]:
  """Checks the structural invariants of a story configuration.

  Args:
    config: the configuration to check.

  Returns:
    A list of human-readable problem descriptions, empty when the
    configuration is valid.
  """
  problems = []
  names = [character.name for character in config.characters]
  duplicates = sorted({name for name in names if names.count(name) > 1})
  if duplicates:
    problems.append(f'duplicate character names: {", ".join(duplicates)}')
  for character in config.characters:
    if not character.name.strip():
      problems.append(f'character with empty name: {character.name!r}')
    if not character.public_facts:
      problems.append(
          f'character {character.name!r} has no public baseline facts'
      )
  if not config.allowed_environment_events:
    problems.append('allowed_environment_events must not be empty')
  return problems


def make_example_config() -> StoryConfig:
  """Returns a minimal four-character skeleton configuration.

  The content is placeholder material; a later task replaces it with a
  complete story.

  Returns:
    A valid example configuration.
  """
  return StoryConfig(
      title='排练室',
      location='一间闭门排练的排练室',
      interior=True,
      time_label='傍晚',
      world_facts=('四个戏剧同好约定今晚完成一次剧本围读。',),
      gm_only_facts=('（占位）导演掌握的隐藏信息，待后续任务补充。',),
      allowed_environment_events=('门外传来敲门声', '有人打来电话'),
      immutable_facts=('故事只发生在排练室内。',),
      characters=(
          CharacterInfo(
              name='王兰',
              identity='编剧（占位）',
              goal='让围读顺利进行（占位）',
              hard_constraint='绝不透露剧本结局（占位）',
              secret='',
              public_facts=('王兰是本次围读的发起人。',),
              style='言简意赅（占位）',
              initial_relationship_notes='',
          ),
          CharacterInfo(
              name='李强',
              identity='导演（占位）',
              goal='说服大家采纳自己的处理方式（占位）',
              hard_constraint='不动怒（占位）',
              secret='',
              public_facts=('李强与王兰对结局有分歧。',),
              style='语气强硬（占位）',
              initial_relationship_notes='',
          ),
          CharacterInfo(
              name='张伟',
              identity='演员（占位）',
              goal='争取主演位置（占位）',
              hard_constraint='不撒谎（占位）',
              secret='',
              public_facts=('张伟是最后到场的人。',),
              style='热情外露（占位）',
              initial_relationship_notes='',
          ),
          CharacterInfo(
              name='赵敏',
              identity='舞台监督（占位）',
              goal='按计划完成围读（占位）',
              hard_constraint='不站队（占位）',
              secret='',
              public_facts=('赵敏负责记录围读进程。',),
              style='冷静克制（占位）',
              initial_relationship_notes='',
          ),
      ),
      conflict_start='围读开始前，剧本最后一页不翼而飞。',
      allowed_endings=('最后一页被找回。', '大家决定重写结局。'),
  )


def make_example_scenes(
    config: StoryConfig,
    *,
    num_rounds: int,
) -> tuple[scene_lib.SceneSpec, ...]:
  """Builds the scene schedule for the story.

  The schedule is a single main scene in which every character speaks
  through the shared speech action spec. Each participant's premise states
  only the public conflict start; game-master-only facts and other
  characters' secrets are never included.

  Args:
    config: the story configuration.
    num_rounds: the total round budget for the story.

  Returns:
    A single-element tuple with the main scene.
  """
  scene = scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='故事主线',
          action_spec=make_speech_action_spec(),
      ),
      participants=tuple(character.name for character in config.characters),
      num_rounds=num_rounds,
      premise={
          character.name: (config.conflict_start,)
          for character in config.characters
      },
  )
  return (scene,)
