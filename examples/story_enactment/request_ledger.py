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

"""Pure parsing, auditing and rebuilding of the append-only request ledger.

The append-only JSONL request ledger is the single source of truth for
request counting (design §4 of
``docs/superpowers/specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md``):
a persisted ``started`` record immediately reserves one budget unit —
including API errors, timeouts and no-result calls — while ``finished``
only observes the terminal state and never adds budget. Counts are
rebuilt from the recorded model/group/sample fields, never from volatile
runtime state. Offline tools therefore re-derive budgets, resume
decisions and reports from this file alone.

This module holds the pure offline functions — loading ledger lines,
auditing them for structural violations (unknown event types, duplicate
or inconsistent ``started`` records, ``finished`` without ``started``,
and sidecar mismatches) and rebuilding counts — plus the
:class:`RequestLedger` writer that spends budget through the same
contract. Pending slots (started without finished) are legal in-flight
or crashed states and are reported, never rejected. Nothing here reads
credentials, environment variables or the network.
"""

from collections.abc import Mapping, Sequence
import copy
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

KNOWN_EVENT_TYPES = frozenset({
    'started',
    'finished',
    'frozen_stub',
    'skipped',
    'second_request_blocked',
    'budget_blocked',
})
_REQUIRED_STARTED_FIELDS = (
    'slot_id',
    'sample_id',
    'group',
    'model_tag',
    'model_name',
)
_SIDECAR_SUFFIX = '.started.json'


class LedgerAuditError(Exception):
  """A ledger structural violation that forbids further paid requests."""


class LedgerBudgetError(Exception):
  """A rebuilt budget would exceed its configured cap."""


def load_events(path: Path) -> list[dict[str, Any]]:
  """Loads a JSONL ledger file into a list of event dicts.

  Args:
    path: ledger file written as one JSON object per line.

  Returns:
    The parsed events, in file order, with blank lines skipped.

  Raises:
    LedgerAuditError: if any line is truncated or invalid JSON; the
      message carries the 1-based line number.
  """
  events = []
  with path.open(encoding='utf-8', errors='replace') as handle:
    for number, line in enumerate(handle, start=1):
      if not line.strip():
        continue
      try:
        events.append(json.loads(line))
      except json.JSONDecodeError as error:
        raise LedgerAuditError(
            f'ledger line {number} is truncated or invalid JSON: {error}'
        ) from error
  return events


