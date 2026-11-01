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

r"""Offline harness for the fixed-sample fidelity/repetition probe.

This module implements the 2026-10-24 fixed-sample verification
protocol (``docs/superpowers/plans/
2026-10-24-story-enactment-fidelity-and-repetition-fixed-probe-protocol.md``)
as an offline harness: frozen fixtures from the S3 batch, paired seeds
for both RNG layers, dual code-state loading with a byte-identity
dependency audit, an execution engine with the protocol's ordering,
four-state stage bookkeeping and unit-level failure rules, a zero-
network dry run and the preregistered report skeleton. It never
contacts a provider, never reads credentials and never creates a real
batch directory; real execution is separately authorized per protocol
§9 and only then swaps the injected in-process stub model for the live
one.

The single data authority is ``fixed_probe_manifest.json`` (the
machine-readable form of the reviewed fixture manifest); this section
of the module loads and verifies it, including byte-level re-extraction
of every batch-derived fixture when a batch directory is supplied.
"""

import ast
from collections.abc import Callable, Collection, Mapping, Sequence
import contextlib
import copy as copylib
import dataclasses
import enum
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import re
import sys
from types import ModuleType
from typing import Any, cast

from concordia.document import interactive_document
from concordia.language_model import language_model
import numpy as np

# Schema identifier of the data-authority JSON.
MANIFEST_SCHEMA = 'fixed-probe-manifest-v1'

# The seven preregistered units of protocol §3/§4.
_UNIT_IDS = ('B-1', 'B-2', 'B-3', 'B-4', 'C-1', 'C-2', 'C-3')

# Condition labels: B-2/3/4 run current production only, the paired
# units run the frozen before state and the current after state.
_PAIRED_UNITS = ('B-1', 'C-1', 'C-2', 'C-3')
_CURRENT_ONLY_UNITS = ('B-2', 'B-3', 'B-4')

# Frozen stage-state vocabulary of protocol §7 (Task 5's ``StageState``
# enum must match this list; see ``verify_manifest``).
_STAGE_STATES = (
    'STARTED',
    'FINISHED',
    'FAILED',
    'PENDING',
    'NOT_STARTED_ORDER',
    'NOT_STARTED_BUDGET',
    'SKIPPED_UPSTREAM',
    'NOT_APPLICABLE',
)


class StageState(enum.Enum):
  """The four-state bookkeeping vocabulary of protocol §7.

  ``STARTED`` covers the started sub-states observed on return
  (``FINISHED``/``FAILED``; ``PENDING`` is the crashed-in-flight
  case). ``NOT_STARTED_ORDER`` marks positions the execution never
  reached after a batch fault; ``NOT_STARTED_BUDGET`` marks positions
  a budget layer refused; ``SKIPPED_UPSTREAM`` marks stages skipped by
  an upstream unit-level failure; ``NOT_APPLICABLE`` marks the C
  suggestion/fusion stages of a No judgment.
  """

  STARTED = 'STARTED'
  FINISHED = 'FINISHED'
  FAILED = 'FAILED'
  PENDING = 'PENDING'
  NOT_STARTED_ORDER = 'NOT_STARTED_ORDER'
  NOT_STARTED_BUDGET = 'NOT_STARTED_BUDGET'
  SKIPPED_UPSTREAM = 'SKIPPED_UPSTREAM'
  NOT_APPLICABLE = 'NOT_APPLICABLE'


# Required fixture fields per unit kind (beyond unit metadata); a tuple
# entry lists alternative fields of which at least one must be present
# (a repetition probe carries its history directly or via history_ref,
# and its candidate directly or via candidate_ref).
_REQUIRED_FIXTURE_FIELDS = {
    'generation_experiment': (
        ('history',),
        ('speech',),
        ('action_intent',),
        ('action_result',),
        ('candidate',),
        ('history_text',),
        ('generation_prefix',),
        ('generation_prompt',),
    ),
    'fixed_draft_review': (('context_ref',), ('draft',)),
    'repetition_probe': (
        ('history', 'history_ref'),
        ('speech',),
        ('action_intent',),
        ('candidate', 'candidate_ref'),
        ('gate', 'gate_ref'),
        ('active_player',),
    ),
}

# Fixture entry kinds sourced from the batch (re-extractable); ``inline``
# entries carry their frozen value directly.
_BATCH_KINDS = frozenset({
    'jsonl_field_list',
    'jsonl_field',
    'response_json_field',
    'whole_file_text',
    'prompt_section',
    'prompt_json_field',
    'prompt_prefix',
})


class ManifestError(Exception):
  """The manifest is structurally invalid or disagrees with the batch."""


@dataclasses.dataclass(frozen=True)
class Manifest:
  """The loaded, structure-verified data authority of the probe.

  Attributes:
    schema: manifest schema identifier (must equal ``MANIFEST_SCHEMA``).
    seeds: paired seed table keyed ``"<unit>|rep<rep>"`` (never the
      condition), each ``{"py_seed": int, "np_seed": int}``.
    budget: frozen two-layer caps (Concordia attempts and SDK creates).
    order: frozen execution order (rounds and per-round sequence).
    labels: frozen stage-state and evaluation-table vocabularies.
    units: per-unit metadata (group, kind, conditions, stages, calls).
    fixtures: per-unit frozen fixtures; batch-derived entries carry a
      ``source`` key into ``batch_sources`` plus their extraction spec.
    batch_sources: batch files (path + sha256) the fixtures derive from.
    model_params: per-stage frozen model parameters (protocol §2.2).
    code_states: frozen before-state file hashes; after = current tree.
    batch_root: repository-relative root of the frozen source batch.
    protocol_sha256: sha256 of the protocol document this manifest
      implements (hash-anchored chain).
    fixture_manifest_doc_sha256: sha256 of the reviewed fixture-manifest
      document this JSON is the data form of.
    seed_derivation: the preregistered derivation rule plus any
      corrections relative to the review document's precomputed table.
    raw: the complete parsed JSON (for fields the harness passes
      through without interpreting).
  """

  schema: str
  seeds: dict[str, dict[str, int]]
  budget: dict[str, Any]
  order: dict[str, Any]
  labels: dict[str, Any]
  units: dict[str, dict[str, Any]]
  fixtures: dict[str, dict[str, Any]]
  batch_sources: dict[str, dict[str, str]]
  model_params: dict[str, Any]
  code_states: dict[str, Any]
  batch_root: str
  protocol_sha256: str
  fixture_manifest_doc_sha256: str
  seed_derivation: dict[str, Any]
  raw: dict[str, Any]


def derive_seeds(unit: str, rep: int) -> tuple[int, int]:
  """Derives the paired (Python, NumPy) seeds for one unit-repetition.

  The preregistered rule (fixture manifest §0): the hex sha256 of
  ``"fixed-probe-v1|<unit>|rep<rep>"``; the first 8 hex digits are the
  Python RNG seed (``random.seed`` for the core ``random.choice`` event
  draw) and digits 9–16 the NumPy seed (the option-permutation
  Generator of the step's internal document).

  Args:
    unit: unit id, e.g. ``"B-1"``.
    rep: repetition round, 1–3.

  Returns:
    ``(py_seed, np_seed)``.
  """
  digest = hashlib.sha256(f'fixed-probe-v1|{unit}|rep{rep}'.encode())
  hexdigest = digest.hexdigest()
  return int(hexdigest[:8], 16), int(hexdigest[8:16], 16)


def load_manifest(path: Path | str) -> Manifest:
  """Loads and structure-verifies the data-authority JSON.

  Args:
    path: path to ``fixed_probe_manifest.json``.

  Returns:
    The verified :class:`Manifest`.

  Raises:
    ManifestError: if the file is unreadable, not the expected schema,
      or fails the structural verification.
  """
  try:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
  except (OSError, json.JSONDecodeError) as error:
    raise ManifestError(f'cannot load manifest {path}: {error}') from error
  if raw.get('schema') != MANIFEST_SCHEMA:
    raise ManifestError(
        f'manifest schema {raw.get("schema")!r} != {MANIFEST_SCHEMA!r}'
    )
  manifest = Manifest(
      schema=raw['schema'],
      seeds=raw['seeds'],
      budget=raw['budget'],
      order=raw['order'],
      labels=raw['labels'],
      units=raw['units'],
      fixtures=raw['fixtures'],
      batch_sources=raw['batch_sources'],
      model_params=raw['model_params'],
      code_states=raw['code_states'],
      batch_root=raw['batch_root'],
      protocol_sha256=raw['protocol_sha256'],
      fixture_manifest_doc_sha256=raw['fixture_manifest_doc_sha256'],
      seed_derivation=raw['seed_derivation'],
      raw=raw,
  )
  verify_manifest(manifest)
  return manifest


def expand_order(manifest: Manifest) -> list[tuple[str, str]]:
  """Expands the frozen order into the full (unit, condition) sequence.

  Protocol §2.1: the fixed per-round sequence rotates once per
  repetition round; skipped executions keep their place in the ledger.

  Args:
    manifest: the verified manifest.

  Returns:
    The full execution order as ``(unit, condition)`` pairs.
  """
  rounds = manifest.order['rounds']
  sequence = [tuple(step) for step in manifest.order['round_sequence']]
  return [step for _ in rounds for step in sequence]


def verify_manifest(
    manifest: Manifest, batch_dir: Path | str | None = None
) -> None:
  """Verifies the manifest, optionally against a batch directory.

  Structural checks always run (units, paired seeds, budget, order,
  labels, fixture shapes, reference resolution). When ``batch_dir`` is
  given, every batch source file must exist with its pinned sha256 and
  every batch-derived fixture value is re-extracted from the file and
  compared; a missing file and a present-but-changed file are distinct
  failures (a missing batch is simply "not supplied", never a silent
  pass of a hash mismatch).

  Args:
    manifest: the manifest to verify.
    batch_dir: root of the batch the sources are relative to; when
      None only the structural checks run.

  Raises:
    ManifestError: on any structural violation, missing source file,
      hash mismatch or re-extraction mismatch.
  """
  _verify_units(manifest)
  _verify_seeds(manifest)
  _verify_budget(manifest)
  _verify_order(manifest)
  _verify_labels(manifest)
  _verify_sources(manifest)
  _verify_fixtures(manifest)
  if batch_dir is not None:
    _verify_against_batch(manifest, Path(batch_dir))


def _verify_units(manifest: Manifest) -> None:
  """Checks the seven units and their frozen condition sets."""
  if set(manifest.units) != set(_UNIT_IDS):
    raise ManifestError(
        f'unit set {sorted(manifest.units)} != {list(_UNIT_IDS)}'
    )
  for unit, meta in manifest.units.items():
    conditions = meta['conditions']
    expected = ['before', 'after'] if unit in _PAIRED_UNITS else ['after']
    if conditions != expected:
      raise ManifestError(f'unit {unit} conditions {conditions} != {expected}')
    if unit.startswith('B-') and meta['group'] != 'B':
      raise ManifestError(f'unit {unit} has group {meta["group"]!r}')
    if unit.startswith('C-') and meta['group'] != 'C':
      raise ManifestError(f'unit {unit} has group {meta["group"]!r}')
    kind = meta['kind']
    if kind not in _REQUIRED_FIXTURE_FIELDS:
      raise ManifestError(f'unit {unit} has unknown kind {kind!r}')


def _verify_seeds(manifest: Manifest) -> None:
  """Checks the paired seed table against the derivation function."""
  expected_keys = {
      f'{unit}|rep{rep}' for unit in _UNIT_IDS for rep in (1, 2, 3)
  }
  if set(manifest.seeds) != expected_keys:
    raise ManifestError('seed table keys != units x reps 1..3')
  for key, entry in manifest.seeds.items():
    if set(entry) != {'py_seed', 'np_seed'}:
      raise ManifestError(f'seed entry {key} has fields {sorted(entry)}')
    unit, rep = key.split('|rep')
    derived = derive_seeds(unit, int(rep))
    if (entry['py_seed'], entry['np_seed']) != derived:
      raise ManifestError(
          f'seed entry {key} {entry} disagrees with derivation {derived}'
      )


def _verify_budget(manifest: Manifest) -> None:
  """Checks the frozen two-layer budget caps."""
  attempts = manifest.budget['concordia_attempts']
  if attempts['global_cap'] != 75:
    raise ManifestError('attempts global cap != 75')
  if attempts['group_caps'] != {'B': 21, 'C': 54}:
    raise ManifestError("attempts group caps != {'B': 21, 'C': 54}")
  if manifest.budget['sdk_create']['global_cap'] != 225:
    raise ManifestError('SDK create cap != 225')


def _verify_order(manifest: Manifest) -> None:
  """Checks the frozen order against the units' condition sets."""
  if manifest.order['rounds'] != [1, 2, 3]:
    raise ManifestError('order rounds != [1, 2, 3]')
  sequence = [tuple(step) for step in manifest.order['round_sequence']]
  seen: set[tuple[str, str]] = set()
  for unit, condition in sequence:
    if condition not in manifest.units[unit]['conditions']:
      raise ManifestError(
          f'order step ({unit}, {condition}) not in unit conditions'
      )
    if (unit, condition) in seen:
      raise ManifestError(f'order step ({unit}, {condition}) repeats')
    seen.add((unit, condition))
  expected_pairs = {
      (unit, condition)
      for unit, meta in manifest.units.items()
      for condition in meta['conditions']
  }
  if seen != expected_pairs:
    raise ManifestError('order does not cover each unit-condition once')
  if expand_order(manifest) != [s for _ in (1, 2, 3) for s in sequence]:
    raise ManifestError('expanded order disagrees with rotation')


