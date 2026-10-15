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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic screenplay rendering of saved story turns.

This module renders spec §7 story turn records (the JSONL records saved by
``collector.py``) into an FDX screenplay, a Fountain screenplay, a plain-text
transcript, or a per-turn tension curve plot. The text renderers are pure
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

A turn whose decoded ``speech`` is empty (an adjudicated action-only
turn of schema 2) contributes only its Action narration: no character
cue, no empty dialogue and no invented placeholder such as “沉默片刻”.
Narration comes only from ``stage_directions``; the unexecuted
``action_intent`` and the raw action JSON never enter any script, and
legacy records missing the new fields render unchanged (spec §9).

Fountain rules (spec §8, following fountain.io syntax): scene headings
carry the uppercase ``INT.``/``EXT.`` prefix; CJK character names are not
valid native character cues, so every character cue is forced with a
leading ``@`` (e.g. ``@林雪``); a parenthetical line sits between the
character cue and its dialogue, and elements are separated by blank
lines. FDX instead declares each paragraph's type explicitly and preserves
the original dialogue, including literal markup and whitespace.

Element-boundary encoding for raw content (acceptance F4): a blank line
ends a Fountain dialogue block, so paragraph breaks inside one speech are
rendered as single line breaks — every paragraph of the speech stays in
the same speaker's dialogue block (whitespace is normalized, wording is
never altered). A dialogue line that itself starts with a Fountain
forcing or block marker — ``!`` (action), ``@`` (character), a single
``.`` before an alphanumeric character (scene heading), ``(``
(parenthetical), ``>`` (transition or centered text), ``~`` (lyric),
``#`` (section), ``=`` (synopsis) or ``[[`` (note) — would be read as
that other element by some Fountain parsers, so such lines carry a
backslash before the literal marker. ``parse_fountain`` strips it again,
but external tools such as Screenplain may display it. Use FDX for exact
text interchange. Stage-direction lines whose first characters would
define another element type are each forced to action with a leading
``!``, which Fountain parsers strip from the content. Only the scene
display ever yields a true scene heading.

Steps whose ``beat_id_after_event`` is None (the beat completed) render
exactly like any other turn; no extra Fountain element is emitted for
beat completion. The diagnostic field ``budget_stopped`` is not rendered;
``tension`` is visualized separately by ``render_tension_curve``.
"""

from collections.abc import Mapping, Sequence
from typing import Any
import xml.etree.ElementTree as ET

# Fountain element type labels returned by parse_fountain.
ELEMENT_TYPES = (
    'scene_heading',
    'character',
    'dialogue',
    'parenthetical',
    'action',
)

# Line-starting markers that would force a Fountain element other than
# dialogue; a stage-directions line starting with any of them is forced to
# action with a leading `!`.
_SPECIAL_ACTION_PREFIXES = ('.', '!', '@', '(', '>', '~', '#', '=')

# Line-starting markers that Fountain reads as another element type even
# inside a dialogue block (forced action `!`, forced character `@`,
# forced scene heading `.` before an alphanumeric character,
# parenthetical `(`, forced transition or centered text `>`, lyric `~`,
# section `#`, synopsis `=`, note `[[`). A dialogue line starting with
# one of them is escaped with a backslash.
_DIALOGUE_MARKERS = ('!', '@', '(', '>', '~', '#', '=', '[[')

# The Fountain escape character: the Markdown-style convention the
# syntax reserves for verbatim content (its documented use is emphasis
# characters, and it keeps the escaped line from starting with a
# forcing marker at all).
_DIALOGUE_ESCAPE = '\\'

# Scene-heading prefixes recognized by Fountain parsers, matched
# case-insensitively.
_SCENE_HEADING_PREFIXES = ('INT.', 'EXT.', 'EST.', 'I/E', 'INT/')


def _needs_action_forcing(line: str) -> bool:
  """Decides whether one stage-directions line must be forced to action.

  Args:
    line: the line without leading/trailing whitespace.

  Returns:
    Whether the line's first characters would define a scene heading,
    character cue, parenthetical or other non-action element, so the
    renderer must prefix it with the Fountain action-forcing marker.
  """
  if line.startswith(_SPECIAL_ACTION_PREFIXES):
    return True
  if line.upper().startswith(_SCENE_HEADING_PREFIXES):
    return True
  # A fully uppercase line containing ASCII letters reads as a character
  # cue; forcing keeps it in the action block.
  return line.upper() == line and any(
      character.isascii() and character.isalpha() for character in line
  )


def _encode_action(stage_directions: str) -> str:
  """Encodes a stage-directions block as unambiguous Fountain action.

  Args:
    stage_directions: the raw stage-directions text of one turn.

  Returns:
    The block with every element-ambiguous line forced to action.
  """
  forced_lines = []
  for line in stage_directions.split('\n'):
    stripped = line.strip()
    if _needs_action_forcing(stripped):
      forced_lines.append('!' + stripped)
    else:
      forced_lines.append(stripped)
  return '\n'.join(forced_lines)


def _needs_dialogue_escape(line: str) -> bool:
  """Decides whether one dialogue line must carry the escape character.

  Args:
    line: the line without leading/trailing whitespace.

  Returns:
    Whether the line's first characters would force a Fountain element
    other than dialogue (or are the escape character itself, which must
    be doubled to stay reversible), so the renderer must prefix it with
    the escape character.
  """
  if line.startswith(_DIALOGUE_MARKERS) or line.startswith(_DIALOGUE_ESCAPE):
    return True
  # A single leading period before an alphanumeric character forces a
  # scene heading; ellipses and other punctuation are safe as-is.
  return line.startswith('.') and len(line) > 1 and line[1].isalnum()


def _encode_dialogue(speech: str) -> str:
  """Encodes one speech as a single Fountain dialogue block body.

  Blank lines inside the speech would end the dialogue block, so
  paragraph breaks are normalized to single line breaks; each line is
  stripped of outer whitespace and empty lines are dropped. A line whose
  first characters would force another Fountain element is escaped with
  a backslash, keeping it dialogue; the wording of every line is
  otherwise preserved verbatim.

  Args:
    speech: the raw speech text of one turn.

  Returns:
    The dialogue body, one or more non-blank escaped lines.
  """
  escaped_lines = []
  for line in speech.split('\n'):
    line = line.strip()
    if not line:
      continue
    escaped_lines.append(
        _DIALOGUE_ESCAPE + line if _needs_dialogue_escape(line) else line
    )
  return '\n'.join(escaped_lines)


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


def render_fdx(
    turns: Sequence[dict[str, Any]],
    scene_display: Mapping[str, Any],
    *,
    show_emotion: bool = True,
) -> str:
  """Renders story turns as a Final Draft XML screenplay.

  Each speech occupies one explicitly typed Dialogue paragraph, so literal
  parentheses, backslashes, markup, spaces and line breaks remain text.
  Only an enabled emotion produces a Parenthetical paragraph. Narration
  comes from stage_directions, never resolved_event, to avoid repeating
  dialogue. A turn with empty speech emits only its Action paragraph
  (no Character, Parenthetical or Dialogue). This function performs no
  file IO or model calls.

  Args:
    turns: story turn records in enactment order.
    scene_display: the scene's interior, location and time_label fields.
    show_emotion: whether to emit parenthetical emotion paragraphs.

  Returns:
    A UTF-8-declared FDX document, newline-terminated.
  """
  elements = [('Scene Heading', _scene_heading(scene_display))]
  for turn in turns:
    if turn['speech']:
      elements.append(('Character', turn['actor']))
      emotion = _emotion(turn, show_emotion)
      if emotion is not None:
        elements.append(('Parenthetical', f'({emotion})'))
      elements.append(('Dialogue', turn['speech']))
    if turn['stage_directions']:
      elements.append(('Action', turn['stage_directions']))

  root = ET.Element(
      'FinalDraft', DocumentType='Script', Template='No', Version='1'
  )
  content = ET.SubElement(root, 'Content')
  for element_type, text in elements:
    paragraph = ET.SubElement(content, 'Paragraph', Type=element_type)
    ET.SubElement(paragraph, 'Text').text = text
  document = ET.tostring(root, encoding='utf-8', xml_declaration=True)
  # XML parsers normalize literal carriage returns; references preserve them.
  return document.decode('utf-8').replace('\r', '&#13;') + '\n'


def render_fountain(
    turns: Sequence[dict[str, Any]],
    scene_display: Mapping[str, Any],
    *,
    show_emotion: bool = True,
) -> str:
  """Renders story turns as a Fountain screenplay.

  The scene heading is emitted once at the top, then each turn in order
  as one dialogue block (forced ``@`` character cue, optional
  parenthetical emotion line, dialogue body with paragraph breaks
  normalized to line breaks) followed by an action block when
  ``stage_directions`` is non-empty. A turn with empty speech emits
  only its action block, without a character cue.

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
    if turn['speech']:
      lines = ['@' + turn['actor']]
      emotion = _emotion(turn, show_emotion)
      if emotion is not None:
        lines.append(f'({emotion})')
      lines.append(_encode_dialogue(turn['speech']))
      elements.append('\n'.join(lines))
    if turn['stage_directions']:
      elements.append(_encode_action(turn['stage_directions']))
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
  separated by a blank line. A turn with empty speech omits the speaker
  line and keeps only its stage-directions line.

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
    if turn['speech']:
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
  emits, following the fountain.io element rules: scene headings start
  with ``INT.`` or ``EXT.`` and require a blank line above; character
  cues start with ``@`` (content excludes the marker) and also require a
  blank line above; a line starting with ``(`` between the character cue
  and the dialogue is a parenthetical (content excludes the
  parentheses); every other line of a dialogue block — including
  continuation lines after the first — is dialogue of the same speaker
  with one leading escape character stripped (the renderer's encoding of
  literal markers, see ``_encode_dialogue``), and the block only ends at
  a blank line; a line forced with a leading ``!`` is action with the
  marker stripped, as is any other non-empty line outside a dialogue
  block.

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
    elif line.startswith('!'):
      elements.append(('action', line[1:]))
      in_dialogue_block = False
    elif not in_dialogue_block and line.startswith(('INT.', 'EXT.')):
      elements.append(('scene_heading', line))
    elif not in_dialogue_block and line.startswith('@'):
      elements.append(('character', line[1:]))
      in_dialogue_block = True
    elif line.startswith('('):
      elements.append(('parenthetical', line[1:-1]))
    elif in_dialogue_block:
      if line.startswith(_DIALOGUE_ESCAPE):
        line = line[1:]
      elements.append(('dialogue', line))
    else:
      elements.append(('action', line))
  return elements
