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

"""Three-model, group-R-only fixed-sample review reliability probe.

This runner implements the 2026-10-17 three-model semantic-review
comparison protocol. It is an INDEPENDENT run configuration derived
from `fixed_model_probe` (which keeps its original two-model R/A
configuration and 120-call cap untouched): this probe fixes

- three reviewer models, ``m1``/``m2``/``m4`` mapped to
  ``AI_MODEL_1``/``AI_MODEL_2``/``AI_MODEL_4`` (``AI_MODEL_3`` is never
  read, and MODEL_4 is never temporarily overlaid onto MODEL_2),
- group R only: 16 review cases x 3 models x 3 repetitions = 144 real
  review forwards (the 144 synthetic generation slots are not paid
  calls and are counted separately); no group A, no full story, no
  director interventions, no scoring,
- a frozen execution order of repetition -> case -> m1/m2/m4,
- a calibrated F10: shared-history event 1 is now an explicit
  committed canonical event (the lawyer's speech plus the completed
  legal own-action record of displaying the unsealed will envelope
  with its residual wax and taking the envelope and the will text back
  into his own custody), and the F10 narration under review is fixed
  to a pure restatement of that committed, unchanged state. Every
  other history event and every other fixture stays byte-identical to
  batch 20261017T112000Z.

The probe never reimplements production logic: each attempt drives the
real `story_resolution.make_final_output_step` through the shared
wrapper of `fixed_model_probe` (first slot synthetic with the frozen
narration, second slot forwarded verbatim to the live reviewer; any
third call, choice call or unexpected routing is a harness error and
never reaches the API). A client-level observation proxy archives, per
response, the requested model name, the provider-returned model
identifier (when the provider exposes one) and the token usage, without
altering any request or response.

Summary accounting fixes the batch-2 limitations: planned, started,
completed, not_started, started_no_result and api/provider errors are
counted separately (``planned - started`` is never used to stand for
indeterminate, and a started-but-no-result entry is never claimed to
have been unsent or uncharged); the number of archived responses is
reported separately from the number of started requests; three-model
counts and the manual review of all 144 reasons are part of this
round's pass criteria; and this R-only round never marks the original
two-model R/A acceptance gate as passed.
"""

import dataclasses
import datetime as datetime_lib
import json
import pathlib
from typing import Any, Mapping

from absl import app
from absl import flags
from concordia.contrib.components.game_master import story_action_adjudication
from concordia.language_model import language_model
from dotenv import dotenv_values

from examples.story_enactment import fixed_model_probe as fp

ModelTag = str

_PROBE_NAME = 'action_authorization_three_model_review'
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_BATCH_PARENT = _REPO_ROOT / 'run_results' / 'story_enactment' / _PROBE_NAME

_PROTOCOL_NAME = 'protocol.json'
_FIXTURES_NAME = 'fixtures.json'
_SNAPSHOT_NAME = 'source_snapshot.json'
_REPORT_NAME = 'report.json'
_REVIEW_DIR_NAME = 'review'
_HUMAN_REVIEW_NAME = 'human_reason_review.jsonl'

# The fixed three-model mapping (protocol section 1). AI_MODEL_3 is
# deliberately not read; MODEL_4 is never overlaid onto MODEL_2.
_MODEL_TAGS: tuple[ModelTag, ...] = ('m1', 'm2', 'm4')
_MODEL_ORDER: tuple[ModelTag, ...] = ('m1', 'm2', 'm4')
_EXPECTED_MODEL_NAMES: dict[ModelTag, str] = {
    'm1': 'gemini-3.8-flash-high',
    'm2': 'gpt-6-luna',
    'm4': 'gpt-6.1-sol',
}
_ENV_MODEL_KEYS: dict[ModelTag, str] = {
    'm1': 'AI_MODEL_1',
    'm2': 'AI_MODEL_2',
    'm4': 'AI_MODEL_4',
}

_REPETITIONS = 3
# R-only cap: 16 cases x 3 models x 3 repetitions real review forwards.
# The synthetic generation slot is never a real call and never counts
# toward this cap. Group A is not part of this protocol.
_MAX_REAL_CALLS = len(fp._REVIEW_CASES) * len(_MODEL_TAGS) * _REPETITIONS  # pylint: disable=protected-access

# CLI stage names (no group-A stage exists in this protocol).
_STAGE_DRY_RUN = 'dry_run'
_STAGE_FREEZE = 'freeze'
_STAGE_RUN_REVIEW = 'run_r'
_STAGE_SUMMARIZE = 'summarize'
_STAGES = (
    _STAGE_DRY_RUN,
    _STAGE_FREEZE,
    _STAGE_RUN_REVIEW,
    _STAGE_SUMMARIZE,
)

