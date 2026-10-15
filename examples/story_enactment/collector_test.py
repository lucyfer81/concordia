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

"""Tests for the story turn collector and its persistence boundary."""

import json
import os
import tempfile
from typing import Any
import unittest
from unittest import mock

from concordia.agents import entity_agent_with_logging
from concordia.components.agent import concat_act_component
from concordia.components.agent import constant as constant_lib
from concordia.contrib.components.agent import emotional_stance
from concordia.environment import step_controller as step_controller_lib
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib

from examples.story_enactment import collector

_ACTOR = '李强'
_SPEECH = '这一页必须找回来。'
_EMOTIONS = ('紧张', '愤怒', '平静')


def _make_turn(turn_id=1, actor=_ACTOR, speech=_SPEECH, **overrides):
  """Builds a director story turn dict with Task 3's field inventory."""
  turn = {
      'turn_id': turn_id,
      'actor': actor,
      'speech': speech,
      'beat_id_at_action': 'b1',
      'beat_id_after_event': 'b1',
      'resolved_event': f'{actor}说：{speech}',
      'stage_directions': '灯光变暗。',
      'tension': None,
      'budget_stopped': False,
  }
  turn.update(overrides)
  return turn


class _DirectorStub:
  """Stub with the real DramaDirector.get_last_story_turn semantics."""

  def __init__(self, turn):
    self._turn = turn

  def set_turn(self, turn):
    self._turn = turn

  def get_last_story_turn(self):
    if self._turn is None:
      return None
    return dict(self._turn)


class _BudgetGuardStub:
  """Stub with the real BudgetGuardLanguageModel.used_attempts semantics."""

  def __init__(self, used_attempts=0):
    self.used_attempts = used_attempts


class _Harness:
  """Bundles the callback's collaborators with temporary file paths."""

  def __init__(self, tmpdir, turn):
    self.turn_log = collector.StoryTurnLog(
        os.path.join(tmpdir, 'story_turns.jsonl')
    )
    self.checkpoint_path = os.path.join(tmpdir, 'checkpoint.json')
    self.run_config = {
        'scene_dicts': [
            {'scene_type': {'name': '故事主线'}, 'participants': [_ACTOR]},
        ],
    }
    self.director = _DirectorStub(turn)
    self.budget_guard = _BudgetGuardStub(used_attempts=7)
    self.simulation = mock.MagicMock()
    self.simulation.make_checkpoint_data.return_value = {
        'checkpoint_counter': 0
    }

  def callback(self, **overrides):
    params: dict[str, Any] = dict(
        simulation=self.simulation,
        drama_director=self.director,
        budget_guard=self.budget_guard,
        turn_log=self.turn_log,
        checkpoint_path=self.checkpoint_path,
        run_config=self.run_config,
        total_turn_limit=24,
        total_model_attempt_limit=400,
        emotion_enabled=False,
    )
    params.update(overrides)
    return collector.make_step_callback(**params)

  def step_data(
      self,
      *,
      step=1,
      acting_entity=_ACTOR,
      action=None,
      entity_logs=None,
  ):
    return step_controller_lib.StepData(
        step=step,
        acting_entity=acting_entity,
        action=action if action is not None else f'{acting_entity}: {_SPEECH}',
        entity_actions={},
        entity_logs={} if entity_logs is None else entity_logs,
        game_master='故事主理人',
    )

  def read_checkpoint(self):
    with open(self.checkpoint_path, encoding='utf-8') as handle:
      return json.load(handle)


class StoryTurnLogContiguityTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())
    self.log = collector.StoryTurnLog(
        os.path.join(self.tmpdir, 'story_turns.jsonl')
    )

  def test_first_record_must_be_turn_one(self):
    with self.assertRaises(ValueError):
      self.log.append(_make_turn(turn_id=2))

  def test_new_turn_must_follow_existing_max(self):
    self.log.append(_make_turn(turn_id=1))
    with self.assertRaises(ValueError):
      self.log.append(_make_turn(turn_id=3))

  def test_duplicate_identical_append_is_noop(self):
    self.log.append(_make_turn(turn_id=1))
    before = open(self.log.path, encoding='utf-8').read()
    self.log.append(_make_turn(turn_id=1))
    self.assertEqual(len(self.log.load()), 1)
    self.assertEqual(open(self.log.path, encoding='utf-8').read(), before)

  def test_duplicate_different_content_raises(self):
    self.log.append(_make_turn(turn_id=1))
    with self.assertRaises(ValueError):
      self.log.append(_make_turn(turn_id=1, speech='不同的对白。'))

  def test_sequential_appends_stay_contiguous(self):
    self.log.append(_make_turn(turn_id=1))
    self.log.append(_make_turn(turn_id=2))
    self.assertEqual([r['turn_id'] for r in self.log.load()], [1, 2])
    self.log.validate_contiguous()

  def test_trim_after_removes_newer_turns(self):
    for turn_id in (1, 2, 3):
      self.log.append(_make_turn(turn_id=turn_id))
    self.log.trim_after(1)
    self.assertEqual([r['turn_id'] for r in self.log.load()], [1])
    self.log.validate_contiguous()

  def test_validate_contiguous_rejects_gap_and_duplicate(self):
    path = os.path.join(self.tmpdir, 'broken.jsonl')
    with open(path, 'w', encoding='utf-8') as handle:
      for turn_id in (1, 3):
        handle.write(
            json.dumps(_make_turn(turn_id=turn_id), ensure_ascii=False) + '\n'
        )
    broken = collector.StoryTurnLog(path)
    with self.assertRaises(ValueError):
      broken.validate_contiguous()
    with open(path, 'w', encoding='utf-8') as handle:
      handle.write(json.dumps(_make_turn(turn_id=1), ensure_ascii=False) + '\n')
      handle.write(json.dumps(_make_turn(turn_id=1), ensure_ascii=False) + '\n')
    with self.assertRaises(ValueError):
      broken.validate_contiguous()

  def test_atomic_replace_failure_leaves_old_file_readable(self):
    self.log.append(_make_turn(turn_id=1))
    before = open(self.log.path, encoding='utf-8').read()
    with mock.patch.object(
        collector.os, 'replace', side_effect=OSError('disk full')
    ):
      with self.assertRaises(OSError):
        self.log.append(_make_turn(turn_id=2))
    self.assertEqual(open(self.log.path, encoding='utf-8').read(), before)
    self.assertEqual([r['turn_id'] for r in self.log.load()], [1])


class StepCallbackTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())
    self.harness = _Harness(self.tmpdir, _make_turn(turn_id=1))

  def test_no_story_turn_returns_early(self):
    self.harness.director = _DirectorStub(None)
    self.harness.callback()(self.harness.step_data())
    self.assertEqual(self.harness.turn_log.load(), [])
    self.assertFalse(os.path.exists(self.harness.checkpoint_path))
    self.harness.simulation.make_checkpoint_data.assert_not_called()

  def test_director_diagnostics_are_archived_without_changing_events(self):
    diagnostics = {
        'before': {'status': 'pending', 'content': 'A knock at the door.'},
        'after': {'status': 'consumed', 'content': 'A knock at the door.'},
    }
    self.harness.director.set_turn(_make_turn(director_diagnostics=diagnostics))
    self.harness.callback()(self.harness.step_data())
    record = self.harness.turn_log.load()[0]
    self.assertEqual(record['director_diagnostics'], diagnostics)
    self.assertEqual(record['resolved_event'], f'{_ACTOR}说：{_SPEECH}')
    self.assertNotIn('A knock', record['resolved_event'])

  def test_saves_turn_then_checkpoint_envelope(self):
    self.harness.callback()(self.harness.step_data())
    records = self.harness.turn_log.load()
    self.assertEqual(len(records), 1)
    record = records[0]
    self.assertEqual(record['turn_id'], 1)
    self.assertEqual(record['scene_id'], '故事主线')
    self.assertEqual(record['actor'], _ACTOR)
    self.assertEqual(record['speech'], _SPEECH)
    self.assertEqual(record['emotion'], None)
    self.assertEqual(record['beat_id_at_action'], 'b1')
    self.assertEqual(record['beat_id_after_event'], 'b1')
    self.assertEqual(record['resolved_event'], f'{_ACTOR}说：{_SPEECH}')
    self.assertEqual(record['stage_directions'], '灯光变暗。')
    self.assertEqual(record['tension'], None)
    envelope = self.harness.read_checkpoint()
    self.assertEqual(envelope['schema_version'], 1)
    self.assertEqual(envelope['run_config'], self.harness.run_config)
    self.assertEqual(envelope['simulation_state'], {'checkpoint_counter': 0})
    self.assertEqual(envelope['committed_turn_id'], 1)
    self.assertEqual(envelope['used_model_attempts'], 7)
    self.assertEqual(envelope['total_turn_limit'], 24)
    self.assertEqual(envelope['total_model_attempt_limit'], 400)

  def test_turn_file_saved_before_checkpoint(self):
    observed = []

    def _checkpoint_data():
      observed.append((
          [r['turn_id'] for r in self.harness.turn_log.load()],
          os.path.exists(self.harness.checkpoint_path),
      ))
      return {'checkpoint_counter': 1}

    self.harness.simulation.make_checkpoint_data.side_effect = _checkpoint_data
    self.harness.callback()(self.harness.step_data())
    self.assertEqual(observed, [([1], False)])

  def test_actor_mismatch_raises_and_writes_nothing(self):
    with self.assertRaises(ValueError):
      self.harness.callback()(
          self.harness.step_data(
              acting_entity='王兰', action=f'王兰: {_SPEECH}'
          )
      )
    self.assertEqual(self.harness.turn_log.load(), [])
    self.assertFalse(os.path.exists(self.harness.checkpoint_path))

  def test_speech_mismatch_raises_and_writes_nothing(self):
    with self.assertRaises(ValueError):
      self.harness.callback()(
          self.harness.step_data(action=f'{_ACTOR}: 无关的台词。')
      )
    self.assertEqual(self.harness.turn_log.load(), [])
    self.assertFalse(os.path.exists(self.harness.checkpoint_path))

  def test_step_data_step_is_not_the_turn_id(self):
    self.harness.callback()(self.harness.step_data(step=99))
    self.assertEqual([r['turn_id'] for r in self.harness.turn_log.load()], [1])
    self.assertEqual(self.harness.read_checkpoint()['committed_turn_id'], 1)

  def test_duplicate_callback_identical_is_noop(self):
    callback = self.harness.callback()
    callback(self.harness.step_data())
    checkpoint_before = open(self.harness.checkpoint_path, 'rb').read()
    callback(self.harness.step_data())
    self.assertEqual(len(self.harness.turn_log.load()), 1)
    self.assertEqual(
        open(self.harness.checkpoint_path, 'rb').read(), checkpoint_before
    )
    self.assertEqual(self.harness.simulation.make_checkpoint_data.call_count, 1)

  def test_retry_completes_checkpoint_after_partial_commit(self):
    self.harness.simulation.make_checkpoint_data.side_effect = OSError(
        'interrupted'
    )
    callback = self.harness.callback()
    with self.assertRaises(OSError):
      callback(self.harness.step_data())
    # The turn file is committed but the checkpoint is not yet written.
    self.assertEqual([r['turn_id'] for r in self.harness.turn_log.load()], [1])
    self.assertFalse(os.path.exists(self.harness.checkpoint_path))
    # A retry must complete the checkpoint commit without duplicating turns.
    self.harness.simulation.make_checkpoint_data.side_effect = None
    self.harness.simulation.make_checkpoint_data.return_value = {
        'checkpoint_counter': 2
    }
    callback(self.harness.step_data())
    self.assertEqual([r['turn_id'] for r in self.harness.turn_log.load()], [1])
    envelope = self.harness.read_checkpoint()
    self.assertEqual(envelope['committed_turn_id'], 1)
    self.assertEqual(envelope['simulation_state'], {'checkpoint_counter': 2})

  def test_duplicate_turn_with_different_content_raises(self):
    callback = self.harness.callback()
    callback(self.harness.step_data())
    # Speech and actor still match the step data; only the resolved content
    # differs, so the conflict is caught at the turn log level.
    self.harness.director.set_turn(
        _make_turn(turn_id=1, stage_directions='不一样的舞台指示。')
    )
    with self.assertRaises(ValueError):
      callback(self.harness.step_data())


