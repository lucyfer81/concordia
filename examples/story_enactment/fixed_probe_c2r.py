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

r"""C-2R replacement mini-probe: manifest, compat view, three-arm audit.

The old C-2 fixture lost its positive-example status because its public
debt admission violated the speaker's bottom line (see the replacement
design). C-2R substitutes a legal first-hand eyewitness disclosure by
林雪 — sourced verbatim from the R2 t6 speech, over the continuous
t1–t5 committed prefix — and executes it as three arms sharing one
before-state baseline: ``before-base`` (the pre-change code, whose
judgment prompt carries no player-contribution zone), ``after-
disclosure`` (the current code plus the contribution zone carrying the
t6 speech) and ``after-waiting`` (the current code plus the same zone
carrying a third restatement of the standing read-aloud request).

This module is a thin adapter over the accepted fixed-sample harness
(:mod:`examples.story_enactment.fixed_probe`): execution, retry
classification, recovery and bookkeeping are reused by direct calls
(``run_unit``/``run_batch``/``load_code_states``/
``write_rubric_report``); nothing here re-implements them. What this
module owns is the C-2R data authority (:class:`C2RManifest`), the
compatibility view that presents the three arms as single-condition
virtual units to the existing engine
(:func:`build_compat_view`), and the three-arm controlled-difference
audit (:func:`audit_controlled_differences`). Real execution is
separately authorized; offline tests run on deterministic stubs with
zero network, zero credentials and zero ``run_results`` dependence.
"""

from collections.abc import Mapping, Sequence
import copy as copylib
import dataclasses
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from examples.story_enactment import fixed_probe as fp

# Schema identifier of the C-2R data authority.
C2R_MANIFEST_SCHEMA = 'fixed-probe-c2r-manifest-v1'

# The shared unit id of the three arms; seeds, budget and order are
# keyed on it (never on the arm), so every arm of one repetition
# shares one paired seed tuple.
C2R_UNIT = 'C-2R'

# The three arms and the within-round execution order (BASE -> DISC ->
# WAIT): the baseline runs first so both comparisons share it live.
ARM_BASE = 'before-base'
ARM_DISCLOSURE = 'after-disclosure'
ARM_WAITING = 'after-waiting'
C2R_ARMS = (ARM_BASE, ARM_DISCLOSURE, ARM_WAITING)

# Re-exported for callers of this module (the manifest error type is
# the harness's own: structural violations are the same failure class).
ManifestError = fp.ManifestError

# The frozen five text fingerprints of the replacement design §8
# (UTF-8, no trailing newline): disclosure speech, control speech,
# action intent, action result and the composed candidate.
_FROZEN_FINGERPRINTS = {
    'speech': (
        '2fafe49a13768a1f40d499a43b5c542b8845679bbb23aa94f27a55518f96f1b1'
    ),
    'speech_control': (
        '3dd53e0723095787a73d3a8852e825c9a070631e7e2392922420d2d7dc608b22'
    ),
    'action_intent': (
        '810666dc159e5f14e27b6cf1e23bb7e47708c92fcd3e4e271c0d5b08325b0f79'
    ),
    'result': (
        '56b98b6a5e2c2fc03558bff0f182a716902d3c62ecb0337bc346380ecbdc6408'
    ),
    'candidate': (
        '65ef47a0a19b2dc69061e850a9bd117de61fd99a9bdb508cf183387a10ad679b'
    ),
}

# The R2 t6 gate witness: the repetition gate really opened and judged
# "not repetitive" in the source run — the probe rebuilds that state.
_FROZEN_GATE = {
    'reason': 'repetition',
    'checked': True,
    'attempted': False,
    'proposed_change': False,
    'executed': False,
}

# The before state differs from the current tree in exactly one file
# (the C judgment chain loads no story_resolution module); its
# repository path and pinned sha256.
_BEFORE_REL = 'concordia/contrib/components/game_master/repetition_push.py'
_BEFORE_SHA256 = (
    '7a396b2af4f913929ecba455e8c6f901bc7b1ff40d56ce8b43b2eb555ee73863'
)


