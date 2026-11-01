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

"""Offline deterministic tests for the fixed-sample probe harness.

The fixed probe implements the 2026-10-24 fixed-sample verification
protocol offline: frozen fixtures, paired seeds, a two-layer ledger and
a zero-network dry run. These tests never contact a provider, never
read credentials and never depend on ``run_results/`` existing — batch
replicas are built in temporary directories from synthetic files.
"""

import copy
import hashlib
import importlib
import json
import pathlib
import random
import re
import shutil
import tempfile
import types
import unittest

from concordia.contrib.components.game_master import story_action_adjudication as _story_action_adjudication
from concordia.contrib.components.game_master import story_resolution as _story_resolution
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.language_model import retry_wrapper

from examples.story_enactment import fixed_model_probe as _fmp
from examples.story_enactment import fixed_probe as fp
from examples.story_enactment import request_ledger as rl

_MANIFEST_PATH = pathlib.Path(__file__).parent / 'fixed_probe_manifest.json'

_UNITS = ('B-1', 'B-2', 'B-3', 'B-4', 'C-1', 'C-2', 'C-3')

# Protocol §2.1: within each repetition round the fixed order is
# B-1 (before→after), B-2, B-3, B-4, C-1..C-3 (before→after each);
# B-2/3/4 run current production only ("after").
_ROUND_SEQUENCE = (
    ('B-1', 'before'),
    ('B-1', 'after'),
    ('B-2', 'after'),
    ('B-3', 'after'),
    ('B-4', 'after'),
    ('C-1', 'before'),
    ('C-1', 'after'),
    ('C-2', 'before'),
    ('C-2', 'after'),
    ('C-3', 'before'),
    ('C-3', 'after'),
)


class ManifestTest(unittest.TestCase):

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_manifest_seeds_paired_by_unit_and_rep(self):
    seed_keys = set(self.manifest.seeds)
    expected_keys = {f'{unit}|rep{rep}' for unit in _UNITS for rep in (1, 2, 3)}
    self.assertEqual(seed_keys, expected_keys)
    for key in seed_keys:
      self.assertNotIn('before', key.split('|'))
      self.assertNotIn('after', key.split('|'))
    for unit in _UNITS:
      for rep in (1, 2, 3):
        entry = self.manifest.seeds[f'{unit}|rep{rep}']
        self.assertEqual(
            fp.derive_seeds(unit, rep),
            (entry['py_seed'], entry['np_seed']),
            f'derivation mismatch for {unit} rep {rep}',
        )

  def test_manifest_budget_and_order_frozen(self):
    budget = self.manifest.budget
    self.assertEqual(budget['concordia_attempts']['global_cap'], 75)
    self.assertEqual(
        budget['concordia_attempts']['group_caps'], {'B': 21, 'C': 54}
    )
    self.assertEqual(budget['sdk_create']['global_cap'], 225)
    order = self.manifest.order
    self.assertEqual(order['rounds'], [1, 2, 3])
    self.assertEqual(
        [tuple(step) for step in order['round_sequence']], list(_ROUND_SEQUENCE)
    )
    # The full execution order rotates the fixed round sequence per rep.
    self.assertEqual(
        [tuple(step) for step in fp.expand_order(self.manifest)],
        [step for _ in (1, 2, 3) for step in _ROUND_SEQUENCE],
    )
    states = self.manifest.labels['stage_states']
    self.assertEqual(
        set(states),
        {
            'STARTED',
            'FINISHED',
            'FAILED',
            'PENDING',
            'NOT_STARTED_ORDER',
            'NOT_STARTED_BUDGET',
            'SKIPPED_UPSTREAM',
            'NOT_APPLICABLE',
        },
    )

  def test_manifest_batch_fields_pin_sources(self):
    # Structure check always runs, without any batch directory present.
    fp.verify_manifest(self.manifest)
    # Every batch-derived fixture pins its source file hash and field path.
    batch_derived = {
        'B-1': ('history', 'speech', 'action_intent', 'action_result'),
        'B-3': ('draft',),
        'C-1': ('history', 'speech', 'action_intent', 'candidate', 'gate'),
    }
    for unit, fields in batch_derived.items():
      for field in fields:
        entry = self.manifest.fixtures[unit][field]
        source = entry['source']
        self.assertIn(source, self.manifest.batch_sources, (unit, field))
        pinned = self.manifest.batch_sources[source]['sha256']
        self.assertRegex(pinned, r'^[0-9a-f]{64}$', (unit, field))
        self.assertIn('kind', entry, (unit, field))
        self.assertTrue(entry['value'] is not None, (unit, field))
    # On a faithful replica of the batch files, verification re-extracts
    # byte-identical values; a tampered file hash raises ManifestError.
    with tempfile.TemporaryDirectory() as tmp:
      batch_dir = pathlib.Path(tmp)
      manifest = _replica_manifest(batch_dir)
      fp.verify_manifest(manifest, batch_dir)
      tampered = [
          json.loads(line)
          for line in (
              (batch_dir / 'replica_turns.jsonl')
              .read_text(encoding='utf-8')
              .splitlines()
          )
          if line.strip()
      ]
      tampered[0]['resolved_event'] = 'tampered'
      (batch_dir / 'replica_turns.jsonl').write_text(
          '\n'.join(json.dumps(t, ensure_ascii=False) for t in tampered) + '\n',
          encoding='utf-8',
      )
      with self.assertRaises(fp.ManifestError):
        fp.verify_manifest(manifest, batch_dir)


