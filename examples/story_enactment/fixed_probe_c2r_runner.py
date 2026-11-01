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

r"""Real-execution driver for the C-2R replacement mini-probe.

This module wires the accepted C-2R offline harness
(:mod:`examples.story_enactment.fixed_probe_c2r` on top of
:mod:`examples.story_enactment.fixed_probe`) for real execution:
freezing the self-contained batch inputs (the manifest copy, the plan
snapshot, the embedded before source materialized in-batch, the R2
turns batch-local copy and the execution-dependency closure),
verifying that freeze against batch-local copies only, and running
the batch itself — behind a one-time atomic execution marker, with
the engine composed as three-arm audit wrapper (contract A: each
round's BASE/DISC/WAIT judgment prompts are audited before the WAIT
judgment is forwarded, so a controlled-difference drift blocks the
request and every later call with zero billing) above the production
retry wrapper above the probe's two ledger layers, and with the C-2R
integrity monitor adding the batch-local file group to the inherited
watched groups. The real run is separately authorized; everything
implemented here is testable offline — zero real model calls, zero
credential reads outside the explicitly passed environment file and
zero network. Credentials travel by parameter, never through
``os.environ``; the freeze and verify stages structurally take no
environment path at all.
"""

from collections.abc import Callable, Mapping, Sequence
import datetime
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from absl import app
from absl import flags
from concordia.language_model import language_model

from examples.story_enactment import fixed_probe as fp
from examples.story_enactment import fixed_probe_c2r as c2r
from examples.story_enactment import fixed_probe_runner as fpr
from examples.story_enactment import request_ledger as rl

# Repository root (the runner lives at examples/story_enactment/).
_REPO_ROOT = Path(__file__).parents[2]

# Frozen model identity: the run may only ever address the Sol model
# recorded under this environment key (same identity as the source
# protocol; no old-batch authorization is reused).
_ENV_KEY = 'AI_MODEL_4'
_MODEL_NAME = 'gpt-6.1-sol'
_MODEL_TAG = 'm4'

# Repository paths of the freeze inputs: the C-2R data authority and
# the implementation plan whose byte copy becomes the batch's
# immutable ``plan_snapshot.md``.
_MANIFEST_PATH = (
    _REPO_ROOT
    / 'examples'
    / 'story_enactment'
    / 'fixed_probe_c2r_manifest.json'
)
_PLAN_DOC_PATH = (
    _REPO_ROOT
    / 'docs'
    / 'superpowers'
    / 'plans'
    / '2026-10-29-story-enactment-c2r-supplementary-verification-plan.md'
)

# Schema identifier of the C-2R freeze record (a distinct shape from
# the source protocol's freeze; sharing the string would lie about
# compatibility).
_FREEZE_SCHEMA = 'fixed-probe-c2r-freeze-v1'

# Files whose presence proves the directory already carries a run —
# reused from the source protocol's runner.
_RUN_TRACE_NAMES = fpr._RUN_TRACE_NAMES  # pylint: disable=protected-access

# The single explicit set of pinned deliverable files: the C-2R probe
# and runner pair with their tests, the reused harness modules, the
# story configuration, both production files (frozen at their current,
# after state; the before state is pinned by the manifest) and the
# replacement design document. The repository plan document is
# excluded (its frozen form is the in-batch ``plan_snapshot.md``).
_FROZEN_SOURCES = (
    (
        'docs/superpowers/specs/'
        '2026-10-28-story-enactment-fixed-probe-c2-replacement-design.md'
    ),
    'examples/story_enactment/fixed_probe_c2r_manifest.json',
    'examples/story_enactment/fixed_probe_c2r.py',
    'examples/story_enactment/fixed_probe_c2r_test.py',
    'examples/story_enactment/fixed_probe_c2r_runner.py',
    'examples/story_enactment/fixed_probe_c2r_runner_test.py',
    'examples/story_enactment/fixed_probe.py',
    'examples/story_enactment/fixed_probe_test.py',
    'examples/story_enactment/fixed_probe_runner.py',
    'examples/story_enactment/fixed_probe_runner_test.py',
    'examples/story_enactment/request_ledger.py',
    'examples/story_enactment/fixed_model_probe.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/README.md',
    'concordia/contrib/components/game_master/repetition_push.py',
    'concordia/contrib/components/game_master/story_resolution.py',
)

# The three arms' execution groups (virtual unit + condition).
_BASE_GROUP = f'{c2r.C2RVirtualUnits[c2r.ARM_BASE][0]}-before'
_DISC_GROUP = f'{c2r.C2RVirtualUnits[c2r.ARM_DISCLOSURE][0]}-after'
_WAIT_GROUP = f'{c2r.C2RVirtualUnits[c2r.ARM_WAITING][0]}-after'


def _load_freeze(batch_dir: Path) -> dict[str, Any]:
  """Loads the C-2R freeze record, refusing absence and schema drift.

  Args:
    batch_dir: the frozen batch directory.

  Returns:
    The parsed freeze record.

  Raises:
    fp.BatchFault: if the record is missing, invalid JSON or not of
      the C-2R freeze schema.
  """
  path = batch_dir / 'freeze.json'
  if not path.is_file():
    raise fp.BatchFault(f'freeze record missing: {path}')
  try:
    freeze = json.loads(path.read_text(encoding='utf-8'))
  except json.JSONDecodeError as error:
    raise fp.BatchFault(f'freeze record is not valid JSON: {error}') from error
  if freeze.get('schema') != _FREEZE_SCHEMA:
    raise fp.BatchFault(
        f'freeze schema {freeze.get("schema")!r} != {_FREEZE_SCHEMA!r}'
    )
  return freeze