def _verify_labels(manifest: Manifest) -> None:
  """Checks the frozen vocabularies (stage states, table columns)."""
  if set(manifest.labels['stage_states']) != set(_STAGE_STATES):
    raise ManifestError(f'stage states != {list(_STAGE_STATES)}')
  for key in ('b_table_columns', 'c_table_columns', 'b_rubric_columns'):
    if not manifest.labels.get(key):
      raise ManifestError(f'labels missing {key}')


def _verify_sources(manifest: Manifest) -> None:
  """Checks batch-source hashes and embedded before-state sources."""
  for name, source in manifest.batch_sources.items():
    if not isinstance(source.get('sha256'), str) or len(source['sha256']) != 64:
      raise ManifestError(f'batch source {name} has no 64-hex sha256')
  before = manifest.code_states.get('before', {})
  files = before.get('files', {})
  sources = before.get('sources', {})
  if set(files) != set(sources):
    raise ManifestError('before-state file hashes and sources disagree')
  for rel, pinned in files.items():
    actual = hashlib.sha256(sources[rel].encode('utf-8')).hexdigest()
    if actual != pinned:
      raise ManifestError(
          f'embedded before-state source {rel} hash {actual} != pinned {pinned}'
      )


def _verify_fixtures(manifest: Manifest) -> None:
  """Checks fixture shapes, kinds, sources and cross-references."""
  for unit, meta in manifest.units.items():
    fixtures = manifest.fixtures[unit]
    for alternatives in _REQUIRED_FIXTURE_FIELDS[meta['kind']]:
      if not any(field in fixtures for field in alternatives):
        raise ManifestError(
            f'unit {unit} fixture missing one of {list(alternatives)}'
        )
    for field, entry in fixtures.items():
      if not isinstance(entry, Mapping):
        # Plain strings are unit metadata (cross-references and the
        # active player), validated separately below.
        continue
      if 'kind' not in entry:
        raise ManifestError(
            f'unit {unit} fixture {field} is not a kind-tagged entry'
        )
      if entry['kind'] in _BATCH_KINDS:
        if entry.get('source') not in manifest.batch_sources:
          raise ManifestError(
              f'unit {unit} fixture {field} has unknown source '
              f'{entry.get("source")!r}'
          )
      elif entry['kind'] != 'inline':
        raise ManifestError(
            f'unit {unit} fixture {field} has unknown kind {entry["kind"]!r}'
        )
    for ref_key in (
        'context_ref',
        'history_ref',
        'candidate_ref',
        'gate_ref',
    ):
      if ref_key in fixtures and fixtures[ref_key] not in _UNIT_IDS:
        raise ManifestError(
            f'unit {unit} {ref_key} {fixtures[ref_key]!r} does not resolve'
        )


def _verify_against_batch(manifest: Manifest, batch_dir: Path) -> None:
  """Re-extracts every batch-derived fixture from the batch directory."""
  for name, source in manifest.batch_sources.items():
    path = batch_dir / source['path']
    if not path.is_file():
      raise ManifestError(f'batch source {name} missing at {path}')
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != source['sha256']:
      raise ManifestError(
          f'batch source {name} hash mismatch: file has {actual}, manifest '
          f"pins {source['sha256']}"
      )
  for unit, fixtures in manifest.fixtures.items():
    for field, entry in fixtures.items():
      if (
          not isinstance(entry, Mapping)
          or entry.get('kind') not in _BATCH_KINDS
      ):
        continue
      extracted = _extract_batch_value(batch_dir, manifest, entry)
      if extracted != entry['value']:
        raise ManifestError(
            f'fixture {unit}.{field} re-extraction disagrees with the '
            'frozen value'
        )


def _extract_batch_value(
    batch_dir: Path, manifest: Manifest, entry: Mapping[str, Any]
) -> Any:
  """Re-extracts one fixture value from its pinned batch source file.

  Args:
    batch_dir: root of the batch.
    manifest: the manifest owning the source table.
    entry: the batch-derived fixture entry.

  Returns:
    The extracted value (string, list of strings, or parsed JSON for
    the dotted-path kinds), to be compared with the frozen value.

  Raises:
    ManifestError: if the file cannot be parsed per its kind or the
      specified turn/heading/field is absent.
  """
  path = batch_dir / manifest.batch_sources[entry['source']]['path']
  text = path.read_text(encoding='utf-8')
  kind = entry['kind']
  if kind == 'whole_file_text':
    return text
  if kind == 'prompt_section':
    return _extract_prompt_section(text, entry['heading'], entry['source'])
  if kind == 'prompt_prefix':
    marker = entry['marker']
    if marker not in text:
      raise ManifestError(f'source lacks prefix marker {marker!r}')
    return text.split(marker, 1)[0]
  if kind == 'prompt_json_field':
    section = _extract_prompt_section(text, entry['heading'], entry['source'])
    payload = json.loads(section)
    return _dotted_get(payload, entry['field']) if 'field' in entry else payload
  if kind == 'response_json_field':
    try:
      payload = json.loads(text)
    except json.JSONDecodeError as error:
      raise ManifestError(
          f'source {entry["source"]} is not valid JSON: {error}'
      ) from error
    return _dotted_get(payload, entry['field'])
  records = _parse_jsonl(text, entry['source'])
  if kind == 'jsonl_field_list':
    values = []
    for turn_id in entry['turn_ids']:
      record = _record_for_turn(records, turn_id, entry['source'])
      values.append(_dotted_get(record, entry['field']))
    return values
  if kind == 'jsonl_field':
    record = _record_for_turn(records, entry['turn_id'], entry['source'])
    return _dotted_get(record, entry['field'])
  raise ManifestError(f'unreachable extraction kind {kind!r}')


def _parse_jsonl(text: str, source: str) -> list[dict[str, Any]]:
  """Parses a JSONL file into records, rejecting invalid lines."""
  records = []
  for number, line in enumerate(text.splitlines(), start=1):
    if not line.strip():
      continue
    try:
      records.append(json.loads(line))
    except json.JSONDecodeError as error:
      raise ManifestError(
          f'source {source} line {number} is not valid JSON: {error}'
      ) from error
  return records


def _record_for_turn(
    records: Sequence[Mapping[str, Any]], turn_id: int, source: str
) -> Mapping[str, Any]:
  """Returns the record with the given turn_id."""
  record = next((r for r in records if r.get('turn_id') == turn_id), None)
  if record is None:
    raise ManifestError(f'source {source} has no turn_id {turn_id}')
  return record


def _extract_prompt_section(text: str, heading: str, source: str) -> str:
  """Extracts the lines between a heading and the next ``## `` heading."""
  lines = text.split('\n')
  starts = [n for n, line in enumerate(lines) if line == heading]
  if not starts:
    raise ManifestError(f'source {source} lacks heading {heading!r}')
  start = starts[0]
  end = next(
      (
          n
          for n, line in enumerate(lines)
          if n > start and line.startswith('## ')
      ),
      len(lines),
  )
  return '\n'.join(lines[start + 1 : end]).rstrip('\n')


# ---------------------------------------------------------------------------
# Paired seeds: NumPy runtime factory + Python seeding (protocol §2.1)
# ---------------------------------------------------------------------------

# Re-entrancy guard: two active seeded steps could cross-wire their
# factories and mismatch seeds, so nesting is explicitly forbidden.
_active_seeded_step = False


class SeedSession:
  """Bookkeeping for one ``seeded_step`` scope.

  Attributes:
    np_seed: the NumPy seed injected into factory-built documents.
    py_seed: the Python seed applied with ``random.seed`` at entry.
  """

  def __init__(self, np_seed: int, py_seed: int) -> None:
    self.np_seed = np_seed
    self.py_seed = py_seed
    self._construction_states: dict[int, Any] = {}

  @property
  def construction_count(self) -> int:
    """Number of factory-built (seed-injected) documents so far."""
    return len(self._construction_states)

  def record_construction(self, generator: np.random.Generator) -> None:
    """Records one factory-built generator's construction-time state."""
    self._construction_states[id(generator)] = copylib.deepcopy(
        generator.bit_generator.state
    )

  def construction_state(
      self, generator: np.random.Generator
  ) -> dict[str, Any] | None:
    """Returns the recorded construction state, or None if not ours."""
    return self._construction_states.get(id(generator))


class _SeededDocumentFactory:
  """Runtime factory replacing the ``InteractiveDocument`` symbol.

  Constructions that pass an explicit ``rng`` — including the
  ``copy()``/``edit()`` paths, which always forward ``rng=self._rng`` —
  go straight through to the original class with the caller's
  generator: no reseeding, no extra draws, the document family shares
  one stream. Only constructions without an ``rng`` (the repetition
  step's own internal document, and any chain document the probe hands
  in unseeded) get a fresh ``np.random.default_rng(np_seed)`` whose
  construction-time bit-generator state is recorded for the
  execution-time RNG proof.
  """

  def __init__(
      self,
      original: type,
      np_seed: int,
      session: SeedSession,
  ) -> None:
    self._original = original
    self._np_seed = np_seed
    self._session = session

  def __call__(self, *args: Any, **kwargs: Any) -> Any:
    rng = kwargs.get('rng')
    if rng is not None:
      return self._original(*args, **kwargs)
    generator = np.random.default_rng(self._np_seed)
    kwargs['rng'] = generator
    instance = self._original(*args, **kwargs)
    self._session.record_construction(generator)
    return instance


@contextlib.contextmanager
def seeded_step(np_seed: int, py_seed: int):
  """Controls both RNG layers for one step execution (protocol §2.1).

  The NumPy Generator that ``InteractiveDocument`` uses for option
  permutation cannot be reached through ``random.seed``; this scope
  replaces the ``InteractiveDocument`` symbol on the
  ``concordia.document.interactive_document`` module (runtime patching
  only — no source changes) with the recording factory above, and
  seeds Python's global ``random`` for the core ``random.choice``
  event draw. Both conditions of one unit-repetition share the same
  seed pair; the original symbol is restored on normal and exceptional
  exit. Nesting is forbidden to prevent cross-wired factories.

  Args:
    np_seed: NumPy seed for factory-built documents.
    py_seed: Python seed applied at scope entry.

  Yields:
    The :class:`SeedSession` holding the recorded construction-time
    generator states for the execution-time RNG proof (Task 4).
  """
  global _active_seeded_step
  if _active_seeded_step:
    raise RuntimeError(
        'nested seeded_step is forbidden: it would cross-wire the'
        ' InteractiveDocument factories of two scopes'
    )
  module = interactive_document
  original = module.InteractiveDocument
  session = SeedSession(np_seed=np_seed, py_seed=py_seed)
  factory = _SeededDocumentFactory(original, np_seed, session)
  module.InteractiveDocument = factory  # pylint: disable=protected-access  # pyrefly: ignore[bad-assignment]
  _active_seeded_step = True
  random.seed(py_seed)
  try:
    yield session
  finally:
    module.InteractiveDocument = original
    _active_seeded_step = False


# ---------------------------------------------------------------------------
# Execution-time RNG proof (protocol §2.1, §7 batch-stop on mismatch)
# ---------------------------------------------------------------------------


class IntegrityError(Exception):
  """The paired seed provably did not reach a core helper's document.

  Protocol §7 treats this as a batch-level integrity failure: the
  "only controlled differences" premise depends on the internal
  document actually carrying the paired seed.
  """


@contextlib.contextmanager
def rng_proof(loaded_module: ModuleType, session: SeedSession):
  """Proves the paired seed reached the helper's internal document.

  Wraps ``maybe_inject_narrative_push`` on the event-resolution module
  the given loaded repetition-push step references. At the wrapper's
  entry — strictly before the real helper runs and consumes the
  random stream — the document's current bit-generator state is
  compared with the state the Task-3 factory recorded at construction
  time. Equality is recorded as the proof; a document whose generator
  the session never recorded (the seed never reached it), or whose
  state already drifted, raises :class:`IntegrityError` before the
  helper executes.

  Args:
    loaded_module: an alias-loaded ``repetition_push`` module (its
      ``event_resolution_lib`` attribute is the core module whose
      symbol is wrapped and, on exit, restored).
    session: the :class:`SeedSession` of the surrounding
      ``seeded_step`` scope.

  Yields:
    The proof list; one record per helper call, each holding
    ``construction_state``, ``check_state`` (taken at entry) and
    ``state_after`` (taken after the helper returned).
  """
  core = loaded_module.event_resolution_lib
  original = core.maybe_inject_narrative_push
  proofs: list[dict[str, Any]] = []

  def _wrapper(chain_of_thought, putative_event, active_player_name):
    generator = getattr(chain_of_thought, '_rng', None)
    if generator is None:
      raise IntegrityError(
          'rng proof failed: the document carries no rng at all'
      )
    recorded = session.construction_state(generator)
    if recorded is None:
      raise IntegrityError(
          'rng proof failed: the document generator was not built by'
          ' the seeded factory (the paired seed never reached the'
          ' internal document)'
      )
    check_state = copylib.deepcopy(generator.bit_generator.state)
    if check_state != recorded:
      raise IntegrityError(
          'rng proof failed: the document generator state at helper'
          ' entry differs from its construction-time state'
      )
    proof = {
        'construction_state': recorded,
        'check_state': check_state,
    }
    proofs.append(proof)
    try:
      return original(chain_of_thought, putative_event, active_player_name)
    finally:
      proof['state_after'] = copylib.deepcopy(generator.bit_generator.state)

  core.maybe_inject_narrative_push = _wrapper
  try:
    yield proofs
  finally:
    core.maybe_inject_narrative_push = original


