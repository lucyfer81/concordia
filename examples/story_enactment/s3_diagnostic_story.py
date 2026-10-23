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

"""S3 diagnostic story runner: ledgered executor of the frozen protocol.

This module serves the S3 diagnostic story batch of
``docs/superpowers/plans/2026-10-23-sol-s3-diagnostic-story-protocol.md``:
two runs (R1/R2) of the production enactment pipeline over
AI_MODEL_4 / gpt-6.1-sol, 24 story turns and 400 model attempts each.
The frozen §3 director configuration — feedback on, scoring interval 2,
window 8, tolerance 1, repetition intervention on with interval 3 and
the shared cooldown of 3 turns — travels explicitly in the run config
the runner builds for each run; production defaults are never relied
upon.
Production ``run.py`` stays byte-identical: the ``LedgerLanguageModel``
is injected by module-attribute replacement with try/finally restore
(later tasks), and every logical model call occupies exactly one
request-ledger slot — the ledger reserves one budget unit when the
``started`` marker is persisted, before the provider is contacted
(design §4.1 of the 2026-10-21 accounting design), so API errors,
timeouts and no-result calls all spend budget.

At the SDK boundary an observing client forwards every
``chat.completions.create`` through the protocol's second ledger layer
(protocol §4): each create is pre-debited against a per-run SDK hard
cap of 400 — cap check and debit synchronized under one lock — before
it is forwarded, so a capped create is refused before the provider is
contacted and books no slot. Apart from that budget gate the client
only observes: it archives the raw response and the provider-returned
model identity before any parsing. Production SDK retries, the
Concordia retry whitelist and the budget guard chain keep their exact
production behavior, which is what makes the batch a
production-fidelity diagnostic. The Concordia-layer ledger and the
SDK-layer ledger (rooted at ``batch_dir/'sdk_ledger'``) are rebuilt
separately from their own started events — associated per call through
the attempt id, never summed into one number.

The freeze stage captures the user-confirmed protocol, a per-file
SHA-256 snapshot of every repository file the real path touches, and
the initial batch report into a fresh batch directory. Freezing is
fully offline (no credentials read, no client built, no network); the
run stage below re-verifies every recorded hash before any attempt and
refuses to continue after drift.

The offline dry-run stage exercises the whole pipeline end to end with
zero network: it freezes a batch into the dry-run directory, executes
both runs through the deterministic stub path of the production runner,
summarizes, and records each batch invariant (terminal reasons,
ledger/production attempt equality, archived prompts, caps, stub-only
request stages) in ``dry_run_report.json`` — the diagnostic has no
acceptance gate. The command-line entry point at the bottom dispatches
the ``--stage`` flag: ``dry_run``, ``freeze``, ``run`` or ``summarize``;
the real ``freeze``/``run`` round requires separate user authorization
per protocol §8.
"""

from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
import contextlib
import dataclasses
import datetime
import hashlib
import json
import os
import pathlib
import threading
import time
from typing import Any

from absl import app
from absl import flags
from concordia.contrib.components.game_master import story_terminator
from concordia.contrib.language_models import budget_wrapper
from concordia.contrib.language_models.openai import gpt_model
from concordia.language_model import language_model

from examples.story_enactment import request_ledger as rl
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config

# -----------------------------------------------------------------------------
# Frozen protocol constants (2026-10-23-sol-s3-diagnostic-story-protocol.md).
# -----------------------------------------------------------------------------

_BATCH_ID = 's3_diagnostic_story'
_RUN_IDS = ('R1', 'R2')
_MODEL_TAG = 'm4'
_MODEL_NAME = 'gpt-6.1-sol'
_ENV_KEY = 'AI_MODEL_4'
_MODE = 'director'
_ACTION_MODE = 'speech_and_action'
_DEFAULT_TURNS = 24
_DEFAULT_ATTEMPTS = 400

# Frozen §3 director parameter table (protocol line 50-58). The runner
# copies these seven values into the run config it builds for every
# run — the batch must not depend on production defaults: drift would
# silently change the diagnostic's configuration mainline.
_FROZEN_DIRECTOR_PARAMS: dict[str, Any] = {
    'feedback_enabled': True,
    'eval_interval': 2,
    'scoring_window': 8,
    'tolerance': 1,
    'repetition_push_enabled': True,
    'repetition_check_interval': 3,
    'cooldown_turns': 3,
}

# Final story termination reasons (story_terminator, spec section 4.7),
# used by later tasks to ground closure checks.
_TERMINAL_REASONS = frozenset({
    story_terminator.REASON_ERROR,
    story_terminator.REASON_BEATS_COMPLETED,
    story_terminator.REASON_BEAT_TIMEOUT,
    story_terminator.REASON_SCENE_LIMIT,
    story_terminator.REASON_STEP_LIMIT,
    story_terminator.REASON_BUDGET_LIMIT,
})


class HarnessError(RuntimeError):
  """Diagnostic-harness fault base class; never forwarded to providers."""


class BatchStop(HarnessError):
  """Unrecoverable harness condition; stop the batch, keep the evidence."""


def ledger_caps(attempt_limit: int = _DEFAULT_ATTEMPTS) -> dict[str, Any]:
  """Returns the ``RequestLedger`` cap kwargs for the two diagnostic runs.

  Args:
    attempt_limit: per-run attempt budget (the protocol freezes 400).

  Returns:
    Keyword arguments for ``RequestLedger``: ``global_cap`` and the
    ``m4`` model cap are ``attempt_limit * len(_RUN_IDS)``, while
    ``group_caps`` and ``sample_caps`` give each run its own independent
    budget keyed by run id (R1 and R2 never share budget).
  """
  total = attempt_limit * len(_RUN_IDS)
  return {
      'global_cap': total,
      'model_caps': {_MODEL_TAG: total},
      'group_caps': {run_id: attempt_limit for run_id in _RUN_IDS},
      'sample_caps': {run_id: attempt_limit for run_id in _RUN_IDS},
  }


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
  if value is None or isinstance(value, (str, int, float, bool, list)):
    return value
  public = {
      name: getattr(value, name)
      for name in dir(value)
      if not name.startswith('_') and not callable(getattr(value, name, None))
  }
  return public or repr(value)


def _first_content(response: Any) -> str | None:
  """Extracts the visible text of a provider response, if any."""
  try:
    return response.choices[0].message.content
  except Exception:  # pylint: disable=broad-except
    return None


class _SdkCreateLedger:
  """Second ledger layer at the SDK ``create`` boundary (protocol §4).

  One instance owns the batch's SDK request ledger, rooted at
  ``batch_dir/'sdk_ledger'``, and enforces the frozen per-run hard cap
  of 400 ``chat.completions.create`` calls. Every create is pre-debited
  before it is forwarded: under one lock, all four cap layers are
  checked and the ``started`` slot is persisted, so concurrent callers
  cannot oversubscribe and a capped create is refused before the
  provider is contacted (it books no slot; only the ledger's own
  ``budget_blocked`` evidence event is appended). Cap exhaustion is
  raised as the production ``BudgetExhaustedError``, so the run stops
  through the budget-exhaustion path with the layer named in the
  message. Counts rebuild from the SDK ledger's started events alone
  and stay separate from the Concordia-layer ledger: a slot associates
  with its logical attempt through ``attempt_id``, the two layers are
  never summed.
  """

  def __init__(
      self,
      batch_dir: pathlib.Path | str,
      *,
      attempt_limit: int = _DEFAULT_ATTEMPTS,
      secrets: Sequence[str] = (),
      before_request: Callable[[], None] | None = None,
  ):
    """Prepares the SDK ledger, rebuilding counts if one already exists.

    Args:
      batch_dir: the batch directory; the SDK ledger lives in its
        ``sdk_ledger`` subdirectory.
      attempt_limit: per-run SDK create hard cap (the protocol freezes
        400); the global cap is twice that, one independent budget per
        run.
      secrets: literal values redacted from every archived text.
      before_request: optional frozen-source and ledger audit performed
        under the shared bookkeeping lock before either layer forwards.
    """
    self._lock = threading.RLock()
    self._before_request = before_request
    self._ledger = rl.RequestLedger(
        pathlib.Path(batch_dir) / _SDK_LEDGER_DIRNAME,
        batch_id=_BATCH_ID,
        secrets=secrets,
        **ledger_caps(attempt_limit),
    )
    # The slot sequence continues after a rebuild: seed it from the
    # ledger's rebuilt per-run count, never from volatile runtime state.
    self._seq = {
        run_id: self._ledger.counts['by_group'].get(run_id, 0)
        for run_id in _RUN_IDS
    }

  @property
  def counts(self) -> dict[str, Any]:
    """A snapshot of the SDK ledger's counts (``rebuild_counts`` shape)."""
    with self._lock:
      return self._ledger.counts

  @contextlib.contextmanager
  def synchronized(self) -> Iterator[None]:
    """Serializes bookkeeping in both ledgers, never network waits."""
    with self._lock:
      yield

  def check_before_request(self) -> None:
    """Checks frozen inputs and ledger integrity before forwarding."""
    with self._lock:
      if self._before_request is not None:
        self._before_request()

  def begin(
      self,
      *,
      run_id: str,
      attempt_id: str,
      prompt: str,
      passed_params: Mapping[str, Any],
  ) -> str:
    """Checks every cap layer and reserves one SDK slot durably.

    Args:
      run_id: the diagnostic run (R1/R2) whose SDK budget is spent.
      attempt_id: the Concordia-layer slot the create belongs to
        (association only; the layers are never summed).
      prompt: the exact request payload sent to the provider.
      passed_params: the parameters passed alongside the payload.

    Returns:
      The reserved SDK slot id.

    Raises:
      BudgetExhaustedError: if any cap layer would be exceeded; nothing
        was forwarded and no slot was booked.
    """
    with self._lock:
      self.check_before_request()
      slot_id = f'{run_id}__sdk{self._seq[run_id] + 1:06d}'
      try:
        self._ledger.begin_request(
            slot_id=slot_id,
            sample_id=run_id,
            group=run_id,
            model_tag=_MODEL_TAG,
            model_name=_MODEL_NAME,
            attempt_id=attempt_id,
            stage='sdk_create',
            prompt=prompt,
            passed_params=dict(passed_params),
        )
      except rl.LedgerBudgetError as error:
        raise budget_wrapper.BudgetExhaustedError(
            'SDK chat.completions.create budget exhausted (stop layer:'
            f' sdk_ledger, separate from the Concordia 400): {error}'
        ) from error
      self._seq[run_id] += 1
      return slot_id

  def finish(
      self,
      slot_id: str,
      *,
      status: str,
      error: str | None = None,
  ) -> None:
    """Records the terminal observation of a started SDK slot.

    Args:
      slot_id: the started SDK slot being closed.
      status: terminal status label (``completed`` or the exception
        type name).
      error: optional error text; redacted before archiving.
    """
    with self._lock:
      self._ledger.finish_request(slot_id=slot_id, status=status, error=error)

  def record_response(self, slot_id: str, text: Any) -> None:
    """Archives the raw provider response at the SDK boundary.

    Args:
      slot_id: the started SDK slot the response belongs to.
      text: the raw response payload (str or JSON-serializable value).
    """
    with self._lock:
      self._ledger.record_raw_response(slot_id, text)


