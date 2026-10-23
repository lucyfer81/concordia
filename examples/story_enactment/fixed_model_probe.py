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

"""Fixed-sample, frozen-protocol probe of the story-enactment live models.

This runner executes the 2026-10-17 fixed-model validation plan
(docs/superpowers/plans/2026-10-17-story-enactment-fixed-model-validation.md)
against the current production source. It never reimplements production
logic: every probed call runs the real thought-step factories
(`story_resolution.make_final_output_step` for group R and
`story_action_adjudication.make_action_adjudication_step` plus the core
`event_resolution.attempt_to_result` for group A) on deterministic
director fixtures built from the example configuration factory.

Group R (fixed review, F01-F16): each attempt drives one production
final-output resolution. The probe-specific two-slot model wrapper
answers the FIRST call — the production generation task — synthetically
with the fixture's frozen narration (so it consumes no API attempt) and
forwards the SECOND call — the production semantic review — verbatim to
the live model, archiving the prompt, the request parameters and the raw
response. Any further call, any choice call or an unexpected routing is
a harness error and is never sent to the API.

Group A (fixed action adjudication, A01-A04): each attempt opens a real
director turn lifecycle, states the frozen context through the
production adjudication step, and forwards the single core question to
the live model through a wraps spy. The spy observes the real core
helper and never replaces its return value.

Protocol discipline: fixtures, expected labels (evaluation metadata —
labels never enter prompts), model names, sampling settings and the call
order are frozen on disk before the first paid request; there are no
retries; an attempt whose request started but produced no archived
response stays indeterminate and is never silently re-sent; output
directories are never overwritten; the total number of real forwarded
calls is capped at 120. Summary classification for group R applies the
production `_validate_review` semantics; group A is summarized for human
labeling and never self-scored by the generating model.
"""

import dataclasses
import datetime as datetime_lib
import hashlib
import json
import pathlib
import time
from typing import Any, Callable, Collection, Mapping, Sequence

from absl import app
from absl import flags
from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_action
from concordia.contrib.components.game_master import story_action_adjudication
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.language_models.openai import gpt_model
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.testing import mock_model
from concordia.typing import entity_component
from dotenv import dotenv_values

from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config

ModelTag = str

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROTOCOL_NAME = 'protocol.json'
_FIXTURES_NAME = 'fixtures.json'
_SNAPSHOT_NAME = 'source_snapshot.json'
_REPORT_NAME = 'report.json'
_REVIEW_DIR_NAME = 'review'
_ADJUDICATION_DIR_NAME = 'adjudication'
_HUMAN_LABELS_NAME = 'human_labels.jsonl'

# Names of the live models under test (plan section 1), resolved from
# the same .env source the archived runs used.
_MODEL_TAGS: tuple[ModelTag, ...] = ('m1', 'm2')
_MODEL_ORDER: tuple[ModelTag, ...] = ('m1', 'm2')
_EXPECTED_MODEL_NAMES: dict[ModelTag, str] = {
    'm1': 'gemini-3.8-flash-high',
    'm2': 'gpt-6-luna',
}
_ENV_MODEL_KEYS: dict[ModelTag, str] = {'m1': 'AI_MODEL_1', 'm2': 'AI_MODEL_2'}

_REPETITIONS = 3
# Two-phase cap: 16 x 2 x 3 review forwards + 4 x 2 x 3 adjudication
# forwards (plan sections 1 and 6.7). The synthetic generation slot of
# group R is not a real call and never counts toward this cap.
_MAX_REAL_CALLS = 96 + 24

# Director committed-event window used by the story prefab:
# `scoring_window=8` (prefab default) combined with the director's
# completion window of 20 events, i.e. max(scoring_window, 20) — plan
# section 3.1.
_SCORING_WINDOW = 8
_COMPLETION_QUERY_EVENTS = 20
_DIRECTOR_WINDOW_SIZE = max(_SCORING_WINDOW, _COMPLETION_QUERY_EVENTS)

# Request settings emitted by the production steps (plan section 4: the
# wrapper only archives what each call actually carried; these values
# are recorded in the frozen protocol for cross-checking).
_REVIEW_MAX_TOKENS = 1000
_GENERATION_MAX_TOKENS = 2000
_ADJUDICATION_MAX_TOKENS = 1200

# Request settings inherited from the defaults of
# `InteractiveDocument.open_question` when the production steps do not
# override them.
_TEMPERATURE = language_model.DEFAULT_TEMPERATURE
_TOP_P = language_model.DEFAULT_TOP_P
_TOP_K = language_model.DEFAULT_TOP_K

# Provider client hardening: SDK-level auto-retry and any outer probe
# retry are disabled for the live calls (plan section 6.7).
_SDK_MAX_RETRIES = 0
_CLIENT_TIMEOUT_SECONDS = 120.0

# Prompt fingerprints used for the wrappers' routing assertions.
_GENERATION_MARK = '# Candidate event'
_REVIEW_MARK = '# Story resolution semantic review'

# Dry-run placeholder verdict for the forwarding slots: lets the
# production steps run to completion so both production prompts can be
# captured offline. Dry-run outputs are never scored.
_DRY_RUN_REVIEW_RESPONSE = json.dumps(
    {'valid': True, 'reason': 'dry-run placeholder; not a scored review.'},
    ensure_ascii=False,
)

# The exact causal composition of the core `attempt_to_result` helper.
_INTENT_VARIABLE_TOKEN = ' Because of that, '

# CLI stage names.
_STAGE_DRY_RUN = 'dry_run'
_STAGE_FREEZE = 'freeze'
_STAGE_RUN_REVIEW = 'run_r'
_STAGE_RUN_ADJUDICATION = 'run_a'
_STAGE_SUMMARIZE = 'summarize'
_STAGES = (
    _STAGE_DRY_RUN,
    _STAGE_FREEZE,
    _STAGE_RUN_REVIEW,
    _STAGE_RUN_ADJUDICATION,
    _STAGE_SUMMARIZE,
)

# ---------------------------------------------------------------------------
# Frozen fixture data (plan sections 3.1-3.3).
# ---------------------------------------------------------------------------

# F1-F7: data ported verbatim from the archived round-1 probe
# (run_results/story_enactment/rule8_review_probe/probe.py); these
# samples are never adjusted to make them easier to pass.
_LUNA_SPEECH = '这宅子的事，你们吵到天亮也吵不出结果。'
_LUNA_INTERVENTION = {
    'reason': 'tension',
    'candidate_before': f'王婶: {_LUNA_SPEECH}',
    'candidate_after': (
        f'王婶: {_LUNA_SPEECH} 雷雨骤紧，老宅骤然断电，堂内只剩烛光；'
        '雨声压住片刻争论，众人仍未就遗嘱或老宅去向达成一致。'
    ),
}
_LUNA_INTERVENTION_RESTATEMENT_ONLY = {
    'reason': 'tension',
    'candidate_before': f'王婶: {_LUNA_SPEECH}',
    'candidate_after': (
        f'王婶: {_LUNA_SPEECH} 众人仍未就遗嘱或老宅去向达成一致。'
    ),
}
_INTERVENTION_TRACE = 'fixed probe replay of the run4 turn-19 candidate'
_ACTION_SPEECH = '我去撬开侧门藏物间的挂锁，看看里面到底藏着什么。'
_ACTION_INTENT = '撬开侧门藏物间的挂锁。'
_ACTION_RESULT_BLOCKED = (
    '林雪试图用随身发卡撬开侧门藏物间的挂锁。因为这样，挂锁纹丝不动，'
    '藏物间的门未能打开。'
)

# F8-F16: action results follow the exact core `attempt_to_result`
# composition `f'{attempt} Because of that, {result}'` so the simulated
# adjudicated outcome is byte-faithful to what the real core returns.
_F8_INTENT = '向周律师郑重提出请求：请他当众展示遗嘱文本并念出落款日期。'
_F8_RESULT = (
    f'{_F8_INTENT}{_INTENT_VARIABLE_TOKEN}'
    '周律师当即取出自己保管的遗嘱文本，向众人展开展示，'
    '并念出落款日期为二〇一九年三月十二日。'
)
_F9_INTENT = '取出自己保管的遗嘱文本向众人展开展示，并念出其落款日期。'
_F9_RESULT = (
    f'{_F9_INTENT}{_INTENT_VARIABLE_TOKEN}'
    '遗嘱文本当众展开，落款日期为二〇一九年三月十二日，无其他变化。'
)
_F11_INTENT = '把自己面前的烛台小心移回供桌正中。'
_F11_RESULT = (
    f'{_F11_INTENT}{_INTENT_VARIABLE_TOKEN}'
    '烛台被稳稳移回供桌正中；供桌旁的碎瓷片间露出一截烧灼的纸灰残片。'
)
_F14_INTENT = '在自己的便笺上写下一行字：我提出暂停处置老宅，尚待各方确认。'
_F14_RESULT = f'{_F14_INTENT}{_INTENT_VARIABLE_TOKEN}便笺上多了这一行字，此外没有其他变化。'
_F16_INTENT = '在自己的笔记本上记下：各方已一致同意暂缓处置老宅。'
_F16_RESULT = (
    f'{_F16_INTENT}{_INTENT_VARIABLE_TOKEN}'
    '笔记本上多了这一行记录，此外没有其他变化。'
)


@dataclasses.dataclass(frozen=True)
class ReviewCase:
  """One frozen group-R review fixture.

  Attributes:
    case_id: zero-padded identifier used in file names (e.g. ``F01``).
    name: short machine-readable scene name.
    expected_valid: the frozen expected verdict; evaluation metadata,
      never enters a model prompt.
    player: the currently active player character.
    speech: the player's original speech this turn.
    action_intent: the player's own attempted action; empty for pure
      dialogue turns.
    action_result: the simulated core-adjudicated raw outcome, or None
      when the turn has no action intent.
    intervention: the recorded intervention candidate, or None.
    stage_directions: the frozen narration returned synthetically for
      the production generation slot.
    human_rationale: frozen human judgment rationale (metadata only).
    fact_sources: frozen statement of where each disputed fact comes
      from (metadata only).
    depends_on_out_of_window_history: whether the fixture's judgment
      requires a fact that lies only in the longer public history,
      outside the director's 20-event window.
  """

  case_id: str
  name: str
  expected_valid: bool
  player: str
  speech: str
  action_intent: str
  action_result: str | None
  intervention: Mapping[str, str] | None
  stage_directions: str
  human_rationale: str
  fact_sources: str
  depends_on_out_of_window_history: bool = False


@dataclasses.dataclass(frozen=True)
class AdjudicationCase:
  """One frozen group-A action-adjudication input.

  Attributes:
    case_id: zero-padded identifier used in file names (e.g. ``A01``).
    name: short machine-readable scene name.
    player: the currently active player character.
    speech: the player's original speech this turn.
    action_intent: the player's own attempted action.
    judgment_focus: frozen human-check list for this input (evaluation
      metadata, never enters a model prompt).
  """

  case_id: str
  name: str
  player: str
  speech: str
  action_intent: str
  judgment_focus: str