def _dotted_get(payload: Mapping[str, Any], field: str) -> Any:
  """Resolves a dotted field path against a parsed JSON object."""
  value: Any = payload
  for part in field.split('.'):
    if not isinstance(value, Mapping) or part not in value:
      raise ManifestError(f'dotted field {field!r} unresolved at {part!r}')
    value = value[part]
  return value


# ---------------------------------------------------------------------------
# Dual code-state loading and dependency audit (protocol §2)
# ---------------------------------------------------------------------------

# The two production modules whose before/after states differ; every
# other module in their import closure must be byte-identical between
# the two states or the batch stops (protocol §2, "otherwise stop").
_PRODUCTION_MODULES = (
    'concordia.contrib.components.game_master.story_resolution',
    'concordia.contrib.components.game_master.repetition_push',
)

_CODE_STATE_PREFIX = 'fixed_probe_code_state'


def load_code_state(alias: str, py_path: Path | str) -> ModuleType:
  """Loads one module file under an isolated alias (importlib).

  The frozen before state is loaded from its snapshot file while the
  rest of its imports resolve through ``sys.modules`` to the current
  tree — exactly the sharing the protocol prescribes: only the two
  production files differ between states, everything else is common.
  The alias keeps the loaded module from clashing with the production
  module of the same name, so both states coexist in one process.

  Args:
    alias: unique alias for this load (e.g. ``"before_story_resolution"``).
    py_path: the module file to load.

  Returns:
    The loaded module object (also registered in ``sys.modules`` under
    ``fixed_probe_code_state_<alias>`` so repeated loads replace their
    own alias only).
  """
  module_name = f'{_CODE_STATE_PREFIX}_{alias}'
  spec = importlib.util.spec_from_file_location(module_name, py_path)
  if spec is None or spec.loader is None:
    raise ImportError(f'cannot load code state from {py_path}')
  module = importlib.util.module_from_spec(spec)
  sys.modules[module_name] = module
  spec.loader.exec_module(module)
  return module


def audit_dependency_identity(
    before_dir: Path | str,
    after_dir: Path | str,
    *,
    seed_modules: Sequence[str] = _PRODUCTION_MODULES,
) -> list[str]:
  """Audits that the two code states differ only in the two files.

  The frozen before state is a *changed-files* snapshot: the two
  production files (and their tests) exist in it, while every other
  module of the import closure is shared from the current tree by
  design. The audit therefore computes the static import closure of
  the seed modules in both directory roots (packages and modules that
  exist as files under the root; stdlib and third-party imports
  resolve nowhere and are ignored) and reports, beyond the production
  whitelist, exactly the modules that would break the "only these two
  files differ" premise: a module the before state has its own copy
  of with different bytes (it changed since the baseline), and a
  module that exists only in the before state (the current tree lost
  it). A module present only in the after state is the normal shared
  case, not drift.

  Args:
    before_dir: root of the before-state tree (the frozen snapshot).
    after_dir: root of the after-state tree (the current tree).
    seed_modules: dotted names of the two production modules.

  Returns:
    Drift findings, one string each; an empty list means the two
    states are identical beyond the whitelisted files.
  """
  before_dir, after_dir = Path(before_dir), Path(after_dir)
  closure: set[str] = set()
  for root in (before_dir, after_dir):
    for seed in seed_modules:
      closure |= _import_closure(root, seed)
  findings: list[str] = []
  for name in sorted(closure):
    if name in _PRODUCTION_MODULES:
      continue
    before_file = _module_file(before_dir, name)
    after_file = _module_file(after_dir, name)
    if before_file is None:
      # Shared from the current tree by design; not drift.
      continue
    if after_file is None:
      findings.append(f'{name}: present only in the before state')
      continue
    if before_file.read_bytes() != after_file.read_bytes():
      findings.append(f'{name}: bytes differ between code states')
  return findings


def _module_file(root: Path, name: str) -> Path | None:
  """Resolves a dotted module name to a file under a tree root."""
  base = root.joinpath(*name.split('.'))
  for candidate in (
      base.parent / f'{name.split(".")[-1]}.py',
      base / '__init__.py',
  ):
    if candidate.is_file():
      return candidate
  return None


def _import_closure(root: Path, seed: str) -> set[str]:
  """Collects the tree-resolvable import closure of one module."""
  closure: set[str] = set()
  queue = [seed]
  while queue:
    name = queue.pop()
    if name in closure:
      continue
    file = _module_file(root, name)
    if file is None:
      continue
    closure.add(name)
    for imported in _imported_names(file, name):
      if imported not in closure:
        queue.append(imported)
  return closure


def _imported_names(file: Path, module_name: str) -> set[str]:
  """Statically collects absolute module names imported by a file."""
  try:
    tree = ast.parse(file.read_text(encoding='utf-8', errors='replace'))
  except SyntaxError:
    return set()
  is_package = file.name == '__init__.py'
  package_parts = module_name.split('.')
  if not is_package:
    package_parts = package_parts[:-1]
  names: set[str] = set()
  for node in ast.walk(tree):
    if isinstance(node, ast.Import):
      for alias in node.names:
        names.add(alias.name)
    elif isinstance(node, ast.ImportFrom):
      if node.level == 0:
        if node.module:
          names.add(node.module)
          # `from package import submodule` imports the qualified
          # name too; unresolvable candidates (plain attributes or
          # re-exports) are dropped when the closure fails to resolve.
          for alias in node.names:
            names.add(f'{node.module}.{alias.name}')
      else:
        base = package_parts[: len(package_parts) - (node.level - 1)]
        prefix = '.'.join(base)
        if node.module:
          names.add(f'{prefix}.{node.module}' if prefix else node.module)
          for alias in node.names:
            names.add(f'{prefix}.{node.module}.{alias.name}')
        else:
          for alias in node.names:
            names.add(f'{prefix}.{alias.name}' if prefix else alias.name)
  # Importing a.b.c also imports every ancestor package.
  expanded: set[str] = set()
  for name in names:
    parts = name.split('.')
    for index in range(1, len(parts) + 1):
      expanded.add('.'.join(parts[:index]))
  return expanded


# ---------------------------------------------------------------------------
# Execution engine (protocol §2.1 order, §6 budget, §7 four-state rules)
# ---------------------------------------------------------------------------

# Reuse, not re-implementation: the state-rebuild conventions of the
# archived probes (director construction, turn opening, public-history
# formatting) and the two-layer request ledger.
from concordia.contrib.components.game_master import drama_director  # pylint: disable=wrong-import-position
from concordia.contrib.components.game_master import story_action  # pylint: disable=wrong-import-position
from concordia.contrib.components.game_master import story_action_adjudication  # pylint: disable=wrong-import-position
from concordia.testing import mock_model  # pylint: disable=wrong-import-position

from examples.story_enactment import fixed_model_probe as _fmp  # pylint: disable=wrong-import-position
from examples.story_enactment import request_ledger as rl  # pylint: disable=wrong-import-position

# Review-stage failure messages of ``story_resolution`` all start with
# this prefix; a rejection additionally carries the rule reason.
_SEMANTIC_REJECTION_PREFIX = 'Semantic review rejected output:'
_SEMANTIC_STAGE_PREFIX = 'Semantic review'

_REVIEW_QUESTION = (
    'Evaluate all rules above. Output only a JSON object with exactly '
    '"valid" (a JSON boolean) and "reason" (a string explaining the '
    'decision). Reject if any rule fails. Do not rewrite the event. '
    'If rejecting, name the specific offending text, whether it comes '
    'from the action result or the narration, and which source is '
    'missing.'
)

# Content marks used to attribute one archived call to its stage. They
# ride on the production prompt shapes (not on any stub), mirroring
# ``testing_stubs``'s documented fingerprint convention. Both output
# contracts are carried: historical (original-contract) prompts carry
# the single-field instruction, current production the two-field one.
_STAGE_MARKS = (
    ('Suggest five plausible events', 'suggestion'),
    ('How does 2 modify', 'fusion'),
    ('# Story resolution semantic review', 'review'),
    ('Output a JSON object with exactly two string fields', 'generation'),
    ('Output a JSON object with exactly one string field', 'generation'),
)

# Controlled-difference markers of the audit (protocol §2): the only
# allowed first-prompt differences between conditions.
# The core's fusion question quotes the randomly selected suggestion
# after "2."; extracting it from the archived prompt is the only way to
# observe the internal draw without touching the core.
_SELECTED_SUGGESTION_RE = re.compile(
    r'Given:\n1\..*?\n2\.(.*?)\n\nHow does 2 modify', re.DOTALL
)

_AUDIT_SECTIONS = {
    'B': (
        "## Deliver this turn's adjudicated result",
        '## Output',
    ),
    'C': (
        '## Current player contribution (not yet committed, data)',
        '## Current candidate (not yet committed)',
    ),
}


class BudgetStop(Exception):
  """A budget layer reached its cap; the batch stops, nothing reruns."""


class BatchFault(Exception):
  """A batch-level integrity failure (audit, proof, infrastructure)."""


# ---------------------------------------------------------------------------
# Original-contract replay (audience-delivery design 7.2)
# ---------------------------------------------------------------------------

# The frozen data authority of the historical protocol's replay. The
# 2026-10-31 audience-delivery contract change means the current tree no
# longer speaks the original single-field output contract the 2026-10-24
# fixed-sample protocol drove: its "after" condition was the then-current
# production, whose bytes exist today only as the executed batches'
# archived prompts. Historical units therefore replay ORIGINAL-CONTRACT
# fixtures: the frozen before state executes inside a byte-pinned
# original-contract dependency closure (never resolving through
# ``sys.modules`` to new-tree components with old signatures), and the
# after condition's controlled prompt is the archived recording,
# reconstructed from the executed before prompt plus the audited
# controlled section and verified against the archive's sha256 pin.
ORIGINAL_CONTRACT_SCHEMA = 'fixed-probe-original-contract-v1'
_ORIGINAL_CONTRACT_PATH = (
    Path(__file__).parent / 'fixed_probe_original_contract.json'
)

# The commit whose bytes back the fixture's closure pins (documented in
# the fixture's provenance; the commit's bytes equal the manifests'
# hash-pinned before states and the executed batches' dependency pins).
_ORIGINAL_CONTRACT_COMMIT = 'caf2c1c58da93b808246583bb34c357157bb118a'

# Per-module original-contract features: (required mark, forbidden mark).
# A historical code-state source that lost the required mark or gained
# the forbidden one speaks the new contract and is rejected loudly —
# never whitelisted around.
_ORIGINAL_CONTRACT_MARKS = {
    'concordia/contrib/components/game_master/story_resolution.py': (
        'Output a JSON object with exactly one string field',
        'Output a JSON object with exactly two string fields',
    ),
    'concordia/contrib/components/game_master/repetition_push.py': (
        '## Committed public events (oldest first)',
        'GM_COMPLETE_RECORD_LABEL',
    ),
    'concordia/contrib/components/game_master/story_action_adjudication.py': (
        'PUBLIC_HISTORY_SECTION_MARK',
        'GM_HISTORY_SECTION_MARK',
    ),
    'concordia/contrib/components/game_master/drama_director.py': (
        'def set_validated_resolution(',
        'def get_public_events(',
    ),
}

_original_contract_fixture: dict[str, Any] | None = None


class NewContractSourceError(BatchFault):
  """A historical-protocol load path met the new audience contract.

  Raised when a harness is asked to drive the historical (single-field
  output) protocol against a source that speaks the 2026-10-31
  audience-delivery contract — a new two-field fingerprint, a missing
  original-contract mark, a renamed parser or a changed call signature.
  Historical replay must use original-contract fixtures; the error
  names the incompatibility instead of weakening any audit.
  """


def load_original_contract() -> dict[str, Any]:
  """Loads and structure-verifies the original-contract replay fixture.

  Returns:
    The parsed fixture (cached): ``closure`` (repository-relative path
    to sha256 of the original-contract import closure's bytes),
    ``frozen_sources`` (embedded original sources of the closure
    modules whose current worktree bytes drifted) and ``replay``
    (per-unit recorded sections, insertion marks and archived
    controlled-prompt sha256 pins).

  Raises:
    NewContractSourceError: if the fixture is unreadable, not the
      expected schema, or its provenance commit changed.
  """
  global _original_contract_fixture
  if _original_contract_fixture is not None:
    return _original_contract_fixture
  try:
    fixture = json.loads(_ORIGINAL_CONTRACT_PATH.read_text(encoding='utf-8'))
  except (OSError, json.JSONDecodeError) as error:
    raise NewContractSourceError(
        f'cannot load the original-contract fixture: {error}'
    ) from error
  if fixture.get('schema') != ORIGINAL_CONTRACT_SCHEMA:
    raise NewContractSourceError(
        'original-contract fixture schema'
        f' {fixture.get("schema")!r} != {ORIGINAL_CONTRACT_SCHEMA!r}'
    )
  if fixture.get('provenance', {}).get('commit') != _ORIGINAL_CONTRACT_COMMIT:
    raise NewContractSourceError(
        'original-contract fixture provenance commit disagrees with the'
        ' pinned commit; the frozen closure is no longer the verified'
        ' original contract'
    )
  for key in ('closure', 'frozen_sources', 'replay'):
    if not isinstance(fixture.get(key), dict):
      raise NewContractSourceError(
          f'original-contract fixture lacks the {key!r} table'
      )
  _original_contract_fixture = fixture
  return fixture