class _ObservationState(threading.local):
  """Per-thread active slot shared with the observing client.

  The wrapper arms it with the slot id right after ``begin_request``
  and disarms it after ``finish_request``; between the two, every
  provider call is attributable to exactly one ledger slot, and a call
  arriving with no active slot is a wiring error reported as
  ``BatchStop``.
  """

  def __init__(self) -> None:
    self.slot_id: str | None = None
    self.boundary: dict[str, Any] = {}
    self.observed_response = False

  def begin(self, slot_id: str) -> None:
    """Arms the state for the slot whose request is starting."""
    self.slot_id = slot_id
    self.boundary = {}
    self.observed_response = False

  def finish(self) -> None:
    """Disarms the state after the slot's terminal observation."""
    self.slot_id = None
    self.boundary = {}
    self.observed_response = False


class _ObservingCompletions:
  """Forwards every ``create()`` under the SDK ledger's discipline.

  The observation fires after the real response returns and before any
  parsing: the boundary kwargs (messages excluded — the final user
  message is the archived prompt), the provider-returned model identity
  and the token usage are recorded, and the raw first content is
  archived through both ledgers. Archival failures propagate as faults
  while retaining the charged request, so missing evidence cannot be
  silently treated as successful observation.

  Budget-wise the create is the SDK layer's unit: ``begin`` pre-debits
  one SDK slot under the cap check before the call is forwarded — a
  capped create raises before the provider is contacted — and
  ``finish`` only observes the terminal state. No request is refused,
  retried or re-shaped here beyond that gate: production retry fidelity
  depends on it.
  """

  def __init__(
      self,
      real: Any,
      state: _ObservationState,
      ledger: rl.RequestLedger,
      sdk_ledger: _SdkCreateLedger,
      run_id: str,
  ):
    self._real = real
    self._state = state
    self._ledger = ledger
    self._sdk_ledger = sdk_ledger
    self._run_id = run_id

  def create(self, *args: Any, **kwargs: Any) -> Any:
    slot_id = self._state.slot_id
    if slot_id is None:
      raise BatchStop('provider call outside any active slot.')
    call_params = {
        key: _jsonable(value)
        for key, value in kwargs.items()
        if key != 'messages'
    }
    messages = kwargs.get('messages')
    # Protocol §4: pre-debit the SDK slot before forwarding — the cap
    # check and the durable started marker happen before the provider
    # is contacted, and a capped create never reaches it.
    sdk_slot = self._sdk_ledger.begin(
        run_id=self._run_id,
        attempt_id=slot_id,
        prompt=json.dumps(messages, ensure_ascii=False, default=repr),
        passed_params=call_params,
    )
    try:
      response = self._real.create(*args, **kwargs)
      content = _first_content(response)
      self._sdk_ledger.record_response(sdk_slot, content)
      boundary = {
          'observation_level': 'sdk_client',
          'requested_model': kwargs.get('model'),
          'boundary_kwargs': call_params,
          'messages_count': len(messages) if messages is not None else None,
          'provider_model': getattr(response, 'model', None),
          'usage': _jsonable(getattr(response, 'usage', None)),
          'first_content': content,
      }
      with self._sdk_ledger.synchronized():
        self._ledger.record_raw_response(slot_id, boundary['first_content'])
      self._state.boundary = boundary
      self._state.observed_response = True
    except BaseException as error:  # pylint: disable=broad-except
      self._sdk_ledger.finish(
          sdk_slot,
          status=type(error).__name__,
          error=f'{type(error).__name__}: {error}',
      )
      raise
    self._sdk_ledger.finish(sdk_slot, status='completed')
    return response


class _ObservingChat:
  """Facade exposing the observing ``completions`` facet."""

  def __init__(
      self,
      real_chat: Any,
      state: _ObservationState,
      ledger: rl.RequestLedger,
      sdk_ledger: _SdkCreateLedger,
      run_id: str,
  ):
    self.completions = _ObservingCompletions(
        real_chat.completions, state, ledger, sdk_ledger, run_id
    )


class _ObservingClient:
  """Client facade exposing the observing ``chat.completions``.

  Every attribute except ``chat`` forwards to the real client, so the
  adapter keeps its full production surface; ``with_options`` records
  the call and returns a new observing client sharing the same
  observation state.
  """

  def __init__(
      self,
      real: Any,
      state: _ObservationState,
      ledger: rl.RequestLedger,
      sdk_ledger: _SdkCreateLedger,
      run_id: str,
  ):
    self._real = real
    self._state = state
    self._ledger = ledger
    self._sdk_ledger = sdk_ledger
    self._run_id = run_id
    self.chat = _ObservingChat(real.chat, state, ledger, sdk_ledger, run_id)
    self.with_options_calls: list[dict[str, Any]] = []

  def __getattr__(self, name: str) -> Any:
    return getattr(self._real, name)

  def with_options(self, **kwargs: Any) -> '_ObservingClient':
    self.with_options_calls.append(dict(kwargs))
    return _ObservingClient(
        self._real.with_options(**kwargs),
        self._state,
        self._ledger,
        self._sdk_ledger,
        self._run_id,
    )