def _frozen_source_hashes(manifest: c2r.C2RManifest) -> dict[str, str]:
  """Hashes the pinned deliverable set, cross-checked with the manifest.

  Args:
    manifest: the C-2R data authority.

  Returns:
    The repository-relative path to sha256 mapping.

  Raises:
    fp.BatchFault: if a manifest before-state file is not covered by
      the frozen set, or a pinned source is missing from the tree.
  """
  derived = set(manifest.code_states['before']['files'])
  explicit = set(_FROZEN_SOURCES)
  missing = sorted(derived - explicit)
  if missing:
    raise fp.BatchFault(
        f'manifest before-state files not covered by _FROZEN_SOURCES: {missing}'
    )
  return {rel: fpr._pinned_sha256(rel) for rel in _FROZEN_SOURCES}  # pylint: disable=protected-access


def _repo_module_hashes(view: fp.Manifest, batch_dir: Path) -> dict[str, str]:
  """Thin routing of the source protocol's module-file enumerator."""
  return fpr._repo_module_hashes(view, batch_dir)  # pylint: disable=protected-access


def _execution_dependency_closure(
    view: fp.Manifest, batch_dir: Path
) -> dict[str, str]:
  """Enumerates the execution dependency closure by running one stub batch.

  Mirrors the source protocol's closure enumeration on the C-2R view:
  imports the real-run composition modules (the production OpenAI
  adapter and the retry wrapper the offline batch never loads),
  executes the complete frozen order on the offline stub model into a
  temporary directory — zero real calls, existing only to trigger
  every delayed import — and records the sha256 of every in-process
  module file under the repository root (via this module's patchable
  ``_repo_module_hashes`` seam).

  Args:
    view: the C-2R compatibility view.
    batch_dir: the freeze target (excluded from the enumeration).

  Returns:
    The repository-relative path to sha256 mapping of the closure.

  Raises:
    fp.BatchFault: if the enumeration batch itself faults.
  """
  importlib.import_module('concordia.contrib.language_models.openai.gpt_model')
  importlib.import_module('concordia.language_model.retry_wrapper')
  with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    states = fp.load_code_states(view, work / 'code_states')
    report = fp.run_batch(
        view,
        fpr._ClosureStubModel(),  # pylint: disable=protected-access
        work / 'probe',
        code_states=states,
    )
    if report.fault is not None:
      raise fp.BatchFault(
          f'closure-enumeration stub batch faulted: {report.fault}'
      )
  return _repo_module_hashes(view, batch_dir)


def freeze_stage(batch_dir: Path | str) -> dict[str, Any]:
  """Freezes the C-2R batch's immutable execution inputs (offline).

  Refuses an existing target, then materializes the self-contained
  batch: the byte copy of the C-2R manifest, the byte copy of the
  implementation plan, the before source written from the manifest's
  embedded (hash-pinned) text, the R2 turns copied from the frozen
  source batch and hash-checked against the manifest pin, and the
  execution-dependency closure. The freeze record binds them all by
  measured sha256 and adds the ``batch_file_hashes`` group the C-2R
  integrity monitor watches. The whole stage is credential-free by
  construction.

  Args:
    batch_dir: the freeze target directory; must not exist.

  Returns:
    The freeze record (also written as ``freeze.json``).

  Raises:
    fp.BatchFault: if the target exists, a pinned source is missing,
      or the R2 source disagrees with the manifest pin.
  """
  batch_dir = Path(batch_dir)
  if batch_dir.exists():
    raise fp.BatchFault(f'freeze target already exists: {batch_dir}')
  manifest = c2r.load_c2r_manifest(_MANIFEST_PATH)
  source_hashes = _frozen_source_hashes(manifest)
  r2 = manifest.batch_sources['R2_turns']
  r2_source = _REPO_ROOT / manifest.batch_root / r2['path']
  if not r2_source.is_file():
    raise fp.BatchFault(
        f'batch source R2_turns missing from the working tree: {r2_source}'
    )
  r2_bytes = r2_source.read_bytes()
  r2_sha = hashlib.sha256(r2_bytes).hexdigest()
  if r2_sha != r2['sha256']:
    raise fp.BatchFault(
        f'batch source R2_turns hash {r2_sha} != manifest pin {r2["sha256"]}'
    )
  view = c2r.build_compat_view(manifest)
  batch_dir.mkdir(parents=True)
  (batch_dir / 'manifest.json').write_bytes(_MANIFEST_PATH.read_bytes())
  (batch_dir / 'plan_snapshot.md').write_bytes(_PLAN_DOC_PATH.read_bytes())
  before_dir = batch_dir / 'code_states' / 'before'
  before_dir.mkdir(parents=True)
  batch_file_hashes: dict[str, str] = {}
  for rel, source in manifest.code_states['before']['sources'].items():
    target = before_dir / Path(rel).name
    target.write_text(source, encoding='utf-8')
    batch_file_hashes[f'code_states/before/{Path(rel).name}'] = (
        manifest.code_states['before']['files'][rel]
    )
  (batch_dir / 'batch_sources').mkdir()
  (batch_dir / 'batch_sources' / 'R2_turns.jsonl').write_bytes(r2_bytes)
  batch_file_hashes['batch_sources/R2_turns.jsonl'] = r2_sha
  closure = _execution_dependency_closure(view, batch_dir)
  fpr._write_json_file(batch_dir / 'dependency_hashes.json', closure)  # pylint: disable=protected-access
  budget = manifest.budget
  freeze: dict[str, Any] = {
      'schema': _FREEZE_SCHEMA,
      'created_utc': (
          datetime.datetime.now(datetime.timezone.utc).strftime(
              '%Y%m%dT%H%M%SZ'
          )
      ),
      'batch_dir': str(batch_dir),
      'manifest_sha256': fpr._sha256_file(batch_dir / 'manifest.json'),  # pylint: disable=protected-access
      'plan_snapshot_sha256': fpr._sha256_file(  # pylint: disable=protected-access
          batch_dir / 'plan_snapshot.md'
      ),
      'dependency_hashes_sha256': fpr._sha256_file(  # pylint: disable=protected-access
          batch_dir / 'dependency_hashes.json'
      ),
      'design_sha256': manifest.design_sha256,
      'budget': {
          'attempts_global_cap': budget['concordia_attempts']['global_cap'],
          'attempts_group_caps': {
              'C': budget['concordia_attempts']['global_cap']
          },
          'sdk_create_cap': budget['sdk_create']['global_cap'],
          'http_upper_bound_disclosure': budget['http_upper_bound_disclosure'],
          'logical_stage_keys': budget['logical_stage_keys'],
      },
      'model': {'tag': _MODEL_TAG, 'name': _MODEL_NAME, 'env_key': _ENV_KEY},
      'source_hashes': source_hashes,
      'batch_file_hashes': batch_file_hashes,
  }
  fpr._write_json_file(batch_dir / 'freeze.json', freeze)  # pylint: disable=protected-access
  return freeze


