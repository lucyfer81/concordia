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

from concordia.contrib.components.game_master import story_action
from concordia.typing import entity as entity_lib
from concordia.typing import scene as scene_lib

# Speech prompt shared by all scenes. It asks for the character's own
# dialogue line only, with no role names, quotation wrapping, other
# characters' lines, stage directions, or reasoning, so that raw outputs
# can be concatenated directly into a transcript.
_SPEECH_CALL_TO_ACTION = (
    '根据你已知的信息，{name} 现在说什么？只输出自己的对白正文；'
    '不要角色名、引号包裹、他人台词、旁白或推理过程。'
    '回应最近公开的新信息，保持自己的目标、底线和表达风格；'
    '说出自己的具体疑问、取舍或不同意见，避免跟着他人重复核验、封存等程序口号。'
    '没有新事实时也不要编造证据或声称他人已经同意。'
)

# Speech-and-action prompt (design doc section 4): the character outputs
# a strict JSON object with their own dialogue and their own current
# action attempt. The action intent never prewrites outcomes, object
# contents, other characters' decisions, or already-completed actions.
# A1 guidance: distinguish executing / waiting on a condition / refusing,
# describe the attempt itself without prewriting success or others'
# cooperation, and do not escalate procedure every turn. The output
# contract and the stub-routing fingerprints (the "{name} 现在说什么、
# 做什么" phrasing and the JSON field names) are unchanged.
_SPEECH_AND_ACTION_CALL_TO_ACTION = (
    '根据你已知的信息，{name} 现在说什么、做什么？只输出一个 JSON 对象，'
    '且仅包含两个字符串字段："speech" 为 {name} 现在的对白正文'
    '（无对白时为空字符串），"action_intent" 为 {name} 现在尝试的自身动作'
    '（无动作时为空字符串），两者不能同时为空。'
    '对白不含角色名、引号包裹、他人台词、旁白或推理过程；'
    '行动意图只描述此刻自己尝试的动作，不预写成功结果、物件内容、'
    '他人的决定或过去已完成的动作。'
    '根据你实际收到的观察，回应尚未解决的问题；若决定行动，'
    '选择自己本轮可以尝试的一步，描述尝试本身。'
    '区分正在执行、等待条件和明确拒绝：等待时说清缺少谁的什么回应'
    '或哪个尚未满足的条件；已观察到条件满足时，不要无理由重新提出'
    '同一前置条件，可以继续行动、改变方案或明确拒绝。'
    '符合自身目标和底线的等待、保留意见与拒绝仍然允许。'
    '不必每轮新增核验、登记或签认步骤；"准备核验"只是准备，'
    '不等于查看自己可访问的文书；必要的程序必须有当前问题作为依据。'
    '自己的疑问得到回应后，选择接受、追问其中具体缺口、改换方案或拒绝。'
    '不把对白中的将来承诺写成已执行动作。'
    '没有新事实时不要编造证据或声称他人已经同意。只输出该 JSON 对象。'
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
    npc_response_rules: GM-only content authorization for public direct
      responses of NPCs that already legally entered the story; each
      rule names the NPC, its entrance basis, the interaction trigger,
      the allowed responses, knowledge limits and the public scope. An
      empty tuple keeps the old boundary (no NPC responses beyond the
      existing director paths); the rules are authorization to generate
      within scope, not established facts and not a broadcast.
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
  npc_response_rules: tuple[str, ...] = ()

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
        'npc_response_rules': list(self.npc_response_rules),
    }

  @classmethod
  def from_dict(cls, d: dict[str, Any]) -> 'StoryConfig':
    """Rebuilds a configuration from its dictionary view.

    Args:
      d: a dictionary produced by ``to_dict``.

    Returns:
      The decoded configuration.

    Raises:
      ValueError: if ``npc_response_rules`` is present but is not a list
        or tuple of non-empty strings.
    """
    raw_npc_rules = d.get('npc_response_rules', ())
    if not isinstance(raw_npc_rules, (list, tuple)) or any(
        not isinstance(rule, str) or not rule.strip() for rule in raw_npc_rules
    ):
      raise ValueError(
          'npc_response_rules must be a list or tuple of non-empty strings'
      )
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
        npc_response_rules=tuple(raw_npc_rules),
    )


def make_speech_action_spec() -> entity_lib.ActionSpec:
  """Returns the FREE action spec used to elicit character speech.

  Returns:
    An action spec whose call to action requests only the character's own
    dialogue line.
  """
  return entity_lib.free_action_spec(call_to_action=_SPEECH_CALL_TO_ACTION)


