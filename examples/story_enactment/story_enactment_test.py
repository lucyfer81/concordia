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

The task-9 action suites cover the adjudicated chain end to end:
`ActionPipelineIntegrationTest` (core results flow to observation and
exports), `ActionBoundaryIntegrationTest` (agency boundaries on real
prompts) and `ActionChainBudgetTest` (both budget paths of the
speech-and-action chain).

Model-call counts are deterministic in the sequential engine: one story
turn costs three perception questions plus the speech exercise plus the
final resolution and its semantic review (6 text attempts), plus one choice
attempt whenever the
beat-completion question is asked. Fixtures position budget exhaustion by
choosing the total attempt limit relative to these counts.
"""

import contextlib
import copy
import dataclasses
import json
import os
import tempfile
import time
import types
from typing import Any, cast
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_action
from concordia.contrib.language_models import budget_wrapper
from concordia.prefabs.simulation import generic as simulation_lib
import numpy as np

from examples.story_enactment import acceptance
from examples.story_enactment import collector
from examples.story_enactment import renderer
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs

# The public conflict-start marker of the appendix-C example story.
_CONFLICT_MARK = '周律师当众启封遗嘱并宣读'

# Distinctive substrings of hidden knowledge (isolation carry-over).
_GM_TRUTH_MARK = '调包'

# Beat fingerprints and foreign markers of the three acceptance
# scenarios: each scenario's first beat must appear in its own prompts,
# and no other scenario's beat vocabulary may leak in (acceptance F1).
_SCENARIOS = (
    (
        acceptance.make_scenario_one_config,
        acceptance.make_scenario_one_beats,
        '宣读遗嘱后的第一轮争执',
        ('面试', '简历', '婚礼', '伴娘'),
    ),
    (
        acceptance.make_scenario_two_config,
        acceptance.make_scenario_two_beats,
        '终面开场后的第一轮交锋',
        ('遗嘱', '旧信', '录音残片', '纸灰', '婚礼', '伴娘'),
    ),
    (
        acceptance.make_scenario_three_config,
        acceptance.make_scenario_three_beats,
        '祝酒辞被打断后的第一轮试探',
        ('遗嘱', '旧信', '录音残片', '纸灰', '面试', '简历'),
    ),
)


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
    action_mode: str = 'speech_only',
) -> dict[str, Any]:
  """Builds a run config backed by the stub model with fixture overrides.

  Args:
    total_turn_limit: the original total story-turn limit.
    total_model_attempt_limit: the original total model-attempt limit.
    beats: fixture beats replacing the example outline.
    completion_answers: beat-id keyed yes/no answers of the stub.
    scene_rounds: override of the scene schedule budget.
    prompt_stub: extra PromptStubModel parameters.
    action_mode: the action contract recorded in the config.

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
      action_mode=action_mode,
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
    sim, director, _, _ = run_lib.build_simulation(
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

  def test_character_depth_wires_appendix_c_emotion_and_relationships(self):
    config = run_lib.make_run_config(self.story, mode='character_depth')
    names = [character.name for character in self.story.characters]
    for name, params in zip(names, config['entity_params']):
      self.assertEqual(
          params['emotion_options'],
          ['平静', '焦虑', '愤怒', '悲伤', '希望', '羞愧'],
      )
      self.assertEqual(params['relationships_refresh_interval'], 3)
      # Each character perceives exactly the other public participants.
      self.assertEqual(
          params['other_player_names'], [n for n in names if n != name]
      )

  def test_other_modes_gain_no_character_depth(self):
    # Mode switches stay independent: only character_depth wires the
    # emotion and relationship components into the entity params.
    for mode in ('beats_only', 'tension_observe', 'director'):
      with self.subTest(mode=mode):
        config = run_lib.make_run_config(self.story, mode=mode)
        for params in config['entity_params']:
          self.assertEqual(params.get('emotion_options', ()), ())
          self.assertEqual(params.get('relationships_refresh_interval', 0), 0)

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
    self.assertEqual(
        config['gm_params']['player_names'],
        [character.name for character in self.story.characters],
    )
    self.assertEqual(
        config['gm_params']['character_constraints'],
        {c.name: c.hard_constraint for c in self.story.characters},
    )
    for character in self.story.characters:
      self.assertIn(
          f'{character.name}：{character.hard_constraint}', constraints
      )

  def test_review_initial_public_facts_exclude_hidden_and_future_information(
      self,
  ):
    config = run_lib.make_run_config(self.story)
    facts = config['gm_params']['public_facts']
    self.assertIn(self.story.conflict_start, facts)
    for fact in self.story.world_facts:
      self.assertIn(fact, facts)
    joined = '\n'.join(facts)
    for character in self.story.characters:
      for fact in character.public_facts:
        self.assertIn(f'{character.name}：{fact}', joined)
      if character.secret:
        self.assertNotIn(character.secret, joined)
    for fact in (*self.story.gm_only_facts, *self.story.allowed_endings):
      self.assertNotIn(fact, joined)

  def test_beats_travel_explicitly_and_default_keeps_estate_outline(self):
    custom = (_beat('x1', min_turns=1, max_turns=2),)
    config = run_lib.make_run_config(self.story, beats=custom)
    self.assertEqual(config['beat_dicts'], [beat.to_dict() for beat in custom])
    default_config = run_lib.make_run_config(self.story)
    self.assertEqual(
        default_config['beat_dicts'],
        [beat.to_dict() for beat in run_lib.make_example_beats()],
    )

  def test_allowed_endings_wire_into_gm_context_but_not_premise(self):
    # Acceptance F3: the ending range conditions the game master's
    # resolution context and the derived closing beat, but never the
    # public premise or scene premises, so characters receive no ending
    # guidance.
    config = run_lib.make_run_config(self.story)
    constraints = '\n'.join(config['gm_params']['world_constraints'])
    self.assertIn('剧情结局只能收束为以下方向之一', constraints)
    for ending in self.story.allowed_endings:
      self.assertIn(ending, constraints)
    public = run_lib.make_public_premise(config['story'])
    for ending in self.story.allowed_endings:
      self.assertNotIn(ending, public)
    for lines in config['scene_dicts'][0]['premise'].values():
      for ending in self.story.allowed_endings:
        self.assertNotIn(ending, '\n'.join(lines))
    # The derived closing beat carries the same range in its description
    # and completion question.
    final = drama_director.Beat.from_dict(config['beat_dicts'][-1])
    for ending in self.story.allowed_endings:
      self.assertIn(ending, final.description)
      self.assertIn(ending, final.done_question)
    self.assertIn('仅按已发生的事件判断', final.done_question)


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
    sim, director, guard, _ = run_lib.build_simulation(
        run_config=config,
        model=testing_stubs.PromptStubModel(),
        embedder=_embedder,
        used_attempts=3,
    )
    self.assertEqual(len(sim.game_masters), 1)
    self.assertEqual(guard.used_attempts, 3)
    self.assertEqual(guard.total_attempt_limit, 400)
    self.assertEqual(director.get_state()['story_turn'], 0)


class AcceptanceScenariosTest(unittest.TestCase):
  """Acceptance F1: each scenario runs on its own matching beat outline."""

  def test_scenario_beats_match_their_story_and_round_trip(self):
    for make_config, make_beats, _, _ in _SCENARIOS:
      with self.subTest(story=make_config().title):
        story = make_config()
        beats = make_beats()
        self.assertEqual(
            [beat.id for beat in beats],
            ['b1', 'b2', 'b3', 'b4'],
        )
        for beat in beats:
          self.assertTrue(beat.description)
          self.assertTrue(beat.done_question)
          self.assertLessEqual(0, beat.min_turns)
          self.assertLessEqual(beat.min_turns, beat.max_turns)
          self.assertLess(0, beat.max_turns)
          self.assertEqual(drama_director.Beat.from_dict(beat.to_dict()), beat)
        # The closing beat derives from this scenario's endings.
        final = beats[-1]
        for ending in story.allowed_endings:
          self.assertIn(ending, final.description)
          self.assertIn(ending, final.done_question)
        # The run config records exactly the passed outline.
        config = run_lib.make_run_config(story, beats=beats)
        self.assertEqual(
            config['beat_dicts'], [beat.to_dict() for beat in beats]
        )

  def test_scenario_beats_do_not_share_story_vocabulary(self):
    outlines = [make_beats() for _, make_beats, _, _ in _SCENARIOS]
    for index, (_, _, _, foreign) in enumerate(_SCENARIOS):
      with self.subTest(scenario=index):
        joined = '\n'.join(
            f'{beat.description}\n{beat.done_question}'
            for beat in outlines[index]
        )
        for marker in foreign:
          self.assertNotIn(marker, joined)

  def test_first_beat_prompts_carry_scenario_beats_only(self):
    # Drive each scenario's opening with the deterministic stub and
    # capture the real resolution and completion-judgment prompts: they
    # must be conditioned on this scenario's own first beat and must not
    # contain any other scenario's beat requirements.
    for make_config, make_beats, fingerprint, foreign in _SCENARIOS:
      with self.subTest(story=make_config().title):
        config = run_lib.make_run_config(
            make_config(),
            beats=make_beats(),
            total_turn_limit=3,
        )
        stub = testing_stubs.PromptStubModel()
        sim, _, _, _ = run_lib.build_simulation(
            run_config=config, model=stub, embedder=_embedder
        )
        # Three turns reach the first beat's min_turns, so the real
        # completion-judgment question is asked once (answered "No").
        sim.play(premise='', max_steps=3)
        prompts = '\n'.join(stub.text_prompts + stub.choice_prompts)
        self.assertIn(fingerprint, prompts)
        self.assertTrue(stub.choice_prompts)
        self.assertIn(make_beats()[0].done_question, prompts)
        for marker in foreign:
          self.assertNotIn(marker, prompts)


class _EndingAwareStub(testing_stubs.PromptStubModel):
  """Scripts closing choices from event markers to test prompt wiring.

  The closing beat's completion question lists the story's allowed
  endings and asks whether a specific allowed ending has happened.
  This stub scripts that judgment deterministically: it answers
  "Yes" only when one of the given markers appears among the recent
  events of the prompt (never merely because the endings are listed in
  the question itself). It does not implement semantic judgment and cannot
  establish that the production model judges endings correctly.
  """

  def __init__(self, happened_markers: list[str]):
    super().__init__()
    self._happened_markers = tuple(happened_markers)

  def _wanted_option(self, prompt: str) -> str:
    if '允许结局之一' in prompt:
      events = prompt.split('## Recent events (oldest first)', 1)
      window = (
          events[1].split('## Completion criteria', 1)[0]
          if len(events) == 2
          else ''
      )
      if any(marker in window for marker in self._happened_markers):
        return 'Yes'
      return 'No'
    return super()._wanted_option(prompt)


class AllowedEndingsWiringTest(unittest.TestCase):
  """Acceptance F3: the ending range reaches the prompts that close a run."""

  _MARKER_ENDING = (
      'ENDING_CONSTRAINT_PROBE_7309: 林雪本人明确同意暂缓出售老宅。'
  )

  def _marker_config(self) -> dict[str, Any]:
    story = dataclasses.replace(
        story_config.make_example_config(),
        allowed_endings=(self._MARKER_ENDING,),
    )
    return run_lib.make_run_config(
        story,
        beats=(run_lib.make_final_beat(story.allowed_endings, beat_id='b1'),),
        total_turn_limit=2,
    )

  def test_marker_endings_reach_resolution_and_closing_judgment_prompts(self):
    config = self._marker_config()
    stub = testing_stubs.PromptStubModel()
    sim, _, _, _ = run_lib.build_simulation(
        run_config=config, model=stub, embedder=_embedder
    )
    sim.play(premise='', max_steps=2)
    text_prompts = '\n'.join(stub.text_prompts)
    choice_prompts = '\n'.join(stub.choice_prompts)
    # The game master's resolution context (world constraints) and the
    # closing beat's judgment question both follow the configured range.
    self.assertIn('ENDING_CONSTRAINT_PROBE_7309', text_prompts)
    self.assertIn('ENDING_CONSTRAINT_PROBE_7309', choice_prompts)

  def test_no_allowed_ending_happened_fixture_stays_unfinished(self):
    config = self._marker_config()
    stub = _EndingAwareStub(happened_markers=['我同意暂缓出售老宅'])
    sim, director, _, _ = run_lib.build_simulation(
        run_config=config, model=stub, embedder=_embedder
    )
    sim.play(premise='', max_steps=2)
    state = director.get_state()
    self.assertEqual(state['beat_index'], 0)
    self.assertFalse(director.is_beats_done())
    turn = director.get_last_story_turn()
    assert turn is not None
    self.assertEqual(turn['beat_id_after_event'], 'b1')

  def test_happened_allowed_ending_completes_the_closing_beat(self):
    # Positive control of the judgment stub: once an event actually
    # carries the allowed ending, the closing beat completes.
    config = self._marker_config()
    stub = _EndingAwareStub(happened_markers=['我同意暂缓出售老宅'])
    stub._dialogue_lines = {  # pylint: disable=protected-access
        '林雪': '我同意暂缓出售老宅。'
    }
    sim, director, _, _ = run_lib.build_simulation(
        run_config=config, model=stub, embedder=_embedder
    )
    sim.play(premise='', max_steps=2)
    self.assertTrue(director.is_beats_done())
    turn = director.get_last_story_turn()
    assert turn is not None
    self.assertIsNone(turn['beat_id_after_event'])

  def test_closing_prompt_requires_fulfilled_conditions_and_player_consent(
      self,
  ):
    final = run_lib.make_final_beat(('各方同意保宅，并已不欢而散。',))
    self.assertIn('全部具体条件', final.done_question)
    self.assertIn('计划离开', final.done_question)
    self.assertIn('自己的明确发言', final.done_question)
    third = run_lib.make_example_beats()[2]
    self.assertIn('未拆阅的信件', third.done_question)
    self.assertIn('指控与核验请求本身都不算事实揭示', third.done_question)

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
    self.assertEqual(
        payload['resolved_event'],
        '林雪: 遗嘱的封印从未核验。\n' + payload['stage_directions'],
    )
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

  def test_character_depth_mode_records_and_renders_emotion(self):
    config = run_lib.make_run_config(
        story_config.make_example_config(),
        total_turn_limit=2,
        total_model_attempt_limit=100,
        mode='character_depth',
        model_config={'api_type': 'stub'},
    )
    config['scene_dicts'][0]['num_rounds'] = 8
    # The emotion multiple choice falls back to the stub's first parsed
    # option (the palette is shuffled by the interactive document), so the
    # assertion is membership in the appendix-C palette, not one label.
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    palette = {'平静', '焦虑', '愤怒', '悲伤', '希望', '羞愧'}
    for record in records:
      self.assertIn(record['emotion'], palette)
    # The screenplay export shows the acting character's emotion as a
    # parenthetical cue, mirrored by the plain-text speaker line.
    elements = renderer.parse_fountain(self.read_script('script.fountain'))
    self.assertEqual(
        [c for kind, c in elements if kind == 'parenthetical'],
        [record['emotion'] for record in records],
    )
    plain = self.read_script('script.txt')
    for record in records:
      self.assertIn(f'{record["actor"]}（{record["emotion"]}）：', plain)
    self.assertEqual(self.summary()['mode'], 'character_depth')

  def test_director_mode_runs_with_feedback_configuration(self):
    story = story_config.make_example_config()
    config = run_lib.make_run_config(
        story,
        total_turn_limit=2,
        total_model_attempt_limit=100,
        mode='director',
        model_config={'api_type': 'stub'},
    )
    config['scene_dicts'][0]['num_rounds'] = 8
    params = config['director_params']
    self.assertTrue(params['feedback_enabled'])
    self.assertEqual(params['cooldown_turns'], 3)
    self.assertEqual(
        params['allowed_environment_events'],
        list(story.allowed_environment_events),
    )
    self.assertEqual(params['immutable_facts'], list(story.immutable_facts))
    # The stub's scoring answers never parse as integers, so the run
    # commits every step without generating directives.
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(self.summary()['mode'], 'director')

  def test_tension_observe_mode_records_scores_without_feedback(self):
    config = run_lib.make_run_config(
        story_config.make_example_config(),
        total_turn_limit=2,
        total_model_attempt_limit=100,
        mode='tension_observe',
        model_config={'api_type': 'stub'},
    )
    config['scene_dicts'][0]['num_rounds'] = 8
    self.assertFalse(config['director_params']['feedback_enabled'])
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)

  def test_first_run_over_existing_checkpoint_refused(self):
    config = _fixture_config(total_turn_limit=2)
    run_lib.run(self.state_dir, run_config=config)
    with self.assertRaises(ValueError):
      run_lib.run(self.state_dir, run_config=config)