@dataclasses.dataclass(frozen=True)
class C2RManifest:
  """The loaded, structure-verified data authority of the C-2R probe.

  Attributes:
    design_sha256: sha256 of the replacement design document this
      manifest implements.
    batch_root: repository-relative root of the frozen source batch.
    seeds: paired seed table keyed ``"C-2R|rep<rep>"``; all three arms
      of one repetition share the same pair.
    budget: frozen caps (27 Concordia attempts, 81 SDK creates, 243
      HTTP disclosure bound) and the 27-logical-stage-key record rule.
    order: frozen execution order (rounds and the BASE→DISC→WAIT
      within-round sequence).
    model_params: frozen real-run model parameters per stage.
    code_states: the before state's embedded, hash-pinned source (one
      file); after is the current working tree.
    batch_sources: the R2 turns file (path + sha256) the fixtures
      derive from; freeze copies it into the batch.
    fixtures: the C-2R fixture set (history, both speeches, action
      intent, candidate, gate witness, active player, fingerprints,
      arms).
    raw: the complete parsed JSON.
  """

  design_sha256: str
  batch_root: str
  seeds: dict[str, dict[str, int]]
  budget: dict[str, Any]
  order: dict[str, Any]
  model_params: dict[str, Any]
  code_states: dict[str, Any]
  batch_sources: dict[str, dict[str, str]]
  fixtures: dict[str, dict[str, Any]]
  raw: dict[str, Any]


def load_c2r_manifest(path: Path | str) -> C2RManifest:
  """Loads and structure-verifies the C-2R data-authority JSON.

  Args:
    path: path to ``fixed_probe_c2r_manifest.json``.

  Returns:
    The verified :class:`C2RManifest`.

  Raises:
    ManifestError: if the file is unreadable, not the expected schema,
      or fails the structural verification.
  """
  try:
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
  except (OSError, json.JSONDecodeError) as error:
    raise ManifestError(f'cannot load C-2R manifest {path}: {error}') from error
  if raw.get('schema') != C2R_MANIFEST_SCHEMA:
    raise ManifestError(
        f'C-2R manifest schema {raw.get("schema")!r} != {C2R_MANIFEST_SCHEMA!r}'
    )
  manifest = C2RManifest(
      design_sha256=raw['design_sha256'],
      batch_root=raw['batch_root'],
      seeds=raw['seeds'],
      budget=raw['budget'],
      order=raw['order'],
      model_params=raw['model_params'],
      code_states=raw['code_states'],
      batch_sources=raw['batch_sources'],
      fixtures=raw['fixtures'],
      raw=raw,
  )
  verify_c2r_manifest(manifest)
  return manifest


def verify_c2r_manifest(manifest: C2RManifest) -> None:
  """Verifies the C-2R manifest's frozen structure and pinned values.

  Checks the paired seeds against the unchanged derivation rule, the
  frozen budget (27/81/243 plus the record-rule keys), the frozen
  order and arms, the embedded before source's pinned sha256, the five
  text fingerprints against the inline fixture values themselves, and
  the frozen gate witness. Batch re-extraction against a real batch
  directory belongs to the runner's verify stage, not here.

  Args:
    manifest: the manifest to verify.

  Raises:
    ManifestError: on any structural violation or pinned-value drift.
  """
  if (
      not isinstance(manifest.design_sha256, str)
      or len(manifest.design_sha256) != 64
  ):
    raise ManifestError('design_sha256 is not a 64-hex digest')
  _verify_c2r_seeds(manifest)
  _verify_c2r_budget(manifest)
  _verify_c2r_order(manifest)
  _verify_c2r_code_states(manifest)
  _verify_c2r_sources(manifest)
  _verify_c2r_fixtures(manifest)


def _verify_c2r_seeds(manifest: C2RManifest) -> None:
  """Checks the seed table against the derivation rule, per rep."""
  expected_keys = {f'{C2R_UNIT}|rep{rep}' for rep in (1, 2, 3)}
  if set(manifest.seeds) != expected_keys:
    raise ManifestError(
        f'seed table keys {sorted(manifest.seeds)} != {sorted(expected_keys)}'
    )
  for key, entry in manifest.seeds.items():
    if set(entry) != {'py_seed', 'np_seed'}:
      raise ManifestError(f'seed entry {key} has fields {sorted(entry)}')
    _, rep = key.split('|rep')
    derived = fp.derive_seeds(C2R_UNIT, int(rep))
    if (entry['py_seed'], entry['np_seed']) != derived:
      raise ManifestError(
          f'seed entry {key} {entry} disagrees with derivation {derived}'
      )


