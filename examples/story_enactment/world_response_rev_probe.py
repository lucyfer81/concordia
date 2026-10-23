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

"""Fixtures for the Sol-only REV re-validation probe (design §6).

This module serves the S2 follow-up batch of
``docs/superpowers/specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md``:
it re-validates the eight REV samples of the archived three-model
world-response probe using only AI_MODEL_4 / gpt-6.1-sol, at most one
real review request per sample. Task 5 provides ONLY the fixture
constants below; the dry_run / freeze / run / summarize stages arrive
in Tasks 6-8 and append to this file under the fixtures section.

Data provenance (design §6.1): the six negative cases REV03-08 and every
inherited field of REV01/REV02 are copied byte-for-byte from the frozen
batch ``run_results/story_enactment/world_response_fixed_probe/``
``20261020T075501Z/probe.py`` (``_SAMPLES`` and the fixture-event
constants). REV01's ``action_result`` and ``stage_directions`` and REV02's
``stage_directions`` follow the §6.1 errata so that the two positive
cases no longer contradict themselves (REV01: the core result must
actually deliver the lawyer's through-the-door notice, the neighbor's
rule-authorized public reply and her departure, and must drop the
"nothing else stirring outside" line that the old narration then
contradicted; REV02: the narration keeps only the established record of
who claimed what and when, adding no unoffered follow-up actions, no
invented clock time and no endorsement of the claim).
``REVISION_RECORD`` lists the per-sample inherited/revised field split
with old/new/reason for every revision, and ``FIXTURE_EVENT_SOURCES``
documents where each extra_history fixture event comes from.

The byte-exact inheritance is verified by a local one-off script
against the frozen batch (never in CI: CI must not read
``run_results``). Below the fixtures, Task 6 appends the runtime
machinery — the two-slot plan, the ledger-guarded client and the real
review path — and Tasks 7-8 append the dry_run / freeze / run /
summarize stages. The fixtures above stay pure data.
"""

from collections.abc import Callable, Collection, Mapping, Sequence
import dataclasses
import datetime
import hashlib
import json
import pathlib
import time
from typing import Any

from absl import app
from absl import flags
from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.language_models.openai import base_gpt_model
from concordia.contrib.language_models.openai import gpt_model
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.typing import entity_component
from dotenv import dotenv_values
import numpy as np

from examples.story_enactment import fixed_model_probe as fp
from examples.story_enactment import request_ledger as rl
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config

# -----------------------------------------------------------------------------
# Fixtures (Task 5). Tasks 6-8 append stage machinery below this section.
# -----------------------------------------------------------------------------

# Frozen fixture events, copied byte-for-byte from the frozen batch's
# probe.py (lines 191-193 and 199-200). They feed extra_history.
_KNOCK = '隔壁邻居被争执声惊动，前来敲门探问'
_PAPER = '周律师从案几抽屉取出一叠记事用的白纸和一支墨笔，摊在供桌旁的空处。'
_CHENMU_CLAIM = '陈母说：头七前夜我一直守在后院灵前，没有离开过半步。'
_PHONE_RING = '周律师的手机响起，来电显示是遗产登记处。'
_PHONE_ANSWER = '周律师接起电话，把手机贴在耳边，侧身避开烛光低声应答。'

# REV01 修订（设计 §6.1）：旧批 action_result 只写到「隔门把话说完，
# 门外雨声里一时没有别的动静」，与随后旁白里邻居的公开回应自相矛盾。
# 核心结果补齐「律师实际隔门告知 + 邻居规则内公开回应 + 离开」，
# 删除与旁白矛盾的「门外没有别的动静」；旁白同时保留律师已执行动作。
_REV01_OLD_ACTION_RESULT = (
    '周律师走到门边，隔着门向邻居说明今晚家事暂不需要帮助，'
    '请她不必久等。 Because of that, '
    '周律师隔门把话说完，门外雨声里一时没有别的动静。'
)
_REV01_ACTION_RESULT = (
    '周律师走到门边，隔着门向邻居说明今晚家事暂不需要帮助，'
    '请她不必久等。 Because of that, 周律师隔门把话说完；'
    '门外传来邻居的应声："知道了，有需要随时叫我一声"，'
    '随后脚步声在雨里渐渐远去。'
)
_REV01_OLD_STAGE_DIRECTIONS = (
    '门外传来邻居的应声："知道了，有需要随时叫我一声"，随后脚步声在雨里渐渐远去。'
)
_REV01_STAGE_DIRECTIONS = (
    '周律师走到门边，隔着门把话说完。' + _REV01_OLD_STAGE_DIRECTIONS
)

# REV02 修订（设计 §6.1）：旧批旁白「吹干墨迹、折好、收进袖中」是
# 未提出的后续动作。旁白只保留「记录了谁的什么未核实主张、说话
# 时刻」的已建立表示；删除吹干、折纸、收存等未提出的后续动作；
# 不补造具体钟点、不认证内容。action_result 原样继承。
_REV02_OLD_STAGE_DIRECTIONS = '林雪吹干墨迹，把那张纸折好，收进袖中。'
_REV02_STAGE_DIRECTIONS = (
    '案上的白纸留着方才的一行记录："陈母自称头七前夜始终守在后院灵前"，'
    '旁边注着说话的时刻。'
)


@dataclasses.dataclass(frozen=True)
class RevCase:
  """One preregistered REV adjudication sample of the Sol-only batch."""

  sample_id: str
  title: str
  player: str
  speech: str
  action_intent: str
  extra_history: tuple[str, ...]
  action_result: str
  stage_directions: str
  expected: str


CASES: tuple[RevCase, ...] = (
    RevCase(
        sample_id='REV01',
        title='已合法登场的邻居给出配置范围内公开回应',
        player='周律师',
        speech='有劳邻居挂心，今晚家事暂不需要帮助，请您回吧。',
        action_intent='周律师走到门边，隔着门向邻居说明今晚家事暂不需要帮助，请她不必久等。',
        extra_history=(_KNOCK,),
        action_result=_REV01_ACTION_RESULT,
        stage_directions=_REV01_STAGE_DIRECTIONS,
        expected='true',
    ),
    RevCase(
        sample_id='REV02',
        title='自己在现有纸上记录带出处的未核实主张',
        player='林雪',
        speech='她这话是不是真的，另当别论，我先原样记下来。',
        action_intent='林雪取过案上的白纸和墨笔，写下陈母方才的主张和说话的时刻。',
        extra_history=(_PAPER, _CHENMU_CLAIM),
        action_result=(
            '林雪取过案上的白纸和墨笔，写下陈母方才的主张和说话的时刻。'
            ' Because of that, '
            '林雪在纸上写下"陈母自称头七前夜始终守在后院灵前"，'
            '并注上说话的时刻。'
        ),
        stage_directions=_REV02_STAGE_DIRECTIONS,
        expected='true',
    ),
    RevCase(
        sample_id='REV03',
        title='没有登场历史却生成邻居回应',
        player='林雪',
        speech='邻居大姐——您还在门外吗？帮我们评评理！',
        action_intent='林雪快步走到大门边，隔着门板大声向隔壁邻居呼喊求助。',
        extra_history=(),
        action_result=(
            '林雪快步走到大门边，隔着门板大声向隔壁邻居呼喊求助。'
            ' Because of that, 林雪隔着门板喊了两声，雨声吞没了大半。'
        ),
        stage_directions=(
            '门外的雨声里传来邻居的应答："来了来了，这就进来帮你们评理。"'
            '不多时，邻居推门进来，收了伞。'
        ),
        expected='false',
    ),
    RevCase(
        sample_id='REV04',
        title='对白声称升级为客观存在与真实性认证',
        player='陈默',
        speech='启封那天的记录我早备下了，白纸黑字，写得清清楚楚！',
        action_intent='陈默把手中的酒杯重重顿在桌上，环视众人。',
        extra_history=(),
        action_result=(
            '陈默把手中的酒杯重重顿在桌上，环视众人。 Because of that, '
            '酒杯顿在桌上，烛火晃了几晃，堂内静了一瞬。'
        ),
        stage_directions='众人传看那页启封记录：纸上的字迹工整，确是启封当日所记。',
        expected='false',
    ),
    RevCase(
        sample_id='REV05',
        title='raw action_result 越权，最终旁白删掉违规句',
        player='陈默',
        speech='妈，您就把钥匙交出来吧，别再为难周叔了。',
        action_intent='陈默转向陈母，要她当场交出保险柜的那把钥匙。',
        extra_history=(),
        action_result=(
            '陈默转向陈母，要她当场交出保险柜的那把钥匙。 Because of that, '
            '陈母叹了口气，从怀里摸出钥匙，放在了案上。'
        ),
        stage_directions='烛光摇曳，陈默的目光落在案上，堂内一片寂静。',
        expected='false',
    ),
    RevCase(
        sample_id='REV06',
        title='合法来电已建立，贴耳私密答复进入全员 canonical',
        player='周律师',
        speech='嗯，您说。',
        action_intent='周律师把手机贴在耳边，继续听电话。',
        extra_history=(_PHONE_RING, _PHONE_ANSWER),
        action_result=(
            '周律师把手机贴在耳边，继续听电话。 Because of that, '
            '周律师把手机贴在耳边，静听对方说话。'
        ),
        stage_directions=(
            '听筒里，登记处的办事员说：您要的登记档案编号查到了，'
            '是〇七三号。堂内众人听着，纷纷点头。'
        ),
        expected='false',
    ),
    RevCase(
        sample_id='REV07',
        title='无来源却声称对方挂断且身份核验失败',
        player='周律师',
        speech='好，我知道了。',
        action_intent='周律师把手机贴在耳边，继续听电话。',
        extra_history=(_PHONE_RING, _PHONE_ANSWER),
        action_result=(
            '周律师把手机贴在耳边，继续听电话。 Because of that, '
            '周律师把手机贴在耳边，静听对方说话。'
        ),
        stage_directions=(
            '听筒那头核验身份失败，对方一言不发地挂断了电话。'
            '周律师收起手机，众人见状渐渐安静下来。'
        ),
        expected='false',
    ),
    RevCase(
        sample_id='REV08',
        title='已登场 NPC 公开披露 GM-only 秘密',
        player='林雪',
        speech='大姐，您是看着陈先生走完这两年的，您评评这个理。',
        action_intent='林雪隔着门把争执的缘由向邻居陈说，请邻居帮忙评理。',
        extra_history=(_KNOCK,),
        action_result=(
            '林雪隔着门把争执的缘由向邻居陈说，请邻居帮忙评理。'
            ' Because of that, 林雪隔着门把争执的缘由说了一遍。'
        ),
        stage_directions=(
            '邻居在门外叹了口气，说：我早瞧见了，头七前夜陈母在后院'
            '烧过带火漆印的纸——这遗嘱，定是换了！'
        ),
        expected='false',
    ),
)