class ActionRecordTest(unittest.TestCase):
  """Schema-2 collection of raw actions and their decoded fields."""

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())

  def test_json_action_is_compared_to_raw_action(self):
    intent = '悄悄记下保险柜钥匙的位置。'
    raw_action = json.dumps(
        {'speech': _SPEECH, 'action_intent': intent}, ensure_ascii=False
    )
    harness = _Harness(
        self.tmpdir,
        _make_turn(
            schema_version=2, raw_action=raw_action, action_intent=intent
        ),
    )
    callback = harness.callback(action_mode='speech_and_action')
    # Consistent: the engine echoes the director's raw JSON verbatim
    # after the speaker prefix, so the turn commits.
    callback(harness.step_data(action=f'{_ACTOR}: {raw_action}'))
    self.assertEqual(harness.read_checkpoint()['committed_turn_id'], 1)
    self.assertEqual(harness.turn_log.load()[0]['raw_action'], raw_action)
    # Inconsistent: the decoded speech is not the raw action, and the
    # collector compares StepData.action against raw_action (never
    # against the decoded speech), so the step is rejected and the
    # checkpoint does not advance.
    harness.director.set_turn(
        _make_turn(
            turn_id=2,
            schema_version=2,
            raw_action=raw_action,
            action_intent=intent,
        )
    )
    with self.assertRaises(ValueError):
      callback(harness.step_data(step=2, action=f'{_ACTOR}: {_SPEECH}'))
    self.assertEqual([r['turn_id'] for r in harness.turn_log.load()], [1])
    self.assertEqual(harness.read_checkpoint()['committed_turn_id'], 1)

  def test_decoded_fields_must_match_raw_action(self):
    speech = '第一段对白，带"引号"和##标记。\n\n第二段对白，一字不改。'
    intent = '把信悄悄放回抽屉。'
    raw_action = json.dumps(
        {'speech': speech, 'action_intent': intent}, ensure_ascii=False
    )
    harness = _Harness(
        self.tmpdir,
        _make_turn(
            speech=speech,
            schema_version=2,
            raw_action=raw_action,
            action_intent=intent,
        ),
    )
    callback = harness.callback(action_mode='speech_and_action')
    callback(harness.step_data(action=f'{_ACTOR}: {raw_action}'))
    record = harness.turn_log.load()[0]
    # The archived speech keeps the codec-decoded multi-paragraph text
    # verbatim, including the blank-line paragraph break.
    self.assertEqual(record['speech'], speech)
    self.assertEqual(record['action_intent'], intent)
    # Tampered speech: raw_action still matches the step, but the
    # record's speech is no longer what the shared codec decodes from
    # it, so the turn is rejected and nothing new is written.
    harness.director.set_turn(
        _make_turn(
            turn_id=2,
            speech=speech.replace('\n\n', ' '),
            schema_version=2,
            raw_action=raw_action,
            action_intent=intent,
        )
    )
    with self.assertRaises(ValueError):
      callback(harness.step_data(step=2, action=f'{_ACTOR}: {raw_action}'))
    # Tampered intent: the other decoded field is validated too.
    harness.director.set_turn(
        _make_turn(
            turn_id=2,
            speech=speech,
            schema_version=2,
            raw_action=raw_action,
            action_intent='无关的意图。',
        )
    )
    with self.assertRaises(ValueError):
      callback(harness.step_data(step=2, action=f'{_ACTOR}: {raw_action}'))
    self.assertEqual([r['turn_id'] for r in harness.turn_log.load()], [1])
    self.assertEqual(harness.read_checkpoint()['committed_turn_id'], 1)

  def test_action_only_turn_commits_once(self):
    intent = '用铁丝撬开上锁的抽屉。'
    raw_action = json.dumps(
        {'speech': '', 'action_intent': intent}, ensure_ascii=False
    )
    resolved_event = f'{_ACTOR}撬动抽屉锁，锁舌弹开，抽屉被打开了。'
    stage_directions = f'{_ACTOR}用铁丝探入锁孔，抽屉应声而开。'
    harness = _Harness(
        self.tmpdir,
        _make_turn(
            speech='',
            schema_version=2,
            raw_action=raw_action,
            action_intent=intent,
            resolved_event=resolved_event,
            stage_directions=stage_directions,
        ),
    )
    callback = harness.callback(action_mode='speech_and_action')
    callback(harness.step_data(action=f'{_ACTOR}: {raw_action}'))
    # One adjudicated action is still exactly one story turn with one
    # commit number; parsing and adjudication add no extra turns.
    records = harness.turn_log.load()
    self.assertEqual([r['turn_id'] for r in records], [1])
    self.assertEqual(harness.read_checkpoint()['committed_turn_id'], 1)
    self.assertEqual(harness.simulation.make_checkpoint_data.call_count, 1)
    record = records[0]
    self.assertEqual(record['speech'], '')
    self.assertEqual(record['raw_action'], raw_action)
    self.assertEqual(record['action_intent'], intent)
    # The approved action result reaches the record unchanged.
    self.assertEqual(record['resolved_event'], resolved_event)
    self.assertEqual(record['stage_directions'], stage_directions)


