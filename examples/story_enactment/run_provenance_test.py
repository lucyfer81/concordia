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

"""Checks that real run boundaries archive sources before model requests."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from examples.story_enactment import run
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs


class RunProvenanceTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.directory = self.enterContext(tempfile.TemporaryDirectory())
    self.model = testing_stubs.PromptStubModel()
    original = self.model.sample_text

    def checked_sample(*args, **kwargs):
      manifest = self._manifest()
      self.assertTrue(manifest['invocations'])
      self.assertTrue(manifest['invocations'][-1]['source_snapshot'])
      return original(*args, **kwargs)

    self.enterContext(
        mock.patch.object(self.model, 'sample_text', side_effect=checked_sample)
    )
    self.enterContext(
        mock.patch.object(
            run, 'make_language_model', return_value=(self.model, ())
        )
    )

  def _manifest(self):
    return json.loads(
        (Path(self.directory) / 'run_manifest.json').read_text(
            encoding='utf-8', errors='replace'
        )
    )

  def test_archive_exists_before_first_request_and_carries_effective_config(
      self,
  ):
    result = run.run(
        self.directory,
        disable_language_model=True,
        total_turn_limit=1,
    )
    self.assertEqual(result.committed_turn_id, 1)
    invocation = self._manifest()['invocations'][0]
    self.assertEqual(invocation['committed_turn_id_at_start'], 0)
    self.assertEqual(
        invocation['run_config']['model_config']['api_type'], 'stub'
    )
    self.assertIn(
        'concordia/contrib/components/game_master/story_resolution.py',
        invocation['source_snapshot'],
    )

  def test_resume_preserves_snapshot_even_when_no_more_steps_are_run(self):
    run.run(
        self.directory,
        disable_language_model=True,
        total_turn_limit=1,
    )
    first = self._manifest()['invocations'][0]
    calls = len(self.model.text_prompts)
    result = run.run(
        self.directory,
        disable_language_model=True,
        resume=True,
    )
    self.assertEqual(result.committed_turn_id, 1)
    self.assertEqual(len(self.model.text_prompts), calls)
    invocations = self._manifest()['invocations']
    self.assertEqual(len(invocations), 2)
    self.assertEqual(invocations[0], first)
    self.assertEqual(invocations[1]['committed_turn_id_at_start'], 1)
    self.assertTrue(invocations[1]['resume'])

  def test_environment_permissions_reach_review_in_every_mode(self):
    story = story_config.make_example_config()
    for mode in run.MODES:
      with self.subTest(mode=mode):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.model.text_prompts.clear()
        result = run.run(
            self.directory,
            disable_language_model=True,
            run_config=run.make_run_config(
                story, mode=mode, total_turn_limit=1
            ),
        )
        self.assertEqual(result.committed_turn_id, 1)
        reviews = [
            prompt
            for prompt in self.model.text_prompts
            if '# Story resolution semantic review' in prompt
        ]
        self.assertEqual(len(reviews), 1)
        constraints_text = reviews[0].split('## GM-only constraints\n', 1)[1]
        constraints, _ = json.JSONDecoder().raw_decode(
            constraints_text.lstrip()
        )
        self.assertEqual(
            constraints['allowed_environment_events'],
            list(story.allowed_environment_events),
        )


if __name__ == '__main__':
  unittest.main()
