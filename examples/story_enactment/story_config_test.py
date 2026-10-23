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
            if 'Output a JSON object with exactly one string field' in p
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


class NpcResponseRulesConfigTest(unittest.TestCase):
  """Design 5.2: optional NPC response rules on StoryConfig."""

  def test_example_config_has_public_neighbor_rule(self):
    rules = story_config.make_example_config().npc_response_rules
    self.assertTrue(rules)
    joined = '\n'.join(rules)
    self.assertIn('隔壁邻居', joined)
    self.assertIn('白名单敲门事件合法登场', joined)
    self.assertIn('不披露屋内秘密', joined)
    self.assertIn('仅"准备告知"不算已经说出', joined)

  def test_roundtrip_keeps_rules_and_default_is_empty(self):
    config = story_config.make_example_config()
    d = config.to_dict()
    self.assertIn('npc_response_rules', d)
    self.assertEqual(story_config.StoryConfig.from_dict(d), config)
    empty = dataclasses.replace(config, npc_response_rules=())
    self.assertEqual(empty.to_dict()['npc_response_rules'], [])

  def test_old_dict_missing_key_reads_empty(self):
    d = story_config.make_example_config().to_dict()
    del d['npc_response_rules']
    config = story_config.StoryConfig.from_dict(d)
    self.assertEqual(config.npc_response_rules, ())

  def test_validate_rejects_bad_rule_structures(self):
    config = story_config.make_example_config()
    for bad in (
        '邻居规则',
        {'邻居可回应': '不是规则序列'},
        None,
        42,
        ['  '],
        [42],
        ['邻居可回应', None],
    ):
      with self.subTest(bad=bad):
        # 直接构造 dataclass 也要验证；类型注解不是运行期校验。
        invalid = dataclasses.replace(config, npc_response_rules=bad)
        problems = ' '.join(story_config.validate(invalid))
        self.assertIn('npc_response_rules', problems)

  def test_from_dict_rejects_before_tuple_conversion(self):
    base = story_config.make_example_config().to_dict()
    for bad in (
        '邻居规则',
        {'邻居可回应': '不是规则序列'},
        None,
        42,
        ['  '],
        [42],
        ['邻居可回应', None],
    ):
      with self.subTest(bad=bad):
        with self.assertRaisesRegex(ValueError, 'npc_response_rules'):
          story_config.StoryConfig.from_dict(
              {**base, 'npc_response_rules': bad}
          )

  def test_from_dict_accepts_list_tuple_and_empty_sequences(self):
    base = story_config.make_example_config().to_dict()
    for raw in ([], (), ['邻居的公开回应规则'], ('邻居的公开回应规则',)):
      with self.subTest(raw=raw):
        loaded = story_config.StoryConfig.from_dict(
            {**base, 'npc_response_rules': raw}
        )
        self.assertEqual(loaded.npc_response_rules, tuple(raw))


class ActionSpecModeTest(unittest.TestCase):

  def test_speech_only_spec_requests_dialogue_only(self):
    spec = story_config.make_scene_action_spec('speech_only')
    self.assertIn('现在说什么？只输出自己的对白正文', spec.call_to_action)

  def test_speech_and_action_spec_requests_the_json_contract(self):
    spec = story_config.make_scene_action_spec('speech_and_action')
    for fragment in ('JSON', 'speech', 'action_intent', '两者不能同时为空'):
      self.assertIn(fragment, spec.call_to_action)

  def test_speech_and_action_spec_guides_one_completable_step(self):
    spec = story_config.make_scene_action_spec('speech_and_action')
    prompt = spec.call_to_action
    # A1（设计 4）：执行/等待/拒绝区分与不预写结果。
    for fragment in (
        '区分正在执行、等待条件和明确拒绝',
        '缺少谁的什么回应或哪个尚未满足的条件',
        '不要无理由重新提出同一前置条件',
        '不预写成功结果',
        '两者不能同时为空',
    ):
      self.assertIn(fragment, prompt)
    # 程序克制：不必每轮新增程序步骤；"准备"只授权准备。
    self.assertIn('不必每轮新增核验、登记或签认步骤', prompt)
    self.assertIn('"准备核验"只是准备', prompt)
    # 将来承诺不写成已执行。
    self.assertIn('不把对白中的将来承诺写成已执行动作', prompt)
    # stub 路由指纹保持（testing_stubs 依赖）。
    self.assertIn('现在说什么、做什么', prompt)
    self.assertRegex(prompt, r'根据你已知的信息，.+? 现在说什么')

  def test_speech_only_spec_stays_dialogue_only(self):
    spec = story_config.make_scene_action_spec('speech_only')
    self.assertNotIn('action_intent', spec.call_to_action)
    self.assertNotIn('区分正在执行', spec.call_to_action)

  def test_unknown_mode_raises(self):
    with self.assertRaises(ValueError):
      story_config.make_scene_action_spec('mime')


class ExampleFactsTest(unittest.TestCase):
  """Evidence grounding facts required by spec sections 5 and 11."""

  def test_cabinet_needs_two_keys_is_public(self):
    config = story_config.make_example_config()
    self.assertTrue(any('两把钥匙' in fact for fact in config.world_facts))

  def test_old_will_date_is_gm_only(self):
    config = story_config.make_example_config()
    self.assertTrue(
        any('二〇一九年三月十二日' in fact for fact in config.gm_only_facts)
    )


if __name__ == '__main__':
  unittest.main()