def _replica_manifest(batch_dir: pathlib.Path) -> fp.Manifest:
  """Builds a small synthetic manifest over replica batch files.

  The replica keeps the real manifest's schema and extraction kinds
  (jsonl field list, response JSON field, prompt section) so that
  ``verify_manifest`` runs the same re-extraction code path offline;
  the source files are synthetic, so no ``run_results/`` dependency
  exists.
  """
  turns = [
      {'turn_id': 1, 'resolved_event': '第一回合事件。'},
      {'turn_id': 2, 'resolved_event': '第二回合事件。'},
  ]
  turns_text = '\n'.join(json.dumps(t, ensure_ascii=False) for t in turns)
  turns_text += '\n'
  (batch_dir / 'replica_turns.jsonl').write_text(turns_text, encoding='utf-8')
  response = {'speech': '复述用对白。', 'action_intent': '复述用动作。'}
  (batch_dir / 'replica_response.txt').write_text(
      json.dumps(response, ensure_ascii=False), encoding='utf-8'
  )
  prompt = (
      '# Repetition check (current turn candidate)\n'
      '## Committed public events (oldest first)\n'
      '- 已提交事件\n'
      '## Current candidate (not yet committed)\n'
      '候选事件正文。\n'
      "## Current turn's adjudicated action result\n"
      '(none)\n'
  )
  (batch_dir / 'replica_prompt.txt').write_text(prompt, encoding='utf-8')
  base = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
  base['batch_sources'] = {
      'replica_turns': {
          'path': 'replica_turns.jsonl',
          'sha256': hashlib.sha256(turns_text.encode('utf-8')).hexdigest(),
      },
      'replica_response': {
          'path': 'replica_response.txt',
          'sha256': (
              hashlib.sha256(
                  json.dumps(response, ensure_ascii=False).encode('utf-8')
              ).hexdigest()
          ),
      },
      'replica_prompt': {
          'path': 'replica_prompt.txt',
          'sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
      },
  }
  base['fixtures']['B-1'] = copy.deepcopy(base['fixtures']['B-1'])
  base['fixtures']['B-1']['history'] = {
      'kind': 'jsonl_field_list',
      'source': 'replica_turns',
      'turn_ids': [1, 2],
      'field': 'resolved_event',
      'value': ['第一回合事件。', '第二回合事件。'],
  }
  base['fixtures']['B-1']['speech'] = {
      'kind': 'response_json_field',
      'source': 'replica_response',
      'field': 'speech',
      'value': '复述用对白。',
  }
  base['fixtures']['C-1']['candidate'] = {
      'kind': 'prompt_section',
      'source': 'replica_prompt',
      'heading': '## Current candidate (not yet committed)',
      'value': '候选事件正文。',
  }
  # Drop the remaining batch-derived entries not covered by the replica.
  base['fixtures']['B-1']['action_intent'] = {
      'kind': 'inline',
      'value': '复述用动作。',
  }
  base['fixtures']['B-1']['action_result'] = {
      'kind': 'inline',
      'value': '裁决结果全文。',
  }
  # The real B-1 also pins the complete historical generation request.
  # This synthetic batch carries synthetic equivalents, never real files.
  for field, value in (
      ('core_result', '裁决结果全文。'),
      ('candidate', '复述用动作。 Because of that, 裁决结果全文。'),
      ('history_text', '0). 第一回合事件。\n1). 第二回合事件。'),
      ('generation_prefix', ''),
      ('generation_prompt', 'synthetic generation request'),
  ):
    base['fixtures']['B-1'][field] = {'kind': 'inline', 'value': value}
  base['fixtures']['B-3']['draft'] = {
      'kind': 'inline',
      'value': '固定草稿。',
  }
  for unit in ('C-1',):
    for field in ('history', 'speech', 'action_intent', 'gate'):
      if field in base['fixtures'][unit]:
        base['fixtures'][unit][field] = {
            'kind': 'inline',
            'value': ['占位'] if field == 'history' else '占位',
        }
  with tempfile.TemporaryDirectory() as manifest_dir:
    manifest_path = pathlib.Path(manifest_dir) / 'replica_manifest.json'
    manifest_path.write_text(
        json.dumps(base, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return fp.load_manifest(manifest_path)


class CodeStateTest(unittest.TestCase):
  """Dual code-state loading and the byte-identity dependency audit."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)
    cls.before_dir = pathlib.Path(
        cls.manifest.code_states['before']['snapshot_dir']
    )
    cls.repo_root = pathlib.Path(fp.__file__).parents[2]
    cls.game_master = 'concordia/contrib/components/game_master/'

  def test_audit_rejects_any_drift_beyond_the_two_files(self):
    production = self.repo_root / 'concordia/contrib/components/game_master/'
    with tempfile.TemporaryDirectory() as tmp:
      before = pathlib.Path(tmp) / 'before'
      after = pathlib.Path(tmp) / 'after'
      for root in (before, after):
        target = root / self.game_master
        target.mkdir(parents=True)
        for name in ('story_resolution.py', 'repetition_push.py'):
          shutil.copyfile(production / name, target / name)
      # Only the two production files differ -> no drift findings.
      (before / self.game_master / 'story_resolution.py').write_text(
          '# before variant\n', encoding='utf-8'
      )
      self.assertEqual(fp.audit_dependency_identity(before, after), [])
      # A drifted third module inside the import closure -> reported.
      for root, marker in ((before, 'A'), (after, 'B')):
        (root / self.game_master / 'drama_director.py').write_text(
            f'# drifted third module {marker}\n', encoding='utf-8'
        )
      findings = fp.audit_dependency_identity(before, after)
      self.assertTrue(
          any('drama_director' in finding for finding in findings), findings
      )

  @unittest.skipUnless(
      pathlib.Path(
          fp.load_manifest(_MANIFEST_PATH).code_states['before']['snapshot_dir']
      ).is_dir(),
      'frozen before-state snapshot not present (offline CI)',
  )
  def test_load_code_state_isolates_namespaces(self):
    before_story = fp.load_code_state(
        'before_story_resolution',
        self.before_dir / self.game_master / 'story_resolution.py',
    )
    after_story = fp.load_code_state(
        'after_story_resolution',
        self.repo_root / self.game_master / 'story_resolution.py',
    )
    self.assertTrue(hasattr(after_story, '_RESULT_FIDELITY_GUIDANCE'))
    self.assertFalse(hasattr(before_story, '_RESULT_FIDELITY_GUIDANCE'))
    self.assertNotEqual(before_story.__name__, after_story.__name__)
    # The production module in sys.modules remains the current tree's.
    production = importlib.import_module(
        'concordia.contrib.components.game_master.story_resolution'
    )
    self.assertTrue(hasattr(production, '_RESULT_FIDELITY_GUIDANCE'))
    # C side: the after state renders the player-contribution data
    # section; the before state does not (stub director, no model call).
    director = _stub_director()
    after_rep = fp.load_code_state(
        'after_repetition_push',
        self.repo_root / self.game_master / 'repetition_push.py',
    )
    before_rep = fp.load_code_state(
        'before_repetition_push',
        self.before_dir / self.game_master / 'repetition_push.py',
    )
    after_text = _render_repetition_context(after_rep, director)
    before_text = _render_repetition_context(before_rep, director)
    self.assertIn('## Current player contribution', after_text)
    self.assertIn('有件事我当着大家说清楚', after_text)
    self.assertNotIn('## Current player contribution', before_text)

  def test_load_code_state_isolates_namespaces_hermetic(self):
    """Namespace isolation without the frozen snapshot (CI-safe half)."""
    with tempfile.TemporaryDirectory() as tmp:
      before_file = pathlib.Path(tmp) / 'before_mod.py'
      after_file = pathlib.Path(tmp) / 'after_mod.py'
      before_file.write_text('MARK = "before"\n', encoding='utf-8')
      after_file.write_text(
          'MARK = "after"\n_RESULT_FIDELITY_GUIDANCE = "guidance"\n',
          encoding='utf-8',
      )
      before = fp.load_code_state('iso_before', before_file)
      after = fp.load_code_state('iso_after', after_file)
      self.assertEqual(before.MARK, 'before')
      self.assertEqual(after.MARK, 'after')
      self.assertFalse(hasattr(before, '_RESULT_FIDELITY_GUIDANCE'))
      self.assertTrue(hasattr(after, '_RESULT_FIDELITY_GUIDANCE'))


class _StubDirector:
  """Minimal director surface read by ``_state_repetition_context``."""

  def get_committed_events(self):
    return ('已提交事件一',)

  def get_current_original_speech(self):
    return '有件事我当着大家说清楚：我欠了一笔赌债。'

  def get_current_action_intent(self):
    return '陈默从桌边退开半步。'

  def get_current_resolution_evidence(self):
    return {}

  allowed_environment_events = ('敲门声',)
  immutable_facts = ('老宅在城中村。',)
  player_names = ('王婶', '陈默', '周律师', '林雪', '陈母')
  character_constraints = {'陈默': '不主动暴露赌债以外的事。'}
  world_constraints = ('堂灯时明时暗。',)


def _stub_director() -> _StubDirector:
  return _StubDirector()


def _render_repetition_context(module, director) -> str:
  """Renders the repetition context through a loaded module's helper."""
  document = interactive_document.InteractiveDocument(model=_ChoiceRecorder())
  module._state_repetition_context(
      document=document,
      director=director,
      candidate_event='陈默从桌边退开半步。 Because of that, 退让成功。',
      active_player_name='陈默',
  )
  return document.view().text()


class _ChoiceRecorder(language_model.LanguageModel):
  """Stub model recording every prompt and answering the first option."""

  def __init__(self):
    self.prompts: list[str] = []

  def sample_text(self, prompt, **kwargs):
    self.prompts.append(prompt)
    return 'ok'

  def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
    self.prompts.append(prompt)
    return 0, responses[0], ''


def _option_order(prompt: str) -> tuple[str, ...]:
  """Extracts the presented option order from a choice prompt."""
  lines = [
      line.strip()
      for line in prompt.splitlines()
      if line.strip().startswith('(')
  ]
  return tuple(line.split(') ', 1)[1] for line in lines)


class SeededStepTest(unittest.TestCase):
  """Paired-seed control of both RNG layers with full restoration."""

  def test_same_seed_same_option_permutation(self):
    orders_by_run = []
    for _ in range(2):
      model = _ChoiceRecorder()
      with fp.seeded_step(np_seed=1234567, py_seed=7654321) as session:
        self.assertIsInstance(session, fp.SeedSession)
        document = interactive_document.InteractiveDocument(model=model)
        for question in ('第一个判断问题？', '第二个判断问题？'):
          document.multiple_choice_question(question, ['甲', '乙', '丙', '丁'])
      orders_by_run.append([_option_order(prompt) for prompt in model.prompts])
    self.assertEqual(orders_by_run[0], orders_by_run[1])
    # A different NumPy seed must not silently reuse the same stream.
    model = _ChoiceRecorder()
    with fp.seeded_step(np_seed=7654321, py_seed=7654321):
      document = interactive_document.InteractiveDocument(model=model)
      for question in ('第一个判断问题？', '第二个判断问题？'):
        document.multiple_choice_question(question, ['甲', '乙', '丙', '丁'])
    different_orders = [_option_order(prompt) for prompt in model.prompts]
    self.assertNotEqual(orders_by_run[0], different_orders)

  def test_factory_passes_through_explicit_rng_no_reseed(self):
    with fp.seeded_step(np_seed=42, py_seed=24) as session:
      document = interactive_document.InteractiveDocument(
          model=_ChoiceRecorder()
      )
      first = document._rng
      construction_state = session.construction_state(first)
      self.assertIsNotNone(construction_state)
      self.assertEqual(
          first.bit_generator.state, copy.deepcopy(construction_state)
      )
      before_copy = copy.deepcopy(first.bit_generator.state)
      replica = document.copy()
      # copy() passes the same generator through; no reseed, no draw,
      # and no new factory construction was recorded for it.
      self.assertIs(replica._rng, first)
      self.assertEqual(session.construction_count, 1)
      self.assertEqual(first.bit_generator.state, before_copy)
      # A draw on the original advances the shared stream; a further
      # copy still shares it (pairing semantics: same condition, one
      # stream shared across the document family).
      document._rng.permutation(8)
      after_draw = copy.deepcopy(first.bit_generator.state)
      self.assertNotEqual(after_draw, before_copy)
      self.assertIs(document.copy()._rng, first)
      self.assertEqual(first.bit_generator.state, after_draw)

  def test_wrapper_restores_symbol_on_exit_and_exception(self):
    module = interactive_document
    original = module.InteractiveDocument
    with fp.seeded_step(np_seed=1, py_seed=1):
      self.assertIsNot(module.InteractiveDocument, original)
    self.assertIs(module.InteractiveDocument, original)
    with self.assertRaises(RuntimeError):
      with fp.seeded_step(np_seed=1, py_seed=1):
        raise RuntimeError('body failure')
    self.assertIs(module.InteractiveDocument, original)

  def test_python_rng_seeded_for_event_extraction(self):
    def _draw():
      return [
          random.choice(['事件一', '事件二', '事件三', '事件四'])
          for _ in range(4)
      ]

    with fp.seeded_step(np_seed=99, py_seed=555001):
      first = _draw()
    with fp.seeded_step(np_seed=99, py_seed=555001):
      second = _draw()
    self.assertEqual(first, second)
    with fp.seeded_step(np_seed=99, py_seed=555002):
      third = _draw()
    self.assertNotEqual(first, third)

  def test_nested_seeded_step_is_forbidden(self):
    with fp.seeded_step(np_seed=1, py_seed=1):
      with self.assertRaises(RuntimeError):
        with fp.seeded_step(np_seed=2, py_seed=2):
          pass


class _GateOpenDirector(_StubDirector):
  """Director surface of ``make_repetition_push_step`` with the gate open."""

  def should_attempt_repetition_push(self):
    return True

  def record_repetition_result(self, **kwargs):
    self.repetition_results = getattr(self, 'repetition_results', [])
    self.repetition_results.append(kwargs)

  def record_resolution_diagnostics(self, **kwargs):
    self.diagnostics = getattr(self, 'diagnostics', [])
    self.diagnostics.append(kwargs)


class RngProofTest(unittest.TestCase):
  """Execution-time proof that the paired seed reached the inner doc."""

  @classmethod
  def setUpClass(cls):
    cls.repo_root = pathlib.Path(fp.__file__).parents[2]
    cls.module_path = (
        cls.repo_root
        / 'concordia/contrib/components/game_master/repetition_push.py'
    )

  def test_state_compared_before_helper_consumes_stream(self):
    module = fp.load_code_state('proof_repetition_push', self.module_path)
    core = module.event_resolution_lib
    original_helper = core.maybe_inject_narrative_push
    director = _GateOpenDirector()
    model = _ChoiceRecorder()
    step = module.make_repetition_push_step(director, model)
    with fp.seeded_step(np_seed=20261025, py_seed=1122) as session:
      with fp.rng_proof(module, session) as proofs:
        result = step(
            interactive_document.InteractiveDocument(model=model),
            '王婶往桌边退开半步。 Because of that, 退让成功。',
            '王婶',
        )
    self.assertIsInstance(result, str)
    self.assertEqual(len(proofs), 1)
    proof = proofs[0]
    # The check happened before the helper consumed the stream:
    # construction-time == check-time != state after execution.
    self.assertEqual(proof['construction_state'], proof['check_state'])
    self.assertNotEqual(proof['check_state'], proof['state_after'])
    self.assertIs(core.maybe_inject_narrative_push, original_helper)

  def test_mismatch_raises_integrity_error(self):
    module = fp.load_code_state('proof2_repetition_push', self.module_path)
    core = module.event_resolution_lib
    original_helper = core.maybe_inject_narrative_push
    director = _GateOpenDirector()
    model = _ChoiceRecorder()
    step = module.make_repetition_push_step(director, model)
    unseeded_session = fp.SeedSession(np_seed=1, py_seed=1)
    with fp.seeded_step(np_seed=999, py_seed=999):
      with fp.rng_proof(module, unseeded_session) as proofs:
        with self.assertRaises(fp.IntegrityError):
          step(
              interactive_document.InteractiveDocument(model=model),
              '候选事件。',
              '王婶',
          )
    self.assertEqual(proofs, [])
    # Exception path restores the core symbol as well.
    self.assertIs(core.maybe_inject_narrative_push, original_helper)


# ---------------------------------------------------------------------------
# Execution-engine tests (Task 5): stub model, temp batch directory.
# ---------------------------------------------------------------------------

_GENERATION_MARK = 'Output a JSON object with exactly one string field'
_REVIEW_MARK = (
    'Evaluate all rules above. Output only a JSON object with exactly'
)
_JUDGMENT_MARK = (
    'Is the story traced out by the above list of events repetitive?'
)
_SUGGESTION_MARK = 'Suggest five plausible events'
_FUSION_MARK = 'How does 2 modify'
_B2_DRAFT_MARK = '整个文件范围内没有见到见证人的署名'
_B1_INTENT_MARK = '尝试仔细查阅全文的书写形式与签署情况'


class _StubModel(language_model.LanguageModel):
  """Fingerprint-routed deterministic model for engine tests.

  Routes like ``testing_stubs`` (last question block, option text
  rather than option letter); counters make one specific execution
  position misbehave without touching any other.
  """

  def __init__(
      self,
      *,
      judgment_yes: bool = False,
      review_reject_marker: str | None = None,
      generation_glitch_count: int | None = None,
  ):
    self.judgment_yes = judgment_yes
    self.review_reject_marker = review_reject_marker
    self.generation_glitch_count = generation_glitch_count
    self.generation_calls = 0
    self.calls: list[tuple[str, str]] = []

  def _fingerprint(self, prompt: str) -> str:
    for mark, name in (
        (_GENERATION_MARK, 'generation'),
        (_REVIEW_MARK, 'review'),
        (_SUGGESTION_MARK, 'suggestion'),
        (_FUSION_MARK, 'fusion'),
        (_JUDGMENT_MARK, 'judgment'),
    ):
      if mark in prompt:
        return name
    raise AssertionError(f'unrouted prompt:\n{prompt[:400]}')

  def sample_text(self, prompt, **kwargs):
    fingerprint = self._fingerprint(prompt)
    self.calls.append((fingerprint, prompt))
    if fingerprint == 'generation':
      self.generation_calls += 1
      if self.generation_glitch_count == self.generation_calls:
        return '这不是合法 JSON 的模型输出。'
      return json.dumps(
          {'stage_directions': '周律师逐页查阅了原件全文的落款与签署。'},
          ensure_ascii=False,
      )
    if fingerprint == 'review':
      if self.review_reject_marker and self.review_reject_marker in prompt:
        return json.dumps(
            {'valid': False, 'reason': '规则7：裁决结果被改写。'},
            ensure_ascii=False,
        )
      return json.dumps({'valid': True, 'reason': '合法。'}, ensure_ascii=False)
    if fingerprint == 'suggestion':
      return '线索一\n线索二\n线索三\n线索四\n线索五'
    return '融合后的复合事件。'

  def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
    self.calls.append(('judgment', prompt))
    target = 'Yes' if self.judgment_yes else 'No'
    match = re.search(rf'^  \((\w+)\) {target}\s*$', prompt, re.MULTILINE)
    assert match is not None, 'no presented option matched the target'
    letter = match.group(1)
    index = responses.index(letter)
    return index, letter, ''


class EngineTest(unittest.TestCase):
  """Ordering, four-state bookkeeping, budget stop and diff audit."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def _run(self, model, tmp, **budget):
    return fp.run_batch(
        self.manifest,
        model,
        pathlib.Path(tmp),
        **budget,
    )

  def test_order_follows_protocol_and_records_skips(self):
    with tempfile.TemporaryDirectory() as tmp:
      model = _StubModel(generation_glitch_count=3)  # B-1 rep2 before
      report = self._run(model, tmp)
      executed = [
          (entry['unit'], entry['condition'])
          for entry in report.execution_ledger
      ]
      self.assertEqual(executed, fp.expand_order(self.manifest))
      states = report.stage_states
      # The glitched repetition: generation FAILED, review skipped with
      # no call at all, and the paired condition still ran normally.
      self.assertEqual(
          states['B-1|before|r2|generation'], fp.StageState.FAILED.name
      )
      self.assertEqual(
          states['B-1|before|r2|review'],
          fp.StageState.SKIPPED_UPSTREAM.name,
      )
      self.assertEqual(
          states['B-1|after|r2|generation'], fp.StageState.FINISHED.name
      )
      glitched_review_prompts = [
          prompt
          for fingerprint, prompt in model.calls
          if fingerprint == 'review'
      ]
      self.assertEqual(len(glitched_review_prompts), 14)
      glitched_entry = next(
          entry
          for entry in report.execution_ledger
          if entry['unit'] == 'B-1'
          and entry['condition'] == 'before'
          and entry['rep'] == 2
      )
      self.assertEqual(glitched_entry['slots']['review'], [])
      self.assertEqual(len(glitched_entry['slots']['generation']), 1)
      # Later units ran normally.
      self.assertEqual(
          states['C-3|after|r3|judgment'], fp.StageState.FINISHED.name
      )

  def test_review_rejection_continues_batch(self):
    with tempfile.TemporaryDirectory() as tmp:
      model = _StubModel(review_reject_marker=_B2_DRAFT_MARK)
      report = self._run(model, tmp)
      for rep in (1, 2, 3):
        self.assertEqual(
            report.stage_states[f'B-2|after|r{rep}|review'],
            fp.StageState.FINISHED.name,
        )
        self.assertEqual(
            report.outcomes[f'B-2|after|r{rep}|review'],
            'semantic_rejected',
        )
      self.assertEqual(
          report.stage_states['C-3|after|r3|judgment'],
          fp.StageState.FINISHED.name,
      )

  def test_c_no_marks_suggestion_fusion_not_applicable(self):
    with tempfile.TemporaryDirectory() as tmp:
      model = _StubModel(judgment_yes=False)
      report = self._run(model, tmp)
      for unit in ('C-1', 'C-2', 'C-3'):
        for condition in ('before', 'after'):
          for rep in (1, 2, 3):
            self.assertEqual(
                report.stage_states[f'{unit}|{condition}|r{rep}|suggestion'],
                fp.StageState.NOT_APPLICABLE.name,
            )
            self.assertEqual(
                report.stage_states[f'{unit}|{condition}|r{rep}|fusion'],
                fp.StageState.NOT_APPLICABLE.name,
            )
      self.assertFalse(
          any(f in ('suggestion', 'fusion') for f, _ in model.calls)
      )

  def test_budget_truncation_marks_not_started_budget(self):
    with tempfile.TemporaryDirectory() as tmp:
      model = _StubModel()
      report = self._run(model, tmp, attempts_global_cap=6)
      states = report.stage_states
      self.assertEqual(
          states['B-3|after|r1|review'], fp.StageState.FINISHED.name
      )
      self.assertEqual(
          states['B-4|after|r1|review'],
          fp.StageState.NOT_STARTED_BUDGET.name,
      )
      self.assertEqual(
          states['C-3|after|r3|judgment'],
          fp.StageState.NOT_STARTED_BUDGET.name,
      )
      self.assertEqual(report.ledger_counts['global'], 6)
      self.assertIsNotNone(report.fault)
      assert report.fault is not None
      self.assertEqual(report.fault['kind'], 'budget_stop')
    with tempfile.TemporaryDirectory() as tmp:
      model = _StubModel()
      report = self._run(model, tmp, attempts_group_caps={'B': 4, 'C': 54})
      self.assertEqual(
          report.stage_states['B-2|after|r1|review'],
          fp.StageState.NOT_STARTED_BUDGET.name,
      )
      self.assertEqual(report.ledger_counts['by_group']['B-1-before'], 2)

  def test_controlled_stage_diff_audit_blocks_other_differences(self):
    base = '前缀\n## Committed public events\n- 事件\n'
    b_tail = '## Output\n输出要求。'
    c_tail = '## Current candidate (not yet committed)\n候选。\n'
    contribution = (
        '## Current player contribution (not yet committed, data)\n'
        '{"active_player_name": "陈默"}\n'
        '随附说明文字。\n'
    )
    before_prompt = base + c_tail
    after_only_section = base + contribution + c_tail
    self.assertEqual(
        fp.expected_diff_audit('C', before_prompt, after_only_section), []
    )
    after_extra_char = base + contribution + c_tail + 'X'
    self.assertNotEqual(
        fp.expected_diff_audit('C', before_prompt, after_extra_char), []
    )
    guidance = (
        "## Deliver this turn's adjudicated result (generation"
        ' guidance)\n指引内容。\n'
    )
    before_b = base + b_tail
    after_b = base + guidance + '\n' + b_tail
    self.assertEqual(fp.expected_diff_audit('B', before_b, after_b), [])
    after_b_extra = base + guidance + '\n' + b_tail + 'Y'
    self.assertNotEqual(
        fp.expected_diff_audit('B', before_b, after_b_extra), []
    )

  def test_ledger_books_per_unit_condition_groups(self):
    with tempfile.TemporaryDirectory() as tmp:
      # C-1 answers Yes (suggestion+fusion billed), C-2/C-3 answer No.
      model = _StubModel(judgment_yes=False)

      class _YesForC1(_StubModel):
        """Answers Yes exactly for C-1's judgment calls.

        In the before condition C-1's and C-3's judgment prompts are
        byte-identical (same history and candidate; the speech rides
        only in the after-only contribution section), so the position
        in the frozen execution order is the only discriminator.
        """

        def __init__(self):
          super().__init__()
          self.judgment_calls = 0

        def sample_choice(
            self, prompt: str, responses, *, seed: int | None = None
        ):
          index = self.judgment_calls
          self.judgment_calls += 1
          self.judgment_yes = index % 6 < 2  # C-1 before/after per rep
          return super().sample_choice(prompt, responses, seed=seed)

      yes_model = _YesForC1()
      report = self._run(yes_model, tmp)
      expected = {
          'B-1-before': 6,
          'B-1-after': 6,
          'B-2-after': 3,
          'B-3-after': 3,
          'B-4-after': 3,
          'C-1-before': 9,
          'C-1-after': 9,
          'C-2-before': 3,
          'C-2-after': 3,
          'C-3-before': 3,
          'C-3-after': 3,
      }
      self.assertEqual(report.ledger_counts['by_group'], expected)
      self.assertEqual(report.ledger_counts['global'], 51)
      self.assertEqual(report.sdk_counts['global'], 51)


class DryRunTest(unittest.TestCase):
  """Full-batch zero-network dry run and archive integrity."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_dry_run_full_batch_on_stub_model(self):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      model = _StubModel()
      report = fp.run_batch(self.manifest, model, out_dir)
      self.assertIsNone(report.fault)
      # Full frozen order executed: 33 unit-condition-reps.
      self.assertEqual(len(report.execution_ledger), 33)
      # Four-state closure: every unit-condition-rep-stage has exactly
      # one state; total stage keys match the frozen matrix.
      self.assertEqual(len(report.stage_states), 75)
      self.assertTrue(
          all(
              state in set(self.manifest.labels['stage_states'])
              for state in report.stage_states.values()
          )
      )
      # Two ledger layers agree; every started slot archived its
      # prompt and raw response in both layers.
      concordia_started = report.ledger_counts['started_slots']
      self.assertEqual(
          report.ledger_counts['global'], report.sdk_counts['global']
      )
      for layer_dir in (out_dir, out_dir / 'sdk_ledger'):
        for slot in (
            concordia_started
            if layer_dir == out_dir
            else (report.sdk_counts['started_slots'])
        ):
          prompt_file = layer_dir / 'raw' / f'{slot}.prompt.txt'
          response_file = layer_dir / 'raw' / f'{slot}.response.txt'
          self.assertTrue(prompt_file.is_file(), slot)
          self.assertTrue(prompt_file.read_text(encoding='utf-8'))
          self.assertTrue(response_file.is_file(), slot)
          self.assertTrue(response_file.read_text(encoding='utf-8'))
      # Every judgment archived its presented option order.
      option_lines = (
          (out_dir / 'option_orders.jsonl')
          .read_text(encoding='utf-8')
          .splitlines()
      )
      option_records = [json.loads(line) for line in option_lines]
      self.assertEqual(len(option_records), 18)
      self.assertTrue(
          all(len(record['options']) == 2 for record in option_records)
      )
      # Seeds and RNG proofs are archived per execution.
      ledger_entries = [
          json.loads(line)
          for line in (
              (out_dir / 'execution_ledger.jsonl')
              .read_text(encoding='utf-8')
              .splitlines()
          )
      ]
      self.assertEqual(len(ledger_entries), 33)
      for entry in ledger_entries:
        self.assertIn('seeds', entry)
        self.assertEqual(
            entry['seeds']['py_seed'],
            self.manifest.seeds[f"{entry['unit']}|rep{entry['rep']}"][
                'py_seed'
            ],
        )
        if entry['unit'].startswith('C-'):
          proofs = entry['rng_proofs']
          self.assertEqual(len(proofs), 1)
          self.assertEqual(
              proofs[0]['construction_state'], proofs[0]['check_state']
          )
          self.assertNotEqual(
              proofs[0]['check_state'], proofs[0]['state_after']
          )
      # Controlled-diff audit archived for every paired unit-rep.
      audits = json.loads(
          (out_dir / 'audit_records.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(audits), 12)
      self.assertTrue(all(not record['findings'] for record in audits))
      # Report on disk with both layers' counts.
      disk_report = json.loads(
          (out_dir / 'report.json').read_text(encoding='utf-8')
      )
      self.assertEqual(disk_report['ledger_counts']['global'], 39)
      self.assertEqual(disk_report['sdk_counts']['global'], 39)


class RubricReportTest(unittest.TestCase):
  """Preregistered evaluation-table skeleton (protocol §5)."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_report_prefilled_rubric_keeps_human_pending(self):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      model = _StubModel(
          judgment_yes=True,
          review_reject_marker=_B2_DRAFT_MARK,
          generation_glitch_count=3,
      )
      report = fp.run_batch(self.manifest, model, out_dir)
      tables = fp.write_rubric_report(self.manifest, report, out_dir)
      # B table: preregistered columns, agent columns left empty.
      self.assertEqual(
          tables['b_table']['columns'],
          self.manifest.labels['b_table_columns'],
      )
      b_rows = tables['b_table']['rows']
      self.assertEqual(len(b_rows), 15)
      for row in b_rows:
        self.assertTrue(row['单元'] and row['条件'] and row['重复'])
        for column in self.manifest.labels['b_rubric_columns']:
          self.assertEqual(row[column], '')
      # C table: observed prefills only, never evaluator interpretation.
      self.assertEqual(
          tables['c_table']['columns'],
          self.manifest.labels['c_table_columns'],
      )
      c_rows = tables['c_table']['rows']
      self.assertEqual(len(c_rows), 18)
      for row in c_rows:
        self.assertTrue(
            row['模型选择'] == 'No' or row['模型选择'].startswith('Yes'),
            row['模型选择'],
        )
        self.assertEqual(row['模型理由'], '未提供')
        self.assertEqual(row['agent 评价依据'], '')
        self.assertEqual(row['人工评价依据'], 'pending')
      yes_rows = [row for row in c_rows if row['模型选择'].startswith('Yes')]
      self.assertEqual(len(yes_rows), 18)
      for row in yes_rows:
        self.assertIn('融合后的复合事件。', row['模型选择'])
      # Classified counts agree with the booked outcomes.
      outcomes = report.outcomes
      counts = tables['counts']
      self.assertEqual(
          counts['semantic_rejections'],
          sum(1 for v in outcomes.values() if v == 'semantic_rejected'),
      )
      self.assertEqual(
          counts['generation_format_failures'],
          sum(1 for v in outcomes.values() if v == 'generation_format_failure'),
      )
      self.assertEqual(
          counts['judgment_yes'],
          sum(
              1
              for k, v in outcomes.items()
              if k.endswith('|judgment') and v == 'yes'
          ),
      )
      self.assertEqual(counts['judgment_no'], 0)
      self.assertEqual(counts['human_pending'], len(c_rows))
      # The skeleton is archived with the batch.
      disk_tables = json.loads(
          (out_dir / 'evaluation_tables.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(disk_tables['c_table']['rows']), 18)


if __name__ == '__main__':
  unittest.main()


class _ReAskClientStub(language_model.LanguageModel):
  """Inner model whose choice path issues two client creates per call.

  Simulates the adapter's judgment re-ask loop at the client boundary:
  ``sample_choice`` drives ``_client.chat.completions.create`` twice
  (an invalid first answer, then a valid one), exactly the path that
  must be counted at the SDK-create layer.
  """

  def __init__(self):
    self.creates = 0
    client = types.SimpleNamespace()
    completions = types.SimpleNamespace()
    completions.create = self._create
    client.chat = types.SimpleNamespace(completions=completions)
    self._client = client

  def _create(self, *args, **kwargs):
    self.creates += 1
    content = 'invalid' if self.creates % 2 == 1 else 'Answer: (a)'
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)]
    )

  def sample_text(self, prompt, **kwargs):
    return 'ok'

  def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
    first = self._client.chat.completions.create(
        messages=[{'role': 'user', 'content': prompt}]
    )
    second = self._client.chat.completions.create(
        messages=[{'role': 'user', 'content': prompt}]
    )
    del first
    return 0, responses[0], second.choices[0].message.content


class _JudgmentFormatErrorStub(_StubModel):
  """Judgment calls exhaust the re-ask loop (InvalidResponseError)."""

  def __init__(self):
    super().__init__()
    self.judgment_calls = 0

  def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
    self.judgment_calls += 1
    if self.judgment_calls == 2:  # C-1 after, rep 1
      raise language_model.InvalidResponseError(
          'no valid choice after the maximum number of attempts'
      )
    return super().sample_choice(prompt, responses, seed=seed)


class ReviewFixTest(unittest.TestCase):
  """Findings from the whole-branch review (fix-pass RED tests)."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_sdk_guard_counts_client_boundary_reasks(self):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      ledger = rl.RequestLedger(
          out_dir,
          batch_id='fixed-probe',
          global_cap=75,
          model_caps={},
          group_caps={},
          sample_caps={},
      )
      guard = fp.SdkCreateGuard(out_dir, create_cap=225)
      model = fp.ProbeModel(
          _ReAskClientStub(),
          ledger=ledger,
          sdk_guard=guard,
          out_dir=out_dir,
      )
      model.bind('C-1-before', 'judgment')
      model.sample_choice(
          'Question: 判断\n  (a) Yes\n  (b) No\nAnswer: (', responses=['a', 'b']
      )
      counts = ledger.counts
      self.assertEqual(counts['global'], 1)
      # One attempt, but TWO SDK creates: the re-ask is visible and
      # capped at the layer the protocol names for it.
      self.assertEqual(guard.counts['global'], 2)
      sdk_slots = guard.counts['started_slots']
      for slot in sdk_slots:
        self.assertTrue(
            (out_dir / 'sdk_ledger' / 'raw' / f'{slot}.response.txt').is_file()
        )

  def test_closed_gate_is_a_batch_fault_not_a_silent_no(self):
    # A replica whose C history has seven turns leaves the repetition
    # gate shut (turn 8 does not hit the check interval); that must be
    # a loud batch fault with artifacts, never eighteen booked "No"s.
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      base = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
      history = base['fixtures']['C-1']['history']
      base['fixtures']['C-1']['history'] = {
          'kind': 'inline',
          'value': history['value'][:7],
      }
      manifest_path = out_dir / 'replica_manifest.json'
      manifest_path.write_text(
          json.dumps(base, ensure_ascii=False, indent=2), encoding='utf-8'
      )
      manifest = fp.load_manifest(manifest_path)
      with self.assertRaises(fp.BatchFault):
        fp.run_batch(manifest, _StubModel(), out_dir)
      stage_states = json.loads(
          (out_dir / 'stage_states.json').read_text(encoding='utf-8')
      )['stage_states']
      # A closed gate never forwarded a request: it cannot be pending.
      # Later positions stay unstarted and nothing is booked as "no".
      self.assertEqual(
          stage_states['C-1|before|r1|judgment'], 'NOT_STARTED_ORDER'
      )
      self.assertEqual(stage_states['C-3|after|r3|fusion'], 'NOT_STARTED_ORDER')
      fault = json.loads((out_dir / 'report.json').read_text(encoding='utf-8'))[
          'fault'
      ]
      self.assertEqual(fault['kind'], 'batch_fault')
      self.assertIn('gate', fault['error'])

  def test_judgment_format_failure_books_and_continues(self):
    with tempfile.TemporaryDirectory() as tmp:
      model = _JudgmentFormatErrorStub()
      report = fp.run_batch(self.manifest, model, pathlib.Path(tmp))
      self.assertIsNone(report.fault)
      self.assertEqual(report.stage_states['C-1|after|r1|judgment'], 'FAILED')
      self.assertEqual(
          report.outcomes['C-1|after|r1|judgment'],
          'judgment_format_failure',
      )
      self.assertEqual(
          report.stage_states['C-1|after|r1|suggestion'],
          'SKIPPED_UPSTREAM',
      )
      # The batch continued: the same condition's later repetitions
      # and every later unit ran normally.
      self.assertEqual(report.stage_states['C-1|after|r2|judgment'], 'FINISHED')
      self.assertEqual(report.stage_states['C-3|after|r3|judgment'], 'FINISHED')

  def test_group_cap_blocks_each_forward_inside_a_unit(self):
    for caps, group in (({'B': 1, 'C': 54}, 'B'), ({'B': 21, 'C': 1}, 'C')):
      with self.subTest(group=group), tempfile.TemporaryDirectory() as tmp:
        model = _StubModel(judgment_yes=True)
        report = fp.run_batch(
            self.manifest, model, tmp, attempts_group_caps=caps
        )
        used = sum(
            count
            for name, count in report.ledger_counts['by_group'].items()
            if name.startswith(group + '-')
        )
        self.assertEqual(used, 1)
        assert report.fault is not None
        self.assertEqual(report.fault['kind'], 'budget_stop')

  def test_mid_unit_budget_stop_preserves_completed_generation(self):
    with tempfile.TemporaryDirectory() as tmp:
      report = fp.run_batch(
          self.manifest, _StubModel(), tmp, attempts_global_cap=1
      )
      self.assertEqual(
          report.stage_states['B-1|before|r1|generation'], 'FINISHED'
      )
      self.assertEqual(
          report.stage_states['B-1|before|r1|review'], 'NOT_STARTED_BUDGET'
      )
      self.assertEqual(len(report.unit_records), 1)
      self.assertEqual(
          len(report.execution_ledger[0]['slots']['generation']), 1
      )

  def test_sdk_stop_preserves_started_failed_review(self):
    with tempfile.TemporaryDirectory() as tmp:
      report = fp.run_batch(self.manifest, _StubModel(), tmp, sdk_create_cap=1)
      self.assertEqual(
          report.stage_states['B-1|before|r1|generation'], 'FINISHED'
      )
      self.assertEqual(report.stage_states['B-1|before|r1|review'], 'FAILED')
      self.assertEqual(report.ledger_counts['global'], 2)
      self.assertEqual(report.sdk_counts['global'], 1)

  def test_infrastructure_fault_states_follow_terminal_ledger(self):
    class ReviewFault(_StubModel):

      def sample_text(self, prompt, **kwargs):
        if self._fingerprint(prompt) == 'review':
          raise RuntimeError('offline review infrastructure fault')
        return super().sample_text(prompt, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaisesRegex(RuntimeError, 'infrastructure fault'):
        fp.run_batch(self.manifest, ReviewFault(), tmp)
      payload = json.loads(
          (pathlib.Path(tmp) / 'stage_states.json').read_text('utf-8')
      )
      self.assertEqual(
          payload['stage_states']['B-1|before|r1|generation'], 'FINISHED'
      )
      self.assertEqual(
          payload['stage_states']['B-1|before|r1|review'], 'FAILED'
      )
      self.assertNotIn('PENDING', payload['stage_states'].values())

  def test_failed_judgment_is_not_a_no_in_rubric(self):
    with tempfile.TemporaryDirectory() as tmp:
      report = fp.run_batch(self.manifest, _JudgmentFormatErrorStub(), tmp)
      tables = fp.write_rubric_report(self.manifest, report, tmp)
      row = next(
          r
          for r in tables['c_table']['rows']
          if (r['单元'], r['条件'], r['重复']) == ('C-1', 'after', 1)
      )
      self.assertIsNone(row['模型选择'])
      self.assertEqual(row['阶段状态'], 'FAILED')
      self.assertEqual(row['阶段结果'], 'judgment_format_failure')
      self.assertEqual(tables['counts']['judgment_no'], 17)
      self.assertEqual(tables['counts']['judgment_format_failures'], 1)
      self.assertEqual(len(report.audit_records), 12)

  def test_b1_replays_frozen_production_generation_prompt(self):
    expected = self.manifest.fixtures['B-1']['generation_prompt']['value']
    with tempfile.TemporaryDirectory() as tmp:
      report = fp.run_batch(self.manifest, _StubModel(), tmp)
      for record in report.unit_records:
        if record['unit'] != 'B-1':
          continue
        prompt = record['controlled_prompt']
        if record['condition'] == 'before':
          self.assertEqual(prompt, expected)
        else:
          self.assertEqual(fp.expected_diff_audit('B', expected, prompt), [])

  def test_suggestion_format_error_is_not_a_failed_judgment(self):
    class SuggestionFault(_StubModel):

      def sample_text(self, prompt, **kwargs):
        if self._fingerprint(prompt) == 'suggestion':
          raise language_model.InvalidResponseError('no visible suggestion')
        return super().sample_text(prompt, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
      with self.assertRaisesRegex(
          language_model.InvalidResponseError, 'no visible suggestion'
      ):
        fp.run_batch(self.manifest, SuggestionFault(judgment_yes=True), tmp)
      payload = json.loads(
          (pathlib.Path(tmp) / 'stage_states.json').read_text('utf-8')
      )
      states = payload['stage_states']
      self.assertEqual(states['C-1|before|r1|judgment'], 'FINISHED')
      self.assertEqual(states['C-1|before|r1|suggestion'], 'FAILED')
      self.assertEqual(states['C-1|before|r1|fusion'], 'SKIPPED_UPSTREAM')
      self.assertEqual(payload['outcomes']['C-1|before|r1|judgment'], 'yes')


class _SentinelEchoStub(_StubModel):
  """Echoes the sentinel into every response; faults one mid-batch call.

  The sentinel rides inside format-legal payload positions — the JSON
  string values of generation/review answers, the first suggestion and
  fusion line, the choice commentary — so it reaches every artifact
  tier, and the configured suggestion call raises an exception whose
  message carries the same sentinel to fault the batch mid-run.
  """

  def __init__(self, fault_on_suggestion: int | None = None):
    super().__init__(judgment_yes=True, review_reject_marker=_B2_DRAFT_MARK)
    self.suggestion_calls = 0
    self.fault_on_suggestion = fault_on_suggestion

  def sample_text(self, prompt, **kwargs):
    fingerprint = self._fingerprint(prompt)
    if fingerprint == 'suggestion':
      self.suggestion_calls += 1
      if self.suggestion_calls == self.fault_on_suggestion:
        raise RuntimeError('transport echoed SECRET-XYZ and failed')
    raw = super().sample_text(prompt, **kwargs)
    if fingerprint in ('generation', 'review'):
      payload = json.loads(raw)
      for key, value in payload.items():
        if isinstance(value, str):
          payload[key] = f'{value} SECRET-XYZ'
          break
      return json.dumps(payload, ensure_ascii=False)
    lines = raw.split('\n')
    lines[0] = f'{lines[0]} SECRET-XYZ'
    return '\n'.join(lines)

  def sample_choice(self, prompt: str, responses, *, seed: int | None = None):
    index, letter, _ = super().sample_choice(prompt, responses, seed=seed)
    return index, letter, 'echo SECRET-XYZ'


class SecretRedactionTest(unittest.TestCase):
  """Whole-chain secret scrubbing of ledgers and derived artifacts."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_secret_scrubbed_from_every_artifact(self):
    sentinel = 'SECRET-XYZ'
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      # Fault run: the sentinel rides in every model response and in
      # the mid-batch faulting exception's message.
      model = _SentinelEchoStub(fault_on_suggestion=3)
      with self.assertRaisesRegex(RuntimeError, 'echoed'):
        fp.run_batch(self.manifest, model, out_dir, secrets=(sentinel,))
      payload = json.loads(
          (out_dir / 'stage_states.json').read_text(encoding='utf-8')
      )
      assert payload['fault'] is not None
      self.assertEqual(payload['fault']['kind'], 'batch_fault')
      self.assertIn('[REDACTED]', payload['fault']['error'])
      self.assertNotIn(sentinel, payload['fault']['error'])
      # The echoed answers really were archived (redacted), so the
      # whole-tree scan below is not vacuous.
      self.assertIn(
          '[REDACTED]',
          (out_dir / 'requests.jsonl').read_text(encoding='utf-8'),
      )
      self.assert_no_sentinel_anywhere(out_dir, sentinel)
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      report = fp.run_batch(
          self.manifest, _SentinelEchoStub(), out_dir, secrets=(sentinel,)
      )
      self.assertIsNone(report.fault)
      fp.write_rubric_report(
          self.manifest, report, out_dir, secrets=(sentinel,)
      )
      # Unit records — including per-stage error text — come back
      # scrubbed (B-2's semantic rejection quotes the echoed reason).
      self.assertNotIn(
          sentinel, json.dumps(report.unit_records, ensure_ascii=False)
      )
      self.assertIn(
          '[REDACTED]', json.dumps(report.unit_records, ensure_ascii=False)
      )
      b2 = next(r for r in report.unit_records if r['unit'] == 'B-2')
      error = b2['stages']['review']['error']
      assert error is not None
      self.assertNotIn(sentinel, error)
      self.assertIn('[REDACTED]', error)
      # Rubric rows: the prefilled model-selection suggestion and
      # fusion text carries the redaction marker only.
      tables = json.loads(
          (out_dir / 'evaluation_tables.json').read_text(encoding='utf-8')
      )
      yes_rows = [
          row
          for row in tables['c_table']['rows']
          if str(row['模型选择']).startswith('Yes')
      ]
      self.assertTrue(yes_rows)
      for row in yes_rows:
        self.assertNotIn(sentinel, row['模型选择'])
        self.assertIn('[REDACTED]', row['模型选择'])
      self.assert_no_sentinel_anywhere(out_dir, sentinel)

  def assert_no_sentinel_anywhere(
      self, out_dir: pathlib.Path, sentinel: str
  ) -> None:
    """Fails listing every artifact file still containing the sentinel.

    Scans every file under the batch directory — both ledger layers
    (``requests.jsonl``, ``raw/``, ``sdk_ledger/``), ``stage_states``,
    ``audit_records``, the execution ledger and the evaluation tables —
    and automatically covers ``unit_records.json`` once it is written.
    """
    leaks = [
        str(path.relative_to(out_dir))
        for path in sorted(out_dir.rglob('*'))
        if path.is_file()
        and sentinel in path.read_text(encoding='utf-8', errors='replace')
    ]
    self.assertEqual(leaks, [], f'sentinel leaked into artifacts: {leaks}')


# ---------------------------------------------------------------------------
# Real-execution wiring, plan batch 2: run_batch injection points
# (Task 2), retry-aware stage classification (Task 3) and the
# fault-path report rebuild (Task 4).
# ---------------------------------------------------------------------------


class _ForwardingRecorder:
  """Recording engine wrapper forwarding the whole ProbeModel protocol.

  ``run_unit`` and ``_recover_unit_record`` drive the engine model
  through ``bind``, ``take_run_log``, ``take_run_events`` and the two
  sampling methods; a real run interposes the production retry wrapper
  here, so the books stay identical only if every member forwards.
  The sampling members can be delegated to a separate engine (e.g. a
  ``RetryLanguageModel`` over the same probe) while the bookkeeping
  members keep forwarding to the probe itself.
  """

  def __init__(self, probe: fp.ProbeModel, engine=None):
    self._probe = probe
    self._engine = probe if engine is None else engine
    self.bound: list[tuple[str, str]] = []

  def bind(self, group: str, stage_label: str) -> None:
    self.bound.append((group, stage_label))
    return self._probe.bind(group, stage_label)

  def take_run_log(self):
    return self._probe.take_run_log()

  def take_run_events(self):
    return self._probe.take_run_events()

  def sample_text(self, prompt, **kwargs):
    return self._engine.sample_text(prompt, **kwargs)

  def sample_choice(self, prompt, responses, **kwargs):
    return self._engine.sample_choice(prompt, responses, **kwargs)


def _no_backoff_retry(probe, retry_on):
  """The frozen outer-retry shape: <=3 tries, fixed 0.0 delay."""
  return retry_wrapper.RetryLanguageModel(
      probe,
      retry_on_exceptions=retry_on,
      retry_tries=3,
      retry_delay=0.0,
      jitter=(0.0, 0.0),
      exponential_backoff=False,
  )


class RunBatchInjectionTest(unittest.TestCase):
  """Task 2: outer retry, request identity and integrity injection."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_model_wrapper_receives_probe_model_and_forwards_protocol(self):
    with tempfile.TemporaryDirectory() as tmp_plain:
      with tempfile.TemporaryDirectory() as tmp_wrapped:
        plain = fp.run_batch(
            self.manifest, _StubModel(), tmp_plain, attempts_global_cap=1
        )
        seen: list[_ForwardingRecorder] = []

        def wrapper(probe):
          recorder = _ForwardingRecorder(probe)
          seen.append(recorder)
          return recorder

        wrapped = fp.run_batch(
            self.manifest,
            _StubModel(),
            tmp_wrapped,
            attempts_global_cap=1,
            model_wrapper=wrapper,
        )
        # The wrapper received the probe's two-layer model itself.
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0]._probe, fp.ProbeModel)  # pylint: disable=protected-access
        # Forwarded bind/take_run_log/take_run_events leave the books
        # bit-identical to the unwrapped run, mid-unit recovery
        # included (cap 1 stops inside the first unit's review).
        self.assertTrue(seen[0].bound)
        self.assertEqual(wrapped.ledger_counts, plain.ledger_counts)
        self.assertEqual(wrapped.sdk_counts, plain.sdk_counts)
        self.assertEqual(wrapped.execution_ledger, plain.execution_ledger)
        self.assertEqual(wrapped.stage_states, plain.stage_states)
        self.assertEqual(wrapped.outcomes, plain.outcomes)
        self.assertEqual(wrapped.unit_records, plain.unit_records)

  def test_outer_retry_bills_each_retry_as_attempt(self):
    class SentinelError(Exception):
      pass

    class _FlakyThenOk(language_model.LanguageModel):
      """Inner transport failing once, then succeeding."""

      def __init__(self):
        self.calls = 0

      def sample_text(self, prompt, **kwargs):
        self.calls += 1
        if self.calls == 1:
          raise SentinelError('transient transport failure')
        return 'ok'

      def sample_choice(self, prompt, responses, *, seed=None):
        return 0, responses[0], ''

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      ledger = rl.RequestLedger(
          out_dir,
          batch_id='fixed-probe',
          global_cap=75,
          model_caps={},
          group_caps={},
          sample_caps={},
      )
      guard = fp.SdkCreateGuard(out_dir, create_cap=225)
      probe = fp.ProbeModel(
          _FlakyThenOk(), ledger=ledger, sdk_guard=guard, out_dir=out_dir
      )
      engine = _no_backoff_retry(probe, (SentinelError,))
      probe.bind('C-1-before', 'request')
      result = engine.sample_text('Question: 判断\nAnswer: (')
      self.assertEqual(result, 'ok')
      # One logical call, two billed attempts: a retry inside the
      # wrapper cannot hide from the attempt ledger.
      self.assertEqual(ledger.counts['global'], 2)
      self.assertEqual(ledger.counts['by_group']['C-1-before'], 2)

  def test_request_identity_recorded_in_both_layers(self):

    def _started(path):
      return [
          event for event in rl.load_events(path) if event['event'] == 'started'
      ]

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      fp.run_batch(
          self.manifest,
          _StubModel(),
          out_dir,
          attempts_global_cap=1,
          model_tag='m4',
          model_name='gpt-6.1-sol',
      )
      for layer in (out_dir, out_dir / 'sdk_ledger'):
        started = _started(layer / 'requests.jsonl')
        self.assertTrue(started, layer)
        for event in started:
          self.assertEqual(event['model_tag'], 'm4', layer)
          self.assertEqual(event['model_name'], 'gpt-6.1-sol', layer)
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      fp.run_batch(self.manifest, _StubModel(), out_dir, attempts_global_cap=1)
      for layer in (out_dir, out_dir / 'sdk_ledger'):
        started = _started(layer / 'requests.jsonl')
        self.assertTrue(started, layer)
        for event in started:
          self.assertEqual(event['model_tag'], 'stub', layer)
          self.assertEqual(event['model_name'], 'stub', layer)

  def test_integrity_checked_before_every_attempt_and_create(self):
    # Counting run: exactly one check per attempt and per SDK create.
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      calls = []

      def counting_check():
        calls.append(1)

      report = fp.run_batch(
          self.manifest,
          _StubModel(),
          out_dir,
          attempts_global_cap=6,
          integrity_check=counting_check,
      )
      self.assertEqual(
          len(calls),
          report.ledger_counts['global'] + report.sdk_counts['global'],
      )
    # Scripted failure: the fifth check faults the batch mid-run.
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      calls = []

      def scripting_check():
        calls.append(1)
        if len(calls) >= 5:
          raise fp.BatchFault('scripted integrity failure')

      with self.assertRaises(fp.BatchFault):
        fp.run_batch(
            self.manifest,
            _StubModel(),
            out_dir,
            integrity_check=scripting_check,
        )
      attempts = [
          event
          for event in rl.load_events(out_dir / 'requests.jsonl')
          if event['event'] == 'started'
      ]
      creates = [
          event
          for event in rl.load_events(out_dir / 'sdk_ledger' / 'requests.jsonl')
          if event['event'] == 'started'
      ]
      # Four checks passed — two attempts with their two creates; the
      # fifth stopped the batch before any further slot was begun, and
      # nothing ran afterwards that could start one.
      self.assertEqual(len(calls), 5)
      self.assertEqual(len(attempts), 2)
      self.assertEqual(len(creates), 2)
      payload = json.loads(
          (out_dir / 'stage_states.json').read_text(encoding='utf-8')
      )
      assert payload['fault'] is not None
      self.assertEqual(payload['fault']['kind'], 'batch_fault')
      self.assertEqual(
          payload['stage_states']['B-1|before|r1|generation'], 'FINISHED'
      )
      self.assertEqual(
          payload['stage_states']['B-1|before|r1|review'], 'FINISHED'
      )
      self.assertEqual(
          payload['stage_states']['B-1|after|r1|generation'],
          'NOT_STARTED_ORDER',
      )
      ledger_lines = [
          json.loads(line)
          for line in (
              (out_dir / 'execution_ledger.jsonl')
              .read_text(encoding='utf-8')
              .splitlines()
          )
          if line.strip()
      ]
      # The completed unit is booked normally, the faulting position
      # as a recovered record; nothing else exists.
      self.assertEqual(
          [(line['unit'], line['condition']) for line in ledger_lines],
          [('B-1', 'before'), ('B-1', 'after')],
      )
      self.assertEqual(
          ledger_lines[0]['stages'],
          {'generation': 'FINISHED', 'review': 'FINISHED'},
      )
      self.assertEqual(
          ledger_lines[1]['stages'],
          {'generation': 'NOT_STARTED_ORDER', 'review': 'NOT_STARTED_ORDER'},
      )


class RetryClassificationTest(unittest.TestCase):
  """Task 3: classification by final attempt and actually failing stage."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_judgment_retry_success_then_suggestion_format_failure(self):
    class SentinelError(Exception):
      pass

    class _JudgmentFlakyThenYes(_StubModel):
      """Judgment attempt 1 faults transiently, attempt 2 answers Yes.

      Every suggestion call stays invalid (``InvalidResponseError`` is
      not in the outer retry set), so the repetition's actually failing
      stage is the suggestion — never the retried-and-won judgment.
      """

      def __init__(self):
        super().__init__()
        self.judgment_calls = 0

      def sample_choice(self, prompt, responses, *, seed=None):
        self.judgment_calls += 1
        if self.judgment_calls == 1:  # C-1 before rep 1, attempt 1
          raise SentinelError('transient judgment transport failure')
        self.judgment_yes = True
        return super().sample_choice(prompt, responses, seed=seed)

      def sample_text(self, prompt, **kwargs):
        if self._fingerprint(prompt) == 'suggestion':
          raise language_model.InvalidResponseError('no visible suggestion')
        return super().sample_text(prompt, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      with self.assertRaisesRegex(
          language_model.InvalidResponseError, 'no visible suggestion'
      ):
        fp.run_batch(
            self.manifest,
            _JudgmentFlakyThenYes(),
            out_dir,
            model_wrapper=lambda probe: _ForwardingRecorder(
                probe, engine=_no_backoff_retry(probe, (SentinelError,))
            ),
        )
      payload = json.loads(
          (out_dir / 'stage_states.json').read_text(encoding='utf-8')
      )
      states = payload['stage_states']
      outcomes = payload['outcomes']
      # The retried judgment succeeded and books by its final attempt.
      self.assertEqual(states['C-1|before|r1|judgment'], 'FINISHED')
      self.assertEqual(outcomes['C-1|before|r1|judgment'], 'yes')
      self.assertEqual(states['C-1|before|r1|suggestion'], 'FAILED')
      self.assertEqual(states['C-1|before|r1|fusion'], 'SKIPPED_UPSTREAM')
      # The failed first judgment attempt stays fully visible in the
      # attempt ledger and in the stage's booked slots.
      events = rl.load_events(out_dir / 'requests.jsonl')
      judgment_slots = [
          event['slot_id']
          for event in events
          if event['event'] == 'started' and event['stage'] == 'judgment'
      ]
      self.assertEqual(len(judgment_slots), 2)
      finished = {
          event['slot_id']: event
          for event in events
          if event['event'] == 'finished'
      }
      self.assertEqual(
          [finished[slot]['status'] for slot in judgment_slots],
          ['SentinelError', 'completed'],
      )
      ledger_lines = [
          json.loads(line)
          for line in (
              (out_dir / 'execution_ledger.jsonl')
              .read_text(encoding='utf-8')
              .splitlines()
          )
          if line.strip()
      ]
      entry = ledger_lines[-1]
      self.assertEqual(
          (entry['unit'], entry['condition'], entry['rep']),
          ('C-1', 'before', 1),
      )
      self.assertEqual(len(entry['slots']['judgment']), 2)

  def test_recovery_uses_final_slot_terminal_state(self):
    class _ChoiceFlakyThenYes(language_model.LanguageModel):
      """First choice call fails terminally; the second answers Yes."""

      def __init__(self):
        self.choice_calls = 0

      def sample_text(self, prompt, **kwargs):
        return 'ok'

      def sample_choice(self, prompt, responses, *, seed=None):
        self.choice_calls += 1
        if self.choice_calls == 1:
          raise RuntimeError('transient choice transport failure')
        match = re.search(r'^  \((\w+)\) Yes\s*$', prompt, re.MULTILINE)
        assert match is not None
        return responses.index(match.group(1)), match.group(1), ''

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      ledger = rl.RequestLedger(
          out_dir,
          batch_id='fixed-probe',
          global_cap=75,
          model_caps={},
          group_caps={},
          sample_caps={},
      )
      guard = fp.SdkCreateGuard(out_dir, create_cap=225)
      model = fp.ProbeModel(
          _ChoiceFlakyThenYes(), ledger=ledger, sdk_guard=guard, out_dir=out_dir
      )
      model.bind('C-1-before', 'judgment')
      prompt = 'Question: 判断\n  (a) Yes\n  (b) No\nAnswer: ('
      with self.assertRaises(RuntimeError):
        model.sample_choice(prompt, responses=['a', 'b'])
      model.sample_choice(prompt, responses=['a', 'b'])
      # A mid-batch stop: the stage verdict must follow the final
      # slot's terminal state, with the failed first slot still booked.
      record = fp._recover_unit_record(  # pylint: disable=protected-access
          self.manifest, {}, ('C-1', 'before', 1), model, budget_stop=False
      )
      judgment = record['stages']['judgment']
      self.assertEqual(judgment['state'], 'FINISHED')
      self.assertEqual(judgment['outcome'], 'yes')
      self.assertEqual(len(judgment['slots']), 2)


class FaultReportRebuildTest(unittest.TestCase):
  """Task 4: fault-path unit records and report rebuilding."""

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)

  def test_rebuild_after_fault_feeds_rubric_report(self):
    class ReviewFault(_StubModel):

      def sample_text(self, prompt, **kwargs):
        if self._fingerprint(prompt) == 'review':
          raise RuntimeError('offline review infrastructure fault')
        return super().sample_text(prompt, **kwargs)

    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      with self.assertRaisesRegex(RuntimeError, 'infrastructure fault'):
        fp.run_batch(self.manifest, ReviewFault(), out_dir)
      # The fault path persisted the unit records like any other.
      persisted = json.loads(
          (out_dir / 'unit_records.json').read_text(encoding='utf-8')
      )
      self.assertEqual(len(persisted), 1)
      report = fp.rebuild_batch_report(out_dir)
      assert report.fault is not None
      self.assertEqual(report.fault['kind'], 'batch_fault')
      self.assertIn('infrastructure fault', report.fault['error'])
      # Rows cover exactly the executed units; nothing is invented.
      executed = len(report.execution_ledger)
      self.assertEqual(executed, 1)
      self.assertEqual(len(report.unit_records), executed)
      self.assertEqual(report.unit_records, persisted)
      # Counts reconcile with both on-disk ledger layers.
      concordia = rl.rebuild_counts(rl.load_events(out_dir / 'requests.jsonl'))
      sdk = rl.rebuild_counts(
          rl.load_events(out_dir / 'sdk_ledger' / 'requests.jsonl')
      )
      self.assertEqual(report.ledger_counts['global'], concordia['global'])
      self.assertEqual(report.ledger_counts['by_group'], concordia['by_group'])
      self.assertEqual(report.sdk_counts['global'], sdk['global'])
      # The rebuilt report feeds the rubric skeleton unchanged.
      tables = fp.write_rubric_report(self.manifest, report, out_dir)
      self.assertEqual(len(tables['b_table']['rows']), executed)
      self.assertEqual(len(tables['c_table']['rows']), 0)


class _CurrentContractRecorder(language_model.LanguageModel):
  """Records every prompt and answers the current contract's calls.

  The generation call is answered with a two-field payload whose
  public track deliberately omits the private finding (the audience
  boundary), and the review is approved; both prompts are captured for
  the current-production assertions.
  """

  def __init__(self):
    self.calls: list[tuple[str, str]] = []

  def _kind(self, prompt: str) -> str:
    if '# Story resolution semantic review' in prompt:
      return 'review'
    if '# Candidate event' in prompt:
      return 'generation'
    raise AssertionError(f'unrouted current-contract prompt:\n{prompt[:200]}')

  def prompt_of(self, kind: str) -> str | None:
    for call_kind, prompt in self.calls:
      if call_kind == kind:
        return prompt
    return None

  def sample_text(self, prompt, **kwargs):
    del kwargs
    kind = self._kind(prompt)
    self.calls.append((kind, prompt))
    if kind == 'generation':
      return json.dumps(
          {
              'stage_directions': (
                  '周律师查阅原件，读到落款日期二〇一九年三月十二日；此次仅由他本人获知。'
              ),
              'public_stage_directions': '周律师查阅了原件，尚未报出日期。',
          },
          ensure_ascii=False,
      )
    return json.dumps({'valid': True, 'reason': '合法。'}, ensure_ascii=False)

  def sample_choice(self, prompt, responses, *, seed=None):
    del prompt, seed
    return 0, responses[0], ''


def _canonical_section_payload(review_prompt: str) -> dict[str, object]:
  """Parses the JSON payload under the canonical-event section mark."""
  mark = _story_resolution._CANONICAL_SECTION_MARK  # pylint: disable=protected-access
  index = review_prompt.find(mark)
  assert index >= 0, 'review prompt lacks the canonical section mark'
  remainder = review_prompt[index + len(mark) :]
  start = remainder.find('{')
  payload, _ = json.JSONDecoder().raw_decode(remainder[start:])
  return payload


class HistoricalReplayTest(unittest.TestCase):
  """Task 3: historical units replay original-contract fixtures.

  Every controlled prompt of the frozen order — the executed before
  prompts and the reconstructed after recordings — must match the
  executed batch's archived sha256 pins, with the unchanged
  ``expected_diff_audit`` returning zero findings on each pair.
  """

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)
    cls.fixture = fp.load_original_contract()

  def test_historical_units_replay_original_contract_fixtures(self):
    pins = self.fixture['replay']['controlled_prompt_sha256']
    with tempfile.TemporaryDirectory() as tmp:
      report = fp.run_batch(self.manifest, _StubModel(), pathlib.Path(tmp))
      self.assertIsNone(report.fault)
      positions = {
          (r['unit'], r['condition'], r['rep']): r['controlled_prompt']
          for r in report.unit_records
          if r.get('controlled_prompt')
      }
      for key, pinned in pins.items():
        unit, condition, rep_text = key.split('|')
        if unit.startswith('C-2R'):
          continue  # The C-2R arms are pinned by their own probe's test.
        rep = int(rep_text)
        prompt = positions[(unit, condition, rep)]
        self.assertEqual(
            fp._sha256_text(prompt),  # pylint: disable=protected-access
            pinned,
            f'{unit}-{condition} rep {rep} differs from the archive',
        )
      # The unchanged audits still pass on the replayed pairs: every
      # paired unit-rep carries zero findings, and B-1's before prompt
      # still equals the manifest's frozen production recording.
      self.assertEqual(len(report.audit_records), 12)
      self.assertTrue(all(not r['findings'] for r in report.audit_records))
      expected = self.manifest.fixtures['B-1']['generation_prompt']['value']
      for rep in (1, 2, 3):
        self.assertEqual(positions[('B-1', 'before', rep)], expected, rep)
        self.assertEqual(
            fp.expected_diff_audit(
                'B', expected, positions[('B-1', 'after', rep)]
            ),
            [],
            rep,
        )

  def test_historical_code_states_never_bind_new_tree_director(self):
    # Dependency-closure isolation: the frozen seeds must reference the
    # frozen original-contract director (two-argument resolution), not
    # the current tree's four-argument one, and the frozen sources'
    # hash pins must match the embedded bytes.
    with tempfile.TemporaryDirectory() as tmp:
      states = fp.load_code_states(
          self.manifest, pathlib.Path(tmp) / 'code_states'
      )
    frozen_dd = states['before']['drama_director']
    current_dd = importlib.import_module(
        'concordia.contrib.components.game_master.drama_director'
    )
    self.assertIsNot(frozen_dd, current_dd)
    for condition, state in states.items():
      story = state['story_resolution']
      self.assertIs(story.drama_director, frozen_dd, condition)
      self.assertIs(
          story.story_action_adjudication,
          states[condition]['story_action_adjudication'],
          condition,
      )
      self.assertIs(
          story.story_action_adjudication,
          states[condition]['story_action_adjudication'],
          condition,
      )
      code = frozen_dd.DramaDirector.set_validated_resolution.__code__
      self.assertEqual(code.co_argcount - 1, 2, condition)
      self.assertEqual(
          current_dd.DramaDirector.set_validated_resolution.__code__.co_argcount
          - 1,
          4,
      )

  def test_old_protocol_rejects_new_contract_source(self):
    # Handing the current (new-contract) source to the historical
    # harness as a code state must fail loudly at load time, naming the
    # incompatibility — with the manifest's own hash pin recomputed so
    # ONLY the contract check can fire (no audit is relaxed).
    repo_root = pathlib.Path(fp.__file__).parents[2]
    base = json.loads(_MANIFEST_PATH.read_text(encoding='utf-8'))
    for rel, feature in (
        (
            'concordia/contrib/components/game_master/story_resolution.py',
            'exactly two string fields',
        ),
        (
            'concordia/contrib/components/game_master/repetition_push.py',
            'GM_COMPLETE_RECORD_LABEL',
        ),
    ):
      with self.subTest(rel=rel):
        current = (repo_root / rel).read_text(encoding='utf-8')
        tampered = json.loads(json.dumps(base))
        tampered['code_states']['before']['sources'][rel] = current
        tampered['code_states']['before']['files'][rel] = hashlib.sha256(
            current.encode('utf-8')
        ).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
          manifest_path = pathlib.Path(tmp) / 'manifest.json'
          manifest_path.write_text(
              json.dumps(tampered, ensure_ascii=False), encoding='utf-8'
          )
          manifest = fp.load_manifest(manifest_path)
          with self.assertRaises(fp.NewContractSourceError) as ctx:
            fp.load_code_states(manifest, pathlib.Path(tmp) / 'code_states')
          self.assertIn(feature, str(ctx.exception))
          self.assertIn(rel, str(ctx.exception))


class CurrentSourceContractTest(unittest.TestCase):
  """Task 3: independent current-production contract tests.

  These verify the CURRENT source directly — the two-field output
  contract, the audience rules in both prompts, the four-key canonical
  review section and the stage accounting — and are deliberately NOT
  comparisons against the old B/C baselines.
  """

  @classmethod
  def setUpClass(cls):
    cls.manifest = fp.load_manifest(_MANIFEST_PATH)
    cls.gm = _fmp.gm_params()
    speech_fixtures = cls.manifest.fixtures['B-1']
    cls.player = speech_fixtures['active_player']
    cls.history = list(speech_fixtures['history']['value'])
    cls.speech = speech_fixtures['speech']['value']
    cls.action_intent = speech_fixtures['action_intent']['value']
    cls.action_result = speech_fixtures['action_result']['value']
    cls.candidate = speech_fixtures['candidate']['value']

  def _run_current_step(self, model):
    """Drives the CURRENT production final-output step once."""
    director, entity = fp._make_probe_director(  # pylint: disable=protected-access
        self.player, self.gm, self.history
    )
    _fmp._open_turn(  # pylint: disable=protected-access
        entity, self.player, self.speech, self.action_intent
    )
    director.record_action_result(self.action_result)
    step = _story_resolution.make_final_output_step(
        director,
        immutable_facts=self.gm['immutable_facts'],
        character_constraints=self.gm['character_constraints'],
        player_names=self.gm['player_names'],
        world_constraints=self.gm['world_constraints'],
        allowed_environment_events=self.gm['allowed_environment_events'],
        npc_response_rules=self.gm.get('npc_response_rules', ()),
        public_facts=self.gm['public_facts'],
        public_history=lambda: _fmp.public_history_text(  # pylint: disable=protected-access
            self.history
        ),
    )
    chain = interactive_document.InteractiveDocument(model)
    return step(chain, self.candidate, self.player)

  def test_current_source_dual_field_roundtrip(self):
    full = '周律师查阅原件，读到落款日期二〇一九年三月十二日；仅他本人获知。'
    public = '周律师查阅了原件，尚未报出日期。'
    payload = json.dumps(
        {'stage_directions': full, 'public_stage_directions': public},
        ensure_ascii=False,
    )
    parsed_full, parsed_public = (
        _story_resolution._parse_final_output(payload)  # pylint: disable=protected-access
    )
    self.assertEqual(parsed_full, full)
    self.assertEqual(parsed_public, public)
    # Roundtrip through the public canonical assembly rule.
    resolved = _story_resolution.assemble_canonical_event(
        active_player_name=self.player,
        original_speech=self.speech,
        stage_directions=parsed_full,
    )
    public_resolved = _story_resolution.assemble_canonical_event(
        active_player_name=self.player,
        original_speech=self.speech,
        stage_directions=parsed_public,
    )
    self.assertIn(self.speech, resolved)
    self.assertIn(full, resolved)
    self.assertIn(public, public_resolved)
    self.assertNotIn('三月十二日', public_resolved)
    # A fenced response parses; the old single-field contract is
    # rejected as a key-set error with no fallback branch.
    fenced = f'```json\n{payload}\n```'
    self.assertEqual(
        _story_resolution._parse_final_output(fenced),  # pylint: disable=protected-access
        (full, public),
    )
    with self.assertRaises(_story_resolution.StoryOutputError) as ctx:
      _story_resolution._parse_final_output(  # pylint: disable=protected-access
          json.dumps({'stage_directions': full}, ensure_ascii=False)
      )
    self.assertIn('public_stage_directions', str(ctx.exception))

  def test_current_source_prompts_carry_audience_rules(self):
    model = _CurrentContractRecorder()
    self._run_current_step(model)
    generation = model.prompt_of('generation')
    self.assertIsNotNone(generation)
    self.assertIn('## Audience rules (two views, one review)', generation)
    self.assertIn(
        'Output a JSON object with exactly two string fields', generation
    )
    self.assertIn(
        _story_action_adjudication.GM_HISTORY_SECTION_MARK, generation
    )
    self.assertIn(
        _story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK, generation
    )
    self.assertIn('## Committed public events (oldest first)', generation)

  def test_current_source_review_mirror_isomorphic(self):
    model = _CurrentContractRecorder()
    self._run_current_step(model)
    review = model.prompt_of('review')
    self.assertIsNotNone(review)
    # The review side is not weaker than production: the audience
    # rules, the shared authorization semantics and both histories
    # reach the reviewer, and the canonical data section carries all
    # four keys (both assembled events and both narrator tracks).
    self.assertIn('## Audience rules (two views, one review)', review)
    self.assertIn('# Story resolution semantic review', review)
    self.assertIn(_story_action_adjudication.GM_HISTORY_SECTION_MARK, review)
    canonical = _canonical_section_payload(review)
    self.assertEqual(
        set(canonical),
        {
            'resolved_event',
            'stage_directions',
            'public_resolved_event',
            'public_stage_directions',
        },
    )
    self.assertIn('三月十二日', canonical['stage_directions'])
    self.assertNotIn('三月十二日', canonical['public_stage_directions'])

  def test_current_source_stage_accounting(self):
    with tempfile.TemporaryDirectory() as tmp:
      out_dir = pathlib.Path(tmp)
      ledger = rl.RequestLedger(
          out_dir,
          batch_id='current-contract-test',
          global_cap=8,
          model_caps={},
          group_caps={},
          sample_caps={},
      )
      guard = fp.SdkCreateGuard(out_dir, create_cap=8)
      probe_model = fp.ProbeModel(
          _CurrentContractRecorder(),
          ledger=ledger,
          sdk_guard=guard,
          out_dir=out_dir,
      )
      probe_model.bind('CURRENT', 'request')
      resolved = self._run_current_step(probe_model)
      self.assertTrue(resolved)
      run_log = probe_model.take_run_log()
      stages = [entry['stage'] for entry in run_log]
      # Generation exactly once, review exactly once; the new
      # two-field fingerprint attributes the generation stage.
      self.assertEqual(stages, ['generation', 'review'])
      generation_prompt = run_log[0]['prompt']
      self.assertIn(
          'Output a JSON object with exactly two string fields',
          generation_prompt,
      )
      self.assertNotIn(
          'Output a JSON object with exactly one string field',
          generation_prompt,
      )
      self.assertEqual(
          fp.ProbeModel._classify_stage(  # pylint: disable=protected-access
              'sample_text', generation_prompt
          ),
          'generation',
      )