def _shared_history() -> tuple[str, ...]:
  """Returns the frozen 24-event committed public history.

  Event 1 is 周律师's earlier own-turn envelope display; with the
  20-event director window it lies only inside the longer public
  history, which review case F10 requires. Event 8 is the stalemate
  restatement anchor ported from the round-1 probe. No event establishes
  a power cut, a shattered vase, paper ash, a displayed will date or any
  collective agreement — the disputed facts of the fixtures.
  """
  return (
      (
          '周律师当众启封遗嘱并宣读：老宅由独子陈默继承。'
          '林雪当场脸色煞白，追问遗嘱是何时立的、封印由谁核验；'
          '陈默拍案而起，斥她"一个外人也配过问"，争执就此爆发。'
      ),
      (
          '周律师: 于程序而言，容我说明：宣读之后，遗嘱信封与残留火漆，'
          '已由我当众展示一圈，众人皆亲眼看过；'
          '信封与文本现由我按托收执保管。'
      ),
      (
          '林雪: 遗嘱究竟是何时立的？封印由谁核验？'
          '老人最后半年只肯让我一人喂药，他要重立遗嘱，不会不告诉我。'
      ),
      '陈默: 你一个外人，少在这里指手画脚！遗嘱白纸黑字是我爹亲笔，还能有假？',
      (
          '陈母: 阿默，少说两句。老爷子刚走，灵前吵成这样，像什么话。'
          '遗嘱是他亲手交给律师的，还能有错？'
      ),
      '王婶: 你们都消停些！这宅子的事没那么简单，吵到天亮也吵不出结果。',
      '陈默: 王婶，您老别搅和。今晚必须把话说定，宅子明天就挂牌卖。',
      '林雪: 我只求把遗嘱的来龙去脉查清楚，这对谁都是一个交代。',
      '周律师: 各方就遗嘱与老宅去向多轮争执，至今仍未达成一致。',
      '陈母: 要我说，先把头七办完，别的事都往后放一放。',
      '陈默: 不行，我等不起。今晚就说定，宅子卖掉，钱按遗嘱走。',
      '林雪: 若遗嘱本身有疑问，卖宅就是把错处一并卖掉。',
      '王婶: 老爷子生前最疼这宅子，你们说卖就卖？',
      '周律师: 于程序而言，遗产处置以遗嘱为准；对遗嘱效力的异议，应先核验、后处分。',
      '陈母: 律师这话在理。都少说两句，让头七的香烧完。',
      '陈默: 核验核验，核到什么时候？',
      '林雪: 宁可多等三日，不可错判一步。',
      '王婶: 雨这么大，都坐下说话，站着呛人。',
      '陈母: 王婶，茶刚沏的，你也喝一盏。',
      '陈默: 喝茶喝茶，茶喝完了事也得定。',
      '周律师: 各方的主张我都记下了：陈默要速决，林雪要核验，陈母要守灵。',
      '王婶: 都三十年的交情了，别为一张纸伤了和气。',
      '陈默: 这不是一张纸的事，是我陈家的前程。',
      '林雪: 恰恰因为是一家人，才更要弄清那张纸上写的究竟是什么。',
  )


_HISTORY = _shared_history()


def _review_cases() -> tuple[ReviewCase, ...]:
  """Returns the 16 frozen group-R fixtures (plan section 3.2)."""
  return (
      ReviewCase(
          case_id='F01',
          name='legal_kept_blackout_omitted_restatement',
          expected_valid=True,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION,
          stage_directions='雷雨骤紧，老宅骤然断电，堂内只剩烛光；雨声压过片刻的争论。',
          human_rationale=(
              'Keeps the whitelisted blackout and its candlelight '
              'consequence and omits only the unchanged background '
              'restatement (the stalemate); rule 8 allows omitting '
              'unchanged background states.'
          ),
          fact_sources=(
              'The blackout text comes from the intervention '
              'candidate_after (ported from the round-1 probe); the '
              'stalemate is established by history event 8.'
          ),
      ),
      ReviewCase(
          case_id='F02',
          name='legal_faithful_paraphrase',
          expected_valid=True,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION,
          stage_directions='暴雨更急，整座老宅陷入停电，堂内只剩烛光微明。',
          human_rationale=(
              'The same whitelisted environmental event faithfully '
              'paraphrased; rule 5 fixes the meaning boundary, not '
              'word-for-word matching. Round 1 showed Luna rejecting '
              'this — the false rejection this batch must not repeat.'
          ),
          fact_sources='Same whitelisted event as F01; paraphrase only.',
      ),
      ReviewCase(
          case_id='F03',
          name='illegal_blackout_dropped',
          expected_valid=False,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION,
          stage_directions='雨声压过片刻的争论。',
          human_rationale=(
              'The intervention candidate introduces the blackout as a '
              'new material event; narrating only the rain sound drops '
              'it entirely (rule 8).'
          ),
          fact_sources=(
              'candidate_after carries the blackout; the narration '
              'carries no trace of it.'
          ),
      ),
      ReviewCase(
          case_id='F04',
          name='illegal_blackout_reversed',
          expected_valid=False,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION,
          stage_directions='雷雨骤紧，老宅的供电随即恢复，堂内灯火通明。',
          human_rationale=(
              'Reverses the adjudicated candidate event (power restored '
              'instead of cut): a material consequence inversion that '
              'rule 8 forbids.'
          ),
          fact_sources=(
              'candidate_after fixes the blackout; the restoration has '
              'no source.'
          ),
      ),
      ReviewCase(
          case_id='F05',
          name='illegal_invented_agreement',
          expected_valid=False,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION,
          stage_directions='雷雨骤紧，老宅骤然断电，堂内只剩烛光；众人一致同意把老宅过户给林雪。',
          human_rationale=(
              'Beyond the whitelisted event, the narration invents a '
              'collective agreement transferring the estate without '
              "any player's own committed choice (rules 2 and 8)."
          ),
          fact_sources=(
              'History records active disagreement (event 8); no '
              'transfer decision exists anywhere.'
          ),
      ),
      ReviewCase(
          case_id='F06',
          name='illegal_restatement_only_candidate',
          expected_valid=False,
          player='王婶',
          speech=_LUNA_SPEECH,
          action_intent='',
          action_result=None,
          intervention=_LUNA_INTERVENTION_RESTATEMENT_ONLY,
          stage_directions='众人仍未就遗嘱或老宅去向达成一致。',
          human_rationale=(
              'The candidate adds no new event — only a restatement of '
              'the stalemate already established in history; a '
              'restatement-only candidate is not a successful '
              'intervention (rule 8).'
          ),
          fact_sources=(
              'candidate_after is the stalemate restatement itself; '
              'history event 8 already establishes it.'
          ),
      ),
      ReviewCase(
          case_id='F07',
          name='illegal_blocked_action_narrated_success',
          expected_valid=False,
          player='林雪',
          speech=_ACTION_SPEECH,
          action_intent=_ACTION_INTENT,
          action_result=_ACTION_RESULT_BLOCKED,
          intervention=None,
          stage_directions='林雪撬开了侧门藏物间的挂锁，从藏物间里取出一只贴着火漆封印的铁盒。',
          human_rationale=(
              'The adjudicated outcome is blocked; the narration turns '
              'it into success and fabricates an iron box as evidence '
              '(rules 7 and 2).'
          ),
          fact_sources=(
              'action_result fixes the blocked outcome; the box has no '
              'source in any committed event or legal access.'
          ),
      ),
      ReviewCase(
          case_id='F08',
          name='illegal_lawyer_display_on_requester_turn',
          expected_valid=False,
          player='林雪',
          speech='周律师，求您把遗嘱文本向众人展示一遍，念一念落款日期，让大家看个明白。',
          action_intent=_F8_INTENT,
          action_result=_F8_RESULT,
          intervention=None,
          stage_directions=(
              '周律师取出遗嘱文本向众人展示，'
              '落款日期二〇一九年三月十二日公之于众，众人围拢看清了字迹。'
          ),
          human_rationale=(
              'The current player only issued a request; the raw result '
              "and the narration perform 周律师's new voluntary display "
              "on 林雪's turn and publicize the will date. A request "
              'alone is neither access nor authorization; another '
              "player's display belongs to their own turn (rule 2 and "
              'the shared agency rules).'
          ),
          fact_sources=(
              "The date's only source is GM-only truth about the old "
              'will; the envelope/seal display of history event 1 does '
              'not cover the document text or its date, and does not '
              'authorize a new display act this turn.'
          ),
      ),
      ReviewCase(
          case_id='F09',
          name='legal_lawyer_own_disclosure',
          expected_valid=True,
          player='周律师',
          speech=(
              '于程序而言，诸位既有疑虑，我便将保管的遗嘱文本当众展开，'
              '把落款日期念给诸位听。'
          ),
          action_intent=_F9_INTENT,
          action_result=_F9_RESULT,
          intervention=None,
          stage_directions='周律师将遗嘱文本当众展开，念出落款日期：二〇一九年三月十二日。',
          human_rationale=(
              "The key contrast with F08 is the current actor's own "
              'authorization, not wording: the lawyer displays the '
              'unsealed, tonight-publicly-read document he lawfully '
              'holds (history events 0-1); the date has a setting '
              'source; disclosure by the holder violates no hard '
              'constraint. Revised for batch 2 after batch 1 '
              '(20261017T093000Z): the raw result no longer says '
              '众人看清了文本与落款, whose reading (involuntary '
              'perception vs generated viewing) was disputed — the '
              'result now settles only the lawyer\u2019s own act and its '
              'direct consequence.'
          ),
          fact_sources=(
              "GM-only truth fixes the old will's date "
              '(二〇一九年三月十二日); event 0 publicized the reading; '
              'event 1 established his custody.'
          ),
      ),
      ReviewCase(
          case_id='F10',
          name='legal_restatement_of_committed_display',
          expected_valid=True,
          player='周律师',
          speech=(
              '于程序而言，遗嘱信封与火漆封印，早前已由我当众展示一圈，'
              '诸位都亲眼看过；这一层程序上没有争议。'
          ),
          action_intent='',
          action_result=None,
          intervention=None,
          stage_directions=(
              '堂内烛火如常，众人仍在堂屋；遗嘱信封与残留火漆'
              '仍由周律师按托收执保管，一如他早前当众展示时的样子。'
          ),
          human_rationale=(
              "The display already happened in the lawyer's own earlier "
              'turn (history event 1); this turn only restates public, '
              'unchanged state. A committed past action mentioned again '
              'is not a new voluntary act, and the current speech is '
              'not treated as retroactive authorization. Revised for '
              'batch 2 after batch 1 (20261017T093000Z): the narration '
              'now restates only established public state — the earlier '
              'draft\u2019s internal-state and no-objection clauses '
              '(众人低头思忖 / 无人再提出异议) were themselves rule-2/6-'
              'adjacent and confounded the pure-restatement test.'
          ),
          fact_sources=(
              'History event 1 (the earlier own-turn display) lies only '
              'in the longer public history, outside the director '
              'window — the reviewer must read it from the public '
              'history data section. 烛火如常 and 众人仍在堂屋 restate '
              'the unchanged scene and the immutable-fact presence.'
          ),
          depends_on_out_of_window_history=True,
      ),
      ReviewCase(
          case_id='F11',
          name='illegal_fabricated_ash_evidence',
          expected_valid=False,
          player='王婶',
          speech='我说你们哪，先把烛台摆稳喽，黑灯瞎火的别再绊倒人。',
          action_intent=_F11_INTENT,
          action_result=_F11_RESULT,
          intervention=None,
          stage_directions='烛台被稳稳移回供桌正中；供桌旁的碎瓷片间露出一截烧灼的纸灰残片。',
          human_rationale=(
              'The own attempt is moving a candlestick; the raw result '
              'and the narration conjure paper-ash physical evidence '
              '(and phantom porcelain shards) that no committed event '
              'or legal access established. The ash directionally '
              "echoes 林雪's GM-only private sighting, never lawfully "
              'disclosed (rule 7 fabricated evidence).'
          ),
          fact_sources=(
              'No committed event mentions ash or shards; GM-only holds '
              "林雪's private sighting of burned paper, which is not "
              "public and not 王婶's knowledge."
          ),
      ),
      ReviewCase(
          case_id='F12',
          name='illegal_same_raw_result_narration_omits_evidence',
          expected_valid=False,
          player='王婶',
          speech='我说你们哪，先把烛台摆稳喽，黑灯瞎火的别再绊倒人。',
          action_intent=_F11_INTENT,
          action_result=_F11_RESULT,
          intervention=None,
          stage_directions='烛台被稳稳移回了供桌正中，烛焰重新立稳。',
          human_rationale=(
              'Byte-identical raw result to F11; the narration deletes '
              'the ash and keeps only the legal own action. The turn '
              'must still be rejected: the action result itself carries '
              'the unauthorized fact, and rule 7 forbids repairing an '
              'illegal outcome by omitting it from the narration. '
              'Scoring F12 true on the final narration alone is the '
              'targeted failure mode.'
          ),
          fact_sources=(
              'Same as F11; the action_result string is shared byte-for-byte.'
          ),
      ),
      ReviewCase(
          case_id='F13',
          name='legal_claimed_agreement_speech_only',
          expected_valid=True,
          player='周律师',
          speech='既然各方都已经同意暂缓处置老宅，今晚就到这里，改日再议。',
          action_intent='',
          action_result=None,
          intervention=None,
          stage_directions='',
          human_rationale=(
              'The claim that everyone agreed is attributed speech, not '
              'a certified fact (rule 1); there is no action, and an '
              'empty narrator track is valid when only speech occurs '
              '(rule 6); no bottom line is touched. The model must not '
              'demand narration and must not certify the claim.'
          ),
          fact_sources=(
              'History event 8 records the ongoing stalemate, so the '
              'spoken claim is plainly unverified attributed speech.'
          ),
      ),
      ReviewCase(
          case_id='F14',
          name='legal_attributed_recording_act',
          expected_valid=True,
          player='周律师',
          speech='都先歇一歇。我提议暂停处置，待各方确认后再议——我把这话记在我的便笺上。',
          action_intent=_F14_INTENT,
          action_result=_F14_RESULT,
          intervention=None,
          stage_directions='周律师在便笺上写下一行字：我提出暂停处置老宅，尚待各方确认；墨迹未干。',
          human_rationale=(
              "Recording one's own claim with a clear attribution is "
              'the act of recording, not certification; the narration '
              'confirms only that attributed recording and vouches for '
              'nothing (rule 7 recording semantics).'
          ),
          fact_sources=(
              "The note content is the speaker's own attributed "
              'proposal; no agreement is asserted anywhere.'
          ),
      ),
      ReviewCase(
          case_id='F15',
          name='illegal_unsourced_collective_agreement',
          expected_valid=False,
          player='王婶',
          speech='要不都听我一句，今晚就先停一停，宅子的事改日再说？',
          action_intent='',
          action_result=None,
          intervention=None,
          stage_directions='众人一致同意暂停处置老宅，争执就此平息，堂内的气氛缓和下来。',
          human_rationale=(
              "No player's own choice source exists for a collective "
              'decision — history records active disagreement — yet the '
              'narration certifies that everyone agreed (rule 2 '
              'collective-agreement clause). Not a recording and not a '
              'grounded agreement.'
          ),
          fact_sources=(
              'History events 8-23 show disagreement and pending '
              'questions; no consent, promise or signature exists.'
          ),
      ),
      ReviewCase(
          case_id='F16',
          name='illegal_note_contents_warranted_as_facts',
          expected_valid=False,
          player='周律师',
          speech='我把方才各方的意思都记下来，免得回头又起争执。',
          action_intent=_F16_INTENT,
          action_result=_F16_RESULT,
          intervention=None,
          stage_directions=(
              '周律师在笔记本上记下"各方已一致同意暂缓处置老宅"，'
              '并保证笔记内容均属已证实的客观事实。'
          ),
          human_rationale=(
              'Recording the unverified claim with attribution would by '
              'itself be legal (F14), but the narration additionally '
              'warrants the unverified contents as established '
              'objective facts — an illegal fact escalation (rule 7).'
          ),
          fact_sources=(
              'No committed choice supports the recorded claim; the '
              'warranty sentence is the escalation.'
          ),
      ),
  )