def sample_by_id(sample_id: str) -> RevCase:
  """Returns the preregistered REV case with the given sample id."""
  for case in CASES:
    if case.sample_id == sample_id:
      return case
  raise ValueError(f'unknown sample id: {sample_id}')


_ALL_FIELDS = (
    'sample_id',
    'title',
    'player',
    'speech',
    'action_intent',
    'extra_history',
    'action_result',
    'stage_directions',
    'expected',
)


def _inherited_except(*revised_fields: str) -> list[str]:
  """Returns the nine field names minus the revised ones, in order."""
  return [f for f in _ALL_FIELDS if f not in revised_fields]


# Per-sample field-level provenance versus the frozen batch (design §6.1:
# 新文件明确原样继承与修订字段的逐项差异). Every revised field records
# the old frozen text, the new text and the §6.1 erratum it implements.
REVISION_RECORD: dict[str, dict] = {
    'REV01': {
        'inherited': _inherited_except('action_result', 'stage_directions'),
        'revised': {
            'action_result': {
                'old': _REV01_OLD_ACTION_RESULT,
                'new': _REV01_ACTION_RESULT,
                'reason': (
                    '设计 §6.1 REV01：敲门已提交、规则适用；核心结果明确'
                    '包含律师实际隔门告知、邻居规则内公开回应及离开，'
                    '旁白忠实保留双方已获准结果；删除与旁白矛盾的'
                    '「门外没有别的动静」。'
                ),
            },
            'stage_directions': {
                'old': _REV01_OLD_STAGE_DIRECTIONS,
                'new': _REV01_STAGE_DIRECTIONS,
                'reason': (
                    '设计 §6.1：同时保留律师走到门边、实际隔门告知与'
                    '邻居公开回应、离开；原始台词不能替代已执行动作。'
                ),
            },
        },
    },
    'REV02': {
        'inherited': _inherited_except('stage_directions'),
        'revised': {
            'stage_directions': {
                'old': _REV02_OLD_STAGE_DIRECTIONS,
                'new': _REV02_STAGE_DIRECTIONS,
                'reason': (
                    '设计 §6.1 REV02：纸笔有来源、当前角色尝试记录；'
                    '旁白只保留「记录了谁的什么未核实主张、说话时刻」'
                    '的已建立表示；不补造具体钟点、不认证内容，'
                    '不新增吹干、折纸、收存等未提出的后续动作。'
                ),
            },
        },
    },
    'REV03': {'inherited': list(_ALL_FIELDS), 'revised': {}},
    'REV04': {'inherited': list(_ALL_FIELDS), 'revised': {}},
    'REV05': {'inherited': list(_ALL_FIELDS), 'revised': {}},
    'REV06': {'inherited': list(_ALL_FIELDS), 'revised': {}},
    'REV07': {'inherited': list(_ALL_FIELDS), 'revised': {}},
    'REV08': {'inherited': list(_ALL_FIELDS), 'revised': {}},
}

# Where each extra_history fixture event comes from (design §6.1: 新夹具
# 应并列列出前置来源). knock is the verbatim whitelist event; the rest
# are fixture-authored committed events copied byte-for-byte from the
# frozen batch's fixture-event constants.
FIXTURE_EVENT_SOURCES: dict[str, str] = {
    'knock': (
        '来自白名单事件（旧批 fixture event _KNOCK 逐字节复制）：'
        '「隔壁邻居被争执声惊动，前来敲门探问」；'
        '喂给 REV01/REV08 的 extra_history。'
    ),
    'paper': (
        '纸笔来源：fixture-authored 已提交事件（旧批 fixture event '
        '_PAPER 逐字节复制）：「周律师从案几抽屉取出一叠记事用的白纸'
        '和一支墨笔，摊在供桌旁的空处。」；喂给 REV02 的 extra_history。'
    ),
    'chenmu_claim': (
        '陈母主张：fixture-authored 已提交事件（旧批 fixture event '
        '_CHENMU_CLAIM 逐字节复制）：「陈母说：头七前夜我一直守在'
        '后院灵前，没有离开过半步。」；喂给 REV02 的 extra_history。'
    ),
    'phone_ring': (
        'fixture-authored 已提交事件（旧批 fixture event _PHONE_RING '
        '逐字节复制）：「周律师的手机响起，来电显示是遗产登记处。」；'
        '喂给 REV06/REV07 的 extra_history。'
    ),
    'phone_answer': (
        'fixture-authored 已提交事件（旧批 fixture event _PHONE_ANSWER '
        '逐字节复制）：「周律师接起电话，把手机贴在耳边，侧身避开烛光'
        '低声应答。」；喂给 REV06/REV07 的 extra_history。'
    ),
}

# -----------------------------------------------------------------------------
# Runtime machinery (Task 6): slot plan, guarded client, review path.
#
# Design §6: the S2 batch runs only AI_MODEL_4 / gpt-6.1-sol over the
# eight REV samples; every attempt spends at most one real request (the
# independent semantic review), with the generation answered from the
# frozen single-field narration. Budget, archival and audit flow through
# the Task-2 request ledger; the run/freeze/summarize stages arrive in
# Tasks 7-8.
# -----------------------------------------------------------------------------

_MODEL_TAG = 'm4'
_MODEL_NAME = 'gpt-6.1-sol'
_ENV_KEY = 'AI_MODEL_4'
_SDK_MAX_RETRIES = 0

_GLOBAL_CAP = 8
_MODEL_CAPS: dict[str, int] = {'m4': 8}
_GROUP_CAPS: dict[str, int] = {'REV': 8}
_SAMPLE_CAPS: dict[str, int] = {'REV': 1}

# Prompt fingerprints of the production questions, copied from the old
# batch and used to assert which logical call a slot is serving.
_GENERATION_MARK = '# Candidate event'
_REVIEW_MARK = '# Story resolution semantic review'


class HarnessError(RuntimeError):
  """Probe-fault base class; never forwarded to any provider."""


class SecondRequestBlocked(HarnessError):
  """A slot refused its second real request (hidden retry loop)."""


class PlanExhausted(HarnessError):
  """More model calls arrived than the sample's slot plan allows."""


class BatchStop(RuntimeError):
  """Unrecoverable condition; stop the batch and preserve the scene."""


@dataclasses.dataclass(frozen=True)
class SlotPlan:
  """One logical call of a sample chain, real or frozen."""

  slot_id: str
  kind: str  # 'real' or 'frozen'
  frozen_text: str | None = None


def slot_plan(case: RevCase) -> tuple[SlotPlan, SlotPlan]:
  """Returns the ordered two-slot plan of one REV attempt.

  The generation slot is frozen — the fixture's single-field narration
  answers it with zero real requests — so only the independent semantic
  review is forwarded to the live model.
  """
  sid = case.sample_id
  return (
      SlotPlan(
          f'{sid}__{_MODEL_TAG}__s1_generation',
          'frozen',
          frozen_text=json.dumps(
              {'stage_directions': case.stage_directions}, ensure_ascii=False
          ),
      ),
      SlotPlan(f'{sid}__{_MODEL_TAG}__s2_review', 'real'),
  )


def attempt_id(sample_id: str) -> str:
  """Returns the attempt identifier of one sample's Sol run."""
  return f'{sample_id}__{_MODEL_TAG}'


def _jsonable(value: Any) -> Any:
  """Best-effort conversion of a provider object to JSON data."""
  dump = getattr(value, 'model_dump', None)
  if callable(dump):
    try:
      return dump()
    except Exception:  # pylint: disable=broad-except
      return repr(value)
  if isinstance(value, Mapping):
    return dict(value)
  return value


def _first_content(response: Any) -> str | None:
  """Extracts the visible text of a provider response, if any."""
  try:
    return response.choices[0].message.content
  except Exception:  # pylint: disable=broad-except
    return None


def _ids_of(slot_id: str) -> tuple[str, str]:
  """Returns (sample_id, group) parsed from a slot id."""
  sample_id = slot_id.split('__', 1)[0]
  return sample_id, ''.join(ch for ch in sample_id if ch.isalpha())


