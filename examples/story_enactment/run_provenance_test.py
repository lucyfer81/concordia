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

import contextlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.prefabs.simulation import generic as simulation_lib

from examples.story_enactment import provenance
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
      entry = manifest['invocations'][-1]
      self.assertTrue(entry['source_snapshot'])
      # The invocation is fully identifiable before any model request.
      self.assertTrue(entry['invocation_id'])
      self.assertEqual(
          entry['summary_path'],
          f"process_summaries/{entry['invocation_id']}.json",
      )
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


class SimulatedInterruption(BaseException):
  """A crash stand-in that the runner must let propagate untouched."""


_REVIEW_MARK = '# Story resolution semantic review'
_PUSH_EVENT = '邮差冒雨敲门，送来一封老人生前寄出的旧信'
_ARCHIVE_FILES = (
    'run_manifest.json',
    'checkpoint.json',
    'story_turns.jsonl',
    'process_usage.json',
    'summary.json',
    'script.fdx',
    'script.fountain',
    'script.txt',
)


class _ScriptedReviewStub(testing_stubs.PromptStubModel):
  """Answers semantic reviews from a per-call script."""

  def __init__(self, *, review_script: tuple[str, ...], **kwargs):
    super().__init__(**kwargs)
    self._review_script = list(review_script)
    self._review_calls = 0

  def _route_text(self, prompt):
    if _REVIEW_MARK in prompt:
      answer = self._review_script[
          min(self._review_calls, len(self._review_script) - 1)
      ]
      self._review_calls += 1
      return answer
    return super()._route_text(prompt)


def _fault_stub():
  """Approves two reviews, then fails turn three's review with bad JSON."""
  return _ScriptedReviewStub(
      review_script=(
          '{"valid": true, "reason": "ok"}',
          '{"valid": true, "reason": "ok"}',
          'not valid json',
      ),
      scoring_answer='5',
      repetition_answer='Yes',
      push_events=_PUSH_EVENT,
      push_merged='',
      stage_directions='',
  )


def _healthy_stub():
  return _ScriptedReviewStub(
      review_script=('{"valid": true, "reason": "resumed"}',) * 8,
      scoring_answer='5',
      repetition_answer='Yes',
      push_events=_PUSH_EVENT,
      push_merged='',
      stage_directions='',
  )


@contextlib.contextmanager
def _interrupt_when(turn_id: int, *, when: str = 'after_commit'):
  """Interrupts the run at a save-boundary point of one story turn."""
  from examples.story_enactment import collector  # pylint: disable=import-outside-toplevel

  real = collector.make_step_callback

  def patched(**kwargs):
    inner = real(**kwargs)
    director = kwargs['drama_director']

    def callback(step_data):
      turn = director.get_last_story_turn()
      if when == 'before_write' and turn and turn['turn_id'] == turn_id:
        raise SimulatedInterruption(f'before write of turn {turn_id}')
      inner(step_data)
      if when == 'after_commit' and turn and turn['turn_id'] == turn_id:
        raise SimulatedInterruption(f'after commit of turn {turn_id}')

    return callback

  with mock.patch.object(collector, 'make_step_callback', patched):
    yield


