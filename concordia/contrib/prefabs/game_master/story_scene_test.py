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

"""Tests for the shared scene codec."""

import json
import unittest

from concordia.contrib.prefabs.game_master import story_scene
from concordia.typing import entity as entity_lib
from concordia.typing import scene as scene_lib

_CALL_TO_ACTION = 'What does {name} say next, in their own words?'


def _make_scene() -> scene_lib.SceneSpec:
  return scene_lib.SceneSpec(
      scene_type=scene_lib.SceneTypeSpec(
          name='main',
          action_spec=entity_lib.free_action_spec(
              call_to_action=_CALL_TO_ACTION, tag='speech'
          ),
      ),
      participants=('Alice', 'Bob'),
      num_rounds=24,
      premise={'Alice': ('The rehearsal stopped.',), 'Bob': ()},
  )


class StorySceneTest(unittest.TestCase):

  def test_roundtrip_restores_scene_fields(self):
    scene = _make_scene()
    rebuilt = story_scene.scene_from_dict(story_scene.scene_to_dict(scene))
    self.assertEqual(rebuilt.scene_type.name, scene.scene_type.name)
    self.assertEqual(rebuilt.scene_type.game_master_name, None)
    self.assertEqual(
        rebuilt.scene_type.action_spec, scene.scene_type.action_spec
    )
    self.assertEqual(rebuilt.participants, scene.participants)
    self.assertEqual(rebuilt.num_rounds, scene.num_rounds)
    self.assertEqual(rebuilt.premise, scene.premise)

  def test_encoded_action_spec_keeps_free_fields(self):
    encoded = story_scene.scene_to_dict(_make_scene())
    spec = encoded['scene_type']['action_spec']
    self.assertEqual(set(spec), {'call_to_action', 'output_type', 'tag'})
    self.assertEqual(spec['output_type'], 'free')
    self.assertEqual(spec['call_to_action'], _CALL_TO_ACTION)
    self.assertEqual(spec['tag'], 'speech')

  def test_encoding_is_json_serializable(self):
    encoded = story_scene.scene_to_dict(_make_scene())
    self.assertIsNotNone(json.dumps(encoded))

  def test_roundtrip_per_participant_action_spec(self):
    scene = scene_lib.SceneSpec(
        scene_type=scene_lib.SceneTypeSpec(
            name='main',
            action_spec={
                'Alice': entity_lib.free_action_spec(
                    call_to_action=_CALL_TO_ACTION, tag='speech'
                ),
            },
        ),
        participants=('Alice',),
        num_rounds=1,
        premise={'Alice': ('The rehearsal stopped.',)},
    )
    rebuilt = story_scene.scene_from_dict(story_scene.scene_to_dict(scene))
    self.assertEqual(
        rebuilt.scene_type.action_spec, scene.scene_type.action_spec
    )


if __name__ == '__main__':
  unittest.main()