class LedgerLanguageModel(language_model.LanguageModel):
  """Wraps one run's provider model with per-attempt ledger discipline.

  Every logical call occupies exactly one ledger slot with id
  ``f'{run_id}__a{seq:06d}'``; the sequence is seeded from the ledger's
  rebuilt per-run count, so numbering continues after a restart.
  ``begin_request`` checks all four cap layers and persists the prompt
  and the started marker before the call is forwarded — a started slot
  occupies budget even if the provider never answers — and ``finish``
  records the terminal status without adding budget. When the inner
  model exposes an OpenAI-compatible ``_client``, the client is wrapped
  by the observing client, which pre-debits the run's SDK ledger at
  every ``chat.completions.create`` and archives raw evidence at the
  SDK boundary; otherwise the returned text stands as the archived
  response at model level.
  """

  def __init__(
      self,
      model_name: str,
      *,
      ledger: rl.RequestLedger,
      sdk_ledger: _SdkCreateLedger,
      run_id: str,
      attempt_limit: int = _DEFAULT_ATTEMPTS,
      stage: str = 'real_request',
      inner: Any = None,
      api_key: str = '',
      api_base: str = '',
  ):
    """Builds the wrapper around a live or injected inner model.

    Args:
      model_name: the live model's provider name.
      ledger: the batch request ledger owning budget and archival.
      sdk_ledger: the second ledger layer whose per-run hard cap every
        ``chat.completions.create`` of the wrapped client pre-debits.
      run_id: the diagnostic run (R1/R2) whose budget the calls spend.
      attempt_limit: the run's configured attempt budget, kept for the
        run stages; budget enforcement itself is the ledger's job.
      stage: pipeline stage label recorded on every started slot.
      inner: injected inner model (offline tests); by default the real
        OpenAI-compatible client for ``model_name``.
      api_key: provider API key when the default inner model is built.
      api_base: provider base URL when the default inner model is built.
    """
    self._model_name = model_name
    self._ledger = ledger
    self._sdk_ledger = sdk_ledger
    self._run_id = run_id
    self._attempt_limit = attempt_limit
    self._stage = stage
    self._state = _ObservationState()
    self._condition = threading.Condition()
    self._active_calls = 0
    self._closed = False
    if inner is None:
      inner = gpt_model.GptLanguageModel(
          model_name=model_name, api_key=api_key, api_base=api_base
      )
    client = getattr(inner, '_client', None)
    if client is not None and hasattr(client, 'chat'):
      inner._client = _ObservingClient(  # pylint: disable=protected-access
          client, self._state, ledger, sdk_ledger, run_id
      )
    self._inner = inner
    # The slot sequence continues after a rebuild: seed it from the
    # ledger's rebuilt per-run count, never from volatile runtime state.
    self._seq = ledger.counts['by_group'].get(run_id, 0)
    self._attempts = 0

  @property
  def model_name(self) -> str:
    """The live model's provider name."""
    return self._model_name

  @property
  def attempts(self) -> int:
    """Number of logical calls (ledger slots) consumed by this wrapper."""
    return self._attempts

  def close(self) -> None:
    """Drains admitted calls and refuses late work after production exits.

    Concordia's fail-fast executor may return before other component
    futures finish. Wait for their admitted calls to archive results,
    while preventing later calls from reaching either ledger/provider.
    """
    with self._condition:
      self._closed = True
      self._condition.notify_all()
      self._condition.wait_for(lambda: self._active_calls == 0)

  @contextlib.contextmanager
  def _active_call(self) -> Iterator[None]:
    with self._condition:
      if self._closed:
        raise BatchStop('model call after run closure; no request forwarded')
      self._active_calls += 1
    try:
      yield
    finally:
      with self._condition:
        self._active_calls -= 1
        self._condition.notify_all()

  def _run_call(
      self,
      prompt: str,
      call: Callable[[], Any],
      passed_params: Mapping[str, Any],
      record_result: Callable[[str, Any], None] | None = None,
  ) -> Any:
    """Runs one logical call as exactly one ledger slot.

    ``begin_request`` checks every cap layer and persists the prompt and
    the started marker before the call is forwarded, so a started slot
    occupies budget even if the provider never answers (design §4.1);
    ``_finish`` only observes the terminal state.

    Args:
      prompt: the exact prompt text sent to the provider.
      call: thunk forwarding to the inner model.
      passed_params: the parameters passed alongside the prompt.
      record_result: optional model-level archiving of the returned
        value, used when no SDK client observation archived a raw
        response first (stub inner models, or observation failures).

    Returns:
      The inner call's result.
    """
    with self._active_call():
      with self._sdk_ledger.synchronized():
        self._sdk_ledger.check_before_request()
        slot_id = f'{self._run_id}__a{self._seq + 1:06d}'
        self._ledger.begin_request(
            slot_id=slot_id,
            sample_id=self._run_id,
            group=self._run_id,
            model_tag=_MODEL_TAG,
            model_name=self._model_name,
            attempt_id=f'{self._run_id}__{_MODEL_TAG}',
            stage=self._stage,
            prompt=prompt,
            passed_params=dict(passed_params),
        )
        self._seq += 1
        self._attempts += 1
      self._state.begin(slot_id)
      try:
        result = call()
        if record_result is not None and not self._state.observed_response:
          with self._sdk_ledger.synchronized():
            record_result(slot_id, result)
      except BaseException as error:  # pylint: disable=broad-except
        self._finish(slot_id, error)
        raise
      self._finish(slot_id, None)
      return result

  def _finish(self, slot_id: str, error: BaseException | None) -> None:
    """Records the slot's terminal observation and disarms the state."""
    boundary = dict(self._state.boundary)
    if not self._state.observed_response:
      # No SDK client was observable (a stub inner model, or the failure
      # came before any response): the call site's model-level archiving
      # stands as the archived response.
      boundary['observation_level'] = 'model_return'
    status = 'completed' if error is None else type(error).__name__
    try:
      with self._sdk_ledger.synchronized():
        self._ledger.finish_request(
            slot_id=slot_id,
            status=status,
            error=None if error is None else f'{type(error).__name__}: {error}',
            boundary=boundary,
        )
    finally:
      self._state.finish()

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
    """Forwards one text call as exactly one ledger slot.

    Args:
      prompt: the initial text to condition on.
      max_tokens: the maximum number of tokens in the response.
      terminators: the response terminates before any of these.
      temperature: temperature for the model.
      top_p: cumulative-probability filter for the model.
      top_k: top-k filter for the model.
      timeout: timeout for the request.
      seed: optional seed for the sampling.

    Returns:
      The sampled response text.
    """
    params = {
        'max_tokens': max_tokens,
        'terminators': list(terminators),
        'temperature': temperature,
        'top_p': top_p,
        'top_k': top_k,
        'timeout': timeout,
        'seed': seed,
    }

    def call() -> str:
      return self._inner.sample_text(
          prompt,
          max_tokens=max_tokens,
          terminators=terminators,
          temperature=temperature,
          top_p=top_p,
          top_k=top_k,
          timeout=timeout,
          seed=seed,
      )

    return self._run_call(
        prompt,
        call,
        params,
        record_result=self._ledger.record_raw_response,
    )

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Mapping[str, Any]]:
    """Forwards one choice call as exactly one ledger slot.

    Args:
      prompt: the initial text to condition on.
      responses: the responses to score.
      seed: optional seed for the sampling.

    Returns:
      (index, response, info) from the inner model.
    """
    params = {'responses': list(responses), 'seed': seed}

    def call() -> tuple[int, str, Mapping[str, Any]]:
      return self._inner.sample_choice(prompt, responses, seed=seed)

    return self._run_call(prompt, call, params)


# -----------------------------------------------------------------------------
# Freeze stage (Task 2): frozen protocol, source snapshot, initial report.
#
# Freezing is offline by design: no credentials are read and no client is
# built; the payloads below are pure data built from the frozen constants
# above. The source snapshot is computed at call time from the repo root,
# so freezing captures the working tree; the run stage re-verifies every
# hash before each attempt and refuses to continue after drift (the S2
# probe pattern, protocol §3: 源冻结).
# -----------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PROTOCOL_DOCUMENT = (
    'docs/superpowers/plans/2026-10-23-sol-s3-diagnostic-story-protocol.md'
)
_PROTOCOL_NAME = 'protocol.json'
_SNAPSHOT_NAME = 'source_snapshot.json'
_REPORT_NAME = 'report.json'
_LEDGER_NAME = 'requests.jsonl'
_RAW_DIRNAME = 'raw'
# The second ledger layer (protocol §4) owns one requests.jsonl of its
# own, rooted at this subdirectory of the batch directory.
_SDK_LEDGER_DIRNAME = 'sdk_ledger'

# Repo-root-relative posix paths of everything the real path touches:
# this runner and its request ledger, the shared story-enactment
# modules, the concordia production modules the enactment pipeline
# executes, and the protocol and design documents.
_SNAPSHOT_FILES: tuple[str, ...] = (
    'examples/story_enactment/s3_diagnostic_story.py',
    'examples/story_enactment/s3_diagnostic_story_test.py',
    'examples/story_enactment/request_ledger.py',
    'examples/story_enactment/request_ledger_test.py',
    'examples/story_enactment/run.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/testing_stubs.py',
    'examples/story_enactment/collector.py',
    'examples/story_enactment/provenance.py',
    'examples/story_enactment/renderer.py',
    'concordia/components/game_master/terminate.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/components/game_master/story_action.py',
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_terminator.py',
    'concordia/contrib/language_models/budget_wrapper.py',
    'concordia/contrib/language_models/retry_stats_wrapper.py',
    'concordia/contrib/prefabs/entity/story_character.py',
    'concordia/contrib/prefabs/game_master/story_dramaturgic.py',
    'concordia/contrib/prefabs/game_master/story_scene.py',
    'concordia/contrib/language_models/openai/gpt_model.py',
    'concordia/contrib/language_models/openai/base_gpt_model.py',
    'concordia/language_model/language_model.py',
    'concordia/prefabs/simulation/generic.py',
    'concordia/agents/entity_agent_with_logging.py',
    _PROTOCOL_DOCUMENT,
    (
        'docs/superpowers/specs/'
        '2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md'
    ),
)