class _SlotGuard:
  """One-real-request state of the active slot, shared with the client.

  Adapts the 2026-10-20 batch's call-boundary guard to the Task-2
  request ledger: ``begin_request`` has already persisted the prompt and
  the started marker before the call is forwarded, the guard archives
  the raw response at the SDK observation boundary BEFORE the adapter
  parses it (so a parse failure still leaves the first response on
  disk), and a second ``create()`` of the same slot is recorded and
  refused before it can reach the provider.
  """

  def __init__(self, ledger: rl.RequestLedger):
    self._ledger = ledger
    self.active_slot: SlotPlan | None = None
    self.request_made = False
    self.boundary: dict[str, Any] = {}

  def begin(self, slot: SlotPlan) -> None:
    """Arms the guard for one real slot whose request is starting."""
    self.active_slot = slot
    self.request_made = False
    self.boundary = {}

  def finish(self) -> None:
    """Disarms the guard after the slot's terminal observation."""
    self.active_slot = None
    self.request_made = False

  def observe_response(
      self,
      *,
      boundary_kwargs: Mapping[str, Any],
      messages_count: int | None,
      provider_model: Any,
      usage: Any,
      first_content: str | None,
  ) -> None:
    """Archives one observed provider response at the call boundary."""
    self.boundary = {
        'requested_model': boundary_kwargs.get('model'),
        'boundary_kwargs': dict(boundary_kwargs),
        'boundary_kwargs_note': (
            'create() kwargs at the call boundary, messages excluded'
            ' (the final user message is the archived prompt);'
            ' top_p/top_k are never sent by the production adapter.'
        ),
        'messages_count': messages_count,
        'provider_model': provider_model,
        'usage': usage,
        'first_content': first_content,
    }
    slot = self.active_slot
    if slot is not None:
      # Design §4.2: persist the raw response before any parsing, so a
      # later InvalidResponseError from the adapter still leaves the
      # first raw answer on disk.
      self._ledger.record_raw_response(slot.slot_id, first_content)

  def record_second_request_block(self) -> None:
    """Archives the hidden-retry block of the active slot."""
    slot = self.active_slot
    if slot is None:
      return
    self._ledger.record_second_request_block(
        slot.slot_id, self.boundary.get('first_content') or ''
    )


class _SlotGuardedCompletions:
  """Proxies ``chat.completions`` with one-request-per-slot guarding.

  The first ``create()`` of the active slot is forwarded untouched and
  archived at the call boundary (kwargs, provider model identifier,
  token usage, raw response). Any further ``create()`` within the same
  slot raises ``SecondRequestBlocked`` *before* reaching the provider,
  so the adapter's multiple-choice retry loop can never spend a second
  paid request.
  """

  def __init__(self, real: Any, guard: _SlotGuard):
    self._real = real
    self._guard = guard

  def create(self, *args: Any, **kwargs: Any) -> Any:
    guard = self._guard
    if guard.request_made:
      guard.record_second_request_block()
      active = guard.active_slot.slot_id if guard.active_slot else 'unknown'
      raise SecondRequestBlocked(
          f'slot {active} already served its one request; the hidden'
          ' retry attempt was blocked before the provider.'
      )
    if guard.active_slot is None:
      raise BatchStop('provider call outside any active slot.')
    guard.request_made = True
    response = self._real.create(*args, **kwargs)
    try:
      guard.observe_response(
          boundary_kwargs={
              key: _jsonable(value)
              for key, value in kwargs.items()
              if key != 'messages'
          },
          messages_count=(
              len(kwargs['messages']) if 'messages' in kwargs else None
          ),
          provider_model=getattr(response, 'model', None),
          usage=_jsonable(getattr(response, 'usage', None)),
          first_content=_first_content(response),
      )
    except Exception:  # pylint: disable=broad-except
      pass  # Observation must never mask or replace the real response.
    return response


class _SlotGuardedChat:
  """Fake-proof ``chat`` facet exposing the guarded ``completions``."""

  def __init__(self, real_chat: Any, guard: _SlotGuard):
    self.completions = _SlotGuardedCompletions(real_chat.completions, guard)


class _SlotGuardedClient:
  """Client facade exposing the slot-guarded ``chat.completions``."""

  def __init__(self, real: Any, guard: _SlotGuard):
    self._real = real
    self._guard = guard
    self.chat = _SlotGuardedChat(real.chat, guard)
    self.with_options_calls: list[dict[str, Any]] = []

  def __getattr__(self, name: str) -> Any:
    return getattr(self._real, name)

  def with_options(self, **kwargs: Any) -> '_SlotGuardedClient':
    self.with_options_calls.append(dict(kwargs))
    return _SlotGuardedClient(self._real.with_options(**kwargs), self._guard)


class SolSlotModel(language_model.LanguageModel):
  """Wraps the Sol model under the REV probe's slot plan and ledger.

  Each logical call acquires the next slot of the active plan. Real
  slots reserve one budget unit through ``RequestLedger.begin_request``
  — caps checked, prompt and started marker persisted before the
  request leaves the process — then forward through the slot-guarded
  client and close with ``finish_request`` (terminal status plus the
  observed provider boundary). Frozen slots answer from the frozen
  text via ``record_frozen_slot`` with zero real requests. A call
  beyond the plan raises ``PlanExhausted`` before touching the ledger.
  """

  def __init__(
      self,
      model_name: str,
      *,
      ledger: rl.RequestLedger,
      inner: Any = None,
      api_key: str = '',
      api_base: str = '',
  ):
    """Builds the wrapper around a live or injected inner model.

    Args:
      model_name: the live model's provider name (gpt-6.1-sol).
      ledger: the batch request ledger owning budget and archival.
      inner: injected inner model (offline tests); by default the real
        OpenAI-compatible client for ``model_name``.
      api_key: provider API key when the default inner model is built.
      api_base: provider base URL when the default inner model is built.
    """
    self._model_name = model_name
    self._ledger = ledger
    self._guard = _SlotGuard(ledger)
    if inner is None:
      inner = gpt_model.GptLanguageModel(
          model_name=model_name, api_key=api_key, api_base=api_base
      )
    guarded = _SlotGuardedClient(inner._client, self._guard)  # pylint: disable=protected-access
    # The only client-level hardening: SDK auto-retries off. The
    # request-level timeout passed by the production adapter governs
    # each call.
    inner._client = guarded.with_options(  # pylint: disable=protected-access
        max_retries=_SDK_MAX_RETRIES
    )
    self._inner = inner
    self._slots: tuple[SlotPlan, ...] = ()
    self._cursor = 0

  @property
  def model_name(self) -> str:
    return self._model_name

  def begin_attempt(self, slots: Sequence[SlotPlan]) -> None:
    """Installs the ordered slot plan of one sample attempt."""
    self._slots = tuple(slots)
    self._cursor = 0

  @property
  def cursor(self) -> int:
    """Number of slots consumed so far in the active attempt."""
    return self._cursor

  def _next_slot(self) -> SlotPlan:
    if self._cursor >= len(self._slots):
      raise PlanExhausted(
          f'model {self._model_name}: more calls than the'
          f' {len(self._slots)}-slot plan of the current sample.'
      )
    slot = self._slots[self._cursor]
    self._cursor += 1
    return slot

  def _forward(
      self,
      slot: SlotPlan,
      prompt: str,
      call: Callable[[], Any],
      passed_params: Mapping[str, Any],
  ) -> Any:
    """Runs one real slot: begin_request, forward, finish_request."""
    sample_id, group = _ids_of(slot.slot_id)
    # begin_request checks all four cap layers, persists the prompt and
    # the started marker and appends the started event: a persisted
    # started slot already occupies budget even if the SDK never
    # returns, and the prompt is durably on disk before the call.
    self._ledger.begin_request(
        slot_id=slot.slot_id,
        sample_id=sample_id,
        group=group,
        model_tag=_MODEL_TAG,
        model_name=self._model_name,
        attempt_id=attempt_id(sample_id),
        stage='real_request',
        prompt=prompt,
        passed_params=dict(passed_params),
    )
    self._guard.begin(slot)
    try:
      result = call()
    except BaseException as error:  # pylint: disable=broad-except
      self._close(slot, error)
      raise
    self._close(slot, None)
    return result

  def _close(self, slot: SlotPlan, error: BaseException | None) -> None:
    """Records the slot's terminal observation and disarms the guard."""
    if error is None:
      status, error_text = 'completed', None
    else:
      status = type(error).__name__
      error_text = f'{type(error).__name__}: {error}'
    try:
      self._ledger.finish_request(
          slot_id=slot.slot_id,
          status=status,
          error=error_text,
          boundary=self._guard.boundary,
      )
    finally:
      self._guard.finish()

  def sample_text(
      self,
      prompt: str,
      *,
      max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
      terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
      temperature: float = language_model.DEFAULT_TEMPERATURE,
      top_p: float = language_model.DEFAULT_TOP_P,
      top_k: int = language_model.DEFAULT_TOP_K,
      timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
      seed: int | None = None,
  ) -> str:
    slot = self._next_slot()
    if slot.kind == 'frozen':
      sample_id, _ = _ids_of(slot.slot_id)
      self._ledger.record_frozen_slot(
          slot_id=slot.slot_id,
          sample_id=sample_id,
          model_tag=_MODEL_TAG,
          attempt_id=attempt_id(sample_id),
          prompt=prompt,
          frozen_text=slot.frozen_text or '',
      )
      return slot.frozen_text or ''
    params = {
        'max_tokens': max_tokens,
        'terminators': list(terminators),
        'temperature': temperature,
        'top_p': top_p,
        'top_k': top_k,
        'timeout': timeout,
        'seed': seed,
    }
    return self._forward(
        slot,
        prompt,
        lambda: self._inner.sample_text(
            prompt,
            max_tokens=max_tokens,
            terminators=terminators,
            temperature=temperature,
            top_p=top_p,
            top_k=top_k,
            timeout=timeout,
            seed=seed,
        ),
        params,
    )

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    slot = self._next_slot()
    params = {'responses': list(responses), 'seed': seed}
    return self._forward(
        slot,
        prompt,
        lambda: self._inner.sample_choice(prompt, responses, seed=seed),
        params,
    )


# -----------------------------------------------------------------------------
# Real review path (Task 6): minimal director mount and REV runner,
# copied from the frozen batch's probe (only the REV branch is kept).
# -----------------------------------------------------------------------------

# Placeholder beat, mirroring the deterministic component-level tests.
_BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the dispute resolved?',
        min_turns=9,
        max_turns=12,
    ),
)

# The calibrated 24-event public history of the archived three-model
# batch, reused as the shared committed baseline.
_BASE_HISTORY: tuple[str, ...] = tuple(
    fp._HISTORY  # pylint: disable=protected-access
)


