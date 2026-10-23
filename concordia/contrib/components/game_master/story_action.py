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

"""Shared codec for the story-enactment speech-and-action contract.

Characters in ``speech_and_action`` mode answer a FREE action spec whose
call to action requests a strict JSON object with exactly two string
fields, ``speech`` and ``action_intent`` (design doc section 3). This
module is the single decoder shared by the drama director (current-turn
parsing), the adjudication thought step, and the example's collector and
test stub, so the contract has one definition. Decoding never normalizes
whitespace inside the fields and never guesses actions out of mixed
prose; an invalid input is a fatal, explicit error: the turn is neither
broadcast nor committed, and the model is never re-asked.
"""

import json
import re

ACTION_MODE_SPEECH_ONLY = 'speech_only'
ACTION_MODE_SPEECH_AND_ACTION = 'speech_and_action'
ACTION_MODES = (ACTION_MODE_SPEECH_ONLY, ACTION_MODE_SPEECH_AND_ACTION)

# The exact key set required of the character's JSON output.
ACTION_JSON_KEYS = frozenset({'speech', 'action_intent'})

# Markdown code fences around the JSON object: stripped before decoding,
# mirroring the tolerance of the stage-directions output parser.
_FENCE_START = re.compile(r'^```[a-zA-Z0-9]*\s*')
_FENCE_END = re.compile(r'\s?```\s*$')


class ActionDecodeError(Exception):
  """Raised when a character action violates the output contract.

  Only decoding and contract-validation failures raise this error.
  Language-model API errors and budget exhaustion are unrelated and
  never wrapped.
  """


def validate_action_mode(mode: str) -> None:
  """Checks that ``mode`` names a known action mode.

  Args:
    mode: the configured action mode.

  Raises:
    ValueError: if the mode is not one of ``ACTION_MODES``.
  """
  if mode not in ACTION_MODES:
    raise ValueError(
        f'unknown action mode {mode!r}; expected one of {ACTION_MODES}.'
    )


def decode_action(raw: str) -> tuple[str, str]:
  """Strictly decodes one character action into speech and action intent.

  Args:
    raw: the raw action text after the engine's speaker prefix has been
      stripped once; one surrounding markdown code fence is tolerated.

  Returns:
    A ``(speech, action_intent)`` pair of strings. Either field may be
    empty, never both. Field-internal whitespace is preserved verbatim.

  Raises:
    ActionDecodeError: if the text is not valid JSON, is not an object
      whose key set is exactly ``{'speech', 'action_intent'}``, holds
      non-string values or duplicate keys, or has both fields empty.
  """
  try:
    payload = json.loads(
        _strip_code_fences(raw), object_pairs_hook=_unique_object
    )
  except json.JSONDecodeError as e:
    raise ActionDecodeError(f'not valid JSON: {raw!r}') from e
  if not isinstance(payload, dict) or set(payload) != ACTION_JSON_KEYS:
    raise ActionDecodeError(
        'must be a JSON object with exactly the keys '
        f'{sorted(ACTION_JSON_KEYS)}, got: {raw!r}'
    )
  speech = payload['speech']
  action_intent = payload['action_intent']
  if not isinstance(speech, str) or not isinstance(action_intent, str):
    raise ActionDecodeError(f'both fields must be strings, got: {raw!r}')
  if not speech and not action_intent:
    raise ActionDecodeError('speech and action_intent cannot both be empty.')
  return speech, action_intent


def _strip_code_fences(response: str) -> str:
  """Strips one surrounding markdown code fence pair, if present."""
  text = response.strip()
  if not text.startswith('```'):
    return text
  text = _FENCE_START.sub('', text, count=1)
  return _FENCE_END.sub('', text, count=1).strip()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
  """Rejects duplicate JSON keys instead of trusting the last one."""
  result = {}
  for key, value in pairs:
    if key in result:
      raise ActionDecodeError(f'JSON contains duplicate key {key!r}.')
    result[key] = value
  return result