def _utc_now() -> str:
  """Returns the current UTC time in the batch timestamp format."""
  return datetime.datetime.now(
      datetime.timezone.utc,
  ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _file_sha256(path: pathlib.Path) -> str:
  """Returns the hex SHA-256 of a file's bytes."""
  return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_file(path: pathlib.Path, payload: Mapping[str, Any]) -> None:
  """Writes indented JSON with a trailing newline, as the batch does."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
  )


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


def protocol_payload(created_utc: str | None = None) -> dict[str, Any]:
  """Builds the frozen protocol document (S3 protocol §3/§4/§5/§6/§7/§8).

  Args:
    created_utc: freeze-time stamp; the payload alone stays None so the
      frozen file and the in-code payload never diverge by a timestamp.

  Returns:
    The protocol dict: model scope and identity disclosure, run plan,
    the frozen §3 director parameter table, budgets, retry disclosure,
    failure handling (§5), evaluation criteria (§6), conclusion
    semantics, invocation and stop boundary.
  """
  return {
      'batch': _BATCH_ID,
      'protocol_document': {
          'path': _PROTOCOL_DOCUMENT,
          'confirmation': 'user-confirmed 2026-10-23',
      },
      'model_scope': 'Sol-only',
      'models': {
          _MODEL_TAG: {
              'env_key': _ENV_KEY,
              'expected_name': _MODEL_NAME,
              'api': (
                  'OpenAI-compatible GptLanguageModel'
                  '（gpt_model.GptLanguageModel，生产适配器）；SDK '
                  'max_retries 保持生产默认值，请求级超时来自生产适配器。'
              ),
          },
      },
      'identity_disclosure': (
          '唯一模型经 OpenAI 兼容端点以 AI_MODEL_4 / gpt-6.1-sol 寻址；'
          '一模型承担全部角色、裁决、生成、评审、评分。提供方返回的'
          '模型标识逐请求留证（provider_model）；请求名与返回名可能'
          '不同，如实披露，该差异不作为已验证身份的证据，不作任何'
          '身份冒称。'
      ),
      'runs': list(_RUN_IDS),
      'director_params': {
          'values': dict(_FROZEN_DIRECTOR_PARAMS),
          'source': (
              '协议 §3 冻结参数表：由 runner 为每次运行显式构建并传入 '
              'run_config，不依赖生产默认值隐式开启重复干预；dry-run '
              '核对 checkpoint 实际生效的 director_params 与此表一致。'
          ),
      },
      'run_policy': (
          '两次独立完整运行按 R1→R2 注册顺序执行：同源同配置、fresh '
          '初始场景、不共享状态、不重跑已完成运行；两次不足以判明'
          '偶然性或建立可靠性，报告必须如实声明该限制，不将两次诊断'
          '当作受控因果比较。'
      ),
      'budgets': {
          'turns_per_run': _DEFAULT_TURNS,
          'attempts_per_run': _DEFAULT_ATTEMPTS,
          'sdk_create_cap_per_run': _DEFAULT_ATTEMPTS,
          'http_attempts_upper_bound_per_run': 3 * _DEFAULT_ATTEMPTS,
          'note': (
              'R1/R2 预算独立：不跨运行结转、不重分配。turns、'
              'Concordia attempts 与 SDK create 次数由追加式请求账本的'
              '对应 started 重建镜像，不混加、不互相代替；started 即'
              '预扣，finished 不再扣费，失败与未决不退款。HTTP 上界'
              '为按 SDK max_retries=2 推导的数值，不是实测请求数。'
          ),
      },
      'retry_disclosure': (
          '保持生产重试链路，观测只记录、不阻止：SDK '
          'chat.completions.create 保持默认 max_retries=2（每次 SDK '
          '调用最多三次 HTTP 尝试，每运行重试尝试上界 1200）；外层 '
          'tenacity 最多 3 次尝试（最多 2 次重试），仅重试 '
          'APIConnectionError / APITimeoutError / RateLimitError，'
          '固定延迟 0.0、jitter (0.0, 0.0)、exponential_backoff=false'
          '（依据 run.py:build_simulation 与 retry_stats_wrapper.py，'
          '不是通用 retry wrapper 的默认指数退避）。不复刻 S2 的 '
          'max_retries=0 或单 slot 单次 SDK 调用守卫。'
      ),
      'failure_handling': {
          '语义评审拒绝': (
              '生产停止行为（StoryOutputError → reason=error，不重新'
              '生成）；该运行结束，批次按 R1→R2 继续。'
          ),
          '认证/路由/启动器失败': (
              '非零退出即停止批次，保留现场，记录未启动运行；退出码不掩盖。'
          ),
          '预算耗尽或 beat_timeout': (
              '记录实际停止原因与层次；无基础设施故障时按注册顺序继续'
              '下一运行，额度不结转；beat_timeout 不是完成结局。'
          ),
          '运行中断': (
              '默认停止该运行并暂停批次、保留现场，标为 interrupted，'
              '交用户审阅；不自动 resume、跳过 pending、补跑、替换'
              '样本或生成缺失结果。'
          ),
          '禁止': (
              '不改提示、结局条件、裁决规则或预算来提高完成率；'
              'no auto-repair、no best-of、已完成运行不重跑。'
          ),
      },
      'evaluation_criteria': (
          '逐轮五问（逐轮、可引用原文证据）：1) 回应具体性，还是'
          '程序性重复；2) 条件满足后的行动或明确拒绝，各角色声音是否'
          '可区分；3) 世界回应的因果，新信息是否有来源、是否只到达'
          '被允许的听众；4) 实际推进判定：首次发现阻塞若改变角色掌握'
          '的信息、可行选择或具体策略可计推进，重复准备、改述请求、'
          '无依据的额外核验与无实质变化的微步不算；5) 导演效果是否'
          '减少循环、改变局势。汇总诊断：循环段识别与推进/循环事件'
          '比；干预-循环因果记录（启用开关不等于必定触发，不强制'
          '敲门、生成回应或增加干预补齐样本）；结局条件核对表：四个'
          '允许结局（真相大白/秘密永埋/协商保宅/僵局散场）逐项核对'
          '条件是否真实发生，进入终局节拍 b4 不足为凭；评审误拒嫌疑'
          '（REV05/REV07 型边界行为被拒）单列，不自动计为模型违规；'
          'beat_timeout 不是完成结局，合理超时照实报告，不粉饰为'
          '接近完成。'
      ),
      'conclusion_semantics': (
          '诊断无验收门：报告只回答「障碍在哪」（逐轮五问、循环诊断、'
          '结局条件核对、误拒嫌疑），不宣布 MVP 质量验收；若出现至少'
          '一条完整合法结局轨迹，仅记为「完成轨迹已演示」，两次运行'
          '不确立可靠性。'
      ),
      'invocation': (
          'source venv/bin/activate; python -m'
          ' examples.story_enactment.s3_diagnostic_story'
          ' --stage={freeze|run|summarize} --batch_dir=<batch directory>'
      ),
      'stop_boundary': (
          '本协议不发起调用：真实执行需工程审阅通过后由用户单独授权，'
          '在 run_results 下新建冻结批次目录后进行；本协议不自动授权'
          '最多 800 次 SDK 调用及其生产重试。'
      ),
      'created_utc': created_utc,
  }


def source_snapshot_payload(root: pathlib.Path = _REPO_ROOT) -> dict[str, Any]:
  """Per-file content hashes of the exact bytes this batch touches.

  Hashes are computed at call time from ``root``, so freezing captures
  the working tree and every later stage can re-verify it. The optional
  ``root`` exists for offline tests that verify drift detection against
  tmp-rooted fake files; production always resolves against the repo
  root.

  Args:
    root: the tree the snapshot paths resolve against.

  Returns:
    The snapshot dict with one ``path``/``sha256``/``bytes`` entry per
    snapshot file, in declaration order.

  Raises:
    BatchStop: if any snapshot file is missing (no silent shrinking).
  """
  files = []
  for relative in _SNAPSHOT_FILES:
    path = root / relative
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
          'Per-file content hashes of this runner and its request'
          ' ledger, the shared story-enactment modules, the concordia'
          ' production modules the real path touches, and the protocol'
          ' and design documents. Run stages re-verify these hashes'
          ' before each attempt and refuse to continue after drift.'
      ),
  }


def _verify_frozen(
    batch_dir: pathlib.Path, root: pathlib.Path = _REPO_ROOT
) -> None:
  """Re-verifies the frozen artifacts by hash before any request.

  Mirrors the S2 probe semantics: the three frozen files must exist,
  every file listed in the source snapshot (paths resolved against
  ``root``) must still match its recorded hash, the report's recorded
  ``protocol.json``/``source_snapshot.json`` hashes must match the
  actual bytes, and the protocol document must still match the
  recorded ``protocol_document_sha256``.

  Args:
    batch_dir: the frozen batch directory.
    root: the tree snapshot paths resolve against (offline test seam).

  Raises:
    BatchStop: on any missing artifact or any hash mismatch.
  """
  for name in (_PROTOCOL_NAME, _SNAPSHOT_NAME, _REPORT_NAME):
    if not (batch_dir / name).exists():
      raise BatchStop(f'frozen artefact missing: {name}')
  report = _load_report(batch_dir)
  snapshot = json.loads(
      (batch_dir / _SNAPSHOT_NAME).read_text(encoding='utf-8')
  )
  for entry in snapshot['files']:
    path = root / entry['path']
    if not path.exists() or _file_sha256(path) != entry['sha256']:
      raise BatchStop(f"source drift detected: {entry['path']}")
  for name, key in (
      (_PROTOCOL_NAME, 'protocol_sha256'),
      (_SNAPSHOT_NAME, 'source_snapshot_sha256'),
  ):
    recorded = report.get(key)
    if not recorded or _file_sha256(batch_dir / name) != recorded:
      raise BatchStop(f'{name} hash mismatch against the batch report.')
  document = root / _PROTOCOL_DOCUMENT
  recorded_document = report.get('protocol_document_sha256')
  if (
      not recorded_document
      or not document.exists()
      or _file_sha256(document) != recorded_document
  ):
    raise BatchStop('protocol document hash mismatch against the report.')


def freeze_stage(batch_dir: pathlib.Path | str) -> None:
  """Freezes protocol, source snapshot and the initial batch report.

  Offline by design: no credentials are read, no client is built and
  no request leaves the process. The source snapshot is computed at
  freeze time, capturing the working tree; the report records the
  hashes of the frozen payloads and of the protocol document itself,
  so the run stage can verify none of them changed.

  Args:
    batch_dir: the fresh batch directory to create and populate.

  Raises:
    BatchStop: if the directory (or any snapshot file) already exists
      or is missing.
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
  _write_json_file(batch_dir / _SNAPSHOT_NAME, source_snapshot_payload())
  report = {
      'batch': _BATCH_ID,
      'batch_dir': str(batch_dir),
      'created_utc': created,
      'models': {_MODEL_TAG: _MODEL_NAME},
      'protocol_document': _PROTOCOL_DOCUMENT,
      'protocol_sha256': _file_sha256(batch_dir / _PROTOCOL_NAME),
      'source_snapshot_sha256': _file_sha256(batch_dir / _SNAPSHOT_NAME),
      'protocol_document_sha256': _file_sha256(_REPO_ROOT / _PROTOCOL_DOCUMENT),
      'budgets': {
          'turns_per_run': _DEFAULT_TURNS,
          'attempts_per_run': _DEFAULT_ATTEMPTS,
          'sdk_create_cap_per_run': _DEFAULT_ATTEMPTS,
      },
      'runs': {},
      'counts': {
          'planned_runs': len(_RUN_IDS),
          'finished_runs': 0,
          'ledger_attempts': 0,
          'by_run': {},
      },
      'stop': None,
      'deviations': [],
  }
  _save_report(batch_dir, report)
  print(
      'frozen:'
      f" protocol={report['protocol_sha256'][:12]}"
      f" snapshot={report['source_snapshot_sha256'][:12]}"
      f" document={report['protocol_document_sha256'][:12]}"
  )


# -----------------------------------------------------------------------------
# Run stage (Task 3): production-story execution under ledger discipline.
#
# The stage replaces ``run_lib.make_language_model`` with a factory that
# wraps whatever production builds — the OpenAI-compatible GPT client
# live, the deterministic prompt stub offline — in the run's
# ``LedgerLanguageModel``, restoring the original module attribute in a
# ``finally`` block. Production stays byte-identical and keeps its whole
# retry chain; every logical model call books exactly one ledger slot.
#
# Failure mapping (protocol §5): in-story terminal results — including a
# semantic-review rejection recorded as ``reason='error'`` and a
# ``budget_limit`` stop (either budget layer: the SDK stop names its
# layer in the budget error) — are normal outcomes written to the report
# while the batch continues with the next run. Infrastructure faults —
# an exception from the production runner, a ledger budget error, an
# audit mismatch between the ledger's per-run count and the production
# attempt count — record ``report['stop']`` and exit the process with
# the evidence preserved. Runs already finished are backfilled from
# their summaries and never re-run. There is deliberately no resume
# path: an interrupted run — a bare checkpoint without a summary, or
# started-without-finish ledger slots — stops the run and pauses the
# batch, marked ``interrupted``, for user review (protocol §5: no
# auto-resume, no skipping pending slots, no re-sending; the S2 skip
# rule does not apply to uncommitted story turns).
# -----------------------------------------------------------------------------


# Loaded live settings: (api_key, api_base, secrets-to-redact).
_Settings = tuple[str, str, list[str]]


def _load_settings(
    env_path: pathlib.Path | str = _REPO_ROOT / '.env',
) -> _Settings:
  """Loads the live credentials and validates the frozen model identity.

  The offline path never calls this. The ``env_path`` parameter exists
  so tests can point the loader at a temporary file and never read the
  real repository ``.env``.

  Args:
    env_path: the environment file to read; defaults to the repository
      ``.env``.

  Returns:
    ``(api_key, api_base, secrets)`` where ``secrets`` lists the values
    the ledger must redact from every archived text.

  Raises:
    BatchStop: if the file cannot be read, ``AI_MODEL_4`` does not
      resolve to the frozen model name, or the API key is missing.
  """
  text = pathlib.Path(env_path).read_text(encoding='utf-8', errors='replace')
  values: dict[str, str] = {}
  for line in text.splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in stripped:
      continue
    key, _, value = stripped.partition('=')
    values[key.strip()] = value.strip().strip('"').strip("'")
  model = values.get(_ENV_KEY, '')
  if model != _MODEL_NAME:
    raise BatchStop(
        f'{_ENV_KEY} resolves to {model!r}, expected {_MODEL_NAME!r};'
        ' refusing to run the batch against a different model.'
    )
  api_key = values.get('AI_API_KEY', '')
  api_base = values.get('AI_BASE_URL', '')
  if not api_key:
    raise BatchStop('AI_API_KEY is missing from the environment file.')
  # The production OpenAI adapter reads its endpoint and key from the
  # environment; fill only what the process does not already provide.
  os.environ.setdefault('OPENAI_API_KEY', api_key)
  os.environ.setdefault('OPENAI_BASE_URL', api_base)
  return api_key, api_base, [api_key]


def _live_model_config(settings: _Settings) -> dict[str, Any]:
  """Builds the production model config from the loaded settings.

  Args:
    settings: the ``(api_key, api_base, secrets)`` tuple of
      ``_load_settings``.

  Returns:
    The model config addressing the frozen model through the production
    OpenAI-compatible adapter.
  """
  return {
      'api_type': 'openai',
      'model_name': _MODEL_NAME,
      'api_key': settings[0],
  }


def _run_decision(state_dir: pathlib.Path) -> str:
  """Classifies a run directory as finished, interrupted, or fresh.

  Args:
    state_dir: the run's state directory inside the batch.

  Returns:
    ``'finished'`` when a summary records a terminal reason (the run is
    never re-run), ``'interrupted'`` when a checkpoint exists without a
    summary (the run stopped mid-story; the batch pauses for review —
    there is no resume), ``'fresh'`` otherwise.

  Raises:
    BatchStop: if a summary exists but records a reason outside the
      terminal set: the batch stops for human review instead of
      guessing.
  """
  summary_path = state_dir / 'summary.json'
  if summary_path.exists():
    summary = json.loads(summary_path.read_text(encoding='utf-8'))
    reason = summary.get('reason')
    if reason not in _TERMINAL_REASONS:
      raise BatchStop(
          f'{state_dir.name}: summary records non-terminal reason'
          f' {reason!r}; refusing to continue the batch.'
      )
    return 'finished'
  if (state_dir / 'checkpoint.json').exists():
    return 'interrupted'
  return 'fresh'


def _report_entry_from_summary(
    run_id: str,
    status: str,
    summary: Mapping[str, Any],
    ledger: rl.RequestLedger,
) -> dict[str, Any]:
  """Builds one run's batch-report entry from its summary-shaped fields.

  A ``RunResult`` and its ``summary.json`` share the field names this
  entry reads, so both the just-executed and the backfilled paths use
  this one builder. ``ledger_attempts`` always comes from the ledger's
  rebuilt counts, never from the summary.

  Args:
    run_id: the diagnostic run (R1/R2) the entry describes.
    status: ``'fresh'``, ``'resumed'`` or ``'finished'``.
    summary: the run's summary fields (a ``RunResult`` asdict or the
      loaded ``summary.json``).
    ledger: the batch request ledger owning the attempt counts.

  Returns:
    The report entry with the protocol's fixed field set.
  """
  return {
      'run_id': run_id,
      'status': status,
      'reason': summary['reason'],
      'committed_turn_id': summary['committed_turn_id'],
      'used_model_attempts': summary['used_model_attempts'],
      'ledger_attempts': ledger.counts['by_group'].get(run_id, 0),
      'used_api_retries': summary['used_api_retries'],
      'scoring_parse_retries': summary['used_scoring_parse_retries'],
      'wall_time_seconds': summary['wall_time_seconds'],
      'budget_exhausted': summary['budget_exhausted'],
      'error_message': summary['error_message'],
  }


def _execute_run(
    run_id: str,
    state_dir: pathlib.Path,
    *,
    ledger: rl.RequestLedger,
    sdk_ledger: _SdkCreateLedger,
    offline: bool,
    settings: _Settings | None,
) -> tuple[dict[str, Any], run_lib.RunResult]:
  """Executes one fresh production run with the ledger model injected.

  ``run_lib.make_language_model`` is replaced by a factory wrapping the
  production-built provider (or offline stub) in the run's
  ``LedgerLanguageModel`` together with the batch's SDK ledger; the
  original attribute is restored in ``finally`` even when the run
  raises. The run config is built here and passed explicitly, so the
  frozen §3 director table travels with it — production defaults are
  never relied upon. After the run returns, the ledger's per-run count
  is audited against the production attempt count before anything is
  reported: a fresh run books no slots before it starts (a checkpoint-
  less run directory with prior slots for this run id is an anomaly
  that stops the batch), so its ledger count must equal the production
  guard's count exactly.

  Args:
    run_id: the diagnostic run (R1/R2) to execute.
    state_dir: the run's state directory inside the batch.
    ledger: the batch request ledger owning budget and archival.
    sdk_ledger: the second ledger layer whose per-run cap every SDK
      create pre-debits.
    offline: run the deterministic stub and book ``stub_request`` slots.
    settings: loaded live settings; unused when ``offline`` is True.

  Returns:
    ``(report_entry, result)``: the batch-report entry and the
    production ``RunResult``.

  Raises:
    BatchStop: if ledger slots for this run id exist before the run
      starts, or the ledger's per-run count and the production
      ``used_model_attempts`` disagree (audit anomaly; the batch stops
      with the evidence preserved).
  """
  prior = ledger.counts['by_group'].get(run_id, 0)
  if prior:
    raise BatchStop(
        f'{run_id}: {prior} ledger slot(s) exist without a run checkpoint'
        ' or summary; refusing to spend budget on an unanchored story.'
    )
  original = run_lib.make_language_model
  wrapped_models: list[LedgerLanguageModel] = []
  stage = 'stub_request' if offline else 'real_request'

  def factory(
      model_config: Mapping[str, Any] | None,
      *,
      disable_language_model: bool = False,
  ) -> tuple[language_model.LanguageModel, tuple[type[Exception], ...]]:
    provider, retryable = original(
        model_config, disable_language_model=disable_language_model
    )
    wrapped = LedgerLanguageModel(
        _MODEL_NAME,
        ledger=ledger,
        sdk_ledger=sdk_ledger,
        run_id=run_id,
        stage=stage,
        inner=provider,
    )
    wrapped_models.append(wrapped)
    return wrapped, retryable

  # Protocol §3: the run config — including the frozen director table —
  # is built by the runner and passed explicitly; the limit/mode/
  # action-mode parameters of ``run`` are ignored when a run config is
  # given, and the live model identity still arrives through
  # ``model_config`` (the README's standard call).
  run_config = run_lib.make_run_config(
      story_config.make_example_config(),
      total_turn_limit=_DEFAULT_TURNS,
      total_model_attempt_limit=_DEFAULT_ATTEMPTS,
      mode=_MODE,
      action_mode=_ACTION_MODE,
      model_config=None,
  )
  run_config['director_params'].update(_FROZEN_DIRECTOR_PARAMS)
  run_lib.make_language_model = factory
  try:
    started = time.time()
    result = run_lib.run(
        str(state_dir),
        disable_language_model=offline,
        model_config=(
            None
            if offline or settings is None
            else _live_model_config(settings)
        ),
        run_config=run_config,
    )
  finally:
    run_lib.make_language_model = original
    for wrapped in wrapped_models:
      wrapped.close()
  # Preserve the primary fault even when production's fail-fast
  # executor left another component call still finishing its ledger.
  _check_run_error(
      run_id, {'reason': result.reason, 'error_message': result.error_message}
  )
  ledger_after = ledger.counts['by_group'].get(run_id, 0)
  if ledger_after != result.used_model_attempts:
    raise BatchStop(
        f'ledger/production attempt mismatch for {run_id}: ledger'
        f' {ledger_after} != production {result.used_model_attempts}.'
    )
  entry = _report_entry_from_summary(
      run_id,
      'fresh',
      dataclasses.asdict(result),
      ledger,
  )
  entry['ledger_attempts'] = ledger_after
  entry['wall_time_seconds'] = time.time() - started
  return entry, result


def _refresh_counts(report: dict[str, Any], ledger: rl.RequestLedger) -> None:
  """Refreshes the report's counts from the ledger alone.

  Mirrors the S2 probe shape: every attempt count derives from the
  ledger's rebuilt counts (never from ``summary.json`` or caller
  numbers), while ``finished_runs`` counts the report entries whose
  recorded reason is terminal.

  Args:
    report: the batch report to update in place.
    ledger: the batch request ledger owning the attempt counts.
  """
  counts = ledger.counts
  report['counts'] = {
      'planned_runs': len(_RUN_IDS),
      'finished_runs': sum(
          1
          for entry in report['runs'].values()
          if entry.get('reason') in _TERMINAL_REASONS
      ),
      'ledger_attempts': counts['global'],
      'by_run': {
          run_id: counts['by_group'].get(run_id, 0) for run_id in _RUN_IDS
      },
  }


def _check_run_error(run_id: str, entry: Mapping[str, Any]) -> None:
  """Stops on faults that production converted into an error result."""
  error = entry.get('error_message') or ''
  if entry.get('reason') != story_terminator.REASON_ERROR and not error:
    return
  if error.startswith('StoryOutputError: Semantic review rejected output:'):
    return
  raise BatchStop(
      f'{run_id}: non-semantic run fault: {error or "missing error evidence"}'
  )


def run_stage(
    batch_dir: pathlib.Path | str,
    *,
    offline: bool = False,
    runs: Sequence[str] = _RUN_IDS,
) -> None:
  """Executes the frozen protocol's runs under ledger discipline.

  Re-verifies the frozen artifacts, rebuilds both ledgers from disk,
  then executes each requested run: finished runs are backfilled from
  their summaries without re-running (idempotent re-entry), fresh runs
  execute through the injected ``LedgerLanguageModel``. After every run
  the counts refresh from the ledger and the report is saved atomically.
  Exceptions from the production runner, ledger budget errors and audit
  mismatches record ``report['stop']`` and exit the process with the
  evidence preserved, while in-story terminal results — including a
  semantic-review rejection recorded as ``reason='error'`` — are normal
  outcomes and the batch continues.

  There is deliberately no resume and no skip authorization (protocol
  §5): an interrupted run — a bare checkpoint without a summary, or
  started-without-finish ledger slots — stops the run and pauses the
  batch, marked ``interrupted``, for user review. Interrupted evidence
  is never resent, refunded, skipped or re-generated, and the S2 skip
  rule does not apply to uncommitted story turns.

  Args:
    batch_dir: the frozen batch directory.
    offline: execute with the deterministic stub, booking
      ``stub_request`` slots and reading no credentials.
    runs: the subset of run ids to execute (default both, in order).

  Raises:
    SystemExit: on any batch-stopping condition (an already-stopped
      report, interruption evidence, or a run-stage fault recorded in
      ``report['stop']``).
    BatchStop: on frozen-artifact drift or unknown run ids.
  """
  batch_dir = pathlib.Path(batch_dir)
  unknown = [run_id for run_id in runs if run_id not in _RUN_IDS]
  if unknown:
    raise BatchStop(f'unknown run ids: {unknown}; expected {list(_RUN_IDS)}.')
  settings = None if offline else _load_settings()
  secrets = () if settings is None else tuple(settings[2])
  _verify_frozen(batch_dir)
  ledger = rl.RequestLedger(
      batch_dir, batch_id=_BATCH_ID, secrets=secrets, **ledger_caps()
  )

  def audit_before_request() -> None:
    # Both writers use the SDK ledger's shared bookkeeping lock, so
    # audits never observe a half-written sidecar/started pair. Pending
    # slots here are in-flight calls, not a reason to resend or refund.
    _verify_frozen(batch_dir)
    for directory, expected in (
        (batch_dir, ledger.counts),
        (batch_dir / _SDK_LEDGER_DIRNAME, sdk_ledger.counts),
    ):
      path = directory / _LEDGER_NAME
      events = rl.load_events(path) if path.exists() else []
      rl.audit_events(events, raw_dir=directory / _RAW_DIRNAME)
      if rl.rebuild_counts(events) != expected:
        raise rl.LedgerAuditError(
            f'ledger changed outside writer: {directory.name}'
        )

  sdk_ledger = _SdkCreateLedger(
      batch_dir, secrets=secrets, before_request=audit_before_request
  )
  report = _load_report(batch_dir)
  if report.get('stop'):
    raise SystemExit(
        f"batch already stopped at {report['stop'].get('utc')};"
        ' only offline summarization is allowed.'
    )
  pending = ledger.pending_slots()
  sdk_counts = sdk_ledger.counts
  pending.extend(
      slot
      for slot in sdk_counts['started_slots']
      if slot not in set(sdk_counts['finished_slots'])
  )
  if pending:
    # Interruption evidence (protocol §5): started-without-finish slots
    # are reserved budget from a dead process. The batch pauses for user
    # review — nothing is resent, refunded, skipped or re-generated.
    report['stop'] = {
        'utc': _utc_now(),
        'run': None,
        'error': (
            'run interrupted: started-without-finish ledger slots'
            f' {pending}; batch paused for user review (no auto-resume,'
            ' no skip).'
        ),
    }
    _save_report(batch_dir, report)
    raise SystemExit(
        'pending ledger slots refuse continuation; batch paused for'
        f' user review: {pending}'
    )
  for run_id in runs:
    state_dir = batch_dir / run_id
    try:
      sdk_ledger.check_before_request()
      decision = _run_decision(state_dir)
      if decision == 'finished':
        # Idempotent re-entry: backfill from the summary, never re-run.
        summary = json.loads(
            (state_dir / 'summary.json').read_text(encoding='utf-8')
        )
        report['runs'][run_id] = _report_entry_from_summary(
            run_id, 'finished', summary, ledger
        )
      elif decision == 'interrupted':
        # Protocol §5: a bare checkpoint is a mid-story interruption.
        # Mark the run, pause the batch, hand the scene to the user.
        report['runs'][run_id] = {'run_id': run_id, 'status': 'interrupted'}
        raise BatchStop(
            f'run {run_id} interrupted mid-story (checkpoint without a'
            ' summary); batch paused for user review — no auto-resume.'
        )
      else:
        entry, _ = _execute_run(
            run_id,
            state_dir,
            ledger=ledger,
            sdk_ledger=sdk_ledger,
            offline=offline,
            settings=settings,
        )
        report['runs'][run_id] = entry
      # run.run() catches provider and script exceptions too. Its
      # reason='error' is not proof of a semantic review rejection.
      _check_run_error(run_id, report['runs'][run_id])
    except Exception as error:  # pylint: disable=broad-except
      # Infrastructure fault (protocol §5): record the stop, keep the
      # evidence and exit non-zero. In-story terminal reasons never
      # reach this path — they are results, not exceptions.
      report['stop'] = {
          'utc': _utc_now(),
          'run': run_id,
          'error': f'{type(error).__name__}: {error}',
      }
      _refresh_counts(report, ledger)
      _save_report(batch_dir, report)
      raise SystemExit(f'batch stopped at run {run_id}: {error}') from error
    _refresh_counts(report, ledger)
    _save_report(batch_dir, report)
  print(f"run stage complete: {report['counts']}")


# -----------------------------------------------------------------------------
# Summarize stage (Task 4): offline cross-check and disclosure.
#
# Fully offline like the freeze stage: no credentials are read, no client
# is built, no request leaves the process. Every integrity number is
# rebuilt from the request ledgers' events alone (never restated from
# the report), each executed run's production summary is cross-checked
# against the Concordia ledger's rebuilt per-run count, and the
# disclosed scope is explicit: the two ledgers count Concordia-layer
# attempts and SDK ``create`` calls separately — associated per call,
# never summed — while the SDK's HTTP retries stay below both ledgers,
# so actual HTTP requests can be more; pending slots (started without
# finished) are reserved-but-unobserved budget, listed as they are.
#
# Diagnostic semantics (protocol §6 conclusion semantics): the summary
# answers "where is the obstacle" and never declares acceptance — no
# pass/fail field exists anywhere in the output. Any integrity failure
# clears a previously saved summary before raising, so a stale summary
# can never survive a failed audit.
# -----------------------------------------------------------------------------


def _dependency_drift(
    reference_snapshot: pathlib.Path | None,
) -> list[dict[str, str]] | str:
  """Lists intersection files whose hash differs from the reference.

  Same shape as the S2 probe's comparison: only files present in both
  the reference snapshot and a freshly computed
  ``source_snapshot_payload()`` are compared, so files added or removed
  since the reference never appear in the drift list.

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
  """Summarizes the batch offline; never builds a client or a call.

  Re-verifies the frozen artifacts, loads and audits both request
  ledgers' events, then rebuilds the integrity block from the events
  alone: total and per-run ledger attempts, pending slots (started
  without finished — reserved budget that was never observed) of both
  the Concordia and the SDK layer, the recorded deviations and stop.
  Each run's production ``summary.json`` is cross-checked against the
  Concordia ledger's rebuilt per-run count — a mismatch is an audit
  failure — and a run without a summary (never executed) is recorded as
  ``no_summary``, not an error. An optional reference snapshot adds the
  dependency-drift disclosure.

  Any integrity failure (frozen-artifact drift, a ledger structural
  violation, a cross-check mismatch, unreadable files) clears the
  report's saved summary, records ``summary_error`` and raises
  ``LedgerAuditError`` — a stale summary never survives a failed audit.
  There is deliberately no pass/fail field: per the protocol's
  conclusion semantics, the summary only answers "where is the
  obstacle", it never declares acceptance.

  Args:
    batch_dir: the frozen batch directory.
    reference_snapshot: optional reference ``source_snapshot.json``
      compared by the dependency-drift disclosure.

  Returns:
    The summary block written into the report.

  Raises:
    LedgerAuditError: on any integrity failure, after clearing the
      report's saved summary.
  """
  batch_dir = pathlib.Path(batch_dir)
  report = _load_report(batch_dir)
  ledger_path = batch_dir / _LEDGER_NAME
  try:
    _verify_frozen(batch_dir)
    events = rl.load_events(ledger_path) if ledger_path.exists() else []
    rl.audit_events(events, raw_dir=batch_dir / _RAW_DIRNAME)
    counts = rl.rebuild_counts(events)
    sdk_events_path = batch_dir / _SDK_LEDGER_DIRNAME / _LEDGER_NAME
    sdk_events = (
        rl.load_events(sdk_events_path) if sdk_events_path.exists() else []
    )
    rl.audit_events(
        sdk_events, raw_dir=batch_dir / _SDK_LEDGER_DIRNAME / _RAW_DIRNAME
    )
    sdk_counts = rl.rebuild_counts(sdk_events)
    runs: dict[str, Any] = {}
    for run_id in _RUN_IDS:
      summary_path = batch_dir / run_id / 'summary.json'
      if not summary_path.exists():
        runs[run_id] = {'status': 'no_summary'}
        continue
      summary = json.loads(summary_path.read_text(encoding='utf-8'))
      ledger_attempts = counts['by_group'].get(run_id, 0)
      production_attempts = summary['used_model_attempts']
      if ledger_attempts != production_attempts:
        raise rl.LedgerAuditError(
            f'{run_id}: ledger/production attempt mismatch: ledger'
            f' {ledger_attempts} != production {production_attempts}.'
        )
      runs[run_id] = {
          'reason': summary['reason'],
          'committed_turn_id': summary['committed_turn_id'],
          'ledger_attempts': ledger_attempts,
          'production_attempts': production_attempts,
          'used_api_retries': summary['used_api_retries'],
          'scoring_parse_retries': summary['used_scoring_parse_retries'],
          'wall_time_seconds': summary['wall_time_seconds'],
      }
  except (
      BatchStop,
      rl.LedgerAuditError,
      KeyError,
      OSError,
      ValueError,
  ) as error:
    # A previously saved summary must not survive a failed audit.
    report['summary'] = None
    report['summary_error'] = f'Integrity audit failed: {error}'
    _save_report(batch_dir, report)
    raise rl.LedgerAuditError(str(error)) from error
  finished = set(counts['finished_slots'])
  sdk_finished = set(sdk_counts['finished_slots'])
  report['summary'] = {
      'integrity': {
          'ledger_attempts': counts['global'],
          'by_run': {
              run_id: counts['by_group'].get(run_id, 0) for run_id in _RUN_IDS
          },
          'pending_slots': [
              slot for slot in counts['started_slots'] if slot not in finished
          ],
          'sdk_ledger': {
              'ledger_dir': _SDK_LEDGER_DIRNAME,
              'ledger_attempts': sdk_counts['global'],
              'by_run': {
                  run_id: sdk_counts['by_group'].get(run_id, 0)
                  for run_id in _RUN_IDS
              },
              'pending_slots': [
                  slot
                  for slot in sdk_counts['started_slots']
                  if slot not in sdk_finished
              ],
              'cap_per_run': _DEFAULT_ATTEMPTS,
          },
          'deviations': report['deviations'],
          'stop': report.get('stop'),
      },
      'runs': runs,
      'dependency_drift_vs_reference': _dependency_drift(reference_snapshot),
      'disclosures': [
          (
              'Concordia attempts 与 SDK create 由两层账本的 started 分别'
              '重建：记录关联（attempt_id）、不混加、不互相代替。'
          ),
          (
              'SDK max_retries 的 HTTP 重试在两层账本之下，实际 HTTP 请求'
              '数可更多；SDK 额度停止属实验边界，不冒称 Concordia 400 已'
              '耗尽。'
          ),
          'pending（started 无 finished）已预扣未观测，照实列出。',
      ],
  }
  # A fresh summary supersedes any error recorded by an earlier audit.
  report.pop('summary_error', None)
  _save_report(batch_dir, report)
  print(json.dumps(report['summary'], ensure_ascii=False, indent=2))
  return report['summary']


# -----------------------------------------------------------------------------
# Dry-run stage (Task 5): the whole pipeline offline, invariants asserted.
#
# The dry run proves the batch wiring end to end with zero network: the
# freeze stage captures the frozen artifacts into ``dry_run_dir`` (which
# therefore doubles as the batch directory), the run stage executes both
# runs through the offline stub path of the production runner, and the
# summarize stage cross-checks both ledgers. The invariants are then
# collected from the artifacts alone — never from closed-over runtime
# objects — and every one of them is recorded pass/fail in
# ``dry_run_report.json`` before a failed dry run exits, so the evidence
# always survives. They cover the frozen §3 configuration (the
# checkpointed director_params must equal the frozen table — the
# explicit run config, not a production default, was in effect), both
# budget layers (Concordia and SDK caps), the archived prompts, the
# stub-only request stages, and the zero-SDK-call property of the
# offline path. The dry run is a wiring diagnostic: per the protocol's
# conclusion semantics it asserts nothing about story quality and no
# acceptance gate exists.
# -----------------------------------------------------------------------------

_DRY_RUN_REPORT_NAME = 'dry_run_report.json'


def _frozen_param_mismatches(dry_run_dir: pathlib.Path) -> list[str]:
  """Compares each run's checkpointed director_params to the §3 table.

  Args:
    dry_run_dir: the dry-run batch directory holding the run state dirs.

  Returns:
    One human-readable entry per run whose checkpoint is missing or
    whose effective ``director_params`` disagree with
    ``_FROZEN_DIRECTOR_PARAMS`` on any frozen key; empty when every
    checkpoint matches.
  """
  mismatches = []
  for run_id in _RUN_IDS:
    checkpoint = dry_run_dir / run_id / 'checkpoint.json'
    if not checkpoint.exists():
      mismatches.append(f'{run_id}: checkpoint artifact missing')
      continue
    envelope = json.loads(checkpoint.read_text(encoding='utf-8'))
    effective = envelope['run_config']['director_params']
    mismatches.extend(
        f'{run_id}: {key}={effective.get(key)!r} != frozen {value!r}'
        for key, value in _FROZEN_DIRECTOR_PARAMS.items()
        if effective.get(key) != value
    )
  return mismatches


def dry_run_stage(dry_run_dir: pathlib.Path | str) -> None:
  """Runs the full batch offline and asserts the batch invariants.

  Freezes a fresh batch into ``dry_run_dir``, executes both runs
  through ``run_stage(offline=True)`` — the production pipeline over
  the deterministic stub: no credentials are read, no client is built,
  no request leaves the process — and cross-checks the result with
  ``summarize_stage``. Each recorded invariant (one pass/fail entry in
  ``dry_run_report.json``): both runs ended in a terminal reason; each
  run's ledger count equals the production attempt count; every
  started ledger slot has its archived prompt on disk; the global
  ledger cap holds; every started slot is an offline ``stub_request``,
  proving the wrapped inner model was the production stub; each run's
  checkpointed ``director_params`` equal the frozen §3 table; and the
  SDK ledger stays within its caps — at zero offline, since the stub
  exposes no SDK client.

  Args:
    dry_run_dir: directory for the dry-run batch and report. A
      populated directory is refused — a dry run is never reused or
      partially repeated; an existing empty directory is accepted (the
      freeze stage recreates it fresh).

  Raises:
    BatchStop: if the directory already exists with contents.
    SystemExit: after writing the report, if any invariant failed.
  """
  dry_run_dir = pathlib.Path(dry_run_dir)
  if dry_run_dir.exists():
    if any(dry_run_dir.iterdir()):
      raise BatchStop(
          f'refusing to reuse a non-empty dry run directory: {dry_run_dir}'
      )
    # An empty directory carries no state; the freeze stage refuses
    # existing directories, so hand it a fresh path instead.
    dry_run_dir.rmdir()
  freeze_stage(dry_run_dir)
  run_stage(dry_run_dir, offline=True)
  summary = summarize_stage(dry_run_dir)
  events = rl.load_events(dry_run_dir / _LEDGER_NAME)
  counts = rl.rebuild_counts(events)
  started = [event for event in events if event['event'] == 'started']
  stages = sorted({str(event.get('stage')) for event in started})
  missing_prompts = [
      slot
      for slot in counts['started_slots']
      if not (dry_run_dir / _RAW_DIRNAME / f'{slot}.prompt.txt').exists()
  ]
  sdk_events_path = dry_run_dir / _SDK_LEDGER_DIRNAME / _LEDGER_NAME
  sdk_counts = rl.rebuild_counts(
      rl.load_events(sdk_events_path) if sdk_events_path.exists() else []
  )
  sdk_by_run = {
      run_id: sdk_counts['by_group'].get(run_id, 0) for run_id in _RUN_IDS
  }
  sdk_caps = ledger_caps()
  runs = {run_id: summary['runs'].get(run_id, {}) for run_id in _RUN_IDS}
  reasons = [entry.get('reason') for entry in runs.values()]
  attempt_pairs = [
      (entry.get('ledger_attempts'), entry.get('production_attempts'))
      for entry in runs.values()
  ]
  global_cap = ledger_caps()['global_cap']
  param_mismatches = _frozen_param_mismatches(dry_run_dir)
  sdk_within_caps = sdk_counts['global'] <= sdk_caps['global_cap'] and all(
      count <= sdk_caps['group_caps'][run_id]
      for run_id, count in sdk_by_run.items()
  )
  checks = [
      (
          'both_runs_terminal',
          all(reason in _TERMINAL_REASONS for reason in reasons),
          f'run reasons: {reasons}',
      ),
      (
          'ledger_matches_production_per_run',
          all(pair[0] == pair[1] for pair in attempt_pairs),
          f'(ledger, production) per run: {attempt_pairs}',
      ),
      (
          'director_params_match_freeze',
          not param_mismatches,
          '; '.join(param_mismatches)
          or f'checkpointed params equal the frozen table {_FROZEN_DIRECTOR_PARAMS}',
      ),
      (
          'every_started_slot_prompt_archived',
          not missing_prompts,
          f'started slots without an archived prompt: {missing_prompts}',
      ),
      (
          'ledger_caps_not_exceeded',
          counts['global'] <= global_cap,
          f"ledger attempts {counts['global']} > global cap {global_cap}"
          if counts['global'] > global_cap
          else f"ledger attempts {counts['global']} <= cap {global_cap}",
      ),
      (
          'sdk_ledger_within_caps',
          sdk_within_caps,
          f'sdk create counts {sdk_by_run} within caps {sdk_caps["group_caps"]}'
          if sdk_within_caps
          else (
              f'sdk create counts {sdk_by_run} exceed caps'
              f' {sdk_caps["group_caps"]}'
          ),
      ),
      (
          'offline_sdk_zero_creates',
          sdk_counts['global'] == 0,
          f'offline stub has no SDK client; sdk create count:'
          f" {sdk_counts['global']}",
      ),
      (
          'offline_stub_requests_only',
          stages == ['stub_request'],
          f"started-slot stages (expected ['stub_request']): {stages}",
      ),
  ]
  findings = [detail for _, passed, detail in checks if not passed]
  report = {
      'batch': _BATCH_ID,
      'created_utc': _utc_now(),
      'offline': True,
      'ok': not findings,
      'runs': {
          run_id: {
              'reason': entry.get('reason'),
              'ledger_attempts': entry.get('ledger_attempts', 0),
              'production_attempts': entry.get('production_attempts', 0),
          }
          for run_id, entry in runs.items()
      },
      'ledger': {
          'attempts': counts['global'],
          'global_cap': global_cap,
          'started_slots': len(counts['started_slots']),
          'missing_prompts': missing_prompts,
      },
      'sdk': {
          'ledger_dir': _SDK_LEDGER_DIRNAME,
          'attempts': sdk_counts['global'],
          'by_run': sdk_by_run,
          'cap_per_run': sdk_caps['group_caps']['R1'],
      },
      'invariants': [
          {'check': name, 'passed': passed, 'detail': detail}
          for name, passed, detail in checks
      ],
      'findings': findings,
      'conclusion': (
          '诊断无验收门：dry run 只证明布线与账本纪律，不宣布 MVP 质量'
          '验收；真实 freeze/run 需按协议 §8 另获授权。'
      ),
  }
  _write_json_file(dry_run_dir / _DRY_RUN_REPORT_NAME, report)
  print(f"dry run: ok={report['ok']}, findings: {len(findings)}")
  for finding in findings:
    print(f'  - {finding}')
  if findings:
    raise SystemExit(f'dry run finished with {len(findings)} findings')


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
    '/tmp/opencode/s3_diagnostic_story_dry_run',
    'Where the offline dry run freezes and executes its batch.',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'reference_snapshot',
    '',
    'Optional reference source snapshot compared by the summarize'
    ' stage (authorized round only; the control round froze'
    ' run_results/story_enactment/full_story_control/'
    '20261019T032409Z/source_snapshot.json).',
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'runs',
    'R1,R2',
    'Comma-separated subset of run ids for the run stage.',
    flag_values=FLAGS,
)


def _parse_flags(argv: list[str]) -> list[str]:
  """Parses this runner's private flags (absl app flags_parser hook)."""
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
        runs=[
            run_id.strip() for run_id in FLAGS.runs.split(',') if run_id.strip()
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