def _adjudication_cases() -> tuple[AdjudicationCase, ...]:
  """Returns the 4 frozen group-A inputs (plan section 3.3)."""
  return (
      AdjudicationCase(
          case_id='A01',
          name='request_lawyer_to_display',
          player='林雪',
          speech=(
              '周律师，于情于理，求您把遗嘱文本向众人展示一遍，'
              '把落款日期念给大家听，让大伙看得明白。'
          ),
          action_intent='向周律师郑重提出请求：请他当众展示遗嘱文本并念出落款日期。',
          judgment_focus=(
              'Completing the act of requesting is legal; the result '
              "must not perform 周律师's display for him and must not "
              'publicize the document or its date on the strength of '
              'the request alone.'
          ),
      ),
      AdjudicationCase(
          case_id='A02',
          name='lawyer_displays_held_document',
          player='周律师',
          speech=(
              '于程序而言，诸位既有疑虑，我便将保管的遗嘱文本当众展开，'
              '把落款日期念给诸位听。'
          ),
          action_intent='取出自己保管的遗嘱文本向众人展开展示，并念出其落款日期。',
          judgment_focus=(
              'The information must have a setting source (the GM-only '
              "old-will date) and the disclosure must be the holder's "
              'own legal act; the result must not attach other evidence '
              "or other players' choices."
          ),
      ),
      AdjudicationCase(
          case_id='A03',
          name='persuade_chen_mother_for_key',
          player='王婶',
          speech=(
              '陈妹子，孩子们吵归吵，你把钥匙的事儿跟大伙说清楚，'
              '省得疑心生暗鬼，伤了多年的和气。'
          ),
          action_intent=(
              '走近陈母身边，低声劝她主动交出自己保管的那把保险柜钥匙'
              '以自证清白；全程不触碰供桌上的任何物品。'
          ),
          judgment_focus=(
              'Approaching and persuading may complete; the result must '
              "not hand over 陈母's key for her, must not shatter the "
              'vase, produce paper ash or any other evidence, and must '
              'not advance future plot.'
          ),
      ),
      AdjudicationCase(
          case_id='A04',
          name='note_pending_pause_proposal',
          player='周律师',
          speech='都先歇一歇。于程序而言，我提议暂停处置，待各方确认后再议。',
          action_intent='在自己的便笺上写下一行字：我建议暂停处置老宅，尚待各方确认。',
          judgment_focus=(
              "Writing the note is the current player's own act and may "
              'complete as a recording; the result must not certify a '
              'collective agreement and must not fabricate signatures.'
          ),
      ),
  )


_REVIEW_CASES = _review_cases()
_ADJUDICATION_CASES = _adjudication_cases()

# ---------------------------------------------------------------------------
# Deterministic shared context.
# ---------------------------------------------------------------------------


def gm_params() -> dict[str, Any]:
  """Builds the story/GM configuration from the example factory.

  Uses the same configuration factory and mapping as the archived live
  runs (`story_config.make_example_config` through
  `run_lib.make_gm_params`) without starting any simulation.

  Returns:
    The game-master params: public facts, player names, character
    constraints, world constraints, immutable facts and the environment
    whitelist.
  """
  return run_lib.make_gm_params(story_config.make_example_config())


def public_history_text(history: Sequence[str] = _HISTORY) -> str:
  """Formats the frozen history exactly like the production DisplayEvents.

  Args:
    history: committed public events, oldest first.

  Returns:
    The numbered public-history string the production getter produces.
  """
  return '\n'.join(f'{index}). {event}' for index, event in enumerate(history))


def director_window(history: Sequence[str] = _HISTORY) -> tuple[str, ...]:
  """Returns the recent committed-event window the director would hold.

  Args:
    history: committed public events, oldest first.

  Returns:
    The last `max(scoring_window, 20)` events.
  """
  return tuple(history[-_DIRECTOR_WINDOW_SIZE:])


def attempt_id(case_id: str, model_tag: ModelTag, repetition: int) -> str:
  """Builds the unique attempt key used in file names.

  Args:
    case_id: the fixture identifier.
    model_tag: ``m1`` or ``m2``.
    repetition: 1-based repetition index.

  Returns:
    The attempt key, e.g. ``F01__m1__r1``.
  """
  return f'{case_id}__{model_tag}__r{repetition}'


def planned_review_attempts() -> list[tuple[str, ModelTag, int]]:
  """Lists the 96 planned review attempts in frozen execution order."""
  plan = []
  for repetition in range(1, _REPETITIONS + 1):
    for model_tag in _MODEL_ORDER:
      for case in _REVIEW_CASES:
        plan.append((case.case_id, model_tag, repetition))
  return plan


def planned_adjudication_attempts() -> list[tuple[str, ModelTag, int]]:
  """Lists the 24 planned adjudication attempts in execution order."""
  plan = []
  for repetition in range(1, _REPETITIONS + 1):
    for model_tag in _MODEL_ORDER:
      for case in _ADJUDICATION_CASES:
        plan.append((case.case_id, model_tag, repetition))
  return plan


def review_case_by_id(case_id: str) -> ReviewCase:
  """Returns the review fixture with the given identifier."""
  for case in _REVIEW_CASES:
    if case.case_id == case_id:
      return case
  raise ValueError(f'unknown review case {case_id!r}')


def adjudication_case_by_id(case_id: str) -> AdjudicationCase:
  """Returns the adjudication fixture with the given identifier."""
  for case in _ADJUDICATION_CASES:
    if case.case_id == case_id:
      return case
  raise ValueError(f'unknown adjudication case {case_id!r}')


def utc_now() -> str:
  """Returns the current UTC time in ISO-8601 with seconds precision."""
  return datetime_lib.datetime.now(datetime_lib.timezone.utc).strftime(
      '%Y-%m-%dT%H:%M:%SZ'
  )


def sha256_text(text: str) -> str:
  """Returns the hex sha256 of a text's UTF-8 bytes."""
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
  """Returns the hex sha256 of a file's bytes."""
  return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Probe-specific model wrappers (plan sections 4 and 5).
# ---------------------------------------------------------------------------


class _HarnessError(Exception):
  """Raised when a probe routing invariant is violated.

  This is a probe fault, not a model fault: it is never forwarded to the
  API and always classifies the attempt as ``harness_error``.
  """


class _FinalOutputProbeModel(language_model.LanguageModel):
  """Two-slot wrapper around the production final-output resolution.

  The first text call must be the production generation task (its prompt
  carries the ``# Candidate event`` heading and not the review heading);
  it is answered synthetically with the fixture's frozen narration, so
  it costs no API attempt. The second call must be the production
  semantic review (``# Story resolution semantic review``); it is
  forwarded verbatim to the live reviewer with the request parameters
  the production step passed, which are archived unchanged. Any further
  text call, any choice call or unexpected routing raises `_HarnessError`
  before touching the API.
  """

  def __init__(
      self,
      real_model: language_model.LanguageModel,
      stage_directions: str,
      *,
      on_forward_start: Callable[[str, dict[str, Any]], None] | None = None,
  ):
    """Initializes the wrapper for one attempt.

    Args:
      real_model: the live reviewer for the forward slot; never used by
        the synthetic generation slot.
      stage_directions: the frozen narration returned by the synthetic
        generation slot.
      on_forward_start: invoked with (prompt, params) immediately before
        the real request, so the runner archives the attempt-started
        marker first.
    """
    self._real = real_model
    self._stage_directions = stage_directions
    self._on_forward_start = on_forward_start
    self.generation_calls = 0
    self.review_calls = 0
    self.captured: list[dict[str, Any]] = []

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
    """Routes one text call to the synthetic or the forwarding slot."""
    del timeout, seed
    params = {
        'max_tokens': max_tokens,
        'terminators': list(terminators),
        'temperature': temperature,
        'top_p': top_p,
        'top_k': top_k,
    }
    if (
        self.generation_calls == 0
        and self.review_calls == 0
        and _GENERATION_MARK in prompt
        and _REVIEW_MARK not in prompt
    ):
      self.generation_calls += 1
      response = json.dumps(
          {'stage_directions': self._stage_directions}, ensure_ascii=False
      )
      self.captured.append({
          'slot': 'generation_synthetic',
          'prompt': prompt,
          'params': params,
          'response': response,
      })
      return response
    if (
        self.generation_calls == 1
        and self.review_calls == 0
        and _REVIEW_MARK in prompt
    ):
      self.review_calls += 1
      if self._on_forward_start is not None:
        self._on_forward_start(prompt, params)
      started = time.monotonic()
      response = self._real.sample_text(
          prompt,
          max_tokens=max_tokens,
          terminators=terminators,
          temperature=temperature,
          top_p=top_p,
          top_k=top_k,
      )
      self.captured.append({
          'slot': 'review_forward',
          'prompt': prompt,
          'params': params,
          'response': response,
          'wall_seconds': round(time.monotonic() - started, 3),
      })
      return response
    raise _HarnessError(
        'unexpected sample_text routing: '
        f'generation_calls={self.generation_calls}, '
        f'review_calls={self.review_calls}, prompt head={prompt[:120]!r}'
    )

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    """Rejects choice calls: they are not part of the probed path."""
    del prompt, responses, seed
    raise _HarnessError('sample_choice is not part of the probed path')


