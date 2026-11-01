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

"""Run-config audience contract marker and checkpoint resume tests.

Task 5 of the audience-delivery design (2026-10-31), sections 7.1 and
7.2: the run config records ``audience_delivery_version = 1``; both
entry points of ``run.run`` — a caller-supplied first-run config and a
checkpoint envelope's saved config — reject a config without the marker
BEFORE any model is constructed, because the two-view delivery contract
cannot honestly continue a run whose character memories may already be
over-delivered (the guidance is to start a fresh run, while offline
read-only export of existing records stays unrestricted). The final
test proves the complementary positive path end to end: a checkpoint
roundtrip of the two-view delivery state re-delivers nothing and the
undisclosed private finding still reaches no other character.
"""

import contextlib
import json
import os
import tempfile
from typing import Any
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import observation_queue
from concordia.contrib.components.game_master import story_action
from concordia.prefabs.simulation import generic as simulation_lib

from examples.story_enactment import collector
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs


class SimulatedInterruption(BaseException):
  """A crash stand-in that the runner must let propagate untouched."""


@contextlib.contextmanager
def _interrupt_when(turn_id: int, *, when: str = 'after_commit'):
  """Patches the collector so a save-boundary point raises a crash.

  Args:
    turn_id: the global turn id at whose save the interruption fires.
    when: either ``'before_write'`` (before the turn record is saved)
      or ``'after_commit'`` (after the checkpoint envelope is committed).
  """
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


class MakeRunConfigAudienceVersionTest(unittest.TestCase):

  def test_make_run_config_records_audience_version(self):
    config = run_lib.make_run_config(story_config.make_example_config())
    self.assertEqual(
        config['audience_delivery_version'],
        drama_director.AUDIENCE_DELIVERY_VERSION,
    )


class AudienceMarkerGateTest(unittest.TestCase):
  """The contract marker gates both entry points before model builds.

  A config missing ``audience_delivery_version`` (or holding a value
  other than 1) belongs to a run that predates the two-view delivery
  contract. Resuming or starting it under the new contract would let
  already over-delivered character memories continue as if they were
  audience-correct, so the config is rejected with fresh-run guidance —
  before any model construction, hence before any model sampling.
  """

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())

  def _markerless_config(self, mutation: str) -> dict[str, Any]:
    config = run_lib.make_run_config(
        story_config.make_example_config(),
        total_turn_limit=1,
    )
    if mutation == 'missing':
      del config['audience_delivery_version']
    else:
      config['audience_delivery_version'] = 2
    return config

  def test_first_run_rejects_config_without_marker(self):
    for mutation in ('missing', 'wrong-value'):
      with self.subTest(mutation=mutation):
        directory = os.path.join(self.tmpdir, mutation)
        with (
            mock.patch.object(run_lib, 'make_language_model') as maker,
            mock.patch.object(run_lib, 'build_simulation') as builder,
        ):
          with self.assertRaises(ValueError) as ctx:
            run_lib.run(directory, run_config=self._markerless_config(mutation))
        self._assert_fresh_run_guidance(str(ctx.exception))
        maker.assert_not_called()
        builder.assert_not_called()
        self.assertFalse(
            os.path.exists(os.path.join(directory, 'checkpoint.json'))
        )

  def test_normalize_rejects_audience_version_other_than_one(self):
    with self.assertRaisesRegex(ValueError, 'audience_delivery_version'):
      run_lib._normalize_run_config(  # pylint: disable=protected-access
          self._markerless_config('wrong-value')
      )

  def test_audience_check_runs_after_schema_validation(self):
    # An unknown schema version without the marker reports the schema
    # error, not the audience error: the marker gate sits after the
    # schema check inside _normalize_run_config (audience-delivery
    # design 7.2; the placement itself is what this test pins).
    config = self._markerless_config('missing')
    config['schema_version'] = 99
    with self.assertRaisesRegex(ValueError, 'schema version'):
      run_lib._normalize_run_config(config)  # pylint: disable=protected-access

  def _assert_fresh_run_guidance(self, message: str) -> None:
    self.assertIn('audience_delivery_version', message)
    self.assertIn('另开 fresh 运行', message)
    self.assertIn('旧 checkpoint 不支持继续', message)
    self.assertIn('离线只读导出不受影响', message)


