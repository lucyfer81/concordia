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

"""Tests for the final dual-track output step."""

import json
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_resolution
from concordia.document import interactive_document
from concordia.testing import mock_model


class FinalOutputStepTest(unittest.TestCase):

  def _run(self, response: str, speech: str | None):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    model = mock_model.MockModel(response)
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(director)
    result = step(
        chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    return result, director

  def test_returns_string_and_stores_dual_track(self):
    payload = json.dumps({
        'resolved_event': 'Alice: "I hid the letter under the stairs." '
                          'Thunder rattles the windows.',
        'stage_directions': 'Thunder rattles the windows.'})
    result, director = self._run(
        payload, speech='I hid the letter under the stairs.')
    self.assertEqual(result,
                     'Alice: "I hid the letter under the stairs." '
                     'Thunder rattles the windows.')
    director.set_validated_resolution.assert_called_once_with(
        'Alice: "I hid the letter under the stairs." Thunder rattles the windows.',
        'Thunder rattles the windows.')

  def test_invalid_json_raises_story_output_error(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run('not json at all', speech=None)

  def test_missing_fields_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(json.dumps({'resolved_event': 'x'}), speech=None)

  def test_rewritten_dialogue_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice claims she burned the letter.',
              'stage_directions': ''}),
          speech='I hid the letter under the stairs.')

  def test_non_string_field_values_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 123,
              'stage_directions': 'Thunder rattles the windows.'}),
          speech=None)
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice slams the door.',
              'stage_directions': ['Thunder rattles the windows.']}),
          speech=None)

  def test_extra_fields_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'x',
              'stage_directions': 'y',
              'tension': 5}),
          speech=None)

  def test_paraphrased_dialogue_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': (
                  'Alice: "I had hidden the letter beneath the staircase."'),
              'stage_directions': ''}),
          speech='I hid the letter under the stairs.')

  def test_missing_speaker_name_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': '"I hid the letter under the stairs."',
              'stage_directions': ''}),
          speech='I hid the letter under the stairs.')

  def test_invalid_output_is_not_stored_in_director(self):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = (
        'I hid the letter under the stairs.')
    model = mock_model.MockModel(
        json.dumps({
            'resolved_event': 'Alice claims she burned the letter.',
            'stage_directions': ''}))
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(director)
    with self.assertRaises(story_resolution.StoryOutputError):
      step(chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    director.set_validated_resolution.assert_not_called()

  def test_speechless_turn_returns_resolved_event(self):
    result, director = self._run(
        json.dumps({
            'resolved_event': 'Alice slams the door.',
            'stage_directions': 'The door slams shut.'}),
        speech=None)
    self.assertEqual(result, 'Alice slams the door.')
    director.set_validated_resolution.assert_called_once_with(
        'Alice slams the door.', 'The door slams shut.')


if __name__ == '__main__':
  unittest.main()
