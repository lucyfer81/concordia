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

"""Read-only closeout tool for world-response probe batches.

This module is the offline errata tool of
``docs/superpowers/specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md``:
it closes out an S1 probe batch without any real model call, credential
lookup, ``.env`` read, run or freeze execution. It never imports the old
batch's ``probe.py`` and never imports ``concordia``, ``openai`` or
``dotenv`` — only the standard library plus the pure offline functions of
``request_ledger`` (it never constructs a :class:`RequestLedger`, because
a closeout never writes a ledger).

I/O discipline: every required input must already exist (missing inputs
fail explicitly — no guessed zeros), the output directory must be fresh
and disjoint from the source batch even through symlinks, and a sha256
manifest of the whole source batch is written before the semantic stage
and re-verified after it, so any drift during the run is detected. A
structurally broken (e.g. truncated) ``requests.jsonl`` surfaces its
:class:`request_ledger.LedgerAuditError` transparently and no
``corrected_report.json`` is produced. The ledger audit also cross-checks
the ``raw/`` sidecars of every started slot.

Semantic errata (design §5.2; every rule is deterministic and derived
from the frozen inputs, with exactly one design-mandated frozen
annotation constant — the REV01/02 fixture-defect marking of §2.2):

- Counts come from the ledger's unique ``started`` slots (rebuilt via
  :func:`request_ledger.rebuild_counts`), never from the old
  ``report.json``; responses observed, no-result requests and format
  failures are listed separately instead of being merged into one
  completion number.
- Attempt status is split into three layers: execution status
  (``completed`` / ``review_rejected`` / ``format_error`` /
  ``started_no_result``), frozen-protocol match (expected vs. actual
  validity, frozen labels never rewritten) and a semantic annotation
  layer.
- The Gemini ``b.``/``b)`` first answers get an offline option-intent
  reading only after the counterpart m4 prompt's sha256 equals the m1
  started event's ``prompt_sha256``; the reading never changes the
  protocol verdict, which stays failed.
- The human-label audit reports missing, duplicate, unknown and
  non-labeled entries; agent labels never count as human confirmation.
- The source batch's ``overall_pass`` and frozen labels are preserved
  verbatim; the tool writes only into the fresh output directory.
"""

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
import datetime
import hashlib
import json
import pathlib
import re
import sys
from typing import Any

from examples.story_enactment import request_ledger

REQUIRED_INPUTS = (
    'protocol.json',
    'fixtures.json',
    'report.json',
    'requests.jsonl',
    'human_labels.jsonl',
    'README.md',
    'source_snapshot.json',
)
MANIFEST_NAME = 'input_manifest.json'
CORRECTED_REPORT_NAME = 'corrected_report.json'
CORRECTIONS_NAME = 'corrections.json'
README_NAME = 'README.md'

# Design §2.2/§5.2 mandated annotation (the only frozen semantic
# constant): REV01/02's positive fixtures are internally inconsistent
# (their narrations contradict the adjudicated action_result), so their
# four unmatched attempts cannot serve as evidence of model
# over-rejection. The frozen expected labels are never rewritten.
_FIXTURE_DEFECT_SAMPLES = {'REV01', 'REV02'}
_FIXTURE_DEFECT_REASON = (
    '设计 §2.2 既定标注：该正例夹具内部矛盾（旁白与已裁定的'
    ' action_result 不一致），其未匹配不能作为模型误拒绝证据；'
    '冻结 expected 标签保持原样，不倒改。'
)
_REV_SEMANTIC_SUMMARY = '十二次有效反例拒绝有据，四次正例尝试夹具有缺陷，合法正例接受能力缺少本组证据'
# A single ASCII letter with optional trailing '.' or ')' and optional
# surrounding whitespace, e.g. 'b.' or 'b)' (design §7 narrow rule).
_LETTER_RE = re.compile(r'\s*([a-z])[.)]?\s*')
_OPTION_LINE_RE = re.compile(r'\(([a-z])\) (.*)')
_README_FROZEN_CLAIM_RE = re.compile(r'冻结生成槽（REV generation）(\d+) 次')
_OFFLINE_INTENT_NOTE = '仅离线解读选项意向；协议判定仍为失败，未计通过。'


