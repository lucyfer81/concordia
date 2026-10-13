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

"""Integration tests of the story enactment runner.

The runner is exercised end to end through ``run.run`` with the semantic
``PromptStubModel`` and a fixed embedder, so no real API is contacted. The
seven mandated scenario groups of the stage-1 plan are covered:

1. end-to-end short runs with distinct termination reasons;
2. checkpoint resume continuation, budget deduction and rejections;
3. interruption at each of the three save-boundary points;
4. turn-0 recovery from the initial checkpoint;
5. budget path A (exhaustion before the resolution is committed);
6. budget path B (exhaustion at the completion-judgment position);
7. API retry exhaustion at the completion judgment recorded as error.

Model-call counts are deterministic in the sequential engine: one story
turn costs three perception questions plus the speech exercise plus the
final resolution (5 text attempts), plus one choice attempt whenever the
beat-completion question is asked. Fixtures position budget exhaustion by
choosing the total attempt limit relative to these counts.
"""

import contextlib
import json
import os
import tempfile
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.language_models import budget_wrapper
from concordia.prefabs.simulation import generic as simulation_lib
import numpy as np

from examples.story_enactment import collector
from examples.story_enactment import renderer
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs

# The public conflict-start marker of the appendix-C example story.
_CONFLICT_MARK = '周律师当众启封遗嘱并宣读'

# Distinctive substrings of hidden knowledge (isolation carry-over).
_GM_TRUTH_MARK = '调包'


class SimulatedInterruption(BaseException):
  """A crash stand-in that the runner must let propagate untouched."""


def _embedder(_: str) -> np.ndarray:
  return np.ones(16, dtype=np.float32)


def _beat(beat_id: str, *, min_turns: int = 2, max_turns: int = 8):
  """Returns a minimal valid beat for run-config fixtures."""
  return drama_director.Beat(
      id=beat_id,
      description=f'节拍 {beat_id}：出现可供角色质疑的新情况',
      tension_range=(3, 6),
      done_question=f'节拍 {beat_id} 的完成条件是否已经出现？',
      min_turns=min_turns,
      max_turns=max_turns,
  )


