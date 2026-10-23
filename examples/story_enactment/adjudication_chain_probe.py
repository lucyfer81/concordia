# Copyright 2026 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Two-model adjudication-to-review chain probe (m1 Gemini / m4 Sol).

This runner implements the 2026-10-18 chain-fidelity protocol: it probes
whether the two models that passed the three-model review batch
(``m1`` -> ``AI_MODEL_1`` gemini-3.8-flash-high and ``m4`` ->
``AI_MODEL_4`` gpt-6.1-sol) produce legal core adjudications, and whether
the downstream production chain then processes those adjudications
faithfully, intercepts overreach, and keeps non-story text out of the
canonical event. It is an INDEPENDENT run configuration derived from
`fixed_model_probe` (whose two-model R/A configuration and 120-call cap
stay untouched) and reuses the public history calibrated by the
three-model batch (revised event 1) via `three_model_review_probe`:

- stage A (24 real calls): the four frozen adjudication inputs
  (A01-A04, byte-identical to the fixed probe) x m1/m4 x 3 repetitions,
  each driving the real `make_action_adjudication_step` core question
  through the shared fixed-probe wrapper. Raw provider responses, the
  complete recorded action_result, prompts, request parameters and
  private diagnostics are archived unmodified for human labeling; a
  defective raw adjudication stays a recorded A-group failure even if a
  later narration would omit the defect.
- stage FINAL (<= 48 real calls): every archived A-group action_result
  of THIS batch is injected as-is (``record_action_result``, no
  extraction, cleaning or rewriting) into the production
  `make_final_output_step` of the SAME model: one real
  stage_directions generation, programmatic canonical assembly, one
  real semantic review. A malformed generation stops at the production
  path - no compensating review call - and the slot is recorded as
  skipped-by-upstream-failure.
- stage E (<= 48 real calls): four frozen evidence injections
  (E01-E04) x m1/m4 x 3 repetitions through the same real
  generation-plus-review path. These are synthetic test evidence and
  are archived strictly separately from the real A-group core outputs.
  E03 reuses the archived batch-2 A02__m2__r2 English-self-talk sample
  byte-for-byte; its full text and expected label are frozen only after
  the engineer confirms it carries no hidden violating assertion.

Protocol discipline follows the two predecessor probes: fixtures,
labels, model names, execution order and per-stage call caps
(24 / 48 / 48, 120 total) are frozen on disk before the first paid
request; no retries of any kind; a started attempt without an archived
response stays indeterminate and is never re-sent; directories are
never overwritten; the production prompts, parser and wiring are never
modified and no per-model parameter is adjusted. The SDK's actual call
boundary is archived per request by a read-only observation proxy: the
production adapter passes a request-level timeout of 60s
(``language_model.DEFAULT_TIMEOUT_SECONDS``) and does not send top_p or
top_k, so the protocol records those facts instead of the document
defaults. The provider-returned identifier of Gemini
(``gemini-3.8-flash-n`` vs the requested ``gemini-3.8-flash-high``) is
recorded per response and marked as a routing-identity limitation.
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
from concordia.contrib.components.game_master import story_resolution
from concordia.document import interactive_document
from concordia.language_model import language_model
from dotenv import dotenv_values

from examples.story_enactment import fixed_model_probe as fp
from examples.story_enactment import three_model_review_probe as tm

ModelTag = str

_PROBE_NAME = 'action_authorization_chain_probe'
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BATCH_PARENT = _REPO_ROOT / 'run_results' / 'story_enactment' / _PROBE_NAME

_PROTOCOL_NAME = 'protocol.json'
_FIXTURES_NAME = 'fixtures.json'
_SNAPSHOT_NAME = 'source_snapshot.json'
_REPORT_NAME = 'report.json'
_ADJUDICATION_DIR_NAME = 'adjudication'
_FINAL_DIR_NAME = 'final'
_INJECTION_DIR_NAME = 'injection'
_HUMAN_LABELS_NAME = 'human_labels.jsonl'
_HUMAN_FINAL_LABELS_NAME = 'human_final_labels.jsonl'
_HUMAN_E_LABELS_NAME = 'human_e_labels.jsonl'
_ANON_SALT_NAME = 'anonymization_salt.json'
_SEMANTIC_REJECTION_PREFIX = 'Semantic review rejected output:'

# The two models that passed the three-model review batch. AI_MODEL_2
# and AI_MODEL_3 are never read by this probe.
_MODEL_TAGS: tuple[ModelTag, ...] = ('m1', 'm4')
_MODEL_ORDER: tuple[ModelTag, ...] = ('m1', 'm4')
_EXPECTED_MODEL_NAMES: dict[ModelTag, str] = {
    'm1': 'gemini-3.8-flash-high',
    'm4': 'gpt-6.1-sol',
}
_ENV_MODEL_KEYS: dict[ModelTag, str] = {
    'm1': 'AI_MODEL_1',
    'm4': 'AI_MODEL_4',
}

_REPETITIONS = 3
_CAP_STAGE_A = 24  # 4 cases x 2 models x 3 repetitions.
_CAP_STAGE_FINAL = 48  # <= 2 calls for each of the 24 archived results.
_CAP_STAGE_E = 48  # 4 injections x 2 models x 3 reps x <= 2 calls.
_MAX_REAL_CALLS = 120

# The request-level timeout the production adapter actually passes on
# every text call (base_gpt_model forwards its default). Recorded here
# so the protocol describes the real call boundary, not the document
# defaults (top_p/top_k are never sent; see module docstring).
_REQUEST_TIMEOUT_SECONDS = language_model.DEFAULT_TIMEOUT_SECONDS

_STAGE_DRY_RUN = 'dry_run'
_STAGE_FREEZE = 'freeze'
_STAGE_RUN_A = 'run_a'
_STAGE_RUN_FINAL = 'run_final'
_STAGE_RUN_E = 'run_e'
_STAGE_WORKSHEETS = 'worksheets'
_STAGE_SUMMARIZE = 'summarize'
_STAGE_RESUMMARIZE_OFFLINE = 'resummarize_offline'
_STAGES = (
    _STAGE_DRY_RUN,
    _STAGE_FREEZE,
    _STAGE_RUN_A,
    _STAGE_RUN_FINAL,
    _STAGE_RUN_E,
    _STAGE_WORKSHEETS,
    _STAGE_SUMMARIZE,
    _STAGE_RESUMMARIZE_OFFLINE,
)

# ---------------------------------------------------------------------------
# Frozen fixtures: calibrated history, A inputs and E injections.
# ---------------------------------------------------------------------------

# The public history calibrated by the three-model batch (revised
# event 1) is reused verbatim through tm's getters so the two probes
# can never drift.


def public_history_text() -> str:
  """Returns the calibrated frozen public-history text."""
  return tm.public_history_text()


def director_window() -> tuple[str, ...]:
  """Returns the director's recent-event window over the history."""
  return tm.director_window()


# E03: the archived batch-2 (20261017T112000Z) A02__m2__r2
# core_action_result, byte-for-byte. The Chinese portion is a legal
# holder disclosure grounded in the GM-only will facts; the English
# tail is the model's deliberation self-talk that leaked into the raw
# output (batch 2 recorded it as a generation-quality defect). Its
# expected label is frozen only after the engineer confirms the full
# text carries no hidden violating assertion.
_E03_ARCHIVED_RESULT = (
    '取出自己保管的遗嘱文本向众人展开展示，并念出其落款日期。'
    ' Because of that, '
    '周律师将自己保管的遗嘱文本当众展开，念出落款日期：'
    '“二〇一九年三月十二日。”文本上可见陈望山的签名与手印，'
    '未见见证人签名。文本内容与先前宣读的一致，仍写着老宅由陈默继承。'
    'seal remains intact? action says text unfold, not envelope. '
    'Existing history says envelope and text in his possession. '
    'They want display and read date. Date factual. Need mention no new '
    "evidence or verification. Can't say no witness perhaps visible "
    'contents. Correct. Chinese. Also no claiming seal? Original text '
    'perhaps after opening? The envelope shown before, text. Date. '
    "Could note doing so doesn't establish authenticity/when made. "
    'We can provide.'
)

# E04 shares E03's Chinese legal base and differs only in the English
# tail: instead of harmless self-talk it asserts another player's new
# promise and key handover - an autonomous choice no source supports.
_E04_RESULT = (
    '取出自己保管的遗嘱文本向众人展开展示，并念出其落款日期。'
    ' Because of that, '
    '周律师将自己保管的遗嘱文本当众展开，念出落款日期：'
    '“二〇一九年三月十二日。”文本上可见陈望山的签名与手印，'
    '未见见证人签名。文本内容与先前宣读的一致，仍写着老宅由陈默继承。'
    ' 陈母 nods and promises to hand her key of the safe to 林雪 '
    'tomorrow morning.'
)

_E02_VIOLATION_FRAGMENT = '在场众人纷纷点头，明确接受暂停处置老宅的提议'
_E03_SELFTALK_FRAGMENTS = ('seal remains intact', 'We can provide')
_E04_VIOLATION_FRAGMENTS = ('nods and promises to hand her key',)


@dataclasses.dataclass(frozen=True)
class InjectionCase:
  """One frozen stage-E evidence injection.

  Attributes:
    case_id: zero-padded identifier used in file names (e.g. ``E01``).
    name: short machine-readable scene name.
    base_case_id: the frozen A-group input whose speech and action
      intent the injection reuses.
    injected_action_result: the synthetic raw adjudicated outcome,
      injected as-is via ``record_action_result``.
    expected_valid: the frozen expected review verdict; evaluation
      metadata, never enters a model prompt.
    human_rationale: frozen human judgment rationale (metadata only).
    fact_sources: frozen statement of where each disputed fact comes
      from (metadata only).
    nonstory_fragments: substrings that must never surface in the
      canonical event; used by a deterministic cleanliness check.
  """

  case_id: str
  name: str
  base_case_id: str
  injected_action_result: str
  expected_valid: bool
  human_rationale: str
  fact_sources: str
  nonstory_fragments: tuple[str, ...] = ()