class FdxExportTest(_StateDirTestCase):
  """The run exports committed dialogue literally as typed FDX paragraphs."""

  def test_run_preserves_literal_dialogue_in_fdx(self):
    speech = '(我的原话) & <标签> \\不是转义。'
    config = _fixture_config(
        total_turn_limit=2,
        prompt_stub={'default_line': speech},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.committed_turn_id, 2)
    root = ET.fromstring(self.read_script('script.fdx'))
    dialogues = root.findall("./Content/Paragraph[@Type='Dialogue']/Text")
    self.assertEqual([node.text for node in dialogues], [speech, speech])

  def test_uncommitted_tail_is_excluded_from_fdx(self):
    config = _fixture_config(total_turn_limit=2)
    result = run_lib.run(self.state_dir, run_config=config)
    records = self.turn_log().load()
    self.turn_log().append({
        **records[-1],
        'turn_id': 3,
        'speech': 'UNCOMMITTED_TAIL_SENTINEL',
    })
    run_lib._write_outputs(  # pylint: disable=protected-access
        self.state_dir,
        result=result,
        run_config=self.envelope()['run_config'],
        turn_log=self.turn_log(),
        committed_turn_id=self.envelope()['committed_turn_id'],
    )
    text = self.read_script('script.fdx')
    self.assertNotIn('UNCOMMITTED_TAIL_SENTINEL', text)
    root = ET.fromstring(text)
    self.assertEqual(
        len(root.findall("./Content/Paragraph[@Type='Dialogue']")), 2
    )


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
    self.assertEqual(self.envelope()['used_model_attempts'], 13)
    with _capture_play() as plays:
      result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'beats_completed')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    self.assertEqual([r['actor'] for r in records[2:]], ['周律师', '王婶'])
    self.assertEqual(self.envelope()['total_turn_limit'], 4)
    self.assertEqual(self.envelope()['used_model_attempts'], 26)
    # The resumed process spent exactly its own share of the budget.
    self.assertEqual(result.attempts_used_this_process, 13)
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

  def test_resume_merges_api_key_and_rejects_mismatched_identity(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )
    self._interrupt_after_turn_two(config)
    # A live credential arrives with the checkpoint's own public
    # identity; the effective model config must keep the identity and
    # gain only the key, since the checkpoint never stores secrets.
    with mock.patch.object(
        run_lib, 'make_language_model', wraps=run_lib.make_language_model
    ) as maker:
      result = run_lib.run(
          self.state_dir,
          resume=True,
          model_config={
              'api_type': 'stub',
              'model_name': 'prompt_stub',
              'prompt_stub': config['model_config']['prompt_stub'],
              'api_key': 'live-secret',
          },
      )
    self.assertEqual(result.reason, 'beats_completed')
    effective = maker.call_args_list[0].args[0]
    self.assertEqual(effective['api_key'], 'live-secret')
    self.assertEqual(effective['api_type'], 'stub')
    self.assertEqual(
        effective['prompt_stub'], config['model_config']['prompt_stub']
    )
    # The credential is per-invocation: it reaches the model but never
    # the persisted checkpoint or the rewritten summary.
    self.assertNotIn('live-secret', json.dumps(self.envelope()))
    self.assertNotIn('live-secret', json.dumps(self.summary()))
    self.assertNotIn('api_key', self.summary()['model_config'])
    # An identity contradicting the checkpoint is rejected, whether the
    # provider or just the model name differs.
    with self.assertRaises(ValueError):
      run_lib.run(
          self.state_dir,
          resume=True,
          model_config={'api_type': 'gemini', 'model_name': 'm'},
      )
    with self.assertRaises(ValueError):
      run_lib.run(
          self.state_dir,
          resume=True,
          model_config={
              'api_type': 'stub',
              'model_name': 'other-model',
              'prompt_stub': config['model_config']['prompt_stub'],
          },
      )

  def test_resume_stops_when_no_model_attempts_remain(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=6,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    with _interrupt_when(1, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(self.envelope()['used_model_attempts'], 6)
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
    expected_sim, _, _, _ = run_lib.build_simulation(
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
        total_model_attempt_limit=6,
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
    self.assertEqual(self.envelope()['used_model_attempts'], 6)
    self.assertEqual(self.summary()['reason'], 'budget_limit')
    elements = renderer.parse_fountain(self.read_script('script.fountain'))
    self.assertEqual(
        [c for kind, c in elements if kind == 'character'], ['林雪']
    )

  def test_exhaustion_on_final_resolution_request_step_two(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=10,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self.assertEqual(result.used_model_attempts, 10)

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

  def test_exhaustion_at_semantic_review_keeps_previous_full_checkpoint(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=11,
        beats=(_beat('b1', max_turns=99),),
    )
    stub = testing_stubs.PromptStubModel()
    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(stub, ())
    ):
      result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertEqual(result.used_model_attempts, 11)
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 6)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    reviews = [
        p
        for p in stub.text_prompts
        if '# Story resolution semantic review' in p
    ]
    # The second review is blocked before reaching the provider.
    self.assertEqual(len(reviews), 1)


class SemanticReviewFailureTest(_StateDirTestCase):
  """Scripted reviewer decisions verify rejection plumbing, not semantics."""

  def _run_rejected(
      self,
      response: str,
      *,
      stage_only: bool = False,
      pending_directive: bool = False,
  ):
    marker = 'REJECTED_EVENT_DO_NOT_PUBLISH'
    stage = (
        '陈母承认烧毁新遗嘱，周律师公开私藏录音，众人同意林雪继承并签字。'
        + marker
    )

    class RejectedCandidate(testing_stubs.PromptStubModel):

      def _dual_track_output(self, prompt):
        result = super()._dual_track_output(prompt)
        if stage_only:
          payload = json.loads(result)
          payload['resolved_event'] = payload['resolved_event'].split('\n', 1)[
              0
          ]
          return json.dumps(payload, ensure_ascii=False)
        return result

    stub = RejectedCandidate(stage_directions=stage, review_response=response)
    config = _fixture_config()
    built = []
    real_build = run_lib.build_simulation

    def capture(**kwargs):
      result = real_build(**kwargs)
      if pending_directive:
        director = result[1]
        state = dict(director.get_state())
        state['directive'] = {
            'beat_id': 'b1',
            'generated_at_turn': 0,
            'content': '雷雨骤紧，老宅骤然断电，堂内只剩烛光',
            'status': 'pending',
            'consumed_at_turn': None,
        }
        director.set_state(state)
      built.append(result)
      return result

    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(stub, ())
    ), mock.patch.object(run_lib, 'build_simulation', capture):
      result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'error')
    self.assertEqual(result.committed_turn_id, 0)
    self.assertEqual(self.turn_log().load(), [])
    self.assertEqual(self.envelope()['committed_turn_id'], 0)
    self.assertEqual(self.envelope()['used_model_attempts'], 0)
    sim, director, _, _ = built[0]
    self.assertEqual(director.get_state()['story_turn'], 0)
    if pending_directive:
      directive = director.get_state()['directive']
      assert isinstance(directive, dict)
      self.assertEqual(directive['status'], 'integrating')
      self.assertIsNone(director.get_state()['last_directive_executed_turn'])
    for character in config['entity_params']:
      self.assertNotIn(marker, _entity_memory_text(sim, character['name']))
    self.assertNotIn(marker, self.read_script('script.fdx'))
    self.assertEqual(
        sum(
            '# Story resolution semantic review' in p for p in stub.text_prompts
        ),
        1,
    )

  def test_reported_agency_and_secrecy_violation_is_not_broadcast_or_committed(
      self,
  ):
    self._run_rejected(
        '{"valid": false, "reason": "Invented confession, disclosure and'
        ' consent"}'
    )

  def test_malformed_review_is_not_broadcast_or_committed(self):
    self._run_rejected('invalid JSON')

  def test_stage_only_signature_rejection_is_not_broadcast_or_committed(self):
    self._run_rejected(
        '{"valid": false, "reason": "Signature exists in stage directions'
        ' only"}',
        stage_only=True,
    )

  def test_rejected_resolution_does_not_consume_director_instruction(self):
    self._run_rejected(
        '{"valid": false, "reason": "Invented collective agreement"}',
        pending_directive=True,
    )

  def test_review_api_failure_retries_are_counted_without_commit(self):
    class FailingReview(testing_stubs.PromptStubModel):

      def _route_text(self, prompt):
        if '# Story resolution semantic review' in prompt:
          raise testing_stubs.TransientAPIError('review API unavailable')
        return super()._route_text(prompt)

    stub = FailingReview()
    config = _fixture_config()
    with mock.patch.object(
        run_lib,
        'make_language_model',
        return_value=(stub, (testing_stubs.TransientAPIError,)),
    ):
      result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'error')
    self.assertEqual(result.committed_turn_id, 0)
    self.assertEqual(result.used_model_attempts, 8)
    self.assertEqual(result.used_api_retries, 2)
    self.assertEqual(self.turn_log().load(), [])
    self.assertEqual(self.envelope()['used_model_attempts'], 0)
    self.assertIn('TransientAPIError', result.error_message or '')