def _verify_c2r_budget(manifest: C2RManifest) -> None:
  """Checks the frozen two-layer caps and record-rule keys."""
  attempts = manifest.budget['concordia_attempts']
  if attempts['global_cap'] != 27:
    raise ManifestError('attempts global cap != 27')
  if manifest.budget['sdk_create']['global_cap'] != 81:
    raise ManifestError('SDK create cap != 81')
  if manifest.budget['http_upper_bound_disclosure'] != 243:
    raise ManifestError('HTTP disclosure bound != 243')
  if manifest.budget['ledger_group_key'] != 'unit-condition':
    raise ManifestError("ledger group key != 'unit-condition'")
  if manifest.budget['logical_stage_keys'] != 27:
    raise ManifestError('logical stage keys != 27')


def _verify_c2r_order(manifest: C2RManifest) -> None:
  """Checks the frozen rounds and within-round arm sequence."""
  if manifest.order['rounds'] != [1, 2, 3]:
    raise ManifestError('order rounds != [1, 2, 3]')
  expected_sequence = [
      [C2R_UNIT, ARM_BASE],
      [C2R_UNIT, ARM_DISCLOSURE],
      [C2R_UNIT, ARM_WAITING],
  ]
  if manifest.order['round_sequence'] != expected_sequence:
    raise ManifestError(
        f"order round sequence != BASE -> DISC -> WAIT over '{C2R_UNIT}'"
    )


def _verify_c2r_code_states(manifest: C2RManifest) -> None:
  """Checks the embedded, single-file before state."""
  before = manifest.code_states.get('before', {})
  files = before.get('files', {})
  sources = before.get('sources', {})
  if set(files) != {_BEFORE_REL} or set(sources) != {_BEFORE_REL}:
    raise ManifestError(
        f'before state must pin exactly [{_BEFORE_REL}] (the C judgment '
        'chain loads no story_resolution module)'
    )
  actual = hashlib.sha256(sources[_BEFORE_REL].encode('utf-8')).hexdigest()
  if actual != _BEFORE_SHA256:
    raise ManifestError(
        f'embedded before source {_BEFORE_REL} hash {actual}'
        f' != pinned {_BEFORE_SHA256}'
    )
  if files[_BEFORE_REL] != _BEFORE_SHA256 or before.get('sha256') != (
      _BEFORE_SHA256
  ):
    raise ManifestError('before-state hash pins disagree with the frozen sha')


def _verify_c2r_sources(manifest: C2RManifest) -> None:
  """Checks the batch-source table (structural; hashes pin at freeze)."""
  if set(manifest.batch_sources) != {'R2_turns'}:
    raise ManifestError('batch sources != {R2_turns}')
  source = manifest.batch_sources['R2_turns']
  if source['path'] != 'R2/story_turns.jsonl':
    raise ManifestError("R2_turns path != 'R2/story_turns.jsonl'")
  if not isinstance(source.get('sha256'), str) or len(source['sha256']) != 64:
    raise ManifestError('R2_turns has no 64-hex sha256')


def _fixture_entry(
    fixtures: Mapping[str, Any], field: str
) -> Mapping[str, Any]:
  """Returns one kind-tagged fixture entry; plain strings are refused."""
  entry = fixtures.get(field)
  if not isinstance(entry, Mapping) or 'value' not in entry:
    raise ManifestError(f'fixture C-2R.{field} is not a kind-tagged entry')
  return entry