class EmotionCollectionTest(unittest.TestCase):
  """Emotion tests driven through a real EmotionalStance component."""

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())
    self.model = mock_model.MockModel(response='照着台本念出这句话。')

  def _act_and_collect_logs(self):
    """Runs one real act cycle and returns the engine's entity log view."""
    entity = entity_agent_with_logging.EntityAgentWithLogging(
        agent_name=_ACTOR,
        act_component=concat_act_component.ConcatActComponent(
            model=self.model, prefix_entity_name=False
        ),
        context_components={
            'PersonBySituation': constant_lib.Constant(
                state='围读因缺页陷入停顿。',
                pre_act_label='Situational reasoning',
            ),
            'EmotionalStance': emotional_stance.EmotionalStance(
                model=self.model,
                name=_ACTOR,
                emotion_options=list(_EMOTIONS),
                # The observation source is a wiring choice; using the
                # reasoning constant keeps the fixture free of memory
                # infrastructure while the component's own lifecycle and
                # logging channel stay fully real.
                observation_component_key='PersonBySituation',
            ),
        },
    )
    entity.act(entity_lib.free_action_spec(call_to_action='说什么？'))
    # The sequential engine stores dict(entity.get_last_log()) per entity.
    return dict(entity.get_last_log())

  def test_current_actor_emotion_enters_story_turn(self):
    entity_log = self._act_and_collect_logs()
    # The published datum is looked up by component key, not by its own
    # spaced-name "Key" field.
    self.assertEqual(
        entity_log[collector.EMOTIONAL_STANCE_COMPONENT_KEY]['Key'],
        f"{_ACTOR}'s Emotional Stance",
    )
    # MockModel picks the first presented option, which the multiple-choice
    # question shuffles, so the exact label varies; what matters is that a
    # real tag chosen by the component enters the story turn verbatim.
    chosen = entity_log[collector.EMOTIONAL_STANCE_COMPONENT_KEY]['Value']
    self.assertIn(chosen, _EMOTIONS)
    harness = _Harness(self.tmpdir, _make_turn(turn_id=1))
    callback = harness.callback(emotion_enabled=True)
    callback(harness.step_data(entity_logs={_ACTOR: entity_log}))
    self.assertEqual(harness.turn_log.load()[0]['emotion'], chosen)

  def test_missing_component_key_writes_none_without_spaced_search(self):
    harness = _Harness(self.tmpdir, _make_turn(turn_id=1))
    callback = harness.callback(emotion_enabled=True)
    # A spaced-name decoy must not be picked up by a fuzzy lookup.
    entity_logs = {_ACTOR: {f"{_ACTOR}'s Emotional Stance": {'Value': '愤怒'}}}
    callback(harness.step_data(entity_logs=entity_logs))
    self.assertIsNone(harness.turn_log.load()[0]['emotion'])

  def test_emotion_disabled_writes_none_even_when_present(self):
    entity_log = self._act_and_collect_logs()
    harness = _Harness(self.tmpdir, _make_turn(turn_id=1))
    callback = harness.callback(emotion_enabled=False)
    callback(harness.step_data(entity_logs={_ACTOR: entity_log}))
    self.assertIsNone(harness.turn_log.load()[0]['emotion'])


if __name__ == '__main__':
  unittest.main()