def audit_events(
    events: Sequence[Mapping[str, Any]],
    *,
    raw_dir: Path | None = None,
    allow_missing_failure_prompts: bool = False,
) -> dict[str, list[str]]:
  """Audits ledger events for structural violations.

  Args:
    events: ledger events in append order.
    raw_dir: optional directory of ``<slot_id>.started.json`` sidecar
      files to cross-check against the ``started`` events.
    allow_missing_failure_prompts: offline historical closeout only:
      report missing prompts for pending or second-request-blocked
      slots as warnings. Writers and paid resume use the strict default.

  Returns:
    ``{'pending_slots': [...], 'warnings': [...]}`` where pending slots
    are started-but-not-finished slots (a legal state, reported only)
    in first-seen order.

  Raises:
    LedgerAuditError: on an unknown event type, a missing required
      ``started`` field, a duplicate ``started`` for one slot, a
      ``finished`` or ``second_request_blocked`` without a prior
      ``started``, a slot started under two different ``model_name``
      values, or — when ``raw_dir`` is given — a ``started`` event
      lacking its sidecar file, an orphan or inconsistent sidecar,
      or a missing or changed request prompt.
  """
  started_model_names: dict[str, Any] = {}
  started_events: dict[str, Mapping[str, Any]] = {}
  warnings: list[str] = []
  finished_slots = set()
  blocked_slots = set()
  for index, event in enumerate(events, start=1):
    event_type = event.get('event')
    if event_type not in KNOWN_EVENT_TYPES:
      raise LedgerAuditError(
          f'ledger event {index} has unknown event type {event_type!r}'
      )
    slot_id = event.get('slot_id')
    if not isinstance(slot_id, str):
      raise LedgerAuditError(
          f'ledger event {index} has no slot_id: {dict(event)!r}'
      )
    if event_type == 'started':
      missing = [
          field
          for field in _REQUIRED_STARTED_FIELDS
          if event.get(field) is None
      ]
      if missing:
        raise LedgerAuditError(
            f'started event {index} for slot {slot_id!r} is missing required'
            f' fields: {", ".join(missing)}'
        )
      model_name = event.get('model_name')
      if slot_id in started_model_names:
        if started_model_names[slot_id] != model_name:
          raise LedgerAuditError(
              f'slot {slot_id} was started under two different model_name'
              f' values: {started_model_names[slot_id]!r} and'
              f' {model_name!r}'
          )
        raise LedgerAuditError(f'duplicate started event for slot {slot_id}')
      started_model_names[slot_id] = model_name
      started_events[slot_id] = event
    elif event_type in ('finished', 'second_request_blocked'):
      if slot_id not in started_model_names:
        raise LedgerAuditError(
            f'{event_type} event {index} for slot {slot_id!r} has no prior'
            ' started event'
        )
      if event_type == 'second_request_blocked':
        blocked_slots.add(slot_id)
      if event_type == 'finished':
        if slot_id in finished_slots:
          raise LedgerAuditError(f'duplicate finished event for slot {slot_id}')
        for field in ('sample_id', 'group', 'model_tag', 'model_name'):
          if field in event and event[field] != started_events[slot_id][field]:
            raise LedgerAuditError(
                f'finished {field} conflicts with started for slot {slot_id}'
            )
        finished_slots.add(slot_id)
  if raw_dir is not None:
    sidecar_slots = {
        path.name.removesuffix(_SIDECAR_SUFFIX)
        for path in raw_dir.glob(f'*{_SIDECAR_SUFFIX}')
    }
    missing_sidecars = [
        slot for slot in started_model_names if slot not in sidecar_slots
    ]
    if missing_sidecars:
      raise LedgerAuditError(
          'started slots have no raw sidecar file'
          f' {_SIDECAR_SUFFIX}: {", ".join(missing_sidecars)}'
      )
    orphan_sidecars = sorted(sidecar_slots - set(started_model_names))
    if orphan_sidecars:
      raise LedgerAuditError(
          'orphan raw sidecar files with no started event:'
          f' {", ".join(orphan_sidecars)}'
      )
    for slot_id, event in started_events.items():
      try:
        sidecar = json.loads(
            (raw_dir / f'{slot_id}{_SIDECAR_SUFFIX}').read_text(
                encoding='utf-8', errors='replace'
            )
        )
      except (OSError, json.JSONDecodeError) as error:
        raise LedgerAuditError(
            f'invalid sidecar for {slot_id}: {error}'
        ) from error
      if sidecar != {
          key: value for key, value in event.items() if key != 'event'
      }:
        raise LedgerAuditError(f'started sidecar mismatch for slot {slot_id}')
      prompt_path = raw_dir / f'{slot_id}.prompt.txt'
      if (
          allow_missing_failure_prompts
          and (slot_id not in finished_slots or slot_id in blocked_slots)
          and not prompt_path.exists()
      ):
        warnings.append(
            f'historical failed/pending slot has no archived prompt: {slot_id}'
        )
        continue
      if not prompt_path.is_file() or hashlib.sha256(
          prompt_path.read_bytes()
      ).hexdigest() != event.get('prompt_sha256'):
        raise LedgerAuditError(f'missing or changed prompt for slot {slot_id}')
  pending_slots = [
      slot for slot in started_model_names if slot not in finished_slots
  ]
  return {'pending_slots': pending_slots, 'warnings': warnings}


