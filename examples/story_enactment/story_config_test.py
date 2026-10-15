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

"""Tests for story_config data model and codecs."""

import dataclasses
import unittest

from concordia.contrib.prefabs.game_master import story_scene
import numpy as np

from examples.story_enactment import acceptance
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs


class StoryConfigTest(unittest.TestCase):

  def test_roundtrip(self):
    config = story_config.make_example_config()
    d = config.to_dict()
    self.assertEqual(story_config.StoryConfig.from_dict(d), config)

  def test_validate_rejects_duplicate_names(self):
    config = story_config.make_example_config()
    broken = dataclasses.replace(
        config, characters=config.characters[:1] + config.characters[:1]
    )
    self.assertIn('duplicate', ' '.join(story_config.validate(broken)))

  def test_default_endings_preserve_mothers_constraint(self):
    config = story_config.make_example_config()
    mother = next(c for c in config.characters if c.name == '陈母')
    self.assertEqual(mother.hard_constraint, '绝不承认自己碰过遗嘱')
    endings = '\n'.join(config.allowed_endings)
    self.assertNotIn('陈母主动说出真相', endings)
    self.assertIn('不要求陈母认罪', endings)

  def test_speech_requests_character_goal_and_response_to_new_information(self):
    prompt = story_config.make_speech_action_spec().call_to_action
    self.assertIn('最近公开的新信息', prompt)
    self.assertIn('自己的目标、底线和表达风格', prompt)
    self.assertIn('不要编造证据', prompt)

  def test_interview_endings_require_no_forbidden_confession(self):
    config = acceptance.make_scenario_two_config()
    candidate = next(c for c in config.characters if c.name == '唐薇')
    self.assertEqual(
        candidate.hard_constraint, '绝不主动承认简历中任何不实之处'
    )
    endings = '\n'.join(config.allowed_endings)
    self.assertNotIn('唐薇当场承认造假', endings)
    self.assertNotIn('和盘托出', endings)
    self.assertIn('有既定来源的公开证据核实', endings)
    self.assertIn('郑兰依据核实结果自主明确决定不录用', endings)
    self.assertIn('不要求唐薇承认简历不实', endings)
    self.assertIn('她自主明确提出退出本次应聘', endings)

  def test_wedding_endings_preserve_both_old_partners_constraints(self):
    config = acceptance.make_scenario_three_config()
    cast = {c.name: c for c in config.characters}
    self.assertEqual(cast['沈玉'].hard_constraint, '绝不主动提起与新郎的过去')
    self.assertEqual(
        cast['陆屿'].hard_constraint, '绝不让任何旧事在今天惊扰新娘'
    )
    endings = '\n'.join(config.allowed_endings)
    self.assertNotIn('陆屿或沈玉', endings)
    self.assertNotIn('和盘托出', endings)
    self.assertIn('不要求沈玉或陆屿主动披露', endings)
    self.assertIn('苏晴保持体面并自主明确决定', endings)

  def test_revised_endings_reach_matching_final_beats_and_real_gm_prompts(self):
    # This exercises actual prefab prompt wiring with a scripted reviewer;
    # it does not establish that a live model respects the ending constraints.
    scenarios = (
        (
            acceptance.make_scenario_two_config,
            acceptance.make_scenario_two_beats,
        ),
        (
            acceptance.make_scenario_three_config,
            acceptance.make_scenario_three_beats,
        ),
    )
    for make_config, make_beats in scenarios:
      story = make_config()
      with self.subTest(story=story.title):
        beats = make_beats()
        final = beats[-1]
        self.assertEqual(final, run_lib.make_final_beat(story.allowed_endings))
        self.assertEqual(
            [(b.min_turns, b.max_turns) for b in beats],
            [(3, 10), (3, 10), (3, 10), (2, 8)],
        )
        config = run_lib.make_run_config(
            story, beats=(final,), total_turn_limit=2
        )
        stub = testing_stubs.PromptStubModel(
            default_line='请先说明这件事的依据。', stage_directions=''
        )
        sim, _, _, _ = run_lib.build_simulation(
            run_config=config,
            model=stub,
            embedder=lambda _: np.ones(16, dtype=np.float32),
        )
        sim.play(premise='', max_steps=2)
        generation = [
            p
            for p in stub.text_prompts
            if 'Output a JSON object with exactly two string fields' in p
        ]
        review = [
            p
            for p in stub.text_prompts
            if '# Story resolution semantic review' in p
        ]
        self.assertEqual(len(generation), 2)
        self.assertEqual(len(review), 2)
        self.assertEqual(len(stub.choice_prompts), 1)
        for ending in story.allowed_endings:
          self.assertIn(ending, final.description)
          self.assertIn(ending, final.done_question)
          for prompt in (*generation, *review, *stub.choice_prompts):
            self.assertIn(ending, prompt)

  def test_scene_roundtrip(self):
    scenes = story_config.make_example_scenes(
        story_config.make_example_config(), num_rounds=24
    )
    for scene in scenes:
      rebuilt = story_scene.scene_from_dict(story_scene.scene_to_dict(scene))
      self.assertEqual(rebuilt.scene_type.name, scene.scene_type.name)
      self.assertEqual(rebuilt.participants, scene.participants)
      self.assertEqual(rebuilt.num_rounds, scene.num_rounds)
      self.assertEqual(rebuilt.premise, scene.premise)
      self.assertEqual(
          rebuilt.scene_type.action_spec, scene.scene_type.action_spec
      )


if __name__ == '__main__':
  unittest.main()
