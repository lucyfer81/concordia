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

"""Offline deterministic tests for the fixed-probe real-execution runner.

The runner wires the accepted offline harness for real execution:
settings loading with the frozen model identity, the offline freeze
stage (deliverable hashes, execution-dependency closure, plan
snapshot), the verify stage and the per-attempt dependency integrity
monitor. These tests never contact a provider, never read the
repository ``.env`` (every settings test points at a temporary file)
and never create a real batch directory — every freeze target and run
lives in a temporary directory.
Ignored documents and historical reports are temporary stand-ins; drift
tests never modify checkout files.
"""

import contextlib
import hashlib
import json
import pathlib
import re
import tempfile
import threading
import types
import unittest
from unittest import mock

from concordia.contrib.language_models.openai import gpt_model
from concordia.language_model import language_model
import httpx
import openai

from examples.story_enactment import fixed_probe as fp
from examples.story_enactment import fixed_probe_runner as runner
from examples.story_enactment import request_ledger as rl


@contextlib.contextmanager
def _isolated_repository():
  """Supplies a disposable freeze tree without reading checkout history.

  The manifest and Python deliverables are copied from the checkout;
  ignored documents and reports are deterministic stand-ins. Dependency
  discovery still runs the real offline batch and the real module-file
  enumerator, with module paths pointing at temporary dependency copies.
  Drift tests therefore change only disposable files, and neither CI nor
  parallel workers depend on the checkout's docs or run_results folders.
  """
  checkout = pathlib.Path(__file__).resolve().parents[2]
  module_hashes = runner._repo_module_hashes  # pylint: disable=protected-access
  paths = (
      set(runner._FROZEN_SOURCES)  # pylint: disable=protected-access
      | set(_REQUIRED_CLOSURE_FILES)
      | {_CLOSURE_TARGET, 'examples/story_enactment/request_ledger.py'}
  )
  with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    for rel in paths:
      target = root / rel
      target.parent.mkdir(parents=True, exist_ok=True)
      if pathlib.PurePosixPath(rel).parts[0] in ('docs', 'run_results'):
        target.write_text(
            f'Offline document stand-in: {rel}\n', encoding='utf-8'
        )
      else:
        target.write_bytes((checkout / rel).read_bytes())
    plan = root / _PLAN_DOC_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text('Offline execution plan stand-in.\n', encoding='utf-8')
    modules = {
        rel: types.SimpleNamespace(__file__=str(root / rel))
        for rel in paths
        if rel.endswith('.py')
    }
    # Exercise the enumerator's ignored-history, external and missing-file
    # filters without importing modules from private historical artifacts.
    modules['history'] = types.SimpleNamespace(
        __file__=str(root / 'run_results' / 'ignored.py')
    )
    modules['external'] = types.SimpleNamespace(
        __file__=str(root.parent / 'outside.py')
    )
    modules['builtin'] = types.SimpleNamespace()

    def dependency_hashes(manifest, batch_dir):
      with mock.patch.object(
          runner, 'sys', types.SimpleNamespace(modules=modules)
      ):
        return module_hashes(manifest, batch_dir)

    with (
        mock.patch.object(runner, '_REPO_ROOT', root),
        mock.patch.object(
            runner,
            '_MANIFEST_PATH',
            root / 'examples/story_enactment/fixed_probe_manifest.json',
        ),
        mock.patch.object(runner, '_PLAN_DOC_PATH', plan),
        mock.patch.object(
            runner, '_repo_module_hashes', side_effect=dependency_hashes
        ),
    ):
      yield root


# The two finalized-history notes of the v4 deliverable list: pinned
# historical deliverables, unlike this round's updatable progress docs.
_V4_NOTES = (
    (
        'docs/superpowers/notes/'
        '2026-10-24-story-enactment-fidelity-and-repetition-context.md'
    ),
    (
        'docs/superpowers/notes/'
        '2026-10-25-story-enactment-fixed-probe-review-fixes.md'
    ),
)

# Updatable progress documents: excluded from the freeze hash set (the
# plan document's frozen form is the in-batch ``plan_snapshot.md``).
_PLAN_DOC_REL = (
    'docs/superpowers/plans/'
    '2026-10-25-story-enactment-fixed-probe-real-execution.md'
)
_FREEZE_NOTE_DOC_REL = (
    'docs/superpowers/plans/'
    '2026-10-25-story-enactment-fixed-probe-execution-freeze.md'
)

# Repository files the closure must demonstrably cover: the document
# tool, the real-run adapter and retry wrapper, the drama director and
# the core narrative-push helper's module.
_REQUIRED_CLOSURE_FILES = (
    'concordia/document/interactive_document.py',
    'concordia/contrib/language_models/openai/gpt_model.py',
    'concordia/language_model/retry_wrapper.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/components/game_master/event_resolution.py',
)


class SettingsTest(unittest.TestCase):
  """Frozen model identity and dotenv parsing of ``_load_settings``."""

  def test_settings_reject_model_mismatch_and_missing_key(self):
    with tempfile.TemporaryDirectory() as tmp:
      env = pathlib.Path(tmp) / 'env'
      env.write_text(
          'AI_MODEL_4=other-model\nAI_API_KEY=sk-test\n',
          encoding='utf-8',
      )
      with self.assertRaises(fp.BatchFault):
        runner._load_settings(env)  # pylint: disable=protected-access
      env.write_text(
          f'AI_MODEL_4={runner._MODEL_NAME}\n',  # pylint: disable=protected-access
          encoding='utf-8',
      )
      with self.assertRaises(fp.BatchFault):
        runner._load_settings(env)  # pylint: disable=protected-access
      env.write_text(
          'AI_MODEL_4=gpt-6.1-sol\n'
          'AI_API_KEY=sk-test\n'
          'AI_BASE_URL=https://example.invalid/api\n',
          encoding='utf-8',
      )
      self.assertEqual(
          runner._load_settings(env),  # pylint: disable=protected-access
          ('sk-test', 'https://example.invalid/api'),
      )

  def test_settings_parses_quotes_and_comments(self):
    with tempfile.TemporaryDirectory() as tmp:
      env = pathlib.Path(tmp) / 'env'
      env.write_text(
          '# live credentials\n'
          '\n'
          'AI_MODEL_4 = "gpt-6.1-sol"\n'
          "AI_API_KEY='sk-quoted'\n"
          'a line without an equals sign\n'
          'AI_BASE_URL=https://example.invalid\n',
          encoding='utf-8',
      )
      self.assertEqual(
          runner._load_settings(env),  # pylint: disable=protected-access
          ('sk-quoted', 'https://example.invalid'),
      )