def make_speech_and_action_action_spec() -> entity_lib.ActionSpec:
  """Returns the FREE action spec of the speech-and-action contract.

  Returns:
    An action spec whose call to action requests the strict JSON object
    with exactly the ``speech`` and ``action_intent`` string fields.
  """
  return entity_lib.free_action_spec(
      call_to_action=_SPEECH_AND_ACTION_CALL_TO_ACTION
  )


def make_scene_action_spec(action_mode: str) -> entity_lib.ActionSpec:
  """Returns the scene action spec of the given action mode.

  Args:
    action_mode: one of ``story_action.ACTION_MODES``.

  Returns:
    The dialogue-only spec for ``speech_only`` and the JSON-contract
    spec for ``speech_and_action``.

  Raises:
    ValueError: if the mode is unknown.
  """
  story_action.validate_action_mode(action_mode)
  if action_mode == story_action.ACTION_MODE_SPEECH_AND_ACTION:
    return make_speech_and_action_action_spec()
  return make_speech_action_spec()


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
  if not isinstance(config.npc_response_rules, (list, tuple)):
    problems.append('npc_response_rules must be a list or tuple')
  else:
    for rule in config.npc_response_rules:
      if not isinstance(rule, str) or not rule.strip():
        problems.append(
            f'npc_response_rules must be non-empty strings, got: {rule!r}'
        )
  return problems