def assert_original_contract_source(rel: str, source: str) -> None:
  """Rejects a source that speaks the new audience contract.

  Args:
    rel: repository-relative path of the module being loaded.
    source: the module's full source text.

  Raises:
    NewContractSourceError: naming the exact incompatibility when the
      source lost the required original-contract mark or gained the
      forbidden new-contract mark.
  """
  marks = _ORIGINAL_CONTRACT_MARKS.get(rel)
  if marks is None:
    return
  required, forbidden = marks
  if forbidden in source:
    raise NewContractSourceError(
        f'historical protocol cannot drive {rel}: the source carries the'
        f' new audience-delivery contract feature {forbidden!r}; the'
        ' original single-field output contract is required'
        ' (audience-delivery design 7.2)'
    )
  if required not in source:
    raise NewContractSourceError(
        f'historical protocol cannot drive {rel}: the original-contract'
        f' mark {required!r} is absent from the source; refusing to'
        ' replay the historical protocol against new-contract source'
    )


def _sha256_text(text: str) -> str:
  """Returns the hex sha256 of one UTF-8 string."""
  return hashlib.sha256(text.encode('utf-8')).hexdigest()


class SdkCreateGuard:
  """Second ledger layer at the SDK-create boundary, transport injected.

  Mirrors ``s3_diagnostic_story``'s guard discipline on top of a second
  :class:`~examples.story_enactment.request_ledger.RequestLedger`:
  every create is pre-debited and durably ``started`` before the
  injected transport runs, and the raw response is archived before any
  parsing. Offline the transport is the in-process stub call; a real
  run injects the provider call. Judgement re-asks would be additional
  creates here; they never add Concordia attempts.
  """

  def __init__(
      self,
      out_dir: Path,
      *,
      create_cap: int,
      model_tag: str = 'stub',
      model_name: str = 'stub',
      secrets: Sequence[str] = (),
      integrity_check: Callable[[], None] | None = None,
  ):
    self._ledger = rl.RequestLedger(
        out_dir / 'sdk_ledger',
        batch_id='fixed-probe-sdk',
        global_cap=create_cap,
        model_caps={},
        group_caps={},
        sample_caps={},
        secrets=secrets,
    )
    self._identity_path = Path(out_dir) / 'sdk_ledger' / 'sdk_identity.jsonl'
    self._model_tag = model_tag
    self._model_name = model_name
    self._integrity_check = integrity_check
    self._seq = 0

  @property
  def counts(self) -> dict[str, Any]:
    return self._ledger.counts

  def begin(
      self,
      *,
      group: str,
      attempt_id: str,
      prompt: str,
      params: Mapping[str, Any],
  ) -> str:
    """Pre-debits one durable SDK-create slot before any forwarding."""
    if self._integrity_check is not None:
      # Batch-level integrity gate, shared with the attempt layer and
      # reached by the client-boundary observer below: it runs before
      # the pre-debit so a failing check never consumes a create.
      self._integrity_check()
    self._seq += 1
    slot_id = f'{group}__sdk{self._seq:06d}'
    try:
      self._ledger.begin_request(
          slot_id=slot_id,
          sample_id=group,
          group=group,
          model_tag=self._model_tag,
          model_name=self._model_name,
          attempt_id=attempt_id,
          stage='sdk_create',
          prompt=prompt,
          passed_params=dict(params),
      )
    except rl.LedgerBudgetError as error:
      raise BudgetStop(f'sdk create cap: {error}') from error
    return slot_id

  def record(self, slot_id: str, text: Any) -> None:
    """Archives the raw create response before any parsing."""
    self._ledger.record_raw_response(slot_id, text)

  def record_identity(
      self, slot_id: str, model: str | None, response_id: str | None
  ) -> None:
    """Appends one SDK-layer response-identity record.

    The two ledgers' ``started`` events carry what was *asked* (the
    frozen request identity); ``sdk_identity.jsonl`` carries what
    *answered* — the model the provider reported on the response and
    its response id — recorded at the create boundary before any
    parsing, so a provider silently answering as a different model
    leaves evidence next to the request books.
    """
    record = {'slot_id': slot_id, 'model': model, 'response_id': response_id}
    with self._identity_path.open('a', encoding='utf-8') as handle:
      handle.write(json.dumps(record, ensure_ascii=False) + '\n')

  def finish(
      self, slot_id: str, *, status: str, error: str | None = None
  ) -> None:
    """Observes the create slot's terminal state; adds no budget."""
    self._ledger.finish_request(slot_id=slot_id, status=status, error=error)

  def forward(
      self,
      *,
      group: str,
      attempt_id: str,
      prompt: str,
      params: Mapping[str, Any],
      transport: Callable[[], Any],
  ) -> Any:
    """Runs one create through the pre-debit/observe discipline."""
    slot_id = self.begin(
        group=group, attempt_id=attempt_id, prompt=prompt, params=params
    )
    try:
      result = transport()
      self.record(slot_id, result)
    except BaseException as error:
      self.finish(
          slot_id,
          status=type(error).__name__,
          error=f'{type(error).__name__}: {error}',
      )
      raise
    self.finish(slot_id, status='completed')
    return result


class _ObservingCompletions:
  """Forwards every client ``create`` under the SDK guard's discipline.

  Mirrors ``s3_diagnostic_story``'s observing client at the boundary
  the protocol's create layer needs: the judgment re-ask loop issues
  several ``chat.completions.create`` calls inside ONE adapter
  ``sample_choice``; only a boundary here can count, cap and archive
  each of them (protocol §6). Offline stubs without a client fall back
  to per-call forwarding in :class:`ProbeModel`.
  """

  def __init__(
      self,
      real_completions: Any,
      guard: SdkCreateGuard,
      state: '_ObservationState',
  ):
    self._real = real_completions
    self._guard = guard
    self._state = state

  def create(self, *args: Any, **kwargs: Any) -> Any:
    active = self._state.active
    if active is None:
      raise BatchFault('SDK create outside any active model call.')
    group, attempt_id = active
    messages = kwargs.get('messages')
    params = {key: value for key, value in kwargs.items() if key != 'messages'}
    slot = self._guard.begin(
        group=group,
        attempt_id=attempt_id,
        prompt=json.dumps(messages, ensure_ascii=False, default=repr),
        params=params,
    )
    try:
      response = self._real.create(*args, **kwargs)
      content = getattr(
          getattr(response.choices[0], 'message', None), 'content', None
      )
      self._guard.record(slot, content)
      self._guard.record_identity(
          slot, getattr(response, 'model', None), getattr(response, 'id', None)
      )
    except BaseException as error:  # pylint: disable=broad-except
      self._guard.finish(
          slot,
          status=type(error).__name__,
          error=f'{type(error).__name__}: {error}',
      )
      raise
    self._guard.finish(slot, status='completed')
    return response


class _ObservationState:
  """The currently active model call, for create attribution."""

  def __init__(self) -> None:
    self.active: tuple[str, str] | None = None


class ProbeModel(language_model.LanguageModel):
  """Wraps the injected model with the probe's two ledger layers.

  Each forwarding reserves one Concordia-attempt slot, after checking
  both its B/C group allowance and the global cap. A choice may issue
  several SDK creates under that same attempt; every create has its
  own guarded ledger slot. Calls retain the ``unit-condition`` group
  key and the production stage, and choice calls archive their option
  order even if the provider subsequently fails.
  """

  def __init__(
      self,
      inner: Any,
      *,
      ledger: rl.RequestLedger,
      sdk_guard: SdkCreateGuard,
      out_dir: Path,
      model_tag: str = 'stub',
      model_name: str = 'stub',
      attempt_group_caps: Mapping[str, int] | None = None,
      integrity_check: Callable[[], None] | None = None,
  ):
    self._inner = inner
    self._ledger = ledger
    self._sdk_guard = sdk_guard
    self._model_tag = model_tag
    self._model_name = model_name
    self._integrity_check = integrity_check
    self._option_log_path = Path(out_dir) / 'option_orders.jsonl'
    self._out_dir = Path(out_dir)
    self._group = 'unbound'
    self._stage_label = 'request'
    self._attempt_group_caps = dict(attempt_group_caps or {})
    self._observation_state = _ObservationState()
    client = getattr(inner, '_client', None)
    completions = getattr(getattr(client, 'chat', None), 'completions', None)
    if client is not None and completions is not None:
      # Real-run wiring: the create boundary is guarded per create, so
      # judgment re-asks count and cap here; the outer production
      # retry wrapper must sit OUTSIDE this ProbeModel so each retry
      # stays one billed attempt.
      client.chat.completions = _ObservingCompletions(
          completions, sdk_guard, self._observation_state
      )
      self._client_guarded = True
    else:
      self._client_guarded = False
    self._seq_by_group: dict[str, int] = {}
    self.call_log: list[dict[str, Any]] = []
    self._run_start = 0
    self._started_before: set[str] = set()

  @property
  def model_name(self) -> str:
    return self._model_name

  def bind(self, group: str, stage_label: str) -> None:
    """Binds subsequent calls to one unit-condition and stage label."""
    self._group = group
    self._stage_label = stage_label
    self._run_start = len(self.call_log)
    self._started_before = set(self._ledger.counts['started_slots'])

  def take_run_log(self) -> list[dict[str, Any]]:
    """Returns the call-log slice of the current bound run."""
    return self.call_log[self._run_start :]

  def take_run_events(self) -> list[dict[str, Any]]:
    """Returns this bound unit's durable started and terminal events."""
    path = self._out_dir / 'requests.jsonl'
    if not path.exists():
      return []
    return [
        event
        for event in rl.load_events(path)
        if event.get('slot_id') not in self._started_before
    ]

  @staticmethod
  def _classify_stage(kind: str, prompt: str) -> str:
    if kind == 'sample_choice':
      return 'judgment'
    for mark, stage in _STAGE_MARKS:
      if mark in prompt:
        return stage
    return 'request'

  def _run_call(
      self,
      kind: str,
      prompt: str,
      params: Mapping[str, Any],
      transport: Callable[[], Any],
  ) -> Any:
    group = self._group
    stage = self._classify_stage(kind, prompt)
    if self._integrity_check is not None:
      # Batch-level integrity gate (protocol §7): strictly before any
      # budget layer is consulted and before the attempt durably
      # starts, so a failing check never consumes budget and its
      # exception escalates to a batch fault unchanged.
      self._integrity_check()
    group_letter = group.split('-', 1)[0]
    group_cap = self._attempt_group_caps.get(group_letter)
    if group_cap is not None:
      used = sum(
          count
          for name, count in self._ledger.counts['by_group'].items()
          if name.startswith(group_letter + '-')
      )
      if used >= group_cap:
        raise BudgetStop(
            f'group {group_letter} attempt cap {group_cap} reached'
        )
    seq = self._seq_by_group.get(group, 0) + 1
    self._seq_by_group[group] = seq
    slot_id = f'{group}__a{seq:06d}'
    try:
      self._ledger.begin_request(
          slot_id=slot_id,
          sample_id=group,
          group=group,
          model_tag=self._model_tag,
          model_name=self._model_name,
          attempt_id=group,
          stage=stage,
          prompt=prompt,
          passed_params={**dict(params), 'kind': kind},
      )
    except rl.LedgerBudgetError as error:
      raise BudgetStop(f'attempt cap: {error}') from error
    self._observation_state.active = (group, slot_id)
    try:
      if kind == 'sample_choice':
        self._archive_option_order(slot_id, group, stage, prompt)
      if self._client_guarded:
        # Each underlying create debits itself at the client boundary.
        result = transport()
      else:
        result = self._sdk_guard.forward(
            group=group,
            attempt_id=slot_id,
            prompt=prompt,
            params={**dict(params), 'kind': kind},
            transport=transport,
        )
      self._ledger.record_raw_response(slot_id, result)
      self._ledger.finish_request(slot_id=slot_id, status='completed')
    except BaseException as error:
      self.call_log.append({
          'slot': slot_id,
          'group': group,
          'stage': stage,
          'prompt': prompt,
          'failed': type(error).__name__,
      })
      self._ledger.finish_request(
          slot_id=slot_id,
          status=type(error).__name__,
          error=f'{type(error).__name__}: {error}',
      )
      raise
    finally:
      self._observation_state.active = None
    entry: dict[str, Any] = {
        'slot': slot_id,
        'group': group,
        'stage': stage,
        'prompt': prompt,
    }
    if kind == 'sample_choice':
      options = dict(re.findall(r'^  \((\w+)\) (Yes|No)\s*$', prompt, re.M))
      entry['choice'] = options.get(result[1])
    self.call_log.append(entry)
    return result

  def _archive_option_order(
      self, slot_id: str, group: str, stage: str, prompt: str
  ) -> None:
    """Archives the presented option order of one choice call."""
    options = [
        line.strip()
        for line in prompt.splitlines()
        if line.strip().startswith('(')
    ]
    record = {
        'slot': slot_id,
        'group': group,
        'stage': stage,
        'options': options,
    }
    with self._option_log_path.open('a', encoding='utf-8') as handle:
      handle.write(json.dumps(record, ensure_ascii=False) + '\n')

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
    """Forwards one text call through both ledger layers."""
    params = {
        'max_tokens': max_tokens,
        'terminators': list(terminators),
        'temperature': temperature,
        'top_p': top_p,
        'top_k': top_k,
        'timeout': timeout,
        'seed': seed,
    }
    return self._run_call(
        'sample_text',
        prompt,
        params,
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
    )

  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, Any]:
    """Forwards one choice call through both ledger layers."""
    params = {'responses': list(responses), 'seed': seed}
    return self._run_call(
        'sample_choice',
        prompt,
        params,
        lambda: self._inner.sample_choice(
            prompt, responses=responses, seed=seed
        ),
    )


