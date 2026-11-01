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

r"""Real-execution driver for the fixed-sample fidelity/repetition probe.

This module wires the accepted offline harness
(``fixed_probe.py`` + ``fixed_probe_manifest.json``) for the
protocol's real execution: loading the live settings with the frozen
model identity (Sol-only), freezing the deliverables, the plan
snapshot and the execution-dependency closure into the batch
directory, verifying that freeze before a run, and running the batch
itself — behind a one-time atomic execution marker, with the
production retry wrapper composed outside the probe's two ledger
layers and with fault paths that preserve every artifact. The real
run is separately authorized (protocol §9); everything implemented
here is testable offline — zero real model calls, zero credential
reads outside the explicitly passed environment file and zero
network. Credentials travel by parameter, never through
``os.environ``: the freeze and verify stages structurally take no
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
import sys
import tempfile
from typing import Any

from absl import app
from absl import flags
from concordia.contrib.language_models.openai import gpt_model
from concordia.language_model import language_model
from concordia.language_model import retry_wrapper
import openai

from examples.story_enactment import fixed_probe as fp
from examples.story_enactment import request_ledger as rl

# Repository root (the runner lives at examples/story_enactment/).
_REPO_ROOT = Path(__file__).parents[2]

# Frozen model identity (protocol §2.2): the run may only ever address
# the Sol model recorded under this environment key.
_ENV_KEY = 'AI_MODEL_4'
_MODEL_NAME = 'gpt-6.1-sol'
_MODEL_TAG = 'm4'

# Repository paths of the freeze inputs: the data-authority manifest
# and the implementation plan whose byte copy becomes the batch's
# immutable ``plan_snapshot.md``.
_MANIFEST_PATH = (
    _REPO_ROOT / 'examples' / 'story_enactment' / 'fixed_probe_manifest.json'
)
_PLAN_DOC_PATH = (
    _REPO_ROOT
    / 'docs'
    / 'superpowers'
    / 'plans'
    / '2026-10-25-story-enactment-fixed-probe-real-execution.md'
)

# Schema identifier of the freeze record.
_FREEZE_SCHEMA = 'fixed-probe-freeze-v1'

# The single explicit set of pinned deliverable files (16 items): all
# 12 items of the v4 hash list — including the two finalized-history
# notes, which stay pinned as historical deliverables — plus this
# round's runner pair and the two production files whose before state
# the manifest pins (frozen at their current, after state). The only
# excluded documents are this round's updatable progress records: the
# repository plan document (its frozen form is the in-batch
# ``plan_snapshot.md``) and the execution-day freeze note.
_FROZEN_SOURCES = (
    (
        'docs/superpowers/notes/'
        '2026-10-24-story-enactment-fidelity-and-repetition-context.md'
    ),
    (
        'docs/superpowers/plans/'
        '2026-10-24-story-enactment-fidelity-and-repetition-context.md'
    ),
    (
        'run_results/story_enactment/s3_diagnostic_story/'
        '20261024T063748Z/review_errata.md'
    ),
    (
        'run_results/story_enactment/s3_diagnostic_story/'
        '20261024T063748Z/turn_evaluation_reviewed.md'
    ),
    (
        'docs/superpowers/plans/'
        '2026-10-24-story-enactment-fidelity-and-repetition-fixed-probe-protocol.md'
    ),
    (
        'docs/superpowers/plans/'
        '2026-10-25-story-enactment-fixed-probe-fixture-manifest.md'
    ),
    (
        'docs/superpowers/plans/'
        '2026-10-25-story-enactment-fixed-probe-implementation.md'
    ),
    'examples/story_enactment/fixed_probe_manifest.json',
    'examples/story_enactment/fixed_probe.py',
    'examples/story_enactment/fixed_probe_test.py',
    'examples/story_enactment/README.md',
    (
        'docs/superpowers/notes/'
        '2026-10-25-story-enactment-fixed-probe-review-fixes.md'
    ),
    'examples/story_enactment/fixed_probe_runner.py',
    'examples/story_enactment/fixed_probe_runner_test.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/repetition_push.py',
)


def _load_settings(env_path: Path | str) -> tuple[str, str]:
  """Loads the live credentials and validates the frozen model identity.

  Line-by-line hand parsing with the same format rules as
  ``s3_diagnostic_story._load_settings``: blank lines and ``#``
  comments are skipped, each entry is ``key=value``, and surrounding
  whitespace plus matching single or double quotes are stripped from
  the value. The loader never writes ``os.environ`` — the key travels
  by return value so the freeze/verify stages (which take no
  environment path at all) stay structurally credential-free.

  Args:
    env_path: the environment file to read; the repository ``.env``
      default is supplied only by the real run stage, never in tests.

  Returns:
    ``(api_key, api_base)``.

  Raises:
    fp.BatchFault: if ``AI_MODEL_4`` does not resolve to the frozen
      model name or ``AI_API_KEY`` is missing.
  """
  text = Path(env_path).read_text(encoding='utf-8', errors='replace')
  values: dict[str, str] = {}
  for line in text.splitlines():
    stripped = line.strip()
    if not stripped or stripped.startswith('#') or '=' not in stripped:
      continue
    key, _, value = stripped.partition('=')
    values[key.strip()] = value.strip().strip('"').strip("'")
  model = values.get(_ENV_KEY, '')
  if model != _MODEL_NAME:
    raise fp.BatchFault(
        f'{_ENV_KEY} resolves to {model!r}, expected {_MODEL_NAME!r};'
        ' refusing to run the batch against a different model.'
    )
  api_key = values.get('AI_API_KEY', '')
  if not api_key:
    raise fp.BatchFault('AI_API_KEY is missing from the environment file.')
  return api_key, values.get('AI_BASE_URL', '')


# ---------------------------------------------------------------------------
# Freeze stage: deliverables, plan snapshot, execution-dependency closure
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
  """Returns the hex sha256 of one file's bytes."""
  return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _pinned_sha256(rel: str) -> str:
  """Hashes one pinned repository file; missing is a batch fault."""
  path = _REPO_ROOT / rel
  if not path.is_file():
    raise fp.BatchFault(f'frozen source missing from the working tree: {rel}')
  return _sha256_file(path)