def _embedder(text: str) -> np.ndarray:
  """Deterministic trivial embedder, mirroring the component tests."""
  del text
  return np.ones(16)


def _history_of(case: RevCase) -> tuple[str, ...]:
  """Returns the full committed history of a REV sample."""
  return _BASE_HISTORY + case.extra_history


def _gm_params() -> dict[str, Any]:
  """Returns the production GM params for the speech-and-action scene."""
  return run_lib.make_gm_params(
      story_config.make_example_config(), action_mode='speech_and_action'
  )


def _make_director(
    model: language_model.LanguageModel,
    gm: Mapping[str, Any],
    *,
    player: str,
    action_mode: str,
) -> tuple[
    drama_director.DramaDirector,
    entity_agent_with_logging.EntityAgentWithLogging,
]:
  """Builds a real director mounted on a minimal GM entity."""
  director = drama_director.DramaDirector(
      model=model,
      beats=_BEATS,
      eval_interval=0,
      allowed_environment_events=gm['allowed_environment_events'],
      immutable_facts=gm['immutable_facts'],
      player_names=gm['player_names'],
      character_constraints=gm['character_constraints'],
      world_constraints=gm['world_constraints'],
      action_mode=action_mode,
  )
  gm_entity = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_NoopAct(),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(
              player
          ),
      },
  )
  return director, gm_entity


class _NoopAct(entity_component.ActingComponent):
  """Acting component that never calls a model (director mount only)."""

  def get_action_attempt(self, context, action_spec):
    del context, action_spec
    return 'No'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def __init__(self, name: str = '林雪'):
    self._name = name

  def get_currently_active_player(self):
    return self._name

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _restore_history(
    director: drama_director.DramaDirector, history: Sequence[str]
) -> None:
  """Restores the committed window through the public state API.

  ``set_state`` consumes the full state schema, so the round trip starts
  from the director's own ``get_state`` and only replaces the committed
  recent-events window (the same pattern as the reused probes).
  """
  state = dict(director.get_state())
  state['recent_events'] = list(history)
  director.set_state(state)


def _putative_event(player: str, speech: str, action_intent: str) -> str:
  """Builds the putative-event observation of a dual-field action."""
  raw = json.dumps(
      {'speech': speech, 'action_intent': action_intent}, ensure_ascii=False
  )
  return f'[putative_event] {player}: {raw}'


_REJECTION_PREFIX = 'Semantic review rejected output:'


def _story_failure_kind(error: story_resolution.StoryOutputError) -> str:
  """Classifies a StoryOutputError: review rejection vs malformed output."""
  return (
      'review_rejected'
      if str(error).startswith(_REJECTION_PREFIX)
      else 'malformed_generation'
  )


def _run_rev_case(
    model: SolSlotModel,
    case: RevCase,
    gm: Mapping[str, Any],
) -> dict[str, Any]:
  """Runs the real final-output factory with a frozen generation slot."""
  director, gm_entity = _make_director(
      model, gm, player=case.player, action_mode='speech_and_action'
  )
  history = _history_of(case)
  _restore_history(director, history)
  history_text = fp.public_history_text(history)
  gm_entity.observe(
      _putative_event(case.player, case.speech, case.action_intent)
  )
  director.record_action_result(case.action_result)
  document = interactive_document.InteractiveDocument(model)
  final_step = story_resolution.make_final_output_step(
      director,
      immutable_facts=gm['immutable_facts'],
      character_constraints=gm['character_constraints'],
      player_names=gm['player_names'],
      world_constraints=gm['world_constraints'],
      allowed_environment_events=gm['allowed_environment_events'],
      npc_response_rules=gm['npc_response_rules'],
      public_facts=gm['public_facts'],
      public_history=lambda: history_text,
  )
  candidate = f'{case.player}: {case.speech}'
  try:
    resolved = final_step(document, candidate, case.player)
  except story_resolution.StoryOutputError as error:
    return {
        'failure_kind': _story_failure_kind(error),
        'failure_detail': str(error),
    }
  return {'resolved_event': resolved, 'review_outcome': 'approved'}


# -----------------------------------------------------------------------------
# Run stage (Task 7): resume, stop rules and failure classification.
#
# Design §4.3/§6.2: re-entry rebuilds every count from the request ledger
# alone (never from report numbers), re-verifies the frozen artifacts by
# hash, refuses a recorded batch_stop, and refuses pending
# started-no-result slots unless the user authorizes exactly those slots
# — a deviation that is never resent, never refunded and scoped to the
# authorized list. Review rejections and malformed generations are
# sample failures; budget, plan, provider and unknown errors stop the
# batch and preserve the scene for offline summarization.
# -----------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BATCH_ID = 'sol_rev_revalidation'
_PROTOCOL_NAME = 'protocol.json'
_FIXTURES_NAME = 'fixtures.json'
_SNAPSHOT_NAME = 'source_snapshot.json'
_REPORT_NAME = 'report.json'
_DEVIATION_NOTE = (
    'user-authorized deviation: slot not resent; scope limited to these slots'
)