class PreAudienceResumeRejectionTest(unittest.TestCase):
  """Resumes of pre-audience checkpoints are refused before any request."""

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())
    self.model = testing_stubs.PromptStubModel()
    self.enterContext(
        mock.patch.object(
            run_lib, 'make_language_model', return_value=(self.model, ())
        )
    )

  def _first_run(self, directory: str) -> None:
    run_lib.run(directory, disable_language_model=True, total_turn_limit=1)

  def _rewrite_run_config(self, directory: str, run_config) -> None:
    checkpoint_path = os.path.join(directory, 'checkpoint.json')
    with open(checkpoint_path, encoding='utf-8') as handle:
      envelope = json.load(handle)
    envelope['run_config'] = run_config
    with open(checkpoint_path, 'w', encoding='utf-8') as handle:
      json.dump(envelope, handle, ensure_ascii=False, indent=2)

  def _pre_audience_variants(self, saved: dict[str, Any]):
    """Builds one schema-1 and one schema-2 run config without the marker.

    Args:
      saved: the run config a current-contract first run recorded.

    Returns:
      ``(label, run_config)`` pairs: a schema-1 config (predating the
      action contract and the audience marker) and a schema-2 config
      (recording the action contract but predating the marker).
    """
    schema1 = {
        key: value
        for key, value in saved.items()
        if key not in ('action_mode', 'audience_delivery_version')
    }
    schema1['schema_version'] = 1
    schema1['gm_params'] = {
        key: value
        for key, value in saved['gm_params'].items()
        if key != 'action_mode'
    }
    schema2 = {
        key: value
        for key, value in saved.items()
        if key != 'audience_delivery_version'
    }
    return (('schema1', schema1), ('schema2', schema2))

  def test_resume_rejects_pre_audience_checkpoint(self):
    directory = os.path.join(self.tmpdir, 'run')
    self._first_run(directory)
    with open(
        os.path.join(directory, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      saved = json.load(handle)['run_config']
    checkpoint_path = os.path.join(directory, 'checkpoint.json')
    log_path = os.path.join(directory, 'story_turns.jsonl')
    for label, variant in self._pre_audience_variants(saved):
      with self.subTest(label=label):
        # Install the pre-audience envelope, then snapshot the bytes the
        # rejected resume must leave untouched.
        self._rewrite_run_config(directory, variant)
        with open(checkpoint_path, encoding='utf-8') as handle:
          checkpoint_before = handle.read()
        with open(log_path, encoding='utf-8') as handle:
          log_before = handle.read()
        with (
            mock.patch.object(run_lib, 'make_language_model') as maker,
            mock.patch.object(run_lib, 'build_simulation') as builder,
        ):
          with self.assertRaises(ValueError) as ctx:
            run_lib.run(directory, resume=True)
        message = str(ctx.exception)
        self.assertIn('audience_delivery_version', message)
        self.assertIn('另开 fresh 运行', message)
        self.assertIn('旧 checkpoint 不支持继续', message)
        self.assertIn('离线只读导出不受影响', message)
        # Rejection happens before any model construction — so no model
        # exists that could sample — and before the simulation is built.
        maker.assert_not_called()
        builder.assert_not_called()
        # The old checkpoint and turn files are not polluted.
        with open(checkpoint_path, encoding='utf-8') as handle:
          self.assertEqual(handle.read(), checkpoint_before)
        with open(log_path, encoding='utf-8') as handle:
          self.assertEqual(handle.read(), log_before)


# Distinctive private fact of the R1-t3-style lookup turn of the
# roundtrip scenario: a concrete date only the acting lawyer learns.
_DATE_MARK = '二〇一九年三月十二日'
_LOOKUP_INTENT = '周律师借口复核，独自抽出遗嘱底档查看落款日期。'
_LOOKUP_RESULT = (
    f'档案室没有别人。周律师翻开底档，看清落款日期是{_DATE_MARK}，'
    '与公示文本不一致。'
)
_LAWYER, _AUNT = '周律师', '王婶'
_AUNT_FIRST_LINE = '第一句：先把底档的事说清楚。'
_AUNT_SECOND_LINE = '第二句：这事还没完。'


class _SequencedStubModel(testing_stubs.PromptStubModel):
  """A stub whose characters speak a configured sequence of lines.

  The roundtrip scenario needs the lawyer to act silently (a pure
  action turn) on each of his turns while the aunt speaks a distinct
  line per turn, so the two committed turns and the two resumed turns
  are individually recognizable in queues, memories and prompts.
  """

  def __init__(self, *, speech_sequences=None, **kwargs):
    super().__init__(**kwargs)
    self._speech_sequences = {
        name: [(speech, intent) for speech, intent in lines]
        for name, lines in (speech_sequences or {}).items()
    }

  def _action_contract_output(self, prompt: str) -> str:
    match = testing_stubs._SPEAKER_IN_CALL.search(  # pylint: disable=protected-access
        prompt
    )
    name = match.group(1) if match else ''
    lines = self._speech_sequences.get(name)
    if lines:
      speech, intent = lines.pop(0)
    else:
      speech = self._dialogue_lines.get(name, self._default_line)
      intent = self._action_intents.get(name, '')
    payload = json.dumps(
        {'speech': speech, 'action_intent': intent}, ensure_ascii=False
    )
    story_action.decode_action(payload)
    return payload


def _roundtrip_stub(aunt_line: str) -> _SequencedStubModel:
  """Builds one process's stub of the two-turn private-lookup scenario.

  The lawyer's every turn is the same silent lookup; the core-adjudicated
  result carries the concrete date into the FULL narrator track only,
  and the configured public track is empty, so the other participant is
  entitled to receive nothing of his turns.

  Args:
    aunt_line: the line the aunt speaks on her turn of this process.

  Returns:
    The deterministic stub model of one process.
  """
  return _SequencedStubModel(
      speech_sequences={
          _LAWYER: [('', _LOOKUP_INTENT)],
          _AUNT: [(aunt_line, '')],
      },
      action_result=_LOOKUP_RESULT,
      stage_directions='',
      public_stage_directions='',
  )


def _roundtrip_config() -> dict[str, Any]:
  """Builds the two-participant private-lookup run config of the roundtrip."""
  config = run_lib.make_run_config(
      story_config.make_example_config(),
      total_turn_limit=4,
      action_mode='speech_and_action',
      model_config={'api_type': 'stub', 'prompt_stub': {}},
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
  # Only the lawyer and the aunt take part, in that order: turns 1 and
  # 3 are his silent lookups, turns 2 and 4 are her public speech.
  scene = config['scene_dicts'][0]
  scene['participants'] = [_LAWYER, _AUNT]
  scene['premise'] = {
      name: lines
      for name, lines in scene['premise'].items()
      if name in (_LAWYER, _AUNT)
  }
  scene['num_rounds'] = 99
  config['entity_params'] = [
      params
      for params in config['entity_params']
      if params['name'] in (_LAWYER, _AUNT)
  ]
  return config


def _director_of(
    sim: simulation_lib.Simulation,
) -> drama_director.DramaDirector:
  """Returns the drama director of the scenario's game master."""
  return sim.game_masters[0].get_component(
      drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
      type_=drama_director.DramaDirector,
  )


def _queue_state(sim: simulation_lib.Simulation) -> dict[str, list[str]]:
  """Returns the game master's undelivered per-player observation queue."""
  return (
      sim.game_masters[0]
      .get_component('__make_observation__')
      .get_state()['queue']
  )


def _queued_items(sim: simulation_lib.Simulation, name: str) -> list[str]:
  """Returns one player's undelivered queue items, boundary-decoded."""
  return [
      observation_queue.decode_observation(item)[0] or item
      for item in _queue_state(sim).get(name, [])
  ]


def _memories_text(sim: simulation_lib.Simulation, name: str) -> str:
  """Returns everything one character's memory holds, joined as text."""
  entities_by_name = {entity.name: entity for entity in sim.entities}
  memory = entities_by_name[name].get_component('__memory__')
  return '\n'.join(memory.get_all_memories_as_text())


def _speech_prompts(
    model: testing_stubs.PromptStubModel, name: str
) -> list[str]:
  """Returns the recorded action prompts of one character's speeches."""
  return [p for p in model.text_prompts if f'{name} 现在说什么' in p]


def _delivery_snapshot(sim: simulation_lib.Simulation) -> dict[str, Any]:
  """Snapshots every audience-relevant restored state of a simulation."""
  director = _director_of(sim)
  return {
      'queue': {name: _queued_items(sim, name) for name in (_LAWYER, _AUNT)},
      'public_events': list(director.get_public_events()),
      'committed_events': list(director.get_committed_events()),
      'memories': {
          name: _memories_text(sim, name) for name in (_LAWYER, _AUNT)
      },
  }


class ResumeRoundtripTest(unittest.TestCase):
  """A checkpoint roundtrip re-delivers nothing of the restored state.

  Design 7.2's positive counterpart of the pre-audience rejection: a
  checkpoint written under the audience contract restores each
  character's own queue and memory, the public history and the GM's
  complete record exactly as they were committed, adds no duplicate
  deliveries, and the still-undisclosed private date keeps reaching
  only the character who legally learned it.
  """

  def setUp(self):
    super().setUp()
    self.tmpdir = self.enterContext(tempfile.TemporaryDirectory())
    self.state_dir = os.path.join(self.tmpdir, 'state')
    self.config = _roundtrip_config()

  def _run_with_capture(self, sink: dict[str, Any], stub, *, resume=False):
    """Runs one process, capturing its built simulation in ``sink``."""
    real_build = run_lib.build_simulation

    def capturing_build(**kwargs):
      built = real_build(**kwargs)
      sink['sim'] = built[0]
      return built

    with (
        mock.patch.object(
            run_lib, 'make_language_model', return_value=(stub, ())
        ),
        mock.patch.object(run_lib, 'build_simulation', capturing_build),
    ):
      return run_lib.run(self.state_dir, run_config=self.config, resume=resume)

  def test_resume_roundtrip_does_not_redeliver(self):
    # Process 1: the lawyer's private lookup (turn 1) and the aunt's
    # public speech (turn 2) commit; a crash follows the checkpoint.
    first_sink: dict[str, Any] = {}
    with _interrupt_when(2):
      with self.assertRaises(SimulatedInterruption):
        self._run_with_capture(first_sink, _roundtrip_stub(_AUNT_FIRST_LINE))
    turn_log = collector.StoryTurnLog(
        os.path.join(self.state_dir, 'story_turns.jsonl')
    )
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      envelope = json.load(handle)
    self.assertEqual(envelope['committed_turn_id'], 2)
    self.assertEqual(envelope['schema_version'], 1)
    records = turn_log.load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2])
    # The committed schema-3 records carry both views: the full event
    # with the private date for the actor, the empty public event that
    # queued nothing for the aunt.
    self.assertEqual(records[0]['actor'], _LAWYER)
    self.assertEqual(records[0]['schema_version'], 3)
    self.assertIn(_DATE_MARK, records[0]['resolved_event'])
    self.assertNotIn(_DATE_MARK, records[0]['public_resolved_event'])
    self.assertEqual(records[0]['public_resolved_event'], '')
    self.assertEqual(records[1]['actor'], _AUNT)
    self.assertEqual(
        records[1]['public_resolved_event'], records[1]['resolved_event']
    )
    before = _delivery_snapshot(first_sink['sim'])
    # The engine delivers queued observations at the next step boundary:
    # the lawyer's own lookup event already reached his memory at the
    # start of turn 2, and each queue now holds exactly the one view of
    # the most recent turn its owner is entitled to.
    self.assertEqual(
        before['queue'][_LAWYER], [records[1]['public_resolved_event']]
    )
    self.assertEqual(before['queue'][_AUNT], [records[1]['resolved_event']])
    self.assertNotIn(_DATE_MARK, before['memories'][_AUNT])
    self.assertIn(_DATE_MARK, before['memories'][_LAWYER])

    # Process 2: the resume restores the exact delivery state before
    # its first model request, then plays turns 3 and 4.
    second_sink: dict[str, Any] = {}
    restored: dict[str, Any] = {}
    real_load = simulation_lib.Simulation.load_from_checkpoint

    def capturing_load(self, data):
      real_load(self, data)
      restored['snapshot'] = _delivery_snapshot(self)

    stub = _roundtrip_stub(_AUNT_SECOND_LINE)
    with mock.patch.object(
        simulation_lib.Simulation, 'load_from_checkpoint', capturing_load
    ):
      result = self._run_with_capture(second_sink, stub, resume=True)
    self.assertEqual(result.committed_turn_id, 4)
    self.assertEqual(restored['snapshot'], before)

    # Counts are not duplicated: exactly four records, the checkpoint
    # advanced to four, and the final queues hold exactly the one view
    # of turn 4 each participant is entitled to — a re-delivery of the
    # restored history would leave extra items behind.
    records = turn_log.load()
    self.assertEqual([r['turn_id'] for r in records], [1, 2, 3, 4])
    with open(
        os.path.join(self.state_dir, 'checkpoint.json'), encoding='utf-8'
    ) as handle:
      self.assertEqual(json.load(handle)['committed_turn_id'], 4)
    self.assertEqual(records[2]['actor'], _LAWYER)
    self.assertIn(_DATE_MARK, records[2]['resolved_event'])
    self.assertEqual(records[2]['public_resolved_event'], '')
    self.assertEqual(records[3]['actor'], _AUNT)
    sim = second_sink['sim']
    self.assertEqual(
        _queued_items(sim, _LAWYER), [records[3]['public_resolved_event']]
    )
    self.assertEqual(_queued_items(sim, _AUNT), [records[3]['resolved_event']])
    # The public history gained only the aunt's two speech events; the
    # lookup turns contributed nothing public.
    self.assertEqual(
        list(_director_of(sim).get_public_events()),
        [
            records[1]['public_resolved_event'],
            records[3]['public_resolved_event'],
        ],
    )

    # The subsequent step still withholds the undisclosed date: the
    # aunt's only actual input of the resumed process carries her own
    # committed line, never the date, while the lawyer's carries his
    # restored private observation.
    aunt_prompts = _speech_prompts(stub, _AUNT)
    self.assertEqual(len(aunt_prompts), 1)
    self.assertIn(_AUNT_FIRST_LINE, aunt_prompts[0])
    self.assertNotIn(_DATE_MARK, aunt_prompts[0])
    self.assertNotIn(_DATE_MARK, _memories_text(sim, _AUNT))
    lawyer_prompts = _speech_prompts(stub, _LAWYER)
    self.assertEqual(len(lawyer_prompts), 1)
    self.assertIn(_DATE_MARK, lawyer_prompts[0])
    self.assertIn(_DATE_MARK, _memories_text(sim, _LAWYER))


if __name__ == '__main__':
  unittest.main()
