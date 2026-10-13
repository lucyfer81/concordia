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
``collector.py``) into a Fountain screenplay, a plain-text transcript, or a
per-turn tension curve plot. The two text renderers are deterministic pure
functions of their inputs: they make no language model calls, perform no
file IO, and read only the turn dictionaries and the scene display
dictionary. ``render_tension_curve`` is likewise deterministic and free of
model calls, but writes its plot image to a file.

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
beat completion. The diagnostic field ``budget_stopped`` is not rendered;
``tension`` is visualized separately by ``render_tension_curve``.
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


def _tension_segments(
    turns: Sequence[Mapping[str, Any]],
) -> list[list[tuple[int, float]]]:
  """Splits turn tensions into contiguous scored segments.

  Turns whose ``tension`` is None end the current segment instead of
  contributing a zero point, so the plotted curve shows a break.

  Args:
    turns: story turn records in enactment order.

  Returns:
    Non-empty lists of ``(turn_id, tension)`` points, one per run of
    consecutive scored turns; empty when no turn is scored.
  """
  segments: list[list[tuple[int, float]]] = []
  current: list[tuple[int, float]] = []
  for index, turn in enumerate(turns):
    tension = turn.get('tension')
    if tension is None:
      if current:
        segments.append(current)
      current = []
      continue
    current.append((int(turn.get('turn_id', index + 1)), float(tension)))
  if current:
    segments.append(current)
  return segments


def render_tension_curve(
    turns: Sequence[dict[str, Any]],
    path: str,
) -> None:
  """Plots the per-turn tension curve and saves it as an image.

  Each scored turn contributes one point at ``(turn_id, tension)`` and
  consecutive scored turns are joined by a line. Turns without a score
  (``tension`` is None, e.g. unscored steps or budget-stopped scoring)
  create a break in the curve rather than being plotted as zero. Turns
  whose ``turn_id`` is missing fall back to their position in the
  sequence. Empty input or input without any score still produces a
  valid empty plot.

  Unlike the text renderers this function performs file IO: it writes
  the plot to ``path``. It forces the non-interactive 'Agg' matplotlib
  backend (imported lazily inside the function so the module stays
  stdlib-only to import), performs no language model calls, and never
  opens a GUI window.

  Args:
    turns: story turn records in enactment order (spec §7 fields).
    path: destination file path for the plot image (format inferred
      from the extension, e.g. ``.png``).
  """
  import matplotlib

  matplotlib.use('Agg')
  from matplotlib import pyplot as plt

  fig, ax = plt.subplots()
  for segment in _tension_segments(turns):
    xs, ys = zip(*segment)
    ax.plot(xs, ys, marker='o')
  ax.set_xlabel('Turn')
  ax.set_ylabel('Tension (1-10)')
  ax.set_ylim(0, 11)
  fig.savefig(path)
  plt.close(fig)


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
