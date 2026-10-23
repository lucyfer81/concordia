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


class SourceArchiveTest(unittest.TestCase):
  """The adapter sources of the run chain are archived verbatim.

  Unlike ``ProvenanceTest`` these checks run against the real repository
  sources (no patched root), so they pin the modules the Phase B/C/D
  adapters added to the enactment chain.
  """

  def test_new_adapters_have_content_and_hashes(self):
    with tempfile.TemporaryDirectory() as directory:
      provenance.archive_invocation(
          directory,
          run_config={},
          committed_turn_id=0,
          resume=False,
      )
      manifest = json.loads(
          (Path(directory) / 'run_manifest.json').read_text(
              encoding='utf-8', errors='replace'
          )
      )
      snapshot = manifest['invocations'][0]['source_snapshot']
      for relative in (
          'concordia/contrib/components/game_master/story_action.py',
          (
              'concordia/contrib/components/game_master/'
              'story_action_adjudication.py'
          ),
          'concordia/contrib/components/game_master/repetition_push.py',
          # The final reason resolution closing every invocation.
          'concordia/contrib/components/game_master/story_terminator.py',
      ):
        source = (provenance._SOURCE_ROOT / relative).read_bytes()  # pylint: disable=protected-access
        entry = snapshot[relative]
        self.assertTrue(entry['text'].strip(), relative)
        self.assertEqual(
            entry['sha256'], hashlib.sha256(source).hexdigest(), relative
        )
        self.assertEqual(
            entry['text'], source.decode('utf-8', errors='replace'), relative
        )


class RecordedUsageTest(unittest.TestCase):
  """Summaries are a budget-relevant usage record of their own."""

  def setUp(self):
    super().setUp()
    self.directory = self.enterContext(tempfile.TemporaryDirectory())

  def _write_summary(self, invocation_id: str, **counters):
    provenance.write_process_summary(
        self.directory,
        invocation_id=invocation_id,
        summary={
            'schema_version': 1,
            'invocation_id': invocation_id,
            **counters,
        },
    )

  def test_max_across_summaries_restores_true_cumulative_usage(self):
    # No summaries directory yet: nothing recorded beyond a checkpoint.
    self.assertEqual(
        provenance.restore_recorded_usage(self.directory, attempt_limit=30),
        {'used_model_attempts': 0, 'used_api_retries': 0},
    )
    self._write_summary(
        'a', used_model_attempts_at_end=15, used_api_retries_at_end=0
    )
    self._write_summary(
        'b', used_model_attempts_at_end=24, used_api_retries_at_end=2
    )
    self.assertEqual(
        provenance.restore_recorded_usage(self.directory, attempt_limit=30),
        {'used_model_attempts': 24, 'used_api_retries': 2},
    )
    # Unpublished temporary files are not summaries.
    (Path(self.directory) / 'process_summaries' / '.summary-tmp').write_text(
        'partial', encoding='utf-8'
    )
    self.assertEqual(
        provenance.restore_recorded_usage(self.directory, attempt_limit=30),
        {'used_model_attempts': 24, 'used_api_retries': 2},
    )

  def test_undeterminable_usage_refuses_instead_of_falling_back(self):
    cases = (
        ('corrupt.json', 'not json'),
        ('no-counters.json', '{"schema_version": 1}'),
        (
            'over-limit.json',
            (
                '{"schema_version": 1, "used_model_attempts_at_end": 99,'
                ' "used_api_retries_at_end": 0}'
            ),
        ),
        (
            'bad-retries.json',
            (
                '{"schema_version": 1, "used_model_attempts_at_end": 5,'
                ' "used_api_retries_at_end": 6}'
            ),
        ),
    )
    for name, text in cases:
      with self.subTest(name=name):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        summaries = Path(directory) / 'process_summaries'
        summaries.mkdir()
        (summaries / name).write_text(text, encoding='utf-8')
        with self.assertRaises(ValueError):
          provenance.restore_recorded_usage(directory, attempt_limit=30)

  def test_archived_invocation_without_summary_refuses(self):
    # An invocation that declared a summary but never published one (a
    # failed or killed finalization) leaves its consumption
    # undeterminable: zero is not a valid reading, so the restore
    # refuses regardless of whether the summaries directory exists.
    directory = self.enterContext(tempfile.TemporaryDirectory())
    provenance.archive_invocation(
        directory, run_config={}, committed_turn_id=0, resume=False
    )
    with self.assertRaisesRegex(ValueError, 'cannot be determined'):
      provenance.restore_recorded_usage(directory, attempt_limit=30)

    # Legacy entries predate invocation ids and summary declarations;
    # they demand nothing and stay resumable from their checkpoint.
    manifest_path = Path(directory) / 'run_manifest.json'
    manifest = json.loads(
        manifest_path.read_text(encoding='utf-8', errors='replace')
    )
    for entry in manifest['invocations']:
      entry.pop('invocation_id', None)
      entry.pop('summary_path', None)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    self.assertEqual(
        provenance.restore_recorded_usage(directory, attempt_limit=30),
        {'used_model_attempts': 0, 'used_api_retries': 0},
    )


if __name__ == '__main__':
  unittest.main()
