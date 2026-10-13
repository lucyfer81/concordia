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

"""Story turn collection and the save boundary of the enactment example.

This module is the single persistence boundary of the story enactment
example. A step callback (plugged into ``Simulation.play``) reads the most
recently committed story turn from the ``DramaDirector``, enriches it with
the current scene id and the acting character's emotion, saves the story
turn JSONL file, and only then atomically saves the run checkpoint envelope.
The checkpoint is the commit boundary between the two files: if the process
dies after the turn file was written but before the checkpoint was saved, a
retry of the callback completes the checkpoint without duplicating turns.

The callback performs no language model calls; it only moves data between
the simulation objects and files on disk.
"""

from collections.abc import Callable, Mapping
import contextlib
import json
import os
import tempfile
from typing import Any

from concordia.contrib.prefabs.entity import story_character
from concordia.environment import step_controller as step_controller_lib

# The context-component key under which the EmotionalStance component is
# registered on story characters. Entity logs are keyed by this component
# name (not by the spaced-name "Key" field inside the published datum).
# The constant is owned by the prefab that registers the component, so
# the two definitions can never drift apart.
EMOTIONAL_STANCE_COMPONENT_KEY = story_character.EMOTIONAL_STANCE_COMPONENT_KEY

# Schema version of the checkpoint envelope written by this module.
CHECKPOINT_SCHEMA_VERSION = 1

StepCallback = Callable[[step_controller_lib.StepData], None]


def _atomic_write_text(path: str, text: str) -> None:
  """Writes text to ``path`` atomically.

  The content is first written to a temporary file in the same directory
  and then moved into place with ``os.replace``, so committed files are
  never modified in place and readers never observe a partial file.

  Args:
    path: the destination file path.
    text: the full file content to write.
  """
  directory = os.path.dirname(os.path.abspath(path))
  fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.collector-')
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
      handle.write(text)
    os.replace(tmp_path, path)
  except BaseException:
    with contextlib.suppress(OSError):
      os.close(fd)
    with contextlib.suppress(OSError):
      os.unlink(tmp_path)
    raise


class StoryTurnLog:
  """An append-only JSONL file of story turn records.

  Records carry a global ``turn_id`` starting at 1 and must be appended in
  strictly consecutive order. Every change rewrites the whole file
  atomically (same-directory temporary file plus ``os.replace``).
  """

  def __init__(self, path: str):
    """Initializes the log.

    Args:
      path: the JSONL file path backing the log.
    """
    self._path = path

  @property
  def path(self) -> str:
    """Returns the JSONL file path backing the log."""
    return self._path

  def load(self) -> list[dict[str, Any]]:
    """Returns all records currently in the file.

    Returns:
      A list of record dictionaries, in file order. An absent or empty
      file yields an empty list.
    """
    if not os.path.exists(self._path):
      return []
    records = []
    with open(self._path, encoding='utf-8') as handle:
      for line in handle:
        if line.strip():
          records.append(json.loads(line))
    return records

  def append(self, record: Mapping[str, Any]) -> None:
    """Appends one record, enforcing turn id contiguity.

    A new record's ``turn_id`` must equal the current maximum plus one
    (starting at 1 for an empty log). Appending a record identical to the
    one with the same ``turn_id`` that is already present is a no-op;
    same number with different content raises an error.

    Args:
      record: the story turn record to append.

    Raises:
      ValueError: if the turn id is not exactly the next one, or if it
        duplicates an existing turn id with different content.
    """
    records = self.load()
    turn_id = record['turn_id']
    if records:
      last = records[-1]
      if turn_id == last['turn_id']:
        if last == dict(record):
          return
        raise ValueError(
            f'turn {turn_id} is already recorded with different content.'
        )
      if turn_id != last['turn_id'] + 1:
        raise ValueError(f'expected turn {last["turn_id"] + 1}, got {turn_id}.')
    elif turn_id != 1:
      raise ValueError(f'the first record must have turn_id 1, got {turn_id}.')
    records.append(dict(record))
    self._rewrite(records)

  def trim_after(self, turn_id: int) -> None:
    """Removes all records with a turn id greater than ``turn_id``.

    This is the recovery-time operation that discards turns written past
    the last committed checkpoint.

    Args:
      turn_id: the highest turn id to keep.
    """
    if not os.path.exists(self._path):
      return
    records = [record for record in self.load() if record['turn_id'] <= turn_id]
    self._rewrite(records)

  def validate_contiguous(self) -> None:
    """Checks that the file holds exactly turns ``1..N`` in order.

    Raises:
      ValueError: if records are missing, duplicated or out of order.
    """
    records = self.load()
    for expected, record in enumerate(records, start=1):
      if record['turn_id'] != expected:
        raise ValueError(
            f'story turn log is not contiguous: position {expected} holds'
            f' turn {record["turn_id"]}.'
        )

  def _rewrite(self, records: list[dict[str, Any]]) -> None:
    text = ''.join(
        json.dumps(record, ensure_ascii=False) + '\n' for record in records
    )
    _atomic_write_text(self._path, text)


def _scene_id(run_config: Mapping[str, Any]) -> str | None:
  """Returns the scene id associated with newly collected turns.

  Stage 1 enacts a single main scene, so every story turn belongs to the
  first scene recorded in the run config. Scene display information lives
  in the story configuration and is associated with turns via this id.

  Args:
    run_config: the run's saved configuration dictionary.

  Returns:
    The scene id, or None when the run config records no scene.
  """
  scenes = run_config.get('scene_dicts') or ()
  if not scenes:
    return None
  return scenes[0].get('scene_type', {}).get('name')