def _write_json_file(path: Path, payload: Any) -> None:
  """Writes indented JSON with a trailing newline."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


class _ClosureStubModel(language_model.LanguageModel):
  """Offline fingerprint-routed stub driving the closure-enumeration batch.

  Routes on the harness's own production stage marks (the same marks
  ``ProbeModel`` classifies stages by), returning well-formed outputs
  so the full frozen order completes with zero real calls — the run
  exists only to trigger every delayed import of the real execution
  path inside this process.
  """

  def sample_text(self, prompt: str, **kwargs: Any) -> str:
    del kwargs
    for mark, stage in fp._STAGE_MARKS:  # pylint: disable=protected-access
      if mark in prompt:
        break
    else:
      raise AssertionError(f'unrouted closure prompt:\n{prompt[:400]}')
    if stage == 'generation':
      return json.dumps(
          {'stage_directions': '周律师逐页查阅了原件全文的落款与签署。'},
          ensure_ascii=False,
      )
    if stage == 'review':
      return json.dumps({'valid': True, 'reason': '合法。'}, ensure_ascii=False)
    if stage == 'suggestion':
      return '线索一\n线索二\n线索三\n线索四\n线索五'
    return '融合后的复合事件。'

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Mapping[str, Any]]:
    del seed
    match = re.search(r'^  \((\w+)\) No\s*$', prompt, re.MULTILINE)
    if match is None:
      raise AssertionError('closure stub: no No option presented')
    letter = match.group(1)
    return list(responses).index(letter), letter, {}


def _execution_dependency_closure(
    manifest: fp.Manifest, batch_dir: Path
) -> dict[str, str]:
  """Enumerates the execution dependency closure by running one stub batch.

  Imports this runner, the harness, both alias-loaded code states and
  the real-run composition modules (the production OpenAI adapter and
  the retry wrapper the offline batch never loads), then executes the
  complete frozen order on the offline stub model into a temporary
  directory — zero real calls, existing only to trigger every delayed
  import — and finally records the sha256 of every ``sys.modules``
  entry whose file lives under the repository root. ``run_results/``,
  the batch directory itself and the two manifest-pinned production
  files (already pinned by the manifest's before-state hashes) are
  excluded; everything else — core helper, director, document tools,
  adapter, retry wrapper, ledger — is pinned at its current state.

  Args:
    manifest: the data authority.
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
    states = fp.load_code_states(manifest, work / 'code_states')
    report = fp.run_batch(
        manifest,
        _ClosureStubModel(),
        work / 'probe',
        code_states=states,
    )
    if report.fault is not None:
      raise fp.BatchFault(
          f'closure-enumeration stub batch faulted: {report.fault}'
      )
  return _repo_module_hashes(manifest, batch_dir)


def _repo_module_hashes(
    manifest: fp.Manifest, batch_dir: Path
) -> dict[str, str]:
  """Hashes the repository-root modules currently loaded in-process."""
  repo_root = _REPO_ROOT.resolve()
  batch_resolved = batch_dir.resolve()
  pinned_before = {
      (_REPO_ROOT / rel).resolve()
      for rel in manifest.code_states['before']['files']
  }
  hashes: dict[str, str] = {}
  for module in sys.modules.values():
    file_name = getattr(module, '__file__', None)
    if not file_name or not file_name.endswith('.py'):
      continue
    resolved = Path(file_name).resolve()
    if repo_root not in resolved.parents:
      continue
    rel = resolved.relative_to(repo_root)
    if rel.parts[0] == 'run_results':
      continue
    if resolved == batch_resolved or batch_resolved in resolved.parents:
      continue
    if resolved in pinned_before:
      continue
    hashes[rel.as_posix()] = _sha256_file(resolved)
  return dict(sorted(hashes.items()))


def _frozen_source_hashes(manifest: fp.Manifest) -> dict[str, str]:
  """Hashes the pinned deliverable set, cross-checked with the manifest."""
  derived = set(manifest.code_states['before']['files'])
  explicit = set(_FROZEN_SOURCES)
  missing = sorted(derived - explicit)
  if missing:
    raise fp.BatchFault(
        f'manifest before-state files not covered by _FROZEN_SOURCES: {missing}'
    )
  return {rel: _pinned_sha256(rel) for rel in _FROZEN_SOURCES}


def freeze_stage(batch_dir: Path | str) -> dict[str, Any]:
  """Freezes the batch's immutable execution inputs (offline, no creds).

  Refuses an existing target, then writes four artifacts: the byte
  copy of the data-authority manifest, the byte copy of the
  implementation plan (the batch's immutable execution spec — the
  repository copy stays a updatable progress record outside every
  hash set), the execution-dependency closure, and the freeze record
  binding them all by measured sha256. The whole stage is
  credential-free by construction.

  Args:
    batch_dir: the freeze target directory; must not exist.

  Returns:
    The freeze record (also written as ``freeze.json``).

  Raises:
    fp.BatchFault: if the target exists or any pinned source is
      missing from the working tree.
  """
  batch_dir = Path(batch_dir)
  if batch_dir.exists():
    raise fp.BatchFault(f'freeze target already exists: {batch_dir}')
  manifest = fp.load_manifest(_MANIFEST_PATH)
  source_hashes = _frozen_source_hashes(manifest)
  batch_dir.mkdir(parents=True)
  (batch_dir / 'manifest.json').write_bytes(_MANIFEST_PATH.read_bytes())
  (batch_dir / 'plan_snapshot.md').write_bytes(_PLAN_DOC_PATH.read_bytes())
  closure = _execution_dependency_closure(manifest, batch_dir)
  _write_json_file(batch_dir / 'dependency_hashes.json', closure)
  budget = manifest.raw['budget']
  freeze: dict[str, Any] = {
      'schema': _FREEZE_SCHEMA,
      'created_utc': (
          datetime.datetime.now(datetime.timezone.utc).strftime(
              '%Y%m%dT%H%M%SZ'
          )
      ),
      'batch_dir': str(batch_dir),
      'manifest_sha256': _sha256_file(batch_dir / 'manifest.json'),
      'plan_snapshot_sha256': _sha256_file(batch_dir / 'plan_snapshot.md'),
      'dependency_hashes_sha256': _sha256_file(
          batch_dir / 'dependency_hashes.json'
      ),
      'protocol_sha256': manifest.protocol_sha256,
      'fixture_manifest_doc_sha256': manifest.fixture_manifest_doc_sha256,
      'budget': {
          'attempts_global_cap': budget['concordia_attempts']['global_cap'],
          'attempts_group_caps': budget['concordia_attempts']['group_caps'],
          'sdk_create_cap': budget['sdk_create']['global_cap'],
          'http_upper_bound_disclosure': budget['http_upper_bound_disclosure'],
      },
      'model': {'tag': _MODEL_TAG, 'name': _MODEL_NAME, 'env_key': _ENV_KEY},
      'source_hashes': source_hashes,
  }
  _write_json_file(batch_dir / 'freeze.json', freeze)
  return freeze


# ---------------------------------------------------------------------------
# Verify stage and the per-forwarding integrity monitor
# ---------------------------------------------------------------------------


def _require_pinned_hash(path: Path, pinned: str, label: str) -> None:
  """Raises a batch fault unless the file exists with the pinned hash."""
  if not path.is_file():
    raise fp.BatchFault(f'{label} missing: {path}')
  actual = _sha256_file(path)
  if actual != pinned:
    raise fp.BatchFault(
        f'{label} drift: {path} has {actual}, freeze pins {pinned}'
    )


def _check_tree_hashes(hashes: Mapping[str, str], label: str) -> None:
  """Recomputes one repository-relative hash table against the tree."""
  for rel, pinned in hashes.items():
    _require_pinned_hash(_REPO_ROOT / rel, pinned, f'{label} {rel}')


def _load_freeze(batch_dir: Path) -> dict[str, Any]:
  """Loads the freeze record, refusing absence and schema drift."""
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


def verify_stage(
    batch_dir: Path | str, *, reextract_fixtures: bool = True
) -> dict[str, Any]:
  """Verifies the whole freeze against the current tree before a run.

  Checks, in order: the freeze record is present with the expected
  schema; the in-batch manifest copy and plan snapshot match their
  freeze-time hashes; the dependency hash manifest matches its own
  pinned hash — verified strictly before any of its mappings are
  used — and then every ``source_hashes`` and ``dependency_hashes``
  entry is recomputed against the working tree (a missing file is a
  failure); finally, unless ``reextract_fixtures`` is false (the CI
  stand-in without the frozen source batch), the manifest's fixtures
  are re-extracted byte-for-byte from the source batch.

  Args:
    batch_dir: the frozen batch directory.
    reextract_fixtures: whether to re-extract the batch-derived
      fixtures from the frozen source batch.

  Returns:
    A verification summary (schema, file counts, re-extraction mode).

  Raises:
    fp.BatchFault: on any absence, schema or hash disagreement.
  """
  batch_dir = Path(batch_dir)
  freeze = _load_freeze(batch_dir)
  _require_pinned_hash(
      batch_dir / 'manifest.json',
      freeze['manifest_sha256'],
      'frozen input manifest.json',
  )
  _require_pinned_hash(
      batch_dir / 'plan_snapshot.md',
      freeze['plan_snapshot_sha256'],
      'frozen input plan_snapshot.md',
  )
  _require_pinned_hash(
      batch_dir / 'dependency_hashes.json',
      freeze['dependency_hashes_sha256'],
      'dependency hash manifest',
  )
  closure = json.loads(
      (batch_dir / 'dependency_hashes.json').read_text(encoding='utf-8')
  )
  _check_tree_hashes(freeze['source_hashes'], 'frozen source')
  _check_tree_hashes(closure, 'dependency closure')
  summary: dict[str, Any] = {
      'schema': freeze['schema'],
      'batch_dir': str(batch_dir),
      'source_files': len(freeze['source_hashes']),
      'dependency_files': len(closure),
      'fixture_reextraction': 'skipped',
  }
  if reextract_fixtures:
    manifest = fp.load_manifest(batch_dir / 'manifest.json')
    try:
      fp.verify_manifest(manifest, _REPO_ROOT / manifest.batch_root)
    except fp.ManifestError as error:
      raise fp.BatchFault(f'fixture re-extraction failed: {error}') from error
    summary['fixture_reextraction'] = 'verified'
  return summary


class DependencyIntegrityMonitor:
  """Re-verifies the three frozen groups before every forwarding.

  The watched groups are: the execution dependency closure
  (``dependency_hashes.json``, itself hash-checked strictly before its
  mappings are used), the pinned deliverable files
  (``source_hashes``), and the batch's frozen input files —
  ``manifest.json``, ``plan_snapshot.md`` and ``dependency_hashes.json``
  — pinned at their freeze-time measured hashes. ``check()``
  re-hashes all three groups in full; any disagreement raises
  :class:`fp.BatchFault`. A run injects ``check`` as the harness's
  ``integrity_check`` so it runs before every attempt and every SDK
  create.
  """

  def __init__(self, freeze: Mapping[str, Any], batch_dir: Path | str):
    self._freeze = dict(freeze)
    self._batch_dir = Path(batch_dir)

  def check(self) -> None:
    """Re-hashes the three groups; raises on any drift or absence."""
    for name, key in (
        ('manifest.json', 'manifest_sha256'),
        ('plan_snapshot.md', 'plan_snapshot_sha256'),
        ('dependency_hashes.json', 'dependency_hashes_sha256'),
    ):
      _require_pinned_hash(
          self._batch_dir / name,
          self._freeze[key],
          f'frozen input {name}',
      )
    closure = json.loads(
        (self._batch_dir / 'dependency_hashes.json').read_text(encoding='utf-8')
    )
    _check_tree_hashes(closure, 'dependency closure')
    _check_tree_hashes(self._freeze['source_hashes'], 'frozen source')


# ---------------------------------------------------------------------------
# Run stage: one-time guard, composition and fault handling
# ---------------------------------------------------------------------------

# Files whose presence proves the directory already carries a run —
# completed or interrupted: both ledgers' first artifacts, the
# engine's persisted bookkeeping, and the one-time marker itself.
_RUN_TRACE_NAMES = (
    'requests.jsonl',
    'sdk_ledger',
    'execution_ledger.jsonl',
    'stage_states.json',
    'run_started.json',
)


class _RetryingProbeModel(retry_wrapper.RetryLanguageModel):
  """The production retry wrapper composed as the probe's engine model.

  Only the probe-protocol members are appended: ``bind``,
  ``take_run_log`` and ``take_run_events`` forward to the wrapped
  :class:`fp.ProbeModel` and ``model_name`` exposes its identity,
  while both sampling methods keep the frozen retry shape (protocol
  §2.2: at most three tries of the three whitelisted transient
  provider errors, fixed zero delay, no backoff). The wrapper sits
  strictly outside the probe, so every retried forwarding is a new
  billed attempt through both ledger layers.
  """

  def __init__(self, probe_model: fp.ProbeModel) -> None:
    super().__init__(
        probe_model,
        retry_on_exceptions=(
            openai.APIConnectionError,
            openai.APITimeoutError,
            openai.RateLimitError,
        ),
        retry_tries=3,
        retry_delay=0.0,
        jitter=(0.0, 0.0),
        exponential_backoff=False,
    )
    self._probe = probe_model

  @property
  def model_name(self) -> str:
    return self._probe.model_name

  def bind(self, group: str, stage_label: str) -> None:
    self._probe.bind(group, stage_label)

  def take_run_log(self) -> list[dict[str, Any]]:
    return self._probe.take_run_log()

  def take_run_events(self) -> list[dict[str, Any]]:
    return self._probe.take_run_events()


def _default_model_factory(
    model_name: str, *, api_key: str, api_base: str
) -> language_model.LanguageModel:
  """The real-run adapter: the production OpenAI-compatible client."""
  return gpt_model.GptLanguageModel(
      model_name, api_key=api_key, api_base=api_base or None
  )


def _utc_now() -> str:
  """Returns the current UTC time in the summary timestamp format."""
  return datetime.datetime.now(
      datetime.timezone.utc,
  ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _write_run_summary(
    batch_dir: Path,
    manifest: fp.Manifest | None,
    error: BaseException | None,
    *,
    secrets: Sequence[str],
) -> dict[str, Any]:
  """Closes the run uniformly: rebuild, rubric, summary; never reruns.

  On every path — success, budget stop or fault — the bookkeeping is
  rebuilt from the persisted artifacts alone (the on-disk record is
  the authority), the preregistered evaluation tables are written from
  that rebuild, and ``run_summary.json`` records the fault, both
  layers' counts, the evaluation-table counts and the completion
  timestamp, all scrubbed through ``request_ledger.redact``. A fault
  that predates any ledger artifact (a refused identity, a failed
  verify) gets a summary with the fault alone; nothing is rerun to
  fill the rest.

  Args:
    batch_dir: the batch directory the run wrote into.
    manifest: the loaded frozen manifest, when the run got that far.
    error: the exception that stopped the run, if any.
    secrets: literal values replaced with ``[REDACTED]``.

  Returns:
    The summary (also written as ``run_summary.json``).
  """
  fault: dict[str, Any] | None = None
  ledger_counts = None
  sdk_counts = None
  rubric_counts = None
  if (batch_dir / 'execution_ledger.jsonl').is_file():
    report = fp.rebuild_batch_report(batch_dir)
    fault = report.fault
    ledger_counts = report.ledger_counts
    sdk_counts = report.sdk_counts
    if manifest is not None:
      rubric_counts = fp.write_rubric_report(
          manifest, report, batch_dir, secrets=secrets
      )['counts']
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
      'completed_utc': _utc_now(),
  }
  _write_json_file(batch_dir / 'run_summary.json', rl.redact(summary, secrets))
  return summary


def run_stage(
    batch_dir: Path | str,
    *,
    env_path: Path | str | None = None,
    model_factory: Callable[..., language_model.LanguageModel] | None = None,
    reextract_fixtures: bool = True,
) -> dict[str, Any]:
  """Runs the frozen batch exactly once, preserving every artifact.

  Order is strict: a one-time guard occupies the directory atomically
  (``os.open`` with ``O_CREAT|O_EXCL``) before any credential is read
  or client constructed — a directory holding any run trace (or an
  interrupted marker) is refused with zero writes, and of concurrent
  starters exactly one loses to the marker. The verified freeze then
  supplies the per-attempt integrity monitor, the environment file
  supplies the credentials (the repository ``.env`` default exists
  only for the real run; tests always pass a temporary file), and the
  factory — the production OpenAI adapter by default — builds the
  inner model under the frozen identity. ``fp.run_batch`` composes it
  behind the probe's two ledger layers with the production retry
  wrapper outside them. Whatever happens, nothing reruns: the
  bookkeeping is rebuilt from disk, the rubric tables are written, the
  summary is scrubbed, and the original exception propagates — its
  text redacted into a :class:`fp.BatchFault` whenever it carries a
  secret.

  Args:
    batch_dir: the frozen batch directory to run in.
    env_path: the environment file with the credentials; defaults to
      the repository ``.env`` (real run only).
    model_factory: callable ``(model_name, *, api_key, api_base)``
      building the inner model; defaults to the production
      ``GptLanguageModel`` adapter.
    reextract_fixtures: passed through to :func:`verify_stage`.

  Returns:
    The run summary (also written as ``run_summary.json``).

  Raises:
    fp.BatchFault: on any refused re-entry, missing freeze, freeze
      drift, mid-run integrity failure or secret-carrying fault.
  """
  batch_dir = Path(batch_dir)
  # (1) One-time guard, before any credential operation: the directory
  # must be a frozen batch that no run (completed or interrupted) has
  # touched, and exactly one starter may occupy the marker.
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
    json.dump({'created_utc': _utc_now(), 'pid': os.getpid()}, handle, indent=2)
    handle.write('\n')
  manifest = None
  api_key = ''
  try:
    # (2) Freeze verification and the per-attempt integrity monitor.
    verify_stage(batch_dir, reextract_fixtures=reextract_fixtures)
    monitor = DependencyIntegrityMonitor(freeze, batch_dir)
    # (3) Credentials: the repository default exists for the real run.
    source = env_path if env_path is not None else _REPO_ROOT / '.env'
    api_key, api_base = _load_settings(source)
    # (4) The inner model under the frozen identity.
    factory = (
        model_factory if model_factory is not None else _default_model_factory
    )
    inner = factory(_MODEL_NAME, api_key=api_key, api_base=api_base)
    manifest = fp.load_manifest(batch_dir / 'manifest.json')
    # (5) The composed run: probe ledger layers inside, production
    # retry outside, integrity gate before every attempt and create.
    fp.run_batch(
        manifest,
        inner,
        batch_dir,
        secrets=[api_key],
        model_wrapper=_RetryingProbeModel,
        model_tag=_MODEL_TAG,
        model_name=_MODEL_NAME,
        integrity_check=monitor.check,
    )
  except BaseException as error:  # pylint: disable=broad-except
    # (6) No reruns: rebuild from disk, write the tables and summary,
    # then propagate — redacted into a BatchFault if it carries a
    # secret, unchanged otherwise.
    _write_run_summary(batch_dir, manifest, error, secrets=[api_key])
    if api_key and api_key in str(error):
      raise fp.BatchFault(
          rl.redact(f'{type(error).__name__}: {error}', [api_key])
      ) from None
    raise
  return _write_run_summary(batch_dir, manifest, None, secrets=[api_key])


# -----------------------------------------------------------------------------
# Entry point.
# -----------------------------------------------------------------------------

# Private flag namespace: sibling probes define absl app flags with the
# same names, and absl has no per-module namespaces.
FLAGS = flags.FlagValues()
flags.DEFINE_string(
    'stage',
    None,
    'One of: freeze, verify, run.',
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
  elif stage == 'run':
    # The only stage that reads credentials: the env path stays a
    # flag so tests and operators never rely on a default silently.
    run_stage(batch_dir, env_path=Path(FLAGS.env_path).resolve())
  else:
    raise SystemExit(f'unknown --stage: {stage!r}')


if __name__ == '__main__':
  app.run(main, flags_parser=_parse_flags)