class BudgetPathBTest(_StateDirTestCase):
  """Scenario 6: exhaustion at the judgment still commits the step once."""

  def test_exhaustion_at_completion_judgment_still_saves_step_once(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=12,
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
    self.assertEqual(self.envelope()['used_model_attempts'], 12)
    self.assertEqual(result.used_model_attempts, 12)
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
        total_model_attempt_limit=12,
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
    self.assertEqual(result.used_model_attempts, 15)
    self.assertEqual(result.attempts_used_this_process, 15)
    # The exhausted choice call made three provider attempts, so it
    # initiated exactly two API retries (acceptance F5); the committed
    # checkpoint's envelope carries the retry count of turn 1 (zero).
    self.assertEqual(result.used_api_retries, 2)
    self.assertEqual(result.api_retries_used_this_process, 2)
    self.assertEqual(self.envelope()['used_api_retries'], 0)
    self.assertEqual(self.summary()['used_api_retries'], 2)
    # The committed checkpoint still reflects the last full turn only.
    self.assertEqual(self.envelope()['used_model_attempts'], 6)


class _FlakyChoiceStub(testing_stubs.PromptStubModel):
  """Fails the first choice calls with a transient error, then recovers."""

  def __init__(self, failures: int):
    super().__init__()
    self._remaining_failures = failures

  def sample_choice(self, prompt, responses, *, seed=None):
    if self._remaining_failures > 0:
      self._remaining_failures -= 1
      raise testing_stubs.TransientAPIError('injected transient failure')
    return super().sample_choice(prompt, responses, seed=seed)


class RetryAccountingTest(_StateDirTestCase):
  """Acceptance F5: retry counts are recorded and resume-cumulative."""

  def test_clean_run_records_zero_retries(self):
    config = _fixture_config(
        total_turn_limit=2,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
    )
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.used_api_retries, 0)
    self.assertEqual(result.api_retries_used_this_process, 0)
    self.assertEqual(result.used_scoring_parse_retries, 0)
    self.assertEqual(result.scoring_parse_retries_used_this_process, 0)
    self.assertEqual(self.envelope()['used_api_retries'], 0)

  def test_scoring_parse_reasks_counted_separately_from_api_retries(self):
    # The stub never answers the tension question with an integer, so
    # every scoring step re-asks once: a parse retry, not an API retry.
    config = _fixture_config(
        total_turn_limit=2,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
    )
    config['mode'] = 'tension_observe'
    config['director_params'] = {
        'eval_interval': 1,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': False,
    }
    result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.used_scoring_parse_retries, 2)
    self.assertEqual(result.scoring_parse_retries_used_this_process, 2)
    self.assertEqual(result.used_api_retries, 0)
    self.assertEqual(result.api_retries_used_this_process, 0)

  def test_retry_counts_accumulate_across_resume(self):
    config = _fixture_config(
        total_turn_limit=4,
        total_model_attempt_limit=100,
        beats=(_beat('b1', min_turns=2, max_turns=99),),
        completion_answers={'b1': 'Yes'},
        scene_rounds=99,
    )
    stub = _FlakyChoiceStub(failures=1)
    patched_model = mock.patch.object(
        run_lib,
        'make_language_model',
        return_value=(stub, (testing_stubs.TransientAPIError,)),
    )
    with patched_model:
      with _interrupt_when(2, when='after_commit'):
        with self.assertRaises(SimulatedInterruption):
          run_lib.run(self.state_dir, run_config=config)
    first = self.envelope()
    # Turn 2's completion choice failed once transiently and then
    # succeeded: one API retry is committed with turn 2's checkpoint.
    self.assertEqual(first['used_api_retries'], 1)
    self.assertEqual(first['used_model_attempts'], 14)

    result = run_lib.run(self.state_dir, resume=True)
    # The story already finished its beats at turn 2; the summary keeps
    # the restored retry baseline and adds none of its own.
    self.assertEqual(result.reason, 'beats_completed')
    self.assertEqual(result.used_api_retries, 1)
    self.assertEqual(result.api_retries_used_this_process, 0)
    self.assertEqual(self.summary()['used_api_retries'], 1)
    self.assertEqual(self.summary()['api_retries_used_this_process'], 0)


