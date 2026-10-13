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
# WITHOUT WARRANTIES OR ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the deterministic screenplay renderer and tension curve."""

import os
import tempfile
import unittest

from examples.story_enactment import acceptance
from examples.story_enactment import renderer
from examples.story_enactment import story_config

_SCENE_DISPLAY = {
    'interior': True,
    'location': '老宅客厅',
    'time_label': '夜',
}


def _make_turns():
  """Builds the fixed three-turn fixture of spec §7 story turn records.

  The fixture covers a Chinese character name ('林雪'), an emotion label,
  non-empty and empty ``stage_directions``, and a beat-completion step
  (turn 3 has ``beat_id_after_event=None``).
  """
  return [
      {
          'turn_id': 1,
          'scene_id': '故事主线',
          'actor': '林雪',
          'speech': '那页遗嘱根本不在父亲的房间里。',
          'resolved_event': '林雪说：那页遗嘱根本不在父亲的房间里。',
          'stage_directions': '窗外雷声轰隆，烛光猛地一晃。',
          'emotion': '勉强一笑',
          'beat_id_at_action': 'beat_1',
          'beat_id_after_event': 'beat_1',
          'tension': None,
      },
      {
          'turn_id': 2,
          'scene_id': '故事主线',
          'actor': '陈默',
          'speech': '你根本没有打开过那个抽屉。',
          'resolved_event': '陈默说：你根本没有打开过那个抽屉。',
          'stage_directions': '',
          'emotion': None,
          'beat_id_at_action': 'beat_1',
          'beat_id_after_event': 'beat_1',
          'tension': None,
      },
      # Beat-completion step: beat_id_after_event is None. The renderer
      # treats it like any other turn and emits no extra element for it.
      {
          'turn_id': 3,
          'scene_id': '故事主线',
          'actor': '林雪',
          'speech': '那就把抽屉里的东西拿出来给大家看。',
          'resolved_event': '林雪说：那就把抽屉里的东西拿出来给大家看。',
          'stage_directions': '陈默后退半步，撞翻了身后的椅子。',
          'emotion': '咬牙',
          'beat_id_at_action': 'beat_1',
          'beat_id_after_event': None,
          'tension': None,
      },
  ]


_EXPECTED_FOUNTAIN = """\
INT. 老宅客厅 - 夜

@林雪
(勉强一笑)
那页遗嘱根本不在父亲的房间里。

窗外雷声轰隆，烛光猛地一晃。

@陈默
你根本没有打开过那个抽屉。

@林雪
(咬牙)
那就把抽屉里的东西拿出来给大家看。

陈默后退半步，撞翻了身后的椅子。
"""

_EXPECTED_ELEMENTS = [
    ('scene_heading', 'INT. 老宅客厅 - 夜'),
    ('character', '林雪'),
    ('parenthetical', '勉强一笑'),
    ('dialogue', '那页遗嘱根本不在父亲的房间里。'),
    ('action', '窗外雷声轰隆，烛光猛地一晃。'),
    ('character', '陈默'),
    ('dialogue', '你根本没有打开过那个抽屉。'),
    ('character', '林雪'),
    ('parenthetical', '咬牙'),
    ('dialogue', '那就把抽屉里的东西拿出来给大家看。'),
    ('action', '陈默后退半步，撞翻了身后的椅子。'),
]

_EXPECTED_PLAIN_TEXT = """\
INT. 老宅客厅 - 夜

林雪（勉强一笑）：那页遗嘱根本不在父亲的房间里。

窗外雷声轰隆，烛光猛地一晃。

陈默：你根本没有打开过那个抽屉。

林雪（咬牙）：那就把抽屉里的东西拿出来给大家看。

陈默后退半步，撞翻了身后的椅子。
"""


class RenderFountainTest(unittest.TestCase):

  def test_renders_mandated_elements(self):
    text = renderer.render_fountain(_make_turns(), _SCENE_DISPLAY)
    lines = text.split('\n')
    self.assertIn('INT. 老宅客厅 - 夜', lines)
    self.assertIn('@林雪', lines)
    self.assertIn('(勉强一笑)', lines)
    self.assertIn('你根本没有打开过那个抽屉。', lines)

  def test_exact_output(self):
    self.assertEqual(
        renderer.render_fountain(_make_turns(), _SCENE_DISPLAY),
        _EXPECTED_FOUNTAIN,
    )

  def test_show_emotion_false_omits_parenthetical(self):
    text = renderer.render_fountain(
        _make_turns(), _SCENE_DISPLAY, show_emotion=False
    )
    self.assertNotIn(
        'parenthetical', (t for t, _ in renderer.parse_fountain(text))
    )
    self.assertNotIn('(', text)
    self.assertIn('@林雪', text.split('\n'))


class ParseFountainTest(unittest.TestCase):

  def test_element_types_and_contents_match_exactly(self):
    self.assertEqual(
        renderer.parse_fountain(
            renderer.render_fountain(_make_turns(), _SCENE_DISPLAY)
        ),
        _EXPECTED_ELEMENTS,
    )


class RenderPlainTextTest(unittest.TestCase):

  def test_exact_output(self):
    self.assertEqual(
        renderer.render_plain_text(_make_turns(), _SCENE_DISPLAY),
        _EXPECTED_PLAIN_TEXT,
    )

  def test_show_emotion_false_omits_parenthetical(self):
    text = renderer.render_plain_text(
        _make_turns(), _SCENE_DISPLAY, show_emotion=False
    )
    self.assertNotIn('（勉强一笑）', text)
    self.assertIn('陈默：你根本没有打开过那个抽屉。', text.split('\n'))