def _verify_c2r_fixtures(manifest: C2RManifest) -> None:
  """Checks arms, fingerprints, gate witness and fixture shapes."""
  if set(manifest.fixtures) != {C2R_UNIT}:
    raise ManifestError(f'fixtures != {{{C2R_UNIT!r}}}')
  fixtures = manifest.fixtures[C2R_UNIT]
  if tuple(fixtures['arms']) != C2R_ARMS:
    raise ManifestError(f'arms != {list(C2R_ARMS)}')
  if fixtures['active_player'] != '林雪':
    raise ManifestError("active player != '林雪'")
  for field in (
      'history',
      'speech_disclosure',
      'speech_control',
      'action_intent',
      'candidate',
      'gate',
  ):
    entry = _fixture_entry(fixtures, field)
    if entry.get('kind') in (
        'jsonl_field_list',
        'jsonl_field',
        'response_json_field',
        'whole_file_text',
        'prompt_section',
        'prompt_json_field',
        'prompt_prefix',
    ):
      if entry.get('source') not in manifest.batch_sources:
        raise ManifestError(
            f'fixture C-2R.{field} has unknown source {entry.get("source")!r}'
        )
    elif entry.get('kind') != 'inline':
      raise ManifestError(
          f'fixture C-2R.{field} has unknown kind {entry.get("kind")!r}'
      )
  history = _fixture_entry(fixtures, 'history')['value']
  turn_ids = _fixture_entry(fixtures, 'history').get('turn_ids')
  if (
      not isinstance(history, list)
      or not history
      or not all(isinstance(event, str) for event in history)
  ):
    raise ManifestError('history value is not a non-empty list of strings')
  if not isinstance(turn_ids, list) or len(turn_ids) != len(history):
    raise ManifestError('history turn_ids and value disagree in length')
  fingerprints = fixtures.get('fingerprints')
  if fingerprints != _FROZEN_FINGERPRINTS:
    raise ManifestError('fingerprints != the frozen five of design §8')
  speech = _fixture_entry(fixtures, 'speech_disclosure')['value']
  control = _fixture_entry(fixtures, 'speech_control')['value']
  intent = _fixture_entry(fixtures, 'action_intent')['value']
  candidate = _fixture_entry(fixtures, 'candidate')['value']
  if not all(isinstance(v, str) for v in (speech, control, intent, candidate)):
    raise ManifestError('speech/intent/candidate values must be strings')
  prefix = intent + ' Because of that, '
  checks = {
      'speech': speech,
      'speech_control': control,
      'action_intent': intent,
      'candidate': candidate,
      'result': (
          candidate[len(prefix) :] if candidate.startswith(prefix) else ''
      ),
  }
  if not candidate.startswith(prefix):
    raise ManifestError('candidate is not intent + result composed')
  for field, value in checks.items():
    actual = hashlib.sha256(value.encode('utf-8')).hexdigest()
    if actual != _FROZEN_FINGERPRINTS[field]:
      raise ManifestError(
          f'fixture C-2R value {field} hash {actual} != fingerprint '
          f'{_FROZEN_FINGERPRINTS[field]}'
      )
  if _fixture_entry(fixtures, 'gate')['value'] != _FROZEN_GATE:
    raise ManifestError('gate witness != the frozen R2 t6 diagnostics')


# ---------------------------------------------------------------------------
# Compatibility view: the three arms as single-condition virtual units
# ---------------------------------------------------------------------------

# Arm -> (virtual unit, condition). The virtual unit ids share the
# "C-2R" prefix so the engine's group-letter budgeting sees one C
# group; the single-condition design keeps run_batch's built-in
# before/after pair audit from ever firing (each unit-condition pair
# occurs exactly once), which assert_no_builtin_pairs pins.
C2RVirtualUnits = {
    ARM_BASE: ('C-2R-BASE', 'before'),
    ARM_DISCLOSURE: ('C-2R-DISC', 'after'),
    ARM_WAITING: ('C-2R-WAIT', 'after'),
}

# The §5 preregistered evaluation-table columns (beyond the fixed
# id/choice columns write_rubric_report prefills).
_C2R_TABLE_COLUMNS = (
    '当轮实际贡献',
    '主张归属',
    '建议是否合法相关',
    '是否新增无必要任务',
    '是否复制或改写玩家对白',
)


def c2r_fixture_value(manifest: C2RManifest, field: str) -> Any:
  """Resolves one fixture value of the C-2R fixture set.

  Args:
    manifest: the verified C-2R manifest.
    field: fixture field (``history``, ``speech_disclosure``,
      ``speech_control``, ``action_intent``, ``candidate``, ``gate``).

  Returns:
    The inline frozen value (string, list of strings, or the gate
    witness dict).
  """
  return _fixture_entry(manifest.fixtures[C2R_UNIT], field)['value']