def make_example_config() -> StoryConfig:
  """Returns the appendix-C scenario one: a dispute over an old estate.

  On the seventh night after the old master's death, his lawyer reads
  the sealed will aloud in the rain-soaked old house: the estate goes
  to the son, 陈默. The game master alone knows the will was swapped;
  林雪 secretly witnessed 陈母 burning a letter that night; and each
  member of the five-character cast carries a private goal into the
  quarrel. 周律师 is the player character.

  Returns:
    A valid five-character example configuration.
  """
  return StoryConfig(
      title='老宅遗嘱',
      location='江南小镇上一栋百年木结构老宅的堂屋',
      interior=True,
      time_label='雷雨深夜',
      world_facts=(
          '陈老先生（陈望山）一周前病逝，今晚是头七，众人聚在老宅守灵。',
          '窗外雷雨大作，老宅电路老化，堂灯时明时暗。',
          '周律师今晚受托当众启封并宣读遗嘱。',
          '堂屋侧室的保险柜须两把钥匙同时转动才能开启，林雪与陈母各执一把。',
      ),
      gm_only_facts=(
          '真相：遗嘱已被调包——头七前夜，有人从保险柜里换走了老人临终新立的遗嘱。',
          '真相：被焚毁的新遗嘱其实把老宅留给了照顾老人两年的林雪。',
          '真相：周律师如今保管的是被换上的旧遗嘱，封印从未被核验。',
          (
              '真相：被换上的旧遗嘱全文落款日期为二〇一九年三月十二日，'
              '有陈望山签名与手印，无见证人；信封封印完好，从未被当众核验。'
          ),
      ),
      allowed_environment_events=(
          '雷雨骤紧，老宅骤然断电，堂内只剩烛光',
          '周律师的手机响起，来电显示是遗产登记处',
          '邮差冒雨敲门，送来一封老人生前寄出的旧信',
          '供桌旁的青瓷花瓶被袖口带倒，应声碎裂',
          '隔壁邻居被争执声惊动，前来敲门探问',
          '堂屋的旧收音机自己响了起来，播报暴雨红色预警',
          '一阵穿堂风扫过，吹灭了供桌上的两支白烛',
      ),
      immutable_facts=(
          '陈望山已于一周前病逝，无法再立遗嘱或亲口作证。',
          '老宅是一栋百年木结构建筑，今晚众人都在它的堂屋里。',
          '陈母是陈望山的遗孀，也是陈默的母亲。',
          '陈默是陈望山的独子。',
          '林雪是陈望山生前聘请的住家看护，王婶是相交三十年的老邻居。',
      ),
      characters=(
          CharacterInfo(
              name='林雪',
              identity='陈老先生生前聘请的住家看护，陪老人走完最后两年',
              goal='查明遗嘱的真相，为自己两年的付出讨一个公道',
              hard_constraint='绝不伪造证据，也绝不作伪证',
              secret='头七前夜她起夜时，亲眼看见陈母蹲在后院烧纸，纸灰里还残着半枚火漆印。',
              public_facts=(
                  '受老人生前嘱托，掌着保险柜的两把钥匙之一。',
                  '老人最后半年只肯让她一人喂药。',
              ),
              style='语速慢而克制，惯用细节和反问，被逼急了才提高声音',
              initial_relationship_notes='感激老人生前待她如亲；对陈默的敌意保持警惕；暗中怀疑陈母动了什么手脚。',
          ),
          CharacterInfo(
              name='陈默',
              identity='陈老先生独子，常年在外经商，近年鲜少回家',
              goal='尽快确认遗嘱效力并把老宅过户出售，拿钱填补亏空',
              hard_constraint='绝不当众承认自己的债务问题',
              secret='他欠下巨额赌债，三个月内必须还清，否则后果不堪设想。',
              public_facts=(
                  '遗嘱指定的老宅继承人。',
                  '老人生前最后两年几乎未曾回家探望。',
              ),
              style='嗓门大，爱打断别人，动辄把"我们陈家"挂在嘴边',
              initial_relationship_notes=(
                  '视林雪为觊觎家产的外人；对母亲言听计从却心存愧疚；对律师表面客气、暗自提防。'
              ),
          ),
          CharacterInfo(
              name='周律师',
              identity='陈老先生的老友兼私人律师，遗嘱的保管与执行人（玩家角色）',
              goal='按程序公正地完成遗嘱执行，同时保住自己三十年的职业声誉',
              hard_constraint='绝不泄露客户生前的保密事项',
              secret='他私藏着老人口述新遗嘱时的一段录音残片，但音质模糊，真伪难辨。',
              public_facts=(
                  '遗嘱由他密封保管至今。',
                  '与陈家相交三十年，是看着陈默长大的人。',
              ),
              style='措辞严谨，爱引程序和条款，表态前必先说"于程序而言"',
              initial_relationship_notes='敬重陈母；对陈默的急躁不以为然；无法完全排除林雪所言的可能性。',
          ),
          CharacterInfo(
              name='王婶',
              identity='老宅隔壁三十年的老邻居，也是陈家的钟点帮佣',
              goal='弄清楚老先生到底留了什么话，顺便护住老宅不被拆卖',
              hard_constraint='绝不撒谎害人',
              secret='',
              public_facts=(
                  '老人生前常与她念叨心事。',
                  '知道老宅每个房间的钥匙挂在哪。',
              ),
              style='大嗓门，满口俗语和老辈规矩，说到激动处拍大腿',
              initial_relationship_notes=(
                  '心疼林雪两年的辛苦；看着陈默长大却不满他多年不归；与陈母是无话不谈的老姐妹。'
              ),
          ),
          CharacterInfo(
              name='陈母',
              identity='陈老先生遗孀，陈默之母，老宅如今的女主人',
              goal='保住儿子的继承权和陈家的体面，让这场风波尽快平息',
              hard_constraint='绝不承认自己碰过遗嘱',
              secret=(
                  '头七前夜，她从保险柜取出老人新立的那份遗嘱烧掉了，又把最早的旧遗嘱放了回去。'
              ),
              public_facts=(
                  '老人临终前半年由她主持家务。',
                  '坚持要办一场体面的头七。',
              ),
              style='声音低柔，绵里藏针，惯用眼泪和旧年恩情施压',
              initial_relationship_notes=(
                  '认定林雪图谋陈家财产；对儿子又气又护；与王婶相交三十年，最近却有心事瞒她。'
              ),
          ),
      ),
      conflict_start=(
          '周律师当众启封遗嘱并宣读：老宅由独子陈默继承。'
          '林雪当场脸色煞白，追问遗嘱是何时立的、封印由谁核验；'
          '陈默拍案而起，斥她"一个外人也配过问"，争执就此爆发。'
      ),
      allowed_endings=(
          '真相大白：调包之事败露，众人同意重新鉴定遗嘱，承认林雪的权益。',
          '秘密永埋：林雪带着疑问离开，陈默卖宅还债，老宅易主。',
          (
              '协商保宅：不要求陈母认罪，各方自主明确同意暂缓处置老宅，'
              '并就保留老宅和林雪的照护权益达成具体安排。'
          ),
          '僵局散场：争执无解，众人不欢而散，遗嘱交由法庭裁决。',
      ),
      npc_response_rules=(
          (
              '邻居（隔壁邻居）：仅在其已因白名单敲门事件合法登场之后，'
              '才可作为已登场 NPC 回应；当当前角色实际隔门告知暂不需要帮助时，'
              '邻居可公开表示听到了、愿意在被请求时提供普通帮助，'
              '或按该告知离开；不披露屋内秘密，不作法律认证，'
              '不替屋内任何玩家同意，不制造证据；'
              '仅"准备告知"不算已经说出，不触发邻居按告知作出反应；'
              '该回应对在场所有场景参与者公开，不要求固定台词或必定合作。'
          ),
      ),
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