def _injection_cases() -> tuple[InjectionCase, ...]:
  """Returns the four frozen stage-E injections.

  E01 and E02 reuse the A04 recording input; E01 is the clean own-act
  recording (F14 semantics, expected pass) while E02 appends other
  players' explicit nodding consent to the pause - an autonomous
  choice without any source, so the whole turn must be rejected even
  if the narration omits it. E03 and E04 reuse the A02 display input;
  they differ only in their English tail (deliberation self-talk vs
  another player's new promise), isolating the two dimensions under
  test: semantic boundary (E04 must be rejected as a whole turn, the
  English violation is not a harmless aside) and output quality (E03
  must pass and its canonical event must stay free of the self-talk;
  expressing the legal result in English is not itself a defect).
  """
  a04 = fp.adjudication_case_by_id('A04')
  a02 = fp.adjudication_case_by_id('A02')
  e01_result = (
      f'{a04.action_intent}'
      f'{fp._INTENT_VARIABLE_TOKEN}'  # pylint: disable=protected-access
      '便笺上多了这一行字，此外没有其他变化。'
  )
  e02_result = f'{e01_result}{_E02_VIOLATION_FRAGMENT}，争执随之平息。'
  return (
      InjectionCase(
          case_id='E01',
          name='legal_personal_note_recording',
          base_case_id='A04',
          injected_action_result=e01_result,
          expected_valid=True,
          human_rationale=(
              "Writing down one's own attributed proposal is the act of"
              ' recording, not certification (rule 7 recording'
              ' semantics; same shape as review fixture F14 and the'
              ' batch-2 A04 core outputs). The narration must preserve'
              ' the completed recording and vouch for nothing else.'
          ),
          fact_sources=(
              "The note content is the speaker's own attributed"
              ' proposal; no agreement is asserted anywhere and history'
              ' records active disagreement (event 8).'
          ),
      ),
      InjectionCase(
          case_id='E02',
          name='recording_plus_explicit_collective_consent',
          base_case_id='A04',
          injected_action_result=e02_result,
          expected_valid=False,
          human_rationale=(
              'Beyond the legal recording, the raw result narrates the'
              " other players' explicit nodding consent to and"
              ' acceptance of the pause - new voluntary choices no'
              ' committed event or attributed speech supports (rule 2'
              ' collective-agreement clause; rule 7 unauthorized fact'
              ' inside the action result). The whole turn must be'
              ' rejected even if the final narration omits the consent'
              ' sentence; the wording is deliberately explicit'
              ' autonomous agreement to avoid the old A04'
              ' 众人先暂歇片刻 ambiguity, which stays a separate listed'
              ' dispute.'
          ),
          fact_sources=(
              'History events 8-23 record ongoing disagreement; no'
              ' player ever consented, promised or paused in any'
              ' committed event.'
          ),
          nonstory_fragments=(_E02_VIOLATION_FRAGMENT,),
      ),
      InjectionCase(
          case_id='E03',
          name='legal_display_plus_english_self_talk',
          base_case_id='A02',
          injected_action_result=_E03_ARCHIVED_RESULT,
          expected_valid=True,
          human_rationale=(
              'Byte-for-byte the archived batch-2 A02__m2__r2 raw'
              ' result: a legal holder disclosure (date, signature,'
              ' thumbprint, missing witness all fixed by the GM-only'
              ' old-will facts; custody by history event 1; public'
              ' reading by history event 0) followed by the model'
              '\u2019s leaked English deliberation self-talk. The'
              ' self-talk adds no new fact - it questions, restates'
              ' history and deliberates - so the expected verdict is'
              ' valid=true: the review must not reject merely because'
              ' of the English text, while the real generation must'
              ' keep the canonical event free of the self-talk and'
              ' retain the legal disclosure substance. Expressing the'
              ' legal result itself in English would not be a defect.'
          ),
          fact_sources=(
              "GM-only truth fixes the old will's date"
              ' (二〇一九年三月十二日), signature, thumbprint and missing'
              ' witness; event 0 publicized the reading; the revised'
              ' event 1 established the lawyer\u2019s custody and his'
              ' earlier envelope display. The English tail asserts'
              ' nothing beyond deliberation over those same facts.'
          ),
          nonstory_fragments=_E03_SELFTALK_FRAGMENTS,
      ),
      InjectionCase(
          case_id='E04',
          name='legal_display_plus_english_player_promise',
          base_case_id='A02',
          injected_action_result=_E04_RESULT,
          expected_valid=False,
          human_rationale=(
              'Differs from E03 only in the English tail: instead of'
              " self-talk it asserts 陈母's new promise and key"
              ' handover - another player\u2019s voluntary commitment'
              ' with no source (rule 2; rule 7 unauthorized fact inside'
              ' the action result). The English wording must not be'
              ' treated as a harmless aside: the whole turn is'
              ' rejected even when the narration omits it.'
          ),
          fact_sources=(
              '陈母 never promised anything or handed over any key in'
              ' any committed event, attributed speech or GM-only'
              ' fact; the safe needs two keys held by 林雪 and 陈母.'
          ),
          nonstory_fragments=_E04_VIOLATION_FRAGMENTS,
      ),
  )


_INJECTION_CASES = _injection_cases()

# The batch-1 (20261017T093000Z) A04__m2__r2 tail that read "依他刚才
# 的话，众人先暂歇片刻，后续仍待各方确认再议" stays a separately
# listed dispute (borderline involuntary rest vs decided behavior);
# it is deliberately NOT reused as an injection sample - E02 uses
# explicit autonomous consent instead.
_DISPUTE_REGISTRY = (
    {
        'id': 'D01',
        'origin': 'batch 20261017T093000Z A04__m2__r2 core_action_result tail',
        'text': '依他刚才的话，众人先暂歇片刻，后续仍待各方确认再议。',
        'status': (
            'listed dispute, not an injection sample: 众人先暂歇片刻'
            ' can be read as a mechanical pause following the speech or'
            ' as decided behavior of other players; E02 replaces it'
            ' with explicit nodding consent to remove the ambiguity.'
        ),
    },
)


def injection_case_by_id(case_id: str) -> InjectionCase:
  """Returns the frozen injection fixture with the given identifier."""
  for case in _INJECTION_CASES:
    if case.case_id == case_id:
      return case
  raise ValueError(f'unknown injection case {case_id!r}')


def attempt_id(case_id: str, model_tag: ModelTag, repetition: int) -> str:
  """Builds the unique attempt key used in file names."""
  return fp.attempt_id(case_id, model_tag, repetition)


def planned_adjudication_attempts() -> list[tuple[str, ModelTag, int]]:
  """Lists the 24 planned stage-A attempts in frozen execution order.

  The order is repetition -> model (m1, m4) -> case id, matching the
  fixed probe's group-A order.
  """
  plan = []
  for repetition in range(1, _REPETITIONS + 1):
    for model_tag in _MODEL_ORDER:
      for case in fp._ADJUDICATION_CASES:  # pylint: disable=protected-access
        plan.append((case.case_id, model_tag, repetition))
  return plan


def planned_injection_attempts() -> list[tuple[str, ModelTag, int]]:
  """Lists the 24 planned stage-E attempts in frozen execution order."""
  plan = []
  for repetition in range(1, _REPETITIONS + 1):
    for model_tag in _MODEL_ORDER:
      for case in _INJECTION_CASES:
        plan.append((case.case_id, model_tag, repetition))
  return plan


def utc_now() -> str:
  """Returns the current UTC time in ISO-8601 with seconds precision."""
  return fp.utc_now()


def sha256_text(text: str) -> str:
  """Returns the hex sha256 of a text's UTF-8 bytes."""
  return fp.sha256_text(text)


def sha256_file(path: pathlib.Path) -> str:
  """Returns the hex sha256 of a file's bytes."""
  return fp.sha256_file(path)


# ---------------------------------------------------------------------------
# Call-boundary observation proxy.
# ---------------------------------------------------------------------------


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


class _ObservingCompletions:
  """Pass-through `chat.completions` proxy archiving the call boundary.

  The proxy never alters the request or the response. Per request it
  archives the exact create() kwargs the SDK received at the call
  boundary (except the messages body, whose final user message is the
  prompt archived separately by the wrapper), plus the provider-returned
  model identifier and token usage. Observation failures never break
  the call.
  """

  def __init__(self, real: Any, records: list[dict[str, Any]]):
    self._real = real
    self._records = records

  def create(self, *args: Any, **kwargs: Any) -> Any:
    """Forwards one request and archives its call-boundary parameters."""
    response = self._real.create(*args, **kwargs)
    try:
      boundary = {
          key: _jsonable(value)
          for key, value in kwargs.items()
          if key != 'messages'
      }
      messages = kwargs.get('messages')
      self._records.append({
          'requested_model': kwargs.get('model'),
          'boundary_kwargs': boundary,
          'boundary_kwargs_note': (
              'create() kwargs at the call boundary, messages excluded'
              ' (the final user message is the archived prompt);'
              ' timeout is request-level and never appears in the HTTP'
              ' body.'
          ),
          'messages_count': len(messages) if messages is not None else None,
          'provider_model': getattr(response, 'model', None),
          'usage': _jsonable(getattr(response, 'usage', None)),
          'observed_at_utc': utc_now(),
      })
    except Exception:  # pylint: disable=broad-except
      pass  # Observation must never mask or replace the real response.
    return response


class _ObservingChat:
  """`chat` facade exposing the observing completions proxy."""

  def __init__(self, real_chat: Any, records: list[dict[str, Any]]):
    self.completions = _ObservingCompletions(real_chat.completions, records)


class _ObservingClient:
  """Client facade exposing an observing `chat.completions.create`."""

  def __init__(self, real: Any, records: list[dict[str, Any]]):
    self._real = real
    self._records = records
    self.chat = _ObservingChat(real.chat, records)

  def __getattr__(self, name: str) -> Any:
    return getattr(self._real, name)

  def with_options(self, **kwargs: Any) -> '_ObservingClient':
    return _ObservingClient(self._real.with_options(**kwargs), self._records)


def _create_live_model(
    model_name: str, *, api_key: str, api_base: str
) -> tuple[language_model.LanguageModel, list[dict[str, Any]]]:
  """Builds the live model with boundary observation and retries off.

  Same OpenAI-compatible interface and configuration source as the
  archived runs. The only client-level hardening is disabling the
  SDK's internal retries; the timeout is NOT overridden here because
  the production adapter already passes a request-level timeout of
  60s on every text call, which is the value that actually governs
  each request (archived per response by the observation proxy).

  Args:
    model_name: the live model's actual name.
    api_key: the API credential (never archived).
    api_base: the OpenAI-compatible endpoint.

  Returns:
    A (live model, boundary observation records) tuple. The records
    list grows by one entry per raw response; attempts attribute their
    entries positionally because the runner is strictly sequential.
  """
  model = fp.gpt_model.GptLanguageModel(
      model_name=model_name, api_key=api_key, api_base=api_base
  )
  records: list[dict[str, Any]] = []
  observing = _ObservingClient(
      model._client, records  # pylint: disable=protected-access
  )
  model._client = observing.with_options(  # pylint: disable=protected-access
      max_retries=fp._SDK_MAX_RETRIES,  # pylint: disable=protected-access
  )
  return model, records


# ---------------------------------------------------------------------------
# Probe wrappers.
# ---------------------------------------------------------------------------


class _HarnessError(Exception):
  """Raised when a probe routing invariant is violated.

  This is a probe fault, not a model fault: it is never forwarded to
  the API and always classifies the attempt as ``harness_error``.
  """


class _DualForwardModel(language_model.LanguageModel):
  """Two-slot wrapper forwarding generation AND review to the live model.

  The first text call must be the production generation task (its
  prompt carries the ``# Candidate event`` heading and not the review
  heading); it is forwarded verbatim to the live model. The second
  call must be the production semantic review (``# Story resolution
  semantic review``); it is also forwarded verbatim. Any further text
  call, any choice call or unexpected routing raises `_HarnessError`
  before touching the API.
  """

  def __init__(
      self,
      real_model: language_model.LanguageModel,
      *,
      on_generation_start: Callable[[str, dict[str, Any]], None] | None = None,
      on_review_start: Callable[[str, dict[str, Any]], None] | None = None,
  ):
    """Initializes the wrapper for one attempt.

    Args:
      real_model: the live model answering both slots.
      on_generation_start: invoked with (prompt, params) immediately
        before the real generation request.
      on_review_start: invoked with (prompt, params) immediately before
        the real review request.
    """
    self._real = real_model
    self._on_generation_start = on_generation_start
    self._on_review_start = on_review_start
    self.generation_calls = 0
    self.review_calls = 0
    self.captured: list[dict[str, Any]] = []

  def _forward(
      self,
      slot: str,
      on_start: Callable[[str, dict[str, Any]], None] | None,
      **kwargs: Any,
  ) -> str:
    started = time.monotonic()
    if on_start is not None:
      on_start(kwargs['prompt'], kwargs['params'])
    response = self._real.sample_text(
        kwargs['prompt'],
        max_tokens=kwargs['max_tokens'],
        terminators=kwargs['terminators'],
        temperature=kwargs['temperature'],
        top_p=kwargs['top_p'],
        top_k=kwargs['top_k'],
    )
    self.captured.append({
        'slot': slot,
        'prompt': kwargs['prompt'],
        'params': kwargs['params'],
        'response': response,
        'wall_seconds': round(time.monotonic() - started, 3),
    })
    return response

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
    """Routes one text call to the generation or the review slot."""
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
        and fp._GENERATION_MARK in prompt  # pylint: disable=protected-access
        and fp._REVIEW_MARK not in prompt  # pylint: disable=protected-access
    ):
      self.generation_calls += 1
      return self._forward(
          'generation_forward',
          self._on_generation_start,
          prompt=prompt,
          params=params,
          max_tokens=max_tokens,
          terminators=terminators,
          temperature=temperature,
          top_p=top_p,
          top_k=top_k,
      )
    if (
        self.generation_calls == 1
        and self.review_calls == 0
        and fp._REVIEW_MARK in prompt  # pylint: disable=protected-access
    ):
      self.review_calls += 1
      return self._forward(
          'review_forward',
          self._on_review_start,
          prompt=prompt,
          params=params,
          max_tokens=max_tokens,
          terminators=terminators,
          temperature=temperature,
          top_p=top_p,
          top_k=top_k,
      )
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


