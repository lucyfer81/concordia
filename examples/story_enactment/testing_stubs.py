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
per-character dialogue line, the speech-and-action contract call
returns strict JSON preserving the authored speech verbatim, the
final narrator-track request returns a single-field
stage-directions JSON derived from the resolution evidence carried
by the current prompt, and the basic entity's perception questions
receive short fixed answers. Text
routing keys on the LAST `Question:`/`Exercise:` block of the prompt,
so earlier question blocks embedded in a component's context (e.g. the
perception questions quoted inside the action prompt) never hijack the
route. Choice prompts are answered by matching option text rather than
option letter, so the interactive document's letter shuffling cannot
break determinism, and beat-completion answers are configured per beat
id so no beat is completed by default. Every prompt is recorded for
later assertions.

The stub doubles as the deterministic verification instrument of the
propagation tests: its stage-directions output narrates only the
core-adjudicated action result and the intervention candidate carried
by the current prompt's marked evidence section, never configuration
values or anything earlier calls generated, so tests can prove that
adjudicated facts actually reach final generation.
"""

from collections.abc import Collection, Mapping, Sequence
import json
import re
from typing import Any, override

from concordia.contrib.components.game_master import story_action
from concordia.language_model import language_model

# Fingerprint of the shared speech call to action, and the pattern that
# extracts the addressed character's name from it. The
# speech-and-action contract variant is checked first, because its call
# to action contains this fingerprint's text as a prefix.
_SPEECH_CALL_MARK = '现在说什么'
_SPEECH_ACTION_CALL_MARK = '现在说什么、做什么'
_SPEAKER_IN_CALL = re.compile(r'根据你已知的信息，(.+?) 现在说什么')

# Fingerprint of the final narrator-track output instruction built by
# `concordia.contrib.components.game_master.story_resolution`.
_FINAL_OUTPUT_MARK = 'Output a JSON object with exactly one string field'
_REVIEW_MARK = (
    'Evaluate all rules above. Output only a JSON object with exactly'
)

# Heading marking the JSON data section that final-generation prompts
# carry next to their evidence (the turn's speech, action intent,
# core-adjudicated action result and recorded intervention candidate).
# The section is read with a JSON decoder, never with regexes, so
# multi-paragraph speech inside the JSON survives verbatim.
EVIDENCE_SECTION_MARK = '## Resolution evidence (data)'

# Fingerprints of the drama director's tension-scoring output
# instruction (prompt P2) and of the core event-resolution questions
# the stub answers with dedicated parameters.
_SCORING_MARK = 'Respond with a single integer from 1 to 10 and nothing else'
_ATTEMPT_RESULT_MARK = 'What happens as a result of the attempted action?'
_PUSH_SUGGEST_MARK = 'Suggest five plausible events'
_PUSH_MERGE_MARK = 'How does 2 modify'
_PUSH_GIVEN = re.compile(r'Given:\n(.*?)\n\nHow does 2 modify', re.S)

# Fingerprint of the core repetition-detector choice question, and the
# statement fingerprint of the drama director's beat-completion prompt
# plus one '(letter) option' pair per line of the final choice block.
_REPETITION_MARK = (
    'Is the story traced out by the above list of events repetitive?'
)
_CURRENT_BEAT = re.compile(r'Current beat "([^"]+)"')
_CHOICE_LINE = re.compile(r'^  \(([^)]+)\) (.+)$', re.M)

# The last `Question:`/`Exercise:` block opener; task routing always
# reads this final block only.
_LAST_BLOCK_OPENER = re.compile(r'^(?:Question|Exercise): ', re.M)

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


def read_evidence_section(text: str) -> dict[str, Any] | None:
  """Reads the marked JSON resolution-evidence section, if present.

  Final-generation prompts carry the turn's actual evidence (speech,
  action intent, core-adjudicated action result and any recorded
  intervention candidate) in a clearly marked JSON data section. The
  section is decoded with a JSON decoder stopping at the matching close
  brace, never guessed at with regexes, so multi-paragraph speech and
  quotes inside the JSON survive verbatim.

  Args:
    text: the prompt (or its last question block) to read from.

  Returns:
    The decoded evidence object, or None when no marked section holds a
    decodable JSON object.
  """
  index = text.find(EVIDENCE_SECTION_MARK)
  if index < 0:
    return None
  remainder = text[index + len(EVIDENCE_SECTION_MARK) :]
  start = remainder.find('{')
  if start < 0:
    return None
  try:
    payload, _ = json.JSONDecoder().raw_decode(remainder[start:])
  except json.JSONDecodeError:
    return None
  return payload if isinstance(payload, dict) else None


def _intervention_narration_fact(intervention: Mapping[str, Any]) -> str:
  r"""Extracts only the new fact of an intervention candidate.

  A test instrument modeling legal narration, never production
  parsing: the recorded `candidate_after` may embed the original
  event (the speech line) as a same-line suffix, an appended line, or
  the core repetition helper's numbered merge
  (`1. <original>\n2. <event>`). The narration fact is what remains
  after removing the original-event portion, so the stub's narrator
  track never repeats the dialogue.

  Args:
    intervention: the decoded intervention candidate.

  Returns:
    The new-fact text (edge whitespace stripped; this is the stub
    deriving its own narration, not rewriting a model response), or
    '' when the candidate carries no text.
  """
  candidate_after = intervention.get('candidate_after')
  if not isinstance(candidate_after, str) or not candidate_after:
    return ''
  candidate_before = intervention.get('candidate_before')
  fact = candidate_after
  if isinstance(candidate_before, str) and candidate_before:
    if fact.startswith(candidate_before):
      fact = fact[len(candidate_before) :]
    else:
      before_lines = {line.strip() for line in candidate_before.split('\n')}
      kept = []
      for line in fact.split('\n'):
        unnumbered = re.sub(r'^\d+\.\s*', '', line)
        if unnumbered.strip() not in before_lines:
          kept.append(unnumbered)
      fact = '\n'.join(kept)
  return fact.strip()


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
      review_response: str = '{"valid": true, "reason": "scripted approval"}',
      action_intents: Mapping[str, str] | None = None,
      action_result: str = '',
      stage_omissions: Collection[str] = (),
      repetition_answer: str | None = None,
      push_events: str = '',
      push_merged: str = '',
      scoring_answer: str = '',
  ) -> None:
    """Initializes the stub.

    Args:
      dialogue_lines: mapping from character name to the line spoken
        when the speech call to action addresses that character.
      default_line: line spoken for characters not in the mapping.
      stage_directions: fixed background narration appended after the
        evidence-derived facts in the stage-directions track; the
        propagation tests set it to ''.
      choice_answer: option text chosen for choice prompts without a
        configured beat-completion answer.
      completion_answers: mapping from beat id to the yes/no option
        text answered for that beat's completion question; beats left
        out are answered with `choice_answer`, so no beat completes by
        default.
      fail_choice_calls_from: 0-based index from which every
        `sample_choice` call raises `TransientAPIError`, letting tests
        exercise API retry exhaustion.
      review_response: scripted JSON returned for semantic reviews. This
        exercises review plumbing only, never real semantic correctness.
      action_intents: mapping from character name to the action intent
        field of the speech-and-action JSON contract call; the speech
        field still comes from `dialogue_lines`.
      action_result: answer to the core attempt_to_result question
        only. It never enters final generation directly: the
        stage-directions output narrates the action result carried by
        the current prompt's evidence section, never this
        configuration value.
      stage_omissions: substrings deleted from the adjudicated action
        result when the stub narrates it in the stage-directions track
        — the F12 probe of a narration that drops the offending
        sentence while the raw outcome keeps it. Everything else stays
        evidence-derived.
      repetition_answer: yes/no option text answered for the core
        repetition-detector choice question when it is the prompt's
        last choice question.
      push_events: single plausible event (one line, no trailing
        newline) answering the repetition push's suggestion question;
        one legal suggestion keeps the core helper's `random.choice`
        deterministic. Configuring it also enables the merge answer,
        which quotes the actual numbered candidate (preserving the real
        action result) and appends `push_merged` as an optional suffix.
      push_merged: optional suffix appended after the quoted candidate
        when answering the repetition push's merge question. It is a
        test fusion template only: the final stage-directions output
        includes the intervention fact solely from the current
        prompt's evidence.
      scoring_answer: answer to the drama director's tension-scoring
        question.
    """
    self._dialogue_lines = dict(dialogue_lines or {})
    self._default_line = default_line
    self._stage_directions = stage_directions
    self._choice_answer = choice_answer
    self._completion_answers = dict(completion_answers or {})
    self._fail_choice_calls_from = fail_choice_calls_from
    self._review_response = review_response
    self._action_intents = dict(action_intents or {})
    self._action_result = action_result
    self._stage_omissions = tuple(stage_omissions)
    self._repetition_answer = repetition_answer
    self._push_events = push_events
    self._push_merged = push_merged
    self._scoring_answer = scoring_answer
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
    block = _last_question_block(prompt)
    if self._repetition_answer and _REPETITION_MARK in block:
      return self._repetition_answer
    beat = _CURRENT_BEAT.search(prompt)
    if beat:
      return self._completion_answers.get(beat.group(1), self._choice_answer)
    return self._choice_answer

  def _route_text(self, prompt: str) -> str:
    """Routes one text prompt to its canned output."""
    block = _last_question_block(prompt)
    if not block:
      return self._default_line
    if _REVIEW_MARK in block:
      return self._review_response
    if _FINAL_OUTPUT_MARK in block:
      return self._stage_directions_output(block)
    if _SCORING_MARK in block and self._scoring_answer:
      return self._scoring_answer
    if _ATTEMPT_RESULT_MARK in block and self._action_result:
      return self._action_result
    if _PUSH_SUGGEST_MARK in block and self._push_events:
      return self._push_events
    if _PUSH_MERGE_MARK in block and self._push_events:
      return self._merge_output(block)
    if _SPEECH_ACTION_CALL_MARK in block:
      return self._action_contract_output(block)
    if _SPEECH_CALL_MARK in block:
      match = _SPEAKER_IN_CALL.search(block)
      name = match.group(1) if match else ''
      return self._dialogue_lines.get(name, self._default_line)
    for fingerprint, answer in _PERCEPTION_ANSWERS:
      if fingerprint in block:
        return answer
    return self._default_line

  def _action_contract_output(self, prompt: str) -> str:
    """Builds the strict speech-and-action JSON of one call to action.

    The payload is validated through the shared codec so the stub can
    never emit a contract-violating response the drama director would
    treat as a fatal turn error.

    Args:
      prompt: the last question block of the action-contract call.

    Returns:
      A JSON object with exactly the `speech` and `action_intent`
      string fields, the speech preserved verbatim.
    """
    match = _SPEAKER_IN_CALL.search(prompt)
    name = match.group(1) if match else ''
    speech = self._dialogue_lines.get(name, self._default_line)
    action_intent = self._action_intents.get(name, '')
    payload = json.dumps(
        {'speech': speech, 'action_intent': action_intent},
        ensure_ascii=False,
    )
    story_action.decode_action(payload)
    return payload

  def _merge_output(self, block: str) -> str:
    """Composes the repetition-push merge answer from the real candidate.

    The merge question quotes the actual numbered candidate (the
    putative event and the actually selected suggestion); the configured
    `push_merged` text is appended as a suffix, never a replacement, so
    the answer always carries the real candidate forward.

    Args:
      block: the last question block, containing the merge question.

    Returns:
      The quoted candidate plus the configured suffix.
    """
    given = _PUSH_GIVEN.search(block)
    if given is None:
      return self._push_merged
    if self._push_merged:
      return f'{given.group(1)}\n{self._push_merged}'
    return given.group(1)

  def _stage_directions_output(self, prompt: str) -> str:
    """Builds the single-field stage-directions JSON of one final turn.

    The narration carries only NEW facts the current prompt's marked
    evidence section actually holds — the core-adjudicated action
    result and the intervention candidate's new fact, with the
    candidate's original-event portion (the speech line) removed —
    plus the configured background suffix. Nothing is read from
    configuration beyond the background, and nothing from what
    earlier calls generated, so propagation tests can trust the
    output as a faithful record of the evidence passed in. The stub
    never emits dialogue, a speaker line or a `resolved_event`, and
    never re-narrates the candidate's original event: the canonical
    event is assembled by the production step, and duplicating the
    speech into the narrator track would violate the no-repetition
    rule a scripted-approving reviewer cannot catch.

    Args:
      prompt: the last question block of the final-generation prompt.

    Returns:
      The single-field JSON object as a string.
    """
    evidence = read_evidence_section(prompt)
    parts: list[str] = []
    if evidence is not None:
      action_result = evidence.get('action_result')
      if isinstance(action_result, str) and action_result:
        narrated = action_result
        for omission in self._stage_omissions:
          narrated = narrated.replace(omission, '')
        if narrated.strip():
          parts.append(narrated)
      intervention = evidence.get('intervention')
      if isinstance(intervention, Mapping):
        fact = _intervention_narration_fact(intervention)
        if fact:
          parts.append(fact)
    if self._stage_directions:
      parts.append(self._stage_directions)
    return json.dumps(
        {'stage_directions': '\n'.join(parts)},
        ensure_ascii=False,
    )
