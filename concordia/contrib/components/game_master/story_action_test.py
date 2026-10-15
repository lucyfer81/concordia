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

"""Tests for the shared speech-and-action codec."""

import json
import unittest

from concordia.contrib.components.game_master import story_action


class DecodeActionTest(unittest.TestCase):

  def test_speech_only_payload_decodes(self):
    raw = json.dumps(
        {'speech': '我现在核对原件上的日期。', 'action_intent': ''}
    )
    speech, intent = story_action.decode_action(raw)
    self.assertEqual(speech, '我现在核对原件上的日期。')
    self.assertEqual(intent, '')

  def test_action_only_payload_decodes(self):
    raw = json.dumps({'speech': '', 'action_intent': '用钥匙尝试开柜。'})
    speech, intent = story_action.decode_action(raw)
    self.assertEqual(speech, '')
    self.assertEqual(intent, '用钥匙尝试开柜。')

  def test_fenced_payload_decodes(self):
    raw = json.dumps({'speech': 'x', 'action_intent': 'y'})
    for fenced in (f'```json\n{raw}\n```', f'```\n{raw}\n```'):
      with self.subTest(fenced=fenced):
        speech, intent = story_action.decode_action(fenced)
        self.assertEqual((speech, intent), ('x', 'y'))

  def test_invalid_json_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action('not json at all')

  def test_non_object_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action('["speech"]')

  def test_missing_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(json.dumps({'speech': 'x'}))

  def test_extra_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(
          json.dumps({'speech': 'x', 'action_intent': '', 'note': 'y'})
      )

  def test_non_string_field_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(json.dumps({'speech': 1, 'action_intent': ''}))

  def test_both_empty_raises(self):
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(
          json.dumps({'speech': '', 'action_intent': ''})
      )

  def test_duplicate_key_raises(self):
    raw = '{"speech": "a", "speech": "b", "action_intent": ""}'
    with self.assertRaises(story_action.ActionDecodeError):
      story_action.decode_action(raw)

  def test_inner_whitespace_is_preserved_verbatim(self):
    raw = json.dumps(
        {'speech': '  第一段。\n\n第二段。  ', 'action_intent': ''}
    )
    speech, _ = story_action.decode_action(raw)
    self.assertEqual(speech, '  第一段。\n\n第二段。  ')


class ActionModeTest(unittest.TestCase):

  def test_modes_are_the_two_contracts(self):
    self.assertEqual(
        story_action.ACTION_MODES, ('speech_only', 'speech_and_action')
    )

  def test_validate_action_mode_rejects_unknown(self):
    story_action.validate_action_mode('speech_only')
    story_action.validate_action_mode('speech_and_action')
    with self.assertRaises(ValueError):
      story_action.validate_action_mode('mime')


if __name__ == '__main__':
  unittest.main()