def sha256_file(path: pathlib.Path) -> str:
  """Returns the hex SHA-256 of a file's bytes."""
  digest = hashlib.sha256()
  with path.open('rb') as handle:
    for chunk in iter(lambda: handle.read(65536), b''):
      digest.update(chunk)
  return digest.hexdigest()


def _sha256_text(text: str) -> str:
  """Returns the hex SHA-256 of a UTF-8 string."""
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _utc_now() -> str:
  """Returns the current UTC time in the batch timestamp format."""
  return datetime.datetime.now(
      datetime.timezone.utc,
  ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _json_text(obj: Mapping[str, Any]) -> str:
  """Returns the exact text ``_write_json_file`` persists for ``obj``."""
  return json.dumps(obj, ensure_ascii=False, indent=2) + '\n'


def _load_json(path: pathlib.Path) -> dict[str, Any]:
  """Loads a UTF-8 JSON object from the read-only batch."""
  return json.loads(path.read_text(encoding='utf-8', errors='replace'))


def _read_text(path: pathlib.Path) -> str:
  """Reads a UTF-8 text file from the read-only batch."""
  return path.read_text(encoding='utf-8', errors='replace')


def _write_json_file(path: pathlib.Path, obj: Mapping[str, object]) -> None:
  """Writes indented JSON with a trailing newline, as the batch does."""
  path.write_text(_json_text(obj), encoding='utf-8')


def input_manifest_payload(batch_dir: pathlib.Path) -> dict:
  """Builds a recursive sha256 manifest of every file in the batch.

  Args:
    batch_dir: root of the read-only source batch.

  Returns:
    ``{'files': [{'path', 'sha256'}]}`` with posix relative paths
    covering every file under ``batch_dir``, sorted by path.
  """
  entries = sorted(
      (path.relative_to(batch_dir).as_posix(), sha256_file(path))
      for path in batch_dir.rglob('*')
      if path.is_file()
  )
  return {
      'files': [{'path': name, 'sha256': digest} for name, digest in entries]
  }


def verify_input_manifest(
    batch_dir: pathlib.Path,
    manifest: Mapping[str, Any],
) -> None:
  """Re-verifies the batch against a manifest payload.

  Args:
    batch_dir: root of the read-only source batch.
    manifest: a payload previously produced by ``input_manifest_payload``.

  Raises:
    ValueError: listing every missing, extra and changed file, if the
      batch has drifted from the manifest.
  """
  recorded = {entry['path']: entry['sha256'] for entry in manifest['files']}
  current = {
      entry['path']: entry['sha256']
      for entry in input_manifest_payload(batch_dir)['files']
  }
  missing = sorted(set(recorded) - set(current))
  extra = sorted(set(current) - set(recorded))
  changed = sorted(
      path
      for path in set(recorded) & set(current)
      if recorded[path] != current[path]
  )
  if missing or extra or changed:
    raise ValueError(
        'batch inputs differ from the manifest:'
        f' missing={missing} extra={extra} changed={changed}'
    )


def assert_disjoint_output_dir(
    source_dir: pathlib.Path,
    output_dir: pathlib.Path,
) -> None:
  """Refuses an output directory that exists or overlaps the source.

  Overlap is checked after ``resolve()`` in both directions, so a symlink
  pointing inside the source batch (or a parent of it) is rejected too.

  Args:
    source_dir: root of the read-only source batch.
    output_dir: directory the closeout is about to create.

  Raises:
    ValueError: if the output directory overlaps the source batch or
      already exists.
  """
  source = source_dir.resolve()
  output = output_dir.resolve()
  if output.is_relative_to(source) or source.is_relative_to(output):
    raise ValueError(
        'output directory must be outside the source batch (after symlink'
        f' resolution): source={source} output={output}'
    )
  if output_dir.exists():
    raise ValueError(f'refusing to overwrite existing directory: {output_dir}')


def _load_and_count(
    batch_dir: pathlib.Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
  """Loads, audits (with raw sidecars) and recounts the batch ledger."""
  events = request_ledger.load_events(batch_dir / 'requests.jsonl')
  audit = request_ledger.audit_events(
      events,
      raw_dir=batch_dir / 'raw',
      allow_missing_failure_prompts=True,
  )
  counts = request_ledger.rebuild_counts(events)
  return events, audit, counts


def validate_ledger(batch_dir: pathlib.Path) -> dict:
  """Loads, audits and recounts ``requests.jsonl`` without writing one.

  The structural audit cross-checks the ``raw/`` sidecars: a started
  slot without its ``<slot>.started.json`` file, or an orphan sidecar,
  fails the closeout instead of being guessed around.
  Missing prompts on historical pending or second-request-blocked slots
  are disclosed as warnings by the report; this permits offline errata
  on the old incomplete archive, never paid recovery or invented evidence.

  Args:
    batch_dir: root of the read-only source batch.

  Returns:
    The ``request_ledger.rebuild_counts`` payload for the batch ledger.

  Raises:
    request_ledger.LedgerAuditError: transparently, if the ledger fails
      the structural audit.
  """
  _, _, counts = _load_and_count(batch_dir)
  return counts


def _attempt_of_slot(slot_id: str) -> str:
  """Maps a slot id ``<sample>__<tag>__<stage>`` to its attempt id."""
  return '__'.join(slot_id.split('__')[:2])


def _classify_execution_status(
    attempt: Mapping[str, Any],
    pending_slots: Sequence[str],
) -> str:
  """Classifies one attempt's execution status (deterministic rules).

  Rules (design §5.2): ``status=='completed'`` → ``completed``;
  ``failure`` starting with ``'SecondRequestBlocked'`` →
  ``format_error``; ``outputs.failure_kind=='review_rejected'`` →
  ``review_rejected``; an attempt holding a pending (started but never
  finished) slot → ``started_no_result``; anything else → ``other``.
  """
  if attempt.get('status') == 'completed':
    return 'completed'
  failure = attempt.get('failure') or ''
  if failure.startswith('SecondRequestBlocked'):
    return 'format_error'
  outputs = attempt.get('outputs') or {}
  if outputs.get('failure_kind') == 'review_rejected':
    return 'review_rejected'
  if pending_slots:
    return 'started_no_result'
  return 'other'


def _rev_protocol_match(
    protocol: Mapping[str, Any],
    attempts: Mapping[str, Mapping[str, Any]],
    execution_status: Mapping[str, str],
    sample_expected: Mapping[str, Any],
) -> dict[str, Any]:
  """Matches REV attempts against their frozen expected labels.

  Rule: ``expected_valid`` is ``fixtures[sample]['expected']=='true'``;
  ``actual_valid`` is ``execution_status=='completed'`` (a review
  rejection is not a valid acceptance); equal values count as matched.
  Unmatched attempts of the design-marked fixture-defect samples carry
  ``fixture_defect: True`` with the frozen reason; the frozen labels
  themselves are never rewritten.
  """
  matched = 0
  unmatched_attempts: list[dict[str, Any]] = []
  for attempt_id in protocol.get('execution_order') or []:
    attempt = attempts.get(attempt_id) or {}
    if attempt.get('group') != 'REV':
      continue
    sample_id = str(attempt.get('sample_id') or attempt_id.split('__')[0])
    expected_valid = sample_expected.get(sample_id) == 'true'
    actual_valid = execution_status.get(attempt_id) == 'completed'
    if expected_valid == actual_valid:
      matched += 1
      continue
    entry: dict[str, Any] = {
        'attempt_id': attempt_id,
        'sample_id': sample_id,
        'expected_valid': expected_valid,
        'actual_valid': actual_valid,
        'fixture_defect': sample_id in _FIXTURE_DEFECT_SAMPLES,
    }
    if entry['fixture_defect']:
      entry['fixture_defect_reason'] = _FIXTURE_DEFECT_REASON
    unmatched_attempts.append(entry)
  return {
      'matched': matched,
      'unmatched': len(unmatched_attempts),
      'unmatched_attempts': unmatched_attempts,
      'frozen_labels_unchanged': True,
  }


def _not_needed_no_branch(
    protocol: Mapping[str, Any],
    counts: Mapping[str, Any],
    attempts: Mapping[str, Mapping[str, Any]],
    execution_status: Mapping[str, str],
) -> list[str]:
  """Lists planned-real slots skipped by a completed 'No' judgment.

  Rule: planned real slots (protocol slots minus the frozen ones) that
  were never started nor skipped by an upstream failure count as
  not-needed only when their attempt completed with
  ``outputs.judgment=='No'`` — the REP 'No' branch legitimately stops
  after the judgment, which is distinct from an upstream failure.
  """
  planned: set[str] = set()
  for per_model in (protocol.get('slots') or {}).values():
    for slot_ids in (per_model or {}).values():
      planned.update(slot_ids or [])
  frozen = set(protocol.get('frozen_slots') or [])
  candidates = (
      planned
      - frozen
      - set(counts['started_slots'])
      - set(counts['skipped_slots'])
  )
  not_needed = []
  for slot_id in sorted(candidates):
    attempt_id = _attempt_of_slot(slot_id)
    attempt = attempts.get(attempt_id) or {}
    outputs = attempt.get('outputs') or {}
    if execution_status.get(attempt_id) == 'completed' and (
        outputs.get('judgment') == 'No'
    ):
      not_needed.append(slot_id)
  return not_needed


def _budget_check(
    protocol: Mapping[str, Any],
    counts: Mapping[str, Any],
    sample_group: Mapping[str, Any],
) -> dict[str, Any]:
  """Checks the rebuilt usage against every configured budget layer.

  Caps come from ``protocol.json`` ``budgets``; used values come from
  the rebuilt ledger counts. A layer without a configured cap reports
  ``cap: None`` and never counts as exceeded.
  """
  budgets = protocol.get('budgets') or {}

  def used_cap_remaining(used: int, cap: Any) -> dict[str, Any]:
    return {
        'used': used,
        'cap': cap,
        'remaining': None if cap is None else cap - used,
    }

  def normalized_caps(caps: Any, keys: Sequence[str]) -> dict[str, Any]:
    # A scalar cap (the real protocol's ``per_model: 47``) applies to
    # every key; a mapping carries per-key caps.
    if isinstance(caps, Mapping):
      return dict(caps)
    return {key: caps for key in keys}

  def layer(used_by: Mapping[str, int], caps: Any) -> dict:
    caps_by_key = normalized_caps(caps, list(used_by))
    layers = {
        key: used_cap_remaining(used, caps_by_key.get(key))
        for key, used in used_by.items()
    }
    for key, cap in caps_by_key.items():
      if key not in layers:
        layers[key] = used_cap_remaining(0, cap)
    return layers

  by_sample: dict[str, dict[str, Any]] = {}
  per_sample_caps = budgets.get('per_sample') or {}
  for key, used in counts['by_sample'].items():
    sample_id = key.split('__')[0]
    group = sample_group.get(sample_id) or ''.join(
        char for char in sample_id if char.isalpha()
    )
    by_sample[key] = used_cap_remaining(used, per_sample_caps.get(group))
  every_layer = [
      used_cap_remaining(counts['global'], budgets.get('global_requests')),
      *layer(counts['by_model'], budgets.get('per_model') or {}).values(),
      *layer(
          counts['by_group'], budgets.get('per_group_both_models') or {}
      ).values(),
      *by_sample.values(),
  ]
  return {
      'global': used_cap_remaining(
          counts['global'], budgets.get('global_requests')
      ),
      'by_model': layer(counts['by_model'], budgets.get('per_model') or {}),
      'by_group': layer(
          counts['by_group'], budgets.get('per_group_both_models') or {}
      ),
      'by_sample': by_sample,
      'any_cap_exceeded': any(
          entry['cap'] is not None and entry['used'] > entry['cap']
          for entry in every_layer
      ),
  }


def _gemini_offline_analysis(
    batch_dir: pathlib.Path,
    srb_slots: Sequence[str],
    started_events: Mapping[str, Mapping[str, Any]],
    sample_expected: Mapping[str, Any],
    execution_status: Mapping[str, str],
) -> list[dict[str, Any]]:
  """Reads the blocked Gemini first answers offline (design §2.2/§5.2).

  For every ``second_request_blocked`` slot: read the sidecar's
  ``first_content``; extract the single option letter with the narrow
  ``_LETTER_RE``; verify the counterpart m4 prompt's sha256 equals the
  m1 started event's ``prompt_sha256`` before any mapping is trusted;
  then parse the ``(a)``/``(b)`` option lines (later — i.e. tail —
  matches win) and map the letter to its option text. Any failed step
  marks the entry with ``error`` and skips the mapping. The protocol
  verdict is never changed by this reading.
  """
  raw_dir = batch_dir / 'raw'
  analysis: list[dict[str, Any]] = []
  for slot_id in srb_slots:
    entry: dict[str, Any] = {'slot_id': slot_id}
    sidecar_path = raw_dir / f'{slot_id}.second_request_blocked.json'
    try:
      first_content = (_load_json(sidecar_path) or {}).get('first_content')
    except (OSError, json.JSONDecodeError) as error:
      entry['error'] = f'cannot read {sidecar_path.name}: {error}'
      analysis.append(entry)
      continue
    entry['raw_first_content'] = first_content
    letter_match = _LETTER_RE.fullmatch(first_content or '')
    if letter_match is None:
      entry['error'] = (
          'first_content is not a single option letter with one optional'
          " '.' or ')'"
      )
      analysis.append(entry)
      continue
    entry['normalized_letter'] = letter_match.group(1)
    sample_id, _, stage = slot_id.split('__', 2)
    m4_slot = f'{sample_id}__m4__{stage}'
    prompt_path = raw_dir / f'{m4_slot}.prompt.txt'
    if not prompt_path.is_file():
      entry['error'] = f'missing counterpart prompt {prompt_path.name}'
      analysis.append(entry)
      continue
    prompt_text = _read_text(prompt_path)
    started = started_events.get(slot_id) or {}
    if _sha256_text(prompt_text) != started.get('prompt_sha256'):
      entry['error'] = (
          'counterpart prompt sha256 differs from the started'
          ' prompt_sha256; the letter mapping is not trusted'
      )
      analysis.append(entry)
      continue
    options = {
        letter: text.strip()
        for letter, text in _OPTION_LINE_RE.findall(prompt_text)
    }
    letter = entry['normalized_letter']
    if letter not in options:
      entry['error'] = (
          f'letter {letter!r} has no option line in the counterpart prompt'
      )
      analysis.append(entry)
      continue
    entry['option_meaning'] = options[letter]
    entry['expected'] = sample_expected.get(sample_id)
    entry['intent_matches_expected'] = (
        entry['option_meaning'] == entry['expected']
    )
    entry['protocol_verdict'] = (
        'failed'
        if execution_status.get(_attempt_of_slot(slot_id)) != 'completed'
        else 'passed'
    )
    entry['note'] = _OFFLINE_INTENT_NOTE
    analysis.append(entry)
  return analysis


def _audit_human_labels(
    batch_dir: pathlib.Path,
    protocol: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
  """Audits the label file against the planned execution order.

  Deterministic reports, never a rewrite: missing planned ids, duplicate
  lines, unknown attempt ids and non-labeled statuses are listed
  separately. ``human_confirmation`` is ``'confirmed'`` only when the
  plan is non-empty and every planned id has exactly one ``labeled``
  line annotated by a human; agent labels stay ``'pending'``.
  """
  planned = [
      str(attempt_id) for attempt_id in protocol.get('execution_order') or []
  ]
  planned_set = set(planned)
  seen: dict[Any, int] = {}
  unknown: list[str] = []
  not_labeled: set[str] = set()
  annotator_counts: Counter[str] = Counter()
  with (batch_dir / 'human_labels.jsonl').open(
      encoding='utf-8', errors='replace'
  ) as handle:
    for line in handle:
      if not line.strip():
        continue
      label = json.loads(line)
      attempt_id = label.get('attempt_id')
      seen[attempt_id] = seen.get(attempt_id, 0) + 1
      if not isinstance(attempt_id, str) or attempt_id not in planned_set:
        unknown.append(
            attempt_id
            if isinstance(attempt_id, str)
            else '(missing attempt_id)'
        )
      annotator_type = label.get('annotator_type')
      if not isinstance(annotator_type, str):
        annotator_type = '(missing)'
      annotator_counts[annotator_type] += 1
      if label.get('status') != 'labeled' and isinstance(attempt_id, str):
        not_labeled.add(attempt_id)
  missing = [attempt_id for attempt_id in planned if attempt_id not in seen]
  duplicates = sorted(
      str(attempt_id)
      for attempt_id, count in seen.items()
      if count > 1 and isinstance(attempt_id, str)
  )
  audit = {
      'planned': planned,
      'labeled': sorted(
          str(attempt_id) for attempt_id in seen if isinstance(attempt_id, str)
      ),
      'missing': missing,
      'duplicates': duplicates,
      'unknown_attempt_ids': sorted(set(unknown)),
      'not_labeled': sorted(not_labeled),
      'annotator_type_counts': dict(sorted(annotator_counts.items())),
  }
  confirmed = (
      bool(planned)
      and not missing
      and not duplicates
      and not unknown
      and not not_labeled
      and set(annotator_counts) == {'human'}
  )
  return audit, 'confirmed' if confirmed else 'pending'


def build_corrected_report(
    batch_dir: pathlib.Path,
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict:
  """Builds the corrected S1 report from the frozen batch.

  Args:
    batch_dir: root of the read-only source batch.
    manifest: the input manifest payload already written by ``main``;
      recomputed from the batch when omitted.

  Returns:
    The ``corrected_report.json`` payload. Every value is derived from
    the frozen inputs by a deterministic rule; the only frozen
    annotation constants are the design-mandated REV01/02
    fixture-defect marking and its summary string.
  """
  events, audit, counts = _load_and_count(batch_dir)
  source_report = _load_json(batch_dir / 'report.json')
  protocol = _load_json(batch_dir / 'protocol.json')
  fixtures = _load_json(batch_dir / 'fixtures.json')
  attempts = source_report.get('attempts') or {}
  pending_by_attempt: dict[str, list[str]] = {}
  for slot_id in audit['pending_slots']:
    pending_by_attempt.setdefault(_attempt_of_slot(slot_id), []).append(slot_id)
  execution_status = {
      attempt_id: _classify_execution_status(
          attempt, pending_by_attempt.get(attempt_id, [])
      )
      for attempt_id, attempt in attempts.items()
  }
  started_events = {
      str(event['slot_id']): event
      for event in events
      if event.get('event') == 'started'
  }
  srb_slots = [
      str(event['slot_id'])
      for event in events
      if event.get('event') == 'second_request_blocked'
  ]
  samples = [
      sample
      for sample in fixtures.get('samples') or []
      if isinstance(sample, dict)
  ]
  sample_expected = {
      str(sample.get('sample_id')): sample.get('expected') for sample in samples
  }
  sample_group = {
      str(sample.get('sample_id')): sample.get('group') for sample in samples
  }
  warnings = list(audit['warnings'])
  if (
      _README_FROZEN_CLAIM_RE.search(_read_text(batch_dir / 'README.md'))
      is None
  ):
    warnings.append(
        'README frozen-generation-slots claim not found; no readme'
        ' correction is emitted'
    )
  if manifest is None:
    manifest = input_manifest_payload(batch_dir)
  labels_audit, human_confirmation = _audit_human_labels(batch_dir, protocol)
  return {
      'source_batch': f'{batch_dir.parent.name}/{batch_dir.name}',
      'provenance': {
          'created_utc': _utc_now(),
          'tool': 'world_response_probe_closeout',
          'input_manifest_sha256': _sha256_text(_json_text(manifest)),
          'source_report_sha256': sha256_file(batch_dir / 'report.json'),
          'new_real_api_calls': 0,
          'note': 'Offline errata only; the source batch is read-only.',
      },
      'ledger_counts': {
          'requests_attempted': {
              'total': counts['global'],
              'by_model': counts['by_model'],
              'by_group': counts['by_group'],
          },
          'responses_observed': len(counts['finished_slots']),
          'no_result_requests': list(audit['pending_slots']),
          'format_failures': srb_slots,
          'second_request_blocks': counts['second_request_blocks'],
          'frozen_generation_slots': len(counts['frozen_slots']),
          'skipped_by_upstream_failure': list(counts['skipped_slots']),
          'not_needed_no_branch': _not_needed_no_branch(
              protocol, counts, attempts, execution_status
          ),
          'budget_check': _budget_check(protocol, counts, sample_group),
      },
      'attempt_status_layers': {
          'execution_status': execution_status,
          'protocol_match': {
              'REV': _rev_protocol_match(
                  protocol, attempts, execution_status, sample_expected
              ),
          },
          'semantic_annotation': {
              'REV': {
                  'summary': _REV_SEMANTIC_SUMMARY,
                  'fixture_defect_samples': sorted(_FIXTURE_DEFECT_SAMPLES),
                  'frozen_expected_unchanged': True,
              },
          },
      },
      'gemini_format_offline_analysis': _gemini_offline_analysis(
          batch_dir,
          srb_slots,
          started_events,
          sample_expected,
          execution_status,
      ),
      'human_labels_audit': labels_audit,
      'human_confirmation': human_confirmation,
      'overall_pass_preserved': bool(source_report.get('overall_pass', False)),
      'warnings': warnings,
  }


def build_corrections(
    batch_dir: pathlib.Path,
    ledger_counts: Mapping[str, Any],
) -> dict:
  """Builds the old-vs-corrected diff with per-entry basis and reason.

  Only fields whose legacy value actually differs from the ledger-derived
  value produce an entry; values the old report already had right are
  not re-labeled as corrections.

  Args:
    batch_dir: root of the read-only source batch.
    ledger_counts: the ``ledger_counts`` block of the corrected report.

  Returns:
    ``{'corrections': [{'field', 'old', 'corrected', 'basis',
    'reason'}]}``.
  """
  source_report = _load_json(batch_dir / 'report.json')
  old_counts = source_report.get('counts') or {}
  readme_text = _read_text(batch_dir / 'README.md')
  attempted = ledger_counts['requests_attempted']
  entries: list[dict[str, Any]] = []

  def add(
      field: str, old: Any, corrected: Any, basis: str, reason: str
  ) -> None:
    if old != corrected:
      entries.append({
          'field': field,
          'old': old,
          'corrected': corrected,
          'basis': basis,
          'reason': reason,
      })

  add(
      'report.counts.real_calls',
      old_counts.get('real_calls'),
      attempted['total'],
      'requests.jsonl rebuild_counts (unique started slots)',
      '旧报告从中断边界恢复，漏计已启动的请求；勘误以账本唯一 started 口径为准。',
  )
  old_by_model = old_counts.get('by_model') or {}
  for tag in sorted(set(old_by_model) | set(attempted['by_model'])):
    add(
        f'report.counts.by_model.{tag}',
        old_by_model.get(tag),
        attempted['by_model'].get(tag),
        'requests.jsonl rebuild_counts (unique started slots)',
        '旧报告该模型计数与账本唯一 started 口径不一致。',
    )
  old_by_group = old_counts.get('by_group') or {}
  for group in sorted(set(old_by_group) | set(attempted['by_group'])):
    add(
        f'report.counts.by_group.{group}',
        old_by_group.get(group),
        attempted['by_group'].get(group),
        'requests.jsonl rebuild_counts (unique started slots)',
        '旧报告该组计数与账本唯一 started 口径不一致。',
    )
  add(
      'report.counts.second_request_blocks',
      old_counts.get('second_request_blocks'),
      ledger_counts['second_request_blocks'],
      'requests.jsonl second_request_blocked events',
      '两次隐藏重试在到达提供方前被阻止；旧报告计 0，账本记录 2 次。',
  )
  claim = _README_FROZEN_CLAIM_RE.search(readme_text)
  if claim is not None:
    add(
        'readme.frozen_generation_slots',
        int(claim.group(1)),
        ledger_counts['frozen_generation_slots'],
        'requests.jsonl frozen_stub events',
        'README 声称的冻结生成槽数与账本 frozen_stub 事件数不一致；'
        '两者均为零真实调用。',
    )
  return {'corrections': entries}


def build_output_readme(
    report: Mapping[str, Any],
    corrections: Mapping[str, Any],
) -> str:
  """Builds the Chinese errata README for the output directory."""
  counts = report['ledger_counts']
  attempted = counts['requests_attempted']
  by_model = json.dumps(attempted['by_model'], ensure_ascii=False)
  by_group = json.dumps(attempted['by_group'], ensure_ascii=False)
  return '\n'.join([
      '# S1 批次离线勘误说明（零真实调用）',
      '',
      (
          f"- 输入批次：`{report['source_batch']}`（只读）。输入清单与逐文件"
          ' 哈希见 `input_manifest.json`，工具结束后已复验一致。'
      ),
      (
          f"- 账本口径：`requests.jsonl` 唯一 started 共 {attempted['total']}"
          f' 次请求尝试（by_model {by_model}；by_group {by_group}），'
          f" 观测到响应 {counts['responses_observed']} 次，"
          f" 无结果请求 {len(counts['no_result_requests'])} 项，"
          f" 格式失败 {len(counts['format_failures'])} 项，"
          f" 冻结生成槽 {counts['frozen_generation_slots']} 个"
          '（均零真实调用）。'
      ),
      (
          '- 三层状态语义：执行状态（completed / review_rejected /'
          ' format_error / started_no_result）、冻结协议匹配（matched /'
          ' unmatched，标签不倒改）与语义标注（REV01/02 夹具缺陷单列）'
          ' 分列于 `attempt_status_layers`，不合并为一个完成数。'
      ),
      (
          '- Gemini 离线意向分析：仅离线解读 `b.`/`b)` 首答对应选项的含义，'
          ' 并先校验 m4 提示 sha256 与 m1 started 记录一致；该分析不作'
          ' 协议判定，原判定仍为失败，未计通过。'
      ),
      f'- 原结论保留：`overall_pass` 保持 '
      f"`{str(report['overall_pass_preserved']).lower()}`，冻结 expected"
      ' 标签与原标签文件原样保留；agent 初评不等同人类确认'
      f"（human_confirmation = {report['human_confirmation']}）。",
      (
          f"- 勘误条目共 {len(corrections['corrections'])} 项，见"
          ' `corrections.json`；每条给出 old / corrected / 依据 / 理由。'
      ),
      (
          '- 本次勘误未发起任何真实模型调用（new_real_api_calls = 0），'
          ' 未读取凭据，未写回源批次。'
      ),
      *(f'- 输入证据缺口：{warning}' for warning in report.get('warnings', ())),
      '',
  ])


def _parse_args(argv: list[str]) -> argparse.Namespace:
  """Parses the closeout CLI (both arguments are required)."""
  parser = argparse.ArgumentParser(
      description=(
          'Offline closeout of a world-response probe batch: zero real'
          ' calls, read-only on the source batch.'
      )
  )
  parser.add_argument(
      '--source_batch_dir',
      required=True,
      help='Read-only source batch directory with all required inputs.',
  )
  parser.add_argument(
      '--output_dir',
      required=True,
      help=(
          'Fresh output directory; it must not exist and must not overlap'
          ' the source batch.'
      ),
  )
  return parser.parse_args(argv)


def main(argv: list[str]) -> None:
  """Runs the full offline closeout into a fresh output directory.

  Flow: parse arguments; require every input in ``REQUIRED_INPUTS`` to
  exist (otherwise exit naming the missing files, writing nothing);
  enforce a fresh, disjoint output directory; create it; write the
  input manifest; validate the request ledger against its ``raw/``
  sidecars (a ``LedgerAuditError`` exits with its message and produces
  no ``corrected_report.json``); build and write ``corrected_report.json``,
  ``corrections.json`` and the Chinese ``README.md``; re-verify the
  manifest to detect drift during the run.

  Args:
    argv: CLI arguments, e.g. ``sys.argv[1:]``.
  """
  args = _parse_args(argv)
  source_dir = pathlib.Path(args.source_batch_dir)
  output_dir = pathlib.Path(args.output_dir)
  missing = [
      name for name in REQUIRED_INPUTS if not (source_dir / name).is_file()
  ]
  if missing:
    raise SystemExit(
        'source batch is missing required inputs (refusing to guess or'
        f' fill zeros): {", ".join(missing)} in {source_dir}'
    )
  try:
    assert_disjoint_output_dir(source_dir, output_dir)
  except ValueError as error:
    raise SystemExit(str(error)) from error
  output_dir.mkdir(parents=True)
  manifest = input_manifest_payload(source_dir)
  _write_json_file(output_dir / MANIFEST_NAME, manifest)
  try:
    validate_ledger(source_dir)
  except request_ledger.LedgerAuditError as error:
    raise SystemExit(
        f'ledger audit failed, no corrected report is written: {error}'
    ) from error
  report = build_corrected_report(source_dir, manifest=manifest)
  corrections = build_corrections(source_dir, report['ledger_counts'])
  _write_json_file(output_dir / CORRECTED_REPORT_NAME, report)
  _write_json_file(output_dir / CORRECTIONS_NAME, corrections)
  (output_dir / README_NAME).write_text(
      build_output_readme(report, corrections), encoding='utf-8'
  )
  try:
    verify_input_manifest(source_dir, manifest)
  except ValueError as error:
    raise SystemExit(f'post-work input drift detected: {error}') from error


if __name__ == '__main__':
  main(sys.argv[1:])
