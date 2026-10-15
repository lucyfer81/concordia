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

"""Boundary-preserving encoding for the game master's observation queue.

The core ``MakeObservation`` component joins queued items with a
triple-newline separator when it delivers them to a player. An adjudicated
event whose own text contains a triple newline is therefore indistinguishable
from two queue items after the join: naive splitting breaks the event into
fragments, and tail fragments that do not start with the reserved ``Event: ``
prefix of the resolution chain are dropped by consumers — precisely the
fragments that may carry the evidence that changes a relationship (for
example, an admission that follows a pause).

This module adds an explicit, reversible encoding on the sending side (a
``MakeObservation`` subclass used by the story prefabs) and the matching
decoder for agent-side components. Each queued item is encoded so that it
contains no triple newline: every run of three or more newlines is replaced
by a paragraph-separator escape (U+2028) followed by the run length and a
colon terminator, and any literal escape character is doubled first. The
terminator gives the length field an explicit end boundary, so digits that
open the following paragraph of the item (a year, an amount, a numbering)
can never be absorbed into the run length. The decoder inverts both steps,
so item boundaries and the full item content — including paragraphs that
themselves start with ``Event: `` or a digit — are restored exactly.

The encoded form only differs from the raw item when the item actually
contains a run of three or more newlines; ordinary items pass through
unchanged, so player memory and observation logs stay readable.
"""

import re

from concordia.components.game_master import make_observation as make_observation_lib

# Separator the observation queue inserts between consecutive items.
OBSERVATION_SEPARATOR = '\n\n\n'

# Escape character standing in for a run of newlines inside an item. The
# Unicode LINE SEPARATOR renders as a line break in most viewers, keeping
# encoded observations readable.
_ESCAPE = '\u2028'

# A run of three or more newlines inside a queue item.
_NEWLINE_RUN = re.compile(r'\n{3,}')

# Ends the run-length field of a newline-run escape. Because the encoder
# always emits exactly one colon after the digits, a greedy digit match
# during decoding stops at the terminator and the digits that open the
# next paragraph of the item stay part of the content.
_ESCAPE_TERMINATOR = ':'

# Escape sequences of an encoded item: a doubled escape stands for a
# literal escape character, an escape followed by digits and a colon
# stands for that many newlines. The doubled-escape alternative must
# come first so it wins at any position.
_DECODE_PATTERN = re.compile(
    re.escape(_ESCAPE) + '{2}|' + re.escape(_ESCAPE) + r'(\d+):'
)


def encode_queue_item(item: str) -> str:
  """Encodes one queue item so it contains no triple newline.

  Doubling literal escape characters first keeps the encoding reversible:
  after encoding, an escape character only ever appears doubled (standing
  for itself) or followed by digits and the terminator (standing for a
  newline run).

  Args:
    item: the raw queue item, e.g. ``Event: <event text>\n``.

  Returns:
    The encoded item, safe to join with ``OBSERVATION_SEPARATOR``.
  """
  escaped = item.replace(_ESCAPE, 2 * _ESCAPE)
  return _NEWLINE_RUN.sub(
      lambda match: f'{_ESCAPE}{len(match.group(0))}{_ESCAPE_TERMINATOR}',
      escaped,
  )


def decode_observation(observation: str) -> list[str]:
  """Restores the queue items of one joined observation string.

  The observation is split on the queue separator; because encoded items
  contain no triple newline, every fragment is exactly one item (bar empty
  separator artifacts). Each fragment is unescaped and stripped of outer
  whitespace, matching the stored form consumers kept before the encoding
  existed. Observations from senders that did not encode their items decode
  to the same blocks the previous separator-splitting produced.

  Args:
    observation: the observation string produced by ``MakeObservation``.

  Returns:
    The non-empty queue items, in delivery order, with their newline runs
    and literal escape characters restored.
  """
  items = []
  for fragment in observation.split(OBSERVATION_SEPARATOR):
    restored = _DECODE_PATTERN.sub(_restore_match, fragment).strip()
    if restored:
      items.append(restored)
  return items


def _restore_match(match: re.Match[str]) -> str:
  """Restores one escape sequence of an encoded item."""
  count = match.group(1)
  if count is None:
    return _ESCAPE
  return '\n' * int(count)


class BoundaryEncodingMakeObservation(make_observation_lib.MakeObservation):
  """A ``MakeObservation`` that keeps queue-item boundaries unambiguous.

  The component encodes every item as it enters the observation queue, so
  the triple-newline separator the core join inserts can never be confused
  with newlines inside an item. Delivery, queue state and checkpoints are
  inherited unchanged from the core component; only the queued strings
  differ, and only for items that contain runs of three or more newlines.
  """

  def add_to_queue(self, entity_name: str, event: str):
    """Adds one item to the queue, boundary-encoded.

    Args:
      entity_name: the player the item is queued for.
      event: the raw item text.
    """
    super().add_to_queue(entity_name, encode_queue_item(event))