# ---------------------------------------------------------------------------
# Calibrated fixture data (protocol section 2).
# ---------------------------------------------------------------------------

# Batch-20261017T112000Z F10 carried two calibration defects: (a) the
# historical display act was attested only inside the lawyer's own
# speech, with no committed objective action record; (b) the history
# record said he kept the "envelope and text" while the narration
# asserted custody of the "residual wax". The revision below replaces
# history event 1 with an explicit existing canonical event: the
# lawyer's speech, then a committed narration of his completed legal
# own action — displaying the unsealed will envelope with the residual
# wax on it, then taking the envelope and the will text back into his
# own custody. The record generates no other player's viewing,
# agreement or endorsement choice, certifies nothing about the seal's
# authenticity, and adds no secret.
_REVISED_EVENT_1 = (
    '周律师: 于程序而言，宣读既毕，容我将已启封的遗嘱信封连同其上的'
    '残留火漆，向诸位展示一遍；此系程序事项，仅此而已。\n'
    '周律师当众展示已启封的遗嘱信封及其上的残留火漆，'
    '随后收回信封与遗嘱文本，自行保管。'
)

# The frozen F10 narration under review (protocol section 2.3): a pure
# restatement of the state committed by revised event 1 and unchanged
# since; no action_intent, no action_result, no intervention.
_F10_STAGE_DIRECTIONS = (
    '遗嘱信封及其上的残留火漆仍由周律师保管，遗嘱文本亦由其收执。'
)
_BATCH2_EVENT_1 = fp._shared_history()[1]  # pylint: disable=protected-access


def _three_model_history() -> tuple[str, ...]:
  """Returns the calibrated 24-event history.

  Event 1 is replaced by the explicit canonical event above; the other
  23 events stay byte-identical to batch 20261017T112000Z so every
  other fixture keeps its frozen grounding. With the 20-event director
  window, event 1 still lies only inside the longer public history.
  """
  events = list(fp._shared_history())  # pylint: disable=protected-access
  events[1] = _REVISED_EVENT_1
  return tuple(events)


_HISTORY = _three_model_history()


def public_history_text() -> str:
  """Returns the calibrated frozen public-history text."""
  return fp.public_history_text(_HISTORY)


def director_window() -> tuple[str, ...]:
  """Returns the director's recent-event window over the calibrated history."""
  return fp.director_window(_HISTORY)


_REVISED_F10 = fp.ReviewCase(
    case_id='F10',
    name='legal_restatement_of_committed_custody',
    expected_valid=True,
    player='周律师',
    speech=(
        '于程序而言，遗嘱信封与火漆封印，早前已由我当众展示一圈，'
        '诸位都亲眼看过；这一层程序上没有争议。'
    ),
    action_intent='',
    action_result=None,
    intervention=None,
    stage_directions=_F10_STAGE_DIRECTIONS,
    human_rationale=(
        'Revised for the three-model batch after batch 2 '
        '(20261017T112000Z): history event 1 is now an explicit '
        'committed canonical event — the lawyer displayed the unsealed '
        'will envelope with its residual wax, then took the envelope '
        'and the will text back into his own custody. The narration '
        'under review restates exactly that committed, publicly '
        'visible state (envelope with residual wax in his custody; '
        'will text also held by him), which no later event changed: '
        'custody of the wax rides on the envelope he took back, so the '
        'batch-2 grounding gap (history said "envelope and text", the '
        'narration said "residual wax") is closed on the history side. '
        'The current turn has no action_intent, action_result or '
        'intervention, and the attributed speech is not treated as a '
        'new voluntary act or retroactive authorization.'
    ),
    fact_sources=(
        'Revised history event 1 (speech plus committed own-action '
        'narration: display of the unsealed envelope with residual '
        'wax, then self-custody of envelope and text) lies only in '
        'the longer public history, outside the director window. No '
        'event after it alters the custody; the narration adds no '
        'other player\u2019s choice and certifies nothing about the '
        'seal. All other history events are byte-identical to batch 2.'
    ),
    depends_on_out_of_window_history=True,
)

_REVIEW_CASES: tuple[fp.ReviewCase, ...] = tuple(
    _REVISED_F10 if case.case_id == 'F10' else case
    for case in fp._REVIEW_CASES  # pylint: disable=protected-access
)


def attempt_id(case_id: str, model_tag: ModelTag, repetition: int) -> str:
  """Builds the unique attempt key used in file names."""
  return fp.attempt_id(case_id, model_tag, repetition)