class ModelIdentityArchivalTest(_StateDirTestCase):
  """Recheck R1: archives record the model actually in effect."""

  _LIVE_CONFIG = {
      'api_type': 'openai',
      'model_name': 'live-model-probe',
      'api_key': 'live-secret',
  }
  _LIVE_IDENTITY = {'api_type': 'openai', 'model_name': 'live-model-probe'}

  @contextlib.contextmanager
  def _live_provider(
      self,
      calls: list[dict[str, Any]] | None = None,
      prompt_stub: dict[str, Any] | None = None,
  ):
    """Runs the real simulation path with a stand-in live provider.

    The provider factory is replaced by the deterministic stub and the
    embedder by a fixed function, so the live-model call pattern of the
    README runs without contacting any API.

    Args:
      calls: optional sink recording the effective model config of
        every ``make_language_model`` call.
      prompt_stub: optional stub parameters (e.g. the fixture's
        completion answers) the stand-in keeps applying, standing in
        for the judgments the live model would make.
    """
    stub_params = dict(prompt_stub or {})

    def maker(model_config, *, disable_language_model=False):
      if calls is not None:
        calls.append(dict(model_config))
      return testing_stubs.PromptStubModel(**stub_params), ()

    with mock.patch.object(
        run_lib, 'make_language_model', maker
    ), mock.patch.object(run_lib, '_make_embedder', return_value=_embedder):
      yield

  def test_readme_combo_archives_the_live_provider_identity(self):
    # The README's standard call: a run config built without a model
    # config, plus the live model passed to run() itself. The checkpoint
    # and summary must archive the live provider, not the default stub.
    config = _fixture_config(
        total_turn_limit=2,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    with self._live_provider():
      result = run_lib.run(
          self.state_dir, run_config=config, model_config=self._LIVE_CONFIG
      )
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(
        self.envelope()['run_config']['model_config'], self._LIVE_IDENTITY
    )
    self.assertEqual(self.summary()['model_config'], self._LIVE_IDENTITY)
    # The credential reaches the provider but never the archives.
    self.assertNotIn('api_key', json.dumps(self.envelope()))
    self.assertNotIn('live-secret', json.dumps(self.envelope()))
    self.assertNotIn('live-secret', json.dumps(self.summary()))

  def test_resume_merges_new_credentials_of_the_same_identity(self):
    config = _fixture_config(
        total_turn_limit=4,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )
    # The stand-in keeps the fixture's completion answers so the beats
    # conclude exactly as they would under the scripted stub.
    stub_params = config['model_config']['prompt_stub']
    with self._live_provider(prompt_stub=stub_params):
      with _interrupt_when(2, when='after_commit'):
        with self.assertRaises(SimulatedInterruption):
          run_lib.run(
              self.state_dir, run_config=config, model_config=self._LIVE_CONFIG
          )
    self.assertEqual(
        self.envelope()['run_config']['model_config'], self._LIVE_IDENTITY
    )
    calls: list[dict[str, Any]] = []
    with self._live_provider(calls, prompt_stub=stub_params):
      result = run_lib.run(
          self.state_dir,
          resume=True,
          model_config={**self._LIVE_IDENTITY, 'api_key': 'rotated-secret'},
      )
    self.assertEqual(result.reason, 'beats_completed')
    self.assertEqual(result.committed_turn_id, 4)
    self.assertEqual(calls[-1]['api_type'], 'openai')
    self.assertEqual(calls[-1]['api_key'], 'rotated-secret')
    self.assertNotIn('rotated-secret', json.dumps(self.envelope()))
    # A contradicting identity is refused on resume.
    with self.assertRaises(ValueError):
      run_lib.run(
          self.state_dir,
          resume=True,
          model_config={'api_type': 'gemini', 'model_name': 'm'},
      )

  def test_disable_language_model_archives_the_stub_actually_used(self):
    config = _fixture_config(
        total_turn_limit=1,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
    )
    result = run_lib.run(
        self.state_dir,
        run_config=config,
        model_config=self._LIVE_CONFIG,
        disable_language_model=True,
    )
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(
        self.envelope()['run_config']['model_config'],
        {'api_type': 'stub', 'model_name': 'prompt_stub'},
    )
    self.assertEqual(
        self.summary()['model_config'],
        {'api_type': 'stub', 'model_name': 'prompt_stub'},
    )


class ScoringParseRetryProcessMetricTest(_StateDirTestCase):
  """Recheck R4: per-process parse-retry metrics exclude the history."""

  @staticmethod
  def _tension_config(total_turn_limit: int) -> dict[str, Any]:
    config = _fixture_config(
        total_turn_limit=total_turn_limit,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
    )
    config['mode'] = 'tension_observe'
    config['director_params'] = {
        'eval_interval': 1,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': False,
    }
    return config

  def _run_until_interrupted_after(self, turn_id: int, config) -> None:
    with _interrupt_when(turn_id, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(self.state_dir, run_config=config)

  def test_resume_without_model_calls_counts_no_parse_retries(self):
    config = self._tension_config(total_turn_limit=1)
    self._run_until_interrupted_after(1, config)
    # Turn 1's scoring answer never parses as an integer: exactly one
    # historical scoring re-ask is committed with the checkpoint.
    _, director = self.restored(config)
    self.assertEqual(director.get_state()['scoring_parse_retries'], 1)
    # The turn budget is exhausted at resume, so the resumed process
    # never calls the model and must not re-count the historical re-ask.
    result = run_lib.run(self.state_dir, resume=True, mode='tension_observe')
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(result.attempts_used_this_process, 0)
    self.assertEqual(result.used_scoring_parse_retries, 1)
    self.assertEqual(result.scoring_parse_retries_used_this_process, 0)

  def test_resume_counts_only_newly_happened_parse_retries(self):
    config = self._tension_config(total_turn_limit=2)
    self._run_until_interrupted_after(1, config)
    result = run_lib.run(self.state_dir, resume=True, mode='tension_observe')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(result.used_scoring_parse_retries, 2)
    self.assertEqual(result.scoring_parse_retries_used_this_process, 1)


class _DelayedStub(testing_stubs.PromptStubModel):
  """Delays every text call so component hooks genuinely overlap."""

  DELAY_SECONDS = 0.02

  def sample_text(self, prompt, **kwargs):
    time.sleep(self.DELAY_SECONDS)
    return super().sample_text(prompt, **kwargs)


class ConcurrentCharacterDepthRetryTest(_StateDirTestCase):
  """Recheck R3: the parallel character-depth path reports zero retries."""

  def test_parallel_character_depth_run_without_failures_reports_zero(self):
    # EntityAgent executes component pre_act hooks concurrently, and
    # character_depth gives every character independent model-calling
    # components that share the one guard-and-retry chain. With delayed
    # calls and no injected failure, overlapping logical requests must
    # not be miscounted as retries of one another.
    config = run_lib.make_run_config(
        story_config.make_example_config(),
        total_turn_limit=2,
        total_model_attempt_limit=100,
        mode='character_depth',
        model_config={'api_type': 'stub'},
    )
    config['scene_dicts'][0]['num_rounds'] = 8
    stub = _DelayedStub()
    with mock.patch.object(
        run_lib,
        'make_language_model',
        return_value=(stub, (testing_stubs.TransientAPIError,)),
    ):
      result = run_lib.run(self.state_dir, run_config=config)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertGreater(result.used_model_attempts, 0)
    self.assertEqual(result.used_api_retries, 0)
    self.assertEqual(result.api_retries_used_this_process, 0)


class ActionModeRunTest(_StateDirTestCase):
  """The action-mode entry points of the runner (spec sections 8-9).

  The action contract is independent of the stage switches: a first run
  without a run config derives it from the explicit parameter (defaulting
  to pure speech), a supplied or checkpointed run config is authoritative
  for it, and a resume never switches contracts mid-story — an explicit
  contradicting mode is rejected before any model request.
  """

  def _dir(self, name: str) -> str:
    return os.path.join(self._tmp.name, name)

  def _envelope_of(self, directory: str) -> dict[str, Any]:
    with open(
        os.path.join(directory, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def _json_fixture(self, *, total_turn_limit: int = 4) -> dict[str, Any]:
    return _fixture_config(
        total_turn_limit=total_turn_limit,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
        action_mode='speech_and_action',
    )

  def _speech_fixture(self, *, total_turn_limit: int = 4) -> dict[str, Any]:
    return _fixture_config(
        total_turn_limit=total_turn_limit,
        beats=(_beat('b1'), _beat('b2')),
        completion_answers={'b1': 'Yes', 'b2': 'Yes'},
    )

  def _commit_after_turn_two(self, directory: str, config) -> None:
    with _interrupt_when(2, when='after_commit'):
      with self.assertRaises(SimulatedInterruption):
        run_lib.run(directory, run_config=config)
    self.assertEqual(self._envelope_of(directory)['committed_turn_id'], 2)

  def test_first_run_defaults_to_speech_only(self):
    result = run_lib.run(self.state_dir, total_turn_limit=2)
    self.assertEqual(result.committed_turn_id, 2)
    envelope = self.envelope()
    self.assertEqual(envelope['run_config']['schema_version'], 2)
    self.assertEqual(envelope['run_config']['action_mode'], 'speech_only')
    self.assertEqual(
        envelope['run_config']['gm_params']['action_mode'], 'speech_only'
    )
    spec = envelope['run_config']['scene_dicts'][0]['scene_type']['action_spec']
    self.assertIn('现在说什么', spec['call_to_action'])
    self.assertNotIn('现在说什么、做什么', spec['call_to_action'])
    self.assertEqual(self.summary()['action_mode'], 'speech_only')

  def test_first_run_records_json_action_mode(self):
    result = run_lib.run(
        self.state_dir, total_turn_limit=2, action_mode='speech_and_action'
    )
    self.assertEqual(result.committed_turn_id, 2)
    envelope = self.envelope()
    self.assertEqual(envelope['run_config']['schema_version'], 2)
    self.assertEqual(envelope['run_config']['action_mode'], 'speech_and_action')
    self.assertEqual(
        envelope['run_config']['gm_params']['action_mode'],
        'speech_and_action',
    )
    spec = envelope['run_config']['scene_dicts'][0]['scene_type']['action_spec']
    self.assertIn('现在说什么、做什么', spec['call_to_action'])
    self.assertEqual(self.summary()['action_mode'], 'speech_and_action')

  def test_supplied_config_is_authoritative_and_not_mutated(self):
    json_config = self._json_fixture(total_turn_limit=2)
    json_config['scene_dicts'][0]['num_rounds'] = 8
    json_original = copy.deepcopy(json_config)
    # The action-mode parameter is ignored when a run config is given,
    # exactly like the limit and mode parameters: the supplied mode
    # must not be overridden by the function's defaults.
    run_lib.run(
        self._dir('json-authoritative'),
        run_config=json_config,
        action_mode='speech_only',
    )
    self.assertEqual(
        self._envelope_of(self._dir('json-authoritative'))['run_config'][
            'action_mode'
        ],
        'speech_and_action',
    )
    self.assertEqual(json_config, json_original)

    speech_config = self._speech_fixture(total_turn_limit=2)
    speech_original = copy.deepcopy(speech_config)
    run_lib.run(
        self._dir('speech-authoritative'),
        run_config=speech_config,
        action_mode='speech_and_action',
    )
    self.assertEqual(
        self._envelope_of(self._dir('speech-authoritative'))['run_config'][
            'action_mode'
        ],
        'speech_only',
    )
    self.assertEqual(speech_config, speech_original)

    # A schema-1 config carries no action_mode: the default is filled
    # on the copy only, and the source record is never relabeled.
    schema1_config = {
        key: value
        for key, value in json_original.items()
        if key != 'action_mode'
    }
    schema1_config['schema_version'] = 1
    schema1_config['gm_params'] = {
        key: value
        for key, value in json_original['gm_params'].items()
        if key != 'action_mode'
    }
    schema1_original = copy.deepcopy(schema1_config)
    run_lib.run(self._dir('schema1-authoritative'), run_config=schema1_config)
    schema1_envelope = self._envelope_of(self._dir('schema1-authoritative'))
    self.assertEqual(
        schema1_envelope['run_config']['action_mode'], 'speech_only'
    )
    self.assertEqual(schema1_envelope['run_config']['schema_version'], 1)
    self.assertEqual(schema1_config, schema1_original)
    self.assertNotIn('action_mode', schema1_config)

  def test_resume_omitted_action_mode_keeps_saved_json_mode(self):
    config = self._json_fixture()
    self._commit_after_turn_two(self.state_dir, config)
    self.assertEqual(
        self.envelope()['run_config']['action_mode'], 'speech_and_action'
    )
    with mock.patch.object(
        run_lib, 'build_simulation', wraps=run_lib.build_simulation
    ) as builder:
      result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'beats_completed')
    self.assertEqual(result.committed_turn_id, 4)
    self.assertEqual(
        [r['turn_id'] for r in self.turn_log().load()], [1, 2, 3, 4]
    )
    self.assertEqual(
        builder.call_args_list[-1].kwargs['run_config']['action_mode'],
        'speech_and_action',
    )
    self.assertEqual(
        self.envelope()['run_config']['action_mode'], 'speech_and_action'
    )

  def test_resume_explicit_same_mode_is_allowed(self):
    config = self._json_fixture()
    self._commit_after_turn_two(self.state_dir, config)
    result = run_lib.run(
        self.state_dir, resume=True, action_mode='speech_and_action'
    )
    self.assertEqual(result.reason, 'beats_completed')
    self.assertEqual(result.committed_turn_id, 4)
    self.assertEqual(
        self.envelope()['run_config']['action_mode'], 'speech_and_action'
    )

  def test_resume_explicit_different_mode_is_rejected(self):
    json_dir = self._dir('json-checkpoint')
    self._commit_after_turn_two(json_dir, self._json_fixture())
    checkpoint_path = os.path.join(json_dir, 'checkpoint.json')
    log_path = os.path.join(json_dir, 'story_turns.jsonl')
    with open(checkpoint_path, encoding='utf-8') as handle:
      checkpoint_before = handle.read()
    with open(log_path, encoding='utf-8') as handle:
      log_before = handle.read()
    with mock.patch.object(
        run_lib, 'make_language_model', wraps=run_lib.make_language_model
    ) as maker:
      with self.assertRaisesRegex(ValueError, 'action_mode mismatch'):
        run_lib.run(json_dir, resume=True, action_mode='speech_only')
    maker.assert_not_called()
    with open(checkpoint_path, encoding='utf-8') as handle:
      self.assertEqual(handle.read(), checkpoint_before)
    with open(log_path, encoding='utf-8') as handle:
      self.assertEqual(handle.read(), log_before)

    speech_dir = self._dir('speech-checkpoint')
    self._commit_after_turn_two(speech_dir, self._speech_fixture())
    with mock.patch.object(
        run_lib, 'make_language_model', wraps=run_lib.make_language_model
    ) as maker:
      with self.assertRaisesRegex(ValueError, 'action_mode mismatch'):
        run_lib.run(speech_dir, resume=True, action_mode='speech_and_action')
    maker.assert_not_called()

  def test_schema1_checkpoint_really_resumes_as_speech_only(self):
    # Construct a complete schema-1 checkpoint: two genuinely committed
    # turns of simulation state under the old contract, an old
    # schema-1 run_config (no action_mode anywhere) and a schema-1
    # history log without the schema-2 turn stamps.
    schema1_config = {
        key: value
        for key, value in self._speech_fixture().items()
        if key != 'action_mode'
    }
    schema1_config['schema_version'] = 1
    schema1_config['gm_params'] = {
        key: value
        for key, value in schema1_config['gm_params'].items()
        if key != 'action_mode'
    }
    self._commit_after_turn_two(self.state_dir, schema1_config)
    envelope = self.envelope()
    envelope['run_config'] = copy.deepcopy(schema1_config)
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'),
        'w',
        encoding='utf-8',
    ) as handle:
      json.dump(envelope, handle, ensure_ascii=False, indent=2)
    schema1_records = [
        {
            key: value
            for key, value in record.items()
            if key not in ('schema_version', 'raw_action', 'action_intent')
        }
        for record in self.turn_log().load()
    ]
    self.assertEqual([r['turn_id'] for r in schema1_records], [1, 2])
    for record in schema1_records:
      self.assertNotIn('schema_version', record)
      self.assertNotIn('raw_action', record)
      self.assertNotIn('action_intent', record)
    old_records = copy.deepcopy(schema1_records)
    self.turn_log()._rewrite(schema1_records)  # pylint: disable=protected-access

    # The real resume keeps the old contract: numbering continues, the
    # original dialogue and the old committed records stay untouched,
    # and only the new turns may carry the schema-2 stamps.
    with mock.patch.object(
        run_lib, 'build_simulation', wraps=run_lib.build_simulation
    ) as builder:
      result = run_lib.run(self.state_dir, resume=True)
    self.assertEqual(result.reason, 'beats_completed')
    self.assertEqual(result.committed_turn_id, 4)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    self.assertEqual(records[:2], old_records)
    for record in old_records:
      self.assertEqual(record['speech'], '这宅子的事，急不得。')
    for record in records[2:]:
      self.assertEqual(record['schema_version'], 2)
    self.assertEqual(
        builder.call_args_list[-1].kwargs['run_config']['action_mode'],
        'speech_only',
    )
    final_envelope = self.envelope()
    self.assertEqual(final_envelope['run_config']['schema_version'], 1)
    self.assertEqual(final_envelope['run_config']['action_mode'], 'speech_only')
    self.assertEqual(self.summary()['action_mode'], 'speech_only')

  def test_cli_distinguishes_omitted_and_explicit_action_mode(self):
    flags = run_lib._FLAGS  # pylint: disable=protected-access
    result_stub = types.SimpleNamespace(
        reason='step_limit',
        committed_turn_id=0,
        used_model_attempts=0,
        wall_time_seconds=0.0,
    )
    saved = {
        name: (flags[name].value, flags[name].present)
        for name in ('state_dir', 'disable_language_model', 'action_mode')
    }
    try:
      flags['state_dir'].value = self.state_dir
      flags['state_dir'].present = True
      flags['disable_language_model'].value = True
      flags['disable_language_model'].present = True
      # Omitted --action_mode: main forwards None so run() itself
      # derives the first-run default or the checkpointed mode.
      flags['action_mode'].value = None
      flags['action_mode'].present = False
      with mock.patch.object(
          run_lib, 'run', return_value=result_stub
      ) as run_mock:
        run_lib.main([])
      self.assertIsNone(run_mock.call_args.kwargs['action_mode'])
      # An explicit --action_mode reaches run() as the given value.
      flags['action_mode'].value = 'speech_and_action'
      flags['action_mode'].present = True
      with mock.patch.object(
          run_lib, 'run', return_value=result_stub
      ) as run_mock:
        run_lib.main([])
      self.assertEqual(
          run_mock.call_args.kwargs['action_mode'], 'speech_and_action'
      )
    finally:
      for name, (value, present) in saved.items():
        flags[name].value = value
        flags[name].present = present

  def test_unknown_run_config_schema_is_rejected(self):
    unknown_config = self._speech_fixture(total_turn_limit=2)
    unknown_config['schema_version'] = 99
    with self.assertRaisesRegex(ValueError, 'schema version'):
      run_lib.run(self.state_dir, run_config=unknown_config)
    self.assertFalse(
        os.path.exists(os.path.join(self.state_dir, 'checkpoint.json'))
    )
    run_lib.run(
        self.state_dir, run_config=self._speech_fixture(total_turn_limit=2)
    )
    envelope = self.envelope()
    envelope['run_config']['schema_version'] = 3
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'),
        'w',
        encoding='utf-8',
    ) as handle:
      json.dump(envelope, handle, ensure_ascii=False, indent=2)
    with self.assertRaisesRegex(ValueError, 'schema version'):
      run_lib.run(self.state_dir, resume=True)


# The GM-only date of the swapped old will: public only after the core
# adjudication discloses it (task-9 pipeline tests).
_WILL_DATE = '二〇一九年三月十二日'

# Fingerprints for locating real prompts of the action chain.
_ATTEMPT_QUESTION = 'What happens as a result of the attempted action?'
_DUAL_TRACK_MARK = 'Output a JSON object with exactly two string fields'
_REVIEW_MARK = '# Story resolution semantic review'


def _last_question_block(prompt: str) -> str:
  """Returns the prompt from its last Question block on."""
  start = prompt.rfind('Question: ')
  return prompt[start:] if start >= 0 else ''


def _prompts_routed_to(stub, fingerprint: str) -> list[str]:
  """Returns the prompts whose LAST question block carries the mark.

  The resolution chain shares one document, so earlier questions stay
  quoted inside later prompts; routing scope mirrors the stub's. Use
  `_prompts_containing` for statement fingerprints that never appear
  inside a question (e.g. the review heading).

  Args:
    stub: the recorded prompt stub model.
    fingerprint: substring identifying the routed question.

  Returns:
    The matching prompts in call order.
  """
  return [
      p for p in stub.text_prompts if fingerprint in _last_question_block(p)
  ]


def _prompts_containing(stub, fingerprint: str) -> list[str]:
  """Returns the prompts containing a statement fingerprint anywhere."""
  return [p for p in stub.text_prompts if fingerprint in p]


def _exercise_prompts_of(stub, name: str) -> list[str]:
  """Returns one character's recorded action prompts (speech contract)."""
  return [p for p in stub.text_prompts if f'{name} 现在说什么' in p]


def _fdx_paragraphs(state_dir: str, paragraph_type: str) -> list[str]:
  """Returns the text of one paragraph type of the exported FDX."""
  root = ET.fromstring(
      open(os.path.join(state_dir, 'script.fdx'), encoding='utf-8').read()
  )
  return [
      node.text or ''
      for node in root.findall(
          f"./Content/Paragraph[@Type='{paragraph_type}']/Text"
      )
  ]


class _ActionChainTestCase(_StateDirTestCase):
  """Base for speech-and-action chain tests with a recorded stub.

  All runs use the beats_only stage switch with scoring disabled and no
  director feedback (and no repetition intervention exists yet at this
  stage), so the only behavioral change under test is the character's
  own action adjudication.
  """

  def _action_config(
      self,
      *,
      total_turn_limit: int = 4,
      total_model_attempt_limit: int = 100,
  ) -> dict[str, Any]:
    return _fixture_config(
        total_turn_limit=total_turn_limit,
        total_model_attempt_limit=total_model_attempt_limit,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
        action_mode='speech_and_action',
    )

  def _run_with_stub(self, directory: str, config, stub):
    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(stub, ())
    ):
      return run_lib.run(directory, run_config=config)