@contextlib.contextmanager
def _frozen_closure_swap(frozen_modules: Mapping[str, ModuleType]):
  """Swaps frozen modules into ``sys.modules`` and package attributes.

  ``from package import module`` binds the package attribute when it
  is already set, consulting ``sys.modules`` only as a fallback — so a
  bare ``sys.modules`` swap would silently bind the current tree's
  module into a frozen seed. Both the mapping entry and the parent
  package attribute are swapped together and restored on exit.

  Args:
    frozen_modules: dotted module name to frozen module object.
  """
  saved_modules: dict[str, ModuleType | None] = {}
  saved_attrs: dict[tuple[str, str], Any] = {}
  try:
    for name, module in frozen_modules.items():
      saved_modules[name] = sys.modules.get(name)
      sys.modules[name] = module
      parent_name, _, leaf = name.rpartition('.')
      parent = importlib.import_module(parent_name)
      if hasattr(parent, leaf):
        saved_attrs[(parent_name, leaf)] = getattr(parent, leaf)
      setattr(parent, leaf, module)
    yield
  finally:
    for (parent_name, leaf), original in saved_attrs.items():
      setattr(sys.modules[parent_name], leaf, original)
    for name, original in saved_modules.items():
      if original is None:
        sys.modules.pop(name, None)
      else:
        sys.modules[name] = original


def load_code_states(manifest: Manifest, work_dir: Path) -> dict[str, Any]:
  """Loads both code states as alias-isolated original-contract modules.

  Historical replay (audience-delivery design 7.2): both conditions
  execute the frozen ORIGINAL-CONTRACT closure. The seed production
  files come from the manifest's embedded, hash-pinned sources (the
  data authority); every other closure module is either byte-identical
  between the current tree and the fixture's pinned original bytes —
  and therefore shared through ``sys.modules``, provably equivalent —
  or loaded frozen from the fixture's embedded original sources under
  a private namespace, so a frozen module can never resolve through
  ``sys.modules`` to a new-tree component and call it with an old
  signature. A closure module that drifted without an embedded frozen
  copy is a loud batch fault, never a silent share.

  Args:
    manifest: the verified manifest.
    work_dir: directory the frozen code-state files are written to.

  Returns:
    ``{'before': {'story_resolution': m, 'repetition_push': m,
    'drama_director': m, 'story_action_adjudication': m},
    'after': {...}}`` — both conditions built from the same
    original-contract bytes, each under its own module aliases.

  Raises:
    ManifestError: if a manifest before source disagrees with its pin.
    NewContractSourceError: if any materialized source speaks the new
      audience-delivery contract, or the closure drifted beyond the
      fixture's frozen coverage.
  """
  fixture = load_original_contract()
  seeds = dict(manifest.code_states['before']['sources'])
  # Contract-check every seed source before anything is executed: a
  # tampered manifest carrying new-contract source is rejected here.
  for rel, source in seeds.items():
    assert_original_contract_source(rel, source)
  frozen_sources = dict(fixture['frozen_sources'])
  for rel, source in frozen_sources.items():
    assert_original_contract_source(rel, source)
  # Resolve the closure: shared (byte-identical) or frozen (embedded).
  for rel in frozen_sources:
    if (
        rel in fixture['closure']
        and _sha256_text(frozen_sources[rel]) != fixture['closure'][rel]
    ):
      raise NewContractSourceError(
          f'original-contract fixture frozen source {rel} disagrees'
          ' with its own closure pin'
      )
  drifted: list[str] = []
  repo_root = Path(__file__).parents[2]
  for rel in fixture['closure']:
    if rel in seeds:
      continue
    worktree = repo_root / rel
    if (
        worktree.is_file()
        and _sha256_text(worktree.read_text(encoding='utf-8'))
        == fixture['closure'][rel]
    ):
      continue  # Shared through sys.modules; provably identical bytes.
    if rel not in frozen_sources:
      drifted.append(rel)
  if drifted:
    raise NewContractSourceError(
        'the import closure drifted beyond the original-contract'
        f' fixture: {drifted}; the historical protocol cannot share a'
        ' changed module with the frozen code states — extend the'
        " fixture's frozen sources or deschedule the historical replay"
    )
  before_dir = Path(work_dir) / 'code_states' / 'before'
  closure_dir = Path(work_dir) / 'code_states' / 'original_closure'
  before_dir.mkdir(parents=True, exist_ok=True)
  closure_dir.mkdir(parents=True, exist_ok=True)
  # Materialize the frozen non-seed closure modules.
  frozen_files: dict[str, Path] = {}
  for rel, source in frozen_sources.items():
    if rel in seeds or rel not in fixture['closure']:
      continue
    target = closure_dir / Path(rel).name
    target.write_text(source, encoding='utf-8')
    frozen_files[rel] = target
  frozen_modules: dict[str, ModuleType] = {}
  frozen_specs: dict[str, importlib.util.Loader] = {}
  for index, (rel, target) in enumerate(frozen_files.items()):
    name = rel[: -len('.py')].replace('/', '.')
    spec = importlib.util.spec_from_file_location(
        f'{_CODE_STATE_PREFIX}_original_{index}', target
    )
    if spec is None or spec.loader is None:
      raise NewContractSourceError(
          f'cannot load the frozen original-contract module {rel}'
      )
    frozen_modules[name] = importlib.util.module_from_spec(spec)
    frozen_specs[name] = spec.loader
  # Exec the frozen closure modules with all of them pre-registered in
  # ``sys.modules`` (circular imports between frozen modules resolve
  # to the frozen objects), then alias-load the seeds per condition
  # under the same swap so their imports bind the frozen modules.
  with _frozen_closure_swap(frozen_modules):
    for name, module in frozen_modules.items():
      frozen_specs[name].exec_module(module)

    def _load_condition(prefix: str) -> dict[str, ModuleType]:
      modules: dict[str, ModuleType] = {}
      for rel, source in seeds.items():
        target = before_dir / Path(rel).name
        target.write_text(source, encoding='utf-8')
        pinned = manifest.code_states['before']['files'][rel]
        actual = _sha256_text(source)
        if actual != pinned:
          raise ManifestError(f'before source {rel} hash {actual} != {pinned}')
        stem = Path(rel).stem
        modules[stem] = load_code_state(f'{prefix}_{stem}', target)
      for dotted in frozen_modules:
        modules[Path(dotted.replace('.', '/')).name] = frozen_modules[dotted]
      return modules

    states = {
        'before': _load_condition('before'),
        'after': _load_condition('after'),
    }
  # The frozen director must keep the original two-argument resolution
  # signature; a mismatched fixture is a loud fault, never a call with
  # old signatures into a new-tree component.
  for state in states.values():
    director = state.get('drama_director')
    if director is not None:
      params = (
          director.DramaDirector.set_validated_resolution.__code__.co_argcount
          - 1
      )
      if params != 2:
        raise NewContractSourceError(
            'the frozen original-contract director accepts'
            f' {params} set_validated_resolution arguments, expected 2;'
            ' the frozen closure is not the original contract'
        )
  return states


@dataclasses.dataclass
class _UnitContext:
  """One unit's rebuilt production context (frozen fixture in place)."""

  director: Any
  entity: Any
  gm: dict[str, Any]
  player: str
  history: list[str]
  speech: str
  action_intent: str
  action_result: str | None
  candidate: str | None
  draft: str | None = None
  history_override: str | None = None
  generation_prefix: str = ''

  @property
  def history_text(self) -> str:
    """The numbered public-history text (production DisplayEvents)."""
    return (
        self.history_override
        if self.history_override is not None
        else _fmp.public_history_text(self.history)
    )


def _fixture_value(manifest: Manifest, unit: str, field: str) -> Any:
  """Resolves one fixture field, following ``<field>_ref`` links."""
  fixtures = manifest.fixtures[unit]
  if field in fixtures and isinstance(fixtures[field], Mapping):
    return fixtures[field]['value']
  ref_key = f'{field}_ref'
  if ref_key in fixtures:
    return _fixture_value(manifest, fixtures[ref_key], field)
  raise ManifestError(f'fixture {unit}.{field} unresolved')


def _make_probe_director(
    player: str,
    gm: Mapping[str, Any],
    history: Sequence[str],
    director_module: ModuleType | None = None,
) -> tuple[Any, Any]:
  """Builds the probe's director with the repetition gate armed.

  Mirrors ``fixed_model_probe._make_director`` (same beats, scoring
  window, GM boundaries, speech-and-action contract and public state
  restore) but constructs the director with ``feedback_enabled`` and
  ``repetition_push_enabled`` on, as the archived run was configured:
  without them the repetition step's gate never opens and the C units
  would make zero judgment calls.

  Args:
    player: the currently active player character.
    gm: the frozen game-master params.
    history: the committed event window restored into the director.
    director_module: the code state's ``drama_director`` module — the
      frozen original-contract class for historical replay, so the
      frozen steps call the original two-argument resolution API.
      None falls back to the current tree's module (the pre-replay
      behavior; only non-driving tests use it).
  """
  director_model = mock_model.MockModel()
  director_class = (
      director_module.DramaDirector
      if director_module is not None
      else drama_director.DramaDirector
  )
  director = director_class(
      model=director_model,
      beats=_fmp._BEATS,  # pylint: disable=protected-access
      eval_interval=0,
      scoring_window=_fmp._SCORING_WINDOW,  # pylint: disable=protected-access
      allowed_environment_events=gm['allowed_environment_events'],
      immutable_facts=gm['immutable_facts'],
      player_names=gm['player_names'],
      character_constraints=gm['character_constraints'],
      world_constraints=gm['world_constraints'],
      action_mode=story_action.ACTION_MODE_SPEECH_AND_ACTION,
      feedback_enabled=True,
      repetition_push_enabled=True,
  )
  entity = _fmp.entity_agent_with_logging.EntityAgentWithLogging(
      agent_name=gm['name'],
      act_component=_fmp._NoopAct(),  # pylint: disable=protected-access
      context_components={
          '__drama_director__': director,
          _fmp.next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: (
              _fmp._FixedActor(player)  # pylint: disable=protected-access
          ),
      },
  )
  state = dict(director.get_state())
  state['recent_events'] = list(history)
  state['story_turn'] = len(history)
  director.set_state(state)
  return director, entity


def _build_unit_context(
    manifest: Manifest, unit: str, code_state: Mapping[str, ModuleType]
) -> _UnitContext:
  """Rebuilds the unit's director state through the production API.

  Follows the archived probes' conventions: a real ``DramaDirector``
  with the story config's GM boundaries, the committed window and
  story-turn counter restored through the public state API, a real
  turn opened for the frozen speech/action-intent pair, and the frozen
  adjudicated outcome injected with ``record_action_result``. The
  director class comes from the code state so historical replay drives
  the frozen original-contract director.
  """
  gm = _fmp.gm_params()
  base = unit
  if unit.startswith('B-'):
    base = manifest.fixtures[unit].get('context_ref', unit)
    player = manifest.fixtures['B-1']['active_player']
    history = _fixture_value(manifest, base, 'history')
    speech = _fixture_value(manifest, base, 'speech')
    action_intent = _fixture_value(manifest, base, 'action_intent')
    action_result = _fixture_value(manifest, base, 'action_result')
    override = manifest.fixtures[unit].get('action_result_override')
    if isinstance(override, Mapping):
      action_result = override['value']
    draft = (
        manifest.fixtures[unit]['draft']['value']
        if 'draft' in manifest.fixtures[unit]
        else None
    )
    candidate = (
        _fixture_value(manifest, base, 'candidate') if unit == 'B-1' else None
    )
  else:
    player = manifest.fixtures[unit]['active_player']
    history = _fixture_value(manifest, unit, 'history')
    speech = _fixture_value(manifest, unit, 'speech')
    action_intent = _fixture_value(manifest, unit, 'action_intent')
    candidate = _fixture_value(manifest, unit, 'candidate')
    # Batch-faithful: the t9 judgment document's adjudicated-result
    # section carried the composed candidate itself (a000070).
    action_result = candidate
    draft = None
  director, entity = _make_probe_director(
      player,
      gm,
      history,
      director_module=code_state.get('drama_director'),
  )
  _fmp._open_turn(entity, player, speech, action_intent)  # pylint: disable=protected-access
  director.record_action_result(action_result)
  return _UnitContext(
      director=director,
      entity=entity,
      gm=gm,
      player=player,
      history=list(history),
      speech=speech,
      action_intent=action_intent,
      action_result=action_result,
      candidate=candidate,
      draft=draft,
      history_override=(
          _fixture_value(manifest, base, 'history_text')
          if unit.startswith('B-')
          else None
      ),
      generation_prefix=(
          _fixture_value(manifest, base, 'generation_prefix')
          if unit == 'B-1'
          else ''
      ),
  )