def build_compat_view(
    manifest: C2RManifest, *, base_speech: str | None = None
) -> fp.Manifest:
  """Builds the view the existing execution engine consumes directly.

  The view presents the three arms as three single-condition virtual
  units over one shared fixture set: ``history``/``action_intent``/
  ``candidate``/``gate``/``active_player`` are identical, and only the
  ``speech`` differs — the disclosure speech for BASE and DISC, the
  control speech for WAIT. ``run_unit``'s repetition branch rebuilds
  the director context from these fixtures itself (including the
  batch-faithful convention that ``action_result`` is the composed
  candidate); nothing here reconstructs execution, recovery or
  bookkeeping.

  Seeds: every virtual unit's ``rep<r>`` key carries the same pair,
  ``derive_seeds("C-2R", rep)`` — the shared-baseline pairing point.
  Budget and order are pass-through placeholders; the C-2R manifest
  stays the authority and the runner passes explicit cap overrides.
  ``base_speech`` swaps only the BASE arm's speech (used by the
  constructive before-state byte-identity check and the preflight
  stage); it never touches the after arms.

  Args:
    manifest: the verified C-2R manifest.
    base_speech: optional override of the BASE arm's speech.

  Returns:
    The compatibility view as a :class:`fp.Manifest`.
  """
  fixtures = manifest.fixtures[C2R_UNIT]
  speech_disclosure = _fixture_entry(fixtures, 'speech_disclosure')['value']
  speech_control = _fixture_entry(fixtures, 'speech_control')['value']
  base_speech_value = speech_disclosure if base_speech is None else base_speech
  speech_by_unit = {
      'C-2R-BASE': base_speech_value,
      'C-2R-DISC': speech_disclosure,
      'C-2R-WAIT': speech_control,
  }
  shared = {
      'history': _fixture_entry(fixtures, 'history'),
      'action_intent': _fixture_entry(fixtures, 'action_intent'),
      'candidate': _fixture_entry(fixtures, 'candidate'),
      'gate': _fixture_entry(fixtures, 'gate'),
  }
  units: dict[str, dict[str, Any]] = {}
  view_fixtures: dict[str, dict[str, Any]] = {}
  seeds: dict[str, dict[str, int]] = {}
  sequence: list[list[str]] = []
  for arm in C2R_ARMS:
    unit, condition = C2RVirtualUnits[arm]
    units[unit] = {
        'group': 'C',
        'kind': 'repetition_probe',
        'conditions': [condition],
        'stages': ['judgment', 'suggestion', 'fusion'],
        'calls_per_rep': {'min': 1, 'max': 3},
    }
    view_fixtures[unit] = {
        # Deep copies per arm: the shared fields are value-identical
        # but never one mutable object, so a late mutation of one
        # arm's fixture cannot silently reach the other two.
        **{field: copylib.deepcopy(entry) for field, entry in shared.items()},
        'speech': {'kind': 'inline', 'value': speech_by_unit[unit]},
        'active_player': fixtures['active_player'],
    }
    for rep in (1, 2, 3):
      py_seed, np_seed = fp.derive_seeds(C2R_UNIT, rep)
      seeds[f'{unit}|rep{rep}'] = {
          'py_seed': py_seed,
          'np_seed': np_seed,
      }
    sequence.append([unit, condition])
  before = manifest.code_states['before']
  budget = dict(manifest.budget)
  budget['concordia_attempts'] = {
      **dict(manifest.budget['concordia_attempts']),
      'group_caps': {'C': manifest.budget['concordia_attempts']['global_cap']},
  }
  return fp.Manifest(
      schema=fp.MANIFEST_SCHEMA,
      seeds=seeds,
      budget=budget,
      order={
          'rotation': (
              'by repetition round r=1..3, fixed within-round sequence'
              ' BASE -> DISC -> WAIT'
          ),
          'rounds': [1, 2, 3],
          'round_sequence': sequence,
      },
      labels={
          'stage_states': list(fp._STAGE_STATES),  # pylint: disable=protected-access
          'started_substates': ['FINISHED', 'FAILED', 'PENDING'],
          'b_table_columns': ['占位'],
          'b_rubric_columns': ['占位'],
          'c_table_columns': list(_C2R_TABLE_COLUMNS),
      },
      units=units,
      fixtures=view_fixtures,
      batch_sources=dict(manifest.batch_sources),
      model_params=dict(manifest.model_params),
      code_states={
          'before': {
              'sources': dict(before['sources']),
              'files': dict(before['files']),
          },
          'after': manifest.code_states['after'],
      },
      batch_root=manifest.batch_root,
      protocol_sha256=manifest.design_sha256,
      fixture_manifest_doc_sha256='',
      seed_derivation={
          'formula': (
              "sha256('fixed-probe-v1|C-2R|rep<rep>') — every arm of one"
              ' rep shares the pair'
          ),
          'key': 'unit-rep',
          'pairing': (
              'shared baseline: all three arms of one rep run one'
              ' paired seed tuple'
          ),
      },
      raw={},
  )