def _reextract_against_batch(
    manifest: c2r.C2RManifest, batch_dir: Path
) -> None:
  """Re-extracts every batch-derived fixture from the batch-local copy.

  The manifest's ``batch_sources`` paths are relative to the frozen
  source batch; inside this batch the copies live under
  ``batch_sources/<name>.jsonl``. Every batch-derived fixture value is
  re-extracted from that copy through the harness's own extractor and
  compared with the frozen inline value — the working tree's
  ``run_results`` is never read.

  Args:
    manifest: the batch's data authority.
    batch_dir: the frozen batch directory.

  Raises:
    fp.BatchFault: on any re-extraction mismatch.
  """
  local_sources = {
      name: {'path': f'batch_sources/{name}.jsonl'}
      for name in manifest.batch_sources
  }
  shim = type('BatchLocalSources', (), {'batch_sources': local_sources})()
  fixtures = manifest.fixtures[c2r.C2R_UNIT]
  for field, entry in fixtures.items():
    if (
        not isinstance(entry, Mapping)
        or entry.get('kind') not in fp._BATCH_KINDS  # pylint: disable=protected-access
    ):
      continue
    extracted = fp._extract_batch_value(  # pylint: disable=protected-access
        batch_dir, shim, entry
    )
    if extracted != entry['value']:
      raise fp.BatchFault(
          f'fixture C-2R.{field} re-extraction from the batch-local copy'
          ' disagrees with the frozen value'
      )


def verify_stage(
    batch_dir: Path | str, *, reextract_fixtures: bool = True
) -> dict[str, Any]:
  """Verifies the C-2R freeze against the tree and the batch-local copies.

  Checks, in order: the freeze record's schema; the in-batch manifest
  copy, plan snapshot and dependency hash manifest against their
  freeze-time hashes; every ``source_hashes`` and dependency-closure
  entry against the working tree; the two ``batch_file_hashes``
  entries (the materialized before source and the R2 turns copy)
  against their pinned hashes; and — unless ``reextract_fixtures`` is
  false — re-extracts every batch-derived fixture from the batch-local
  copy.

  Args:
    batch_dir: the frozen batch directory.
    reextract_fixtures: whether to re-extract the fixtures from the
      batch-local copies.

  Returns:
    A verification summary.

  Raises:
    fp.BatchFault: on any absence, schema or hash disagreement.
  """
  batch_dir = Path(batch_dir)
  freeze = _load_freeze(batch_dir)
  fpr._require_pinned_hash(  # pylint: disable=protected-access
      batch_dir / 'manifest.json',
      freeze['manifest_sha256'],
      'frozen input manifest.json',
  )
  fpr._require_pinned_hash(  # pylint: disable=protected-access
      batch_dir / 'plan_snapshot.md',
      freeze['plan_snapshot_sha256'],
      'frozen input plan_snapshot.md',
  )
  fpr._require_pinned_hash(  # pylint: disable=protected-access
      batch_dir / 'dependency_hashes.json',
      freeze['dependency_hashes_sha256'],
      'dependency hash manifest',
  )
  closure = json.loads(
      (batch_dir / 'dependency_hashes.json').read_text(encoding='utf-8')
  )
  fpr._check_tree_hashes(freeze['source_hashes'], 'frozen source')  # pylint: disable=protected-access
  fpr._check_tree_hashes(closure, 'dependency closure')  # pylint: disable=protected-access
  for rel, pinned in freeze['batch_file_hashes'].items():
    fpr._require_pinned_hash(  # pylint: disable=protected-access
        batch_dir / rel, pinned, f'batch file {rel}'
    )
  summary: dict[str, Any] = {
      'schema': freeze['schema'],
      'batch_dir': str(batch_dir),
      'source_files': len(freeze['source_hashes']),
      'dependency_files': len(closure),
      'batch_files': len(freeze['batch_file_hashes']),
      'fixture_reextraction': 'skipped',
  }
  if reextract_fixtures:
    manifest = c2r.load_c2r_manifest(batch_dir / 'manifest.json')
    _reextract_against_batch(manifest, batch_dir)
    summary['fixture_reextraction'] = 'verified'
  return summary