class _OfflineFinalStub(language_model.LanguageModel):
  """Offline two-slot stub for dry runs and unit tests.

  Answers the generation slot with a canned valid single-field JSON
  object and the review slot with a canned valid verdict, so the full
  production final-output step runs to completion offline. Responses
  are configurable per slot for exception-classification rehearsals.
  """

  def __init__(
      self,
      *,
      generation_response: str | None = None,
      review_response: str | None = None,
      generation_error: Exception | None = None,
      review_error: Exception | None = None,
  ):
    self._generation_response = generation_response or json.dumps(
        {'stage_directions': '干跑占位旁白：仅用于离线路由验证。'},
        ensure_ascii=False,
    )
    self._review_response = review_response or json.dumps(
        {'valid': True, 'reason': 'dry-run placeholder; not scored.'},
        ensure_ascii=False,
    )
    self._generation_error = generation_error
    self._review_error = review_error
    self.generation_calls = 0
    self.review_calls = 0

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
    """Answers the generation and review slots in order."""
    del max_tokens, terminators, temperature, top_p, top_k, timeout, seed
    if (
        fp._GENERATION_MARK in prompt  # pylint: disable=protected-access
        and fp._REVIEW_MARK not in prompt  # pylint: disable=protected-access
    ):
      if self.generation_calls or self.review_calls:
        raise _HarnessError('unexpected second generation call')
      self.generation_calls += 1
      if self._generation_error is not None:
        raise self._generation_error
      return self._generation_response
    if fp._REVIEW_MARK in prompt:  # pylint: disable=protected-access
      if self.generation_calls != 1 or self.review_calls:
        raise _HarnessError('unexpected review routing')
      self.review_calls += 1
      if self._review_error is not None:
        raise self._review_error
      return self._review_response
    raise _HarnessError(f'unexpected prompt head={prompt[:120]!r}')

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


# ---------------------------------------------------------------------------
# Final-stage attempt execution (stages FINAL and E share it).
# ---------------------------------------------------------------------------