class DeterminismTest(unittest.TestCase):

  def test_repeated_rendering_is_byte_identical(self):
    for render in (renderer.render_fountain, renderer.render_plain_text):
      first = render(_make_turns(), _SCENE_DISPLAY)
      second = render(_make_turns(), _SCENE_DISPLAY)
      self.assertEqual(first.encode('utf-8'), second.encode('utf-8'))


def _scored_turns():
  """Builds turns with mixed tension scores for the curve tests.

  Turns 3, 5 and 6 carry no score (``tension=None``, e.g. unscored
  post-processing or budget-stopped steps) and must break the curve.
  """
  return [
      {'turn_id': 1, 'tension': 3},
      {'turn_id': 2, 'tension': 5},
      {'turn_id': 3, 'tension': None},
      {'turn_id': 4, 'tension': 8},
      {'turn_id': 5, 'tension': None},
      {'turn_id': 6, 'tension': None},
      {'turn_id': 7, 'tension': 10},
  ]


class TensionSegmentsTest(unittest.TestCase):

  def test_unscored_turns_break_segments_instead_of_counting_as_zero(self):
    self.assertEqual(
        renderer._tension_segments(_scored_turns()),
        [[(1, 3.0), (2, 5.0)], [(4, 8.0)], [(7, 10.0)]],
    )

  def test_empty_turns_yield_no_segments(self):
    self.assertEqual(renderer._tension_segments([]), [])

  def test_all_unscored_turns_yield_no_segments(self):
    turns = [{'turn_id': i, 'tension': None} for i in (1, 2, 3)]
    self.assertEqual(renderer._tension_segments(turns), [])


class RenderTensionCurveTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())

  def _path(self, name):
    return os.path.join(self.tmpdir, name)

  def test_writes_png_file(self):
    path = self._path('curve.png')
    renderer.render_tension_curve(_scored_turns(), path)
    with open(path, 'rb') as handle:
      self.assertEqual(handle.read(8), b'\x89PNG\r\n\x1a\n')
    self.assertGreater(os.path.getsize(path), 0)

  def test_empty_turns_still_write_plot(self):
    path = self._path('empty.png')
    renderer.render_tension_curve([], path)
    with open(path, 'rb') as handle:
      self.assertEqual(handle.read(8), b'\x89PNG\r\n\x1a\n')

  def test_all_unscored_turns_still_write_plot(self):
    path = self._path('unscored.png')
    renderer.render_tension_curve(
        [{'turn_id': i, 'tension': None} for i in (1, 2, 3)], path
    )
    with open(path, 'rb') as handle:
      self.assertEqual(handle.read(8), b'\x89PNG\r\n\x1a\n')

  def test_repeated_rendering_is_byte_identical(self):
    first = self._path('first.png')
    second = self._path('second.png')
    renderer.render_tension_curve(_scored_turns(), first)
    renderer.render_tension_curve(_scored_turns(), second)
    with open(first, 'rb') as handle_one, open(second, 'rb') as handle_two:
      self.assertEqual(handle_one.read(), handle_two.read())


class AcceptanceScenarioTest(unittest.TestCase):

  def _factories(self):
    return (
        acceptance.make_scenario_one_config,
        acceptance.make_scenario_two_config,
        acceptance.make_scenario_three_config,
    )

  def test_scenario_one_reuses_default_example_config(self):
    self.assertEqual(
        acceptance.make_scenario_one_config(),
        story_config.make_example_config(),
    )

  def test_all_scenarios_pass_validation(self):
    for factory in self._factories():
      with self.subTest(factory=factory.__name__):
        self.assertEqual(story_config.validate(factory()), [])

  def test_all_scenarios_meet_appendix_c_floor(self):
    for factory in self._factories():
      with self.subTest(factory=factory.__name__):
        config = factory()
        self.assertGreaterEqual(len(config.gm_only_facts), 1)
        self.assertGreaterEqual(len(config.allowed_environment_events), 6)
        self.assertGreaterEqual(len(config.characters), 4)
        self.assertLessEqual(len(config.characters), 5)
        players = [c for c in config.characters if '玩家角色' in c.identity]
        self.assertEqual(len(players), 1)

  def test_characters_carry_full_profiles(self):
    for factory in self._factories():
      with self.subTest(factory=factory.__name__):
        characters = factory().characters
        secrets = 0
        for character in characters:
          for field in (
              character.identity,
              character.goal,
              character.hard_constraint,
              character.style,
              character.initial_relationship_notes,
          ):
            self.assertTrue(field.strip(), character.name)
          self.assertTrue(all(f.strip() for f in character.public_facts))
          secrets += bool(character.secret.strip())
        self.assertGreaterEqual(secrets, 2)


class ReviewChecklistTest(unittest.TestCase):

  def test_text_contains_all_dimensions_questions_and_record_column(self):
    text = acceptance.review_checklist_text('老宅遗嘱')
    self.assertIn('# 人工评审表 — 老宅遗嘱', text)
    self.assertIn('| 维度 | 检查问题 | 记录 |', text)
    dimensions = (
        '角色区分',
        '因果连续',
        '信息泄漏',
        '对白重复',
        '导演越权',
        '结尾状态',
    )
    for dimension in dimensions:
      self.assertIn(dimension, text)
    self.assertIn('台词是否能盲猜出说话者？', text)
    self.assertIn('resolved_event 是否替玩家角色行动？', text)

  def test_text_states_three_runs_and_user_defined_threshold(self):
    text = acceptance.review_checklist_text('终面疑云')
    self.assertIn('每场景跑 3 次', text)
    self.assertIn('逐项标 turn_id', text)
    self.assertIn('发布前由用户定通过阈值', text)


if __name__ == '__main__':
  unittest.main()