# ---------------------------------------------------------------------------
# Three-arm controlled-difference audit (the C-2R analogue of the
# harness's per-unit before/after pair audit)
# ---------------------------------------------------------------------------

# The controlled zone marks of the after-state judgment prompt (the
# same section pair the harness's C audit strips).
_ZONE_START = '## Current player contribution (not yet committed, data)'
_ZONE_END = '## Current candidate (not yet committed)'

# The single allowed in-zone difference: the original_speech value on
# the raw JSON line. Audits mask it with this fixed placeholder before
# the byte comparison, so nothing else in the zone may differ.
_SPEECH_MASK = '<original_speech>'

# Locates the original_speech key inside the zone's raw JSON line. A
# key encoded with JSON escapes does not match; for a legally
# generated line that cannot occur, and for anything else the masking
# failure itself is the finding (fail closed).
_SPEECH_KEY_RE = re.compile(r'"original_speech"\s*:\s*')


def _zone(prompt: str) -> str:
  """Extracts the contribution-zone text of one after-state prompt."""
  start = prompt.find(_ZONE_START)
  if start < 0:
    raise ValueError('prompt carries no contribution zone')
  end = prompt.find(_ZONE_END, start)
  if end < 0:
    raise ValueError('prompt carries no candidate mark after the zone')
  return prompt[start:end]


def _zone_speech_findings(
    disclosure_prompt: str, waiting_prompt: str
) -> list[str]:
  """Audits that two after prompts differ only in the speech JSON value.

  Stripping the zone from both must leave identical prompts (the
  difference is confined to the zone). Within the zone, the parsed
  JSON line must stay the unique ``original_speech`` line with the
  same keys, equal non-speech values and a differing speech value —
  and, after masking only the ``original_speech`` value on the raw
  JSON line, the complete zones (heading line, masked JSON line,
  explanation paragraph, any other line) must be byte-identical: any
  other in-zone difference is a finding.
  """
  findings: list[str] = []
  try:
    disclosure_zone = _zone(disclosure_prompt)
    waiting_zone = _zone(waiting_prompt)
    disclosure_line, disclosure_json = _zone_json_line(disclosure_zone)
    waiting_line, waiting_json = _zone_json_line(waiting_zone)
  except ValueError as error:
    return [f'after prompt zone extraction failed: {error}']
  stripped_disclosure = disclosure_prompt.replace(disclosure_zone, '', 1)
  stripped_waiting = waiting_prompt.replace(waiting_zone, '', 1)
  if stripped_disclosure != stripped_waiting:
    findings.append('the two after prompts differ beyond the contribution zone')
  try:
    masked_disclosure = _masked_zone(disclosure_zone, disclosure_line)
    masked_waiting = _masked_zone(waiting_zone, waiting_line)
  except ValueError as error:
    findings.append(f'after prompt zone masking failed: {error}')
  else:
    if masked_disclosure != masked_waiting:
      findings.append(
          'the two after arms differ inside the contribution zone beyond'
          ' the original_speech value'
      )
  if set(disclosure_json) != set(waiting_json):
    findings.append('the contribution JSON keys differ between arms')
    return findings
  for key in disclosure_json:
    if key == 'original_speech':
      continue
    if disclosure_json[key] != waiting_json[key]:
      findings.append(
          f'contribution JSON field {key!r} differs between the after arms'
      )
  if disclosure_json.get('original_speech') == waiting_json.get(
      'original_speech'
  ):
    findings.append(
        'the two after arms carry the same original_speech (the view '
        'wired one speech into both arms)'
    )
  return findings


