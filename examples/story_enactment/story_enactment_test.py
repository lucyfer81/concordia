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
final resolution and its semantic review (6 text attempts), plus one choice
attempt whenever the
beat-completion question is asked. Fixtures position budget exhaustion by
choosing the total attempt limit relative to these counts.
"""

import contextlib
import dataclasses
import json
import os
import tempfile
import time
from typing import Any
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from concordia.contrib.components.game_master import drama_director
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


if __name__ == '__main__':
  unittest.main()