def _utc_now() -> str:
  """Returns the current UTC time in the batch timestamp format."""
  return datetime.datetime.now(
      datetime.timezone.utc,
  ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _file_sha256(path: pathlib.Path) -> str:
  """Returns the hex SHA-256 of a file's bytes."""
  return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_settings() -> tuple[dict[str, str], str, str, list[str]]:
  """Resolves the live model name and credentials from the repo .env."""
  settings = dotenv_values(_REPO_ROOT / '.env')
  name = settings.get(_ENV_KEY)
  if not name:
    raise BatchStop(f'{_ENV_KEY} is not set in .env')
  if name != _MODEL_NAME:
    raise BatchStop(
        f'{_ENV_KEY} is {name!r}; this frozen batch probes {_MODEL_NAME!r}'
    )
  names = {_MODEL_TAG: name}
  api_key = settings.get('AI_API_KEY') or ''
  base_url = settings.get('AI_BASE_URL') or ''
  if not api_key or not base_url:
    raise BatchStop('AI_API_KEY and AI_BASE_URL must be set in .env')
  return names, api_key, base_url, [api_key, base_url]


def _load_report(batch_dir: pathlib.Path) -> dict[str, Any]:
  """Loads the batch report, refusing a missing file."""
  path = batch_dir / _REPORT_NAME
  if not path.exists():
    raise BatchStop(f'missing batch report: {path}')
  return json.loads(path.read_text(encoding='utf-8'))


def _save_report(batch_dir: pathlib.Path, report: Mapping[str, Any]) -> None:
  """Atomically saves the batch report (write tmp, then replace)."""
  tmp = batch_dir / f'{_REPORT_NAME}.tmp'
  tmp.write_text(
      json.dumps(report, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )
  tmp.replace(batch_dir / _REPORT_NAME)


def _verify_frozen(batch_dir: pathlib.Path) -> None:
  """Re-verifies the frozen artifacts by hash before any request.

  Payload-agnostic by design (the freeze stage owns the payloads): the
  report's three recorded sha256 fields must match the actual bytes of
  ``protocol.json``, ``fixtures.json`` and ``source_snapshot.json``, and
  every file listed in the source snapshot (paths resolved against the
  repo root; absolute paths pass through) must still match its recorded
  hash.
  """
  for name in (
      _PROTOCOL_NAME,
      _FIXTURES_NAME,
      _SNAPSHOT_NAME,
      _REPORT_NAME,
  ):
    if not (batch_dir / name).exists():
      raise BatchStop(f'frozen artefact missing: {name}')
  report = _load_report(batch_dir)
  snapshot = json.loads(
      (batch_dir / _SNAPSHOT_NAME).read_text(encoding='utf-8')
  )
  for entry in snapshot['files']:
    path = _REPO_ROOT / entry['path']
    if not path.exists() or _file_sha256(path) != entry['sha256']:
      raise BatchStop(f"source drift detected: {entry['path']}")
  for name, key in (
      (_PROTOCOL_NAME, 'protocol_sha256'),
      (_FIXTURES_NAME, 'fixtures_sha256'),
      (_SNAPSHOT_NAME, 'source_snapshot_sha256'),
  ):
    recorded = report.get(key)
    if not recorded or _file_sha256(batch_dir / name) != recorded:
      raise BatchStop(f'{name} hash mismatch against the batch report.')


def _refresh_counts(report: dict[str, Any], ledger: rl.RequestLedger) -> None:
  """Refreshes report counts from the ledger alone.

  Fixes the archived three-model batch's stale-counts defect: counts are
  always re-derived from ``ledger.counts`` (rebuilt from
  ``requests.jsonl``), never accumulated in volatile runtime state.
  """
  counts = ledger.counts
  report['counts'] = {
      'planned_attempts': len(CASES),
      'executed_attempts': len(report['attempts']),
      'real_calls': counts['global'],
      'by_model': dict(counts['by_model']),
      'by_group': dict(counts['by_group']),
      'second_request_blocks': counts['second_request_blocks'],
  }


def _run_attempt(
    ledger: rl.RequestLedger,
    case: RevCase,
    *,
    api_key: str = '',
    api_base: str = '',
    inner_factory: Callable[[str], language_model.LanguageModel] | None = None,
) -> dict[str, Any]:
  """Runs one REV sample; never raises for sample-level failures.

  Sample-level format and semantic failures (a malformed review, a
  rejected review, a blocked hidden retry) mark the attempt
  ``sample_failure`` and its remaining real slots skipped, then return
  normally. Budget or plan violations, provider errors and any other
  unexpected fault mark the attempt ``batch_stop``: the caller must stop
  the batch and preserve the scene.
  """
  inner = inner_factory(_MODEL_NAME) if inner_factory is not None else None
  model = SolSlotModel(
      _MODEL_NAME,
      ledger=ledger,
      inner=inner,
      api_key=api_key,
      api_base=api_base,
  )
  slots = slot_plan(case)
  model.begin_attempt(slots)
  result: dict[str, Any] = {
      'sample_id': case.sample_id,
      'model_tag': _MODEL_TAG,
      'group': 'REV',
      'planned_slots': [slot.slot_id for slot in slots],
      'real_slots': [slot.slot_id for slot in slots if slot.kind == 'real'],
      'status': 'running',
      'outputs': {},
      'skipped_slots': [],
      'failure': None,
      'started_at_utc': _utc_now(),
  }

  def mark_skipped(reason: str) -> None:
    for slot in slots[model.cursor :]:
      if slot.kind == 'real':
        ledger.record_skipped(
            slot_id=slot.slot_id,
            sample_id=case.sample_id,
            reason=reason,
        )
        result['skipped_slots'].append(slot.slot_id)

  gm = _gm_params()
  try:
    outputs = _run_rev_case(model, case, gm)
  except (
      SecondRequestBlocked,
      language_model.InvalidResponseError,
  ) as error:
    result['status'] = 'sample_failure'
    result['failure'] = f'{type(error).__name__}: {error}'
    mark_skipped(result['failure'])
    result['finished_at_utc'] = _utc_now()
    return result
  except (rl.LedgerBudgetError, PlanExhausted, BatchStop) as error:
    result['status'] = 'batch_stop'
    result['failure'] = f'{type(error).__name__}: {error}'
    mark_skipped(result['failure'])
    result['finished_at_utc'] = _utc_now()
    return result
  except (KeyboardInterrupt, SystemExit):
    raise
  except Exception as error:  # pylint: disable=broad-except
    # Unknown provider or script error: stop the batch, preserve scene.
    result['status'] = 'batch_stop'
    result['failure'] = (
        f'unexpected {type(error).__name__}: {error}; batch stopped'
    )
    mark_skipped(result['failure'])
    result['finished_at_utc'] = _utc_now()
    return result
  result['outputs'] = outputs
  if outputs.get('failure_kind'):
    result['status'] = 'sample_failure'
    result['failure'] = (
        f"{outputs['failure_kind']}: {outputs.get('failure_detail', '')}"
    )
  else:
    result['status'] = 'completed'
  result['finished_at_utc'] = _utc_now()
  return result


def _book_authorized_deviations(
    report: dict[str, Any],
    ledger: rl.RequestLedger,
    pending: Sequence[str],
) -> None:
  """Books user-authorized pending slots; never resends or refunds.

  Design §4.3: the authorization releases only the pending-slot stop
  condition. Each affected attempt is recorded as ``sample_failure``
  with the deviation note, its un-started real slots are marked skipped,
  and one deviation entry scopes the authorization to exactly the listed
  slots. The authorization never makes other failures retryable and
  never adds budget.
  """
  pending_set = set(pending)
  started = set(ledger.counts['started_slots'])
  for case in CASES:
    slots = slot_plan(case)
    real_ids = [slot.slot_id for slot in slots if slot.kind == 'real']
    case_pending = [slot_id for slot_id in real_ids if slot_id in pending_set]
    if not case_pending:
      continue
    reason = (
        'started_no_result upstream (user-authorized deviation:'
        f' {", ".join(case_pending)} not resent)'
    )
    skipped: list[str] = []
    for slot in slots:
      if (
          slot.kind == 'real'
          and slot.slot_id not in pending_set
          and slot.slot_id not in started
      ):
        ledger.record_skipped(
            slot_id=slot.slot_id,
            sample_id=case.sample_id,
            reason=reason,
        )
        skipped.append(slot.slot_id)
    report['attempts'][attempt_id(case.sample_id)] = {
        'sample_id': case.sample_id,
        'model_tag': _MODEL_TAG,
        'group': 'REV',
        'planned_slots': [slot.slot_id for slot in slots],
        'real_slots': real_ids,
        'status': 'sample_failure',
        'outputs': {},
        'skipped_slots': skipped,
        'failure': (
            f'started_no_result: {", ".join(case_pending)} ({_DEVIATION_NOTE})'
        ),
        'deviation': True,
        'started_at_utc': _utc_now(),
        'finished_at_utc': _utc_now(),
    }
  report['deviations'].append({
      'slots': list(pending),
      'note': _DEVIATION_NOTE,
      'utc': _utc_now(),
  })


def run_stage(
    batch_dir: pathlib.Path | str,
    *,
    authorize_skip_pending: Sequence[str] = (),
    inner_factory: Callable[[str], language_model.LanguageModel] | None = None,
) -> None:
  """Executes the frozen REV order with budgets, resume and stop rules.

  Args:
    batch_dir: the frozen batch directory.
    authorize_skip_pending: pending started-no-result slot ids the user
      explicitly authorizes to skip; they are booked as sample failures
      with a scoped deviation entry and never resent or refunded.
    inner_factory: offline test seam replacing the live inner model;
      when given, no credentials or ``.env`` are read.

  Raises:
    SystemExit: on frozen-artifact drift, a recorded batch_stop, an
      unauthorized pending slot, or a batch-stopping attempt failure.
  """
  batch_dir = pathlib.Path(batch_dir)
  try:
    if inner_factory is not None:
      api_key = api_base = ''
      secrets: list[str] = []
    else:
      _, api_key, api_base, secrets = _load_settings()
    _verify_frozen(batch_dir)
    report = _load_report(batch_dir)
    ledger = rl.RequestLedger(
        batch_dir,
        batch_id=_BATCH_ID,
        global_cap=_GLOBAL_CAP,
        model_caps=_MODEL_CAPS,
        group_caps=_GROUP_CAPS,
        sample_caps=_SAMPLE_CAPS,
        secrets=secrets,
    )
    stopped = [
        key
        for key, attempt in report['attempts'].items()
        if attempt.get('status') == 'batch_stop'
    ]
    if stopped:
      raise SystemExit(
          'a previous batch_stop is recorded; the run stage refuses to'
          ' continue. Offline summarization only.'
          f' Attempts: {stopped}'
      )
    pending = ledger.pending_slots()
    if pending:
      authorized = [
          slot for slot in pending if slot in set(authorize_skip_pending)
      ]
      if len(authorized) != len(pending):
        raise SystemExit(
            'started_no_result slots present; never resent, never'
            ' refunded. Re-enter with --authorize_skip_pending for every'
            ' slot to book a user-authorized deviation.'
            f' Slots: {pending}'
        )
      _book_authorized_deviations(report, ledger, authorized)
      _refresh_counts(report, ledger)
      _save_report(batch_dir, report)
    for case in CASES:
      key = attempt_id(case.sample_id)
      prior = report['attempts'].get(key)
      if prior is not None and prior.get('status') in (
          'completed',
          'sample_failure',
      ):
        continue
      _verify_frozen(batch_dir)
      print(f'running {key} (REV) ...', flush=True)
      started_at = time.time()
      result = _run_attempt(
          ledger,
          case,
          api_key=api_key,
          api_base=api_base,
          inner_factory=inner_factory,
      )
      result['wall_seconds'] = round(time.time() - started_at, 2)
      report['attempts'][key] = result
      _refresh_counts(report, ledger)
      _save_report(batch_dir, report)
      if result['status'] == 'batch_stop':
        report['stop'] = {
            'utc': _utc_now(),
            'attempt': key,
            'reason': result['failure'],
        }
        _save_report(batch_dir, report)
        raise SystemExit(f'batch stopped at {key}: {result["failure"]}')
    _refresh_counts(report, ledger)
    _save_report(batch_dir, report)
    print(f"run complete: {report['counts']['real_calls']} real calls")
  except BatchStop as error:
    raise SystemExit(f'batch stopped before any request: {error}') from error


# -----------------------------------------------------------------------------
# Frozen payloads and the freeze / dry_run / summarize stages (Task 8).
#
# Design §6.2: the real eight-call execution is a separate authorized
# round that first freezes protocol, fixtures and a source snapshot into
# a fresh directory. The payloads below are pure data built from the
# fixtures above; freeze stamps them, the run stage re-verifies them by
# hash, and summarize rebuilds every number offline from the ledger.
# -----------------------------------------------------------------------------

_PLAN_DOCUMENT = (
    'docs/superpowers/plans/'
    '2026-10-21-story-enactment-probe-accounting-and-sol-validation.md'
)
_DESIGN_DOCUMENT = (
    'docs/superpowers/specs/'
    '2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md'
)
_REQUESTS_NAME = 'requests.jsonl'
_LABELS_NAME = 'human_labels.jsonl'
_DRY_RUN_REPORT_NAME = 'dry_run_report.json'
_RAW_DIRNAME = 'raw'

# Repo-root-relative posix paths of everything the real path touches:
# this probe and its ledger, the shared story-enactment modules, the
# concordia production modules the Sol-only review path executes, and
# the design and plan documents (design §6.2: 来源与 runner 哈希).
_SNAPSHOT_FILES: tuple[str, ...] = (
    'examples/story_enactment/world_response_rev_probe.py',
    'examples/story_enactment/world_response_rev_probe_test.py',
    'examples/story_enactment/request_ledger.py',
    'examples/story_enactment/request_ledger_test.py',
    'examples/story_enactment/fixed_model_probe.py',
    'examples/story_enactment/run.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/testing_stubs.py',
    'examples/story_enactment/provenance.py',
    'concordia/agents/entity_agent_with_logging.py',
    'concordia/components/game_master/next_acting.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_action.py',
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/language_models/openai/gpt_model.py',
    'concordia/contrib/language_models/openai/base_gpt_model.py',
    'concordia/document/interactive_document.py',
    'concordia/language_model/language_model.py',
    _DESIGN_DOCUMENT,
    _PLAN_DOCUMENT,
)


def _write_json_file(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
  """Writes indented JSON with a trailing newline, as the batch does."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
  )


def protocol_payload(created_utc: str | None = None) -> dict[str, Any]:
  """Builds the frozen protocol document (Sol-only, design §6.2).

  Args:
    created_utc: freeze-time stamp; the payload alone stays None so the
      frozen file and the in-code payload never diverge by a timestamp.

  Returns:
    The protocol dict: model scope and identity disclosure, caps, slot
    plan, execution order, request settings, pass criteria, stop rules,
    labeling policy and the invocation line.
  """
  slots: dict[str, dict[str, Any]] = {}
  for case in CASES:
    plan = slot_plan(case)
    slots[case.sample_id] = {
        'model': _MODEL_TAG,
        'slot_ids': [slot.slot_id for slot in plan],
        'frozen_slots': [
            slot.slot_id for slot in plan if slot.kind == 'frozen'
        ],
    }
  return {
      'batch': _BATCH_ID,
      'plan_document': _PLAN_DOCUMENT,
      'model_scope': 'Sol-only',
      'models': {
          _MODEL_TAG: {
              'env_key': _ENV_KEY,
              'expected_name': _MODEL_NAME,
              'api': (
                  'OpenAI-compatible GptLanguageModel; client-level'
                  ' max_retries=0; request-level timeout comes from the'
                  ' production adapter.'
              ),
          },
      },
      'identity_disclosure': (
          'The single model is addressed through the OpenAI-compatible'
          ' endpoint as AI_MODEL_4 / gpt-6.1-sol. The provider-returned'
          ' model identifier is archived per request (provider_model);'
          ' the requested and the returned names may differ, and such'
          ' naming differences are not evidence of verified identity and'
          ' no such claim is made.'
      ),
      'budgets': {
          'global_requests': _GLOBAL_CAP,
          'per_model': dict(_MODEL_CAPS),
          'per_group': dict(_GROUP_CAPS),
          'per_sample': dict(_SAMPLE_CAPS),
          'note': (
              'Caps, not targets. Every request attempt (including API'
              ' errors and timeouts) occupies exactly one slot; budgets'
              ' are enforced at the SDK call boundary before any request'
              ' leaves the process. Saved budget is never reallocated.'
          ),
      },
      'slots': slots,
      'execution_order': [attempt_id(case.sample_id) for case in CASES],
      'request_settings': {
          'no_retry_policy': (
              'SDK auto-retries disabled (max_retries=0); the slot guard'
              ' blocks the second request of every slot before the'
              ' provider, so the sample_choice answer loop cannot re-ask;'
              ' no outer retry, no compensation, no best-of reruns.'
          ),
          'started_marker_policy': (
              'Every real slot persists its prompt and a started marker'
              ' through the request ledger before the request leaves the'
              ' process; a started slot occupies its budget unit even if'
              ' no result ever returns.'
          ),
          'interruption_policy': (
              'A pending started-no-result slot is never resent and never'
              ' refunded; re-entry must authorize exactly those slots as'
              ' a user deviation or stay offline.'
          ),
      },
      'pass_criteria': (
          '合法两例接受，非法六例拒绝：REV01/REV02 评审通过且最终事件'
          '忠实，REV03-08 评审拒绝且拒绝理由指向具体越权；'
          '格式错误与未决不算通过。每例至多一次真实评审请求，总真实请求'
          '不超过八次。逐项判定需要具体文本证据；更多旁白、放宽停止规则'
          '或跨批拼接旧结果不算通过。标注（annotation）与人工确认'
          '（human confirmation）状态分列，overall_pass 仅在 REV 门通过'
          '且人工确认后为真。'
      ),
      'stop_rules': (
          'Network/API errors, timeouts, budget or plan violations,'
          ' frozen-artifact drift and script faults stop the batch'
          ' (batch_stop); the scene is preserved for offline'
          ' summarization and configuration is never swapped mid-batch.'
          ' Review rejections and malformed generations are sample'
          ' failures: the sample fails, its remaining real slots are'
          ' marked skipped, and the other preregistered samples continue.'
      ),
      'labeling_policy': (
          'Expected values, titles and sample ids are fixture metadata'
          ' and never enter any prompt (the dry run verifies this). Human'
          ' labels are recorded per attempt in human_labels.jsonl with'
          ' annotator_type and status; human confirmation stays pending'
          ' and overall_pass is never true without it.'
      ),
      'invocation': (
          'source venv/bin/activate; python -m'
          ' examples.story_enactment.world_response_rev_probe'
          ' --stage={dry_run|freeze|run|summarize}'
          ' --batch_dir=<batch directory> (--dry_run_dir,'
          ' --authorize_skip_pending and --reference_snapshot as needed)'
      ),
      'created_utc': created_utc,
  }


def fixtures_payload() -> dict[str, Any]:
  """Builds the frozen fixture document (labels excluded from prompts)."""
  return {
      'batch': _BATCH_ID,
      'shared': {
          'story_source': (
              'examples/story_enactment/story_config.make_example_config()'
              ' （老宅遗嘱场景）'
          ),
          'gm_params_source': (
              'run_lib.make_gm_params(story_config.make_example_config(),'
              ' action_mode="speech_and_action")'
          ),
          'base_history_source': (
              'the calibrated 24-event public history of fixed_model_probe'
              ' (fp._HISTORY, batch 20261017T143746Z)'
          ),
          'base_history': list(_BASE_HISTORY),
          'fixture_events': {
              'knock (verbatim whitelist)': _KNOCK,
              'paper (fixture-authored)': _PAPER,
              'chenmu_claim (fixture-authored, attributed speech)': (
                  _CHENMU_CLAIM
              ),
              'phone_ring (verbatim whitelist)': _PHONE_RING,
              'phone_answer (fixture-authored)': _PHONE_ANSWER,
          },
          'fixture_event_sources': dict(FIXTURE_EVENT_SOURCES),
      },
      'samples': [dataclasses.asdict(case) for case in CASES],
      'revision_record': REVISION_RECORD,
      'labels': {
          'human_confirmation': 'pending',
          'policy': (
              'Labels, expected values, titles and sample ids are metadata'
              ' only; the dry run verifies none of them enter any prompt.'
          ),
      },
  }


def source_snapshot_payload() -> dict[str, Any]:
  """Per-file content hashes of the exact bytes this batch touches.

  Hashes are computed at call time from the repo root, so freezing
  captures the working tree and every later stage can re-verify it.
  """
  files = []
  for relative in _SNAPSHOT_FILES:
    path = _REPO_ROOT / relative
    if not path.exists():
      raise BatchStop(f'snapshot file missing: {relative}')
    files.append({
        'path': relative,
        'sha256': _file_sha256(path),
        'bytes': path.stat().st_size,
    })
  return {
      'created_utc': _utc_now(),
      'hash_algorithm': 'sha256',
      'files': files,
      'note': (
          'Per-file content hashes of this probe, its request ledger, the'
          ' shared story-enactment modules, the concordia production'
          ' modules the real path touches, and the design and plan'
          ' documents. Run stages re-verify these hashes before each'
          ' attempt and refuse to continue after drift.'
      ),
  }


def freeze_stage(batch_dir: pathlib.Path | str) -> None:
  """Freezes protocol, fixtures, source snapshot and the initial report.

  Offline by design: no credentials are read and no client is built; the
  run stage re-verifies ``AI_MODEL_4`` against ``.env`` before any
  request. Freezing into an existing directory is refused.

  Args:
    batch_dir: the fresh batch directory to create and populate.

  Raises:
    BatchStop: if the directory (or any frozen artefact) already exists.
  """
  batch_dir = pathlib.Path(batch_dir)
  if batch_dir.exists():
    raise BatchStop(
        f'refusing to freeze into an existing directory: {batch_dir}'
    )
  batch_dir.mkdir(parents=True)
  created = _utc_now()
  _write_json_file(
      batch_dir / _PROTOCOL_NAME, protocol_payload(created_utc=created)
  )
  _write_json_file(batch_dir / _FIXTURES_NAME, fixtures_payload())
  _write_json_file(batch_dir / _SNAPSHOT_NAME, source_snapshot_payload())
  report = {
      'batch': _BATCH_ID,
      'batch_dir': str(batch_dir),
      'created_utc': created,
      'models': {_MODEL_TAG: _MODEL_NAME},
      'protocol_sha256': _file_sha256(batch_dir / _PROTOCOL_NAME),
      'fixtures_sha256': _file_sha256(batch_dir / _FIXTURES_NAME),
      'source_snapshot_sha256': _file_sha256(batch_dir / _SNAPSHOT_NAME),
      'budgets': {
          'global_requests': _GLOBAL_CAP,
          'per_model': dict(_MODEL_CAPS),
          'per_group': dict(_GROUP_CAPS),
          'per_sample': dict(_SAMPLE_CAPS),
      },
      'counts': {
          'planned_attempts': len(CASES),
          'executed_attempts': 0,
          'real_calls': 0,
          'by_model': {_MODEL_TAG: 0},
          'by_group': {'REV': 0},
          'second_request_blocks': 0,
      },
      'attempts': {},
      'stop': None,
      'deviations': [],
      'summary': None,
      'overall_pass': None,
  }
  _save_report(batch_dir, report)
  print(
      'frozen:'
      f" protocol={report['protocol_sha256'][:12]}"
      f" fixtures={report['fixtures_sha256'][:12]}"
      f" snapshot={report['source_snapshot_sha256'][:12]}"
  )


# -----------------------------------------------------------------------------
# Offline dry run (Task 8): no real client is ever created.
# -----------------------------------------------------------------------------

_DRY_RUN_APPROVAL = '{"valid": true, "reason": "dry run approval"}'


class _DryRunResponse:
  """Minimal provider response object (old batch probe.py pattern)."""

  def __init__(self, content: str, model: str):
    self.model = model
    self.usage = None
    self.choices = [
        type(
            'Choice',
            (),
            {'message': type('Message', (), {'content': content})()},
        )()
    ]


class _DryRunCompletions:
  """Scripted ``create`` routing production prompts to frozen answers.

  Generation prompts (``_GENERATION_MARK``) receive the case's frozen
  narration JSON; review prompts (``_REVIEW_MARK``) a fixed approval
  verdict. Anything else is a harness fault, never a real call.
  """

  def __init__(self, case: RevCase, model_name: str):
    self._case = case
    self._model_name = model_name
    self.calls: list[dict[str, Any]] = []

  def create(self, *args: Any, **kwargs: Any) -> _DryRunResponse:
    prompt = kwargs['messages'][-1]['content']
    if _GENERATION_MARK in prompt:
      answer = json.dumps(
          {'stage_directions': self._case.stage_directions},
          ensure_ascii=False,
      )
    elif _REVIEW_MARK in prompt:
      answer = _DRY_RUN_APPROVAL
    else:
      raise BatchStop('dry run: prompt matched no production mark.')
    self.calls.append({k: v for k, v in kwargs.items() if k != 'messages'})
    return _DryRunResponse(answer, self._model_name)


class _DryRunChat:
  """Fake ``chat`` facet exposing only ``completions``."""

  def __init__(self, completions: _DryRunCompletions):
    self.completions = completions


class _DryRunClient:
  """Fake provider client; ``with_options`` is recorded, not applied."""

  def __init__(self, completions: _DryRunCompletions):
    self.chat = _DryRunChat(completions)
    self.completions = completions
    self.with_options_calls: list[dict[str, Any]] = []

  def with_options(self, **kwargs: Any) -> '_DryRunClient':
    self.with_options_calls.append(dict(kwargs))
    return self


def _dry_run_inner_factory(
    case: RevCase, clients: list[_DryRunClient]
) -> Callable[[str], language_model.LanguageModel]:
  """Builds the offline inner-factory seam for one REV attempt.

  The inner model is the real production adapter mounted on the fake
  client, so the dry run exercises the true parsing path with zero
  OpenAI client construction.
  """

  def factory(model_name: str) -> language_model.LanguageModel:
    client: Any = _DryRunClient(_DryRunCompletions(case, model_name))
    clients.append(client)
    return base_gpt_model.BaseGPTModel(model_name=model_name, client=client)

  return factory


def _dry_run_label_hygiene(
    dry_run_dir: pathlib.Path, findings: list[str]
) -> None:
  """Asserts no label metadata enters any archived prompt.

  Concrete, deterministic needles: the fixtures' expected-value field
  name and every sample's id and title must appear in no prompt file.
  """
  prompt_paths = sorted((dry_run_dir / _RAW_DIRNAME).glob('*.prompt.txt'))
  expected_prompts = 2 * len(CASES)
  if len(prompt_paths) != expected_prompts:
    findings.append(
        f'expected {expected_prompts} archived prompts, found'
        f' {len(prompt_paths)}'
    )
  forbidden = ['expected_valid']
  forbidden += [case.sample_id for case in CASES]
  forbidden += [case.title for case in CASES]
  for path in prompt_paths:
    text = path.read_text(encoding='utf-8')
    for needle in forbidden:
      if needle and needle in text:
        findings.append(f'{path.name}: prompt leaks {needle[:30]!r}')


def dry_run_stage(dry_run_dir: pathlib.Path | str) -> None:
  """Runs all eight REV attempts offline; zero real clients.

  The attempts go through the real ``_run_rev_case`` path with the
  production adapter mounted on a scripted fake provider; the request
  ledger archives prompts and events into ``dry_run_dir`` with the full
  budget caps enforced.

  Args:
    dry_run_dir: fresh directory for the dry-run artifacts.

  Raises:
    BatchStop: if the directory already exists.
    SystemExit: if the dry run finished with findings.
  """
  dry_run_dir = pathlib.Path(dry_run_dir)
  if dry_run_dir.exists():
    raise BatchStop(
        f'refusing to reuse an existing dry run directory: {dry_run_dir}'
    )
  dry_run_dir.mkdir(parents=True)
  ledger = rl.RequestLedger(
      dry_run_dir,
      batch_id=f'{_BATCH_ID}__dry_run',
      global_cap=_GLOBAL_CAP,
      model_caps=_MODEL_CAPS,
      group_caps=_GROUP_CAPS,
      sample_caps=_SAMPLE_CAPS,
  )
  findings: list[str] = []
  attempts_report: list[dict[str, Any]] = []
  for case in CASES:
    clients: list[_DryRunClient] = []
    factory = _dry_run_inner_factory(case, clients)
    result = _run_attempt(ledger, case, inner_factory=factory)
    key = attempt_id(case.sample_id)
    if result['status'] != 'completed':
      findings.append(f"{key}: status {result['status']} ({result['failure']})")
    if any(
        client.with_options_calls != [{'max_retries': _SDK_MAX_RETRIES}]
        for client in clients
    ):
      findings.append(f'{key}: client not hardened with max_retries=0')
    attempts_report.append({
        'attempt': key,
        'status': result['status'],
        'provider_calls': sum(
            len(client.completions.calls) for client in clients
        ),
        'with_options_calls': [client.with_options_calls for client in clients],
    })
  counts = ledger.counts
  if counts['global'] != len(CASES):
    findings.append(
        f"expected {len(CASES)} real calls, ledger has {counts['global']}"
    )
  if len(counts['frozen_slots']) != len(CASES):
    findings.append(
        f'expected {len(CASES)} frozen slots, ledger has'
        f" {len(counts['frozen_slots'])}"
    )
  if len(counts['started_slots']) != len(CASES):
    findings.append(
        f'expected {len(CASES)} review slots, ledger has'
        f" {len(counts['started_slots'])}"
    )
  for case in CASES:
    s1, s2 = slot_plan(case)
    if s1.slot_id not in counts['frozen_slots']:
      findings.append(f'{s1.slot_id}: frozen generation slot missing')
    if s2.slot_id not in counts['started_slots']:
      findings.append(f'{s2.slot_id}: review slot never started')
  if counts['second_request_blocks'] != 0:
    findings.append('dry run saw a blocked hidden retry')
  _dry_run_label_hygiene(dry_run_dir, findings)
  report = {
      'batch': _BATCH_ID,
      'created_utc': _utc_now(),
      'attempts': attempts_report,
      'findings': findings,
      'provider_calls_total': sum(
          entry['provider_calls'] for entry in attempts_report
      ),
      'runtime_real_calls': counts['global'],
      'ok': not findings,
  }
  _write_json_file(dry_run_dir / _DRY_RUN_REPORT_NAME, report)
  print(f'dry run: {len(attempts_report)} attempts, findings: {len(findings)}')
  for finding in findings:
    print(f'  - {finding}')
  if findings:
    raise SystemExit(f'dry run finished with {len(findings)} findings')


# -----------------------------------------------------------------------------
# Offline summarization (Task 8): classification, gates, disclosure.
# -----------------------------------------------------------------------------

# Failure-class prefixes that mark a malformed or unusable model answer
# (a blocked hidden retry, an unparseable review, a malformed
# generation) as opposed to a batch-stopping fault.
_FORMAT_FAILURE_PREFIXES = (
    'SecondRequestBlocked',
    'InvalidResponseError',
    'malformed_generation',
)


def _classify_rev_attempt(
    attempt: Mapping[str, Any], pending_slots: frozenset[str]
) -> str:
  """Classifies one REV attempt into the eight reporting buckets.

  A pending started-no-result slot always wins (indeterminate). A
  completed attempt is approved (expected true) or a missed detection
  (expected false). A review rejection is the expected outcome for the
  six negatives and a false rejection for the two positives. Format
  failures are malformed or unusable answers; every remaining fault
  (provider exception, budget/plan refusal, unknown error) is grouped
  as api_error — all of them keep the gate closed.
  """
  if any(slot in pending_slots for slot in attempt.get('real_slots', ())):
    return 'pending'
  status = attempt.get('status')
  failure = attempt.get('failure') or ''
  expected_valid = sample_by_id(attempt['sample_id']).expected == 'true'
  if status == 'completed':
    return 'approved' if expected_valid else 'missed_detection'
  if failure.startswith('review_rejected'):
    return 'rejected_expectedly' if not expected_valid else 'false_rejection'
  if failure.startswith(_FORMAT_FAILURE_PREFIXES):
    return 'format_error'
  return 'api_error'


def _load_human_labels(batch_dir: pathlib.Path) -> dict[str, dict[str, Any]]:
  """Loads labels, rejecting duplicate, unknown or malformed entries."""
  labels: dict[str, dict[str, Any]] = {}
  planned = {attempt_id(case.sample_id) for case in CASES}
  labels_path = batch_dir / _LABELS_NAME
  if not labels_path.exists():
    return labels
  for line in labels_path.read_text(encoding='utf-8').splitlines():
    if line.strip():
      try:
        entry = json.loads(line)
      except json.JSONDecodeError as error:
        raise rl.LedgerAuditError(f'invalid human label: {error}') from error
      key = entry.get('attempt_id') if isinstance(entry, dict) else None
      if not isinstance(key, str) or key not in planned:
        raise rl.LedgerAuditError(f'unknown human label attempt_id: {key!r}')
      if key in labels:
        raise rl.LedgerAuditError(f'duplicate human label attempt_id: {key}')
      labels[key] = entry
  return labels


def _audit_rev_results(
    batch_dir: pathlib.Path,
    report: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
) -> None:
  """Binds reported REV results to the planned slots and saved evidence."""
  planned = {slot_plan(case)[1].slot_id: case for case in CASES}
  starts = {
      event['slot_id']: event for event in events if event['event'] == 'started'
  }
  finishes = {
      event['slot_id']: event
      for event in events
      if event['event'] == 'finished'
  }
  frozen = [
      event['slot_id'] for event in events if event['event'] == 'frozen_stub'
  ]
  for slot_id, start in starts.items():
    case = planned.get(slot_id)
    if case is None or any(
        start.get(key) != value
        for key, value in {
            'sample_id': case.sample_id,
            'group': 'REV',
            'model_tag': _MODEL_TAG,
            'model_name': _MODEL_NAME,
            'attempt_id': attempt_id(case.sample_id),
        }.items()
    ):
      raise rl.LedgerAuditError(
          f'REV request outside frozen slot plan: {slot_id}'
      )
  known_attempts = {attempt_id(case.sample_id) for case in CASES}
  if set(report['attempts']) - known_attempts:
    raise rl.LedgerAuditError('unknown REV result attempt_id')
  for case in CASES:
    attempt = report['attempts'].get(attempt_id(case.sample_id))
    if attempt is None:
      continue
    generation, review = slot_plan(case)
    if attempt.get('sample_id') != case.sample_id or attempt.get(
        'real_slots'
    ) != [review.slot_id]:
      raise rl.LedgerAuditError(f'REV result slot mismatch: {case.sample_id}')
    kind = _classify_rev_attempt(attempt, frozenset())
    if kind not in (
        'approved',
        'rejected_expectedly',
        'false_rejection',
        'missed_detection',
    ):
      continue
    terminal = finishes.get(review.slot_id)
    if (
        review.slot_id not in starts
        or terminal is None
        or terminal.get('status') != 'completed'
        or frozen.count(generation.slot_id) != 1
    ):
      raise rl.LedgerAuditError(
          f'missing completed REV evidence: {case.sample_id}'
      )
    raw_dir = batch_dir / _RAW_DIRNAME
    record = json.loads(
        (raw_dir / f'{review.slot_id}.record.json').read_text(
            encoding='utf-8', errors='replace'
        )
    )
    if record != {
        key: value for key, value in terminal.items() if key != 'event'
    }:
      raise rl.LedgerAuditError(
          f'REV terminal record mismatch: {case.sample_id}'
      )
    response = (raw_dir / f'{review.slot_id}.response.txt').read_text(
        encoding='utf-8', errors='replace'
    )
    try:
      story_resolution._validate_review(response)  # pylint: disable=protected-access
      approved = True
    except story_resolution.StoryOutputError as error:
      if _story_failure_kind(error) != 'review_rejected':
        raise rl.LedgerAuditError(
            f'invalid REV verdict evidence: {case.sample_id}'
        ) from error
      approved = False
    if approved != (attempt['status'] == 'completed'):
      raise rl.LedgerAuditError(
          f'REV verdict/result mismatch: {case.sample_id}'
      )


def _dependency_drift(
    reference_snapshot: pathlib.Path | None,
) -> list[dict[str, str]] | str:
  """Lists intersection files whose hash differs from the reference.

  Args:
    reference_snapshot: path to a reference ``source_snapshot.json``;
      None records that the comparison was not performed.

  Returns:
    A list of ``{path, reference_sha256, current_sha256}`` entries for
    every file present in both snapshots whose sha256 differs, or the
    string ``'not compared'``.
  """
  if reference_snapshot is None:
    return 'not compared'
  reference = json.loads(
      pathlib.Path(reference_snapshot).read_text(encoding='utf-8')
  )
  reference_hashes = {
      entry['path']: entry['sha256'] for entry in reference['files']
  }
  current_hashes = {
      entry['path']: entry['sha256']
      for entry in source_snapshot_payload()['files']
  }
  return [
      {
          'path': path,
          'reference_sha256': reference_hashes[path],
          'current_sha256': current_hashes[path],
      }
      for path in sorted(set(reference_hashes) & set(current_hashes))
      if reference_hashes[path] != current_hashes[path]
  ]


def summarize_stage(
    batch_dir: pathlib.Path | str,
    *,
    reference_snapshot: pathlib.Path | None = None,
) -> dict[str, Any]:
  """Summarizes the batch offline; never creates a client or a call.

  Every integrity number is rebuilt from the request ledger alone
  (never restated from the report), each attempt is classified into the
  eight REV buckets, the REV gate requires the two positives approved,
  the six negatives rejected expectedly, zero format/api/pending
  defects and at most eight real calls, and ``overall_pass`` stays
  false without all-eight human labels.

  Args:
    batch_dir: the frozen batch directory.
    reference_snapshot: optional reference ``source_snapshot.json`` for
      the dependency-drift disclosure.

  Returns:
    The summary block written into the report.
  """
  batch_dir = pathlib.Path(batch_dir)
  report = _load_report(batch_dir)
  ledger_path = batch_dir / _REQUESTS_NAME
  try:
    _verify_frozen(batch_dir)
    events = rl.load_events(ledger_path) if ledger_path.exists() else []
    rl.audit_events(events, raw_dir=batch_dir / _RAW_DIRNAME)
    _audit_rev_results(batch_dir, report, events)
    labels = _load_human_labels(batch_dir)
  except (BatchStop, rl.LedgerAuditError, OSError, ValueError) as error:
    # A previously saved pass must not survive a later failed audit.
    report['overall_pass'] = False
    report['summary'] = None
    report['overall_pass_note'] = f'Integrity audit failed: {error}'
    _save_report(batch_dir, report)
    raise rl.LedgerAuditError(str(error)) from error
  counts = rl.rebuild_counts(events)
  finished = set(counts['finished_slots'])
  pending = [slot for slot in counts['started_slots'] if slot not in finished]
  integrity = {
      'planned_attempts': len(CASES),
      'executed_attempts': len(report['attempts']),
      'real_calls': counts['global'],
      'by_model': dict(counts['by_model']),
      'by_group': dict(counts['by_group']),
      'frozen_slots': list(counts['frozen_slots']),
      'skipped_slots': list(counts['skipped_slots']),
      'second_request_blocks': counts['second_request_blocks'],
      'started_no_result': pending,
      'retries': 0,
      'stop_reason': report.get('stop'),
  }
  pending_set = frozenset(pending)
  classification = {
      key: 0
      for key in (
          'approved',
          'rejected_expectedly',
          'false_rejection',
          'missed_detection',
          'format_error',
          'api_error',
          'pending',
          'not_run',
      )
  }
  per_sample: dict[str, str] = {}
  for case in CASES:
    key = attempt_id(case.sample_id)
    attempt = report['attempts'].get(key)
    kind = (
        'not_run'
        if attempt is None
        else _classify_rev_attempt(attempt, pending_set)
    )
    classification[kind] += 1
    per_sample[key] = kind
  rev_gate = (
      classification['approved'] == 2
      and classification['rejected_expectedly'] == 6
      and classification['format_error'] == 0
      and classification['api_error'] == 0
      and classification['pending'] == 0
      and integrity['real_calls'] <= _GLOBAL_CAP
  )
  human_confirmed = all(
      labels.get(key, {}).get('annotator_type') == 'human'
      and labels.get(key, {}).get('status') == 'labeled'
      for key in per_sample
  )
  report['summary'] = {
      'integrity': integrity,
      'rev_classification': classification,
      'rev_per_sample': per_sample,
      'rev_gate': rev_gate,
      'dependency_drift_vs_reference': _dependency_drift(reference_snapshot),
      'human_labels': {'labeled_attempts': sorted(labels)},
      'human_confirmation': 'confirmed' if human_confirmed else 'pending',
  }
  report['overall_pass'] = bool(rev_gate and human_confirmed)
  report['overall_pass_note'] = (
      'overall_pass requires the REV gate (two positives approved, six'
      ' negatives rejected expectedly, zero format/api/pending defects,'
      ' at most eight real calls) and explicit all-eight human'
      ' confirmation; labels never enter prompts.'
  )
  _save_report(batch_dir, report)
  print(json.dumps(integrity, ensure_ascii=False, indent=2))
  print('rev_gate:', rev_gate)
  print('overall_pass:', report['overall_pass'])
  return report['summary']


# -----------------------------------------------------------------------------
# Entry point.
# -----------------------------------------------------------------------------

# Private flag namespace: sibling probes define absl app flags with the
# same names, and absl has no per-module namespaces.
FLAGS = flags.FlagValues()
flags.DEFINE_string(
    'stage',
    None,
    'One of: dry_run, freeze, run, summarize.',
    required=True,
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'batch_dir',
    str(pathlib.Path(__file__).resolve().parent),
    'The frozen batch directory.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'dry_run_dir',
    '/tmp/opencode/world_response_rev_probe_dry_run',
    'Where the offline dry run archives its artifacts.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'authorize_skip_pending',
    '',
    'Comma-separated pending slot ids the user authorizes to skip.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'reference_snapshot',
    '',
    'Optional reference source snapshot compared by the summarize stage.',
    flag_values=FLAGS,
)


def _parse_flags(argv: list[str]) -> list[str]:
  """Parses this probe's private flags (absl app flags_parser hook)."""
  args = FLAGS(argv)
  flags.FLAGS.mark_as_parsed()
  return args


def main(argv: Sequence[str]) -> None:
  """Dispatches the batch stage."""
  del argv
  stage = FLAGS.stage
  batch_dir = pathlib.Path(FLAGS.batch_dir).resolve()
  if stage == 'run':
    run_stage(
        batch_dir,
        authorize_skip_pending=[
            slot
            for slot in (
                entry.strip()
                for entry in FLAGS.authorize_skip_pending.split(',')
            )
            if slot
        ],
    )
  elif stage == 'dry_run':
    dry_run_stage(pathlib.Path(FLAGS.dry_run_dir))
  elif stage == 'freeze':
    freeze_stage(batch_dir)
  elif stage == 'summarize':
    reference = (
        pathlib.Path(FLAGS.reference_snapshot).resolve()
        if FLAGS.reference_snapshot
        else None
    )
    summarize_stage(batch_dir, reference_snapshot=reference)
  else:
    raise SystemExit(f'unknown --stage: {stage!r}')


if __name__ == '__main__':
  app.run(main, flags_parser=_parse_flags)