def planned_review_attempts() -> list[tuple[str, ModelTag, int]]:
  """Lists the 144 planned review attempts in frozen execution order.

  The order is repetition -> case -> m1/m2/m4 (protocol section 4.4),
  which deliberately differs from the two-model probe's
  repetition -> model -> case order.
  """
  plan = []
  for repetition in range(1, _REPETITIONS + 1):
    for case in _REVIEW_CASES:
      for model_tag in _MODEL_ORDER:
        plan.append((case.case_id, model_tag, repetition))
  return plan


def review_case_by_id(case_id: str) -> fp.ReviewCase:
  """Returns the calibrated review fixture with the given identifier."""
  for case in _REVIEW_CASES:
    if case.case_id == case_id:
      return case
  raise ValueError(f'unknown review case {case_id!r}')


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
# Provider observation proxy (protocol section 1.2 / 4.5).
# ---------------------------------------------------------------------------


def _usage_payload(usage: Any) -> Any:
  """Extracts a JSON-serializable usage payload, if the provider sent one."""
  if usage is None:
    return None
  dump = getattr(usage, 'model_dump', None)
  if callable(dump):
    try:
      return dump()
    except Exception:  # pylint: disable=broad-except
      return None
  if isinstance(usage, Mapping):
    return dict(usage)
  return None