def rebuild_counts(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
  """Rebuilds request counts and slot inventories from ledger events.

  Only unique ``started`` slots consume budget: ``finished`` never adds
  a unit, and ``frozen_stub``, ``skipped``, ``second_request_blocked``
  and ``budget_blocked`` events consume nothing.

  Args:
    events: ledger events in append order.

  Returns:
    A dict with ``global`` (int), ``by_model``, ``by_group``,
    ``by_sample`` (keys ``'sample_id__model_tag'``), ``started_slots``,
    ``finished_slots``, ``frozen_slots``, ``skipped_slots`` (each in
    first-seen order) and ``second_request_blocks`` (int).
  """
  by_model: dict[str, int] = {}
  by_group: dict[str, int] = {}
  by_sample: dict[str, int] = {}
  started_slots: list[str] = []
  finished_slots: list[str] = []
  frozen_slots: list[str] = []
  skipped_slots: list[str] = []
  second_request_blocks = 0
  seen_started: set[str] = set()
  seen_finished: set[str] = set()
  for event in events:
    event_type = event.get('event')
    slot_id: Any = event.get('slot_id')
    if event_type == 'started':
      if slot_id in seen_started:
        continue
      seen_started.add(slot_id)
      started_slots.append(slot_id)
      model_tag = str(event.get('model_tag'))
      group = str(event.get('group'))
      sample_key = f"{event.get('sample_id')}__{model_tag}"
      by_model[model_tag] = by_model.get(model_tag, 0) + 1
      by_group[group] = by_group.get(group, 0) + 1
      by_sample[sample_key] = by_sample.get(sample_key, 0) + 1
    elif event_type == 'finished':
      if slot_id not in seen_finished:
        seen_finished.add(slot_id)
        finished_slots.append(slot_id)
    elif event_type == 'frozen_stub':
      frozen_slots.append(slot_id)
    elif event_type == 'skipped':
      skipped_slots.append(slot_id)
    elif event_type == 'second_request_blocked':
      second_request_blocks += 1
  return {
      'global': len(started_slots),
      'by_model': by_model,
      'by_group': by_group,
      'by_sample': by_sample,
      'started_slots': started_slots,
      'finished_slots': finished_slots,
      'frozen_slots': frozen_slots,
      'skipped_slots': skipped_slots,
      'second_request_blocks': second_request_blocks,
  }


_FROZEN_STUB_NOTE = (
    'Generation answered from the frozen single-field narration; zero'
    ' real requests.'
)
_SECOND_REQUEST_BLOCKED_NOTE = (
    'The production sample_choice loop attempted a second request in'
    ' this slot; it was blocked before the provider. The first raw'
    ' answer follows.'
)


def _utc_now() -> str:
  """Returns the current UTC time in the batch timestamp format."""
  return datetime.datetime.now(
      datetime.timezone.utc,
  ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _sha256_text(text: str) -> str:
  """Returns the hex SHA-256 of a UTF-8 string."""
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _write_text_file(path: Path, text: str) -> None:
  """Writes a UTF-8 text file."""
  path.write_text(text, encoding='utf-8')


def _write_json_file(path: Path, obj: Mapping[str, Any]) -> None:
  """Writes indented JSON with a trailing newline, as the batch does."""
  path.write_text(
      json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
  )


class RequestLedger:
  """Append-only ledger writer that reserves budget at ``started``.

  The writer is the single point through which a runner spends budget
  (design §4.1): it checks all four cap layers — global, model, group
  and per-``(sample_id, model_tag)`` — before any provider call, then
  persists the prompt and the ``started`` marker, so a persisted
  ``started`` immediately reserves one unit even if the process dies
  before the SDK returns. ``finished`` only observes the terminal state
  and evidence-only records (``frozen_stub``, ``skipped``,
  ``second_request_blocked``, ``budget_blocked``) never change counts.
  On construction an existing ledger is re-audited and counts are
  rebuilt from the ledger file alone — never from ``report.json`` or
  caller-supplied numbers.
  """

  def __init__(
      self,
      batch_dir: Path | str,
      *,
      batch_id: str,
      global_cap: int,
      model_caps: Mapping[str, int],
      group_caps: Mapping[str, int],
      sample_caps: Mapping[str, int],
      secrets: Sequence[str] = (),
  ):
    """Prepares the ledger, rebuilding counts if one already exists.

    Args:
      batch_dir: directory holding ``requests.jsonl`` and ``raw/``.
      batch_id: identifier of the batch this ledger belongs to.
      global_cap: cap on total started requests.
      model_caps: cap per ``model_tag``; a tag without an entry has no
        model-level cap.
      group_caps: cap per ``group``; a group without an entry has no
        group-level cap.
      sample_caps: cap per ``(sample_id, model_tag)`` pair, keyed by
        the sample's group (caps are uniform within a group, as in the
        2026-10-20 batch where REV pairs were capped at 1 and ACT pairs
        at 4).
      secrets: literal values replaced with ``[REDACTED]`` in every
        string written to disk.

    Raises:
      LedgerAuditError: if the ledger or its evidence fails the structural
        audit, including orphan markers when the ledger is missing.
    """
    self._batch_id = batch_id
    self._global_cap = global_cap
    self._model_caps = dict(model_caps)
    self._group_caps = dict(group_caps)
    self._sample_caps = dict(sample_caps)
    self._secrets = tuple(secrets)
    self._batch_dir = Path(batch_dir)
    self._ledger_path = self._batch_dir / 'requests.jsonl'
    self._raw_dir = self._batch_dir / 'raw'
    self._started_info: dict[str, dict[str, str]] = {}
    events = (
        load_events(self._ledger_path) if self._ledger_path.exists() else []
    )
    audit_events(events, raw_dir=self._raw_dir)
    self._counts = rebuild_counts(events)
    for event in events:
      if event.get('event') == 'started':
        self._started_info[event['slot_id']] = {
            'sample_id': event['sample_id'],
            'group': event['group'],
            'model_tag': event['model_tag'],
        }
    self._raw_dir.mkdir(parents=True, exist_ok=True)

  @property
  def counts(self) -> dict[str, Any]:
    """A snapshot of the current counts, shaped like ``rebuild_counts``."""
    return copy.deepcopy(self._counts)

  def pending_slots(self) -> list[str]:
    """Returns started-but-not-finished slots, in first-started order."""
    finished = set(self._counts['finished_slots'])
    return [
        slot for slot in self._counts['started_slots'] if slot not in finished
    ]

  def begin_request(
      self,
      *,
      slot_id: str,
      sample_id: str,
      group: str,
      model_tag: str,
      model_name: str,
      attempt_id: str,
      stage: str = 'real_request',
      prompt: str,
      passed_params: Mapping[str, Any],
  ) -> None:
    """Checks all four budget layers, then reserves one unit durably.

    Write order (design §4.1–§4.2): cap check; ``raw/<slot>.prompt.txt``;
    ``raw/<slot>.started.json``; append the ``started`` event; increment
    in-memory counts. A crash anywhere after the sidecar write leaves a
    slot that already occupies budget, which is exactly the state
    resume rebuilds from.

    Args:
      slot_id: unique batch slot identifier.
      sample_id: sample the slot belongs to.
      group: sample group used for the group cap layer.
      model_tag: short model identifier used for the model cap layer.
      model_name: the live model's actual provider name.
      attempt_id: identifier of the attempt this slot belongs to.
      stage: pipeline stage label for the request.
      prompt: the exact prompt text sent to the provider.
      passed_params: the parameters passed alongside the prompt.

    Raises:
      LedgerBudgetError: if any layer's ``used + 1`` would exceed its
        cap; only a ``budget_blocked`` event is appended.
      LedgerAuditError: if the slot was already started once.
    """
    if slot_id in self._started_info:
      raise LedgerAuditError(f'duplicate started event for slot {slot_id}')
    sample_key = f'{sample_id}__{model_tag}'
    budgets_before = {
        'global': self._counts['global'],
        'model': self._counts['by_model'].get(model_tag, 0),
        'group': self._counts['by_group'].get(group, 0),
        'sample': self._counts['by_sample'].get(sample_key, 0),
    }
    budget_caps = {
        'global': self._global_cap,
        'model': self._model_caps.get(model_tag),
        'group': self._group_caps.get(group),
        'sample': self._sample_caps.get(group),
    }
    for level in ('global', 'model', 'group', 'sample'):
      cap = budget_caps[level]
      if cap is not None and budgets_before[level] + 1 > cap:
        self._append_event({
            'event': 'budget_blocked',
            'slot_id': slot_id,
            'level': level,
            'used': budgets_before[level],
            'cap': cap,
        })
        raise LedgerBudgetError(
            f'{level} budget exceeded for slot {slot_id!r}: used'
            f' {budgets_before[level]} + 1 > cap {cap}'
        )
    prompt_text = self._redact(prompt)
    started = {
        'event': 'started',
        'slot_id': slot_id,
        'sample_id': sample_id,
        'group': group,
        'model_tag': model_tag,
        'model_name': model_name,
        'stage': stage,
        'request_started_at_utc': _utc_now(),
        'budgets_before': budgets_before,
        'budget_caps': budget_caps,
        'prompt_sha256': _sha256_text(prompt_text),
        'passed_params': self._redact(passed_params),
        'batch_id': self._batch_id,
        'attempt_id': attempt_id,
    }
    _write_text_file(self._raw_dir / f'{slot_id}.prompt.txt', prompt_text)
    _write_json_file(
        self._raw_dir / f'{slot_id}.started.json',
        {key: value for key, value in started.items() if key != 'event'},
    )
    self._append_event(started)
    self._started_info[slot_id] = {
        'sample_id': sample_id,
        'group': group,
        'model_tag': model_tag,
    }
    self._counts['global'] += 1
    self._counts['by_model'][model_tag] = budgets_before['model'] + 1
    self._counts['by_group'][group] = budgets_before['group'] + 1
    self._counts['by_sample'][sample_key] = budgets_before['sample'] + 1
    self._counts['started_slots'].append(slot_id)

  def finish_request(
      self,
      *,
      slot_id: str,
      status: str,
      error: str | None = None,
      boundary: Mapping[str, Any] | None = None,
  ) -> None:
    """Records the terminal observation of a started slot.

    Counts are untouched: the unit was already reserved at ``started``.

    Args:
      slot_id: the started slot being closed.
      status: terminal status label (e.g. ``completed``).
      error: optional error text; redacted before archiving.
      boundary: optional provider-boundary metadata (actual model,
        call kwargs) captured at the SDK observation boundary.

    Raises:
      LedgerAuditError: if the slot has no prior ``started`` event.
    """
    info = self._started_info.get(slot_id)
    if info is None:
      raise LedgerAuditError(
          f'finished event for slot {slot_id!r} has no prior started event'
      )
    finished = {
        'event': 'finished',
        'slot_id': slot_id,
        'sample_id': info['sample_id'],
        'group': info['group'],
        'model_tag': info['model_tag'],
        'status': status,
        'error': None if error is None else self._redact(str(error)),
        'boundary': self._redact(dict(boundary) if boundary else {}),
        'finished_at_utc': _utc_now(),
    }
    _write_json_file(
        self._raw_dir / f'{slot_id}.record.json',
        {key: value for key, value in finished.items() if key != 'event'},
    )
    self._append_event(finished)
    if slot_id not in set(self._counts['finished_slots']):
      self._counts['finished_slots'].append(slot_id)

  def record_raw_response(self, slot_id: str, text: Any) -> None:
    """Archives the raw SDK response at the observation boundary.

    Must be called before any parsing, so a parse failure still leaves
    the first response on disk. A non-str payload is JSON-encoded so
    the file always holds readable text. Counts are untouched.

    Args:
      slot_id: the started slot the response belongs to.
      text: the raw response payload (str or JSON-serializable value).
    """
    if not isinstance(text, str):
      text = json.dumps(text, ensure_ascii=False)
    _write_text_file(
        self._raw_dir / f'{slot_id}.response.txt', self._redact(text)
    )

  def record_frozen_slot(
      self,
      *,
      slot_id: str,
      sample_id: str,
      model_tag: str,
      attempt_id: str,
      prompt: str,
      frozen_text: str,
  ) -> None:
    """Records a slot answered from frozen inputs; consumes no budget.

    Mirrors the 2026-10-20 batch shape: a ``frozen_stub`` event plus
    ``<slot>.prompt.txt``, ``<slot>.response.txt`` and
    ``<slot>.record.json`` sidecars.

    Args:
      slot_id: unique batch slot identifier.
      sample_id: sample the slot belongs to.
      model_tag: short model identifier the stub stands in for.
      attempt_id: identifier of the attempt this slot belongs to.
      prompt: the frozen prompt text the stub answers.
      frozen_text: the frozen answer text.
    """
    prompt_text = self._redact(prompt)
    stub = {
        'slot_id': slot_id,
        'sample_id': sample_id,
        'model_tag': model_tag,
        'attempt_id': attempt_id,
        'status': 'frozen_stub',
        'note': _FROZEN_STUB_NOTE,
        'prompt_sha256': _sha256_text(prompt_text),
        'utc': _utc_now(),
    }
    _write_text_file(self._raw_dir / f'{slot_id}.prompt.txt', prompt_text)
    _write_text_file(
        self._raw_dir / f'{slot_id}.response.txt', self._redact(frozen_text)
    )
    _write_json_file(self._raw_dir / f'{slot_id}.record.json', stub)
    self._append_event({'event': 'frozen_stub', **stub})
    self._counts['frozen_slots'].append(slot_id)

  def record_skipped(
      self, *, slot_id: str, sample_id: str, reason: str
  ) -> None:
    """Records a slot skipped after an upstream failure; no budget.

    Mirrors the old batch: a ``skipped`` event plus a
    ``<slot>.record.json`` sidecar holding the same fields.

    Args:
      slot_id: unique batch slot identifier.
      sample_id: sample the slot belongs to.
      reason: why the slot was never started; redacted before archiving.
    """
    record = {
        'slot_id': slot_id,
        'sample_id': sample_id,
        'status': 'skipped_by_upstream_failure',
        'reason': self._redact(reason),
        'utc': _utc_now(),
    }
    _write_json_file(self._raw_dir / f'{slot_id}.record.json', record)
    self._append_event({'event': 'skipped', **record})
    self._counts['skipped_slots'].append(slot_id)

  def record_second_request_block(
      self, slot_id: str, first_content: str
  ) -> None:
    """Records that a second request attempt was blocked pre-provider.

    The first (already billed) answer is preserved verbatim in the
    ``<slot>.second_request_blocked.json`` sidecar. The blocked attempt
    consumes no budget, but the first request's unit stays spent.

    Args:
      slot_id: the started slot whose loop attempted a second request.
      first_content: the first raw answer; redacted before archiving.
    """
    content = self._redact(first_content)
    block = {
        'event': 'second_request_blocked',
        'slot_id': slot_id,
        'first_content_sha256': _sha256_text(content),
        'utc': _utc_now(),
    }
    _write_json_file(
        self._raw_dir / f'{slot_id}.second_request_blocked.json',
        {'note': _SECOND_REQUEST_BLOCKED_NOTE, 'first_content': content},
    )
    self._append_event(block)
    self._counts['second_request_blocks'] += 1

  def _redact(self, value: Any) -> Any:
    """Recursively replaces secret literals in strings within a value."""
    if isinstance(value, str):
      for secret in self._secrets:
        if secret:
          value = value.replace(secret, '[REDACTED]')
      return value
    if isinstance(value, Mapping):
      return {key: self._redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
      return [self._redact(item) for item in value]
    return value

  def _append_event(self, event: Mapping[str, Any]) -> None:
    """Appends one complete JSON line to the ledger file."""
    with self._ledger_path.open('a', encoding='utf-8') as handle:
      handle.write(json.dumps(event, ensure_ascii=False) + '\n')