class ActionPipelineIntegrationTest(_ActionChainTestCase):
  """Adjudicated core results flow through observation and exports.

  The deterministic stub's dual-track draft narrates only the evidence
  the current prompt carries (never stub configuration), so a marker
  reaching the canonical event, the next player's observation and the
  FDX Action paragraph proves the core helper's true return value
  traveled the whole chain.
  """

  _LAWYER_LINE = '于程序而言，我要求当庭核验这份原件。'
  _LAWYER_INTENT = '周律师从封套中取出旧遗嘱原件，逐字查验落款日期。'
  _SUCCESS_RESULT = f'原件落款日期为{_WILL_DATE}，纸面上有陈望山的签名与手印。'
  _FAILURE_RESULT = '封套受潮粘连，落款日期无法辨认。'
  _SAFE_INTENT = '林雪用自己持有的那把钥匙去开堂屋侧室的保险柜。'
  _BLOCKED_RESULT = (
      '保险柜纹丝不动：它须两把钥匙同时转动才能开启，林雪手里只有一把，'
      '柜门没有开启。'
  )

  def test_success_result_flows_to_observation_and_fdx(self):
    config = self._action_config()
    stub = testing_stubs.PromptStubModel(
        dialogue_lines={'周律师': self._LAWYER_LINE},
        action_intents={'周律师': self._LAWYER_INTENT},
        action_result=self._SUCCESS_RESULT,
        stage_directions='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 4)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    # The lawyer's turn is decoded from the JSON contract.
    lawyer = records[2]
    self.assertEqual(lawyer['actor'], '周律师')
    self.assertEqual(lawyer['speech'], self._LAWYER_LINE)
    self.assertEqual(lawyer['action_intent'], self._LAWYER_INTENT)
    speech, intent = story_action.decode_action(lawyer['raw_action'])
    self.assertEqual((speech, intent), (self._LAWYER_LINE, self._LAWYER_INTENT))
    self.assertEqual(lawyer['schema_version'], 2)
    # The core helper's true return value (its causal glue included) is
    # the whole narrator track and reaches the canonical event verbatim.
    core_result = (
        f'{self._LAWYER_INTENT} Because of that, {self._SUCCESS_RESULT}'
    )
    self.assertEqual(lawyer['stage_directions'], core_result)
    self.assertEqual(
        lawyer['resolved_event'],
        f'周律师: {self._LAWYER_LINE}\n' + core_result,
    )
    # The same evidence section reached the final generation and review.
    finals = _prompts_routed_to(stub, _DUAL_TRACK_MARK)
    reviews = _prompts_containing(stub, _REVIEW_MARK)
    self.assertEqual(len(finals), 4)
    self.assertEqual(len(reviews), 4)
    for prompt in (finals[2], reviews[2]):
      evidence = testing_stubs.read_evidence_section(prompt)
      assert evidence is not None
      self.assertEqual(evidence['action_result'], core_result)
      self.assertEqual(evidence['action_intent'], self._LAWYER_INTENT)
    # Only the lawyer's turn was adjudicated.
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 1)
    # Before the action happened, no earlier round exposed the date.
    for record in records[:2]:
      self.assertNotIn(_WILL_DATE, record['resolved_event'])
      self.assertNotIn(_WILL_DATE, record['stage_directions'])
    for name in ('林雪', '陈默', '周律师'):
      for prompt in _exercise_prompts_of(stub, name):
        self.assertNotIn(_WILL_DATE, prompt)
    # After the adjudicated disclosure, the next player observed it: the
    # date is in 王婶's actual action prompt and in her restored memory.
    wang_prompts = _exercise_prompts_of(stub, '王婶')
    self.assertEqual(len(wang_prompts), 1)
    self.assertIn(_WILL_DATE, wang_prompts[0])
    sim_after, director_after = self.restored(config)
    recent_events = cast(
        'list[str]', director_after.get_state()['recent_events']
    )
    self.assertIn(_WILL_DATE, recent_events[2])
    self.assertIn(_WILL_DATE, _entity_memory_text(sim_after, '王婶'))
    # The FDX carries the adjudicated date in exactly one Action
    # paragraph; the dialogue track only has the lawyer's own words.
    actions = _fdx_paragraphs(self.state_dir, 'Action')
    self.assertEqual(actions, [core_result])
    self.assertIn(_WILL_DATE, actions[0])
    dialogues = _fdx_paragraphs(self.state_dir, 'Dialogue')
    self.assertEqual(dialogues.count(self._LAWYER_LINE), 1)
    self.assertNotIn(_WILL_DATE, '\n'.join(dialogues))

  def test_blocked_result_flows_to_observation_and_fdx(self):
    config = self._action_config(total_turn_limit=2)
    stub = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._SAFE_INTENT},
        action_result=self._BLOCKED_RESULT,
        stage_directions='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    records = self.turn_log().load()
    blocked = records[0]
    core_result = f'{self._SAFE_INTENT} Because of that, {self._BLOCKED_RESULT}'
    self.assertEqual(blocked['stage_directions'], core_result)
    self.assertIn('柜门没有开启', blocked['resolved_event'])
    # The adjudication question was conditioned on the public two-key
    # constraint (the world constraints section of the shared document).
    adjudication = _prompts_routed_to(stub, _ATTEMPT_QUESTION)[0]
    self.assertIn('保险柜须两把钥匙同时转动才能开启', adjudication)
    # No other player's new choice is generated: nobody hands over the
    # second key, and the key holder is never made to act.
    self.assertNotIn('陈母', blocked['resolved_event'])
    for fabricated in ('交出', '递上', '第二把钥匙', '陈默', '周律师'):
      self.assertNotIn(fabricated, blocked['stage_directions'])
    # The explicit failure reached the next player's observation.
    chen_prompts = _exercise_prompts_of(stub, '陈默')
    self.assertEqual(len(chen_prompts), 1)
    self.assertIn('柜门没有开启', chen_prompts[0])
    # And the exported Action paragraph narrates the failure.
    self.assertEqual(_fdx_paragraphs(self.state_dir, 'Action'), [core_result])

  def test_changing_only_core_result_changes_final_event(self):
    # Same config, inputs and background (no fixed stage directions):
    # only the core adjudication's answer differs between the two runs.
    def core_run(subdir: str, core_answer: str):
      os.makedirs(subdir, exist_ok=True)
      config = self._action_config()
      stub = testing_stubs.PromptStubModel(
          dialogue_lines={'周律师': self._LAWYER_LINE},
          action_intents={'周律师': self._LAWYER_INTENT},
          action_result=core_answer,
          stage_directions='',
      )
      result = self._run_with_stub(subdir, config, stub)
      return result, self._records_of(subdir), stub

    success_dir = os.path.join(self._tmp.name, 'success')
    failure_dir = os.path.join(self._tmp.name, 'failure')
    success_result, success_records, success_stub = core_run(
        success_dir, self._SUCCESS_RESULT
    )
    failure_result, failure_records, failure_stub = core_run(
        failure_dir, self._FAILURE_RESULT
    )
    self.assertEqual(success_result.reason, failure_result.reason)
    # The turns before the action are identical across the runs.
    self.assertEqual(success_records[:2], failure_records[:2])
    # The final event and every export follow the core's answer.
    self.assertNotEqual(
        success_records[2]['resolved_event'],
        failure_records[2]['resolved_event'],
    )
    self.assertIn(_WILL_DATE, success_records[2]['resolved_event'])
    self.assertNotIn(_WILL_DATE, failure_records[2]['resolved_event'])
    self.assertIn('无法辨认', failure_records[2]['stage_directions'])
    self.assertNotEqual(
        _fdx_paragraphs(success_dir, 'Action'),
        _fdx_paragraphs(failure_dir, 'Action'),
    )
    self.assertIn(_WILL_DATE, _fdx_paragraphs(success_dir, 'Action')[0])
    self.assertNotIn(
        _WILL_DATE, '\n'.join(_fdx_paragraphs(failure_dir, 'Action'))
    )
    # The next player's observation also differs.
    self.assertIn(_WILL_DATE, _exercise_prompts_of(success_stub, '王婶')[0])
    self.assertNotIn(
        _WILL_DATE, '\n'.join(_exercise_prompts_of(failure_stub, '王婶'))
    )

  def _records_of(self, directory: str) -> list[dict[str, Any]]:
    return collector.StoryTurnLog(
        os.path.join(directory, 'story_turns.jsonl')
    ).load()

  def test_action_only_is_visible_without_fabricated_speech(self):
    intent = '林雪起身去关紧被穿堂风扫开的窗门。'
    for subdir, core_answer, marker in (
        ('success', '窗门被关紧，堂内的烛火稳了下来。', '窗门被关紧'),
        ('failure', '窗门卡住了，怎么也关不上。', '窗门卡住'),
    ):
      with self.subTest(outcome=subdir):
        directory = os.path.join(self._tmp.name, subdir)
        os.makedirs(directory, exist_ok=True)
        config = self._action_config(total_turn_limit=2)
        stub = testing_stubs.PromptStubModel(
            dialogue_lines={'林雪': ''},
            action_intents={'林雪': intent},
            action_result=core_answer,
            stage_directions='',
        )
        result = self._run_with_stub(directory, config, stub)
        self.assertEqual(result.reason, 'step_limit')
        self.assertEqual(result.committed_turn_id, 2)
        records = self._records_of(directory)
        action_only = records[0]
        self.assertEqual(action_only['actor'], '林雪')
        self.assertEqual(action_only['speech'], '')
        self.assertEqual(action_only['action_intent'], intent)
        speech, decoded_intent = story_action.decode_action(
            action_only['raw_action']
        )
        self.assertEqual((speech, decoded_intent), ('', intent))
        # The canonical event carries no fabricated speaker line: it is
        # the adjudicated narrator track alone.
        core_result = f'{intent} Because of that, {core_answer}'
        self.assertEqual(action_only['resolved_event'], core_result)
        self.assertEqual(action_only['stage_directions'], core_result)
        # The outcome reached the next player's observation...
        chen_prompts = _exercise_prompts_of(stub, '陈默')
        self.assertEqual(len(chen_prompts), 1)
        self.assertIn(marker, chen_prompts[0])
        # ...and the export: an Action paragraph, no Character cue and
        # no invented dialogue for the silent turn.
        self.assertIn(marker, _fdx_paragraphs(directory, 'Action')[0])
        characters = [
            c
            for c in ET.fromstring(
                open(
                    os.path.join(directory, 'script.fdx'),
                    encoding='utf-8',
                ).read()
            ).findall("./Content/Paragraph[@Type='Character']/Text")
        ]
        self.assertEqual([c.text for c in characters], ['陈默'])
        self.assertNotIn(
            '林雪', '\n'.join(_fdx_paragraphs(directory, 'Dialogue'))
        )

  def test_multiline_speech_is_preserved_end_to_end(self):
    speech = '这话说前半句。\n\n这后半句，我今天必须说完。'

    class FlatteningDraft(testing_stubs.PromptStubModel):
      """A draft that flattens paragraph breaks, like a real model might."""

      def _dual_track_output(self, prompt):
        payload = json.loads(super()._dual_track_output(prompt))
        payload['resolved_event'] = payload['resolved_event'].replace(
            '\n\n', ' '
        )
        return json.dumps(payload, ensure_ascii=False)

    config = self._action_config(total_turn_limit=1)
    stub = FlatteningDraft(dialogue_lines={'林雪': speech}, stage_directions='')
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 1)
    record = self.turn_log().load()[0]
    # Whitespace-level draft flattening is tolerated; the canonical
    # event and the JSONL keep the original characters.
    self.assertEqual(record['speech'], speech)
    self.assertEqual(record['resolved_event'], f'林雪: {speech}')
    self.assertEqual(record['stage_directions'], '')
    # The XML-decoded FDX dialogue keeps the raw paragraph break.
    self.assertEqual(_fdx_paragraphs(self.state_dir, 'Dialogue'), [speech])

    class RewritingDraft(testing_stubs.PromptStubModel):
      """A draft that changes one word: the word-level error branch."""

      def _dual_track_output(self, prompt):
        payload = json.loads(super()._dual_track_output(prompt))
        payload['resolved_event'] = payload['resolved_event'].replace(
            '必须说完', '不必再说'
        )
        return json.dumps(payload, ensure_ascii=False)

    error_dir = os.path.join(self._tmp.name, 'word-change')
    os.makedirs(error_dir, exist_ok=True)
    stub = RewritingDraft(dialogue_lines={'林雪': speech}, stage_directions='')
    result = self._run_with_stub(error_dir, config, stub)
    self.assertEqual(result.reason, 'error')
    self.assertIn('rewrote the original speech', result.error_message or '')
    self.assertEqual(self._records_of(error_dir), [])
    self.assertEqual(
        _fdx_paragraphs(error_dir, 'Dialogue'),
        [],
    )


