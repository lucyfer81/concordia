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

"""A minimal semantic prompt stub for story enactment tests.

Unlike `concordia.testing.mock_model.MockModel`, which always returns
one fixed string and always the first choice, this stub routes by
prompt semantics: the shared speech call to action returns a
per-character dialogue line, the dual-track resolution request returns
valid JSON that preserves the authored speech verbatim, and the basic
entity's perception questions receive short fixed answers. Choice
prompts are answered by matching option text rather than option
letter, so the interactive document's letter shuffling cannot break
determinism. Every prompt is recorded for later assertions.

Task 10 extends this class into the full runner-facing stub; keep it
minimal and semantically routed here.
"""

from collections.abc import Collection, Mapping, Sequence
import json
import re
from typing import override

from concordia.language_model import language_model

# Fingerprint of the shared speech call to action, and the pattern that
# extracts the addressed character's name from it.
_SPEECH_CALL_MARK = '现在说什么'
_SPEAKER_IN_CALL = re.compile(r'根据你已知的信息，(.+?) 现在说什么')

# Fingerprints and field patterns of the dual-track resolution prompt
# built by `concordia.contrib.components.game_master.story_resolution`.
_DUAL_TRACK_MARK = 'Output a JSON object with exactly two string fields'
_CURRENT_SPEAKER = re.compile(r'## Current speaker\n(.+)')
_ORIGINAL_SPEECH = re.compile(r'## Original speech \(verbatim\)\n(.*)')

# One '(letter) option' pair per line of a choice prompt.
_CHOICE_LINE = re.compile(r'^  \(([^)]+)\) (.+)$', re.M)

# Fixed answers for the basic entity's perception questions, keyed by a
# fingerprint of the question text.
_PERCEPTION_ANSWERS = (
    ('What kind of situation is', '堂屋里的空气紧得像要断了。'),
    ('What kind of person is', '一个被卷入遗产争执、各怀心事的人。'),
    ('What would a person like', '把心里的疑问说出来。'),
)


class PromptStubModel(language_model.LanguageModel):
  """Deterministic stub routing story prompts to canned outputs.

  Attributes:
    text_prompts: every prompt passed to `sample_text`, in call order.
    choice_prompts: every prompt passed to `sample_choice`, in call
      order.
  """

  def __init__(
      self,
      *,
      dialogue_lines: Mapping[str, str] | None = None,
      default_line: str = '这宅子的事，急不得。',
      stage_directions: str = '窗外的雷声滚过，烛火摇曳。',
      choice_answer: str = 'No',
  ) -> None:
    """Initializes the stub.

    Args:
      dialogue_lines: mapping from character name to the line spoken
        when the speech call to action addresses that character.
      default_line: line spoken for characters not in the mapping.
      stage_directions: fixed stage-directions track of resolutions.
      choice_answer: option text chosen for every choice prompt.
    """
    self._dialogue_lines = dict(dialogue_lines or {})
    self._default_line = default_line
    self._stage_directions = stage_directions
    self._choice_answer = choice_answer
    self.text_prompts: list[str] = []
    self.choice_prompts: list[str] = []

  @override
  def sample_text(
      self,
      prompt: str,
      *,
      max_tokens: int = language_model.DEFAULT_MAX_TOKENS,
      terminators: Collection[str] = language_model.DEFAULT_TERMINATORS,
      temperature: float = language_model.DEFAULT_TEMPERATURE,
      top_p: float = language_model.DEFAULT_TOP_P,
      top_k: int = language_model.DEFAULT_TOP_K,
      timeout: float = language_model.DEFAULT_TIMEOUT_SECONDS,
      seed: int | None = None,
  ) -> str:
    del (  # pyrefly: ignore[unsupported-delete]
        max_tokens,
        terminators,
        temperature,
        top_p,
        top_k,
        timeout,
        seed,
    )
    self.text_prompts.append(prompt)
    return self._route_text(prompt)

  @override
  def sample_choice(
      self,
      prompt: str,
      responses: Sequence[str],
      *,
      seed: int | None = None,
  ) -> tuple[int, str, dict[str, float]]:
    del seed
    self.choice_prompts.append(prompt)
    for letter, option in _CHOICE_LINE.findall(prompt):
      if option.strip() == self._choice_answer:
        return responses.index(letter), letter, {}
    return 0, responses[0], {}

  def _route_text(self, prompt: str) -> str:
    """Routes one text prompt to its canned output."""
    if _DUAL_TRACK_MARK in prompt:
      return self._dual_track_output(prompt)
    if _SPEECH_CALL_MARK in prompt:
      match = _SPEAKER_IN_CALL.search(prompt)
      name = match.group(1) if match else ''
      return self._dialogue_lines.get(name, self._default_line)
    for fingerprint, answer in _PERCEPTION_ANSWERS:
      if fingerprint in prompt:
        return answer
    return self._default_line

  def _dual_track_output(self, prompt: str) -> str:
    """Builds a valid dual-track JSON preserving the speech verbatim."""
    speaker_match = _CURRENT_SPEAKER.search(prompt)
    speech_match = _ORIGINAL_SPEECH.search(prompt)
    speaker = speaker_match.group(1) if speaker_match else ''
    speech = speech_match.group(1) if speech_match else ''
    if not speech or speech == '(none)':
      resolved = f'{speaker} 沉默片刻。'
    else:
      resolved = f'{speaker}: {speech}'
    return json.dumps(
        {
            'resolved_event': resolved,
            'stage_directions': self._stage_directions,
        },
        ensure_ascii=False,
    )