def _read_emotion(
    step_data: step_controller_lib.StepData,
    actor: str,
    emotion_enabled: bool,
) -> str | None:
  """Reads the acting character's emotion from this step's entity logs.

  The emotion is taken only from the acting entity's log entry published
  under the EmotionalStance component key; no fuzzy or spaced-name lookup
  is performed, so stale or foreign entries can never leak in.

  Args:
    step_data: the engine's step data for this step.
    actor: the acting entity's name.
    emotion_enabled: whether emotional stance collection is enabled.

  Returns:
    The emotion label, or None when disabled or unavailable.
  """
  if not emotion_enabled:
    return None
  entity_log = step_data.entity_logs.get(actor, {})
  emotion_log = entity_log.get(EMOTIONAL_STANCE_COMPONENT_KEY)
  if emotion_log is None:
    return None
  return emotion_log.get('Value')


def _read_committed_turn_id(checkpoint_path: str) -> int | None:
  """Returns the committed turn id of the saved checkpoint, if any."""
  if not os.path.exists(checkpoint_path):
    return None
  with open(checkpoint_path, encoding='utf-8') as handle:
    envelope = json.load(handle)
  return envelope.get('committed_turn_id')


def _save_checkpoint(
    *,
    simulation: Any,
    budget_guard: Any,
    checkpoint_path: str,
    run_config: Mapping[str, Any],
    committed_turn_id: int,
    total_turn_limit: int,
    total_model_attempt_limit: int,
) -> None:
  """Atomically saves the run checkpoint envelope.

  The envelope contains the fields of spec §12: the run configuration, the
  standard simulation checkpoint state, the committed turn id, the used
  model attempts, and the original total limits. Saving it is the commit
  boundary for the accompanying story turn.

  Args:
    simulation: the simulation; must expose ``make_checkpoint_data()``.
    budget_guard: the budget guard; must expose ``used_attempts``.
    checkpoint_path: destination file path of the envelope.
    run_config: the run's saved configuration dictionary.
    committed_turn_id: the maximum fully saved story turn id.
    total_turn_limit: the run's original total turn limit.
    total_model_attempt_limit: the run's original model attempt limit.
  """
  envelope = {
      'schema_version': CHECKPOINT_SCHEMA_VERSION,
      'run_config': run_config,
      'simulation_state': simulation.make_checkpoint_data(),
      'committed_turn_id': committed_turn_id,
      'used_model_attempts': budget_guard.used_attempts,
      'total_turn_limit': total_turn_limit,
      'total_model_attempt_limit': total_model_attempt_limit,
  }
  _atomic_write_text(
      checkpoint_path, json.dumps(envelope, ensure_ascii=False, indent=2)
  )


def make_step_callback(
    *,
    simulation: Any,
    drama_director: Any,
    budget_guard: Any,
    turn_log: StoryTurnLog,
    checkpoint_path: str,
    run_config: Mapping[str, Any],
    total_turn_limit: int,
    total_model_attempt_limit: int,
    emotion_enabled: bool,
) -> StepCallback:
  """Builds the step callback that collects and saves story turns.

  The callback first saves the story turn JSONL record and only then the
  checkpoint envelope, making the checkpoint the commit boundary. A
  repeated callback for an already fully committed turn is a no-op; if the
  turn file was written but the checkpoint was not yet committed, the
  retry completes the checkpoint commit. The callback never calls a
  language model and never uses ``StepData.step`` as a global turn id.

  Args:
    simulation: the running simulation; must expose
      ``make_checkpoint_data()``.
    drama_director: the drama director; must expose
      ``get_last_story_turn()``.
    budget_guard: the budget guard; must expose ``used_attempts``.
    turn_log: the story turn log to append to.
    checkpoint_path: destination file path of the checkpoint envelope.
    run_config: the run's saved configuration dictionary.
    total_turn_limit: the run's original total turn limit.
    total_model_attempt_limit: the run's original model attempt limit.
    emotion_enabled: whether emotional stance collection is enabled.

  Returns:
    The step callback to pass to ``Simulation.play``.
  """

  def step_callback(step_data: step_controller_lib.StepData) -> None:
    turn = drama_director.get_last_story_turn()
    if turn is None:
      return

    actor = turn['actor']
    if actor != step_data.acting_entity:
      raise ValueError(
          f'story turn actor {actor!r} does not match the acting entity'
          f' {step_data.acting_entity!r}.'
      )
    # The engine prefixes the action with the speaker's name exactly once;
    # stripping it once must recover the director's recorded speech.
    if turn['speech'] != step_data.action.removeprefix(f'{actor}: '):
      raise ValueError(
          f'story turn speech {turn["speech"]!r} does not match the step'
          f' action {step_data.action!r} stripped of its speaker prefix.'
      )

    record = {
        'turn_id': turn['turn_id'],
        'scene_id': _scene_id(run_config),
        'actor': actor,
        'speech': turn['speech'],
        'resolved_event': turn['resolved_event'],
        'stage_directions': turn['stage_directions'],
        'emotion': _read_emotion(step_data, actor, emotion_enabled),
        'beat_id_at_action': turn['beat_id_at_action'],
        'beat_id_after_event': turn['beat_id_after_event'],
        'tension': turn['tension'],
        'budget_stopped': turn['budget_stopped'],
    }
    turn_log.append(record)

    committed = _read_committed_turn_id(checkpoint_path)
    if committed == record['turn_id']:
      return
    if committed is not None and committed > record['turn_id']:
      raise ValueError(
          f'checkpoint is committed at turn {committed}, ahead of story'
          f' turn {record["turn_id"]}.'
      )
    _save_checkpoint(
        simulation=simulation,
        budget_guard=budget_guard,
        checkpoint_path=checkpoint_path,
        run_config=run_config,
        committed_turn_id=record['turn_id'],
        total_turn_limit=total_turn_limit,
        total_model_attempt_limit=total_model_attempt_limit,
    )

  return step_callback