def _stage_slots(run_log: Sequence[Mapping[str, Any]], stage: str) -> list[str]:
  """Returns the archived slot ids of one stage's calls."""
  return [entry['slot'] for entry in run_log if entry['stage'] == stage]


def _stage_prompt(
    run_log: Sequence[Mapping[str, Any]], stage: str
) -> str | None:
  """Returns the first prompt of one stage (the controlled prompt)."""
  for entry in run_log:
    if entry['stage'] == stage:
      return entry['prompt']
  return None


class _ReplayPromptModel:
  """Substitutes one unit's controlled-stage calls with the recording.

  Historical replay of an after-condition unit: the frozen step builds
  its own (before-shaped) prompt, and this outermost wrapper replaces
  every controlled-stage call's prompt with the archived original-
  contract recording before it reaches the engine model — so the
  ledger, the raw archive and the diff audit all see exactly the
  prompt the executed batch archived. The substitution is
  classification-based (generation fingerprint / choice kind), so an
  outer retry's re-attempts are substituted equally; every other call
  (the review, the suggestion and fusion stages) forwards unchanged.

  Drift guard: the incoming (discarded) prompt is compared with the
  recording's base prompt on every substituted call. The frozen step
  derives it from the unit's own fixtures under the paired seed, so a
  drifted fixture — the pre-replay prompt-level audit's target —
  still cannot reach the model: a mismatch is a batch fault naming
  the unit, never a silent substitution.
  """

  def __init__(
      self,
      inner: Any,
      recorded_prompt: str,
      kind: str,
      *,
      base_prompt: str | None = None,
      label: str = '',
  ):
    self._inner = inner
    self._recorded = recorded_prompt
    self._kind = kind
    self._base = base_prompt
    self._label = label

  @property
  def model_name(self) -> str:
    return getattr(self._inner, 'model_name', 'stub')

  def bind(self, group: str, stage_label: str) -> None:
    self._inner.bind(group, stage_label)

  def take_run_log(self) -> list[dict[str, Any]]:
    return self._inner.take_run_log()

  def take_run_events(self) -> list[dict[str, Any]]:
    return self._inner.take_run_events()

  def _verify_base(self, prompt: str) -> None:
    if self._base is not None and prompt != self._base:
      raise BatchFault(
          f'{self._label}: the frozen step built a controlled prompt'
          ' that differs from the archived recording base; the'
          " unit's fixture context drifted from the executed batch's"
          ' recording — refusing to substitute'
      )

  def sample_text(self, prompt: str, **kwargs: Any) -> str:
    if self._kind == 'generation' and any(
        mark in prompt for mark, stage in _STAGE_MARKS if stage == 'generation'
    ):
      self._verify_base(prompt)
      return self._inner.sample_text(self._recorded, **kwargs)
    return self._inner.sample_text(prompt, **kwargs)

  def sample_choice(
      self, prompt: str, responses: Sequence[str], *, seed: int | None = None
  ) -> tuple[int, str, Any]:
    if self._kind == 'judgment':
      self._verify_base(prompt)
      return self._inner.sample_choice(self._recorded, responses, seed=seed)
    return self._inner.sample_choice(prompt, responses, seed=seed)


def run_unit(
    manifest: Manifest,
    unit: str,
    condition: str,
    rep: int,
    code_state: Mapping[str, ModuleType],
    model: Any,
    out_dir: Path,
    *,
    progress: dict[str, Any] | None = None,
    recorded_replay_prompt: str | None = None,
    replay_base_prompt: str | None = None,
) -> dict[str, Any]:
  """Runs one unit-condition-repetition through the production steps.

  Args:
    manifest: the data authority.
    unit: unit id (e.g. ``"B-1"``).
    condition: ``"before"`` or ``"after"``.
    rep: repetition round 1-3.
    code_state: the condition's alias-loaded production modules.
    model: the engine model driving the unit — the probe's two-layer
      :class:`ProbeModel`, or an outer wrapper (e.g. the production
      retry wrapper) forwarding its ``bind``/``take_run_log`` protocol.
    out_dir: batch output directory (archives and diagnostics).
    progress: optional record populated in place so a batch stop can
      retain the unit's seeds and RNG proofs alongside durable events.
    recorded_replay_prompt: the archived original-contract controlled
      prompt for an after-condition unit (already reconstructed and
      hash-verified by the caller); when given, the controlled-stage
      calls are replayed through it instead of the frozen step's own
      prompt (historical replay of the executed batch's recording).
    replay_base_prompt: the recording's base prompt (the executed
      before prompt); the replay wrapper compares it with the frozen
      step's own (discarded) prompt so a drifted fixture is a batch
      fault instead of a silent substitution.

  Returns:
    The unit-run record: per-stage states/outcomes/slots, the
    controlled first prompt (for the diff audit) and extras.
  """
  del out_dir
  group = f'{unit}-{condition}'
  seed = manifest.seeds[f'{unit}|rep{rep}']
  context = _build_unit_context(manifest, unit, code_state)
  kind = manifest.units[unit]['kind']
  if recorded_replay_prompt is not None:
    model = _ReplayPromptModel(
        model,
        recorded_replay_prompt,
        'generation' if kind == 'generation_experiment' else 'judgment',
        base_prompt=replay_base_prompt,
        label=f'{unit}-{condition} rep {rep}',
    )
  kind = manifest.units[unit]['kind']
  record: dict[str, Any] = progress if progress is not None else {}
  record.update({
      'unit': unit,
      'condition': condition,
      'rep': rep,
      'seeds': {'py_seed': seed['py_seed'], 'np_seed': seed['np_seed']},
      'rng_proofs': [],
      'stages': {},
      'controlled_prompt': None,
      'extra': {},
  })
  gm = context.gm
  if kind == 'generation_experiment':
    story = code_state['story_resolution']
    step = story.make_final_output_step(
        context.director,
        immutable_facts=gm['immutable_facts'],
        character_constraints=gm['character_constraints'],
        player_names=gm['player_names'],
        world_constraints=gm['world_constraints'],
        allowed_environment_events=gm['allowed_environment_events'],
        npc_response_rules=gm.get('npc_response_rules', ()),
        public_facts=gm['public_facts'],
        public_history=lambda: context.history_text,
    )
    model.bind(group, 'generation')
    with seeded_step(np_seed=seed['np_seed'], py_seed=seed['py_seed']):
      chain = interactive_document.InteractiveDocument(model)
      chain.statement(context.generation_prefix, end='')
      try:
        resolved = step(chain, context.candidate, context.player)
        record['stages'] = {
            'generation': {'state': 'FINISHED', 'outcome': 'ok'},
            'review': {'state': 'FINISHED', 'outcome': 'approved'},
        }
        record['extra']['resolved_event'] = resolved
      except story.StoryOutputError as error:
        message = str(error)
        if message.startswith(_SEMANTIC_REJECTION_PREFIX):
          record['stages'] = {
              'generation': {'state': 'FINISHED', 'outcome': 'ok'},
              'review': {
                  'state': 'FINISHED',
                  'outcome': 'semantic_rejected',
                  'error': message,
              },
          }
        elif message.startswith(_SEMANTIC_STAGE_PREFIX):
          record['stages'] = {
              'generation': {'state': 'FINISHED', 'outcome': 'ok'},
              'review': {
                  'state': 'FAILED',
                  'outcome': 'review_format_failure',
                  'error': message,
              },
          }
        else:
          record['stages'] = {
              'generation': {
                  'state': 'FAILED',
                  'outcome': 'generation_format_failure',
                  'error': message,
              },
              'review': {
                  'state': 'SKIPPED_UPSTREAM',
                  'outcome': 'skipped_upstream_format_failure',
              },
          }
    record['controlled_prompt'] = _stage_prompt(
        model.take_run_log(), 'generation'
    )
  elif kind == 'fixed_draft_review':
    story = code_state['story_resolution']
    constraints = story._constraints_context(  # pylint: disable=protected-access
        immutable_facts=gm['immutable_facts'],
        character_constraints=gm['character_constraints'],
        player_names=gm['player_names'],
        world_constraints=gm['world_constraints'],
        allowed_environment_events=gm['allowed_environment_events'],
        npc_response_rules=gm.get('npc_response_rules', ()),
    )
    canonical = story._assemble_canonical_event(  # pylint: disable=protected-access
        active_player_name=context.player,
        original_speech=context.speech,
        stage_directions=context.draft or '',
    )
    model.bind(group, 'review')
    with seeded_step(np_seed=seed['np_seed'], py_seed=seed['py_seed']):
      document = interactive_document.InteractiveDocument(model)
      _state_review_document(
          document,
          story=story,
          context=context,
          constraints=constraints,
          canonical=canonical,
      )
      verdict = document.open_question(
          _REVIEW_QUESTION, max_tokens=1000, terminators=()
      )
    try:
      story._validate_review(verdict)  # pylint: disable=protected-access
      record['stages'] = {
          'review': {'state': 'FINISHED', 'outcome': 'approved'}
      }
    except story.StoryOutputError as error:
      message = str(error)
      record['stages'] = {
          'review': {
              'state': 'FINISHED',
              'outcome': (
                  'semantic_rejected'
                  if message.startswith(_SEMANTIC_REJECTION_PREFIX)
                  else 'review_format_failure'
              ),
              'error': message,
          }
      }
    record['extra']['canonical_event'] = canonical
  else:  # repetition_probe
    push = code_state['repetition_push']
    step = push.make_repetition_push_step(context.director, model)
    model.bind(group, 'judgment')
    judgment_error: Exception | None = None
    pushed: str | None = None
    with seeded_step(
        np_seed=seed['np_seed'], py_seed=seed['py_seed']
    ) as session, rng_proof(push, session) as proofs:
      record['rng_proofs'] = proofs
      outer = interactive_document.InteractiveDocument(model=model)
      try:
        pushed = step(outer, context.candidate, context.player)
      except language_model.InvalidResponseError as error:
        # Protocol §7 unit-level rule: a judgment that stays invalid
        # after the re-ask loop is this repetition's format failure;
        # the re-ask Q&A itself is archived at the create boundary.
        # Under an outer retry, earlier attempts of this unit may have
        # failed transiently and then recovered; the error belongs to
        # the stage of the LAST failed call, so a stale early failure
        # cannot hijack the classification (Task 3).
        failed_stages = [
            entry['stage']
            for entry in model.take_run_log()
            if entry.get('failed')
        ]
        if failed_stages and failed_stages[-1] == 'judgment':
          judgment_error = error
        else:
          # An empty suggestion/fusion response is not a failed choice.
          # Let the batch handler archive the actual failing stage.
          raise
    run_log = model.take_run_log()
    if judgment_error is None and (
        not any(entry['stage'] == 'judgment' for entry in run_log) or not proofs
    ):
      # A gate that never opened would otherwise masquerade as a
      # legitimate "No" judgment with zero calls; that is a silent
      # integrity failure, never a booked outcome (batch-stop level).
      raise BatchFault(
          f'{group} rep {rep}: the repetition gate did not open (no'
          ' judgment call and no rng proof); the state rebuild'
          ' diverges from the frozen gate fixture'
      )
    if judgment_error is not None:
      record['stages'] = {
          'judgment': {
              'state': 'FAILED',
              'outcome': 'judgment_format_failure',
              'error': f'{type(judgment_error).__name__}: {judgment_error}',
          },
          'suggestion': {
              'state': 'SKIPPED_UPSTREAM',
              'outcome': 'skipped_upstream_format_failure',
          },
          'fusion': {
              'state': 'SKIPPED_UPSTREAM',
              'outcome': 'skipped_upstream_format_failure',
          },
      }
      for stage, entry in cast(
          'dict[str, dict[str, Any]]', record['stages']
      ).items():
        entry['slots'] = _stage_slots(run_log, stage)
      record['extra']['rng_proofs'] = len(proofs)
      record['rng_proofs'] = proofs
      record['controlled_prompt'] = _stage_prompt(run_log, 'judgment')
      return record
    judged_yes = any(entry['stage'] == 'suggestion' for entry in run_log)
    fusion_prompt = next(
        (entry['prompt'] for entry in run_log if entry['stage'] == 'fusion'),
        None,
    )
    selected = _SELECTED_SUGGESTION_RE.search(fusion_prompt or '')
    record['stages'] = {
        'judgment': {
            'state': 'FINISHED',
            'outcome': 'yes' if judged_yes else 'no',
        },
        'suggestion': {
            'state': 'FINISHED' if judged_yes else 'NOT_APPLICABLE',
            'outcome': 'suggested' if judged_yes else 'not_applicable',
        },
        'fusion': {
            'state': 'FINISHED' if judged_yes else 'NOT_APPLICABLE',
            'outcome': 'fused' if judged_yes else 'not_applicable',
        },
    }
    record['extra']['pushed_candidate'] = pushed
    record['extra']['rng_proofs'] = len(proofs)
    record['rng_proofs'] = proofs
    record['extra']['selected_suggestion'] = (
        selected.group(1).strip() if selected else None
    )
    record['controlled_prompt'] = _stage_prompt(run_log, 'judgment')
  for stage, entry in cast(
      'dict[str, dict[str, Any]]', record['stages']
  ).items():
    entry['slots'] = _stage_slots(model.take_run_log(), stage)
  return record