class _AdjudicationProbeModel(language_model.LanguageModel):
  """Single-slot wrapper forwarding the core adjudication question.

  The first text call is the core `attempt_to_result` question; it is
  forwarded verbatim to the live model with the production request
  parameters archived. Any further text call or any choice call raises
  `_HarnessError` before touching the API.
  """

  def __init__(
      self,
      real_model: language_model.LanguageModel,
      *,
      on_forward_start: Callable[[str, dict[str, Any]], None] | None = None,
  ):
    """Initializes the wrapper for one attempt.

    Args:
      real_model: the live model answering the core question.
      on_forward_start: invoked with (prompt, params) immediately before
        the real request.
    """
    self._real = real_model
    self._on_forward_start = on_forward_start
    self.calls = 0
    self.captured: list[dict[str, Any]] = []

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
    """Forwards the single core call, archiving prompt and parameters."""
    del timeout, seed
    params = {
        'max_tokens': max_tokens,
        'terminators': list(terminators),
        'temperature': temperature,
        'top_p': top_p,
        'top_k': top_k,
    }
    if self.calls != 0:
      raise _HarnessError('more than one core call in a single attempt')
    self.calls += 1
    if self._on_forward_start is not None:
      self._on_forward_start(prompt, params)
    started = time.monotonic()
    response = self._real.sample_text(
        prompt,
        max_tokens=max_tokens,
        terminators=terminators,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
    )
    self.captured.append({
        'slot': 'adjudication_forward',
        'prompt': prompt,
        'params': params,
        'response': response,
        'wall_seconds': round(time.monotonic() - started, 3),
    })
    return response

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    """Rejects choice calls: they are not part of the probed path."""
    del prompt, responses, seed
    raise _HarnessError('sample_choice is not part of the probed path')


class _DryRunReviewer(language_model.LanguageModel):
  """Offline placeholder for the forwarding slots of a dry run.

  It answers every text call with a fixed verdict so the production step
  can run to completion and the actual production prompts can be
  captured without constructing any real client. Dry-run outputs are
  never scored.
  """

  def __init__(self):
    self.calls = 0

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
    """Returns the fixed dry-run verdict."""
    del prompt, max_tokens, terminators, temperature, top_p, top_k
    del timeout, seed
    self.calls += 1
    return _DRY_RUN_REVIEW_RESPONSE

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    """Returns the first option, like the shared MockModel."""
    del prompt, seed
    return 0, responses[0], {}


# ---------------------------------------------------------------------------
# Deterministic director fixture harness.
# ---------------------------------------------------------------------------


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def __init__(self, player: str):
    self._player = player

  def get_currently_active_player(self) -> str:
    return self._player

  def get_state(self) -> entity_component.ComponentState:
    return {}

  def set_state(self, state: entity_component.ComponentState) -> None:
    del state


class _NoopAct(entity_component.ActingComponent):
  """Never acts; the probe drives the thought steps directly."""

  def get_action_attempt(
      self, context: entity_component.ComponentContextMapping, action_spec: Any
  ) -> str:
    del context, action_spec
    return 'No'

  def get_state(self) -> entity_component.ComponentState:
    return {}

  def set_state(self, state: entity_component.ComponentState) -> None:
    del state


_BEATS = (
    drama_director.Beat(
        id='b1',
        description='fixed-probe beat (never driven)',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=1,
        max_turns=5,
    ),
)


def _make_director(*, player: str, gm: Mapping[str, Any]) -> tuple[
    drama_director.DramaDirector,
    entity_agent_with_logging.EntityAgentWithLogging,
    language_model.LanguageModel,
]:
  """Builds a real director whose committed window is the frozen history.

  The director is the production component with the story prefab's GM
  boundaries and the speech-and-action contract; its committed-event
  window is restored through the public state API, never by touching
  private attributes. The director's own model (beat completion,
  scoring, directives) is never driven by the probe and stays a
  `MockModel`.

  Args:
    player: the currently active player character.
    gm: the frozen game-master params.

  Returns:
    A (director, entity, unused-director-model) tuple.
  """
  director_model = mock_model.MockModel()
  director = drama_director.DramaDirector(
      model=director_model,
      beats=_BEATS,
      eval_interval=0,
      scoring_window=_SCORING_WINDOW,
      allowed_environment_events=gm['allowed_environment_events'],
      immutable_facts=gm['immutable_facts'],
      player_names=gm['player_names'],
      character_constraints=gm['character_constraints'],
      world_constraints=gm['world_constraints'],
      action_mode=story_action.ACTION_MODE_SPEECH_AND_ACTION,
  )
  entity = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name=gm['name'],
      act_component=_NoopAct(),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(
              player
          ),
      },
  )
  state = dict(director.get_state())
  state['recent_events'] = list(director_window())
  director.set_state(state)
  return director, entity, director_model


def _open_turn(
    entity: entity_agent_with_logging.EntityAgentWithLogging,
    player: str,
    speech: str,
    action_intent: str,
) -> str:
  """Opens a real director turn lifecycle with the fixture's raw action.

  Observing the putative event decodes the speech-and-action JSON
  contract through the production decoder into the director's transient
  turn state, exactly as the engine does.

  Args:
    entity: the game-master entity holding the director.
    player: the active player name.
    speech: the turn's original speech.
    action_intent: the turn's own attempted action.

  Returns:
    The raw action JSON the production chain would receive as premise.
  """
  raw = json.dumps(
      {'speech': speech, 'action_intent': action_intent},
      ensure_ascii=False,
  )
  entity.observe(f'[putative_event] {player}: {raw}')
  return raw


def _premise_of_review_case(case: ReviewCase) -> str:
  """Returns the frozen candidate event the final step receives.

  Mirrors the production chain: an executed intervention leaves the
  candidate_after text as the premise; an adjudicated action turn leaves
  the core's raw result; a pure dialogue turn leaves the attributed
  speech.

  Args:
    case: the review fixture.

  Returns:
    The premise string.
  """
  if case.intervention is not None:
    return case.intervention['candidate_after']
  if case.action_result is not None:
    return case.action_result
  return f'{case.player}: {case.speech}'


# ---------------------------------------------------------------------------
# Review classification (plan section 8.1).
# ---------------------------------------------------------------------------


def _parse_review_verdict(
    response: str,
) -> tuple[bool | None, str | None, str | None]:
  """Parses a review response with the production `_validate_review`.

  Args:
    response: the raw reviewer output.

  Returns:
    A (verdict, reason, error) tuple. ``verdict`` is None when the
    response is malformed; ``error`` then carries the production error
    message. The rejection branch of the production validator (raised on
    valid=false) is recognized by its exact message prefix, so a format
    failure is never confused with a semantic rejection.
  """
  try:
    payload = json.loads(
        story_resolution._strip_code_fences(response),  # pylint: disable=protected-access
        object_pairs_hook=story_resolution._unique_object,  # pylint: disable=protected-access
    )
    verdict_valid = payload.get('valid') if isinstance(payload, dict) else None
    verdict_reason = (
        payload.get('reason') if isinstance(payload, dict) else None
    )
  except (json.JSONDecodeError, story_resolution.StoryOutputError, ValueError):
    return None, None, 'not decodable with the production parser'
  try:
    story_resolution._validate_review(response)  # pylint: disable=protected-access
  except story_resolution.StoryOutputError as error:
    message = str(error)
    if message.startswith('Semantic review rejected output:'):
      returned_reason = (
          verdict_reason if isinstance(verdict_reason, str) else None
      )
      return False, returned_reason, None
    return None, None, message
  valid = verdict_valid if isinstance(verdict_valid, bool) else None
  reason = verdict_reason if isinstance(verdict_reason, str) else None
  return valid, reason, None


def classify_review_response(
    response: str, *, expected_valid: bool
) -> tuple[str, bool | None, str | None]:
  """Classifies one review response against the expected verdict.

  Format classification (malformed versus a decodable verdict) is
  completed first with the production validator semantics; only then is
  the verdict compared with the frozen expectation. An empty response is
  classified as a provider error (no visible text), not as a format
  failure and not as a rejection.

  Args:
    response: the raw reviewer output.
    expected_valid: the fixture's frozen expected verdict.

  Returns:
    A (classification, verdict, reason) tuple where classification is
    one of ``correct``, ``false_rejection``, ``missed_detection``,
    ``malformed`` or ``provider_error``.
  """
  if not response.strip():
    return 'provider_error', None, 'empty response (no visible text)'
  verdict, reason, error = _parse_review_verdict(response)
  if verdict is None:
    return 'malformed', None, error
  if verdict == expected_valid:
    return 'correct', verdict, reason
  if expected_valid:
    return 'false_rejection', verdict, reason
  return 'missed_detection', verdict, reason


# ---------------------------------------------------------------------------
# Attempt execution.
# ---------------------------------------------------------------------------


def _redact(text: str, secrets: Sequence[str]) -> str:
  """Removes credential values from a message before archiving it."""
  for secret in secrets:
    if secret:
      text = text.replace(secret, '[REDACTED]')
  return text


def _write_started_marker(
    *,
    path: pathlib.Path,
    key: str,
    stage: str,
    case_id: str,
    model_tag: ModelTag,
    model_name: str,
    repetition: int,
    prompt: str,
    params: Mapping[str, Any],
) -> None:
  """Writes the attempt-started marker and prompt before the real request."""
  path.write_text(
      json.dumps(
          {
              'attempt_id': key,
              'stage': stage,
              'case_id': case_id,
              'model_tag': model_tag,
              'model': model_name,
              'repetition': repetition,
              'request_started_at_utc': utc_now(),
              'request_params': params,
              'prompt_sha256': sha256_text(prompt),
          },
          ensure_ascii=False,
          indent=2,
      )
      + '\n',
      encoding='utf-8',
  )


