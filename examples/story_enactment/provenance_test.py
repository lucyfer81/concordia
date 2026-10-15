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

"""Tests for immutable invocation history and credential-free provenance."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from examples.story_enactment import provenance


class ProvenanceTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.directory = self.enterContext(tempfile.TemporaryDirectory())
    self.source_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
    self.source_file = self.source_root / 'prompt.py'
    self.source_file.write_text('PROMPT = "核验"\n', encoding='utf-8')
    self.enterContext(
        mock.patch.object(provenance, '_SOURCE_ROOT', self.source_root)
    )
    self.enterContext(
        mock.patch.object(provenance, '_SOURCE_PATHS', ('prompt.py',))
    )
    self.config = {
        'model_config': {'model_name': 'review-model', 'api_key': 'PRIVATE'},
        'story': {'secret': 'The will was swapped.'},
        'nested': [{'Authorization': 'BEARER_PRIVATE'}],
    }

  def _archive(self, *, resume=False, turn=0):
    provenance.archive_invocation(
        self.directory,
        run_config=self.config,
        committed_turn_id=turn,
        resume=resume,
    )

  def _read(self):
    return json.loads(
        (Path(self.directory) / 'run_manifest.json').read_text(
            encoding='utf-8', errors='replace'
        )
    )

  def test_archives_exact_source_and_config_without_credentials(self):
    self._archive()
    manifest = self._read()
    invocation = manifest['invocations'][0]
    self.assertTrue(manifest['history_complete'])
    snapshot = invocation['source_snapshot']['prompt.py']
    source = self.source_file.read_text(encoding='utf-8')
    self.assertEqual(snapshot['text'], source)
    self.assertEqual(
        snapshot['sha256'], hashlib.sha256(source.encode('utf-8')).hexdigest()
    )
    self.assertEqual(invocation['run_config']['story'], self.config['story'])
    self.assertNotIn('PRIVATE', json.dumps(manifest))
    self.assertEqual(self.config['model_config']['api_key'], 'PRIVATE')

  def test_resume_preserves_original_and_records_changed_sources(self):
    self._archive()
    first = self._read()['invocations'][0]
    self.source_file.write_text('PROMPT = "公开证据"\n', encoding='utf-8')
    self._archive(resume=True, turn=3)
    manifest = self._read()
    self.assertEqual(manifest['invocations'][0], first)
    second = manifest['invocations'][1]
    self.assertEqual(second['committed_turn_id_at_start'], 3)
    self.assertNotEqual(second['source_version'], first['source_version'])
    self.assertTrue(manifest['history_complete'])

  def test_snapshot_preserves_source_line_endings(self):
    source = b'PROMPT = "first"\r\nMORE = "second"\r\n'
    self.source_file.write_bytes(source)
    self._archive()
    snapshot = self._read()['invocations'][0]['source_snapshot']['prompt.py']
    self.assertEqual(snapshot['text'].encode('utf-8'), source)
    self.assertEqual(snapshot['sha256'], hashlib.sha256(source).hexdigest())

  def test_old_run_is_explicitly_marked_as_missing_original_provenance(self):
    self._archive(resume=True, turn=2)
    self.assertFalse(self._read()['history_complete'])

  def test_first_run_does_not_overwrite_existing_manifest(self):
    self._archive()
    before = self._read()
    with self.assertRaisesRegex(ValueError, 'already exists'):
      self._archive()
    self.assertEqual(self._read(), before)


if __name__ == '__main__':
  unittest.main()