def _state_review_document(
    document: interactive_document.InteractiveDocument,
    *,
    story: ModuleType,
    context: _UnitContext,
    constraints: str,
    canonical: str,
) -> None:
  """States the fixed-draft review document (production mirror).

  Assembles exactly the review the production final-output step builds
  after a draft: same rules, constraints, facts, committed events,
  history, evidence and canonical-event data sections. Only the draft
  is fixed; the review prompt itself is byte-stable across conditions.
  """
  gm = context.gm
  # The section marks come from the code state's own modules (the
  # frozen story_resolution's adjudication binding), so the historical
  # review mirror carries the original-contract marks, never the
  # current tree's relabeled ones.
  saa = getattr(story, 'story_action_adjudication', story_action_adjudication)
  document.statement('# Story resolution semantic review')
  document.statement(story._SEMANTIC_RULES)  # pylint: disable=protected-access
  document.statement('## GM-only constraints\n' + constraints)
  document.statement('## Initial public facts')
  for fact in gm['public_facts']:
    document.statement(fact)
  document.statement('## Committed public events (oldest first)')
  for event in context.director.get_committed_events():
    document.statement(event)
  document.statement(saa.PUBLIC_HISTORY_SECTION_MARK)
  document.statement(json.dumps(context.history_text, ensure_ascii=False))
  document.statement(story._EVIDENCE_SECTION_MARK)  # pylint: disable=protected-access
  document.statement(
      json.dumps(
          {
              'active_player_name': context.player,
              'original_speech': context.speech,
              'action_intent': context.action_intent,
              'action_result': context.action_result,
              'intervention': None,
          },
          ensure_ascii=False,
      )
  )
  document.statement(story._CANONICAL_SECTION_MARK)  # pylint: disable=protected-access
  document.statement(
      json.dumps(
          {
              'resolved_event': canonical,
              'stage_directions': context.draft,
          },
          ensure_ascii=False,
      )
  )


def expected_diff_audit(
    group: str, prompt_before: str, prompt_after: str
) -> list[str]:
  """Audits one controlled-stage prompt pair (protocol §2).

  The first generation prompt (B) and the first judgment prompt (C) of
  a unit-repetition may differ between the two code states by exactly
  the controlled section — the fidelity-guidance block for B, the
  player-contribution data section for C. Removing that section from
  the after prompt must leave the two byte-identical; anything else is
  an unexpected controlled-stage difference (batch-stop level).

  Args:
    group: ``"B"`` or ``"C"``.
    prompt_before: the before condition's controlled prompt.
    prompt_after: the after condition's controlled prompt.

  Returns:
    Violations; an empty list means the pair carries only the
    controlled difference.
  """
  start_mark, end_mark = _AUDIT_SECTIONS[group]
  findings: list[str] = []
  start = prompt_after.find(start_mark)
  if start < 0:
    return [f'after prompt lacks the controlled {start_mark!r} section']
  end = prompt_after.find(end_mark, start)
  if end < 0:
    return [f'after prompt lacks the trailing {end_mark!r} mark']
  stripped = prompt_after[:start] + prompt_after[end:]
  if stripped != prompt_before:
    findings.append(
        'prompts differ beyond the controlled section '
        f'(before {len(prompt_before)} chars, stripped-after '
        f'{len(stripped)} chars)'
    )
  if prompt_before.find(start_mark) >= 0:
    findings.append('before prompt unexpectedly contains the section')
  return findings


@dataclasses.dataclass
class BatchReport:
  """The run's bookkeeping: order, stage states, counts, audits.

  Attributes:
    execution_ledger: one entry per executed unit-condition-rep, in
      execution order (skips and failures included).
    stage_states: every unit-condition-rep-stage mapped to exactly one
      stage state (four-state closure of protocol §7).
    outcomes: per-stage outcome labels (approved/rejected/no/...).
    ledger_counts: Concordia-attempt counts rebuilt from the ledger.
    sdk_counts: SDK-create counts of the second layer.
    audit_records: one record per audited (unit, rep) prompt pair.
    fault: None on a complete run; else the batch-stop record.
  """

  execution_ledger: list[dict[str, Any]]
  stage_states: dict[str, str]
  outcomes: dict[str, str]
  ledger_counts: dict[str, Any]
  sdk_counts: dict[str, Any]
  audit_records: list[dict[str, Any]]
  fault: dict[str, Any] | None
  unit_records: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def _recover_unit_record(
    manifest: Manifest,
    record: dict[str, Any],
    position: tuple[str, str, int],
    model: Any,
    *,
    budget_stop: bool,
) -> dict[str, Any]:
  """Rebuilds interrupted stages from durable per-request observations.

  A completed stage stays completed even when its next stage faults.
  Only a started slot without a terminal ledger event is pending;
  stages whose forwarding never began cannot be pending or failed.

  Args:
    manifest: the data authority.
    record: the in-progress unit record to complete in place.
    position: the ``(unit, condition, rep)`` that was interrupted.
    model: the engine model of the run (the probe model or an outer
      wrapper forwarding ``take_run_events``/``take_run_log``).
    budget_stop: whether the interruption was a budget-layer stop.
  """
  unit, condition, rep = position
  seed = manifest.seeds[f'{unit}|rep{rep}']
  record.setdefault('unit', unit)
  record.setdefault('condition', condition)
  record.setdefault('rep', rep)
  record.setdefault('seeds', dict(seed))
  record.setdefault('rng_proofs', [])
  record.setdefault('extra', {})
  events = model.take_run_events()
  started = [event for event in events if event['event'] == 'started']
  finished = {
      event['slot_id']: event
      for event in events
      if event['event'] == 'finished'
  }
  stages = {}
  upstream_failed = False
  for stage in manifest.units[unit]['stages']:
    slots = [event['slot_id'] for event in started if event['stage'] == stage]
    if not slots:
      state = (
          'NOT_STARTED_BUDGET'
          if budget_stop
          else 'SKIPPED_UPSTREAM'
          if upstream_failed
          else 'NOT_STARTED_ORDER'
      )
      outcome = state.lower()
    else:
      # Task 3: an outer retry may leave earlier failed slots behind;
      # the stage verdict follows its FINAL slot's terminal state
      # (classification by the final attempt), while every earlier
      # slot stays booked in ``slots`` and in the ledger.
      final = finished.get(slots[-1])
      if final is None:
        state, outcome = 'PENDING', 'started_without_terminal_observation'
      elif final['status'] != 'completed':
        state, outcome = 'FAILED', f'{stage}_failed'
      else:
        state, outcome = 'FINISHED', 'completed_before_stop'
      if state == 'FINISHED' and stage == 'judgment':
        # The choice of the unit is the last observed judgment answer
        # (a retried repetition may have answered differently first).
        choice = next(
            (
                entry.get('choice')
                for entry in reversed(model.take_run_log())
                if entry['stage'] == 'judgment' and entry.get('choice')
            ),
            None,
        )
        if choice is not None:
          outcome = choice.lower()
    stages[stage] = {'state': state, 'outcome': outcome, 'slots': slots}
    upstream_failed = upstream_failed or state in ('FAILED', 'PENDING')
  record['stages'] = stages
  record['controlled_prompt'] = _stage_prompt(
      model.take_run_log(),
      'generation' if unit.startswith('B-') else 'judgment',
  )
  return record


def recorded_after_prompt(
    unit: str, condition: str, rep: int, base_prompt: str
) -> str | None:
  """Reconstructs and pin-verifies one after condition's recording.

  The archived after prompt equals the executed before prompt with the
  audited controlled section inserted at the insertion mark — the exact
  equality the executed batch's diff audit certified. The
  reconstruction is verified against the archive's sha256 pin before
  use, so any drift in the frozen closure's execution, the recorded
  section or the reconstruction itself is a batch fault.

  Args:
    unit: the unit id (e.g. ``"B-1"`` or ``"C-2R-DISC"``).
    condition: must be ``"after"`` for a replay entry to exist.
    rep: repetition round.
    base_prompt: the base position's executed controlled prompt (the
      same unit's before condition, or the declared base unit).

  Returns:
    The reconstructed recorded prompt, or None when the unit carries
    no replay entry (before conditions and after-only review units).

  Raises:
    BatchFault: when the insertion mark is absent or the sha256 pin
      disagrees with the reconstruction.
  """
  fixture = load_original_contract()
  entry = fixture['replay']['units'].get(unit)
  if entry is None or condition != 'after':
    return None
  mark = entry['insertion_mark']
  index = base_prompt.find(mark)
  if index < 0:
    raise BatchFault(
        f'{unit} rep {rep}: the base controlled prompt lacks the'
        f' insertion mark {mark!r}; the recorded after prompt cannot be'
        ' reconstructed'
    )
  recorded = base_prompt[:index] + entry['section'] + base_prompt[index:]
  pinned = fixture['replay']['controlled_prompt_sha256'].get(
      f'{unit}|{condition}|{rep}'
  )
  if pinned is not None and _sha256_text(recorded) != pinned:
    raise BatchFault(
        f'{unit} rep {rep}: the reconstructed after prompt disagrees'
        ' with the archived original-contract recording (sha256'
        ' mismatch); the frozen replay diverges from the executed batch'
    )
  return recorded