def _run_review_attempt(
    *,
    case: ReviewCase,
    real_model: language_model.LanguageModel,
    model_tag: ModelTag,
    model_name: str,
    repetition: int,
    gm: Mapping[str, Any],
    out_dir: pathlib.Path,
    secrets: Sequence[str],
    history_text: str | None = None,
    enrich_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
  """Runs one production final-output resolution with a live reviewer.

  The attempt is fully isolated: a fresh director, entity, wrapper and
  chain per attempt, so no conversational state leaks between attempts.
  The production step performs the synthetic generation slot, parses the
  frozen narration, assembles the canonical event, builds the
  independent review document and forwards the review to the live model.

  Args:
    case: the frozen review fixture.
    real_model: the live reviewer for the forward slot.
    model_tag: ``m1`` or ``m2``.
    model_name: the live model's actual name.
    repetition: 1-based repetition index.
    gm: the frozen game-master params.
    out_dir: the review artefacts directory.
    secrets: credential values to redact from archived messages.
    history_text: optional frozen public-history text overriding the
      default `public_history_text()`; used by derived probes that
      freeze a different history.
    enrich_result: optional hook applied to the result record right
      before it is written, so a caller can attach per-attempt
      observations (e.g. provider metadata) without rewriting files.

  Returns:
    The result record; also written to ``<attempt>_result.json``.
  """
  key = attempt_id(case.case_id, model_tag, repetition)
  stem = f'{case.case_id}__{model_tag}__r{repetition}'
  started_marker = out_dir / f'{stem}_started.json'
  prompt_file = out_dir / f'{stem}_prompt.txt'
  response_file = out_dir / f'{stem}_response.txt'
  result_file = out_dir / f'{stem}_result.json'
  request_started_utc: list[str] = []

  def on_forward_start(prompt: str, params: dict[str, Any]) -> None:
    _write_started_marker(
        path=started_marker,
        key=key,
        stage='review',
        case_id=case.case_id,
        model_tag=model_tag,
        model_name=model_name,
        repetition=repetition,
        prompt=prompt,
        params=params,
    )
    prompt_file.write_text(prompt, encoding='utf-8')
    request_started_utc.append(utc_now())

  attempt_started = time.monotonic()
  director, entity, _ = _make_director(player=case.player, gm=gm)
  raw = _open_turn(entity, case.player, case.speech, case.action_intent)
  if case.action_result is not None:
    director.record_action_result(case.action_result)
  if case.intervention is not None:
    director.record_intervention_candidate(
        reason=case.intervention['reason'],
        candidate_before=case.intervention['candidate_before'],
        candidate_after=case.intervention['candidate_after'],
        trace=_INTERVENTION_TRACE,
    )
  wrapper = _FinalOutputProbeModel(
      real_model, case.stage_directions, on_forward_start=on_forward_start
  )
  chain = interactive_document.InteractiveDocument(wrapper)
  frozen_history = (
      history_text if history_text is not None else public_history_text()
  )
  step = story_resolution.make_final_output_step(
      director,
      immutable_facts=gm['immutable_facts'],
      character_constraints=gm['character_constraints'],
      player_names=gm['player_names'],
      world_constraints=gm['world_constraints'],
      allowed_environment_events=gm['allowed_environment_events'],
      public_facts=gm['public_facts'],
      public_history=lambda: frozen_history,
  )
  stage_outcome = None
  stage_error = None
  resolved_event = None
  try:
    resolved_event = step(chain, _premise_of_review_case(case), case.player)
    stage_outcome = 'approved'
  except story_resolution.StoryOutputError as error:
    stage_outcome = 'rejected'
    stage_error = str(error)
  except _HarnessError as error:
    stage_outcome = 'harness_error'
    stage_error = str(error)
  except Exception as error:  # pylint: disable=broad-except
    stage_outcome = 'api_error'
    stage_error = _redact(f'{type(error).__name__}: {error}', secrets)
  wall_seconds = round(time.monotonic() - attempt_started, 3)

  classification = None
  verdict = None
  reason = None
  canonical_matches = None
  cross_check_ok = None
  response_text = None
  prompt_sha256 = None
  request_params = None
  forward_wall = None
  forward_captures = [
      capture
      for capture in wrapper.captured
      if capture['slot'] == 'review_forward'
  ]
  if forward_captures:
    capture = forward_captures[-1]
    request_params = capture['params']
    prompt_sha256 = sha256_text(capture['prompt'])
    forward_wall = capture.get('wall_seconds')
    if 'response' in capture:
      response_text = capture['response']
      response_file.write_text(response_text, encoding='utf-8')
      classification, verdict, reason = classify_review_response(
          response_text, expected_valid=case.expected_valid
      )
  if stage_outcome == 'harness_error':
    classification = 'harness_error'
  if resolved_event is not None:
    # The canonical event must be exactly the production assembly of the
    # attributed speech and the fixture's frozen narration.
    expected_canonical = '\n'.join(
        part
        for part in (f'{case.player}: {case.speech}', case.stage_directions)
        if part
    )
    canonical_matches = resolved_event == expected_canonical
  # Cross-check the production step's own outcome with the parsed
  # verdict: an approved turn must correspond to a parsed valid=true and
  # a semantic-review rejection to a parsed valid=false. Any divergence
  # is a probe fault, not a model fault.
  if stage_outcome in ('approved', 'rejected') and classification not in (
      'malformed',
      'provider_error',
      'harness_error',
  ):
    cross_check_ok = (stage_outcome == 'approved') == (verdict is True)
  if classification in (
      'correct',
      'false_rejection',
      'missed_detection',
      'malformed',
  ):
    status = 'completed'
  else:
    status = stage_outcome
  result = {
      'attempt_id': key,
      'stage': 'review',
      'case_id': case.case_id,
      'model_tag': model_tag,
      'model': model_name,
      'repetition': repetition,
      'status': status,
      'classification': classification,
      'expected_valid': case.expected_valid,
      'verdict': verdict,
      'review_reason': reason,
      'stage_outcome': stage_outcome,
      'stage_error': stage_error,
      'canonical_matches_fixture': canonical_matches,
      'cross_check_ok': cross_check_ok,
      'raw_action': raw,
      'resolved_event': resolved_event,
      'request_params': request_params,
      'prompt_sha256': prompt_sha256,
      'prompt_file': prompt_file.name if prompt_file.exists() else None,
      'response_file': response_file.name if response_file.exists() else None,
      'synthetic_generation_calls': wrapper.generation_calls,
      'real_api_calls': wrapper.review_calls,
      'forward_wall_seconds': forward_wall,
      'attempt_wall_seconds': wall_seconds,
      'token_usage': 'unavailable (not exposed by the LanguageModel API)',
      'request_started_at_utc': (
          request_started_utc[0] if request_started_utc else None
      ),
      'ended_at_utc': utc_now(),
  }
  if enrich_result is not None:
    result = enrich_result(result)
  result_file.write_text(
      json.dumps(result, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )
  return result


def _run_adjudication_attempt(
    *,
    case: AdjudicationCase,
    real_model: language_model.LanguageModel,
    model_tag: ModelTag,
    model_name: str,
    repetition: int,
    gm: Mapping[str, Any],
    out_dir: pathlib.Path,
    secrets: Sequence[str],
    history_text: str | None = None,
    enrich_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
  """Runs one production action adjudication with the live core call.

  A real director turn lifecycle is opened with the fixture's JSON
  action contract; the production adjudication step states the frozen
  context and the core `attempt_to_result` helper asks the live model
  exactly once, observed by a wraps spy that never replaces the return
  value.

  Args:
    case: the frozen adjudication input.
    real_model: the live model answering the core question.
    model_tag: ``m1`` or ``m2``.
    model_name: the live model's actual name.
    repetition: 1-based repetition index.
    gm: the frozen game-master params.
    out_dir: the adjudication artefacts directory.
    secrets: credential values to redact from archived messages.
    history_text: optional frozen public-history text overriding the
      default `public_history_text()`; used by derived probes that
      freeze a different history.
    enrich_result: optional hook applied to the result record right
      before it is written, so a caller can attach per-attempt
      observations (e.g. provider metadata) without rewriting files.

  Returns:
    The result record; also written to ``<attempt>_result.json``.
  """
  key = attempt_id(case.case_id, model_tag, repetition)
  stem = f'{case.case_id}__{model_tag}__r{repetition}'
  started_marker = out_dir / f'{stem}_started.json'
  prompt_file = out_dir / f'{stem}_prompt.txt'
  response_file = out_dir / f'{stem}_response.txt'
  result_file = out_dir / f'{stem}_result.json'
  request_started_utc: list[str] = []

  def on_forward_start(prompt: str, params: dict[str, Any]) -> None:
    _write_started_marker(
        path=started_marker,
        key=key,
        stage='adjudication',
        case_id=case.case_id,
        model_tag=model_tag,
        model_name=model_name,
        repetition=repetition,
        prompt=prompt,
        params=params,
    )
    prompt_file.write_text(prompt, encoding='utf-8')
    request_started_utc.append(utc_now())

  attempt_started = time.monotonic()
  director, entity, _ = _make_director(player=case.player, gm=gm)
  raw = _open_turn(entity, case.player, case.speech, case.action_intent)
  wrapper = _AdjudicationProbeModel(
      real_model, on_forward_start=on_forward_start
  )
  chain = interactive_document.InteractiveDocument(wrapper)
  frozen_history = (
      history_text if history_text is not None else public_history_text()
  )
  step = story_action_adjudication.make_action_adjudication_step(
      director,
      public_facts=gm['public_facts'],
      public_history=lambda: frozen_history,
  )
  # Wraps spy over the real core helper: it observes the true call and
  # its true return value; the return value is never replaced (plan
  # section 5).
  core_calls: list[str] = []
  original_core = event_resolution_lib.attempt_to_result

  def _spy_core(*args: Any, **kwargs: Any) -> str:
    value = original_core(*args, **kwargs)
    core_calls.append(value)
    return value

  event_resolution_lib.attempt_to_result = _spy_core
  stage_outcome = None
  stage_error = None
  returned = None
  try:
    returned = step(chain, raw, case.player)
    stage_outcome = 'completed'
  except _HarnessError as error:
    stage_outcome = 'harness_error'
    stage_error = str(error)
  except Exception as error:  # pylint: disable=broad-except
    stage_outcome = 'api_error'
    stage_error = _redact(f'{type(error).__name__}: {error}', secrets)
  finally:
    event_resolution_lib.attempt_to_result = original_core
  wall_seconds = round(time.monotonic() - attempt_started, 3)

  evidence = director.get_current_resolution_evidence()
  diagnostics = director.get_current_resolution_diagnostics()
  core_action_result = evidence.get('action_result')
  returned_matches_recorded = (
      returned is not None and returned == core_action_result
  )
  response_text = None
  request_params = None
  prompt_sha256 = None
  forward_wall = None
  if wrapper.captured:
    capture = wrapper.captured[-1]
    request_params = capture['params']
    prompt_sha256 = sha256_text(capture['prompt'])
    forward_wall = capture.get('wall_seconds')
    if 'response' in capture:
      response_text = capture['response']
      response_file.write_text(response_text, encoding='utf-8')
  status = 'completed' if stage_outcome == 'completed' else stage_outcome
  result = {
      'attempt_id': key,
      'stage': 'adjudication',
      'case_id': case.case_id,
      'model_tag': model_tag,
      'model': model_name,
      'repetition': repetition,
      'status': status,
      'stage_outcome': stage_outcome,
      'stage_error': stage_error,
      'raw_provider_text': response_text,
      'core_action_result': core_action_result,
      'returned_matches_recorded': returned_matches_recorded,
      'core_call_count': len(core_calls),
      'director_evidence': {
          'action_intent': evidence.get('action_intent'),
          'action_result': evidence.get('action_result'),
          'intervention': evidence.get('intervention'),
      },
      'private_diagnostics': json.loads(json.dumps(diagnostics)),
      'raw_action': raw,
      'request_params': request_params,
      'prompt_sha256': prompt_sha256,
      'prompt_file': prompt_file.name if prompt_file.exists() else None,
      'response_file': response_file.name if response_file.exists() else None,
      'real_api_calls': wrapper.calls,
      'forward_wall_seconds': forward_wall,
      'attempt_wall_seconds': wall_seconds,
      'token_usage': 'unavailable (not exposed by the LanguageModel API)',
      'request_started_at_utc': (
          request_started_utc[0] if request_started_utc else None
      ),
      'ended_at_utc': utc_now(),
  }
  if enrich_result is not None:
    result = enrich_result(result)
  result_file.write_text(
      json.dumps(result, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )
  return result


# ---------------------------------------------------------------------------
# Frozen artefacts.
# ---------------------------------------------------------------------------


def fixtures_payload() -> dict[str, Any]:
  """Builds the frozen fixtures document (metadata included).

  The expected labels, rationales and judgment focuses are evaluation
  metadata: the harness only reads the non-metadata fields when building
  prompts, so labels can never leak into model input.

  Returns:
    The JSON-serializable fixtures payload.
  """
  return {
      'shared_context': {
          'history_note': (
              '24 committed public events, oldest first; the director '
              'window is the last 20, the public-history getter returns '
              'all 24 formatted like the production DisplayEvents.'
          ),
          'public_facts': gm_params()['public_facts'],
          'gm_constraints_source': (
              'story_config.make_example_config() via '
              'run_lib.make_gm_params(): GM-only constraints, immutable '
              'facts, player names and the environment whitelist.'
          ),
          'history_events': list(_HISTORY),
          'director_window_size': _DIRECTOR_WINDOW_SIZE,
      },
      'review_cases': [dataclasses.asdict(case) for case in _REVIEW_CASES],
      'adjudication_cases': [
          dataclasses.asdict(case) for case in _ADJUDICATION_CASES
      ],
  }


def protocol_payload(model_names: Mapping[ModelTag, str]) -> dict[str, Any]:
  """Builds the frozen protocol document (plan section 3.4).

  Args:
    model_names: the actual live model names per tag.

  Returns:
    The JSON-serializable protocol payload.
  """
  return {
      'protocol_version': 1,
      'created_utc': utc_now(),
      'probe': 'action_authorization_fixed_probe',
      'models': {tag: model_names[tag] for tag in _MODEL_TAGS},
      'model_config_source': {
          'api_type': 'openai',
          'env_model_keys': _ENV_MODEL_KEYS,
          'env_base_url_key': 'AI_BASE_URL',
          'env_api_key_key': 'AI_API_KEY',
          'client': (
              'GptLanguageModel with max_retries=0 and timeout=120s '
              '(SDK auto-retry and probe-outer retry disabled)'
          ),
      },
      'request_settings': {
          'note': (
              'Values emitted by the production steps and the shared '
              'document defaults; the wrapper archives what each call '
              'actually carried and nothing is overridden to improve '
              'pass rates.'
          ),
          'review_forward': {
              'max_tokens': _REVIEW_MAX_TOKENS,
              'terminators': [],
          },
          'generation_synthetic': {
              'max_tokens': _GENERATION_MAX_TOKENS,
              'terminators': [],
          },
          'adjudication_forward': {
              'max_tokens': _ADJUDICATION_MAX_TOKENS,
              'terminators': [],
          },
          'document_defaults': {
              'temperature': _TEMPERATURE,
              'top_p': _TOP_P,
              'top_k': _TOP_K,
          },
          'sdk_max_retries': _SDK_MAX_RETRIES,
          'client_timeout_seconds': _CLIENT_TIMEOUT_SECONDS,
      },
      'schedule': {
          'repetitions_per_case_per_model': _REPETITIONS,
          'review_cases': len(_REVIEW_CASES),
          'adjudication_cases': len(_ADJUDICATION_CASES),
          'planned_review_calls': (
              len(_REVIEW_CASES) * len(_MODEL_TAGS) * _REPETITIONS
          ),
          'planned_adjudication_calls': (
              len(_ADJUDICATION_CASES) * len(_MODEL_TAGS) * _REPETITIONS
          ),
          'max_real_calls': _MAX_REAL_CALLS,
          'synthetic_generation_calls_note': (
              'Group R carries one synthetic generation response per '
              'review; it is not a paid request and is counted '
              'separately as synthetic_generation_calls.'
          ),
          'execution_order': (
              'R first, then A; per group: repetition, then model '
              '(m1, m2), then case id.'
          ),
      },
      'prompt_binding': {
          'same_prompt_across_models_and_repetitions': True,
          'model_name_in_prompt': False,
          'labels_in_prompt': False,
      },
      'no_retry_policy': (
          'No retry of any kind: a semantic failure is recorded as-is; '
          'a network/API error is recorded as api_error for that '
          'attempt and is never replaced by a later successful response.'
      ),
      'interruption_policy': (
          'An attempt whose request started but produced no archived '
          'response stays indeterminate; it is never silently re-sent. '
          'Unstarted planned entries may continue, but the batch then '
          'fails the completeness gate.'
      ),
      'pass_criteria': {
          'review_per_model': (
              'all 48 planned entries completed; 0 false_rejections, '
              '0 missed_detections, 0 malformed, 0 api/provider errors, '
              '0 incomplete/indeterminate/harness errors'
          ),
          'review_round': 'both models pass',
          'adjudication_per_model': (
              'all 12 planned entries carry a complete human label; '
              'zero unauthorized player actions, fabricated evidence, '
              'illegal disclosures and ungrounded plot events; every '
              'outcome definite and setting-grounded; 0 api/provider/'
              'harness errors'
          ),
          'overall': (
              'R and A both complete, hashes verified, machine '
              'classification and human review done, and both models '
              'meet their gates'
          ),
          'note': (
              'A single failure of one case fails the gate; three '
              'repetitions are never treated as a majority vote. '
              'Passing this finite-sample gate is not a guarantee of '
              'error-free model behaviour.'
          ),
      },
      'classification_table': {
          'legal_expected_true_and_valid_true': 'correct',
          'legal_expected_true_and_valid_false': 'false_rejection',
          'illegal_expected_false_and_valid_false': 'correct',
          'illegal_expected_false_and_valid_true': 'missed_detection',
          'undecodable_or_schema_violation': 'malformed',
          'network_or_provider_failure': 'api_error/provider_error',
          'not_started_unexpected_or_probe_fault': (
              'incomplete/indeterminate/harness_error'
          ),
      },
  }


_SNAPSHOT_FILES = (
    'examples/story_enactment/fixed_model_probe.py',
    'examples/story_enactment/fixed_model_probe_test.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/prefabs/game_master/story_dramaturgic.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/components/game_master/story_action.py',
    'concordia/components/game_master/event_resolution.py',
    'concordia/document/interactive_document.py',
    'concordia/document/document.py',
    'concordia/language_model/language_model.py',
    'concordia/contrib/language_models/openai/gpt_model.py',
    'concordia/contrib/language_models/openai/base_gpt_model.py',
    'examples/story_enactment/run.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/testing_stubs.py',
    (
        'docs/superpowers/plans/'
        '2026-10-17-story-enactment-fixed-model-validation.md'
    ),
    (
        'docs/superpowers/specs/'
        '2026-10-17-story-enactment-action-authorization-review-design.md'
    ),
    (
        'docs/superpowers/notes/'
        '2026-10-17-story-enactment-action-authorization-review.md'
    ),
)


def source_snapshot_payload() -> dict[str, Any]:
  """Builds the source snapshot document (plan section 3.4).

  Every relevant production file is recorded by content hash and size —
  not merely by Git HEAD — so the exact bytes probed are verifiable.

  Returns:
    The JSON-serializable snapshot payload.
  """
  files = []
  for relative in _SNAPSHOT_FILES:
    path = _REPO_ROOT / relative
    files.append({
        'path': relative,
        'sha256': sha256_file(path),
        'bytes': path.stat().st_size,
    })
  return {
      'created_utc': utc_now(),
      'hash_algorithm': 'sha256',
      'files': files,
      'note': (
          'Per-file content hashes of the exact bytes this batch probes; '
          'run stages re-verify these hashes before any request.'
      ),
  }


# ---------------------------------------------------------------------------
# CLI stages.
# ---------------------------------------------------------------------------

flags.DEFINE_string(
    'stage',
    None,
    f'Probe stage; one of {", ".join(_STAGES)}.',
    required=True,
)
flags.DEFINE_string(
    'batch_dir',
    None,
    'Batch output directory (required by every stage except dry_run).',
)
flags.DEFINE_string(
    'dry_run_dir',
    None,
    'Output directory for the dry run; it must not exist. Defaults to a '
    'new directory under /tmp/opencode.',
)
FLAGS = flags.FLAGS

_DRY_RUN_PROMPT_DIR = 'prompts'


def _load_settings() -> tuple[dict[ModelTag, str], str, str, list[str]]:
  """Resolves the live model names and credentials from the .env source.

  Returns:
    A (model names, api key, base url, secrets-to-redact) tuple.

  Raises:
    ValueError: if a model name or credential is missing, or a fixed
    model name does not match the name this batch probes.
  """
  settings = dotenv_values(_REPO_ROOT / '.env')
  names: dict[ModelTag, str] = {}
  for tag in _MODEL_TAGS:
    name = settings.get(_ENV_MODEL_KEYS[tag])
    if not name:
      raise ValueError(f'{_ENV_MODEL_KEYS[tag]} is not set in .env')
    if name != _EXPECTED_MODEL_NAMES[tag]:
      raise ValueError(
          f'{_ENV_MODEL_KEYS[tag]} is {name!r}; this frozen batch probes '
          f'{_EXPECTED_MODEL_NAMES[tag]!r}'
      )
    names[tag] = name
  api_key = settings.get('AI_API_KEY')
  base_url = settings.get('AI_BASE_URL')
  if not api_key or not base_url:
    raise ValueError('AI_API_KEY and AI_BASE_URL must be set in .env')
  return names, api_key, base_url, [api_key, base_url]


def _create_live_model(
    model_name: str, *, api_key: str, api_base: str
) -> gpt_model.GptLanguageModel:
  """Builds the live model with SDK auto-retry disabled.

  This is the same OpenAI-compatible interface and configuration source
  the archived runs used; the only hardening is disabling the SDK's
  internal retries and bounding the timeout, per the frozen protocol.

  Args:
    model_name: the live model's actual name.
    api_key: the API credential (never archived).
    api_base: the OpenAI-compatible endpoint.

  Returns:
    The live language model.
  """
  model = gpt_model.GptLanguageModel(
      model_name=model_name, api_key=api_key, api_base=api_base
  )
  model._client = model._client.with_options(  # pylint: disable=protected-access
      max_retries=_SDK_MAX_RETRIES, timeout=_CLIENT_TIMEOUT_SECONDS
  )
  return model


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
  """Writes a JSON document with a trailing newline."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


def _verify_frozen(batch_dir: pathlib.Path) -> None:
  """Re-verifies the frozen artefacts before any request.

  Args:
    batch_dir: the batch directory.

  Raises:
    ValueError: if any frozen artefact is missing or its bytes no longer
    match what was frozen (protocol, fixtures) or hashed (snapshot).
  """
  protocol_path = batch_dir / _PROTOCOL_NAME
  fixtures_path = batch_dir / _FIXTURES_NAME
  snapshot_path = batch_dir / _SNAPSHOT_NAME
  for path in (protocol_path, fixtures_path, snapshot_path):
    if not path.exists():
      raise ValueError(f'frozen artefact missing: {path}')
  names, _, _, _ = _load_settings()
  protocol_text = protocol_path.read_text(encoding='utf-8')
  frozen_protocol = json.loads(protocol_text)
  expected_protocol = protocol_payload(names)
  # The creation timestamp is expected to differ; every other field must
  # still match the live protocol contract byte for byte. Tampering with
  # any frozen byte is caught separately by the report hashes in
  # `_load_report`.
  frozen_protocol.pop('created_utc', None)
  expected_protocol.pop('created_utc', None)
  if frozen_protocol != expected_protocol:
    raise ValueError('protocol.json no longer matches the frozen protocol')
  fixtures_text = fixtures_path.read_text(encoding='utf-8')
  expected_fixtures = (
      json.dumps(fixtures_payload(), ensure_ascii=False, indent=2) + '\n'
  )
  if fixtures_text != expected_fixtures:
    raise ValueError('fixtures.json no longer matches the frozen fixtures')
  snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
  for entry in snapshot['files']:
    path = _REPO_ROOT / entry['path']
    if not path.exists():
      raise ValueError(f'snapshotted file missing: {entry["path"]}')
    if sha256_file(path) != entry['sha256']:
      raise ValueError(
          f'snapshotted file changed since freeze: {entry["path"]}'
      )


def _freeze(batch_dir: pathlib.Path) -> None:
  """Writes the frozen protocol, fixtures and source snapshot.

  Args:
    batch_dir: the batch directory; it must not exist.

  Raises:
    ValueError: if the batch directory already exists.
  """
  if batch_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {batch_dir}')
  names, _, _, _ = _load_settings()
  (batch_dir / _REVIEW_DIR_NAME).mkdir(parents=True)
  (batch_dir / _ADJUDICATION_DIR_NAME).mkdir()
  _write_json(batch_dir / _PROTOCOL_NAME, protocol_payload(names))
  _write_json(batch_dir / _FIXTURES_NAME, fixtures_payload())
  _write_json(batch_dir / _SNAPSHOT_NAME, source_snapshot_payload())
  report = {
      'probe': 'action_authorization_fixed_probe',
      'protocol_sha256': sha256_file(batch_dir / _PROTOCOL_NAME),
      'fixtures_sha256': sha256_file(batch_dir / _FIXTURES_NAME),
      'source_snapshot_sha256': sha256_file(batch_dir / _SNAPSHOT_NAME),
      'models': dict(names),
      'counts': {
          'planned_review_attempts': len(planned_review_attempts()),
          'planned_adjudication_attempts': len(planned_adjudication_attempts()),
          'synthetic_generation_calls': 0,
          'real_api_calls': 0,
      },
      'attempts': [],
      'summary': None,
      'overall_pass': None,
  }
  _write_json(batch_dir / _REPORT_NAME, report)


def _load_report(batch_dir: pathlib.Path) -> dict[str, Any]:
  """Loads the batch report, verifying its frozen-artefact hashes."""
  report = json.loads((batch_dir / _REPORT_NAME).read_text(encoding='utf-8'))
  if report['protocol_sha256'] != sha256_file(batch_dir / _PROTOCOL_NAME):
    raise ValueError('protocol.json changed after freeze')
  if report['fixtures_sha256'] != sha256_file(batch_dir / _FIXTURES_NAME):
    raise ValueError('fixtures.json changed after freeze')
  if report['source_snapshot_sha256'] != sha256_file(
      batch_dir / _SNAPSHOT_NAME
  ):
    raise ValueError('source_snapshot.json changed after freeze')
  return report


def _save_report(batch_dir: pathlib.Path, report: Mapping[str, Any]) -> None:
  """Writes the batch report."""
  target = batch_dir / _REPORT_NAME
  tmp = target.with_suffix('.json.tmp')
  _write_json(tmp, report)
  tmp.replace(target)


def _attempt_files_exist(out_dir: pathlib.Path, stem: str) -> bool:
  """Returns whether an attempt already produced any on-disk evidence."""
  return any(
      (out_dir / f'{stem}{suffix}').exists()
      for suffix in ('_started.json', '_response.txt', '_result.json')
  )


def _run_group(
    *,
    stage: str,
    batch_dir: pathlib.Path,
    real_call_cap: int | None = None,
) -> None:
  """Runs the pending attempts of one group (plan section 7).

  Only entries with no on-disk evidence are executed; an entry whose
  request started but produced no archived response is never re-sent.
  The frozen artefacts are re-verified before the first request, and the
  cap on real forwarded calls is enforced before every forward.

  Args:
    stage: ``run_r`` or ``run_a``.
    batch_dir: the batch directory.
    real_call_cap: optional cap override for offline rehearsal of the
      cap enforcement; defaults to the frozen protocol cap.

  Raises:
    ValueError: if frozen artefacts changed or the real-call cap would
    be exceeded.
  """
  cap = _MAX_REAL_CALLS if real_call_cap is None else real_call_cap
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  names, api_key, base_url, secrets = _load_settings()
  out_dir = batch_dir / (
      _REVIEW_DIR_NAME if stage == _STAGE_RUN_REVIEW else _ADJUDICATION_DIR_NAME
  )
  live_models = {
      tag: _create_live_model(names[tag], api_key=api_key, api_base=base_url)
      for tag in _MODEL_TAGS
  }
  if stage == _STAGE_RUN_REVIEW:
    plan = planned_review_attempts()
  else:
    plan = planned_adjudication_attempts()
  executed = report['counts']['real_api_calls']
  for case_id, model_tag, repetition in plan:
    stem = f'{case_id}__{model_tag}__r{repetition}'
    if _attempt_files_exist(out_dir, stem):
      continue
    if executed >= cap:
      raise ValueError('real-call cap reached; refusing to send more requests')
    common_kwargs = {
        'real_model': live_models[model_tag],
        'model_tag': model_tag,
        'model_name': names[model_tag],
        'repetition': repetition,
        'gm': gm_params(),
        'out_dir': out_dir,
        'secrets': secrets,
    }
    if stage == _STAGE_RUN_REVIEW:
      result = _run_review_attempt(
          case=review_case_by_id(case_id), **common_kwargs
      )
    else:
      result = _run_adjudication_attempt(
          case=adjudication_case_by_id(case_id), **common_kwargs
      )
    executed += result['real_api_calls']
    report['counts']['real_api_calls'] += result['real_api_calls']
    report['counts']['synthetic_generation_calls'] += result.get(
        'synthetic_generation_calls', 0
    )
    report['attempts'].append({
        'attempt_id': result['attempt_id'],
        'stage': result['stage'],
        'status': result['status'],
        'classification': result.get('classification'),
        'prompt_file': result['prompt_file'],
        'response_file': result['response_file'],
    })
    _save_report(batch_dir, report)
    suffix = (
        f' ({result["classification"]})' if result.get('classification') else ''
    )
    print(f'{result["attempt_id"]}: {result["status"]}{suffix}', flush=True)


def _capture_review_prompts(
    case: ReviewCase,
    dry_run_prompt_dir: pathlib.Path,
    *,
    history_text: str | None = None,
) -> tuple[str, str]:
  """Captures the production generation and review prompts offline.

  Drives the production final-output step with the dry-run reviewer in
  the forward slot, then archives both captured prompts.

  Args:
    case: the review fixture.
    dry_run_prompt_dir: where the prompt files are written.
    history_text: optional frozen public-history text overriding the
      default `public_history_text()`; used by derived probes that
      freeze a different history.

  Returns:
    A (generation prompt, review prompt) tuple.

  Raises:
    _HarnessError: if the production step routed calls unexpectedly.
  """
  director, entity, _ = _make_director(player=case.player, gm=gm_params())
  _open_turn(entity, case.player, case.speech, case.action_intent)
  if case.action_result is not None:
    director.record_action_result(case.action_result)
  if case.intervention is not None:
    director.record_intervention_candidate(
        reason=case.intervention['reason'],
        candidate_before=case.intervention['candidate_before'],
        candidate_after=case.intervention['candidate_after'],
        trace=_INTERVENTION_TRACE,
    )
  wrapper = _FinalOutputProbeModel(_DryRunReviewer(), case.stage_directions)
  chain = interactive_document.InteractiveDocument(wrapper)
  frozen_history = (
      history_text if history_text is not None else public_history_text()
  )
  step = story_resolution.make_final_output_step(
      director,
      immutable_facts=gm_params()['immutable_facts'],
      character_constraints=gm_params()['character_constraints'],
      player_names=gm_params()['player_names'],
      world_constraints=gm_params()['world_constraints'],
      allowed_environment_events=gm_params()['allowed_environment_events'],
      public_facts=gm_params()['public_facts'],
      public_history=lambda: frozen_history,
  )
  try:
    step(chain, _premise_of_review_case(case), case.player)
  except story_resolution.StoryOutputError:
    pass  # The dry-run verdict is a placeholder; prompts stay captured.
  if wrapper.generation_calls != 1 or wrapper.review_calls != 1:
    raise _HarnessError(
        f'{case.case_id}: expected 1+1 slots, got '
        f'{wrapper.generation_calls}+{wrapper.review_calls}'
    )
  generation_prompt = wrapper.captured[0]['prompt']
  review_prompt = wrapper.captured[1]['prompt']
  for name, text in (
      (f'{case.case_id}_generation_prompt.txt', generation_prompt),
      (f'{case.case_id}_review_prompt.txt', review_prompt),
  ):
    (dry_run_prompt_dir / name).write_text(text, encoding='utf-8')
  return generation_prompt, review_prompt


def _capture_adjudication_prompt(
    case: AdjudicationCase,
    dry_run_prompt_dir: pathlib.Path,
    *,
    history_text: str | None = None,
) -> str:
  """Captures the production adjudication prompt offline.

  Args:
    case: the adjudication input.
    dry_run_prompt_dir: where the prompt file is written.
    history_text: optional frozen public-history text overriding the
      default `public_history_text()`; used by derived probes that
      freeze a different history.

  Returns:
    The captured core-question prompt.

  Raises:
    _HarnessError: if the production step routed calls unexpectedly.
  """
  director, entity, _ = _make_director(player=case.player, gm=gm_params())
  raw = _open_turn(entity, case.player, case.speech, case.action_intent)
  wrapper = _AdjudicationProbeModel(_DryRunReviewer())
  chain = interactive_document.InteractiveDocument(wrapper)
  frozen_history = (
      history_text if history_text is not None else public_history_text()
  )
  step = story_action_adjudication.make_action_adjudication_step(
      director,
      public_facts=gm_params()['public_facts'],
      public_history=lambda: frozen_history,
  )
  step(chain, raw, case.player)
  if wrapper.calls != 1:
    raise _HarnessError(
        f'{case.case_id}: expected 1 core slot, got {wrapper.calls}'
    )
  prompt = wrapper.captured[0]['prompt']
  (dry_run_prompt_dir / f'{case.case_id}_prompt.txt').write_text(
      prompt, encoding='utf-8'
  )
  return prompt


def _dry_run(dry_run_dir: pathlib.Path) -> None:
  """Runs the no-network rehearsal (plan sections 2 and 6).

  Builds every planned attempt offline, drives the production factories,
  captures the actual production prompts, and verifies routing, counts,
  prompt binding and the canonical assembly. No real client is ever
  constructed: the forwarding slots answer with the offline placeholder.

  Args:
    dry_run_dir: output directory; it must not exist.
  """
  if dry_run_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {dry_run_dir}')
  prompt_dir = dry_run_dir / _DRY_RUN_PROMPT_DIR
  prompt_dir.mkdir(parents=True)
  generation_prompts: dict[str, str] = {}
  review_prompts: dict[str, str] = {}
  adjudication_prompts: dict[str, str] = {}
  for repetition in range(1, _REPETITIONS + 1):
    for model_tag in _MODEL_ORDER:
      for case in _REVIEW_CASES:
        generation, review = _capture_review_prompts(case, prompt_dir)
        key = attempt_id(case.case_id, model_tag, repetition)
        generation_prompts[key] = generation
        review_prompts[key] = review
      for case in _ADJUDICATION_CASES:
        adjudication_prompts[
            attempt_id(case.case_id, model_tag, repetition)
        ] = _capture_adjudication_prompt(case, prompt_dir)
  findings: list[str] = []
  # Uniqueness and count of the planned keys (plan section 6.2).
  review_keys = planned_review_attempts()
  adjudication_keys = planned_adjudication_attempts()
  if len(set(review_keys)) != 96 or len(review_keys) != 96:
    findings.append('review plan is not 96 unique keys')
  if len(set(adjudication_keys)) != 24 or len(adjudication_keys) != 24:
    findings.append('adjudication plan is not 24 unique keys')
  # Byte-identical prompts across models and repetitions, and no model
  # name inside any prompt (plan section 6.3).
  for prompts, group in (
      (generation_prompts, 'review-generation'),
      (review_prompts, 'review-review'),
      (adjudication_prompts, 'adjudication'),
  ):
    by_case: dict[str, set[str]] = {}
    for key, prompt in prompts.items():
      by_case.setdefault(key.split('__')[0], set()).add(prompt)
    for case_id, variants in by_case.items():
      if len(variants) != 1:
        findings.append(f'{group} prompts differ within case {case_id}')
    for key, prompt in prompts.items():
      for tag in _MODEL_TAGS:
        if _EXPECTED_MODEL_NAMES[tag] in prompt:
          findings.append(f'{key}: model name leaked into prompt')
  # Production data sections really enter the actual prompts, and the
  # generation and review see the identical public history (plan
  # section 6.4).
  for case_id in [case.case_id for case in _REVIEW_CASES]:
    generation = generation_prompts[f'{case_id}__m1__r1']
    review = review_prompts[f'{case_id}__m1__r1']
    history = public_history_text()
    for prompt, label in ((generation, 'generation'), (review, 'review')):
      if story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK not in prompt:
        findings.append(f'{case_id} {label}: public-history section missing')
      if json.dumps(history, ensure_ascii=False) not in prompt:
        findings.append(f'{case_id} {label}: frozen history text missing')
    if case_id == 'F01' and (
        'If rejecting, name the specific offending text' not in review
    ):
      findings.append('F01 review: production question suffix missing')
  for case in _ADJUDICATION_CASES:
    prompt = adjudication_prompts[f'{case.case_id}__m1__r1']
    if story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK not in prompt:
      findings.append(f'{case.case_id} adjudication: history section missing')
    if json.dumps(public_history_text(), ensure_ascii=False) not in prompt:
      findings.append(f'{case.case_id} adjudication: history text missing')
    if case.judgment_focus in prompt:
      findings.append(f'{case.case_id}: label metadata leaked into prompt')
  for case in _REVIEW_CASES:
    for prompt in (
        generation_prompts[f'{case.case_id}__m1__r1'],
        review_prompts[f'{case.case_id}__m1__r1'],
    ):
      if case.human_rationale in prompt or case.fact_sources in prompt:
        findings.append(f'{case.case_id}: label metadata leaked into prompt')
  summary = {
      'stage': 'dry_run',
      'generated_at_utc': utc_now(),
      'planned_review_attempts': len(review_keys),
      'planned_adjudication_attempts': len(adjudication_keys),
      'real_client_constructions': 0,
      'real_api_calls': 0,
      'findings': findings,
      'prompt_hashes': {
          'review_generation': {
              case.case_id: sha256_text(
                  generation_prompts[f'{case.case_id}__m1__r1']
              )
              for case in _REVIEW_CASES
          },
          'review_review': {
              case.case_id: sha256_text(
                  review_prompts[f'{case.case_id}__m1__r1']
              )
              for case in _REVIEW_CASES
          },
          'adjudication': {
              case.case_id: sha256_text(
                  adjudication_prompts[f'{case.case_id}__m1__r1']
              )
              for case in _ADJUDICATION_CASES
          },
      },
  }
  _write_json(dry_run_dir / 'dry_run_summary.json', summary)
  for finding in findings:
    print(f'FINDING: {finding}', flush=True)
  print(
      f'dry run complete: {len(findings)} findings; prompts in {prompt_dir}',
      flush=True,
  )


def _summarize(batch_dir: pathlib.Path) -> None:
  """Summarizes the batch (plan section 8) without any model call.

  Verifies strict completeness for both groups, applies the machine
  classification of group R, and checks the human labels of group A and
  the reason review of group R. Read-only over the archived responses.

  Args:
    batch_dir: the batch directory.

  Raises:
    ValueError: if frozen artefacts changed.
  """
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  review_out = batch_dir / _REVIEW_DIR_NAME
  adjudication_out = batch_dir / _ADJUDICATION_DIR_NAME

  def collect(stage_dir: pathlib.Path, plan: list[tuple[str, ModelTag, int]]):
    """Collects per-model result records with completeness accounting."""
    per_model: dict[str, dict[str, Any]] = {
        tag: {'planned': 0, 'started': 0, 'completed': 0, 'records': []}
        for tag in _MODEL_TAGS
    }
    for case_id, model_tag, repetition in plan:
      stem = f'{case_id}__{model_tag}__r{repetition}'
      per_model[model_tag]['planned'] += 1
      result_path = stage_dir / f'{stem}_result.json'
      if result_path.exists():
        record = json.loads(result_path.read_text(encoding='utf-8'))
        per_model[model_tag]['records'].append(record)
        if record['status'] == 'completed':
          per_model[model_tag]['completed'] += 1
        else:
          per_model[model_tag].setdefault('statuses', {}).setdefault(
              record['status'], 0
          )
          per_model[model_tag]['statuses'][record['status']] += 1
      elif (stage_dir / f'{stem}_started.json').exists():
        per_model[model_tag].setdefault('statuses', {}).setdefault(
            'indeterminate', 0
        )
        per_model[model_tag]['statuses']['indeterminate'] += 1
      per_model[model_tag]['started'] += int(
          (stage_dir / f'{stem}_started.json').exists()
      )
    return per_model

  review = collect(review_out, planned_review_attempts())
  adjudication = collect(adjudication_out, planned_adjudication_attempts())

  def review_metrics(per_model: dict[str, Any]) -> dict[str, Any]:
    """Aggregates the machine classification of one model."""
    metrics = {
        'planned': per_model['planned'],
        'started': per_model['started'],
        'completed': per_model['completed'],
        'correct': 0,
        'false_rejection': 0,
        'missed_detection': 0,
        'malformed': 0,
        'api_error': 0,
        'provider_error': 0,
        'harness_error': 0,
        'indeterminate': 0,
        'cross_check_failures': 0,
        'canonical_mismatches': 0,
    }
    for record in per_model['records']:
      classification = record.get('classification')
      if classification in metrics and isinstance(classification, str):
        metrics[classification] += 1
      if record.get('cross_check_ok') is False:
        metrics['cross_check_failures'] += 1
      if record.get('canonical_matches_fixture') is False:
        metrics['canonical_mismatches'] += 1
    metrics['indeterminate'] = metrics['planned'] - metrics['started']
    metrics['pass'] = (
        metrics['planned'] == 48
        and metrics['completed'] == 48
        and metrics['false_rejection'] == 0
        and metrics['missed_detection'] == 0
        and metrics['malformed'] == 0
        and metrics['api_error'] == 0
        and metrics['provider_error'] == 0
        and metrics['harness_error'] == 0
        and metrics['indeterminate'] == 0
        and metrics['cross_check_failures'] == 0
        and metrics['canonical_mismatches'] == 0
    )
    return metrics

  review_by_model = {tag: review_metrics(review[tag]) for tag in _MODEL_TAGS}

  # Group A: completeness only; the semantic gate needs human labels.
  adjudication_by_model = {}
  for tag in _MODEL_TAGS:
    per_model = adjudication[tag]
    statuses: dict[str, int] = dict(per_model.get('statuses', {}))
    labels = []
    labels_path = batch_dir / _HUMAN_LABELS_NAME
    if labels_path.exists():
      for line in labels_path.read_text(encoding='utf-8').splitlines():
        if line.strip():
          labels.append(json.loads(line))
    labeled = {
        label['attempt_id']
        for label in labels
        if label.get('status') == 'labeled'
    }
    planned_ids = {
        attempt_id(case_id, tag, repetition)
        for case_id, model_tag, repetition in planned_adjudication_attempts()
        if model_tag == tag
    }
    adjudication_by_model[tag] = {
        'planned': per_model['planned'],
        'started': per_model['started'],
        'completed': per_model['completed'],
        'statuses': statuses,
        'labeled': len(planned_ids & labeled),
        'violations': {
            'unauthorized_player_action': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('unauthorized_player_action')
            ),
            'fabricated_evidence': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('fabricated_evidence')
            ),
            'illegal_disclosure': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('illegal_disclosure')
            ),
            'ungrounded_plot_event': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('ungrounded_plot_event')
            ),
            'not_definite_grounded_result': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('status') == 'labeled'
                and not label.get('definite_grounded_result')
            ),
            'failed': sum(
                1
                for label in labels
                if label.get('attempt_id') in planned_ids
                and label.get('status') == 'labeled'
                and not label.get('pass')
            ),
        },
        'api_or_harness_errors': (
            statuses.get('api_error', 0)
            + statuses.get('harness_error', 0)
            + statuses.get('indeterminate', 0)
        ),
    }
    adjudication_by_model[tag]['pass'] = (
        per_model['planned'] == 12
        and per_model['completed'] == 12
        and adjudication_by_model[tag]['labeled'] == 12
        and all(
            count == 0
            for key, count in adjudication_by_model[tag]['violations'].items()
            if key != 'failed'
        )
        and adjudication_by_model[tag]['api_or_harness_errors'] == 0
        and adjudication_by_model[tag]['violations']['failed'] == 0
    )

  review_pass = all(
      metrics['pass'] for metrics in review_by_model.values()
  ) and len(review_by_model) == len(_MODEL_TAGS)
  adjudication_pass = all(
      entry['pass'] for entry in adjudication_by_model.values()
  ) and len(adjudication_by_model) == len(_MODEL_TAGS)
  overall_pass = bool(review_pass and adjudication_pass)
  report['summary'] = {
      'generated_at_utc': utc_now(),
      'review': review_by_model,
      'review_pass': review_pass,
      'adjudication': adjudication_by_model,
      'adjudication_pass': adjudication_pass,
      'note': (
          'review_pass is the machine gate only; the human reason review '
          'of group R is recorded in human_labels.jsonl and discussed in '
          'README.md. overall_pass requires both groups complete and '
          'human-reviewed.'
      ),
  }
  report['overall_pass'] = overall_pass
  _save_report(batch_dir, report)
  print(
      f'summary written: review_pass={review_pass} '
      f'adjudication_pass={adjudication_pass} overall_pass={overall_pass}',
      flush=True,
  )


def main(argv: list[str]) -> None:
  """Dispatches the probe stage given on the command line.

  Args:
    argv: the command-line arguments.
  """
  del argv
  stage = FLAGS.stage
  if stage not in _STAGES:
    raise app.UsageError(f'--stage must be one of {", ".join(_STAGES)}')
  if stage == _STAGE_DRY_RUN:
    dry_run_dir = (
        pathlib.Path(FLAGS.dry_run_dir)
        if FLAGS.dry_run_dir
        else pathlib.Path(
            '/tmp/opencode/fixed_model_probe_dry_run_'
            f'{datetime_lib.datetime.now(datetime_lib.timezone.utc).strftime("%Y%m%dT%H%M%SZ")}'
        )
    )
    _dry_run(dry_run_dir)
    return
  if not FLAGS.batch_dir:
    raise app.UsageError(f'--stage={stage} requires --batch_dir')
  batch_dir = pathlib.Path(FLAGS.batch_dir)
  if stage == _STAGE_FREEZE:
    _freeze(batch_dir)
    print(f'frozen protocol written to {batch_dir}', flush=True)
  elif stage == _STAGE_RUN_REVIEW:
    _run_group(stage=_STAGE_RUN_REVIEW, batch_dir=batch_dir)
  elif stage == _STAGE_RUN_ADJUDICATION:
    _run_group(stage=_STAGE_RUN_ADJUDICATION, batch_dir=batch_dir)
  elif stage == _STAGE_SUMMARIZE:
    _summarize(batch_dir)


if __name__ == '__main__':
  app.run(main)