class _ObservingCompletions:
  """Pass-through `chat.completions` proxy archiving provider metadata.

  The proxy never alters the request or the response: it records the
  requested model name, the provider-returned model identifier (when
  exposed) and the token usage of each raw response, then returns the
  response unchanged. Observation failures never break the call.
  """

  def __init__(self, real: Any, records: list[dict[str, Any]]):
    self._real = real
    self._records = records

  def create(self, *args: Any, **kwargs: Any) -> Any:
    """Forwards one request and archives the raw response metadata."""
    response = self._real.create(*args, **kwargs)
    try:
      self._records.append({
          'requested_model': kwargs.get('model'),
          'provider_model': getattr(response, 'model', None),
          'usage': _usage_payload(getattr(response, 'usage', None)),
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
  """Builds the live model with an observation proxy and retries off.

  Same OpenAI-compatible interface and configuration source as the
  archived runs, with the SDK's internal retries disabled and the
  timeout bounded per the frozen protocol. The observation proxy sits
  below `sample_text` and archives raw provider metadata.

  Args:
    model_name: the live model's actual name.
    api_key: the API credential (never archived).
    api_base: the OpenAI-compatible endpoint.

  Returns:
    A (live model, provider observation records) tuple. The records
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
      timeout=fp._CLIENT_TIMEOUT_SECONDS,  # pylint: disable=protected-access
  )
  return model, records


# ---------------------------------------------------------------------------
# Frozen artefacts (protocol sections 2.4 and 3).
# ---------------------------------------------------------------------------


def fixtures_payload() -> dict[str, Any]:
  """Builds the frozen fixtures document for the three-model batch.

  Evaluation metadata (expected labels, rationales, fact sources) never
  enters any model prompt. The payload also freezes the F10 calibration
  record: the before/after diff of history event 1 and the F10
  narration, with their fact sources.

  Returns:
    The JSON-serializable fixtures payload.
  """
  return {
      'shared_context': {
          'history_note': (
              '24 committed public events, oldest first; the director '
              'window is the last 20, the public-history getter returns '
              'all 24 formatted like the production DisplayEvents. '
              'Event 1 is a two-line canonical event (speech plus '
              'committed own-action narration).'
          ),
          'public_facts': fp.gm_params()['public_facts'],
          'gm_constraints_source': (
              'story_config.make_example_config() via '
              'run_lib.make_gm_params(): GM-only constraints, immutable '
              'facts, player names and the environment whitelist.'
          ),
          'history_events': list(_HISTORY),
          'director_window_size': fp._DIRECTOR_WINDOW_SIZE,  # pylint: disable=protected-access
      },
      'f10_calibration': {
          'reason': (
              'Batch-2 F10 defects: the historical display was attested '
              'only by the lawyer\u2019s own speech (no committed '
              'objective action record), and the history recorded '
              'custody of "envelope and text" while the narration '
              'asserted custody of the "residual wax".'
          ),
          'history_event_1_before': _BATCH2_EVENT_1,
          'history_event_1_after': _REVISED_EVENT_1,
          'f10_stage_directions_before': (
              fp.review_case_by_id('F10').stage_directions
          ),
          'f10_stage_directions_after': _F10_STAGE_DIRECTIONS,
          'constraints': (
              'The committed record states the lawyer displayed the '
              'unsealed will envelope with its residual wax and then '
              'took the envelope and the will text back into his own '
              'custody; it generates no other player\u2019s viewing, '
              'agreement or endorsement choice, certifies nothing '
              'about seal authenticity, and adds no secret. Expected '
              'valid stays true: the narration only restates committed '
              'state that no later event changed.'
          ),
          'other_fixtures': (
              'The other 23 history events and the other 15 review '
              'fixtures are byte-identical to batch 20261017T112000Z; '
              'their labels were re-checked against the revised event '
              '1 and are unchanged.'
          ),
      },
      'review_cases': [dataclasses.asdict(case) for case in _REVIEW_CASES],
  }


def protocol_payload(model_names: Mapping[ModelTag, str]) -> dict[str, Any]:
  """Builds the frozen protocol document for the three-model batch.

  Args:
    model_names: the actual live model names per tag.

  Returns:
    The JSON-serializable protocol payload.
  """
  return {
      'protocol_version': 1,
      'created_utc': utc_now(),
      'probe': _PROBE_NAME,
      'models': {tag: model_names[tag] for tag in _MODEL_TAGS},
      'model_config_source': {
          'api_type': 'openai',
          'env_model_keys': _ENV_MODEL_KEYS,
          'note': (
              'AI_MODEL_3 is not used; MODEL_4 is never overlaid onto '
              'MODEL_2. Requested model names are the .env values; '
              'provider-returned model identifiers are archived per '
              'response by the observation proxy and may differ.'
          ),
          'env_base_url_key': 'AI_BASE_URL',
          'env_api_key_key': 'AI_API_KEY',
          'client': (
              'GptLanguageModel with max_retries=0 and timeout=120s '
              '(SDK auto-retry and probe-outer retry disabled); a '
              'read-only observation proxy archives requested vs '
              'provider-returned model identifiers and usage.'
          ),
      },
      'request_settings': {
          'note': (
              'Values emitted by the production steps and the shared '
              'document defaults; the wrapper archives what each call '
              'actually carried and nothing is overridden to improve '
              'pass rates. The adapter\u2019s uniform defaults '
              '(reasoning_effort=low, verbosity=low) apply identically '
              'to all three models; no per-model quota, reasoning or '
              'parameter change is made. If a provider rejects the '
              'parameters, the actual error is recorded and the '
              'request is never silently re-sent with new parameters.'
          ),
          'review_forward': {
              'max_tokens': fp._REVIEW_MAX_TOKENS,  # pylint: disable=protected-access
              'terminators': [],
          },
          'generation_synthetic': {
              'max_tokens': fp._GENERATION_MAX_TOKENS,  # pylint: disable=protected-access
              'terminators': [],
          },
          'document_defaults': {
              'temperature': fp._TEMPERATURE,  # pylint: disable=protected-access
              'top_p': fp._TOP_P,  # pylint: disable=protected-access
              'top_k': fp._TOP_K,  # pylint: disable=protected-access
          },
          'sdk_max_retries': fp._SDK_MAX_RETRIES,  # pylint: disable=protected-access
          'client_timeout_seconds': fp._CLIENT_TIMEOUT_SECONDS,  # pylint: disable=protected-access
      },
      'schedule': {
          'repetitions_per_case_per_model': _REPETITIONS,
          'review_cases': len(_REVIEW_CASES),
          'adjudication_cases': 0,
          'planned_review_calls': _MAX_REAL_CALLS,
          'planned_adjudication_calls': 0,
          'max_real_calls': _MAX_REAL_CALLS,
          'synthetic_generation_calls_note': (
              'Each review carries one synthetic generation response '
              '(the fixed stage_directions); it is not a paid request '
              'and is counted separately (144 synthetic vs 144 real).'
          ),
          'execution_order': (
              'R only; repetition -> case -> m1/m2/m4. Group A, full '
              'stories, director interventions and scoring are out of '
              'scope for this batch.'
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
          'attempt and is never replaced by a later successful '
          'response. No per-model budget increase, reasoning change or '
          'parameter change may be mixed into this batch; parameter '
          'adjustments require a new protocol.'
      ),
      'interruption_policy': (
          'An attempt whose request started but produced no archived '
          'response is started_no_result: it stays indeterminate, is '
          'never silently re-sent, and nothing may be claimed about '
          'whether it was delivered or charged. Unstarted planned '
          'entries may continue, but the batch then fails the '
          'completeness gate.'
      ),
      'pass_criteria': {
          'review_per_model': (
              'all 48 planned entries completed with archived '
              'responses; 0 false_rejections, 0 missed_detections, 0 '
              'malformed, 0 api/provider errors, 0 not_started, 0 '
              'started_no_result, 0 harness errors, 0 cross-check or '
              'canonical mismatches'
          ),
          'review_round': (
              'all three models (m1, m2, m4) present and passing the '
              'per-model gate; 144/144 results archived'
          ),
          'human_reason_review': (
              'all 144 reasons carry a completed manual review with '
              'reason_fact_based, reason_conclusion_matches_verdict '
              'and fixture_or_label_dispute filled; zero reasons '
              'without factual basis and zero reason/verdict '
              'contradictions. A correct label with a defective reason '
              'is never described as semantic validation passed.'
          ),
          'overall': (
              'machine gates of the three models plus the completed '
              'manual reason review; newly found fixture/label '
              'disputes keep the original scoring and are listed '
              'separately instead of being re-labelled for a PASS'
          ),
          'ra_dual_model_gate': (
              'not evaluated: this R-only batch does not replace or '
              'imply the original two-model R/A acceptance gate'
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
          'never_started': 'not_started',
          'started_without_archived_response': (
              'started_no_result (indeterminate; delivery and billing unknown)'
          ),
          'probe_fault': 'harness_error',
      },
  }


_SNAPSHOT_FILES = (
    'examples/story_enactment/three_model_review_probe.py',
    'examples/story_enactment/three_model_review_probe_test.py',
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
)


def source_snapshot_payload() -> dict[str, Any]:
  """Builds the source snapshot document.

  Every relevant production file is recorded by content hash and size
  — not merely by Git HEAD — so the exact bytes probed are verifiable.

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
          'Per-file content hashes of the exact bytes this batch '
          'probes; run stages re-verify these hashes before any '
          'request.'
      ),
  }