class FreezeStageTest(unittest.TestCase):
  """Deliverables, plan snapshot and dependency closure of freezing."""

  def _freeze(self, tmp: str) -> tuple[pathlib.Path, dict]:
    batch = pathlib.Path(tmp) / 'batch'
    freeze = runner.freeze_stage(batch)
    return batch, freeze

  def test_freeze_refuses_existing_dir(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      target = pathlib.Path(tmp) / 'batch'
      target.mkdir()
      (target / 'keep.txt').write_text('unchanged', encoding='utf-8')
      with self.assertRaises(fp.BatchFault):
        runner.freeze_stage(target)
      self.assertEqual(
          (target / 'keep.txt').read_text(encoding='utf-8'), 'unchanged'
      )
      self.assertEqual(
          sorted(path.name for path in target.iterdir()), ['keep.txt']
      )

  def test_freeze_and_fake_run_need_no_checkout_history(self):
    checkout = pathlib.Path(__file__).resolve().parents[2]
    is_file = pathlib.Path.is_file
    path_open = pathlib.Path.open

    def is_checkout_history(path):
      return any(
          path.resolve().is_relative_to(checkout / name)
          for name in ('docs', 'run_results')
      )

    def missing_history(path):
      return False if is_checkout_history(path) else is_file(path)

    def refuse_history(path, *args, **kwargs):
      if is_checkout_history(path):
        raise AssertionError(f'test accessed checkout history: {path}')
      return path_open(path, *args, **kwargs)

    with (
        _isolated_repository(),
        tempfile.TemporaryDirectory() as tmp,
        mock.patch.object(pathlib.Path, 'is_file', missing_history),
        mock.patch.object(pathlib.Path, 'open', refuse_history),
    ):
      batch, env = _prepare_run(tmp)
      client = _ScriptedCompletions()
      summary = runner.run_stage(
          batch,
          env_path=env,
          model_factory=lambda *args, **kwargs: _FakeClientModel(
              _fake_client(client)
          ),
          reextract_fixtures=False,
      )
      self.assertIsNone(summary['fault'])
      self.assertEqual(summary['ledger_counts']['global'], 39)
      self.assertEqual(len(_ledger_entries(batch)), 33)

  def test_freeze_pins_deliverables_plan_snapshot_and_closure(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, freeze = self._freeze(tmp)
      self.assertEqual(freeze['schema'], 'fixed-probe-freeze-v1')
      self.assertTrue(freeze['created_utc'])
      # (a)/(b): byte-identical copies of the manifest and the plan.
      self.assertEqual(
          (batch / 'manifest.json').read_bytes(),
          runner._MANIFEST_PATH.read_bytes(),  # pylint: disable=protected-access
      )
      self.assertEqual(
          (batch / 'plan_snapshot.md').read_bytes(),
          runner._PLAN_DOC_PATH.read_bytes(),  # pylint: disable=protected-access
      )
      # The frozen source set is exactly the single 16-item list.
      self.assertEqual(len(runner._FROZEN_SOURCES), 16)  # pylint: disable=protected-access
      self.assertEqual(
          set(freeze['source_hashes']),
          set(runner._FROZEN_SOURCES),  # pylint: disable=protected-access
      )
      for rel, pinned in freeze['source_hashes'].items():
        self.assertEqual(
            pinned,
            hashlib.sha256(
                (runner._REPO_ROOT / rel).read_bytes()  # pylint: disable=protected-access
            ).hexdigest(),
            rel,
        )
      for note in _V4_NOTES:
        self.assertIn(note, freeze['source_hashes'])
      self.assertNotIn(_PLAN_DOC_REL, freeze['source_hashes'])
      # (d): the dependency closure is present, self-bound and covers
      # the real execution dependencies.
      closure = json.loads(
          (batch / 'dependency_hashes.json').read_text(encoding='utf-8')
      )
      self.assertTrue(closure)
      for required in _REQUIRED_CLOSURE_FILES:
        self.assertIn(required, closure, required)
        self.assertEqual(
            closure[required],
            hashlib.sha256(
                (runner._REPO_ROOT / required).read_bytes()  # pylint: disable=protected-access
            ).hexdigest(),
            required,
        )
      self.assertEqual(
          freeze['dependency_hashes_sha256'],
          hashlib.sha256(
              (batch / 'dependency_hashes.json').read_bytes()
          ).hexdigest(),
      )
      self.assertEqual(
          freeze['manifest_sha256'],
          hashlib.sha256((batch / 'manifest.json').read_bytes()).hexdigest(),
      )
      self.assertEqual(
          freeze['plan_snapshot_sha256'],
          hashlib.sha256((batch / 'plan_snapshot.md').read_bytes()).hexdigest(),
      )
      # (c): the frozen budget echo and model identity.
      self.assertEqual(
          freeze['budget'],
          {
              'attempts_global_cap': 75,
              'attempts_group_caps': {'B': 21, 'C': 54},
              'sdk_create_cap': 225,
              'http_upper_bound_disclosure': 675,
          },
      )
      self.assertEqual(
          freeze['model'],
          {'tag': 'm4', 'name': 'gpt-6.1-sol', 'env_key': 'AI_MODEL_4'},
      )
      manifest = fp.load_manifest(batch / 'manifest.json')
      self.assertEqual(freeze['protocol_sha256'], manifest.protocol_sha256)
      self.assertEqual(
          freeze['fixture_manifest_doc_sha256'],
          manifest.fixture_manifest_doc_sha256,
      )
      # No credential field anywhere in the freeze record.
      freeze_text = (batch / 'freeze.json').read_text(encoding='utf-8')

      def _keys(value):
        if isinstance(value, dict):
          for key, item in value.items():
            yield key
            yield from _keys(item)
        elif isinstance(value, list):
          for item in value:
            yield from _keys(item)

      for key in _keys(freeze):
        self.assertNotIn('api_key', key.lower())
        self.assertNotIn('secret', key.lower())
      self.assertNotIn('AI_API_KEY', freeze_text)

  def test_freeze_closure_excludes_batch_and_progress_docs(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, freeze = self._freeze(tmp)
      closure = json.loads(
          (batch / 'dependency_hashes.json').read_text(encoding='utf-8')
      )
      batch_prefix = str(batch.resolve())
      for rel in closure:
        self.assertFalse(pathlib.Path(rel).is_absolute(), rel)
        self.assertNotIn(batch_prefix, rel, rel)
        self.assertNotIn('run_results', pathlib.PurePosixPath(rel).parts, rel)
      manifest = fp.load_manifest(batch / 'manifest.json')
      for rel in manifest.code_states['before']['files']:
        self.assertNotIn(rel, closure)
      for rel in freeze['source_hashes']:
        self.assertFalse(pathlib.Path(rel).is_absolute(), rel)
        self.assertNotIn(batch_prefix, rel, rel)
      self.assertNotIn(_PLAN_DOC_REL, freeze['source_hashes'])
      self.assertNotIn(_FREEZE_NOTE_DOC_REL, freeze['source_hashes'])
      for note in _V4_NOTES:
        self.assertIn(note, freeze['source_hashes'])


# A temporary dependency copy outside the deliverable set proves that
# the closure group is watched independently (README covers deliverables).
_CLOSURE_TARGET = 'concordia/document/tool.py'
_SOURCE_TARGET = 'examples/story_enactment/README.md'


class _ProbeStub(language_model.LanguageModel):
  """Fingerprint-routed offline stub for the mid-run integrity tests."""

  def sample_text(self, prompt: str, **kwargs) -> str:
    del kwargs
    if 'Output a JSON object with exactly one string field' in prompt:
      return json.dumps(
          {'stage_directions': '周律师逐页查阅了原件全文的落款与签署。'},
          ensure_ascii=False,
      )
    if '# Story resolution semantic review' in prompt:
      return json.dumps({'valid': True, 'reason': '合法。'}, ensure_ascii=False)
    if 'Suggest five plausible events' in prompt:
      return '线索一\n线索二\n线索三\n线索四\n线索五'
    assert 'How does 2 modify' in prompt, prompt[:200]
    return '融合后的复合事件。'

  def sample_choice(self, prompt, responses, *, seed=None):
    del seed
    match = re.search(r'^  \((\w+)\) No\s*$', prompt, re.MULTILINE)
    assert match is not None, 'no No option presented'
    letter = match.group(1)
    return list(responses).index(letter), letter, ''


class VerifyStageTest(unittest.TestCase):
  """Pre-run freeze verification and the three-group integrity monitor."""

  def _freeze(self, tmp: str) -> tuple[pathlib.Path, dict]:
    batch = pathlib.Path(tmp) / 'batch'
    freeze = runner.freeze_stage(batch)
    return batch, freeze

  def test_verify_detects_drift_after_freeze(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, _ = self._freeze(tmp)
      # ① a pinned deliverable file drifted from source_hashes.
      source_path = runner._REPO_ROOT / _SOURCE_TARGET  # pylint: disable=protected-access
      original = source_path.read_bytes()
      try:
        source_path.write_bytes(original + b'\n<!-- drift -->\n')
        with self.assertRaises(fp.BatchFault):
          runner.verify_stage(batch, reextract_fixtures=False)
      finally:
        source_path.write_bytes(original)
      # ② a closure dependency file drifted.
      closure_path = runner._REPO_ROOT / _CLOSURE_TARGET  # pylint: disable=protected-access
      original = closure_path.read_bytes()
      try:
        closure_path.write_bytes(original + b'\n# drift\n')
        with self.assertRaises(fp.BatchFault):
          runner.verify_stage(batch, reextract_fixtures=False)
      finally:
        closure_path.write_bytes(original)
      # ③ the in-batch manifest copy drifted.
      manifest_copy = batch / 'manifest.json'
      original = manifest_copy.read_bytes()
      manifest_copy.write_bytes(original + b'\n')
      with self.assertRaises(fp.BatchFault):
        runner.verify_stage(batch, reextract_fixtures=False)
      manifest_copy.write_bytes(original)
      # ④ the dependency hash manifest itself drifted.
      deps_copy = batch / 'dependency_hashes.json'
      original = deps_copy.read_text(encoding='utf-8')
      payload = json.loads(original)
      payload['drift/added.py'] = '0' * 64
      deps_copy.write_text(
          json.dumps(payload, ensure_ascii=False, indent=2) + '\n',
          encoding='utf-8',
      )
      with self.assertRaises(fp.BatchFault):
        runner.verify_stage(batch, reextract_fixtures=False)
      deps_copy.write_text(original, encoding='utf-8')
      # After every restore the clean freeze verifies again.
      summary = runner.verify_stage(batch, reextract_fixtures=False)
      self.assertEqual(summary['fixture_reextraction'], 'skipped')

  def test_verify_pass_and_monitor_semantics(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, freeze = self._freeze(tmp)
      closure = json.loads(
          (batch / 'dependency_hashes.json').read_text(encoding='utf-8')
      )
      summary = runner.verify_stage(batch, reextract_fixtures=False)
      self.assertEqual(summary['fixture_reextraction'], 'skipped')
      self.assertEqual(summary['source_files'], 16)
      self.assertEqual(summary['dependency_files'], len(closure))
      monitor = runner.DependencyIntegrityMonitor(freeze, batch)
      monitor.check()
      # Closure drift trips check().
      closure_path = runner._REPO_ROOT / _CLOSURE_TARGET  # pylint: disable=protected-access
      original = closure_path.read_bytes()
      try:
        closure_path.write_bytes(original + b'\n# drift\n')
        with self.assertRaises(fp.BatchFault):
          monitor.check()
      finally:
        closure_path.write_bytes(original)
      # Deliverable drift trips check().
      source_path = runner._REPO_ROOT / _SOURCE_TARGET  # pylint: disable=protected-access
      original = source_path.read_bytes()
      try:
        source_path.write_bytes(original + b'\n<!-- drift -->\n')
        with self.assertRaises(fp.BatchFault):
          monitor.check()
      finally:
        source_path.write_bytes(original)
      # Frozen-input (in-batch manifest copy) drift trips check().
      manifest_copy = batch / 'manifest.json'
      original = manifest_copy.read_bytes()
      manifest_copy.write_bytes(original + b'\n')
      with self.assertRaises(fp.BatchFault):
        monitor.check()
      manifest_copy.write_bytes(original)
      monitor.check()

  def test_midrun_frozen_input_tamper_intercepted_before_next_forwarding(self):
    manifest = fp.load_manifest(runner._MANIFEST_PATH)  # pylint: disable=protected-access
    for kind in ('closure', 'source', 'frozen_input'):
      with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        batch, freeze = self._freeze(tmp)
        monitor = runner.DependencyIntegrityMonitor(freeze, batch)
        target = {
            'closure': runner._REPO_ROOT / _CLOSURE_TARGET,  # pylint: disable=protected-access
            'source': runner._REPO_ROOT / _SOURCE_TARGET,  # pylint: disable=protected-access
            'frozen_input': batch / 'manifest.json',
        }[kind]
        marker = {
            'closure': 'tool.py',
            'source': 'README.md',
            'frozen_input': 'manifest.json',
        }[kind]
        calls = {'count': 0}

        def integrity_check() -> None:
          calls['count'] += 1
          if calls['count'] == 3:
            target.write_bytes(target.read_bytes() + b'\n# tamper\n')
          monitor.check()

        original = target.read_bytes()
        try:
          with self.assertRaises(fp.BatchFault):
            fp.run_batch(
                manifest,
                _ProbeStub(),
                root / 'run',
                integrity_check=integrity_check,
            )
        finally:
          target.write_bytes(original)
        stage_payload = json.loads(
            (root / 'run' / 'stage_states.json').read_text(encoding='utf-8')
        )
        self.assertEqual(stage_payload['fault']['kind'], 'batch_fault')
        self.assertIn(marker, stage_payload['fault']['error'])
        # Exactly the attempts completed before the tamper are booked,
        # each with a terminal observation; nothing else started.
        events = rl.load_events(root / 'run' / 'requests.jsonl')
        started = [e for e in events if e['event'] == 'started']
        finished = {e['slot_id'] for e in events if e['event'] == 'finished'}
        self.assertEqual(len(started), 1)
        self.assertTrue(all(e['slot_id'] in finished for e in started))
        self.assertEqual(
            stage_payload['stage_states']['B-1|before|r1|generation'],
            'FINISHED',
        )


# ---------------------------------------------------------------------------
# Run stage (Task 8): one-time guard, composition and fault handling on
# the fake-client path (zero network, zero real credentials).
# ---------------------------------------------------------------------------

# The same production stage marks the harness's own stubs route on;
# the choice suffix mark rides only on judgment prompts.
_GENERATION_MARK = 'Output a JSON object with exactly one string field'
_REVIEW_MARK = (
    'Evaluate all rules above. Output only a JSON object with exactly'
)
_SUGGESTION_MARK = 'Suggest five plausible events'
_FUSION_MARK = 'How does 2 modify'
_CHOICE_SUFFIX_MARK = 'Respond EXACTLY with one of the following strings'


def _fingerprint(prompt: str) -> str:
  """Routes one client prompt to its production stage by the stub marks."""
  for mark, stage in (
      (_CHOICE_SUFFIX_MARK, 'judgment'),
      (_SUGGESTION_MARK, 'suggestion'),
      (_FUSION_MARK, 'fusion'),
      (_REVIEW_MARK, 'review'),
      (_GENERATION_MARK, 'generation'),
  ):
    if mark in prompt:
      return stage
  raise AssertionError(f'unrouted client prompt:\n{prompt[:400]}')


class _ScriptedCompletions:
  """Scripted ``chat.completions.create`` routing on the stage marks.

  Shared by the fake-client run-stage tests and the real-adapter tests:
  every create is fingerprinted by the same marks the harness stubs
  use, so scripted behaviors — a transient provider error, an invalid
  first judgment answer, an empty visible-text response — land on real
  execution positions with zero network. Judgment answers parse the
  option block of the very prompt they answer, so the returned letter
  follows the actual (seeded) option permutation.
  """

  def __init__(
      self,
      *,
      invalid_first_judgment: bool = False,
      judgment_yes=(),
      raise_at=None,
      empty_at=(),
      model_name: str = 'gpt-6.1-sol',
  ):
    self.calls: list[dict] = []
    self.model_name = model_name
    self._invalid_first_judgment = invalid_first_judgment
    self._judgment_yes = frozenset(judgment_yes)
    self._raise_at = dict(raise_at or {})
    self._empty_at = frozenset(empty_at)
    self._judgments_done = 0
    self._creates_this_judgment = 0

  @property
  def creates(self) -> int:
    return len(self.calls)

  def create(self, **kwargs):
    index = len(self.calls) + 1
    prompt = kwargs['messages'][-1]['content']
    stage = _fingerprint(prompt)
    self.calls.append({'index': index, 'stage': stage, 'kwargs': kwargs})
    if index in self._raise_at:
      raise self._raise_at[index]
    return types.SimpleNamespace(
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content=self._content(stage, prompt, index)
                )
            )
        ],
        model=self.model_name,
        id=f'fake-resp-{index}',
    )

  def _content(self, stage: str, prompt: str, index: int):
    if index in self._empty_at:
      return None
    if stage == 'judgment':
      return self._judgment(prompt)
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

  def _judgment(self, prompt: str) -> str:
    if (
        self._invalid_first_judgment
        and self._judgments_done == 0
        and self._creates_this_judgment == 0
    ):
      # An off-vocabulary first answer: the adapter re-asks inside the
      # same attempt, so this costs one more create, never an attempt.
      self._creates_this_judgment += 1
      return '是'
    self._creates_this_judgment = 0
    target = 'Yes' if self._judgments_done in self._judgment_yes else 'No'
    self._judgments_done += 1
    match = re.search(rf'^  \((\w+)\) {target}\s*$', prompt, re.MULTILINE)
    assert match is not None, 'judgment prompt carries no option block'
    return match.group(1)


def _fake_client(completions):
  """Builds the mutable client namespace the probe guard wraps."""
  client = types.SimpleNamespace()
  chat = types.SimpleNamespace()
  chat.completions = completions
  client.chat = chat
  return client


class _FakeClientModel(language_model.LanguageModel):
  """Adapter-shaped stand-in driving the scripted in-process client.

  Mirrors the production adapter's client boundary — one create per
  text call, the choice re-ask loop inside one call — so the run
  exercises ``ProbeModel``'s observing-completions wiring exactly as a
  real run would, with zero network.
  """

  def __init__(self, client):
    self._client = client

  def sample_text(
      self,
      prompt,
      *,
      max_tokens=language_model.DEFAULT_MAX_TOKENS,
      terminators=language_model.DEFAULT_TERMINATORS,
      temperature=language_model.DEFAULT_TEMPERATURE,
      top_p=language_model.DEFAULT_TOP_P,
      top_k=language_model.DEFAULT_TOP_K,
      timeout=language_model.DEFAULT_TIMEOUT_SECONDS,
      seed=None,
  ):
    del terminators, top_p, top_k
    response = self._client.chat.completions.create(
        model='gpt-6.1-sol',
        messages=[{'role': 'user', 'content': prompt}],
        temperature=temperature,
        max_completion_tokens=max_tokens,
        timeout=timeout,
        seed=seed,
        reasoning_effort='low',
        verbosity='low',
    )
    return response.choices[0].message.content

  def sample_choice(self, prompt, responses, *, seed=None):
    extended = (
        prompt
        + '\nRespond EXACTLY with one of the following strings:\n'
        + '\n'.join(responses)
        + '.'
    )
    for _ in range(20):
      answer = self.sample_text(extended, temperature=1.0, seed=seed)
      try:
        index = list(responses).index(answer)
      except ValueError:
        continue
      return index, responses[index], {}
    raise language_model.InvalidResponseError(
        f'no valid choice among {list(responses)}'
    )


def _ledger_entries(batch: pathlib.Path) -> list[dict]:
  return [
      json.loads(line)
      for line in (
          (batch / 'execution_ledger.jsonl')
          .read_text(encoding='utf-8')
          .splitlines()
      )
      if line.strip()
  ]


def _prepare_run(
    tmp: str, *, key: str = 'sk-fixed-probe-fake', model=None
) -> tuple[pathlib.Path, pathlib.Path]:
  """Freezes a stand-in batch and writes a temporary environment file."""
  batch = pathlib.Path(tmp) / 'batch'
  runner.freeze_stage(batch)
  env = pathlib.Path(tmp) / 'env'
  model_name = (
      runner._MODEL_NAME if model is None else model  # pylint: disable=protected-access
  )
  env.write_text(
      f'AI_MODEL_4={model_name}\nAI_API_KEY={key}\n', encoding='utf-8'
  )
  return batch, env


class RunStageTest(unittest.TestCase):
  """Run-stage guard, composition and fault handling on the fake client."""

  def _prepare(
      self, tmp: str, *, key: str = 'sk-fixed-probe-fake', model=None
  ) -> tuple[pathlib.Path, pathlib.Path]:
    return _prepare_run(tmp, key=key, model=model)

  def _factory(self, client, seen=None):
    def factory(model_name, *, api_key, api_base):
      del api_base
      if seen is not None:
        seen.append((model_name, api_key))
      return _FakeClientModel(_fake_client(client))

    return factory

  def _run(self, batch, env, client, **kwargs):
    return runner.run_stage(
        batch,
        env_path=env,
        model_factory=self._factory(client),
        reextract_fixtures=False,
        **kwargs,
    )

  def test_run_stage_full_batch_on_fake_client(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp)
      client = _ScriptedCompletions(invalid_first_judgment=True)
      seen = []
      summary = runner.run_stage(
          batch,
          env_path=env,
          model_factory=self._factory(client, seen),
          reextract_fixtures=False,
      )
      self.assertIsNone(summary['fault'])
      # The factory received the frozen model identity and loaded key.
      self.assertEqual(seen, [('gpt-6.1-sol', 'sk-fixed-probe-fake')])
      # Nominal full run: 39 booked attempts (every judgment No) over
      # the complete 33-position execution ledger.
      self.assertEqual(summary['ledger_counts']['global'], 39)
      self.assertEqual(len(_ledger_entries(batch)), 33)
      # The judgment re-ask costs one extra create, never an attempt.
      self.assertGreaterEqual(
          summary['sdk_counts']['global'], summary['ledger_counts']['global']
      )
      self.assertEqual(summary['sdk_counts']['global'], 40)
      events = rl.load_events(batch / 'requests.jsonl')
      judgments = [
          event
          for event in events
          if event['event'] == 'started' and event['stage'] == 'judgment'
      ]
      self.assertEqual(len(judgments), 18)
      first = judgments[0]
      self.assertEqual(
          (first['group'], first['slot_id']),
          ('C-1-before', 'C-1-before__a000001'),
      )
      sdk_events = rl.load_events(batch / 'sdk_ledger' / 'requests.jsonl')
      reask = [
          event
          for event in sdk_events
          if event['event'] == 'started'
          and event.get('attempt_id') == first['slot_id']
      ]
      self.assertEqual(len(reask), 2)
      # Both layers' started events carry the frozen run identity.
      for layer in (events, sdk_events):
        started = [event for event in layer if event['event'] == 'started']
        self.assertTrue(started)
        for event in started:
          self.assertEqual(event['model_tag'], 'm4')
          self.assertEqual(event['model_name'], 'gpt-6.1-sol')
      # The rubric skeleton: every human column stays pending.
      tables = json.loads(
          (batch / 'evaluation_tables.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(tables['c_table']['rows']), 18)
      for row in tables['c_table']['rows']:
        self.assertEqual(row['人工评价依据'], 'pending')
      self.assertEqual(tables['counts']['judgment_no'], 18)
      self.assertEqual(tables['counts']['human_pending'], 18)
      # run_summary.json reconciles with the on-disk artifacts.
      report = json.loads((batch / 'report.json').read_text(encoding='utf-8'))
      self.assertEqual(summary['ledger_counts'], report['ledger_counts'])
      self.assertEqual(summary['sdk_counts'], report['sdk_counts'])
      self.assertEqual(summary['rubric_counts'], tables['counts'])
      self.assertTrue(summary['completed_utc'])
      disk_summary = json.loads(
          (batch / 'run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(disk_summary, summary)
      self.assertTrue((batch / 'run_started.json').is_file())

  def test_transient_retry_billed_and_budget_stops_exactly(self):
    checks: list[int] = []
    real_monitor = runner.DependencyIntegrityMonitor

    class _CountingMonitor(real_monitor):

      def check(self):
        checks.append(1)
        super().check()

    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp)
      client = _ScriptedCompletions(
          raise_at={
              1: openai.APIConnectionError(
                  message='transient connection failure',
                  # The runtime accepts httpx.Request; only openai's
                  # vendored httpx2 annotation disagrees.
                  request=httpx.Request(  # pyrefly: ignore[bad-argument-type]
                      'POST', 'https://example.invalid'
                  ),
              )
          }
      )
      with mock.patch.object(
          runner, 'DependencyIntegrityMonitor', _CountingMonitor
      ):
        summary = self._run(batch, env, client)
      self.assertEqual(summary['fault']['kind'], 'budget_stop')
      # The transient retry is billed inside B-1-before round 1: the
      # group consumed three attempts there (failed generation, retried
      # generation, review) and seven overall (6 nominal + 1 retry).
      self.assertEqual(summary['ledger_counts']['by_group']['B-1-before'], 7)
      started = [
          event
          for event in rl.load_events(batch / 'requests.jsonl')
          if event['event'] == 'started'
      ]
      self.assertEqual(
          [(event['group'], event['stage']) for event in started[:3]],
          [
              ('B-1-before', 'generation'),
              ('B-1-before', 'generation'),
              ('B-1-before', 'review'),
          ],
      )
      finished = {
          event['slot_id']: event
          for event in rl.load_events(batch / 'requests.jsonl')
          if event['event'] == 'finished'
      }
      self.assertEqual(
          finished[started[0]['slot_id']]['status'], 'APIConnectionError'
      )
      b_used = sum(
          count
          for name, count in summary['ledger_counts']['by_group'].items()
          if name.startswith('B-')
      )
      self.assertEqual(b_used, 21)
      states = json.loads(
          (batch / 'stage_states.json').read_text(encoding='utf-8')
      )['stage_states']
      self.assertEqual(states['B-4|after|r3|review'], 'NOT_STARTED_BUDGET')
      for key, state in states.items():
        if key[:2] in ('C-1', 'C-2', 'C-3') and '|r3|' in key:
          self.assertEqual(state, 'NOT_STARTED_BUDGET', key)
      # 26 executed positions stay booked (11 + 11 + 4); the rubric and
      # the summary still exist on the budget-stop path.
      self.assertEqual(len(_ledger_entries(batch)), 26)
      self.assertTrue((batch / 'evaluation_tables.json').is_file())
      disk_summary = json.loads(
          (batch / 'run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(disk_summary['fault']['kind'], 'budget_stop')
      # The integrity gate kept firing until the stop: exactly one
      # check per attempt and per create across the whole run.
      self.assertGreater(len(checks), 0)
      self.assertEqual(
          len(checks),
          summary['ledger_counts']['global'] + summary['sdk_counts']['global'],
      )

  def test_batch_fault_preserves_artifacts_and_rubric(self):
    secret = 'sk-sentinel-fixed-probe-batch4'
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp, key=secret)
      client = _ScriptedCompletions(
          raise_at={4: RuntimeError(f'非白名单基础设施故障，消息携带 {secret}')}
      )
      with self.assertRaises(fp.BatchFault) as ctx:
        self._run(batch, env, client)
      message = str(ctx.exception)
      self.assertIn('[REDACTED]', message)
      self.assertNotIn(secret, message)
      payload = json.loads(
          (batch / 'stage_states.json').read_text(encoding='utf-8')
      )
      self.assertEqual(payload['fault']['kind'], 'batch_fault')
      # The failure is classified truthfully: the faulting stage failed,
      # its predecessor stayed finished, nothing later started.
      self.assertEqual(
          payload['stage_states']['B-1|after|r1|generation'], 'FINISHED'
      )
      self.assertEqual(payload['stage_states']['B-1|after|r1|review'], 'FAILED')
      self.assertEqual(
          payload['outcomes']['B-1|after|r1|review'], 'review_failed'
      )
      self.assertEqual(
          payload['stage_states']['B-2|after|r1|review'], 'NOT_STARTED_ORDER'
      )
      self.assertEqual(
          payload['stage_states']['C-3|after|r3|fusion'], 'NOT_STARTED_ORDER'
      )
      # The rubric skeleton and the summary exist on the fault path.
      tables = json.loads(
          (batch / 'evaluation_tables.json').read_text(encoding='utf-8')
      )
      self.assertEqual(
          tables['counts']['human_pending'], len(tables['c_table']['rows'])
      )
      summary = json.loads(
          (batch / 'run_summary.json').read_text(encoding='utf-8')
      )
      self.assertEqual(summary['fault']['kind'], 'batch_fault')
      # No artifact anywhere carries the raw sentinel (Task 1 chain).
      for path in sorted(batch.rglob('*')):
        if path.is_file():
          self.assertNotIn(
              secret,
              path.read_text(encoding='utf-8', errors='replace'),
              str(path),
          )

  def test_model_identity_refused_before_any_call(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp, model='other-model')
      client = _ScriptedCompletions()
      with self.assertRaises(fp.BatchFault):
        self._run(batch, env, client)
      self.assertEqual(client.creates, 0)
      self.assertFalse((batch / 'requests.jsonl').exists())
      # The one-time marker is taken, but no ledger event was produced.
      self.assertTrue((batch / 'run_started.json').is_file())

  def test_run_stage_refuses_reentry_and_keeps_state(self):
    # A completed directory is refused with zero writes.
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp)
      self._run(batch, env, _ScriptedCompletions())

      def _tree_hashes() -> dict[str, str]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(batch.rglob('*'))
            if path.is_file()
        }

      before = _tree_hashes()
      with self.assertRaises(fp.BatchFault):
        self._run(batch, env, _ScriptedCompletions())
      self.assertEqual(_tree_hashes(), before)
    # A hand-placed marker (an interrupted run) is refused with zero
    # writes as well.
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp)
      (batch / 'run_started.json').write_text('{"pid": 0}', encoding='utf-8')
      listing = sorted(str(path) for path in batch.rglob('*'))
      with self.assertRaises(fp.BatchFault):
        self._run(batch, env, _ScriptedCompletions())
      self.assertEqual(sorted(str(path) for path in batch.rglob('*')), listing)
      self.assertFalse((batch / 'requests.jsonl').exists())
    # Two concurrent starts: exactly one side loses to the marker.
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = self._prepare(tmp)
      outcomes: list[str] = []

      def _start() -> None:
        try:
          self._run(batch, env, _ScriptedCompletions())
          outcomes.append('ok')
        except fp.BatchFault:
          outcomes.append('fault')

      threads = [threading.Thread(target=_start) for _ in range(2)]
      for thread in threads:
        thread.start()
      for thread in threads:
        thread.join()
      self.assertEqual(sorted(outcomes), ['fault', 'ok'])


class RealAdapterRunStageTest(unittest.TestCase):
  """The production adapter path end-to-end on a fake ``openai.OpenAI``.

  The real :class:`GptLanguageModel` is constructed by ``run_stage``
  while ``openai.OpenAI`` is patched at the factory entry (the client
  is created inside ``__init__``, so patching after construction would
  be too late); the factory returns a recording in-process client, so
  every create stays inside the process — zero network, zero real
  credentials.
  """

  def _run_with_fake_openai(self, batch, env, client):
    def _openai_factory(**kwargs):
      del kwargs
      return _fake_client(client)

    with mock.patch.object(gpt_model.openai, 'OpenAI', _openai_factory):
      return runner.run_stage(
          batch,
          env_path=env,
          model_factory=gpt_model.GptLanguageModel,
          reextract_fixtures=False,
      )

  def test_real_adapter_sends_frozen_params_per_stage(self):
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = _prepare_run(tmp)
      client = _ScriptedCompletions(judgment_yes=(0,))
      constructor_calls = []

      def _openai_factory(**kwargs):
        constructor_calls.append(kwargs)
        return _fake_client(client)

      with mock.patch.object(gpt_model.openai, 'OpenAI', _openai_factory):
        summary = runner.run_stage(
            batch,
            env_path=env,
            model_factory=gpt_model.GptLanguageModel,
            reextract_fixtures=False,
        )
      self.assertIsNone(summary['fault'])
      # The adapter was constructed once, with the loaded credentials.
      self.assertEqual(len(constructor_calls), 1)
      self.assertEqual(constructor_calls[0]['api_key'], 'sk-fixed-probe-fake')
      self.assertEqual(summary['ledger_counts']['global'], 41)
      by_stage: dict[str, list[dict]] = {}
      for call in client.calls:
        by_stage.setdefault(call['stage'], []).append(call['kwargs'])
      self.assertEqual(
          {stage: len(calls) for stage, calls in by_stage.items()},
          {
              'generation': 6,
              'review': 15,
              'judgment': 18,
              'suggestion': 1,
              'fusion': 1,
          },
      )
      forbidden = {'max_tokens', 'top_p', 'top_k', 'terminators'}
      expected_tokens = {
          'generation': 2000,
          'review': 1000,
          'judgment': 5000,
          'suggestion': 1500,
          'fusion': 1500,
      }
      expected_effort = {
          'generation': 'low',
          'review': 'low',
          'judgment': 'medium',
          'suggestion': 'low',
          'fusion': 'low',
      }
      for stage, calls in by_stage.items():
        for kwargs in calls:
          self.assertEqual(kwargs['model'], 'gpt-6.1-sol', stage)
          self.assertEqual(kwargs['temperature'], 1.0, stage)
          self.assertEqual(kwargs['timeout'], 60, stage)
          self.assertEqual(kwargs['verbosity'], 'low', stage)
          self.assertEqual(
              kwargs['max_completion_tokens'], expected_tokens[stage], stage
          )
          self.assertEqual(
              kwargs['reasoning_effort'], expected_effort[stage], stage
          )
          self.assertFalse(forbidden & set(kwargs), stage)
          # The frozen few-shot message structure rides on every call.
          messages = kwargs['messages']
          self.assertEqual(
              [message['role'] for message in messages],
              ['system', 'user', 'assistant', 'user', 'assistant', 'user'],
              stage,
          )
          self.assertIn('You always continue input', messages[0]['content'])
          self.assertIn('Is Jake a turtle?', messages[1]['content'])
          self.assertEqual(messages[2]['content'], 'not a turtle.')
          self.assertIn('What is Priya doing', messages[3]['content'])
          self.assertEqual(messages[4]['content'], 'sleeping.')
          self.assertTrue(messages[5]['content'], stage)

  def test_real_adapter_choice_reask_and_identity_evidence(self):
    # (a) The judgment re-ask through the real adapter: an invalid first
    # answer costs one more create under the same single attempt.
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = _prepare_run(tmp)
      client = _ScriptedCompletions(invalid_first_judgment=True)
      summary = self._run_with_fake_openai(batch, env, client)
      self.assertIsNone(summary['fault'])
      self.assertEqual(summary['ledger_counts']['global'], 39)
      judgment_calls = [
          call for call in client.calls if call['stage'] == 'judgment'
      ]
      self.assertEqual(len(judgment_calls), 19)  # 18 judgments + 1 re-ask
      first_prompt = judgment_calls[0]['kwargs']['messages'][-1]['content']
      second_prompt = judgment_calls[1]['kwargs']['messages'][-1]['content']
      for prompt in (first_prompt, second_prompt):
        self.assertIn(
            'Respond EXACTLY with one of the following strings', prompt
        )
        options = re.findall(r'^  \((\w+)\) (Yes|No)\s*$', prompt, re.MULTILINE)
        self.assertEqual(len(options), 2, prompt[-200:])
      # The re-ask stays inside one attempt: one started judgment slot
      # for C-1-before, two SDK creates booked under it.
      events = rl.load_events(batch / 'requests.jsonl')
      judgments = [
          event
          for event in events
          if event['event'] == 'started' and event['stage'] == 'judgment'
      ]
      self.assertEqual(len(judgments), 18)
      sdk_events = rl.load_events(batch / 'sdk_ledger' / 'requests.jsonl')
      under_first = [
          event
          for event in sdk_events
          if event['event'] == 'started'
          and event.get('attempt_id') == judgments[0]['slot_id']
      ]
      self.assertEqual(len(under_first), 2)
      # SDK-layer response identity: one line per create, each carrying
      # the model name the provider reported on the response.
      identity = [
          json.loads(line)
          for line in (
              (batch / 'sdk_ledger' / 'sdk_identity.jsonl')
              .read_text(encoding='utf-8')
              .splitlines()
          )
          if line.strip()
      ]
      self.assertEqual(len(identity), client.creates)
      for record in identity:
        self.assertEqual(record['model'], client.model_name)
        self.assertTrue(record['response_id'])
    # (b) An empty visible-text response is a terminal
    # InvalidResponseError: one attempt, never retried by the outer
    # whitelist, failed truthfully.
    with _isolated_repository(), tempfile.TemporaryDirectory() as tmp:
      batch, env = _prepare_run(tmp)
      client = _ScriptedCompletions(empty_at=(1,))
      with mock.patch.object(
          gpt_model.openai,
          'OpenAI',
          lambda **kwargs: _fake_client(client),
      ):
        with self.assertRaises(language_model.InvalidResponseError):
          runner.run_stage(
              batch,
              env_path=env,
              model_factory=gpt_model.GptLanguageModel,
              reextract_fixtures=False,
          )
      self.assertEqual(client.creates, 1)
      events = rl.load_events(batch / 'requests.jsonl')
      started = [event for event in events if event['event'] == 'started']
      finished = {
          event['slot_id']: event
          for event in events
          if event['event'] == 'finished'
      }
      self.assertEqual(len(started), 1)
      self.assertEqual(
          finished[started[0]['slot_id']]['status'], 'InvalidResponseError'
      )
      states = json.loads(
          (batch / 'stage_states.json').read_text(encoding='utf-8')
      )['stage_states']
      self.assertEqual(states['B-1|before|r1|generation'], 'FAILED')
      self.assertEqual(states['B-1|before|r1|review'], 'SKIPPED_UPSTREAM')


if __name__ == '__main__':
  unittest.main()