class ActionBoundaryIntegrationTest(_ActionChainTestCase):
  """Agency boundaries of the adjudicated chain, observed on real prompts.

  NOTE: the stub's fixed approving review exercises the plumbing of the
  boundary prompts (and, where injected, the rejection path), never the
  real semantic judgment of a production model. Each case records the
  actual adjudication / review prompts and asserts the constraints they
  carry; live-model acceptance remains necessary (see README).
  """

  _SIGN_INTENT = '周律师把会议笔录推到陈母面前，要求她当场签字认罪。'
  _SIGN_REFUSAL = '陈母把笔录推了回去，既没有签字，也没有答应任何条件。'
  _CONSENT_INTENT = '林雪当众明确表示同意暂缓出售老宅。'
  _CONSENT_RESULT = '林雪当众声明她本人同意暂缓处置老宅，这是她自己的表态。'
  _READING_INTENT = '周律师当众逐字宣读旧遗嘱的全文内容。'
  _READING_RESULT = (
      '周律师展开旧遗嘱准备宣读，但全文的具体条款并未在此刻被当众披露。'
  )
  _SECRET_SENTENCE = '他欠下巨额赌债，三个月内必须还清，否则后果不堪设想。'

  def test_signature_request_does_not_create_the_other_players_choice(self):
    config = self._action_config(total_turn_limit=3)
    stub = testing_stubs.PromptStubModel(
        action_intents={'周律师': self._SIGN_INTENT},
        action_result=self._SIGN_REFUSAL,
        stage_directions='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.committed_turn_id, 3)
    # The adjudication prompt carries the agency rule and names every
    # player whose voluntary choice it must not decide.
    adjudication = _prompts_routed_to(stub, _ATTEMPT_QUESTION)[0]
    self.assertIn(
        'Never decide a new voluntary action for any other player character',
        adjudication,
    )
    self.assertIn(
        'Player characters whose voluntary actions only they can choose:'
        ' 林雪, 陈默, 周律师, 王婶, 陈母',
        adjudication,
    )
    # The review prompt carries the matching signature boundary.
    review = _prompts_containing(stub, _REVIEW_MARK)[2]
    self.assertIn('signature', review)
    # The committed event narrates only the scripted refusal.
    record = self.turn_log().load()[2]
    self.assertIn('既没有签字', record['stage_directions'])
    self.assertNotIn('陈母当众签下名字', record['resolved_event'])

    # Injected reviewer rejection of an invented signature: nothing is
    # broadcast or committed. This verifies the rejection plumbing; the
    # approving stub above cannot verify real semantic judgment.
    class SigningDraft(testing_stubs.PromptStubModel):

      def _dual_track_output(self, prompt):
        payload = json.loads(super()._dual_track_output(prompt))
        extra = '陈母当众签下名字并认罪。'
        payload['stage_directions'] += '\n' + extra
        payload['resolved_event'] += '\n' + extra
        return json.dumps(payload, ensure_ascii=False)

    reject_dir = os.path.join(self._tmp.name, 'sign-reject')
    os.makedirs(reject_dir, exist_ok=True)
    stub = SigningDraft(
        action_intents={'周律师': self._SIGN_INTENT},
        action_result=self._SIGN_REFUSAL,
        stage_directions='',
        review_response=(
            '{"valid": false, "reason": "Invented signature for 陈母"}'
        ),
    )
    result = self._run_with_stub(reject_dir, config, stub)
    self.assertEqual(result.reason, 'error')
    self.assertIn('Invented signature', result.error_message or '')
    records = collector.StoryTurnLog(
        os.path.join(reject_dir, 'story_turns.jsonl')
    ).load()
    self.assertEqual(records, [])
    self.assertNotIn(
        '陈母当众签下名字', '\n'.join(_fdx_paragraphs(reject_dir, 'Action'))
    )

  def test_own_consent_recorded_only_for_the_consenting_character(self):
    config = self._action_config(total_turn_limit=2)
    stub = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._CONSENT_INTENT},
        action_result=self._CONSENT_RESULT,
        stage_directions='',
    )
    self._run_with_stub(self.state_dir, config, stub)
    adjudication = _prompts_routed_to(stub, _ATTEMPT_QUESTION)[0]
    self.assertIn(
        'Adjudicate only the attempted action the current speaker proposed'
        ' for themself this turn',
        adjudication,
    )
    record = self.turn_log().load()[0]
    core_result = (
        f'{self._CONSENT_INTENT} Because of that, {self._CONSENT_RESULT}'
    )
    self.assertEqual(record['stage_directions'], core_result)
    # Only 林雪's own consent is recorded; nobody else's stance appears.
    for other in ('陈默', '陈母', '周律师', '王婶', '同意出售', '也都同意'):
      self.assertNotIn(other, record['resolved_event'])

  def test_past_claims_do_not_generate_physical_evidence(self):
    claim = '陈默当众声称：保险柜里封存着老人生前留下的监控录像。'
    config = self._action_config(total_turn_limit=2)
    stub = testing_stubs.PromptStubModel(
        dialogue_lines={'陈默': claim}, stage_directions=''
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.committed_turn_id, 2)
    # A spoken claim about the past is not an action attempt: no
    # adjudication call happens for it.
    self.assertEqual(_prompts_routed_to(stub, _ATTEMPT_QUESTION), [])
    review = _prompts_containing(stub, _REVIEW_MARK)[1]
    self.assertIn('not proof of the claims it contains', review)
    record = self.turn_log().load()[1]
    # The claim stays attributed speech; no evidence materializes.
    self.assertIn('监控录像', record['speech'])
    self.assertEqual(record['stage_directions'], '')
    self.assertNotIn(
        '监控录像', '\n'.join(_fdx_paragraphs(self.state_dir, 'Action'))
    )

  def test_outsider_assertion_does_not_confirm_a_secret(self):
    assertion = '王婶放大嗓门问道：陈默是不是在外头欠了赌债？'
    config = self._action_config(total_turn_limit=4)
    stub = testing_stubs.PromptStubModel(
        dialogue_lines={'王婶': assertion}, stage_directions=''
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.committed_turn_id, 4)
    review = _prompts_containing(stub, _REVIEW_MARK)[3]
    self.assertIn('GM-only knowledge is not public knowledge', review)
    record = self.turn_log().load()[3]
    # The assertion remains quoted speech: it is never upgraded into an
    # established secret in the narrator track or the exports.
    self.assertIn('赌债', record['speech'])
    self.assertEqual(record['stage_directions'], '')
    self.assertNotIn(
        '赌债', '\n'.join(_fdx_paragraphs(self.state_dir, 'Action'))
    )
    # The exact GM-only secret sentence never broadcasts; other players
    # hold at most the public accusation they heard.
    sim_after, _ = self.restored(config)
    for name in ('林雪', '陈默', '周律师', '王婶'):
      memory = _entity_memory_text(sim_after, name)
      self.assertNotIn(self._SECRET_SENTENCE, memory)

  def test_unconfigured_document_content_is_not_invented(self):
    config = self._action_config(total_turn_limit=3)
    stub = testing_stubs.PromptStubModel(
        action_intents={'周律师': self._READING_INTENT},
        action_result=self._READING_RESULT,
        stage_directions='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.committed_turn_id, 3)
    records = self.turn_log().load()
    # The GM-only configured content of the document (its date) stays
    # hidden: the adjudicated result alone is narrated.
    self.assertIn('并未在此刻被当众披露', records[2]['stage_directions'])
    for record in records:
      self.assertNotIn(_WILL_DATE, record['resolved_event'])
      self.assertNotIn(_WILL_DATE, record['stage_directions'])
    self.assertNotIn(
        _WILL_DATE, '\n'.join(_fdx_paragraphs(self.state_dir, 'Action'))
    )


class ActionChainBudgetTest(_ActionChainTestCase):
  """Budget paths of the adjudicated chain (spec section 11.3).

  Model-call pattern per story turn in the sequential engine: three
  perception questions plus the contract exercise (four entity text
  calls), then — for a turn with an action intent — one adjudication
  text call, the final dual-track generation and its semantic review;
  one extra choice attempt whenever the beat-completion question is
  asked. Fixtures position exhaustion by the total attempt limit.
  """

  _INTENTS = {'林雪': '林雪检查窗栓。', '陈默': '陈默翻看遗嘱封套。'}
  _RESULT = '动作有了确定的结果。'

  def _intent_config(self, *, limit: int, turns: int = 4) -> dict[str, Any]:
    return self._action_config(
        total_turn_limit=turns, total_model_attempt_limit=limit
    )

  def _run_budget(self, directory: str, config, *, intents=None, **stub_params):
    stub = testing_stubs.PromptStubModel(
        action_intents=self._INTENTS if intents is None else intents,
        action_result=self._RESULT,
        stage_directions='',
        **stub_params,
    )
    return self._run_with_stub(directory, config, stub), stub

  def test_exhaustion_at_adjudication_of_step_two(self):
    # Turn one costs 7 attempts (4 entity + adjudication + final +
    # review); step two's four entity calls land at 11, so the
    # adjudication of step two is the blocked 12th attempt.
    config = self._intent_config(limit=11)
    result, stub = self._run_budget(
        self.state_dir, config, dialogue_lines={'陈默': '第二条未广播的台词。'}
    )
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 7)
    self.assertEqual(result.used_model_attempts, 11)
    # The blocked turn was never adjudicated, reviewed, broadcast or
    # exported: no new StoryTurn, no checkpoint advance, no observation.
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 1)
    self.assertEqual(len(_prompts_containing(stub, _REVIEW_MARK)), 1)
    sim_after, _ = self.restored(config)
    self.assertNotIn(
        '第二条未广播的台词', _entity_memory_text(sim_after, '林雪')
    )
    self.assertNotIn('第二条未广播的台词', self.read_script('script.fdx'))

  def test_exhaustion_at_final_generation_of_step_two(self):
    # Limit 12 lets step two's adjudication through (the 12th attempt)
    # and blocks the final dual-track generation.
    config = self._intent_config(limit=12)
    result, stub = self._run_budget(self.state_dir, config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(result.used_model_attempts, 12)
    # The adjudicated result of the blocked turn exists in the document
    # trace but is never committed: no second final draft or review.
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 2)
    self.assertEqual(len(_prompts_routed_to(stub, _DUAL_TRACK_MARK)), 1)
    self.assertEqual(len(_prompts_containing(stub, _REVIEW_MARK)), 1)

  def test_exhaustion_at_semantic_review_of_step_two(self):
    # Limit 13 lets step two's final generation through (the 13th
    # attempt) and blocks its semantic review.
    config = self._intent_config(limit=13)
    result, stub = self._run_budget(self.state_dir, config)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual([r['turn_id'] for r in self.turn_log().load()], [1])
    self.assertEqual(result.used_model_attempts, 13)
    self.assertEqual(len(_prompts_routed_to(stub, _DUAL_TRACK_MARK)), 2)
    self.assertEqual(len(_prompts_containing(stub, _REVIEW_MARK)), 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 7)

  def test_pure_dialogue_skips_the_adjudication_call(self):
    # In speech_and_action mode, turns without an action intent make
    # zero adjudication calls: two turns cost exactly the speech-only
    # pattern (4 + 2 per turn plus one beat-completion choice).
    action_config = self._intent_config(limit=13, turns=2)
    result, stub = self._run_budget(self.state_dir, action_config, intents={})
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(result.used_model_attempts, 13)
    self.assertEqual(_prompts_routed_to(stub, _ATTEMPT_QUESTION), [])
    speech_config = _fixture_config(
        total_turn_limit=2,
        total_model_attempt_limit=13,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
    )
    speech_dir = os.path.join(self._tmp.name, 'speech-only')
    speech_result = self._run_with_stub(
        speech_dir, speech_config, testing_stubs.PromptStubModel()
    )
    self.assertEqual(speech_result.reason, 'step_limit')
    self.assertEqual(speech_result.used_model_attempts, 13)

  def test_intent_adds_exactly_one_attempt_for_the_step(self):
    # One turn with an adjudicated intent costs exactly one text call
    # more than the same turn without one (no completion choice is
    # asked at turn one, since the beat's min_turns is two).
    with_intent_config = self._intent_config(limit=7, turns=1)
    result, _ = self._run_budget(self.state_dir, with_intent_config)
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(result.used_model_attempts, 7)
    without_intent_dir = os.path.join(self._tmp.name, 'no-intent')
    result = self._run_with_stub(
        without_intent_dir,
        self._intent_config(limit=6, turns=1),
        testing_stubs.PromptStubModel(stage_directions=''),
    )
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(result.used_model_attempts, 6)

  def test_path_b_completion_judgment_commits_the_action_step_once(self):
    # Limit 14 covers both turns fully (7 + 7) and blocks step two's
    # beat-completion choice: path B commits the adjudicated step once
    # and the budget stop (not the turn limit) ends the run.
    config = self._intent_config(limit=14, turns=4)
    result, _ = self._run_budget(self.state_dir, config)
    self.assertEqual(result.reason, 'budget_limit')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    self.assertTrue(records[1]['budget_stopped'])
    self.assertIsNone(records[1]['tension'])
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    self.assertEqual(self.envelope()['used_model_attempts'], 14)
    # Both adjudicated turns are in the exports exactly once.
    self.assertEqual(len(_fdx_paragraphs(self.state_dir, 'Action')), 2)
    _, director = self.restored(config)
    state = director.get_state()
    self.assertEqual(state['story_turn'], 2)
    self.assertTrue(state['budget_stopped'])
    self.assertFalse(director._awaiting_event)  # pylint: disable=protected-access
    self.assertIsNone(director._pending_commit)  # pylint: disable=protected-access

  def test_path_b_scoring_position_commits_the_action_step_once(self):
    # With scoring enabled every second turn, limit 15 covers both
    # turns plus step two's completion choice, and blocks the scoring
    # question: still exactly one commit of the adjudicated step.
    config = self._intent_config(limit=15, turns=4)
    config['mode'] = 'tension_observe'
    config['director_params'] = {
        'eval_interval': 2,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': False,
    }
    result, _ = self._run_budget(self.state_dir, config)
    self.assertEqual(result.reason, 'budget_limit')
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    self.assertTrue(records[1]['budget_stopped'])
    self.assertEqual(self.envelope()['committed_turn_id'], 2)
    self.assertEqual(result.used_model_attempts, 15)


if __name__ == '__main__':
  unittest.main()
