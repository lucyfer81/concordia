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
# WITHOUT WARRANTIES OR ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic screenplay rendering of saved story turns.

This module renders spec §7 story turn records (the JSONL records saved by
``collector.py``) into a Fountain screenplay or a plain-text transcript.
Both renderers are deterministic pure functions of their inputs: they make
no language model calls, perform no file IO, and read only the turn
dictionaries and the scene display dictionary.

Element mapping (spec §10):

- scene heading: built once from ``scene_display`` as
  ``INT./EXT. {location} - {time_label}``;
- dialogue: the turn's ``speech`` (never ``resolved_event``, so dialogue
  is never repeated);
- parenthetical: the acting character's ``emotion`` label, omitted when
  the export option ``show_emotion`` is disabled or the label is empty;
- action: the turn's ``stage_directions`` when non-empty.

Fountain rules (spec §8, following fountain.io syntax): scene headings
carry the uppercase ``INT.``/``EXT.`` prefix; CJK character names are not
valid native character cues, so every character cue is forced with a
leading ``@`` (e.g. ``@林雪``); a parenthetical line sits between the
character cue and its dialogue, and elements are separated by blank
lines.

Steps whose ``beat_id_after_event`` is None (the beat completed) render
exactly like any other turn; no extra Fountain element is emitted for
beat completion. The diagnostic fields ``budget_stopped`` and ``tension``
are not rendered (tension visualization is a later task).
"""

from collections.abc import Mapping, Sequence
from typing import Any

# Fountain element type labels returned by parse_fountain.
ELEMENT_TYPES = (
    'scene_heading',
    'character',
    'dialogue',
    'parenthetical',
    'action',
)


def _scene_heading(scene_display: Mapping[str, Any]) -> str:
  """Builds the Fountain scene heading from the scene display mapping.

  Args:
    scene_display: mapping with ``interior`` (bool), ``location`` and
      ``time_label`` (str), as configured in the story configuration.

  Returns:
    The heading, e.g. ``INT. 老宅客厅 - 夜``.
  """
  prefix = 'INT.' if scene_display['interior'] else 'EXT.'
  return f'{prefix} {scene_display["location"]} - {scene_display["time_label"]}'


def _emotion(turn: Mapping[str, Any], show_emotion: bool) -> str | None:
  """Returns the turn's emotion label when it should be rendered."""
  if not show_emotion:
    return None
  return turn.get('emotion') or None


def render_fountain(
    turns: Sequence[dict[str, Any]],
    scene_display: Mapping[str, Any],
    *,
    show_emotion: bool = True,
) -> str:
  """Renders story turns as a Fountain screenplay.

  The scene heading is emitted once at the top, then each turn in order
  as one dialogue block (forced ``@`` character cue, optional
  parenthetical emotion line, dialogue) followed by an action line when
  ``stage_directions`` is non-empty.

  Args:
    turns: story turn records in enactment order (spec §7 fields).
    scene_display: the scene's display information (``interior``,
      ``location``, ``time_label``).
    show_emotion: whether to emit parenthetical emotion lines.

  Returns:
    The Fountain screenplay text, newline-terminated.
  """
  elements = [_scene_heading(scene_display)]
  for turn in turns:
    lines = ['@' + turn['actor']]
    emotion = _emotion(turn, show_emotion)
    if emotion is not None:
      lines.append(f'({emotion})')
    lines.append(turn['speech'])
    elements.append('\n'.join(lines))
    if turn['stage_directions']:
      elements.append(turn['stage_directions'])
  return '\n\n'.join(elements) + '\n'


def render_plain_text(
    turns: Sequence[dict[str, Any]],
    scene_display: Mapping[str, Any],
    *,
    show_emotion: bool = True,
) -> str:
  """Renders story turns as a plain-text transcript.

  The layout mirrors ``render_fountain``: the scene heading once at the
  top, then per turn an ``角色（情绪）：对白`` line (full-width
  parentheses) and, when non-empty, a stage-directions line, each block
  separated by a blank line.

  Args:
    turns: story turn records in enactment order (spec §7 fields).
    scene_display: the scene's display information (``interior``,
      ``location``, ``time_label``).
    show_emotion: whether to include the emotion label in the speaker
      line.

  Returns:
    The transcript text, newline-terminated.
  """
  elements = [_scene_heading(scene_display)]
  for turn in turns:
    emotion = _emotion(turn, show_emotion)
    if emotion is not None:
      elements.append(f'{turn["actor"]}（{emotion}）：{turn["speech"]}')
    else:
      elements.append(f'{turn["actor"]}：{turn["speech"]}')
    if turn['stage_directions']:
      elements.append(turn['stage_directions'])
  return '\n\n'.join(elements) + '\n'


def parse_fountain(text: str) -> list[tuple[str, str]]:
  """Parses Fountain text emitted by ``render_fountain``.

  This test helper understands exactly the element forms the renderer
  emits: scene headings start with ``INT.`` or ``EXT.``; character cues
  start with ``@`` (content excludes the marker); parentheticals start
  with ``(`` (content excludes the parentheses); the first line of a
  dialogue block after a character cue or parenthetical is dialogue, any
  other non-empty line is action.

  Args:
    text: Fountain text, e.g. the output of ``render_fountain``.

  Returns:
    ``(element_type, content)`` pairs in document order, with
    ``element_type`` one of ``ELEMENT_TYPES``.
  """
  elements: list[tuple[str, str]] = []
  in_dialogue_block = False
  for raw_line in text.split('\n'):
    line = raw_line.strip()
    if not line:
      in_dialogue_block = False
    elif line.startswith(('INT.', 'EXT.')):
      elements.append(('scene_heading', line))
      in_dialogue_block = False
    elif line.startswith('@'):
      elements.append(('character', line[1:]))
      in_dialogue_block = True
    elif line.startswith('('):
      elements.append(('parenthetical', line[1:-1]))
    elif in_dialogue_block:
      elements.append(('dialogue', line))
      in_dialogue_block = False
    else:
      elements.append(('action', line))
  return elements