def _fixture_config(
    *,
    total_turn_limit: int = 4,
    total_model_attempt_limit: int = 100,
    beats=None,
    completion_answers=None,
    scene_rounds: int | None = None,
    prompt_stub: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Builds a run config backed by the stub model with fixture overrides.

  Args:
    total_turn_limit: the original total story-turn limit.
    total_model_attempt_limit: the original total model-attempt limit.
    beats: fixture beats replacing the example outline.
    completion_answers: beat-id keyed yes/no answers of the stub.
    scene_rounds: override of the scene schedule budget.
    prompt_stub: extra PromptStubModel parameters.

  Returns:
    The run-config dictionary for ``run.run``.
  """
  story = story_config.make_example_config()
  stub_params = dict(prompt_stub or {})
  if completion_answers is not None:
    stub_params['completion_answers'] = dict(completion_answers)
  config = run_lib.make_run_config(
      story,
      total_turn_limit=total_turn_limit,
      total_model_attempt_limit=total_model_attempt_limit,
      model_config={'api_type': 'stub', 'prompt_stub': stub_params},
  )
  if beats is not None:
    config['beat_dicts'] = [beat.to_dict() for beat in beats]
  if scene_rounds is not None:
    config['scene_dicts'][0]['num_rounds'] = scene_rounds
  return config


class _StateDirTestCase(unittest.TestCase):
  """Base case providing a per-test state directory and file readers."""

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.state_dir = self._tmp.name

  def turn_log(self) -> collector.StoryTurnLog:
    return collector.StoryTurnLog(
        os.path.join(self.state_dir, 'story_turns.jsonl')
    )

  def envelope(self) -> dict[str, Any]:
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def summary(self) -> dict[str, Any]:
    with open(
        os.path.join(self.state_dir, 'summary.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def read_script(self, filename: str) -> str:
    with open(
        os.path.join(self.state_dir, filename), encoding='utf-8'
    ) as handle:
      return handle.read()

  def restored(
      self, run_config: dict[str, Any]
  ) -> tuple[simulation_lib.Simulation, drama_director.DramaDirector]:
    """Rebuilds the simulation from the saved checkpoint envelope."""
    sim, director, _ = run_lib.build_simulation(
        run_config=run_config,
        model=testing_stubs.PromptStubModel(),
        embedder=_embedder,
    )
    sim.load_from_checkpoint(self.envelope()['simulation_state'])
    return sim, director


@contextlib.contextmanager
def _interrupt_when(turn_id: int, *, when: str):
  """Patches the collector so a save-boundary point raises a crash.

  Args:
    turn_id: the global turn id at whose save the interruption fires.
    when: either ``'before_write'`` (before the turn record is saved)
      or ``'after_commit'`` (after the checkpoint envelope is committed).
  """
  real = collector.make_step_callback

  def patched(**kwargs):
    inner = real(**kwargs)
    director = kwargs['drama_director']

    def callback(step_data):
      turn = director.get_last_story_turn()
      if when == 'before_write' and turn and turn['turn_id'] == turn_id:
        raise SimulatedInterruption(f'before write of turn {turn_id}')
      inner(step_data)
      if when == 'after_commit' and turn and turn['turn_id'] == turn_id:
        raise SimulatedInterruption(f'after commit of turn {turn_id}')

    return callback

  with mock.patch.object(collector, 'make_step_callback', patched):
    yield


@contextlib.contextmanager
def _interrupt_before_checkpoint(turn_id: int):
  """Patches the collector to crash after the turn write, before commit."""
  real = collector._save_checkpoint  # pylint: disable=protected-access

  def patched(**kwargs):
    if kwargs['committed_turn_id'] == turn_id:
      raise SimulatedInterruption(f'before checkpoint of turn {turn_id}')
    return real(**kwargs)

  with mock.patch.object(collector, '_save_checkpoint', patched):
    yield


@contextlib.contextmanager
def _capture_play():
  """Captures the keyword arguments of every ``Simulation.play`` call."""
  calls = []
  real = simulation_lib.Simulation.play

  def patched(self, **kwargs):
    calls.append(kwargs)
    return real(self, **kwargs)

  with mock.patch.object(simulation_lib.Simulation, 'play', patched):
    yield calls


def _gm_memory_text(sim: simulation_lib.Simulation) -> str:
  memory = sim.game_masters[0].get_component('__memory__')
  return '\n'.join(memory.get_all_memories_as_text())


def _entity_memory_text(sim: simulation_lib.Simulation, name: str) -> str:
  entities = {entity.name: entity for entity in sim.entities}
  memory = entities[name].get_component('__memory__')
  return '\n'.join(memory.get_all_memories_as_text())


class MakeRunConfigTest(unittest.TestCase):
  """The run config records the full story and keeps secrets isolated."""

  def setUp(self):
    super().setUp()
    self.story = story_config.make_example_config()

  def test_records_story_scenes_beats_params_and_limits(self):
    config = run_lib.make_run_config(
        self.story,
        total_turn_limit=7,
        total_model_attempt_limit=51,
        model_config={'api_type': 'gemini', 'model_name': 'm'},
    )
    self.assertEqual(config['schema_version'], run_lib.RUN_CONFIG_SCHEMA)
    self.assertEqual(config['mode'], 'beats_only')
    self.assertEqual(config['total_turn_limit'], 7)
    self.assertEqual(config['total_model_attempt_limit'], 51)
    self.assertEqual(config['story']['title'], self.story.title)
    self.assertEqual(len(config['scene_dicts']), 1)
    self.assertEqual(config['scene_dicts'][0]['num_rounds'], 7)
    self.assertEqual(
        [b['id'] for b in config['beat_dicts']],
        [b.id for b in run_lib.make_example_beats()],
    )
    self.assertEqual(
        [p['name'] for p in config['entity_params']],
        [c.name for c in self.story.characters],
    )
    self.assertEqual(
        config['gm_params']['immutable_facts'],
        list(self.story.immutable_facts),
    )
    # The API key never enters the persisted config.
    self.assertNotIn('api_key', json.dumps(config))

  def test_rejects_unknown_mode(self):
    with self.assertRaises(ValueError):
      run_lib.make_run_config(self.story, mode='unknown_mode')

  def test_scene_premises_carry_public_background_only(self):
    config = run_lib.make_run_config(self.story)
    premise = config['scene_dicts'][0]['premise']
    for name, lines in premise.items():
      joined = '\n'.join(lines)
      self.assertIn(self.story.conflict_start, joined)
      for fact in self.story.world_facts:
        self.assertIn(fact, joined)
      self.assertNotIn(_GM_TRUTH_MARK, joined)
      self.assertNotIn('火漆印', joined)
    # The public premise shown to the GM also excludes GM-only truth.
    public = run_lib.make_public_premise(config['story'])
    self.assertIn(self.story.conflict_start, public)
    self.assertNotIn(_GM_TRUTH_MARK, public)

  def test_gm_world_constraints_carry_hidden_truth_and_catalog(self):
    config = run_lib.make_run_config(self.story)
    constraints = '\n'.join(config['gm_params']['world_constraints'])
    self.assertIn(_GM_TRUTH_MARK, constraints)
    self.assertIn('火漆印', constraints)
    self.assertIn('仅裁决者可知', constraints)
    self.assertIn('；'.join(self.story.allowed_environment_events), constraints)


class BuildSimulationTest(unittest.TestCase):
  """The wrapper chain rejects broad or budget-catching retry whitelists."""

  def test_rejects_broad_or_budget_retry_whitelists(self):
    story = story_config.make_example_config()
    config = run_lib.make_run_config(story)
    whitelists: list[Any] = [
        (Exception,),
        (BaseException,),
        (budget_wrapper.BudgetExhaustedError,),
    ]
    for whitelist in whitelists:
      with self.subTest(whitelist=whitelist):
        with self.assertRaises(ValueError):
          run_lib.build_simulation(
              run_config=config,
              model=testing_stubs.PromptStubModel(),
              embedder=_embedder,
              retryable_exceptions=whitelist,
          )

  def test_builds_one_story_gm_and_returns_director_and_guard(self):
    story = story_config.make_example_config()
    config = run_lib.make_run_config(story)
    sim, director, guard = run_lib.build_simulation(
        run_config=config,
        model=testing_stubs.PromptStubModel(),
        embedder=_embedder,
        used_attempts=3,
    )
    self.assertEqual(len(sim.game_masters), 1)
    self.assertEqual(guard.used_attempts, 3)
    self.assertEqual(guard.total_attempt_limit, 400)
    self.assertEqual(director.get_state()['story_turn'], 0)


class PromptStubRoutingTest(unittest.TestCase):
  """The stub routes on the last question block, semantically."""

  def test_text_routes_on_last_question_block(self):
    stub = testing_stubs.PromptStubModel(dialogue_lines={'林雪': '我有话说。'})
    earlier_speech = (
        'Question: 根据你已知的信息，王婶 现在说什么？只输出对白。\nAnswer: x\n'
    )
    perception_last = (
        earlier_speech + 'Question: What kind of person is 王婶?\nAnswer: '
    )
    self.assertEqual(
        stub.sample_text(perception_last), '一个被卷入遗产争执、各怀心事的人。'
    )
    speech_last = (
        'Question: What kind of situation is 林雪 in right now?\n'
        'Answer: x\n'
        'Exercise: 根据你已知的信息，林雪 现在说什么？只输出对白。\n'
        'Answer: '
    )
    self.assertEqual(stub.sample_text(speech_last), '我有话说。')

  def test_dual_track_route_takes_precedence_and_preserves_speech(self):
    stub = testing_stubs.PromptStubModel()
    prompt = (
        'Question: What would a person like 林雪 do in a situation like '
        'this?\nAnswer: x\n'
        'Question: # Candidate event\n'
        '## Current speaker\n'
        '林雪\n'
        '## Original speech (verbatim)\n'
        '遗嘱的封印从未核验。\n'
        '## Output\n'
        'Output a JSON object with exactly two string fields:\n'
        '- "resolved_event"\n- "stage_directions"\n'
        'Answer: '
    )
    payload = json.loads(stub.sample_text(prompt))
    self.assertEqual(payload['resolved_event'], '林雪: 遗嘱的封印从未核验。')
    self.assertEqual(payload['stage_directions'], '窗外的雷声滚过，烛火摇曳。')

  def test_choice_maps_by_beat_completion_answer_with_shuffled_letters(
      self,
  ):
    stub = testing_stubs.PromptStubModel(completion_answers={'b2': 'Yes'})
    prompt = (
        'Recent events of the story, in order:\n'
        '- 林雪: 提出质疑\n'
        'Current beat "b2": 疑点发酵\n'
        'Question: 节拍 b2 的完成条件是否已经出现？\n'
        '  (A) No\n'
        '  (B) Yes\n'
        'Answer: ('
    )
    self.assertEqual(stub.sample_choice(prompt, ['A', 'B']), (1, 'B', {}))
    swapped = prompt.replace('(A) No\n  (B) Yes', '(A) Yes\n  (B) No')
    self.assertEqual(stub.sample_choice(swapped, ['A', 'B']), (0, 'A', {}))

  def test_raw_yes_text_answer_does_not_flip_completion_choice(self):
    stub = testing_stubs.PromptStubModel(
        default_line='Yes', completion_answers={'b1': 'No'}
    )
    prompt = 'Question: 什么情况？\nAnswer: '
    self.assertEqual(stub.sample_text(prompt), 'Yes')
    completion = (
        'Current beat "b1": 开场\n'
        'Question: 节拍 b1 的完成条件是否已经出现？\n'
        '  (A) Yes\n'
        '  (B) No\n'
        'Answer: ('
    )
    self.assertEqual(stub.sample_choice(completion, ['A', 'B']), (1, 'B', {}))

  def test_fail_choice_calls_from_raises_transient_api_error(self):
    stub = testing_stubs.PromptStubModel(fail_choice_calls_from=1)
    prompt = 'Question: 任意选择题\n  (A) Yes\n  (B) No\nAnswer: ('
    self.assertEqual(stub.sample_choice(prompt, ['A', 'B']), (1, 'B', {}))
    with self.assertRaises(testing_stubs.TransientAPIError):
      stub.sample_choice(prompt, ['A', 'B'])


class EndToEndShortRunTest(_StateDirTestCase):
  """Scenario 1: short runs end with distinct, well-defined reasons."""

  def test_beats_completed_exactly_at_step_four(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'beats_completed')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    self.assertEqual(self.envelope()['committed_turn_id'], 4)
    self.assertEqual(self.envelope()['total_turn_limit'], 4)
    self.assertEqual(
        [(r['beat_id_at_action'], r['beat_id_after_event']) for r in records],
        [('b1', 'b1'), ('b1', 'b2'), ('b2', 'b2'), ('b2', None)],
    )
    self.assertEqual(self.summary()['reason'], 'beats_completed')
    self.assertIsNone(result.token_usage)
    elements = renderer.parse_fountain(self.read_script('script.fountain'))
    characters = [c for kind, c in elements if kind == 'character']
    self.assertEqual(characters, ['林雪', '陈默', '周律师', '王婶'])
    dialogues = [c for kind, c in elements if kind == 'dialogue']
    self.assertEqual(dialogues, ['这宅子的事，急不得。'] * 4)
    headings = [c for kind, c in elements if kind == 'scene_heading']
    self.assertEqual(len(headings), 1)
    self.assertTrue(headings[0].startswith('INT.'))

  def test_step_limit_reason_when_beats_never_complete(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=8,
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 4)
    self.assertEqual(len(self.turn_log().load()), 4)
    self.assertEqual(self.turn_log().load()[-1]['beat_id_after_event'], 'b1')

  def test_scene_limit_reason_when_scene_budget_exhausts_first(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=2,
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'scene_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(len(self.turn_log().load()), 2)

  def test_stage2_modes_raise_not_implemented(self):
    config = _fixture_config(total_turn_limit=2)
    config['mode'] = 'director'
    with self.assertRaises(NotImplementedError):
      run_lib.run(self.state_dir, run_config=config)

  def test_first_run_over_existing_checkpoint_refused(self):
    config = _fixture_config(total_turn_limit=2)
    run_lib.run(self.state_dir, run_config=config)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, run_config=config)


class ResumeContinuationTest(_StateDirTestCase):
  """Scenario 2: resume continues global numbering and deducts budgets."""

  def _interrupt_after_turn_two(self, config):
    with _interrupt_when(2, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)

  def test_resume_continues_global_turn_numbering(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )
    self._interrupt_after_turn_two(config)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1, 2])
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    self.assertEqual(self.envelope()['used_model_attempts'], 11)
    with _capture_play() as plays:
      result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'beats_completed')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    self.assertEqual([r['actor'] for r in records[2:]], ['周律师', '王婶'])
    self.assertEqual(self.envelope()['total_turn_limit'], 4)
    self.assertEqual(self.envelope()['used_model_attempts'], 22)
    # The resumed process spent exactly its own share of the budget.
    self.assertEqual(result.attempts_used_this_process, 11)
    # Resume plays with an explicit empty premise and remaining turns.
    self.assertEqual(plays[-1]['premise'], '')
    self.assertEqual(plays[-1]['max_steps'], 2)
    # The public premise was never re-injected into the game master.
    sim_after, _ = self.restored(config)
    gm_text = _gm_memory_text(sim_after)
    self.assertEqual(
        len([
            line
            for line in gm_text.splitlines()
            if line.startswith(f'[observation] [event] {_CONFLICT_MARK}')
        ]),
        1,
    )

  def test_resume_rejects_mismatched_limits_and_mode(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )
    self._interrupt_after_turn_two(config)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, resume=True, total_turn_limit=5)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, resume=True, total_model_attempt_limit=99)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, resume=True, mode='director')

  def test_resume_stops_when_no_model_attempts_remain(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=5,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    with _interrupt_when(1, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(self.envelope()['used_model_attempts'], 5)
    result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    self.assertEqual(result.attempts_used_this_process, 0)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)


class SaveInterruptionPointsTest(_StateDirTestCase):
  """Scenario 3: each save-boundary interruption recovers losslessly."""

  def _make_config(self) -> dict[str, Any]:
    return _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )

  def _finish_and_verify(self, config):
    result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'beats_completed')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    elements = renderer.parse_fountain(self.read_script('script.fountain'))
    self.assertEqual(
        [c for kind, c in elements if kind == 'character'],
        ['林雪', '陈默', '周律师', '王婶'],
    )

  def test_interrupted_before_turn_write(self):
    config = self._make_config()
    with _interrupt_when(2, when='before_write'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    # The old files remain readable and consistent at turn 1.
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self._finish_and_verify(config)

  def test_interrupted_after_turn_write_before_checkpoint(self):
    config = self._make_config()
    with _interrupt_before_checkpoint(2):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    # Turn 2 is on disk but uncommitted; the checkpoint stays at 1.
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1, 2])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self._finish_and_verify(config)

  def test_interrupted_after_checkpoint_commit(self):
    config = self._make_config()
    with _interrupt_when(2, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1, 2])
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    self._finish_and_verify(config)


class TurnZeroRecoveryTest(_StateDirTestCase):
  """Scenario 4: recovery from the initial checkpoint re-runs step one."""

  def test_restore_from_initial_checkpoint_preserves_state_and_runs(self):
    config = _fixture_config(
        total_turn_limit=2,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=4,
    )
    with mock.patch.object(
        simulation_lib.Simulation, 'play', side_effect=SimulatedInterruption
    ):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    envelope = self.envelope()
    self.assertEqual(envelope['committed_turn_id'], 0)
    self.assertEqual(self.turn_log().load(), [])

    # The saved snapshot equals the first-run initial state: the game
    # master has observed the public premise, characters hold nothing.
    expected_sim, _, _ = run_lib.build_simulation(
        run_config=config,
        model=testing_stubs.PromptStubModel(),
        embedder=_embedder,
    )
    expected_sim.game_masters[0].observe(
        '[event] ' + run_lib.make_public_premise(config['story'])
    )
    restored_sim, _ = self.restored(config)
    self.assertEqual(
        _gm_memory_text(restored_sim), _gm_memory_text(expected_sim)
    )
    for name in ('林雪', '陈默'):
      self.assertEqual(
          _entity_memory_text(restored_sim, name),
          _entity_memory_text(expected_sim, name),
      )

    result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1, 2])
    # Background is injected exactly once across the interrupted lifetime:
    # once in the game master's own premise observation, plus one echo per
    # participant when the scene tracker queues the premises.
    sim_after, _ = self.restored(config)
    self.assertEqual(_gm_memory_text(sim_after).count(_CONFLICT_MARK), 6)


class BudgetPathATest(_StateDirTestCase):
  """Scenario 5: exhaustion before the step commits keeps the last commit."""

  def test_exhaustion_on_character_request_step_two(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=5,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    self.assertIsNotNone(result.budget_error)
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 5)
    self.assertEqual(self.summary()['reason'], 'budget_limit')
    elements = renderer.parse_fountain(self.read_script('script.fountain'))
    self.assertEqual(
        [c for kind, c in elements if kind == 'character'], ['林雪']
    )

  def test_exhaustion_on_final_resolution_request_step_two(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=9,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self.assertEqual(result.used_model_attempts, 9)

  def test_resume_after_budget_exhausted_summary_refused(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=5,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    run_lib.run(self.state_dir, run_config=config)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, resume=True)


class BudgetPathBTest(_StateDirTestCase):
  """Scenario 6: exhaustion at the judgment still commits the step once."""

  def test_exhaustion_at_completion_judgment_still_saves_step_once(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=10,
        beats=(_beat('b1', max_turns=8),),
        completion_answers={},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    self.assertTrue(records[1]['budget_stopped'])
    self.assertIsNone(records[1]['tension'])
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    # No further model calls happen after the stop, and the committed
    # director state carries the stop flag with a cleared turn pipeline.
    self.assertEqual(self.envelope()['used_model_attempts'], 10)
    self.assertEqual(result.used_model_attempts, 10)
    _, director = self.restored(config)
    state = director.get_state()
    self.assertEqual(state['story_turn'], 2)
    self.assertTrue(state['budget_stopped'])
    self.assertFalse(director._awaiting_event)  # pylint: disable=protected-access
    self.assertIsNone(director._pending_commit)  # pylint: disable=protected-access

  def test_max_turns_timeout_wins_reason_priority_with_budget_diagnostic(
      self,
  ):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=10,
        beats=(_beat('b1', min_turns=2, max_turns=2),),
        completion_answers={},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'beat_timeout')
    self.assertTrue(result.budget_exhausted)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    self.assertEqual(records[1]['beat_id_after_event'], 'b1')
    self.assertTrue(records[1]['budget_stopped'])


class ApiErrorTest(_StateDirTestCase):
  """Scenario 7: completion-judgment API retry exhaustion is an error."""

  def test_completion_api_retry_exhaustion_records_error(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=100,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={'b1': 'Yes'},
        prompt_stub={'fail_choice_calls_from': 0},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'error')
    self.assertFalse(result.budget_exhausted)
    self.assertIsNotNone(result.error_message)
    # The failed turn is not committed: it must not degrade to a
    # "not completed" judgment, which would still commit the turn.
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.summary()['reason'], 'error')
    # All three retry attempts were spent and counted.
    self.assertEqual(result.used_model_attempts, 13)
    self.assertEqual(result.attempts_used_this_process, 13)
    # The committed checkpoint still reflects the last full turn only.
    self.assertEqual(self.envelope()['used_model_attempts'], 5)


if __name__ == '__main__':
  unittest.main()