def run_batch(
    manifest: Manifest,
    model: Any,
    out_dir: Path | str,
    *,
    attempts_global_cap: int | None = None,
    attempts_group_caps: Mapping[str, int] | None = None,
    sdk_create_cap: int | None = None,
    code_states: Mapping[str, Mapping[str, ModuleType]] | None = None,
    secrets: Sequence[str] = (),
    model_wrapper: Callable[[ProbeModel], Any] | None = None,
    model_tag: str = 'stub',
    model_name: str = 'stub',
    integrity_check: Callable[[], None] | None = None,
) -> BatchReport:
  """Runs the whole frozen order on the injected (stub) model.

  Executes the protocol's rotation (round-major, fixed sequence
  within), books every call through the two ledger layers keyed by
  unit-condition, audits each completed before/after prompt pair at
  the controlled stage, and enforces the budget caps: any layer
  reaching its cap stops the batch, and everything not started is
  booked ``NOT_STARTED_BUDGET`` — never rerun, never compensated.

  Args:
    manifest: the data authority.
    model: the injected model (offline stub; a real run injects the
      live model with the frozen §2.2 parameters).
    out_dir: batch output directory (created; offline tests use a
      temporary directory).
    attempts_global_cap: override of the global attempt cap (tests).
    attempts_group_caps: override of the B/C group caps (tests).
    sdk_create_cap: override of the SDK-create cap (tests).
    code_states: preloaded code states; loaded from the manifest when
      omitted.
    secrets: literal values replaced with ``[REDACTED]`` through
      ``request_ledger.redact`` in both ledger layers' archives and in
      every derived artifact of the run — unit records (including
      per-stage error text), both fault branches' ``fault['error']``,
      and the persisted execution-ledger, stage-state and audit
      payloads; rubric rows are scrubbed by ``write_rubric_report``.
    model_wrapper: wraps the constructed :class:`ProbeModel` into the
      engine model the production steps actually call. The outer
      production retry wrapper must sit here — outside the probe — so
      every retried forwarding stays one billed attempt; placing it
      inside (on ``model``) would let retries escape the ledger. The
      wrapper must forward ``bind``, ``take_run_log``,
      ``take_run_events`` and the two sampling methods.
    model_tag: request-identity tag recorded by both ledger layers'
      ``started`` events (the frozen run uses ``'m4'``).
    model_name: request-identity model name recorded by both layers
      (the frozen run uses ``'gpt-6.1-sol'``).
    integrity_check: batch-level integrity gate invoked before every
      attempt (before any budget layer is consulted) and before every
      SDK create, including each client-boundary create of the
      judgment re-ask loop; its exception propagates unchanged and
      stops the batch as a fault.

  Returns:
    The :class:`BatchReport`.
  """
  out_dir = Path(out_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  secrets = tuple(secrets)

  def scrub(value: Any) -> Any:
    """Scrubs a bookkeeping payload; a no-op pass-through unsecrets."""
    return rl.redact(value, secrets) if secrets else value

  budget = manifest.budget
  attempt_cap = (
      attempts_global_cap
      if attempts_global_cap is not None
      else budget['concordia_attempts']['global_cap']
  )
  group_caps = dict(
      attempts_group_caps
      if attempts_group_caps is not None
      else budget['concordia_attempts']['group_caps']
  )
  create_cap = (
      sdk_create_cap
      if sdk_create_cap is not None
      else budget['sdk_create']['global_cap']
  )
  ledger = rl.RequestLedger(
      out_dir,
      batch_id='fixed-probe',
      global_cap=attempt_cap,
      model_caps={},
      group_caps={},
      sample_caps={},
      secrets=secrets,
  )
  probe_model = ProbeModel(
      model,
      ledger=ledger,
      sdk_guard=SdkCreateGuard(
          out_dir,
          create_cap=create_cap,
          model_tag=model_tag,
          model_name=model_name,
          secrets=secrets,
          integrity_check=integrity_check,
      ),
      out_dir=out_dir,
      model_tag=model_tag,
      model_name=model_name,
      attempt_group_caps=group_caps,
      integrity_check=integrity_check,
  )
  # The engine the production steps call: the outer retry wrapper (or
  # any other interposer) wraps the probe here, never the inner model,
  # so every retried forwarding still passes the two ledger layers.
  engine_model = (
      model_wrapper(probe_model) if model_wrapper is not None else probe_model
  )
  states = code_states or load_code_states(manifest, out_dir)
  replay_units = load_original_contract()['replay']['units']
  rounds = manifest.order['rounds']
  sequence = [tuple(step) for step in manifest.order['round_sequence']]
  ordered_stages = [
      (unit, condition, rep, stage)
      for rep in rounds
      for unit, condition in sequence
      for stage in manifest.units[unit]['stages']
  ]
  stage_states: dict[str, str] = {}
  outcomes: dict[str, str] = {}
  execution_ledger: list[dict[str, Any]] = []
  audit_pairs: dict[tuple[str, int], dict[str, str]] = {}
  audit_records: list[dict[str, Any]] = []
  unit_records: list[dict[str, Any]] = []
  controlled_by_position: dict[tuple[str, str, int], str] = {}
  fault: dict[str, Any] | None = None
  key_of = lambda u, c, r, s: f'{u}|{c}|r{r}|{s}'

  def _recorded_replay_prompt(
      unit: str, condition: str, rep: int
  ) -> tuple[str | None, str | None]:
    """Resolves one after condition's base position and its recording."""
    entry = replay_units.get(unit)
    if entry is None or condition != 'after':
      return None, None
    base_unit, base_condition = entry['base']
    base = controlled_by_position.get((base_unit, base_condition, rep))
    if base is None:
      raise BatchFault(
          f'{unit} rep {rep}: the replay base position'
          f' ({base_unit}, {base_condition}) has not produced its'
          ' controlled prompt yet; the recorded after prompt cannot be'
          ' reconstructed'
      )
    return recorded_after_prompt(unit, condition, rep, base), base

  def _mark_remaining(state: str) -> None:
    for unit, condition, rep, stage in ordered_stages:
      key = key_of(unit, condition, rep, stage)
      stage_states.setdefault(key, state)

  def _budget_headroom(group_letter: str) -> None:
    counts = ledger.counts
    if counts['global'] >= attempt_cap:
      raise BudgetStop(f'global attempt cap {attempt_cap} reached')
    used = sum(
        value
        for name, value in counts['by_group'].items()
        if name.startswith(f'{group_letter}-')
    )
    if used >= group_caps[group_letter]:
      raise BudgetStop(
          f'group {group_letter} attempt cap {group_caps[group_letter]} reached'
      )
    if probe_model._sdk_guard.counts['global'] >= create_cap:  # pylint: disable=protected-access
      raise BudgetStop(f'sdk create cap {create_cap} reached')

  current_position: tuple[str, str, int] | None = None
  current_record: dict[str, Any] = {}

  def _save_record(record: dict[str, Any]) -> None:
    unit, condition, rep = record['unit'], record['condition'], record['rep']
    unit_records.append(scrub(record))
    ledger_entry = {
        'unit': unit,
        'condition': condition,
        'rep': rep,
        'seeds': record['seeds'],
        'rng_proofs': record['rng_proofs'],
        'stages': {s: e['state'] for s, e in record['stages'].items()},
        'slots': {s: e['slots'] for s, e in record['stages'].items()},
    }
    execution_ledger.append(scrub(ledger_entry))
    for stage, entry in record['stages'].items():
      stage_states[key_of(unit, condition, rep, stage)] = entry['state']
      outcomes[key_of(unit, condition, rep, stage)] = entry['outcome']

  try:
    for rep in rounds:
      for unit, condition in sequence:
        _budget_headroom(unit[0])
        current_position = (unit, condition, rep)
        current_record = {}
        probe_model.bind(f'{unit}-{condition}', 'request')
        replay_prompt, replay_base = _recorded_replay_prompt(
            unit, condition, rep
        )
        record = run_unit(
            manifest,
            unit,
            condition,
            rep,
            states[condition],
            engine_model,
            out_dir,
            progress=current_record,
            recorded_replay_prompt=replay_prompt,
            replay_base_prompt=replay_base,
        )
        _save_record(record)
        current_position = None
        controlled = record['controlled_prompt']
        if controlled is not None:
          controlled_by_position[(unit, condition, rep)] = controlled
        if controlled is not None:
          pair = audit_pairs.setdefault((unit, rep), {})
          pair[condition] = controlled
          if len(pair) == 2:
            findings = expected_diff_audit(
                unit[0], pair['before'], pair['after']
            )
            audit_records.append({
                'unit': unit,
                'rep': rep,
                'findings': findings,
            })
            if findings:
              raise BatchFault(
                  f'controlled-stage diff audit failed for {unit}'
                  f' rep {rep}: {findings}'
              )
    unassigned = [
        key
        for unit, condition, rep, stage in ordered_stages
        if (key := key_of(unit, condition, rep, stage)) not in stage_states
    ]
    if unassigned:
      raise BatchFault(f'stage bookkeeping hole: {unassigned[:5]}')
  except BudgetStop as error:
    fault = {'kind': 'budget_stop', 'error': scrub(str(error))}
    if current_position is not None:
      _save_record(
          _recover_unit_record(
              manifest,
              current_record,
              current_position,
              engine_model,
              budget_stop=True,
          )
      )
    _mark_remaining('NOT_STARTED_BUDGET')
  except BaseException as error:  # pylint: disable=broad-except
    fault = {
        'kind': 'batch_fault',
        'error': scrub(f'{type(error).__name__}: {error}'),
    }
    if current_position is not None:
      _save_record(
          _recover_unit_record(
              manifest,
              current_record,
              current_position,
              engine_model,
              budget_stop=False,
          )
      )
    _mark_remaining('NOT_STARTED_ORDER')
    report = BatchReport(
        execution_ledger=execution_ledger,
        stage_states=stage_states,
        outcomes=outcomes,
        ledger_counts=ledger.counts,
        sdk_counts=probe_model._sdk_guard.counts,  # pylint: disable=protected-access
        audit_records=audit_records,
        fault=fault,
        unit_records=unit_records,
    )
    _write_batch_artifacts(out_dir, report, secrets)
    raise
  report = BatchReport(
      execution_ledger=execution_ledger,
      stage_states=stage_states,
      outcomes=outcomes,
      ledger_counts=ledger.counts,
      sdk_counts=probe_model._sdk_guard.counts,  # pylint: disable=protected-access
      audit_records=audit_records,
      fault=fault,
      unit_records=unit_records,
  )
  _write_batch_artifacts(out_dir, report, secrets)
  return report


def _write_batch_artifacts(
    out_dir: Path, report: BatchReport, secrets: Sequence[str] = ()
) -> None:
  """Persists the run's bookkeeping artifacts.

  Writes the execution ledger, the stage-state/outcome/fault payload,
  the audit records, both layers' counts and the per-unit records —
  on the success path and on the fault path alike, so a stopped batch
  still carries everything the evaluation tables (protocol §5) and
  ``rebuild_batch_report`` need. Every persisted payload is passed
  through ``request_ledger.redact`` when secrets are configured, so
  the on-disk artifacts stay scrubbed independently of the build-time
  redaction inside ``run_batch``.
  """

  def _scrub(value: Any) -> Any:
    return rl.redact(value, secrets) if secrets else value

  with (out_dir / 'execution_ledger.jsonl').open(
      'a', encoding='utf-8'
  ) as handle:
    for entry in report.execution_ledger:
      handle.write(json.dumps(_scrub(entry), ensure_ascii=False) + '\n')
  _write_json_file(
      out_dir / 'stage_states.json',
      _scrub({
          'stage_states': report.stage_states,
          'outcomes': report.outcomes,
          'fault': report.fault,
      }),
  )
  _write_json_file(out_dir / 'audit_records.json', _scrub(report.audit_records))
  _write_json_file(
      out_dir / 'report.json',
      _scrub({
          'ledger_counts': report.ledger_counts,
          'sdk_counts': report.sdk_counts,
          'fault': report.fault,
          'execution_entries': len(report.execution_ledger),
      }),
  )
  _write_json_file(out_dir / 'unit_records.json', _scrub(report.unit_records))


def _write_json_file(
    path: Path, payload: 'Mapping[str, Any] | Sequence[Any]'
) -> None:
  """Writes indented JSON with a trailing newline."""
  path.write_text(
      json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
      encoding='utf-8',
  )


def rebuild_batch_report(out_dir: Path | str) -> BatchReport:
  """Rebuilds a :class:`BatchReport` from a batch directory's artifacts.

  Pure deserialization of what ``_write_batch_artifacts`` persisted:
  the evaluation pipeline of protocol §5 must also run when the batch
  faulted and ``run_batch`` raised, so the report is reconstructed
  from disk alone — the execution ledger, the stage-state/outcome and
  fault payload, the audit records, both layers' counts and the unit
  records. Nothing is re-derived or re-audited here: the persisted
  artifacts are the record.

  Args:
    out_dir: the batch output directory.

  Returns:
    The rebuilt :class:`BatchReport`.

  Raises:
    OSError: if a required artifact file is missing.
    KeyError: if a required field is absent from an artifact.
  """
  out_dir = Path(out_dir)
  execution_ledger = [
      json.loads(line)
      for line in (
          (out_dir / 'execution_ledger.jsonl')
          .read_text(encoding='utf-8')
          .splitlines()
      )
      if line.strip()
  ]
  stage_payload = json.loads(
      (out_dir / 'stage_states.json').read_text(encoding='utf-8')
  )
  report_payload = json.loads(
      (out_dir / 'report.json').read_text(encoding='utf-8')
  )
  return BatchReport(
      execution_ledger=execution_ledger,
      stage_states=stage_payload['stage_states'],
      outcomes=stage_payload['outcomes'],
      ledger_counts=report_payload['ledger_counts'],
      sdk_counts=report_payload['sdk_counts'],
      audit_records=json.loads(
          (out_dir / 'audit_records.json').read_text(encoding='utf-8')
      ),
      fault=stage_payload.get('fault'),
      unit_records=json.loads(
          (out_dir / 'unit_records.json').read_text(encoding='utf-8')
      ),
  )


def write_rubric_report(
    manifest: Manifest,
    report: BatchReport,
    out_dir: Path | str,
    *,
    secrets: Sequence[str] = (),
) -> dict[str, Any]:
  """Writes the preregistered evaluation-table skeleton (protocol §5).

  Only observed facts are prefilled — ids, the C judgment choice with
  its actual suggestion/fusion outputs, and the fixed "模型理由: 未提
  供" that the production chain genuinely never provides. Every agent
  assessment column stays empty and every human column stays pending:
  the rubric was frozen before execution and agent initial assessments
  are filled only afterwards, never by the harness.

  Args:
    manifest: the data authority (frozen table vocabularies).
    report: the run's bookkeeping.
    out_dir: batch directory the skeleton is written to.
    secrets: literal values replaced with ``[REDACTED]`` in every
      string value of the written (and returned) tables — the
      model-selection prefill quotes the raw suggestion/fusion text,
      which must not carry secrets into the evaluation tables.

  Returns:
    The written structure (``evaluation_tables.json``).
  """
  b_columns = manifest.labels['b_table_columns']
  c_columns = manifest.labels['c_table_columns']
  b_rows: list[dict[str, Any]] = []
  c_rows: list[dict[str, Any]] = []
  for record in report.unit_records:
    if record['unit'].startswith('B-'):
      row = {
          '单元': record['unit'],
          '条件': record['condition'],
          '重复': record['rep'],
      }
      for column in manifest.labels['b_rubric_columns']:
        row[column] = ''
      b_rows.append(row)
    else:
      key = f"{record['unit']}|{record['condition']}|r{record['rep']}"
      judged = report.outcomes[f'{key}|judgment']
      choice = {'yes': 'Yes', 'no': 'No'}.get(judged)
      if choice == 'Yes':
        suggestion = record['extra'].get('selected_suggestion')
        fusion = record['extra'].get('pushed_candidate')
        choice = (
            f'Yes；建议：{suggestion}；融合：{fusion}'
            if suggestion
            else f'Yes；融合：{fusion}'
        )
      row = {
          '单元': record['unit'],
          '条件': record['condition'],
          '重复': record['rep'],
          '模型选择': choice,
          '阶段状态': report.stage_states[f'{key}|judgment'],
          '阶段结果': judged,
          '模型理由': '未提供',
          'agent 评价依据': '',
          '人工评价依据': 'pending',
      }
      for column in c_columns:
        row.setdefault(column, '')
      c_rows.append(row)
  outcomes = report.outcomes
  tables = {
      'b_table': {'columns': b_columns, 'rows': b_rows},
      'c_table': {'columns': c_columns, 'rows': c_rows},
      'counts': {
          'semantic_rejections': sum(
              1 for value in outcomes.values() if value == 'semantic_rejected'
          ),
          'generation_format_failures': sum(
              1
              for value in outcomes.values()
              if value == 'generation_format_failure'
          ),
          'review_format_failures': sum(
              1
              for value in outcomes.values()
              if value == 'review_format_failure'
          ),
          'judgment_yes': sum(
              1
              for name, value in outcomes.items()
              if name.endswith('|judgment') and value == 'yes'
          ),
          'judgment_no': sum(
              1
              for name, value in outcomes.items()
              if name.endswith('|judgment') and value == 'no'
          ),
          'judgment_format_failures': sum(
              value == 'judgment_format_failure' for value in outcomes.values()
          ),
          'human_pending': len(c_rows),
      },
  }
  if secrets:
    tables = rl.redact(tables, secrets)
  _write_json_file(Path(out_dir) / 'evaluation_tables.json', tables)
  return tables