class ProcessSummaryTest(unittest.TestCase):
  """Every real invocation archives its own immutable process summary.

  The runs below use the real ``run()``/collector/resume paths with the
  deterministic stub; the fault stub fails turn three's semantic review
  so the failure evidence (intervention candidate, traces, rejection)
  must survive in the failed invocation's private summary while the
  commit boundary stays at two turns.
  """

  def setUp(self):
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)

  def _directory(self, name: str) -> str:
    path = os.path.join(self._tmp.name, name)
    os.makedirs(path, exist_ok=True)
    return path

  def _director_config(
      self,
      *,
      total_turn_limit: int = 4,
      total_model_attempt_limit: int = 100,
  ) -> dict:
    story = story_config.make_example_config()
    config = run.make_run_config(
        story,
        total_turn_limit=total_turn_limit,
        total_model_attempt_limit=total_model_attempt_limit,
        model_config={
            'api_type': 'stub',
            'prompt_stub': {'completion_answers': {}},
        },
    )
    config['beat_dicts'] = [
        drama_director.Beat(
            id='b1',
            description='节拍 b1：出现可供角色质疑的新情况',
            tension_range=(3, 6),
            done_question='节拍 b1 的完成条件是否已经出现？',
            min_turns=2,
            max_turns=99,
        ).to_dict()
    ]
    config['scene_dicts'][0]['num_rounds'] = 99
    config['mode'] = 'director'
    config['director_params'] = {
        'eval_interval': 1,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': True,
        'cooldown_turns': 3,
        'allowed_environment_events': list(story.allowed_environment_events),
        'immutable_facts': list(story.immutable_facts),
        'repetition_push_enabled': True,
        'repetition_check_interval': 3,
    }
    return config

  def _run(self, directory: str, stub, *, config=None, **kwargs):
    with mock.patch.object(run, 'make_language_model', return_value=(stub, ())):
      return run.run(directory, run_config=config, **kwargs)

  def _manifest(self, directory: str) -> dict:
    with open(
        os.path.join(directory, 'run_manifest.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def _summary_path(self, directory: str, invocation_id: str) -> str:
    return os.path.join(directory, 'process_summaries', f'{invocation_id}.json')

  def _summary(self, directory: str, invocation_id: str) -> dict:
    with open(
        self._summary_path(directory, invocation_id), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def _usage(self, directory: str) -> dict:
    with open(
        os.path.join(directory, 'process_usage.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def _envelope(self, directory: str) -> dict:
    with open(
        os.path.join(directory, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      return json.load(handle)

  def _read(self, path: str) -> bytes:
    with open(path, 'rb') as handle:
      return handle.read()

  def test_archive_exists_before_first_request(self):
    directory = self._directory('run')
    stub = testing_stubs.PromptStubModel(completion_answers={})
    checks = []
    original_text = stub.sample_text
    original_choice = stub.sample_choice

    def checked_text(prompt, **kwargs):
      entry = self._manifest(directory)['invocations'][-1]
      checks.append(
          bool(
              entry['invocation_id']
              and entry['summary_path']
              and entry['started_at_utc']
              and entry['source_snapshot']
              and not entry['resume']
          )
      )
      return original_text(prompt, **kwargs)

    def checked_choice(prompt, responses, **kwargs):
      entry = self._manifest(directory)['invocations'][-1]
      checks.append(bool(entry['invocation_id']))
      return original_choice(prompt, responses, **kwargs)

    with mock.patch.object(
        stub, 'sample_text', side_effect=checked_text
    ), mock.patch.object(stub, 'sample_choice', side_effect=checked_choice):
      result = self._run(
          directory,
          stub,
          config=self._director_config(total_turn_limit=1),
      )
    self.assertEqual(result.committed_turn_id, 1)
    self.assertTrue(checks)
    self.assertTrue(all(checks))

  def test_success_and_failure_each_get_immutable_summary(self):
    directory = self._directory('run')
    config = self._director_config()
    first = self._run(directory, _fault_stub(), config=config)
    self.assertEqual(first.reason, 'error')
    first_entry = self._manifest(directory)['invocations'][0]
    first_id = first_entry['invocation_id']
    first_bytes = self._read(self._summary_path(directory, first_id))

    resumed = self._run(
        directory, _healthy_stub(), resume=True, mode='director'
    )
    self.assertEqual(resumed.reason, 'step_limit')
    self.assertEqual(resumed.committed_turn_id, 4)

    entries = self._manifest(directory)['invocations']
    self.assertEqual(len(entries), 2)
    second_id = entries[1]['invocation_id']
    self.assertNotEqual(second_id, first_id)
    # The old summary's bytes are untouched and each manifest entry
    # points at its own summary file.
    self.assertEqual(
        self._read(self._summary_path(directory, first_id)), first_bytes
    )
    for entry in entries:
      self.assertEqual(
          entry['summary_path'],
          f"process_summaries/{entry['invocation_id']}.json",
      )
    first_summary = self._summary(directory, first_id)
    second_summary = self._summary(directory, second_id)
    self.assertEqual(first_summary['invocation_id'], first_id)
    self.assertEqual(first_summary['reason'], 'error')
    self.assertFalse(first_summary['resume'])
    self.assertTrue(second_summary['resume'])
    self.assertEqual(second_summary['committed_turn_id_at_start'], 2)
    self.assertEqual(second_summary['committed_turn_id_at_end'], 4)
    self.assertEqual(
        first_summary['source_version'], entries[0]['source_version']
    )
    self.assertEqual(
        second_summary['source_version'], entries[1]['source_version']
    )

  def test_duplicate_summary_id_cannot_overwrite(self):
    directory = self._directory('run')
    result = self._run(
        directory,
        _healthy_stub(),
        config=self._director_config(total_turn_limit=1),
    )
    self.assertEqual(result.committed_turn_id, 1)
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    path = self._summary_path(directory, invocation_id)
    before = self._read(path)
    with self.assertRaisesRegex(ValueError, 'refusing to overwrite'):
      provenance.write_process_summary(
          directory,
          invocation_id=invocation_id,
          summary={'schema_version': 1, 'invocation_id': invocation_id},
      )
    self.assertEqual(self._read(path), before)

  def test_failed_candidate_and_partial_trace_are_archived_without_commit(self):
    directory = self._directory('run')
    result = self._run(directory, _fault_stub(), config=self._director_config())
    self.assertEqual(result.reason, 'error')
    self.assertEqual(result.committed_turn_id, 2)
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    summary = self._summary(directory, invocation_id)
    # The failed turn's private diagnostics survive: the intervention
    # candidate, the repetition trace and the review rejection.
    uncommitted = summary['uncommitted_resolution']
    self.assertIn(_PUSH_EVENT, uncommitted['intervention']['candidate_after'])
    # The trace history holds the recorded intervention candidate and
    # the final review rejection side by side; 'repetition_push' would
    # only appear on an API/budget failure mid-helper.
    stages = [record['stage'] for record in uncommitted['history']]
    self.assertIn('intervention_repetition', stages)
    self.assertIn('semantic_review', stages)
    self.assertTrue(any(record['error'] for record in uncommitted['history']))
    self.assertEqual(summary['exception']['type'], 'StoryOutputError')
    self.assertEqual(summary['committed_turn_id_at_end'], 2)
    # The commit boundary and the public story outputs hold only the
    # two committed turns; the failed candidate never reaches them.
    self.assertEqual(self._envelope(directory)['committed_turn_id'], 2)
    with open(
        os.path.join(directory, 'story_turns.jsonl'), encoding='utf-8'
    ) as handle:
      records = [json.loads(line) for line in handle if line.strip()]
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    for filename in ('script.fdx', 'script.fountain', 'script.txt'):
      with open(os.path.join(directory, filename), encoding='utf-8') as handle:
        self.assertNotIn(_PUSH_EVENT, handle.read(), filename)

  def test_caught_interruption_preserves_summary_and_rethrows(self):
    directory = self._directory('run')
    config = self._director_config()
    with _interrupt_when(3):
      with self.assertRaises(SimulatedInterruption):
        self._run(directory, _healthy_stub(), config=config)
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    summary = self._summary(directory, invocation_id)
    self.assertEqual(summary['reason'], 'interrupted')
    self.assertEqual(summary['exception']['type'], 'SimulatedInterruption')
    self.assertEqual(summary['committed_turn_id_at_end'], 3)
    self.assertEqual(
        self._usage(directory)['used_model_attempts'],
        summary['used_model_attempts_at_end'],
    )

    # A KeyboardInterrupt is archived the same way and still propagates.
    crash_dir = self._directory('crash')
    with mock.patch.object(
        simulation_lib.Simulation, 'play', side_effect=KeyboardInterrupt
    ):
      with self.assertRaises(KeyboardInterrupt):
        self._run(crash_dir, _healthy_stub(), config=self._director_config())
    crash_id = self._manifest(crash_dir)['invocations'][0]['invocation_id']
    crash_summary = self._summary(crash_dir, crash_id)
    self.assertEqual(crash_summary['reason'], 'interrupted')
    self.assertEqual(crash_summary['exception']['type'], 'KeyboardInterrupt')
    self.assertEqual(crash_summary['committed_turn_id_at_end'], 0)

  def test_zero_step_resume_gets_summary_without_model_calls(self):
    directory = self._directory('run')
    stub = _healthy_stub()
    result = self._run(
        directory, stub, config=self._director_config(total_turn_limit=1)
    )
    self.assertEqual(result.committed_turn_id, 1)
    calls = len(stub.text_prompts) + len(stub.choice_prompts)
    resumed = self._run(
        directory, _healthy_stub(), resume=True, mode='director'
    )
    self.assertEqual(resumed.committed_turn_id, 1)
    self.assertEqual(resumed.attempts_used_this_process, 0)
    entries = self._manifest(directory)['invocations']
    self.assertEqual(len(entries), 2)
    self.assertNotEqual(
        entries[1]['invocation_id'], entries[0]['invocation_id']
    )
    summary = self._summary(directory, entries[1]['invocation_id'])
    self.assertTrue(summary['resume'])
    self.assertEqual(summary['committed_turn_id_at_start'], 1)
    self.assertEqual(summary['committed_turn_id_at_end'], 1)
    self.assertEqual(summary['attempts_used_this_process'], 0)
    self.assertEqual(summary['api_retries_used_this_process'], 0)
    self.assertEqual(summary['reason'], resumed.reason)
    # No model request happened during the zero-step resume.
    fresh = self._read(
        self._summary_path(directory, entries[1]['invocation_id'])
    )
    self.assertIn(b'"attempts_used_this_process": 0', fresh)

  def test_usage_and_summary_agree_after_failed_resume(self):
    directory = self._directory('run')
    config = self._director_config(total_model_attempt_limit=30)
    first = self._run(directory, _fault_stub(), config=config)
    self.assertEqual(first.reason, 'error')
    self.assertEqual(self._envelope(directory)['used_model_attempts'], 15)
    self.assertEqual(first.used_model_attempts, 24)
    self.assertEqual(self._usage(directory)['used_model_attempts'], 24)
    first_id = self._manifest(directory)['invocations'][0]['invocation_id']
    first_summary = self._summary(directory, first_id)
    self.assertEqual(first_summary['used_model_attempts_at_start'], 0)
    self.assertEqual(first_summary['used_model_attempts_at_end'], 24)
    self.assertEqual(first_summary['attempts_used_this_process'], 24)
    self.assertEqual(
        first_summary['total_model_attempt_limit'],
        config['total_model_attempt_limit'],
    )

    resumed = self._run(
        directory, _healthy_stub(), resume=True, mode='director'
    )
    self.assertEqual(resumed.reason, 'budget_limit')
    # Only the six remaining attempts existed; the cumulative report and
    # the real guard both stop at the original limit of thirty.
    self.assertEqual(resumed.attempts_used_this_process, 6)
    self.assertEqual(resumed.used_model_attempts, 30)
    self.assertEqual(self._usage(directory)['used_model_attempts'], 30)
    second_id = self._manifest(directory)['invocations'][1]['invocation_id']
    second_summary = self._summary(directory, second_id)
    self.assertEqual(second_summary['used_model_attempts_at_start'], 24)
    self.assertEqual(second_summary['used_model_attempts_at_end'], 30)
    self.assertEqual(second_summary['attempts_used_this_process'], 6)
    self.assertEqual(
        second_summary['total_model_attempt_limit'],
        config['total_model_attempt_limit'],
    )

  def test_archive_roundtrip_preserves_failed_turn_usage(self):
    directory = self._directory('run')
    config = self._director_config(total_model_attempt_limit=30)
    first = self._run(directory, _fault_stub(), config=config)
    self.assertEqual(first.reason, 'error')
    for name in _ARCHIVE_FILES:
      self.assertTrue(os.path.exists(os.path.join(directory, name)), name)
    self.assertTrue(os.path.isdir(os.path.join(directory, 'process_summaries')))

    # A full copy of the archive resumes from the true cumulative total
    # (24), never falling back to the checkpoint's committed count (15).
    restored = self._directory('restored')
    shutil.copytree(directory, restored, dirs_exist_ok=True)
    resumed = self._run(restored, _healthy_stub(), resume=True, mode='director')
    self.assertEqual(resumed.reason, 'budget_limit')
    self.assertEqual(resumed.used_model_attempts, 30)
    self.assertEqual(resumed.attempts_used_this_process, 6)
    self.assertEqual(self._usage(restored)['used_model_attempts'], 30)
    # The source directory's manifest history stays untouched.
    self.assertEqual(len(self._manifest(directory)['invocations']), 1)
    self.assertEqual(len(self._manifest(restored)['invocations']), 2)

  def test_resume_restores_budget_from_summaries_when_usage_write_fails(self):
    # The usage sidecar's write fails on the first process, but its
    # process summary still records the true cumulative usage (24 while
    # the checkpoint holds 15): the resume must restore from that
    # summary — only six attempts remain under the original limit of
    # thirty — and never silently fall back to the stale checkpoint,
    # which would grant fifteen more attempts and let the run consume
    # thirty-nine real calls while reporting thirty.
    directory = self._directory('run')
    config = self._director_config(total_model_attempt_limit=30)
    with mock.patch.object(
        run,
        '_write_process_usage',
        side_effect=OSError('usage disk full'),
    ):
      with self.assertRaisesRegex(RuntimeError, 'process summary'):
        self._run(directory, _fault_stub(), config=config)
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    first_summary = self._summary(directory, invocation_id)
    self.assertEqual(first_summary['used_model_attempts_at_end'], 24)
    self.assertFalse(
        os.path.exists(os.path.join(directory, 'process_usage.json'))
    )

    resumed = self._run(
        directory, _healthy_stub(), resume=True, mode='director'
    )
    self.assertEqual(resumed.reason, 'budget_limit')
    self.assertEqual(resumed.used_model_attempts, 30)
    self.assertEqual(resumed.attempts_used_this_process, 6)
    second_id = self._manifest(directory)['invocations'][1]['invocation_id']
    second_summary = self._summary(directory, second_id)
    self.assertEqual(second_summary['used_model_attempts_at_start'], 24)
    self.assertEqual(second_summary['attempts_used_this_process'], 6)

  def test_exception_text_credentials_are_redacted_in_archives(self):
    # A provider exception whose message embeds the live API key must
    # not leak that key into summary.json or the process summary — the
    # redaction covers every known runtime credential value with no
    # length exemption — while the returned error message stays
    # informative for the key's owner and the committed dialogue and
    # script exports stay byte-identical.
    directory = self._directory('run')
    secret = 'SK-42x'

    class ReviewLeak(_ScriptedReviewStub):

      def _route_text(self, prompt):
        if _REVIEW_MARK in prompt and self._review_calls == 2:
          raise RuntimeError(f'provider 401: key {secret} rejected')
        return super()._route_text(prompt)

    config = self._director_config()
    result = self._run(
        directory,
        ReviewLeak(
            review_script=('{"valid": true, "reason": "ok"}',) * 4,
            scoring_answer='5',
            repetition_answer='Yes',
            push_events=_PUSH_EVENT,
            push_merged='',
            stage_directions='',
        ),
        config=config,
        model_config={
            'api_type': 'stub',
            'api_key': secret,
            'prompt_stub': config['model_config']['prompt_stub'],
        },
    )
    self.assertEqual(result.reason, 'error')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertIn(secret, result.error_message or '')
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    summary_bytes = self._read(self._summary_path(directory, invocation_id))
    self.assertNotIn(secret.encode(), summary_bytes)
    self.assertIn(b'[REDACTED]', summary_bytes)
    with open(
        os.path.join(directory, 'summary.json'), encoding='utf-8'
    ) as handle:
      summary_json = handle.read()
    self.assertNotIn(secret, summary_json)
    self.assertIn('[REDACTED]', summary_json)
    # The committed dialogue and script exports are untouched by the
    # redaction, which only rewrites the secret's own occurrences.
    with open(
        os.path.join(directory, 'story_turns.jsonl'), encoding='utf-8'
    ) as handle:
      records = [json.loads(line) for line in handle if line.strip()]
    speech = records[0]['speech']
    self.assertTrue(speech)
    with open(
        os.path.join(directory, 'script.txt'), encoding='utf-8'
    ) as handle:
      self.assertIn(speech, handle.read())
    self.assertNotIn(secret, speech)

  def test_interrupted_core_call_preserves_partial_trace(self):
    # A KeyboardInterrupt raised inside the core fusion call still
    # archives the outer-visible trace — the merge prompt already holds
    # the selected event — while the checkpoint stays at two committed
    # turns and nothing is recorded as a successful check.
    directory = self._directory('run')

    class MergeInterrupt(_ScriptedReviewStub):

      def _route_text(self, prompt):
        if 'How does 2 modify' in prompt:
          raise KeyboardInterrupt
        return super()._route_text(prompt)

    stub = MergeInterrupt(
        review_script=('{"valid": true, "reason": "ok"}',) * 4,
        scoring_answer='5',
        repetition_answer='Yes',
        push_events=_PUSH_EVENT,
        push_merged='',
        stage_directions='',
    )
    with self.assertRaises(KeyboardInterrupt):
      self._run(directory, stub, config=self._director_config())
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    summary = self._summary(directory, invocation_id)
    self.assertEqual(summary['reason'], 'interrupted')
    self.assertEqual(summary['exception']['type'], 'KeyboardInterrupt')
    self.assertEqual(summary['committed_turn_id_at_end'], 2)
    uncommitted = summary['uncommitted_resolution']
    pushes = [
        record
        for record in uncommitted['history']
        if record['stage'] == 'repetition_push'
    ]
    self.assertEqual(len(pushes), 1)
    self.assertIn('KeyboardInterrupt', pushes[0]['error'])
    self.assertIn(_PUSH_EVENT, pushes[0]['trace'])
    # The interruption consumed neither the gate nor a candidate slot.
    self.assertIsNone(uncommitted['intervention'])
    self.assertEqual(self._envelope(directory)['committed_turn_id'], 2)

  def test_resume_refused_when_both_usage_records_failed(self):
    # Both consumption records fail to write on the first process: the
    # manifest holds an invocation that declared a summary that was
    # never published, so the true cumulative usage (24 real calls vs
    # the checkpoint's 15) is undeterminable. The resume must refuse —
    # treating the missing records as zero would grant fifteen more
    # attempts (thirty-nine real calls reported as thirty).
    directory = self._directory('run')
    config = self._director_config(total_model_attempt_limit=30)
    with mock.patch.object(
        run, '_write_process_usage', side_effect=OSError('usage disk full')
    ), mock.patch.object(
        provenance,
        'write_process_summary',
        side_effect=OSError('summary disk full'),
    ):
      with self.assertRaisesRegex(RuntimeError, 'process summary'):
        self._run(directory, _fault_stub(), config=config)
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    self.assertFalse(
        os.path.exists(self._summary_path(directory, invocation_id))
    )
    self.assertFalse(
        os.path.exists(os.path.join(directory, 'process_usage.json'))
    )

    with self.assertRaisesRegex(ValueError, 'cannot be determined'):
      self._run(directory, _healthy_stub(), resume=True, mode='director')
    # The committed checkpoint stays intact for manual recovery.
    self.assertEqual(self._envelope(directory)['committed_turn_id'], 2)

  def test_cleanup_failure_does_not_mask_original_exception(self):
    directory = self._directory('run')
    with _interrupt_when(3), mock.patch.object(
        provenance,
        'write_process_summary',
        side_effect=OSError('summary disk full'),
    ):
      with self.assertRaises(SimulatedInterruption):
        self._run(directory, _healthy_stub(), config=self._director_config())
    # The original interruption is still recognizable and the other
    # cleanup action (the usage sidecar) was not skipped.
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    self.assertFalse(
        os.path.exists(self._summary_path(directory, invocation_id))
    )
    self.assertGreater(self._usage(directory)['used_model_attempts'], 0)

  def test_archive_failure_without_original_exception_is_raised(self):
    directory = self._directory('run')
    with mock.patch.object(
        provenance,
        'write_process_summary',
        side_effect=OSError('summary disk full'),
    ):
      with self.assertRaisesRegex(RuntimeError, 'process summary'):
        self._run(
            directory,
            _healthy_stub(),
            config=self._director_config(total_turn_limit=1),
        )
    # The committed checkpoint itself is untouched by the archive
    # failure: the story boundary stays valid for a later resume.
    self.assertEqual(self._envelope(directory)['committed_turn_id'], 1)

  def test_credentials_and_private_trace_do_not_leak(self):
    directory = self._directory('run')
    config = self._director_config()
    secret = 'SK-LEAK-CHECK-123'
    result = self._run(
        directory,
        _fault_stub(),
        config=config,
        model_config={
            'api_type': 'stub',
            'api_key': secret,
            'prompt_stub': config['model_config']['prompt_stub'],
        },
    )
    self.assertEqual(result.reason, 'error')
    for root, _, files in os.walk(directory):
      for name in files:
        self.assertNotIn(
            secret.encode(),
            self._read(os.path.join(root, name)),
            os.path.join(root, name),
        )
    # The private candidate trace is archived in the failed summary but
    # never reaches the public script exports.
    invocation_id = self._manifest(directory)['invocations'][0]['invocation_id']
    summary_bytes = self._read(self._summary_path(directory, invocation_id))
    self.assertIn(_PUSH_EVENT.encode(), summary_bytes)
    for filename in ('script.fdx', 'script.fountain', 'script.txt'):
      with open(os.path.join(directory, filename), encoding='utf-8') as handle:
        self.assertNotIn(_PUSH_EVENT, handle.read(), filename)

  def test_legacy_manifest_and_missing_usage_remain_compatible(self):
    directory = self._directory('run')
    self._run(
        directory,
        _healthy_stub(),
        config=self._director_config(total_turn_limit=1),
    )
    # Simulate a legacy manifest whose entries predate invocation ids,
    # and a state directory without the usage sidecar.
    manifest_path = os.path.join(directory, 'run_manifest.json')
    manifest = self._manifest(directory)
    for entry in manifest['invocations']:
      entry.pop('invocation_id', None)
      entry.pop('summary_path', None)
    with open(manifest_path, 'w', encoding='utf-8') as handle:
      json.dump(manifest, handle, ensure_ascii=False, indent=2)
    os.unlink(os.path.join(directory, 'process_usage.json'))
    legacy_entry = dict(manifest['invocations'][0])
    # The first run's own summary file stays on disk as an orphan whose
    # manifest link was removed; the resume must not touch it.
    orphan_summaries = set(
        os.listdir(os.path.join(directory, 'process_summaries'))
    )
    self.assertEqual(len(orphan_summaries), 1)

    resumed = self._run(
        directory, _healthy_stub(), resume=True, mode='director'
    )
    self.assertEqual(resumed.committed_turn_id, 1)
    entries = self._manifest(directory)['invocations']
    self.assertEqual(len(entries), 2)
    # History is never fabricated: the old entry stays as it was and
    # only the new invocation carries an id and a summary.
    self.assertEqual(entries[0], legacy_entry)
    self.assertTrue(entries[1]['invocation_id'])
    self.assertTrue(entries[1]['summary_path'])
    summaries = set(os.listdir(os.path.join(directory, 'process_summaries')))
    self.assertEqual(
        summaries,
        orphan_summaries | {f"{entries[1]['invocation_id']}.json"},
    )


if __name__ == '__main__':
  unittest.main()