def _masked_zone(zone: str, json_line: str) -> str:
  """Masks only the original_speech value inside one zone's raw line.

  The replacement happens on the raw JSON line text — the value's
  exact byte span is located by decoding from the key position with
  ``json.JSONDecoder.raw_decode`` and swapped for the fixed
  placeholder — so every other byte of the zone (heading line, key
  order, whitespace, escaping, explanation text, injected lines)
  survives and stays comparable. Re-serializing the parsed object
  instead would normalize exactly those real drifts away.

  Args:
    zone: the contribution-zone text of one after-state prompt.
    json_line: the zone's raw ``original_speech`` JSON line.

  Returns:
    The zone with the ``original_speech`` value replaced by the mask.

  Raises:
    ValueError: when the raw line exposes no decodable
      ``original_speech`` value to mask.
  """
  key_match = _SPEECH_KEY_RE.search(json_line)
  if key_match is None:
    raise ValueError('the JSON line exposes no original_speech key')
  value_start = key_match.end()
  try:
    _, value_end = json.JSONDecoder().raw_decode(json_line, value_start)
  except json.JSONDecodeError as error:
    raise ValueError('the original_speech value does not decode') from error
  masked_line = json_line[:value_start] + _SPEECH_MASK + json_line[value_end:]
  return zone.replace(json_line, masked_line, 1)


def _zone_json_line(zone: str) -> tuple[str, dict[str, Any]]:
  """Parses the zone's single JSON object line, keeping the raw line.

  Args:
    zone: the contribution-zone text of one after-state prompt.

  Returns:
    The raw line text and the parsed JSON object, which must carry
    ``original_speech``.

  Raises:
    ValueError: unless exactly one line parses as such an object.
  """
  parsed = []
  for line in zone.splitlines():
    text = line.strip()
    if not (text.startswith('{') and text.endswith('}')):
      continue
    try:
      payload = json.loads(text)
    except json.JSONDecodeError:
      continue
    if isinstance(payload, dict) and 'original_speech' in payload:
      parsed.append((line, payload))
  if len(parsed) != 1:
    raise ValueError('the zone carries no unique original_speech JSON line')
  return parsed[0]


def audit_controlled_differences(
    before_prompt: str,
    disclosure_prompt: str,
    waiting_prompt: str,
) -> list[str]:
  """Audits one repetition's three judgment prompts (batch-stop level).

  The preregistered controlled differences are exhaustive: (1) the
  before-state prompt carries no ``## Current player contribution``
  zone; (2) the two after prompts differ only in their zone's
  ``original_speech`` JSON value; (3) each after prompt equals the
  before prompt plus the zone insertion (the harness's own C-section
  strip audit, reused); and thereby (4) history, candidate,
  adjudicated action result, judgment rules and boundaries are
  byte-identical across the three arms.

  Args:
    before_prompt: the BASE arm's first judgment prompt.
    disclosure_prompt: the DISC arm's first judgment prompt.
    waiting_prompt: the WAIT arm's first judgment prompt.

  Returns:
    Violations; an empty list means the three prompts carry only the
    controlled differences.
  """
  findings: list[str] = []
  for label, after_prompt in (
      ('disclosure', disclosure_prompt),
      ('waiting', waiting_prompt),
  ):
    findings.extend(
        f'{label} arm: {finding}'
        for finding in fp.expected_diff_audit('C', before_prompt, after_prompt)
    )
  findings.extend(_zone_speech_findings(disclosure_prompt, waiting_prompt))
  return findings


def assert_no_builtin_pairs(audit_records: Sequence[Mapping[str, Any]]) -> None:
  """Asserts run_batch's built-in before/after audit never fired.

  The view's units are single-condition, so the engine's own pair
  audit (which fires when one unit's before and after prompts are
  both collected) must observe zero pairs. A non-empty record means
  the view was misconfigured (a unit gained two conditions) and is a
  batch-level integrity failure.

  Args:
    audit_records: the run report's ``audit_records``.

  Raises:
    fp.BatchFault: when any built-in pair audit was recorded.
  """
  if audit_records:
    raise fp.BatchFault(
        'run_batch built-in before/after pair audit fired on the C-2R '
        f'view (single-condition units misconfigured): {list(audit_records)}'
    )
