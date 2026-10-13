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

"""Tests for the deterministic Fountain/plain-text screenplay renderer."""

import unittest

from examples.story_enactment import renderer

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


if __name__ == '__main__':
  unittest.main()