class C2RIntegrityMonitor(fpr.DependencyIntegrityMonitor):
  """The C-2R per-forwarding monitor: inherited groups plus batch files.

  ``check()`` first re-verifies the three inherited groups (the
  batch's frozen input files, the dependency closure, the pinned
  deliverable files) exactly as the source protocol's monitor does,
  then re-hashes the C-2R batch-local file group recorded in
  ``freeze["batch_file_hashes"]`` — the materialized before source
  (which ``load_code_states`` rewrites at run start with the same
  bytes) and the R2 turns copy. The inherited monitor never reads
  this group; this mapping is the only way those files are watched.

  Attributes:
    _batch_file_hashes: the batch-relative path to pinned sha256 map.
  """

  def __init__(self, freeze: Mapping[str, Any], batch_dir: Path | str):
    super().__init__(freeze, batch_dir)
    self._batch_file_hashes = dict(freeze['batch_file_hashes'])

  def check(self) -> None:
    """Re-hashes all groups; raises on any drift or absence."""
    super().check()
    for rel, pinned in self._batch_file_hashes.items():
      fpr._require_pinned_hash(  # pylint: disable=protected-access
          self._batch_dir / rel, pinned, f'batch file {rel}'
      )


class ThreeArmAuditEngine:
  """Contract A: per-round three-arm audit before the WAIT judgment.

  Composed above the production retry wrapper (which sits above the
  probe's two ledger layers), this interposer remembers each round's
  BASE and DISC judgment prompts as it forwards them; when the WAIT
  arm's judgment request arrives — strictly before any retry attempt
  or ledger booking — it audits the three actual prompts with
  :func:`c2r.audit_controlled_differences`. Non-empty findings raise
  a :class:`fp.BatchFault` (a batch integrity fault) that blocks this
  request and every subsequent call with zero billing. Binding the
  BASE group resets the per-round memory; retries happen below this
  layer and never double-count an audit.

  Attributes:
    audit_log: one ``{'rep', 'findings'}`` record per executed audit.
  """

  def __init__(
      self,
      inner: Any,
      *,
      audit_fn: Callable[[str, str, str], list[str]] = (
          c2r.audit_controlled_differences
      ),
  ):
    self._inner = inner
    self._audit_fn = audit_fn
    self.audit_log: list[dict[str, Any]] = []
    self._group = ''
    self._stage_label = ''
    self._rep_prompts: dict[str, str] = {}

  @property
  def model_name(self) -> str:
    return self._inner.model_name

  def bind(self, group: str, stage_label: str) -> None:
    """Forwards a bind; the BASE group's bind starts a new round."""
    if group == _BASE_GROUP:
      self._rep_prompts = {}
    self._group = group
    self._stage_label = stage_label
    self._inner.bind(group, stage_label)

  def sample_text(self, prompt: str, **kwargs: Any) -> str:
    """Forwards one text call unchanged."""
    return self._inner.sample_text(prompt, **kwargs)

  def sample_choice(
      self, prompt: str, responses: Sequence[str], *, seed: int | None = None
  ) -> tuple[int, str, Any]:
    """Forwards one choice call, auditing WAIT judgments in flight."""
    if self._stage_label == 'judgment':
      self._observe_judgment(self._group, prompt)
    return self._inner.sample_choice(prompt, responses, seed=seed)

  def take_run_log(self) -> list[dict[str, Any]]:
    return self._inner.take_run_log()

  def take_run_events(self) -> list[dict[str, Any]]:
    return self._inner.take_run_events()

  def _observe_judgment(self, group: str, prompt: str) -> None:
    """Records BASE/DISC prompts; audits the WAIT prompt pre-forward."""
    if group != _WAIT_GROUP:
      self._rep_prompts[group] = prompt
      return
    findings: list[str] = []
    for name, arm_group in (
        ('BASE', _BASE_GROUP),
        ('DISC', _DISC_GROUP),
    ):
      if arm_group not in self._rep_prompts:
        findings.append(
            f'{name} judgment prompt missing for this round (order violation)'
        )
    if not findings:
      findings = list(
          self._audit_fn(
              self._rep_prompts[_BASE_GROUP],
              self._rep_prompts[_DISC_GROUP],
              prompt,
          )
      )
    self.audit_log.append(
        {'rep': len(self.audit_log) + 1, 'findings': findings}
    )
    if findings:
      raise fp.BatchFault(
          'three-arm controlled-difference audit failed (batch integrity'
          f' fault): {findings}'
      )


