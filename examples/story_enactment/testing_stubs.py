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

"""A semantic prompt stub for story enactment tests and demos.

Unlike `concordia.testing.mock_model.MockModel`, which always returns
one fixed string and always the first choice, this stub routes by
prompt semantics: the shared speech call to action returns a
per-character dialogue line, the dual-track resolution request returns
valid JSON that preserves the authored speech verbatim, and the basic
entity's perception questions receive short fixed answers. Text
routing keys on the LAST `Question:`/`Exercise:` block of the prompt,
so earlier question blocks embedded in a component's context (e.g. the
perception questions quoted inside the action prompt) never hijack the
route. Choice prompts are answered by matching option text rather than
option letter, so the interactive document's letter shuffling cannot
break determinism, and beat-completion answers are configured per beat
id so no beat is completed by default. Every prompt is recorded for
later assertions.
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

# The last `Question:`/`Exercise:` block opener; task routing always
# reads this final block only.
_LAST_BLOCK_OPENER = re.compile(r'^(?:Question|Exercise): ', re.M)

# Statement fingerprint of the drama director's beat-completion prompt,
# and one '(letter) option' pair per line of the final block.
_CURRENT_BEAT = re.compile(r'Current beat "([^"]+)"')
_CHOICE_LINE = re.compile(r'^  \(([^)]+)\) (.+)$', re.M)

# Fixed answers for the basic entity's perception questions, keyed by a
# fingerprint of the question text.
_PERCEPTION_ANSWERS = (
    ('What kind of situation is', '堂屋里的空气紧得像要断了。'),
    ('What kind of person is', '一个被卷入遗产争执、各怀心事的人。'),
    ('What would a person like', '把心里的疑问说出来。'),
)


class TransientAPIError(Exception):
  """A transient API failure injected by test stubs.

  The stub provider's retry whitelist contains exactly this exception,
  so tests can exercise retry exhaustion deterministically without
  contacting a real service.
  """


def _last_question_block(prompt: str) -> str:
  """Returns the prompt from its last Question/Exercise block on."""
  matches = list(_LAST_BLOCK_OPENER.finditer(prompt))
  if not matches:
    return ''
  return prompt[matches[-1].start() :]


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
      completion_answers: Mapping[str, str] | None = None,
      fail_choice_calls_from: int | None = None,
  ) -> None:
    """Initializes the stub.

    Args:
      dialogue_lines: mapping from character name to the line spoken
        when the speech call to action addresses that character.
      default_line: line spoken for characters not in the mapping.
      stage_directions: fixed stage-directions track of resolutions.
      choice_answer: option text chosen for choice prompts without a
        configured beat-completion answer.
      completion_answers: mapping from beat id to the yes/no option
        text answered for that beat's completion question; beats left
        out are answered with `choice_answer`, so no beat completes by
        default.
      fail_choice_calls_from: 0-based index from which every
        `sample_choice` call raises `TransientAPIError`, letting tests
        exercise API retry exhaustion.
    """
    self._dialogue_lines = dict(dialogue_lines or {})
    self._default_line = default_line
    self._stage_directions = stage_directions
    self._choice_answer = choice_answer
    self._completion_answers = dict(completion_answers or {})
    self._fail_choice_calls_from = fail_choice_calls_from
    self._choice_calls = 0
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
    call_index = self._choice_calls
    self._choice_calls += 1
    if (
        self._fail_choice_calls_from is not None
        and call_index >= self._fail_choice_calls_from
    ):
      raise TransientAPIError(
          f'injected transient failure on choice call {call_index}'
      )
    wanted = self._wanted_option(prompt)
    block = _last_question_block(prompt)
    pairs = _CHOICE_LINE.findall(block)
    for letter, option in pairs:
      if option.strip() == wanted:
        return responses.index(letter), letter, {}
    if pairs:
      # The wanted text is absent; fall back to the first parsed
      # option, still mapped through its letter rather than a fixed
      # index.
      letter = pairs[0][0]
      return responses.index(letter), letter, {}
    return 0, responses[0], {}

  def _wanted_option(self, prompt: str) -> str:
    """Returns the option text semantically wanted for this choice."""
    beat = _CURRENT_BEAT.search(prompt)
    if beat:
      return self._completion_answers.get(beat.group(1), self._choice_answer)
    return self._choice_answer

  def _route_text(self, prompt: str) -> str:
    """Routes one text prompt to its canned output."""
    block = _last_question_block(prompt)
    if not block:
      return self._default_line
    if _DUAL_TRACK_MARK in block:
      return self._dual_track_output(block)
    if _SPEECH_CALL_MARK in block:
      match = _SPEAKER_IN_CALL.search(block)
      name = match.group(1) if match else ''
      return self._dialogue_lines.get(name, self._default_line)
    for fingerprint, answer in _PERCEPTION_ANSWERS:
      if fingerprint in block:
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