def _write_started_marker(
    *,
    path: pathlib.Path,
    prompt_file: pathlib.Path,
    key: str,
    stage: str,
    case_id: str,
    model_tag: ModelTag,
    model_name: str,
    repetition: int,
    slot: str,
    prompt: str,
    params: Mapping[str, Any],
) -> None:
  """Writes one slot's attempt-started marker and prompt file."""
  path.write_text(
      json.dumps(
          {
              'attempt_id': key,
              'stage': stage,
              'case_id': case_id,
              'model_tag': model_tag,
              'model': model_name,
              'repetition': repetition,
              'slot': slot,
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
  prompt_file.write_text(prompt, encoding='utf-8')


def _run_final_stage_attempt(
    *,
    stage: str,
    case_id: str,
    player: str,
    speech: str,
    action_intent: str,
    injected_action_result: str,
    real_model: language_model.LanguageModel,
    model_tag: ModelTag,
    model_name: str,
    repetition: int,
    gm: Mapping[str, Any],
    out_dir: pathlib.Path,
    secrets: Sequence[str],
    expected_valid: bool | None,
    nonstory_fragments: Sequence[str] = (),
    source_attempt_id: str | None = None,
    enrich_result: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
  """Runs one real generation-plus-review final-output resolution.

  The archived or frozen adjudicated outcome is injected as-is through
  the production director API (``record_action_result``), never
  extracted, cleaned or rewritten, and the premise handed to the
  production step is exactly that raw string. Both production calls -
  the stage_directions generation and the semantic review - are
  forwarded to the live model. A malformed generation stops at the
  production path: no compensating review call is made and the slot is
  recorded as a structural failure.

  Args:
    stage: ``final`` or ``injection``.
    case_id: the fixture identifier (A-case id or E-case id).
    player: the active player character.
    speech: the turn's original speech.
    action_intent: the turn's own attempted action.
    injected_action_result: the raw adjudicated outcome, injected
      verbatim.
    real_model: the live model answering both slots.
    model_tag: ``m1`` or ``m4``.
    model_name: the live model's actual name.
    repetition: 1-based repetition index.
    gm: the frozen game-master params.
    out_dir: the stage artefacts directory.
    secrets: credential values to redact from archived messages.
    expected_valid: the frozen expected review verdict, or None when
      the expectation is derived later (stage FINAL derives it from
      the human A labels at summarize time).
    nonstory_fragments: substrings that must never surface in the
      canonical event (deterministic cleanliness check).
    source_attempt_id: the stage-A attempt this result came from, for
      stage FINAL; None for stage E.
    enrich_result: optional hook applied to the result record right
      before it is written.

  Returns:
    The result record; also written to ``<attempt>_result.json``.
  """
  key = attempt_id(case_id, model_tag, repetition)
  stem = f'{case_id}__{model_tag}__r{repetition}'
  generation_started = out_dir / f'{stem}_generation_started.json'
  generation_prompt_file = out_dir / f'{stem}_generation_prompt.txt'
  review_started = out_dir / f'{stem}_review_started.json'
  review_prompt_file = out_dir / f'{stem}_review_prompt.txt'
  generation_response_file = out_dir / f'{stem}_generation_response.txt'
  review_response_file = out_dir / f'{stem}_review_response.txt'
  result_file = out_dir / f'{stem}_result.json'
  started_utc: list[str] = []

  def on_generation_start(prompt: str, params: dict[str, Any]) -> None:
    _write_started_marker(
        path=generation_started,
        prompt_file=generation_prompt_file,
        key=key,
        stage=stage,
        case_id=case_id,
        model_tag=model_tag,
        model_name=model_name,
        repetition=repetition,
        slot='generation',
        prompt=prompt,
        params=params,
    )
    started_utc.append(utc_now())

  def on_review_start(prompt: str, params: dict[str, Any]) -> None:
    _write_started_marker(
        path=review_started,
        prompt_file=review_prompt_file,
        key=key,
        stage=stage,
        case_id=case_id,
        model_tag=model_tag,
        model_name=model_name,
        repetition=repetition,
        slot='review',
        prompt=prompt,
        params=params,
    )
    started_utc.append(utc_now())

  attempt_started = time.monotonic()
  director, entity, _ = fp._make_director(player=player, gm=gm)  # pylint: disable=protected-access
  raw = fp._open_turn(entity, player, speech, action_intent)  # pylint: disable=protected-access
  director.record_action_result(injected_action_result)
  wrapper = _DualForwardModel(
      real_model,
      on_generation_start=on_generation_start,
      on_review_start=on_review_start,
  )
  chain = interactive_document.InteractiveDocument(wrapper)
  history = public_history_text()
  step = story_resolution.make_final_output_step(
      director,
      immutable_facts=gm['immutable_facts'],
      character_constraints=gm['character_constraints'],
      player_names=gm['player_names'],
      world_constraints=gm['world_constraints'],
      allowed_environment_events=gm['allowed_environment_events'],
      public_facts=gm['public_facts'],
      public_history=lambda: history,
  )
  stage_outcome = None
  stage_error = None
  resolved_event = None
  stage_directions = None
  try:
    resolved_event = step(chain, injected_action_result, player)
    stage_outcome = 'approved'
  except story_resolution.StoryOutputError as error:
    message = str(error)
    if (
        message.startswith(_SEMANTIC_REJECTION_PREFIX)
        and wrapper.review_calls == 1
    ):
      stage_outcome = 'semantic_rejected'
    elif wrapper.review_calls == 1:
      stage_outcome = 'review_malformed'
    else:
      # The production path stopped before the review (malformed
      # generation, empty narrator track or empty canonical event):
      # no compensating review call is ever made.
      stage_outcome = 'generation_structural_failure'
    stage_error = message
  except _HarnessError as error:
    stage_outcome = 'harness_error'
    stage_error = str(error)
  except Exception as error:  # pylint: disable=broad-except
    stage_outcome = 'api_error'
    stage_error = fp._redact(  # pylint: disable=protected-access
        f'{type(error).__name__}: {error}', secrets
    )
  wall_seconds = round(time.monotonic() - attempt_started, 3)

  generation_response = None
  review_response = None
  generation_params = None
  review_params = None
  generation_wall = None
  review_wall = None
  prompt_hashes = {}
  for capture in wrapper.captured:
    if capture['slot'] == 'generation_forward':
      generation_response = capture['response']
      generation_params = capture['params']
      generation_wall = capture.get('wall_seconds')
      prompt_hashes['generation'] = sha256_text(capture['prompt'])
      generation_response_file.write_text(generation_response, encoding='utf-8')
    elif capture['slot'] == 'review_forward':
      review_response = capture['response']
      review_params = capture['params']
      review_wall = capture.get('wall_seconds')
      prompt_hashes['review'] = sha256_text(capture['prompt'])
      review_response_file.write_text(review_response, encoding='utf-8')
  if resolved_event is not None and generation_response is not None:
    try:
      stage_directions = story_resolution._parse_stage_directions(  # pylint: disable=protected-access
          generation_response
      )
    except story_resolution.StoryOutputError:
      stage_directions = None

  verdict = None
  review_reason = None
  classification = None
  if review_response is not None:
    verdict, review_reason, _ = fp._parse_review_verdict(  # pylint: disable=protected-access
        review_response
    )
  if stage_outcome == 'generation_structural_failure':
    # The production path stopped before the review: the review slot is
    # recorded as not executed due to upstream failure.
    classification = 'generation_malformed'
  elif review_response is not None and not review_response.strip():
    classification = 'provider_error'
  elif review_response is not None and verdict is None:
    classification = 'malformed'
  elif stage_outcome == 'harness_error':
    classification = 'harness_error'
  elif verdict is not None and expected_valid is not None:
    if verdict == expected_valid:
      classification = 'correct'
    elif expected_valid:
      classification = 'false_rejection'
    else:
      classification = 'missed_detection'
  cross_check_ok = None
  if stage_outcome in ('approved', 'semantic_rejected') and verdict is not None:
    cross_check_ok = (stage_outcome == 'approved') == (verdict is True)
  canonical_nonstory = None
  if resolved_event is not None and nonstory_fragments:
    canonical_nonstory = any(
        fragment in resolved_event for fragment in nonstory_fragments
    )
  # A terminal, fully classified outcome counts as completed even when
  # the label-derived classification is deferred to summarize time
  # (stage FINAL runs before the human A labels exist).
  if stage_outcome in (
      'approved',
      'semantic_rejected',
      'review_malformed',
      'generation_structural_failure',
  ):
    status = 'completed'
  else:
    status = stage_outcome
  result = {
      'attempt_id': key,
      'stage': stage,
      'case_id': case_id,
      'model_tag': model_tag,
      'model': model_name,
      'repetition': repetition,
      'source_attempt_id': source_attempt_id,
      'status': status,
      'classification': classification,
      'expected_valid': expected_valid,
      'verdict': verdict,
      'review_reason': review_reason,
      'stage_outcome': stage_outcome,
      'stage_error': stage_error,
      'review_executed': wrapper.review_calls == 1,
      'resolved_event': resolved_event,
      'stage_directions': stage_directions,
      'canonical_known_nonstory': canonical_nonstory,
      'injected_action_result': injected_action_result,
      'injected_action_result_sha256': sha256_text(injected_action_result),
      'raw_action': raw,
      'request_params': {
          'generation': generation_params,
          'review': review_params,
      },
      'prompt_sha256': prompt_hashes,
      'generation_response_file': (
          generation_response_file.name
          if generation_response_file.exists()
          else None
      ),
      'review_response_file': (
          review_response_file.name if review_response_file.exists() else None
      ),
      'real_api_calls': wrapper.generation_calls + wrapper.review_calls,
      'generation_wall_seconds': generation_wall,
      'review_wall_seconds': review_wall,
      'attempt_wall_seconds': wall_seconds,
      'request_started_at_utc': started_utc[0] if started_utc else None,
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
  """Builds the frozen fixtures document for the chain batch.

  Evaluation metadata (expected labels, rationales, fact sources,
  judgment focuses, the final-stage expected-verdict derivation rule)
  never enters any model prompt. The payload also freezes the dispute
  registry and the E-sample provenance.
  """
  return {
      'shared_context': {
          'history_note': (
              '24 committed public events, oldest first; identical to'
              ' the three-model batch 20261017T143746Z (revised event'
              ' 1: speech plus committed own-action narration of the'
              " lawyer's envelope display and custody). Director window"
              ' is the last 20 events.'
          ),
          'public_facts': fp.gm_params()['public_facts'],
          'gm_constraints_source': (
              'story_config.make_example_config() via'
              ' run_lib.make_gm_params(): GM-only constraints, immutable'
              ' facts, player names and the environment whitelist.'
          ),
          'history_events': list(tm._HISTORY),  # pylint: disable=protected-access
          'director_window_size': fp._DIRECTOR_WINDOW_SIZE,  # pylint: disable=protected-access
      },
      'adjudication_cases': [
          dataclasses.asdict(case) for case in fp._ADJUDICATION_CASES  # pylint: disable=protected-access
      ],
      'injection_cases': [
          dataclasses.asdict(case) for case in _INJECTION_CASES
      ],
      'injection_provenance': {
          'E03': (
              'byte-for-byte the archived batch-2 20261017T112000Z'
              ' A02__m2__r2 core_action_result; expected label frozen'
              ' only after the engineer confirmed the full text carries'
              ' no hidden violating assertion'
          ),
          'E04': (
              "E03's Chinese legal base with a newly composed English"
              " tail asserting another player's new promise and key"
              ' handover'
          ),
          'E01_E02': (
              "A04's frozen speech and action intent with an F14-shaped"
              ' clean recording result; E02 appends explicit collective'
              ' nodding consent'
          ),
      },
      'final_stage_expected_verdict_rule': (
          'For each stage-A attempt the human label decides: the raw'
          ' adjudication labeled legal (pass=true) must be APPROVED by'
          ' the final review of the same model; one labeled illegal'
          ' (pass=false) must be REJECTED whole-turn even if the final'
          ' narration omits the offending part. Labels are confirmed'
          ' by the engineer after run_a; the machine classification of'
          ' stage FINAL is applied only afterwards, at summarize.'
      ),
      'dispute_registry': _DISPUTE_REGISTRY,
  }


def protocol_payload(model_names: Mapping[ModelTag, str]) -> dict[str, Any]:
  """Builds the frozen protocol document for the chain batch."""
  return {
      'protocol_version': 1,
      'created_utc': utc_now(),
      'probe': _PROBE_NAME,
      'models': {tag: model_names[tag] for tag in _MODEL_TAGS},
      'model_config_source': {
          'api_type': 'openai',
          'env_model_keys': _ENV_MODEL_KEYS,
          'note': (
              'AI_MODEL_2 and AI_MODEL_3 are never read. Requested'
              ' model names are the .env values; provider-returned'
              ' identifiers are archived per response and may differ.'
          ),
          'env_base_url_key': 'AI_BASE_URL',
          'env_api_key_key': 'AI_API_KEY',
          'client': (
              'GptLanguageModel with client-level max_retries=0 only'
              ' (SDK auto-retry and probe-outer retry disabled). No'
              ' client-level timeout override: every text call'
              ' carries the production request-level timeout of 60s'
              ' (language_model.DEFAULT_TIMEOUT_SECONDS), which is'
              ' what actually governs each request.'
          ),
          'routing_identity_limitation': (
              'Prior batches observed the provider returning'
              ' "gemini-3.8-flash-n" for requested'
              ' "gemini-3.8-flash-high" (48/48). The discrepancy is'
              ' not resolved; it is recorded per response as a'
              ' routing-identity limitation, not as a pass criterion.'
          ),
      },
      'request_settings': {
          'call_boundary_note': (
              'The observation proxy archives the exact create()'
              ' kwargs per request. The production adapter sends'
              ' temperature=1.0, max_completion_tokens, seed=null,'
              ' reasoning_effort=low, verbosity=low and a request-'
              ' level timeout=60s; it does NOT send top_p or top_k'
              ' (deleted by base_gpt_model before the call). The'
              ' document defaults top_p=0.95/top_k=64 are therefore'
              ' NOT in effect and are not recorded as such.'
          ),
          'adjudication_forward': {
              'max_tokens': fp._ADJUDICATION_MAX_TOKENS,  # pylint: disable=protected-access
              'terminators': [],
          },
          'final_generation': {
              'max_tokens': fp._GENERATION_MAX_TOKENS,  # pylint: disable=protected-access
              'terminators': [],
          },
          'final_review': {
              'max_tokens': fp._REVIEW_MAX_TOKENS,  # pylint: disable=protected-access
              'terminators': [],
          },
          'request_timeout_seconds': _REQUEST_TIMEOUT_SECONDS,
          'sdk_max_retries': fp._SDK_MAX_RETRIES,  # pylint: disable=protected-access
      },
      'schedule': {
          'repetitions_per_case_per_model': _REPETITIONS,
          'adjudication_cases': len(
              fp._ADJUDICATION_CASES  # pylint: disable=protected-access
          ),
          'injection_cases': len(_INJECTION_CASES),
          'planned_adjudication_calls': _CAP_STAGE_A,
          'planned_final_calls_max': _CAP_STAGE_FINAL,
          'planned_injection_calls_max': _CAP_STAGE_E,
          'max_real_calls': _MAX_REAL_CALLS,
          'execution_order': (
              'run_a, then run_final over the archived results of THIS'
              ' batch in the same A order, then run_e. Within each'
              ' stage: repetition -> model (m1, m4) -> case id. A'
              ' stage-FINAL slot whose upstream adjudication produced'
              ' no archived action_result is recorded'
              ' skipped_upstream and costs no calls. Simulated calls'
              ' (dry run, unit-test stubs) are counted separately and'
              ' never touch the batch counters.'
          ),
      },
      'prompt_binding': {
          'same_prompt_across_models_and_repetitions': True,
          'model_name_in_prompt': False,
          'labels_in_prompt': False,
          'production_prompts_modified': False,
          'per_model_parameters': False,
      },
      'no_retry_policy': (
          'No retry of any kind: a semantic failure is recorded as-is;'
          ' a network/API error (including a 60s request timeout) is'
          ' recorded as api_error for that attempt and is never'
          ' replaced by a later successful response. No per-model'
          ' budget increase, reasoning change or parameter change may'
          ' be mixed into this batch; adjustments require a new'
          ' protocol.'
      ),
      'interruption_policy': (
          'An attempt whose request started but produced no archived'
          ' response is started_no_result: it stays indeterminate, is'
          ' never silently re-sent, and nothing may be claimed about'
          ' whether it was delivered or charged. Unstarted planned'
          ' entries may continue, but the batch then fails the'
          ' completeness gate. A malformed stage-FINAL generation'
          ' stops at the production path: the review slot is never'
          ' compensatorily called and is recorded as not executed due'
          ' to upstream failure.'
      ),
      'labeling_policy': (
          'Labels are confirmed by the engineer. Worksheets anonymize'
          ' model identity (alias = sha256(salt + attempt_id)); the'
          ' salt file stays in the batch directory and is not read'
          ' during review. The A-group raw adjudication is labeled on'
          ' its own content: unauthorized player action, fabricated'
          ' evidence, illegal disclosure, ungrounded plot change,'
          ' definiteness/grounding, and leaked reasoning / self-talk'
          ' / instruction text (English wording of a legal result is'
          ' not itself a defect). A defective raw adjudication stays'
          ' a recorded A failure even if a later narration omits it.'
      ),
      'pass_criteria': {
          'stage_a_per_model': (
              'all 12 planned entries completed and labeled; zero'
              ' unauthorized player actions, fabricated evidence,'
              ' illegal disclosures, ungrounded plot changes; every'
              ' outcome definite and grounded (full success is NOT'
              ' required); zero leaked reasoning/self-talk/'
              ' instruction text; 0 api/provider/harness errors'
          ),
          'stage_final_per_model': (
              'every archived adjudication of this batch processed'
              ' through the real final path: 0 generation_malformed,'
              ' 0 malformed reviews, 0 api/provider/harness errors,'
              ' 0 cross-check mismatches; the review verdict correct'
              ' against the label-derived expectation (legal ->'
              ' approved with the material result retained, illegal'
              ' -> rejected whole-turn even when the narration omits'
              ' the violation); human labels confirm substance'
              ' retention, no new other-player choices and no'
              ' non-story text in the canonical event'
          ),
          'stage_e_per_model': (
              'all 12 planned entries completed and correctly'
              ' classified (E01/E03 approved, E02/E04 rejected'
              ' whole-turn); E03 canonical free of the frozen'
              ' self-talk fragments while retaining the legal'
              ' substance, and never rejected merely for English;'
              ' E04 never passed off as a harmless aside'
          ),
          'overall': (
              'all three stage gates pass for BOTH models; real_api_calls'
              ' <= 120; hashes verified; all human labels complete;'
              ' no unresolved sample disputes'
          ),
          'full_story_gate': (
              'entering the four full-story runs (Gemini/Sol x '
              'speech_only/speech_and_action) additionally requires'
              ' the chain gate above with no open interface errors,'
              ' missing results or label disputes; those runs are a'
              ' separate frozen decision'
          ),
          'note': (
              'A single failure of one case fails the gate; three'
              ' repetitions are never treated as a majority vote.'
              ' Passing this finite-sample gate is not a guarantee of'
              ' error-free model behaviour.'
          ),
      },
      'classification_table': {
          'expected_true_and_verdict_true': 'correct',
          'expected_true_and_verdict_false': 'false_rejection',
          'expected_false_and_verdict_false': 'correct',
          'expected_false_and_verdict_true': 'missed_detection',
          'undecodable_review_or_schema_violation': 'malformed',
          'generation_stopped_at_production_path': 'generation_malformed',
          'network_or_provider_failure_including_timeout': (
              'api_error/provider_error'
          ),
          'never_started': 'not_started',
          'started_without_archived_response': (
              'started_no_result (indeterminate; delivery and billing unknown)'
          ),
          'upstream_adjudication_failed': 'skipped_upstream',
          'probe_fault': 'harness_error',
      },
  }


_SNAPSHOT_FILES = (
    'examples/story_enactment/adjudication_chain_probe.py',
    'examples/story_enactment/adjudication_chain_probe_test.py',
    'examples/story_enactment/fixed_model_probe.py',
    'examples/story_enactment/fixed_model_probe_test.py',
    'examples/story_enactment/three_model_review_probe.py',
    'examples/story_enactment/three_model_review_probe_test.py',
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
)

_README_NAME = 'README.md'

# The offline re-summary may only run with source changes that were
# approved for this closeout: the summary tool and its test. Every
# production file must stay byte-identical to the experiment snapshot.
_RESUMMARIZE_ALLOWED_SOURCE_CHANGES = (
    'examples/story_enactment/adjudication_chain_probe.py',
    'examples/story_enactment/adjudication_chain_probe_test.py',
)


def source_snapshot_payload() -> dict[str, Any]:
  """Builds the source snapshot document."""
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
          'Per-file content hashes of the exact bytes this batch'
          ' probes; run stages re-verify these hashes before any'
          ' request. The production files are byte-identical to the'
          ' three-model batch 20261017T143746Z snapshot.'
      ),
  }


# ---------------------------------------------------------------------------
# Batch bookkeeping.
# ---------------------------------------------------------------------------


def _load_settings() -> tuple[dict[ModelTag, str], str, str, list[str]]:
  """Resolves the two live model names and credentials from .env."""
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


def _write_json(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
  """Writes a JSON document with a trailing newline."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


def _verify_frozen(batch_dir: pathlib.Path) -> None:
  """Re-verifies the frozen artefacts before any request."""
  protocol_path = batch_dir / _PROTOCOL_NAME
  fixtures_path = batch_dir / _FIXTURES_NAME
  snapshot_path = batch_dir / _SNAPSHOT_NAME
  for path in (protocol_path, fixtures_path, snapshot_path):
    if not path.exists():
      raise ValueError(f'frozen artefact missing: {path}')
  names, _, _, _ = _load_settings()
  frozen_protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
  expected_protocol = protocol_payload(names)
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
  """Writes the frozen protocol, fixtures, snapshot and salt."""
  if batch_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {batch_dir}')
  names, _, _, _ = _load_settings()
  for name in (_ADJUDICATION_DIR_NAME, _FINAL_DIR_NAME, _INJECTION_DIR_NAME):
    (batch_dir / name).mkdir(parents=True)
  _write_json(batch_dir / _PROTOCOL_NAME, protocol_payload(names))
  _write_json(batch_dir / _FIXTURES_NAME, fixtures_payload())
  _write_json(batch_dir / _SNAPSHOT_NAME, source_snapshot_payload())
  _write_json(
      batch_dir / _ANON_SALT_NAME,
      {
          'created_utc': utc_now(),
          'salt': (
              hashlib.sha256(
                  (
                      datetime_lib.datetime.now(
                          datetime_lib.timezone.utc
                      ).isoformat()
                      + str(batch_dir)
                  ).encode('utf-8')
              ).hexdigest()
          ),
          'note': (
              'Worksheet aliases are sha256(salt + attempt_id)[:12];'
              ' reviewers must not read this file before labeling.'
          ),
      },
  )
  report = {
      'probe': _PROBE_NAME,
      'protocol_sha256': sha256_file(batch_dir / _PROTOCOL_NAME),
      'fixtures_sha256': sha256_file(batch_dir / _FIXTURES_NAME),
      'source_snapshot_sha256': sha256_file(batch_dir / _SNAPSHOT_NAME),
      'models': dict(names),
      'counts': {
          'planned_adjudication_attempts': len(planned_adjudication_attempts()),
          'planned_final_attempts': len(planned_adjudication_attempts()),
          'planned_injection_attempts': len(planned_injection_attempts()),
          'real_api_calls': 0,
          'real_api_calls_stage_a': 0,
          'real_api_calls_stage_final': 0,
          'real_api_calls_stage_e': 0,
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
  """Writes the batch report atomically."""
  target = batch_dir / _REPORT_NAME
  tmp = target.with_suffix('.json.tmp')
  _write_json(tmp, report)
  tmp.replace(target)


def _attempt_files_exist(out_dir: pathlib.Path, stem: str) -> bool:
  """Returns whether an attempt already produced any on-disk evidence."""
  suffixes = [
      '_started.json',
      '_result.json',
      '_generation_started.json',
      '_review_started.json',
      '_response.txt',
  ]
  return any((out_dir / f'{stem}{suffix}').exists() for suffix in suffixes)


def _alias(batch_dir: pathlib.Path, key: str) -> str:
  """Derives the stable worksheet alias of an attempt id."""
  salt = json.loads((batch_dir / _ANON_SALT_NAME).read_text(encoding='utf-8'))[
      'salt'
  ]
  return 'item-' + hashlib.sha256((salt + key).encode('utf-8')).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Stage runners.
# ---------------------------------------------------------------------------


def _enrich_with_observations(
    records: list[dict[str, Any]],
    first_new_record: int,
    names: Mapping[ModelTag, str],
    model_tag: ModelTag,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
  """Builds an enrich hook attaching this attempt's observations."""

  def enrich(result: dict[str, Any]) -> dict[str, Any]:
    """Attaches requested/provider model and boundary observations."""
    observed = list(records[first_new_record:])
    result['requested_model'] = names[model_tag]
    result['provider_observations'] = observed
    providers = [entry['provider_model'] for entry in observed]
    result['provider_model'] = providers[-1] if providers else None
    usage = observed[-1]['usage'] if observed else None
    if usage:
      result['token_usage'] = usage
    boundary = [entry['boundary_kwargs'] for entry in observed]
    result['call_boundary_kwargs'] = boundary
    return result

  return enrich


def _run_adjudication_group(
    batch_dir: pathlib.Path, *, real_call_cap: int | None = None
) -> None:
  """Runs the pending stage-A adjudication attempts."""
  cap = _CAP_STAGE_A if real_call_cap is None else real_call_cap
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  names, api_key, base_url, secrets = _load_settings()
  out_dir = batch_dir / _ADJUDICATION_DIR_NAME
  live: dict[ModelTag, tuple[language_model.LanguageModel, list]] = {}
  for tag in _MODEL_TAGS:
    live[tag] = _create_live_model(
        names[tag], api_key=api_key, api_base=base_url
    )
  gm = fp.gm_params()
  history = public_history_text()
  stage_counter = report['counts']['real_api_calls_stage_a']
  for case_id, model_tag, repetition in planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    if _attempt_files_exist(out_dir, stem):
      continue
    if (
        stage_counter >= cap
        or report['counts']['real_api_calls'] + 1 > _MAX_REAL_CALLS
    ):
      raise ValueError('real-call cap reached; refusing to send more requests')
    model, records = live[model_tag]
    first_new_record = len(records)
    case = fp.adjudication_case_by_id(case_id)

    def enrich(result: dict[str, Any]) -> dict[str, Any]:
      """Attaches this attempt's provider observations to its record."""
      observed = list(records[first_new_record:])
      result['requested_model'] = names[model_tag]
      result['provider_model'] = (
          observed[-1]['provider_model'] if observed else None
      )
      usage = observed[-1]['usage'] if observed else None
      if usage:
        result['token_usage'] = usage
      result['provider_observations'] = observed
      result['call_boundary_kwargs'] = [
          entry['boundary_kwargs'] for entry in observed
      ]
      return result

    result = fp._run_adjudication_attempt(  # pylint: disable=protected-access
        case=case,
        real_model=model,
        model_tag=model_tag,
        model_name=names[model_tag],
        repetition=repetition,
        gm=gm,
        out_dir=out_dir,
        secrets=secrets,
        history_text=history,
        enrich_result=enrich,
    )
    stage_counter += result['real_api_calls']
    report['counts']['real_api_calls'] += result['real_api_calls']
    report['counts']['real_api_calls_stage_a'] += result['real_api_calls']
    report['attempts'].append({
        'attempt_id': result['attempt_id'],
        'stage': 'adjudication',
        'status': result['status'],
        'prompt_file': result['prompt_file'],
        'response_file': result['response_file'],
    })
    _save_report(batch_dir, report)
    print(f'{result["attempt_id"]}: {result["status"]}', flush=True)


def _archived_action_result(
    batch_dir: pathlib.Path, stem: str
) -> tuple[str | None, str | None]:
  """Reads an archived adjudication's outcome from this batch.

  Args:
    batch_dir: the batch directory.
    stem: the stage-A attempt stem (e.g. ``A01__m1__r1``).

  Returns:
    A (core_action_result, upstream_status) tuple; the result is None
    when the archived attempt did not complete.
  """
  path = batch_dir / _ADJUDICATION_DIR_NAME / f'{stem}_result.json'
  if not path.exists():
    started = batch_dir / _ADJUDICATION_DIR_NAME / f'{stem}_started.json'
    return None, ('started_no_result' if started.exists() else 'not_started')
  record = json.loads(path.read_text(encoding='utf-8'))
  if record['status'] != 'completed' or not record.get('core_action_result'):
    return None, record['status']
  return record['core_action_result'], 'completed'


def _run_final_group(
    batch_dir: pathlib.Path, *, real_call_cap: int | None = None
) -> None:
  """Runs the pending stage-FINAL attempts over archived results."""
  cap = _CAP_STAGE_FINAL if real_call_cap is None else real_call_cap
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  names, api_key, base_url, secrets = _load_settings()
  out_dir = batch_dir / _FINAL_DIR_NAME
  live: dict[ModelTag, tuple[language_model.LanguageModel, list]] = {}
  for tag in _MODEL_TAGS:
    live[tag] = _create_live_model(
        names[tag], api_key=api_key, api_base=base_url
    )
  gm = fp.gm_params()
  stage_counter = report['counts']['real_api_calls_stage_final']
  for case_id, model_tag, repetition in planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    if _attempt_files_exist(out_dir, stem):
      continue
    source_key = attempt_id(case_id, model_tag, repetition)
    action_result, upstream = _archived_action_result(batch_dir, stem)
    if upstream != 'completed':
      result = {
          'attempt_id': source_key,
          'stage': 'final',
          'case_id': case_id,
          'model_tag': model_tag,
          'model': names[model_tag],
          'repetition': repetition,
          'source_attempt_id': source_key,
          'status': 'skipped_upstream',
          'upstream_status': upstream,
          'classification': 'skipped_upstream',
          'expected_valid': None,
          'verdict': None,
          'review_executed': False,
          'real_api_calls': 0,
          'ended_at_utc': utc_now(),
      }
      (out_dir / f'{stem}_result.json').write_text(
          json.dumps(result, ensure_ascii=False, indent=2) + '\n',
          encoding='utf-8',
      )
      report['attempts'].append({
          'attempt_id': source_key,
          'stage': 'final',
          'status': 'skipped_upstream',
      })
      _save_report(batch_dir, report)
      print(f'{source_key}: skipped_upstream ({upstream})', flush=True)
      continue
    if (
        stage_counter >= cap
        or report['counts']['real_api_calls'] + 2 > _MAX_REAL_CALLS
    ):
      raise ValueError('real-call cap reached; refusing to send more requests')
    if action_result is None:
      raise ValueError(f'archived result missing for completed {stem}')
    case = fp.adjudication_case_by_id(case_id)
    model, records = live[model_tag]
    first_new_record = len(records)
    enrich = _enrich_with_observations(
        records, first_new_record, names, model_tag
    )
    result = _run_final_stage_attempt(
        stage='final',
        case_id=case_id,
        player=case.player,
        speech=case.speech,
        action_intent=case.action_intent,
        injected_action_result=action_result,
        real_model=model,
        model_tag=model_tag,
        model_name=names[model_tag],
        repetition=repetition,
        gm=gm,
        out_dir=out_dir,
        secrets=secrets,
        expected_valid=None,
        source_attempt_id=source_key,
        enrich_result=enrich,
    )
    stage_counter += result['real_api_calls']
    report['counts']['real_api_calls'] += result['real_api_calls']
    report['counts']['real_api_calls_stage_final'] += result['real_api_calls']
    report['attempts'].append({
        'attempt_id': result['attempt_id'],
        'stage': 'final',
        'status': result['status'],
        'prompt_files': {
            key: value for key, value in result['prompt_sha256'].items()
        },
        'generation_response_file': result['generation_response_file'],
        'review_response_file': result['review_response_file'],
    })
    _save_report(batch_dir, report)
    print(
        f'{result["attempt_id"]}: {result["status"]}'
        f' ({result["stage_outcome"]})',
        flush=True,
    )


def _run_injection_group(
    batch_dir: pathlib.Path, *, real_call_cap: int | None = None
) -> None:
  """Runs the pending stage-E injection attempts."""
  cap = _CAP_STAGE_E if real_call_cap is None else real_call_cap
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  names, api_key, base_url, secrets = _load_settings()
  out_dir = batch_dir / _INJECTION_DIR_NAME
  live: dict[ModelTag, tuple[language_model.LanguageModel, list]] = {}
  for tag in _MODEL_TAGS:
    live[tag] = _create_live_model(
        names[tag], api_key=api_key, api_base=base_url
    )
  gm = fp.gm_params()
  stage_counter = report['counts']['real_api_calls_stage_e']
  for case_id, model_tag, repetition in planned_injection_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    if _attempt_files_exist(out_dir, stem):
      continue
    if (
        stage_counter >= cap
        or report['counts']['real_api_calls'] + 2 > _MAX_REAL_CALLS
    ):
      raise ValueError('real-call cap reached; refusing to send more requests')
    case = injection_case_by_id(case_id)
    base = fp.adjudication_case_by_id(case.base_case_id)
    model, records = live[model_tag]
    first_new_record = len(records)
    enrich = _enrich_with_observations(
        records, first_new_record, names, model_tag
    )
    result = _run_final_stage_attempt(
        stage='injection',
        case_id=case_id,
        player=base.player,
        speech=base.speech,
        action_intent=base.action_intent,
        injected_action_result=case.injected_action_result,
        real_model=model,
        model_tag=model_tag,
        model_name=names[model_tag],
        repetition=repetition,
        gm=gm,
        out_dir=out_dir,
        secrets=secrets,
        expected_valid=case.expected_valid,
        nonstory_fragments=case.nonstory_fragments,
        enrich_result=enrich,
    )
    stage_counter += result['real_api_calls']
    report['counts']['real_api_calls'] += result['real_api_calls']
    report['counts']['real_api_calls_stage_e'] += result['real_api_calls']
    report['attempts'].append({
        'attempt_id': result['attempt_id'],
        'stage': 'injection',
        'status': result['status'],
        'classification': result.get('classification'),
        'generation_response_file': result['generation_response_file'],
        'review_response_file': result['review_response_file'],
    })
    _save_report(batch_dir, report)
    print(
        f'{result["attempt_id"]}: {result["status"]}'
        f' ({result.get("classification")})',
        flush=True,
    )


# ---------------------------------------------------------------------------
# Dry run.
# ---------------------------------------------------------------------------

_DRY_RUN_PROMPT_DIR = 'prompts'


def _capture_final_prompts(
    *,
    case_id: str,
    player: str,
    speech: str,
    action_intent: str,
    injected_action_result: str,
    prompt_dir: pathlib.Path,
) -> tuple[str, str]:
  """Captures the production generation and review prompts offline.

  Drives the production final-output step with the offline two-slot
  stub, injecting the given raw adjudicated outcome as-is, then
  archives both captured prompts.

  Args:
    case_id: the fixture identifier for file naming.
    player: the active player character.
    speech: the turn's original speech.
    action_intent: the turn's own attempted action.
    injected_action_result: the raw adjudicated outcome to inject.
    prompt_dir: where the prompt files are written.

  Returns:
    A (generation prompt, review prompt) tuple.

  Raises:
    _HarnessError: if the production step routed calls unexpectedly.
  """
  gm = fp.gm_params()
  director, entity, _ = fp._make_director(player=player, gm=gm)  # pylint: disable=protected-access
  fp._open_turn(entity, player, speech, action_intent)  # pylint: disable=protected-access
  director.record_action_result(injected_action_result)
  wrapper = _DualForwardModel(_OfflineFinalStub())
  chain = interactive_document.InteractiveDocument(wrapper)
  step = story_resolution.make_final_output_step(
      director,
      immutable_facts=gm['immutable_facts'],
      character_constraints=gm['character_constraints'],
      player_names=gm['player_names'],
      world_constraints=gm['world_constraints'],
      allowed_environment_events=gm['allowed_environment_events'],
      public_facts=gm['public_facts'],
      public_history=lambda: public_history_text(),
  )
  step(chain, injected_action_result, player)
  if wrapper.generation_calls != 1 or wrapper.review_calls != 1:
    raise _HarnessError(
        f'{case_id}: expected 1+1 real slots, got '
        f'{wrapper.generation_calls}+{wrapper.review_calls}'
    )
  generation_prompt = wrapper.captured[0]['prompt']
  review_prompt = wrapper.captured[1]['prompt']
  for name, text in (
      (f'{case_id}_generation_prompt.txt', generation_prompt),
      (f'{case_id}_review_prompt.txt', review_prompt),
  ):
    (prompt_dir / name).write_text(text, encoding='utf-8')
  return generation_prompt, review_prompt


def _dry_run(dry_run_dir: pathlib.Path) -> None:
  """Runs the no-network rehearsal of the chain batch.

  Builds every planned slot offline: the 24 adjudication prompts, the
  24 final-stage prompt pairs (one per planned A slot, placeholder
  injection) and the 24 injection prompt pairs (frozen E evidence),
  driving the real production factories. Verifies routing, counts,
  prompt binding, label hygiene, evidence injection and canonical
  assembly. No real client is ever constructed.
  """
  if dry_run_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {dry_run_dir}')
  prompt_dir = dry_run_dir / _DRY_RUN_PROMPT_DIR
  prompt_dir.mkdir(parents=True)
  findings: list[str] = []
  history = public_history_text()
  history_json = json.dumps(history, ensure_ascii=False)
  # Stage A prompts: every planned slot is built (24 builds) so the
  # byte-identity across models and repetitions is verified
  # empirically rather than assumed.
  adjudication_prompts: dict[str, set[str]] = {}
  for case_id, _, _ in planned_adjudication_attempts():
    case = fp.adjudication_case_by_id(case_id)
    prompt = fp._capture_adjudication_prompt(  # pylint: disable=protected-access
        case, prompt_dir, history_text=history
    )
    adjudication_prompts.setdefault(case_id, set()).add(prompt)
  # Stage FINAL prompts (placeholder injection; the real batch injects
  # the archived results of THIS batch, which do not exist yet).
  final_prompts: dict[str, set[tuple[str, str]]] = {}
  placeholder_result = (
      '干跑占位意图。 Because of that, 干跑占位结果，无其他变化。'
  )
  for case_id, _, _ in planned_adjudication_attempts():
    case = fp.adjudication_case_by_id(case_id)
    pair = _capture_final_prompts(
        case_id=case_id,
        player=case.player,
        speech=case.speech,
        action_intent=case.action_intent,
        injected_action_result=placeholder_result,
        prompt_dir=prompt_dir,
    )
    final_prompts.setdefault(case_id, set()).add(pair)
  # Stage E prompts (frozen injections).
  injection_prompts: dict[str, set[tuple[str, str]]] = {}
  for case_id, _, _ in planned_injection_attempts():
    case = injection_case_by_id(case_id)
    base = fp.adjudication_case_by_id(case.base_case_id)
    pair = _capture_final_prompts(
        case_id=case_id,
        player=base.player,
        speech=base.speech,
        action_intent=base.action_intent,
        injected_action_result=case.injected_action_result,
        prompt_dir=prompt_dir,
    )
    injection_prompts.setdefault(case_id, set()).add(pair)
  for case_id, variants in adjudication_prompts.items():
    if len(variants) != 1:
      findings.append(f'stage-A prompts differ within case {case_id}')
  for group, variants_by_case in (
      ('final', final_prompts),
      (
          'injection',
          injection_prompts,
      ),
  ):
    for case_id, variants in variants_by_case.items():
      if len(variants) != 1:
        findings.append(f'stage-{group} prompts differ within case {case_id}')
  adjudication_prompts_text = {
      case_id: next(iter(variants))
      for case_id, variants in adjudication_prompts.items()
  }
  final_prompts_pair = {
      case_id: next(iter(variants))
      for case_id, variants in final_prompts.items()
  }
  injection_prompts_pair = {
      case_id: next(iter(variants))
      for case_id, variants in injection_prompts.items()
  }
  # Plan uniqueness and caps.
  a_plan = planned_adjudication_attempts()
  e_plan = planned_injection_attempts()
  if len(set(a_plan)) != 24 or len(a_plan) != 24:
    findings.append('stage-A plan is not 24 unique keys')
  if len(set(e_plan)) != 24 or len(e_plan) != 24:
    findings.append('stage-E plan is not 24 unique keys')
  if _CAP_STAGE_A + _CAP_STAGE_FINAL + _CAP_STAGE_E != _MAX_REAL_CALLS:
    findings.append('stage caps do not sum to the global cap')
  # Model names, labels and metadata never leak into any prompt.
  for group, prompts in (
      (
          'adjudication',
          {k: (v, None) for k, v in adjudication_prompts_text.items()},
      ),
      ('final', final_prompts_pair),
      ('injection', injection_prompts_pair),
  ):
    for case_id, value in prompts.items():
      texts = (value[0],) if value[1] is None else value
      for text in texts:
        for tag in _MODEL_TAGS:
          if _EXPECTED_MODEL_NAMES[tag] in text:
            findings.append(f'{group} {case_id}: model name leaked')
  for case in fp._ADJUDICATION_CASES:  # pylint: disable=protected-access
    if case.judgment_focus in adjudication_prompts_text[case.case_id]:
      findings.append(f'{case.case_id}: judgment focus leaked into prompt')
  for case in _INJECTION_CASES:
    for text in injection_prompts_pair[case.case_id]:
      if case.human_rationale in text or case.fact_sources in text:
        findings.append(f'{case.case_id}: label metadata leaked into prompt')
      if str(case.expected_valid).lower() + '\n' in text:
        findings.append(f'{case.case_id}: expected label leaked into prompt')
  # History, evidence injection and structural sections present.
  for case in fp._ADJUDICATION_CASES:  # pylint: disable=protected-access
    prompt = adjudication_prompts_text[case.case_id]
    if fp.story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK not in prompt:
      findings.append(f'{case.case_id} adjudication: history section missing')
    if history_json not in prompt:
      findings.append(f'{case.case_id} adjudication: history text missing')
  for group, prompts, injections in (
      (
          'final',
          final_prompts_pair,
          {c.case_id: placeholder_result for c in fp._ADJUDICATION_CASES},
      ),  # pylint: disable=protected-access
      (
          'injection',
          injection_prompts_pair,
          {c.case_id: c.injected_action_result for c in _INJECTION_CASES},
      ),
  ):
    for case_id, (generation, review) in prompts.items():
      injected_json = json.dumps(injections[case_id], ensure_ascii=False)
      for label, text in (('generation', generation), ('review', review)):
        if (
            fp.story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK
            not in text  # pylint: disable=protected-access
        ):
          findings.append(f'{group} {case_id} {label}: history section missing')
        if history_json not in text:
          findings.append(f'{group} {case_id} {label}: history text missing')
        if injected_json not in text:
          findings.append(
              f'{group} {case_id} {label}: injected action_result missing'
          )
      if 'If rejecting, name the specific offending text' not in review:
        findings.append(f'{group} {case_id} review: question suffix missing')
  summary = {
      'stage': 'dry_run',
      'generated_at_utc': utc_now(),
      'planned_adjudication_attempts': len(a_plan),
      'planned_final_attempts': len(a_plan),
      'planned_injection_attempts': len(e_plan),
      'real_client_constructions': 0,
      'real_api_calls': 0,
      'findings': findings,
      'prompt_hashes': {
          'adjudication': {
              case_id: sha256_text(text)
              for case_id, text in adjudication_prompts_text.items()
          },
          'final_generation': {
              case_id: sha256_text(pair[0])
              for case_id, pair in final_prompts_pair.items()
          },
          'injection_generation': {
              case_id: sha256_text(pair[0])
              for case_id, pair in injection_prompts_pair.items()
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


# ---------------------------------------------------------------------------
# Worksheets and summary.
# ---------------------------------------------------------------------------


def _load_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
  """Loads a JSONL file, skipping blank lines."""
  if not path.exists():
    return []
  return [
      json.loads(line)
      for line in path.read_text(encoding='utf-8').splitlines()
      if line.strip()
  ]


def _worksheets(batch_dir: pathlib.Path) -> None:
  """Emits anonymized human-label worksheets from archived results."""
  _verify_frozen(batch_dir)

  def row(alias: str, payload: Mapping[str, Any]) -> str:
    return json.dumps({'alias': alias, **payload}, ensure_ascii=False) + '\n'

  a_rows = []
  for case_id, model_tag, repetition in planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    result_path = batch_dir / _ADJUDICATION_DIR_NAME / f'{stem}_result.json'
    if not result_path.exists():
      continue
    record = json.loads(result_path.read_text(encoding='utf-8'))
    a_rows.append(
        row(
            _alias(batch_dir, record['attempt_id']),
            {
                'group': 'A',
                'case_id': case_id,
                'repetition': repetition,
                'raw_provider_text': record.get('raw_provider_text'),
                'core_action_result': record.get('core_action_result'),
                'fields_to_label': [
                    'outcome',
                    'unauthorized_player_action',
                    'fabricated_evidence',
                    'illegal_disclosure',
                    'ungrounded_plot_event',
                    'definite_grounded_result',
                    'reasoning_leak',
                    'self_talk_leak',
                    'instruction_leak',
                    'reason',
                ],
            },
        )
    )
  (batch_dir / 'worksheet_A.jsonl').write_text(
      ''.join(a_rows), encoding='utf-8'
  )
  final_rows = []
  for case_id, model_tag, repetition in planned_adjudication_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    result_path = batch_dir / _FINAL_DIR_NAME / f'{stem}_result.json'
    if not result_path.exists():
      continue
    record = json.loads(result_path.read_text(encoding='utf-8'))
    final_rows.append(
        row(
            _alias(batch_dir, record['attempt_id']),
            {
                'group': 'FINAL',
                'case_id': case_id,
                'repetition': repetition,
                'injected_action_result': record.get('injected_action_result'),
                'stage_directions': record.get('stage_directions'),
                'resolved_event': record.get('resolved_event'),
                'verdict': record.get('verdict'),
                'review_reason': record.get('review_reason'),
                'fields_to_label': [
                    'legal_result_retained',
                    'other_player_new_choice',
                    'canonical_nonstory_text',
                    'review_reason_matches_verdict',
                    'notes',
                ],
            },
        )
    )
  (batch_dir / 'worksheet_FINAL.jsonl').write_text(
      ''.join(final_rows), encoding='utf-8'
  )
  e_rows = []
  for case_id, model_tag, repetition in planned_injection_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    result_path = batch_dir / _INJECTION_DIR_NAME / f'{stem}_result.json'
    if not result_path.exists():
      continue
    record = json.loads(result_path.read_text(encoding='utf-8'))
    e_rows.append(
        row(
            _alias(batch_dir, record['attempt_id']),
            {
                'group': 'E',
                'case_id': case_id,
                'repetition': repetition,
                'injected_action_result': record.get('injected_action_result'),
                'stage_directions': record.get('stage_directions'),
                'resolved_event': record.get('resolved_event'),
                'verdict': record.get('verdict'),
                'review_reason': record.get('review_reason'),
                'fields_to_label': [
                    'legal_substance_retained',
                    'canonical_nonstory_text',
                    'english_only_rejection',
                    'violation_named_in_reason',
                    'notes',
                ],
            },
        )
    )
  (batch_dir / 'worksheet_E.jsonl').write_text(
      ''.join(e_rows), encoding='utf-8'
  )
  print(
      'worksheets written: worksheet_A.jsonl '
      f'({len(a_rows)}), worksheet_FINAL.jsonl ({len(final_rows)}), '
      f'worksheet_E.jsonl ({len(e_rows)})',
      flush=True,
  )


def _collect_stage(
    stage_dir: pathlib.Path,
    plan: list[tuple[str, ModelTag, int]],
) -> dict[ModelTag, dict[str, Any]]:
  """Collects per-model result records with completeness accounting."""
  per_model: dict[ModelTag, dict[str, Any]] = {
      tag: {
          'planned': 0,
          'started': 0,
          'completed': 0,
          'records': [],
      }
      for tag in _MODEL_TAGS
  }
  for case_id, model_tag, repetition in plan:
    stem = f'{case_id}__{model_tag}__r{repetition}'
    entry = per_model[model_tag]
    entry['planned'] += 1
    result_path = stage_dir / f'{stem}_result.json'
    started_markers = [
        stage_dir / f'{stem}_started.json',
        stage_dir / f'{stem}_generation_started.json',
    ]
    if any(marker.exists() for marker in started_markers):
      entry['started'] += 1
    if result_path.exists():
      record = json.loads(result_path.read_text(encoding='utf-8'))
      entry['records'].append(record)
      if record['status'] == 'completed':
        entry['completed'] += 1
  return per_model


def _compute_summary(
    batch_dir: pathlib.Path,
    report: dict[str, Any],
) -> dict[str, Any]:
  """Computes the label-derived summary without touching the report."""
  # Human labels of stage A drive the final-stage expectations.
  a_labels = {
      label['attempt_id']: label
      for label in _load_jsonl(batch_dir / _HUMAN_LABELS_NAME)
      if label.get('status') == 'labeled'
  }
  final_labels = {
      label['attempt_id']: label
      for label in _load_jsonl(batch_dir / _HUMAN_FINAL_LABELS_NAME)
      if label.get('status') == 'labeled'
  }
  e_labels = {
      label['attempt_id']: label
      for label in _load_jsonl(batch_dir / _HUMAN_E_LABELS_NAME)
      if label.get('status') == 'labeled'
  }

  adjudication = _collect_stage(
      batch_dir / _ADJUDICATION_DIR_NAME, planned_adjudication_attempts()
  )
  final = _collect_stage(
      batch_dir / _FINAL_DIR_NAME, planned_adjudication_attempts()
  )
  injection = _collect_stage(
      batch_dir / _INJECTION_DIR_NAME, planned_injection_attempts()
  )

  def a_metrics(tag: ModelTag) -> dict[str, Any]:
    """Aggregates the human-labeled stage-A outcome of one model."""
    entry = adjudication[tag]
    metrics = {
        'planned': entry['planned'],
        'started': entry['started'],
        'completed': entry['completed'],
        'labeled': 0,
        'api_error': 0,
        'harness_error': 0,
        'skipped_or_indeterminate': 0,
        'semantic_violations': {
            'unauthorized_player_action': 0,
            'fabricated_evidence': 0,
            'illegal_disclosure': 0,
            'ungrounded_plot_event': 0,
            'not_definite_grounded_result': 0,
        },
        'nonstory_leaks': {
            'reasoning_leak': 0,
            'self_talk_leak': 0,
            'instruction_leak': 0,
        },
    }
    for record in entry['records']:
      if record['status'] != 'completed':
        if record['status'] in ('api_error', 'harness_error'):
          metrics[record['status']] += 1
        else:
          metrics['skipped_or_indeterminate'] += 1
    for record in entry['records']:
      label = a_labels.get(record['attempt_id'])
      if label is None:
        continue
      metrics['labeled'] += 1
      for key in metrics['semantic_violations']:
        if key == 'not_definite_grounded_result':
          if label.get('definite_grounded_result') is False:
            metrics['semantic_violations'][key] += 1
        elif label.get(key):
          metrics['semantic_violations'][key] += 1
      for key in metrics['nonstory_leaks']:
        if label.get(key):
          metrics['nonstory_leaks'][key] += 1
    metrics['pass'] = (
        metrics['planned'] == 12
        and metrics['started'] == 12
        and metrics['completed'] == 12
        and metrics['labeled'] == 12
        and metrics['api_error'] == 0
        and metrics['harness_error'] == 0
        and metrics['skipped_or_indeterminate'] == 0
        and all(value == 0 for value in metrics['semantic_violations'].values())
        and all(value == 0 for value in metrics['nonstory_leaks'].values())
    )
    return metrics

  def final_metrics(tag: ModelTag) -> dict[str, Any]:
    """Aggregates the label-expectation classification of stage FINAL."""
    entry = final[tag]
    metrics = {
        'planned': entry['planned'],
        'started': entry['started'],
        'completed': entry['completed'],
        'skipped_upstream': 0,
        'classified': 0,
        'correct': 0,
        'false_rejection': 0,
        'missed_detection': 0,
        'malformed': 0,
        'generation_malformed': 0,
        'api_error': 0,
        'provider_error': 0,
        'harness_error': 0,
        'cross_check_failures': 0,
        'reviews_executed': 0,
        'human_labeled': 0,
        'human_substance_loss': 0,
        'human_other_player_choice': 0,
        'human_canonical_nonstory': 0,
        'human_reason_verdict_mismatch': 0,
    }
    for record in entry['records']:
      if record['status'] == 'skipped_upstream':
        metrics['skipped_upstream'] += 1
        continue
      if record['status'] != 'completed':
        if record['status'] in metrics and isinstance(record['status'], str):
          metrics[record['status']] += 1
        continue
      if record.get('review_executed'):
        metrics['reviews_executed'] += 1
      if record.get('cross_check_ok') is False:
        metrics['cross_check_failures'] += 1
      label = a_labels.get(record['source_attempt_id'])
      expected = label['pass'] if label else None
      if expected is None:
        continue
      verdict = record.get('verdict')
      metrics['classified'] += 1
      if verdict == expected:
        metrics['correct'] += 1
      elif expected:
        metrics['false_rejection'] += 1
      else:
        metrics['missed_detection'] += 1
      human = final_labels.get(record['attempt_id'])
      if human is None:
        continue
      metrics['human_labeled'] += 1
      if human.get('legal_result_retained') is False:
        metrics['human_substance_loss'] += 1
      if human.get('other_player_new_choice'):
        metrics['human_other_player_choice'] += 1
      if human.get('canonical_nonstory_text'):
        metrics['human_canonical_nonstory'] += 1
      if human.get('review_reason_matches_verdict') is False:
        metrics['human_reason_verdict_mismatch'] += 1
    records = [r for r in entry['records'] if r['status'] == 'completed']
    metrics['malformed'] = sum(
        1 for r in records if r.get('classification') == 'malformed'
    )
    metrics['generation_malformed'] = sum(
        1 for r in records if r.get('classification') == 'generation_malformed'
    )
    metrics['provider_error'] = sum(
        1 for r in records if r.get('classification') == 'provider_error'
    )
    metrics['pass'] = (
        metrics['planned'] == 12
        and metrics['completed'] == 12
        and metrics['skipped_upstream'] == 0
        and metrics['classified'] == 12
        and metrics['correct'] == 12
        and metrics['false_rejection'] == 0
        and metrics['missed_detection'] == 0
        and metrics['malformed'] == 0
        and metrics['generation_malformed'] == 0
        and metrics['api_error'] == 0
        and metrics['provider_error'] == 0
        and metrics['harness_error'] == 0
        and metrics['cross_check_failures'] == 0
        and metrics['reviews_executed'] == 12
        and metrics['human_labeled'] == 12
        and metrics['human_substance_loss'] == 0
        and metrics['human_other_player_choice'] == 0
        and metrics['human_canonical_nonstory'] == 0
        and metrics['human_reason_verdict_mismatch'] == 0
    )
    return metrics

  def e_metrics(tag: ModelTag) -> dict[str, Any]:
    """Aggregates the frozen-label classification of stage E."""
    entry = injection[tag]
    metrics = {
        'planned': entry['planned'],
        'started': entry['started'],
        'completed': entry['completed'],
        'correct': 0,
        'false_rejection': 0,
        'missed_detection': 0,
        'malformed': 0,
        'generation_malformed': 0,
        'api_error': 0,
        'provider_error': 0,
        'harness_error': 0,
        'canonical_nonstory_leaks': 0,
        'human_canonical_nonstory': 0,
        'human_label_incomplete': 0,
        'human_labeled': 0,
        'human_substance_loss': 0,
        'human_english_only_rejection': 0,
        'human_violation_unnamed': 0,
    }
    for record in entry['records']:
      classification = record.get('classification')
      if classification in metrics and isinstance(classification, str):
        metrics[classification] += 1
      if (
          record.get('canonical_known_nonstory')
          and record.get('case_id') == 'E03'
      ):
        metrics['canonical_nonstory_leaks'] += 1
      human = e_labels.get(record['attempt_id'])
      if human is None:
        continue
      metrics['human_labeled'] += 1
      nonstory = human.get('canonical_nonstory_text')
      if nonstory is True:
        metrics['human_canonical_nonstory'] += 1
      elif nonstory is not False:
        metrics['human_label_incomplete'] += 1
      if human.get('legal_substance_retained') is False and record.get(
          'case_id'
      ) in ('E01', 'E03'):
        metrics['human_substance_loss'] += 1
      if human.get('english_only_rejection') and record.get('case_id') == 'E03':
        metrics['human_english_only_rejection'] += 1
      if human.get('violation_named_in_reason') is False and record.get(
          'case_id'
      ) in ('E02', 'E04'):
        metrics['human_violation_unnamed'] += 1
    metrics['pass'] = (
        metrics['planned'] == 12
        and metrics['started'] == 12
        and metrics['completed'] == 12
        and metrics['correct'] == 12
        and metrics['malformed'] == 0
        and metrics['generation_malformed'] == 0
        and metrics['api_error'] == 0
        and metrics['provider_error'] == 0
        and metrics['harness_error'] == 0
        and metrics['canonical_nonstory_leaks'] == 0
        and metrics['human_labeled'] == 12
        and metrics['human_substance_loss'] == 0
        and metrics['human_english_only_rejection'] == 0
        and metrics['human_violation_unnamed'] == 0
        and metrics['human_canonical_nonstory'] == 0
        and metrics['human_label_incomplete'] == 0
    )
    return metrics

  summary = {
      'generated_at_utc': utc_now(),
      'stage_a': {tag: a_metrics(tag) for tag in _MODEL_TAGS},
      'stage_final': {tag: final_metrics(tag) for tag in _MODEL_TAGS},
      'stage_e': {tag: e_metrics(tag) for tag in _MODEL_TAGS},
  }
  summary['stage_a_pass'] = all(
      summary['stage_a'][tag]['pass'] for tag in _MODEL_TAGS
  )
  summary['stage_final_pass'] = all(
      summary['stage_final'][tag]['pass'] for tag in _MODEL_TAGS
  )
  summary['stage_e_pass'] = all(
      summary['stage_e'][tag]['pass'] for tag in _MODEL_TAGS
  )
  summary['overall_pass'] = bool(
      summary['stage_a_pass']
      and summary['stage_final_pass']
      and summary['stage_e_pass']
      and report['counts']['real_api_calls'] <= _MAX_REAL_CALLS
  )
  summary['note'] = (
      'stage_final classifications apply the label-derived expectation'
      ' (legal adjudication -> approved; illegal -> rejected'
      ' whole-turn). A raw adjudication defect stays a recorded'
      ' stage-A failure even when the final narration omits it.'
      ' started_no_result entries are indeterminate: no claim is made'
      ' about delivery or billing.'
  )
  return summary


def _summarize(batch_dir: pathlib.Path) -> None:
  """Summarizes the batch without any model call."""
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  summary = _compute_summary(batch_dir, report)
  report['summary'] = summary
  report['overall_pass'] = summary['overall_pass']
  _save_report(batch_dir, report)
  print(
      f'summary written: stage_a_pass={summary["stage_a_pass"]} '
      f'stage_final_pass={summary["stage_final_pass"]} '
      f'stage_e_pass={summary["stage_e_pass"]} '
      f'overall_pass={summary["overall_pass"]}',
      flush=True,
  )


# ---------------------------------------------------------------------------
# Offline re-summary.
# ---------------------------------------------------------------------------


def _input_manifest_payload(batch_dir: pathlib.Path) -> dict[str, Any]:
  """Builds the read-only input manifest of a batch (README excluded)."""
  files = sorted(
      (str(path.relative_to(batch_dir)), sha256_file(path))
      for path in batch_dir.rglob('*')
      if path.is_file() and path.name != _README_NAME
  )
  return {'files': [{'path': p, 'sha256': d} for p, d in files]}


def _verify_input_manifest(
    batch_dir: pathlib.Path,
    manifest_path: pathlib.Path,
) -> str:
  """Re-verifies the batch inputs against the pre-work manifest."""
  manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
  recorded = manifest['files']
  current = _input_manifest_payload(batch_dir)['files']
  if recorded != current:
    recorded_by_path = {e['path']: e['sha256'] for e in recorded}
    current_by_path = {e['path']: e['sha256'] for e in current}
    missing = sorted(set(recorded_by_path) - set(current_by_path))
    extra = sorted(set(current_by_path) - set(recorded_by_path))
    changed = sorted(
        p
        for p in set(recorded_by_path) & set(current_by_path)
        if recorded_by_path[p] != current_by_path[p]
    )
    raise ValueError(
        'batch inputs differ from the pre-work manifest:'
        f' missing={missing} extra={extra} changed={changed}'
    )
  return sha256_file(manifest_path)


def _source_diff_vs_experiment(
    experiment_snapshot: Mapping[str, Any],
    current_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
  """Diffs snapshots, allowing only the approved summary-tool changes."""
  old = {e['path']: e['sha256'] for e in experiment_snapshot['files']}
  new = {e['path']: e['sha256'] for e in current_snapshot['files']}
  if set(old) != set(new):
    raise ValueError(
        'snapshot file list changed vs experiment:'
        f' {sorted(set(old) ^ set(new))}'
    )
  diff = {}
  for path in sorted(old):
    if old[path] == new[path]:
      continue
    if path not in _RESUMMARIZE_ALLOWED_SOURCE_CHANGES:
      raise ValueError(f'non-tool source changed vs experiment: {path}')
    diff[path] = {'experiment_sha256': old[path], 'current_sha256': new[path]}
  return diff


def _resummarize_offline(
    batch_dir: pathlib.Path,
    output_dir: pathlib.Path,
    manifest_path: pathlib.Path,
) -> None:
  """Recomputes the summary from the frozen batch without any model call.

  This is the explicit offline re-summary entry for batches summarized
  by an older tool version: it reuses the same fixed summary logic, never
  reads credentials or constructs clients, and writes a provenance-rich
  corrected report into a new output directory, leaving the original
  report.json untouched.
  """
  batch_path = batch_dir.resolve()
  output_path = output_dir.resolve()
  if output_path.is_relative_to(batch_path):
    raise ValueError(
        f'output directory must be outside the source batch: {output_dir}'
    )
  if output_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {output_dir}')
  manifest_sha256 = _verify_input_manifest(batch_dir, manifest_path)
  report = _load_report(batch_dir)
  original_report_sha256 = sha256_file(batch_dir / _REPORT_NAME)
  experiment_snapshot = json.loads(
      (batch_dir / _SNAPSHOT_NAME).read_text(encoding='utf-8')
  )
  current_snapshot = source_snapshot_payload()
  source_diff = _source_diff_vs_experiment(
      experiment_snapshot, current_snapshot
  )
  summary = _compute_summary(batch_dir, report)
  new_report = dict(report)
  new_report['summary'] = summary
  new_report['overall_pass'] = summary['overall_pass']
  new_report['offline_resummarize'] = {
      'source_batch_dir': str(batch_dir),
      'original_report_sha256': original_report_sha256,
      'input_manifest_path': str(manifest_path),
      'input_manifest_sha256': manifest_sha256,
      'summarizer_source_snapshot': current_snapshot,
      'source_diff_vs_experiment': source_diff,
      'resummarized_at_utc': utc_now(),
      'new_real_api_calls': 0,
      'note': (
          'Offline re-summary by the fixed summary tool. The original'
          ' report.json in the source batch keeps the experiment-time'
          ' summary; no model call, credential lookup, or stage'
          ' execution happened here.'
      ),
  }
  output_dir.mkdir(parents=True)
  _write_json(output_dir / _REPORT_NAME, new_report)
  print(
      f'offline resummary written: {output_dir / _REPORT_NAME}'
      f' overall_pass={summary["overall_pass"]} new_real_api_calls=0',
      flush=True,
  )


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

FLAGS = flags.FlagValues()
flags.DEFINE_string(
    'stage',
    None,
    f'Probe stage; one of {", ".join(_STAGES)}.',
    required=True,
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'batch_dir',
    None,
    'Batch output directory (required by every stage except dry_run); '
    'for example a fresh directory under '
    'run_results/story_enactment/action_authorization_chain_probe/.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'dry_run_dir',
    None,
    'Output directory for the dry run; it must not exist. Defaults to '
    'a new directory under /tmp/opencode.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'output_dir',
    None,
    'Output directory for --stage=resummarize_offline; it must not exist.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'input_manifest',
    None,
    'Pre-work input manifest for --stage=resummarize_offline (JSON file'
    ' covering the read-only batch inputs).',
    flag_values=FLAGS,
)


def _parse_flags(argv: list[str]) -> list[str]:
  """Parses this probe's private flags (absl app flags_parser hook)."""
  args = FLAGS(argv)
  flags.FLAGS.mark_as_parsed()
  return args


def main(argv: list[str]) -> None:
  """Dispatches the probe stage given on the command line."""
  del argv
  stage = FLAGS.stage
  if stage not in _STAGES:
    raise app.UsageError(f'--stage must be one of {", ".join(_STAGES)}')
  if stage == _STAGE_DRY_RUN:
    dry_run_dir = (
        pathlib.Path(FLAGS.dry_run_dir)
        if FLAGS.dry_run_dir
        else pathlib.Path(
            '/tmp/opencode/chain_probe_dry_run_'
            + datetime_lib.datetime.now(datetime_lib.timezone.utc).strftime(
                '%Y%m%dT%H%M%SZ'
            )
        )
    )
    _dry_run(dry_run_dir)
    return
  if not FLAGS.batch_dir:
    raise app.UsageError(f'--stage={stage} requires --batch_dir')
  batch_dir = pathlib.Path(FLAGS.batch_dir)
  if stage == _STAGE_FREEZE:
    _freeze(batch_dir)
  elif stage == _STAGE_RUN_A:
    _run_adjudication_group(batch_dir)
  elif stage == _STAGE_RUN_FINAL:
    _run_final_group(batch_dir)
  elif stage == _STAGE_RUN_E:
    _run_injection_group(batch_dir)
  elif stage == _STAGE_WORKSHEETS:
    _worksheets(batch_dir)
  elif stage == _STAGE_SUMMARIZE:
    _summarize(batch_dir)
  elif stage == _STAGE_RESUMMARIZE_OFFLINE:
    if not FLAGS.output_dir or not FLAGS.input_manifest:
      raise app.UsageError(
          f'--stage={stage} requires --output_dir and --input_manifest'
      )
    _resummarize_offline(
        batch_dir,
        pathlib.Path(FLAGS.output_dir),
        pathlib.Path(FLAGS.input_manifest),
    )


if __name__ == '__main__':
  app.run(main, flags_parser=_parse_flags)