def _final_label(outcome: str | None) -> str | None:
  """Maps one judgment outcome to its final legal Yes/No label."""
  return {'yes': 'yes', 'no': 'no'}.get(outcome or '')


def _pair_record(
    base_outcome: str | None, base_slot: str | None, other_outcome: str | None
) -> dict[str, Any]:
  """Builds one comparison record against the shared baseline."""
  base_label = _final_label(base_outcome)
  other_label = _final_label(other_outcome)
  if base_label is None or other_label is None:
    return {
        'pairable': False,
        'reason': 'unpairable: no final legal judgment label',
        'base_slot': base_slot,
    }
  return {
      'pairable': True,
      'base_slot': base_slot,
      'base': base_label,
      'other': other_label,
      'comparison': f'{base_label}->{other_label}',
  }


def write_paired_report(
    report: fp.BatchReport,
    out_dir: Path | str,
    *,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
  """Writes the C-2R paired-comparison records (plan §5).

  Each round yields one record holding the three arms' final judgment
  outcomes and their BASE-referencing slot bookkeeping: the baseline's
  complete judgment-attempt slot list (retries preserved), the shared
  slot both comparisons reference (the final attempt's slot id, the
  same one for B→D and B→W — the shared baseline is never counted as
  two independent observations), and the two paired comparisons. A
  round whose baseline (or counterpart) lacks a final legal Yes/No
  label is recorded unpairable: it never enters the eight-combo rows
  and no final label is fabricated for it.

  Args:
    report: the run's (rebuilt) bookkeeping.
    out_dir: batch directory the records are written to.
    secrets: literal values replaced with ``[REDACTED]``.

  Returns:
    The written structure (``paired_comparisons.json``).
  """
  by_position = {
      (entry['unit'], entry['condition'], entry['rep']): entry
      for entry in report.execution_ledger
  }
  records: list[dict[str, Any]] = []
  for rep in (1, 2, 3):
    arms = {}
    for unit, condition in (
        ('C-2R-BASE', 'before'),
        ('C-2R-DISC', 'after'),
        ('C-2R-WAIT', 'after'),
    ):
      entry = by_position.get((unit, condition, rep))
      if entry is None:
        arms[unit] = {
            'final_outcome': None,
            'judgment_slots': [],
            'shared_slot': None,
        }
        continue
      key = f'{unit}|{condition}|r{rep}|judgment'
      slots = list(entry['slots'].get('judgment', []))
      arms[unit] = {
          'final_outcome': report.outcomes.get(key),
          'judgment_slots': slots,
          'shared_slot': slots[-1] if slots else None,
      }
    base = arms['C-2R-BASE']
    record = {
        'rep': rep,
        'base': base,
        'disclosure': arms['C-2R-DISC'],
        'waiting': arms['C-2R-WAIT'],
        'pairs': {
            'b_d': _pair_record(
                base['final_outcome'],
                base['shared_slot'],
                arms['C-2R-DISC']['final_outcome'],
            ),
            'b_w': _pair_record(
                base['final_outcome'],
                base['shared_slot'],
                arms['C-2R-WAIT']['final_outcome'],
            ),
        },
    }
    labels = tuple(
        _final_label(arms[unit]['final_outcome'])
        for unit in ('C-2R-BASE', 'C-2R-DISC', 'C-2R-WAIT')
    )
    record['combo'] = (
        '|'.join(str(label) for label in labels) if all(labels) else None
    )
    records.append(record)
  payload = {
      'records': records,
      'combo_rows': [
          record['combo'] for record in records if record['combo'] is not None
      ],
      'unpairable_reps': [
          record['rep'] for record in records if record['combo'] is None
      ],
  }
  if secrets:
    payload = rl.redact(payload, secrets)
  fpr._write_json_file(Path(out_dir) / 'paired_comparisons.json', payload)  # pylint: disable=protected-access
  return payload


def _write_run_summary(
    batch_dir: Path,
    view: fp.Manifest | None,
    error: BaseException | None,
    audits: Sequence[ThreeArmAuditEngine],
    *,
    secrets: Sequence[str],
) -> dict[str, Any]:
  """Closes the C-2R run uniformly: rebuild, tables, pairs, summary.

  On every path — success, budget stop or fault — the bookkeeping is
  rebuilt from the persisted artifacts alone, the preregistered
  evaluation tables and the paired comparisons are written from that
  rebuild, and ``run_summary.json`` records the fault, both layers'
  counts, the three-arm audit log and the completion timestamp, all
  scrubbed through ``request_ledger.redact``. A fault that predates
  any ledger artifact gets a summary with the fault alone; nothing is
  rerun to fill the rest.

  Args:
    batch_dir: the batch directory the run wrote into.
    view: the compatibility view, when the run got that far.
    error: the exception that stopped the run, if any.
    audits: the audit engines composed into the run (first one's log
      is summarized).
    secrets: literal values replaced with ``[REDACTED]``.

  Returns:
    The summary (also written as ``run_summary.json``).
  """
  fault: dict[str, Any] | None = None
  ledger_counts = None
  sdk_counts = None
  rubric_counts = None
  pairs = None
  if (batch_dir / 'execution_ledger.jsonl').is_file():
    report = fp.rebuild_batch_report(batch_dir)
    fault = report.fault
    ledger_counts = report.ledger_counts
    sdk_counts = report.sdk_counts
    if view is not None:
      rubric_counts = fp.write_rubric_report(
          view, report, batch_dir, secrets=secrets
      )['counts']
      pairs = write_paired_report(report, batch_dir, secrets=secrets)
  if fault is None and error is not None:
    fault = {
        'kind': 'batch_fault',
        'error': rl.redact(f'{type(error).__name__}: {error}', secrets),
    }
  summary = {
      'fault': fault,
      'ledger_counts': ledger_counts,
      'sdk_counts': sdk_counts,
      'rubric_counts': rubric_counts,
      'paired_comparisons': pairs,
      'three_arm_audits': (
          [dict(entry) for entry in audits[0].audit_log] if audits else None
      ),
      'completed_utc': fpr._utc_now(),  # pylint: disable=protected-access
  }
  fpr._write_json_file(  # pylint: disable=protected-access
      batch_dir / 'run_summary.json', rl.redact(summary, secrets)
  )
  return summary


class _PreflightStubModel(language_model.LanguageModel):
  """Offline No stub carrying the probe-model run protocol.

  Routes on the harness's own production stage marks and answers the
  judgment with No (parsing the presented option block), so the four
  preflight executions run the full repetition branch with zero
  network. ``bind``/``take_run_log`` mirror :class:`fp.ProbeModel`'s
  protocol (run_unit's gate check reads the run log); nothing is
  billed anywhere — the stub writes no ledger and no trace.
  """

  def __init__(self) -> None:
    self.call_log: list[dict[str, Any]] = []
    self._run_start = 0

  @property
  def model_name(self) -> str:
    return 'stub'

  def bind(self, group: str, stage_label: str) -> None:
    del group, stage_label
    self._run_start = len(self.call_log)

  def take_run_log(self) -> list[dict[str, Any]]:
    return self.call_log[self._run_start :]

  def take_run_events(self) -> list[dict[str, Any]]:
    return []

  def sample_text(self, prompt: str, **kwargs: Any) -> str:
    del kwargs
    stage = (
        'suggestion'
        if 'Suggest five plausible events' in prompt
        else 'fusion'
        if 'How does 2 modify' in prompt
        else 'request'
    )
    self.call_log.append({
        'slot': f'preflight-text{len(self.call_log):06d}',
        'group': '',
        'stage': stage,
        'prompt': prompt,
    })
    if stage == 'suggestion':
      return '线索一\n线索二\n线索三\n线索四\n线索五'
    return '融合后的复合事件。'

  def sample_choice(
      self, prompt: str, responses: Sequence[str], *, seed: int | None = None
  ) -> tuple[int, str, Any]:
    del seed
    self.call_log.append({
        'slot': f'preflight-choice{len(self.call_log):06d}',
        'group': '',
        'stage': 'judgment',
        'prompt': prompt,
    })
    match = re.search(r'^  \((\w+)\) No\s*$', prompt, re.MULTILINE)
    assert match is not None, 'no No option presented'
    letter = match.group(1)
    return list(responses).index(letter), letter, {}


def preflight_stage(batch_dir: Path | str) -> dict[str, Any]:
  """Produces the zero-network preflight evidence (contract B).

  Runs four rep-1 stub executions off the batch's own self-contained
  manifest copy — BASE under the disclosure speech, BASE under the
  control speech, DISC and WAIT — on the offline No stub, with the
  before-state code materialized in a temporary directory. The four
  captured judgment prompts and the assertion facts land in the
  batch's ``preflight/`` subdirectory only: no ledger, no run trace
  and no one-time marker is ever created (the first real run's guard
  stays unoccupied), and nothing outside ``preflight/`` is written.

  Args:
    batch_dir: the frozen batch directory (``manifest.json`` present).

  Returns:
    The written facts (also ``preflight/preflight-facts.json``).

  Raises:
    fp.BatchFault: if the batch manifest is missing or the preflight
      target already exists.
  """
  batch_dir = Path(batch_dir)
  manifest_path = batch_dir / 'manifest.json'
  if not manifest_path.is_file():
    raise fp.BatchFault(
        f'preflight needs a frozen batch: {manifest_path} is missing'
    )
  # An executed (or interrupted) batch directory is frozen forever —
  # the same refusal the run guard applies, so preflight never writes
  # into a directory a run already touched.
  for name in _RUN_TRACE_NAMES:
    if (batch_dir / name).exists():
      raise fp.BatchFault(
          f'refusing to preflight {batch_dir}: {name} is already present;'
          ' an executed or interrupted batch directory never changes.'
      )
  preflight_dir = batch_dir / 'preflight'
  if preflight_dir.exists():
    raise fp.BatchFault(f'preflight target already exists: {preflight_dir}')
  manifest = c2r.load_c2r_manifest(manifest_path)
  view = c2r.build_compat_view(manifest)
  control_view = c2r.build_compat_view(
      manifest,
      base_speech=c2r.c2r_fixture_value(manifest, 'speech_control'),
  )
  stub = _PreflightStubModel()
  preflight_dir.mkdir()
  with tempfile.TemporaryDirectory() as tmp:
    work = Path(tmp)
    states = fp.load_code_states(view, work / 'code_states')
    runs: dict[str, dict[str, Any]] = {}
    base_prompt: str | None = None
    for name, source_view, unit, condition in (
        ('before-disclosure', view, 'C-2R-BASE', 'before'),
        ('before-waiting', control_view, 'C-2R-BASE', 'before'),
        ('after-disclosure', view, 'C-2R-DISC', 'after'),
        ('after-waiting', view, 'C-2R-WAIT', 'after'),
    ):
      # Historical replay: the after arms' judgment prompts are the
      # archived original-contract recordings, reconstructed
      # (pin-verified) from the before-disclosure base prompt exactly
      # as run_batch does in the frozen order.
      recorded = None
      if condition == 'after':
        assert base_prompt is not None
        recorded = fp.recorded_after_prompt(unit, condition, 1, base_prompt)
      record = fp.run_unit(
          source_view,
          unit,
          condition,
          1,
          states[condition],
          stub,
          work,
          recorded_replay_prompt=recorded,
          replay_base_prompt=base_prompt if recorded else None,
      )
      runs[name] = record
      if name == 'before-disclosure':
        base_prompt = record['controlled_prompt']
      (preflight_dir / f'{name}.prompt.txt').write_text(
          record['controlled_prompt'] or '', encoding='utf-8'
      )
  before_disclosure = runs['before-disclosure']['controlled_prompt'] or ''
  before_waiting = runs['before-waiting']['controlled_prompt'] or ''
  after_disclosure = runs['after-disclosure']['controlled_prompt'] or ''
  after_waiting = runs['after-waiting']['controlled_prompt'] or ''
  findings = c2r.audit_controlled_differences(
      before_disclosure, after_disclosure, after_waiting
  )
  intent = c2r.c2r_fixture_value(manifest, 'action_intent')
  candidate = c2r.c2r_fixture_value(manifest, 'candidate')
  prefix = intent + ' Because of that, '
  fingerprints = dict(manifest.fixtures['C-2R']['fingerprints'])
  recomputed = {
      'speech': c2r.c2r_fixture_value(manifest, 'speech_disclosure'),
      'speech_control': c2r.c2r_fixture_value(manifest, 'speech_control'),
      'action_intent': intent,
      'candidate': candidate,
      'result': candidate[len(prefix) :],
  }
  stage_keys = [
      f'{unit}|{condition}|r{rep}|{stage}'
      for rep in view.order['rounds']
      for unit, condition in view.order['round_sequence']
      for stage in view.units[unit]['stages']
  ]
  facts = {
      'schema': 'fixed-probe-c2r-preflight-v1',
      'rep': 1,
      'gate_opened': {
          name: bool(record['controlled_prompt'] and record['rng_proofs'])
          for name, record in runs.items()
      },
      'fingerprints': fingerprints,
      'fingerprints_match_manifest': all(
          hashlib.sha256(value.encode('utf-8')).hexdigest()
          == fingerprints[field]
          for field, value in recomputed.items()
      ),
      'byte_relations': {
          'before_lacks_contribution_zone': (
              '## Current player contribution' not in before_disclosure
              and '## Current player contribution' not in before_waiting
          ),
          'after_prompts_differ_only_in_speech_line': (
              after_disclosure != after_waiting and not findings
          ),
          'after_equals_before_plus_zone': not findings,
          'audit_findings': findings,
      },
      'before_dual_speech_identical': before_disclosure == before_waiting,
      'stage_keys': {'count': len(stage_keys), 'keys': stage_keys},
  }
  fpr._write_json_file(preflight_dir / 'preflight-facts.json', facts)  # pylint: disable=protected-access
  return facts


def _engine_wrapper_factory(track: list[ThreeArmAuditEngine]):
  """Builds the run's model wrapper: audit engine above retry wrapper."""

  def _wrapper(probe_model: fp.ProbeModel) -> ThreeArmAuditEngine:
    engine = ThreeArmAuditEngine(fpr._RetryingProbeModel(probe_model))  # pylint: disable=protected-access
    track.append(engine)
    return engine

  return _wrapper


def run_stage(
    batch_dir: Path | str,
    *,
    env_path: Path | str | None = None,
    model_factory: Callable[..., language_model.LanguageModel] | None = None,
    reextract_fixtures: bool = True,
) -> dict[str, Any]:
  """Runs the frozen C-2R batch exactly once, preserving every artifact.

  Order is strict and mirrors the source protocol's run stage: a
  one-time guard occupies the directory atomically before any
  credential is read or client constructed; the verified freeze then
  supplies the per-attempt integrity monitor (the inherited groups
  plus the batch-local file group); the environment file supplies the
  credentials; the factory builds the inner model under the frozen
  identity; and ``fp.run_batch`` executes the view with the engine
  composed as three-arm audit wrapper above the production retry
  wrapper above the probe's two ledger layers, under the C-2R caps
  (27/81 with the single C group). Whatever happens, nothing reruns:
  the bookkeeping is rebuilt from disk, the rubric tables and paired
  comparisons are written, the summary is scrubbed, and the original
  exception propagates — redacted into a :class:`fp.BatchFault`
  whenever it carries a secret.

  Args:
    batch_dir: the frozen batch directory to run in.
    env_path: the environment file with the credentials; defaults to
      the repository ``.env`` (real run only).
    model_factory: callable ``(model_name, *, api_key, api_base)``
      building the inner model; defaults to the production adapter.
    reextract_fixtures: passed through to :func:`verify_stage`.

  Returns:
    The run summary (also written as ``run_summary.json``).

  Raises:
    fp.BatchFault: on any refused re-entry, missing freeze, freeze
      drift, mid-run integrity failure (including a failed three-arm
      controlled-difference audit) or secret-carrying fault.
  """
  batch_dir = Path(batch_dir)
  # (1) One-time guard, before any credential operation.
  freeze = _load_freeze(batch_dir)
  for name in _RUN_TRACE_NAMES:
    if (batch_dir / name).exists():
      raise fp.BatchFault(
          f'refusing to run {batch_dir}: {name} is already present; a'
          ' directory that was executed or interrupted is never run'
          ' again, and its ledgers and reports never change.'
      )
  try:
    marker_fd = os.open(
        batch_dir / 'run_started.json',
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o644,
    )
  except FileExistsError as error:
    raise fp.BatchFault(
        f'concurrent run of {batch_dir}: run_started.json already exists'
    ) from error
  with os.fdopen(marker_fd, 'w', encoding='utf-8') as handle:
    json.dump({'created_utc': fpr._utc_now(), 'pid': os.getpid()}, handle, indent=2)  # pylint: disable=protected-access
    handle.write('\n')
  manifest = None
  view = None
  api_key = ''
  audits: list[ThreeArmAuditEngine] = []
  try:
    # (2) Freeze verification and the per-attempt integrity monitor.
    verify_stage(batch_dir, reextract_fixtures=reextract_fixtures)
    monitor = C2RIntegrityMonitor(freeze, batch_dir)
    # (3) Credentials: the repository default exists for the real run.
    source = env_path if env_path is not None else _REPO_ROOT / '.env'
    api_key, api_base = fpr._load_settings(source)  # pylint: disable=protected-access
    # (4) The inner model under the frozen identity.
    factory = (
        model_factory if model_factory is not None else fpr._default_model_factory  # pylint: disable=protected-access
    )
    inner = factory(_MODEL_NAME, api_key=api_key, api_base=api_base)
    manifest = c2r.load_c2r_manifest(batch_dir / 'manifest.json')
    view = c2r.build_compat_view(manifest)
    # (5) The composed run: audit engine outside the retry wrapper,
    # retry wrapper outside the probe's two ledger layers, integrity
    # gate before every attempt and create, C-2R caps from the
    # manifest (the authority), the engine's built-in pair audit
    # pinned to zero firings.
    report = fp.run_batch(
        view,
        inner,
        batch_dir,
        attempts_global_cap=(
            manifest.budget['concordia_attempts']['global_cap']
        ),
        attempts_group_caps={
            'C': manifest.budget['concordia_attempts']['global_cap']
        },
        sdk_create_cap=manifest.budget['sdk_create']['global_cap'],
        secrets=[api_key],
        model_wrapper=_engine_wrapper_factory(audits),
        model_tag=_MODEL_TAG,
        model_name=_MODEL_NAME,
        integrity_check=monitor.check,
    )
    c2r.assert_no_builtin_pairs(report.audit_records)
  except BaseException as error:  # pylint: disable=broad-except
    # (6) No reruns: rebuild from disk, write the tables, the paired
    # comparisons and the summary, then propagate — redacted into a
    # BatchFault if it carries a secret, unchanged otherwise.
    _write_run_summary(batch_dir, view, error, audits, secrets=[api_key])
    if api_key and api_key in str(error):
      raise fp.BatchFault(
          rl.redact(f'{type(error).__name__}: {error}', [api_key])
      ) from None
    raise
  return _write_run_summary(batch_dir, view, None, audits, secrets=[api_key])


# -----------------------------------------------------------------------------
# Entry point.
# -----------------------------------------------------------------------------

# Private flag namespace: sibling probes define absl app flags with the
# same names, and absl has no per-module namespaces.
FLAGS = flags.FlagValues()
flags.DEFINE_string(
    'stage',
    None,
    'One of: freeze, verify, preflight, run.',
    required=True,
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'batch_dir',
    None,
    'The frozen batch directory (freeze writes it; verify/run read it).',
    required=True,
    flag_values=FLAGS,
)
flags.DEFINE_string(
    'env_path',
    str(_REPO_ROOT / '.env'),
    'Environment file for the run stage (AI_MODEL_4 / AI_API_KEY);'
    ' the freeze and verify stages never read it.',
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
  batch_dir = Path(FLAGS.batch_dir).resolve()
  if stage == 'freeze':
    freeze_stage(batch_dir)
  elif stage == 'verify':
    verify_stage(batch_dir)
  elif stage == 'preflight':
    preflight_stage(batch_dir)
  elif stage == 'run':
    # The only stage that reads credentials: the env path stays a
    # flag so tests and operators never rely on a default silently.
    run_stage(batch_dir, env_path=Path(FLAGS.env_path).resolve())
  else:
    raise SystemExit(f'unknown --stage: {stage!r}')


if __name__ == '__main__':
  app.run(main, flags_parser=_parse_flags)
