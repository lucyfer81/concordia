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

from examples.story_enactment import story_config


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