# ---------------------------------------------------------------------------
# CLI stages.
# ---------------------------------------------------------------------------

# The flags live in a module-private FlagValues instance so this probe
# can be imported alongside `fixed_model_probe` (whose global absl
# flags carry the same names) without registering duplicates.
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
    'run_results/story_enactment/action_authorization_three_model_review/.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'dry_run_dir',
    None,
    'Output directory for the dry run; it must not exist. Defaults to '
    'a new directory under /tmp/opencode.',
    flag_values=FLAGS,
)

_DRY_RUN_PROMPT_DIR = 'prompts'


def _load_settings() -> tuple[dict[ModelTag, str], str, str, list[str]]:
  """Resolves the three live model names and credentials from .env.

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
    ValueError: if any frozen artefact is missing or its bytes no
    longer match what was frozen (protocol, fixtures) or hashed
    (snapshot).
  """
  protocol_path = batch_dir / _PROTOCOL_NAME
  fixtures_path = batch_dir / _FIXTURES_NAME
  snapshot_path = batch_dir / _SNAPSHOT_NAME
  for path in (protocol_path, fixtures_path, snapshot_path):
    if not path.exists():
      raise ValueError(f'frozen artefact missing: {path}')
  names, _, _, _ = _load_settings()
  frozen_protocol = json.loads(protocol_path.read_text(encoding='utf-8'))
  expected_protocol = protocol_payload(names)
  # The creation timestamp is expected to differ; every other field
  # must still match the live protocol contract byte for byte.
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
  _write_json(batch_dir / _PROTOCOL_NAME, protocol_payload(names))
  _write_json(batch_dir / _FIXTURES_NAME, fixtures_payload())
  _write_json(batch_dir / _SNAPSHOT_NAME, source_snapshot_payload())
  report = {
      'probe': _PROBE_NAME,
      'protocol_sha256': sha256_file(batch_dir / _PROTOCOL_NAME),
      'fixtures_sha256': sha256_file(batch_dir / _FIXTURES_NAME),
      'source_snapshot_sha256': sha256_file(batch_dir / _SNAPSHOT_NAME),
      'models': dict(names),
      'counts': {
          'planned_review_attempts': len(planned_review_attempts()),
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
  """Writes the batch report atomically."""
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


def _run_review_group(
    batch_dir: pathlib.Path,
    *,
    real_call_cap: int | None = None,
) -> None:
  """Runs the pending review attempts of the three-model batch.

  Only entries with no on-disk evidence are executed; an entry whose
  request started but produced no archived response is never re-sent.
  The frozen artefacts are re-verified before the first request, and
  the cap on real forwarded calls is enforced before every forward.

  Args:
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
  out_dir = batch_dir / _REVIEW_DIR_NAME
  live: dict[ModelTag, language_model.LanguageModel] = {}
  provider_records: dict[ModelTag, list[dict[str, Any]]] = {}
  for tag in _MODEL_TAGS:
    model, records = _create_live_model(
        names[tag], api_key=api_key, api_base=base_url
    )
    live[tag] = model
    provider_records[tag] = records
  gm = fp.gm_params()
  history = public_history_text()
  executed = report['counts']['real_api_calls']
  for case_id, model_tag, repetition in planned_review_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    if _attempt_files_exist(out_dir, stem):
      continue
    if executed >= cap:
      raise ValueError('real-call cap reached; refusing to send more requests')
    records = provider_records[model_tag]
    first_new_record = [len(records)]

    def enrich(result: dict[str, Any]) -> dict[str, Any]:
      """Attaches this attempt's provider observations to its record."""
      observed = list(records[first_new_record[0] :])
      result['requested_model'] = names[model_tag]
      result['provider_model'] = (
          observed[-1]['provider_model'] if observed else None
      )
      usage = observed[-1]['usage'] if observed else None
      result['token_usage'] = usage if usage else result['token_usage']
      result['provider_observations'] = observed
      return result

    result = fp._run_review_attempt(  # pylint: disable=protected-access
        case=review_case_by_id(case_id),
        real_model=live[model_tag],
        model_tag=model_tag,
        model_name=names[model_tag],
        repetition=repetition,
        gm=gm,
        out_dir=out_dir,
        secrets=secrets,
        history_text=history,
        enrich_result=enrich,
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
    case: fp.ReviewCase, dry_run_prompt_dir: pathlib.Path
) -> tuple[str, str]:
  """Captures the production generation and review prompts offline.

  Args:
    case: the calibrated review fixture.
    dry_run_prompt_dir: where the prompt files are written.

  Returns:
    A (generation prompt, review prompt) tuple.
  """
  return fp._capture_review_prompts(  # pylint: disable=protected-access
      case, dry_run_prompt_dir, history_text=public_history_text()
  )


def _dry_run(dry_run_dir: pathlib.Path) -> None:
  """Runs the no-network rehearsal of the three-model batch.

  Builds every planned attempt offline (144 review slots), drives the
  production final-output factory, captures the actual production
  prompts, and verifies routing, counts, prompt binding, label
  hygiene and the canonical assembly over the calibrated history. No
  real client is ever constructed.

  Args:
    dry_run_dir: output directory; it must not exist.
  """
  if dry_run_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {dry_run_dir}')
  prompt_dir = dry_run_dir / _DRY_RUN_PROMPT_DIR
  prompt_dir.mkdir(parents=True)
  generation_prompts: dict[str, str] = {}
  review_prompts: dict[str, str] = {}
  # Build every one of the 144 planned slots through the real
  # production path, mirroring the frozen execution order, so the
  # byte-identity across models and repetitions is verified
  # empirically rather than assumed.
  for repetition in range(1, _REPETITIONS + 1):
    for case in _REVIEW_CASES:
      for model_tag in _MODEL_ORDER:
        generation, review = _capture_review_prompts(case, prompt_dir)
        key = attempt_id(case.case_id, model_tag, repetition)
        generation_prompts[key] = generation
        review_prompts[key] = review
  findings: list[str] = []
  # Uniqueness and count of the planned keys (144 unique forwards).
  review_keys = planned_review_attempts()
  if len(set(review_keys)) != 144 or len(review_keys) != 144:
    findings.append('review plan is not 144 unique keys')
  # Byte-identical prompts across models and repetitions, and no model
  # name, label or other model's answer inside any prompt.
  for prompts, group in (
      (generation_prompts, 'review-generation'),
      (review_prompts, 'review-review'),
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
  history = public_history_text()
  history_json = json.dumps(history, ensure_ascii=False)
  for case in _REVIEW_CASES:
    generation = generation_prompts[f'{case.case_id}__m1__r1']
    review = review_prompts[f'{case.case_id}__m1__r1']
    for prompt, label in ((generation, 'generation'), (review, 'review')):
      if story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK not in prompt:
        findings.append(
            f'{case.case_id} {label}: public-history section missing'
        )
      if history_json not in prompt:
        findings.append(f'{case.case_id} {label}: frozen history text missing')
      if case.human_rationale in prompt or case.fact_sources in prompt:
        findings.append(f'{case.case_id}: label metadata leaked into prompt')
      if str(case.expected_valid).lower() + '\n' in prompt:
        findings.append(f'{case.case_id}: expected label leaked into prompt')
    if 'If rejecting, name the specific offending text' not in review:
      findings.append(f'{case.case_id} review: question suffix missing')
  # The calibrated F10 anchor reaches the prompts only through the
  # public-history data section (embedded JSON-escaped, so single-line
  # fragments are checked), and stays outside the director window.
  f10_review_prompt = review_prompts['F10__m1__r1']
  for fragment in (
      '容我将已启封的遗嘱信封连同其上的残留火漆',
      '周律师当众展示已启封的遗嘱信封及其上的残留火漆',
      '随后收回信封与遗嘱文本，自行保管',
  ):
    if fragment not in f10_review_prompt:
      findings.append(
          f'F10: revised history event fragment missing: {fragment}'
      )
    if fragment in '\n'.join(director_window()):
      findings.append(f'F10: revised event fragment inside director window')
  summary = {
      'stage': 'dry_run',
      'generated_at_utc': utc_now(),
      'planned_review_attempts': len(review_keys),
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
      },
  }
  _write_json(dry_run_dir / 'dry_run_summary.json', summary)
  for finding in findings:
    print(f'FINDING: {finding}', flush=True)
  print(
      f'dry run complete: {len(findings)} findings; prompts in {prompt_dir}',
      flush=True,
  )


def _load_reason_reviews(
    batch_dir: pathlib.Path,
) -> list[dict[str, Any]]:
  """Loads the manual reason-review records, if the file exists."""
  path = batch_dir / _HUMAN_REVIEW_NAME
  if not path.exists():
    return []
  reviews = []
  for line in path.read_text(encoding='utf-8').splitlines():
    if line.strip():
      reviews.append(json.loads(line))
  return reviews


def _summarize(batch_dir: pathlib.Path) -> None:
  """Summarizes the batch without any model call.

  Counts planned, started, completed, not_started, started_no_result
  and api/provider errors separately (``planned - started`` never
  stands for indeterminate); reports archived responses separately
  from started requests; applies the machine classification; and
  gates the round on the three-model counts plus the completed manual
  review of all 144 reasons. Never constructs a real client and never
  marks the original two-model R/A gate.

  Args:
    batch_dir: the batch directory.

  Raises:
    ValueError: if frozen artefacts changed.
  """
  _verify_frozen(batch_dir)
  report = _load_report(batch_dir)
  review_out = batch_dir / _REVIEW_DIR_NAME
  per_model: dict[ModelTag, dict[str, Any]] = {}
  for tag in _MODEL_TAGS:
    per_model[tag] = {
        'planned': 0,
        'started': 0,
        'completed': 0,
        'responses_archived': 0,
        'records': [],
    }
  for case_id, model_tag, repetition in planned_review_attempts():
    stem = f'{case_id}__{model_tag}__r{repetition}'
    entry = per_model[model_tag]
    entry['planned'] += 1
    started = (review_out / f'{stem}_started.json').exists()
    entry['started'] += int(started)
    if (review_out / f'{stem}_response.txt').exists():
      entry['responses_archived'] += 1
    result_path = review_out / f'{stem}_result.json'
    if result_path.exists():
      record = json.loads(result_path.read_text(encoding='utf-8'))
      entry['records'].append(record)
      if record['status'] == 'completed':
        entry['completed'] += 1

  def review_metrics(tag: ModelTag) -> dict[str, Any]:
    """Aggregates the machine classification of one model."""
    entry = per_model[tag]
    statuses: dict[str, int] = {}
    for record in entry['records']:
      if record['status'] != 'completed':
        statuses[record['status']] = statuses.get(record['status'], 0) + 1
    metrics = {
        'planned': entry['planned'],
        'started': entry['started'],
        'completed': entry['completed'],
        'responses_archived': entry['responses_archived'],
        # A started entry without a result is indeterminate: delivery
        # and billing are unknown and it is never re-sent.
        'started_no_result': entry['started'] - len(entry['records']),
        'not_started': entry['planned'] - entry['started'],
        'statuses': statuses,
        'correct': 0,
        'false_rejection': 0,
        'missed_detection': 0,
        'malformed': 0,
        'api_error': 0,
        'provider_error': 0,
        'harness_error': 0,
        'cross_check_failures': 0,
        'canonical_mismatches': 0,
    }
    for record in entry['records']:
      classification = record.get('classification')
      if classification in metrics and isinstance(classification, str):
        metrics[classification] += 1
      if record.get('cross_check_ok') is False:
        metrics['cross_check_failures'] += 1
      if record.get('canonical_matches_fixture') is False:
        metrics['canonical_mismatches'] += 1
    metrics['pass'] = (
        metrics['planned'] == 48
        and metrics['started'] == 48
        and metrics['completed'] == 48
        and metrics['responses_archived'] == 48
        and metrics['not_started'] == 0
        and metrics['started_no_result'] == 0
        and metrics['false_rejection'] == 0
        and metrics['missed_detection'] == 0
        and metrics['malformed'] == 0
        and metrics['api_error'] == 0
        and metrics['provider_error'] == 0
        and metrics['harness_error'] == 0
        and metrics['cross_check_failures'] == 0
        and metrics['canonical_mismatches'] == 0
    )
    return metrics

  review_by_model = {tag: review_metrics(tag) for tag in _MODEL_TAGS}

  # Manual review of every reason (three independent fields). The
  # review is required for the round gate; label-correct-but-defective
  # reasons keep their machine classification but can never be
  # described as semantic validation passed.
  reviews = _load_reason_reviews(batch_dir)
  planned_ids = {
      attempt_id(case_id, tag, repetition)
      for case_id, tag, repetition in planned_review_attempts()
  }
  reviewed = {}
  for item in reviews:
    if item.get('status') == 'reviewed':
      reviewed[item['attempt_id']] = item
  reason_review = {
      'planned': len(planned_ids),
      'reviewed': len(planned_ids & set(reviewed)),
      'reasons_without_factual_basis': sum(
          1
          for attempt, item in reviewed.items()
          if attempt in planned_ids and item.get('reason_fact_based') is False
      ),
      'reason_conclusion_mismatches': sum(
          1
          for attempt, item in reviewed.items()
          if attempt in planned_ids
          and item.get('reason_conclusion_matches_verdict') is False
      ),
      'fixture_or_label_disputes': sorted({
          str(item.get('dispute_case_id'))
          for attempt, item in reviewed.items()
          if attempt in planned_ids and item.get('fixture_or_label_dispute')
      }),
      'missing_fields': sorted(
          attempt
          for attempt, item in reviewed.items()
          if attempt in planned_ids
          and (
              item.get('reason_fact_based') is None
              or item.get('reason_conclusion_matches_verdict') is None
              or item.get('fixture_or_label_dispute') is None
          )
      ),
  }
  reason_review['complete'] = (
      reason_review['reviewed'] == 144 and not reason_review['missing_fields']
  )
  review_pass = len(review_by_model) == len(_MODEL_TAGS) and all(
      metrics['pass'] for metrics in review_by_model.values()
  )
  reason_defects = (
      reason_review['reasons_without_factual_basis']
      + reason_review['reason_conclusion_mismatches']
  )
  overall_pass = bool(
      review_pass
      and reason_review['complete']
      and reason_defects == 0
      and report['counts']['real_api_calls'] <= _MAX_REAL_CALLS
  )
  report['summary'] = {
      'generated_at_utc': utc_now(),
      'review': review_by_model,
      'review_pass': review_pass,
      'human_reason_review': reason_review,
      'overall_pass': overall_pass,
      'semantic_validation_established': bool(
          overall_pass and not reason_review['fixture_or_label_disputes']
      ),
      'ra_dual_model_gate': (
          'not evaluated: this R-only batch does not replace or imply '
          'the original two-model R/A acceptance gate'
      ),
      'note': (
          'review_pass is the machine gate of the three models; '
          'overall_pass additionally requires the completed manual '
          'review of all 144 reasons with zero reason defects. '
          'Fixture/label disputes keep the original scoring and are '
          'listed separately. started_no_result entries are '
          'indeterminate: no claim is made about delivery or billing.'
      ),
  }
  report['overall_pass'] = overall_pass
  _save_report(batch_dir, report)
  print(
      f'summary written: review_pass={review_pass} '
      f'reason_review_complete={reason_review["complete"]} '
      f'overall_pass={overall_pass}',
      flush=True,
  )


def _parse_flags(argv: list[str]) -> list[str]:
  """Parses this probe's private flags (absl app flags_parser hook).

  The global absl flags belong to `fixed_model_probe` (imported here)
  and are never read by this binary; marking them parsed only satisfies
  absl's post-parse assertion without touching their defaults.

  Args:
    argv: the raw command line including the program name.

  Returns:
    The remaining positional arguments.
  """
  args = FLAGS(argv)
  flags.FLAGS.mark_as_parsed()
  return args


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
            '/tmp/opencode/three_model_review_dry_run_'
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
    print(f'frozen protocol written to {batch_dir}', flush=True)
  elif stage == _STAGE_RUN_REVIEW:
    _run_review_group(batch_dir)
  elif stage == _STAGE_SUMMARIZE:
    _summarize(batch_dir)


if __name__ == '__main__':
  app.run(main, flags_parser=_parse_flags)
