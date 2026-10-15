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

"""Unit tests of the evidence-propagating PromptStubModel.

The stub is the deterministic verification instrument of the story
enactment example: propagation tests assert that core-adjudicated
action results and intervention facts actually flow into final
generation, so the stub must derive its dual-track output from the
evidence carried by the CURRENT prompt, never from stale configuration
or from what an earlier call generated. These tests pin that instrument
down in isolation (stage A); the real final-generation prompt format is
wired in a later stage and adopts the marked-section reader unchanged.
"""

import json
from typing import Any
import unittest

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import story_action
from concordia.document import interactive_document

from examples.story_enactment import story_config
from examples.story_enactment import testing_stubs

_CONFIG_ONLY_RESULT = 'CONFIG_ONLY：配置写死的结果，绝不能凭空进入旁白。'
_SUCCEEDED_RESULT = 'ACTION_SUCCEEDED：锁匣被撬开，遗书滑了出来。'
_FAILED_RESULT = 'ACTION_FAILED：锁匣纹丝不动，锁孔已经锈死。'
_PRESET_PUSH_EVENT = '预设新事件：管家破门而入，声称一切早有安排。'
_PUSH_MERGE_SUFFIX = '门外传来急促的敲门声。'
_INTENT = '试图拆开遗嘱的封印。'


def _final_prompt(
    speech: str,
    *,
    action_result: str | None = None,
    intervention: dict[str, Any] | None = None,
    include_evidence: bool = True,
) -> str:
  """Builds a final-generation prompt around one turn's evidence.

  The shape mirrors the P5 template of ``story_resolution._build_prompt``
  with the clearly marked JSON data section that final-generation prompts
  carry (stage A preview of the later wiring), so the stub is exercised
  against the prompt format propagation depends on.

  Args:
    speech: the turn's authored speech.
    action_result: the core-adjudicated action result, or None.
    intervention: the recorded intervention candidate, or None.
    include_evidence: whether the marked evidence section is present.

  Returns:
    The final-generation prompt text.
  """
  evidence_block = ''
  if include_evidence:
    evidence = json.dumps(
        {
            'speech': speech,
            'action_intent': _INTENT,
            'action_result': action_result,
            'intervention': intervention,
        },
        ensure_ascii=False,
    )
    evidence_block = f'{testing_stubs.EVIDENCE_SECTION_MARK}\n{evidence}\n\n'
  return (
      'Question: # Candidate event\n'
      f'林雪: {speech}\n'
      '\n'
      '## Current speaker\n'
      '林雪\n'
      '\n'
      '## Original speech (verbatim)\n'
      f'{speech}\n'
      '\n'
      f'{evidence_block}'
      '## GM-only constraints\n'
      '{}\n'
      '\n'
      '## Output\n'
      'Output a JSON object with exactly two string fields:\n'
      '- "resolved_event": the full canonical event.\n'
      '- "stage_directions": visible outcomes only.\n'
      'Output only the JSON object, no other text.\n'
      'Answer: '
  )


def _scoring_prompt() -> str:
  """Builds the P2 tension-scoring prompt of the drama director.

  Historical question blocks (a perception question and an answered
  beat-completion choice with its beat context) precede the scoring
  output instruction; they must not hijack the route.
  """
  return (
      'Question: What kind of person is 王婶?\nAnswer: x\n'
      'Current beat "b1": 开场\n'
      'Question: 节拍 b1 的完成条件是否已经出现？\n'
      '  (A) Yes\n  (B) No\nAnswer: (B)\n'
      'Rate the current dramatic tension of the story on a scale from 1'
      ' to 10.\n'
      '## Scale anchors\n'
      '1 = calm routine, no conflict or stakes in motion.\n'
      '10 = an immediate crisis with new consequential risk or an'
      ' irreversible turning point.\n'
      '## Recent events (oldest first)\n'
      '- 林雪: 提出质疑\n'
      '- 王婶: 反驳\n'
      '## Output\n'
      'Question: Respond with a single integer from 1 to 10 and nothing'
      ' else.\n'
      'Answer: '
  )


class _RecordingStub(testing_stubs.PromptStubModel):
  """Recording subclass logging every text answer it returns.

  The records let tests prove per-call derivation from the current
  prompt: no returned answer may depend on what an earlier call
  generated, and no hidden extra calls may occur.
  """

  def __init__(self, **kwargs: Any) -> None:
    super().__init__(**kwargs)
    self.text_answers: list[str] = []

  def sample_text(self, prompt, **kwargs):
    answer = super().sample_text(prompt, **kwargs)
    self.text_answers.append(answer)
    return answer


class PromptStubActionTest(unittest.TestCase):
  """Action-contract output and evidence propagation of the stub."""

  def test_json_contract_preserves_multiline_speech(self):
    # The speech-and-action contract must round-trip raw dialogue with
    # newlines, quotes and '##' prefixes verbatim through the shared
    # codec: field-internal whitespace is part of the player's authored
    # speech, so any normalization would corrupt player agency.
    raw_speech = (
        '第一段：“遗嘱的封印从未核验。”\n'
        '## 不是标题，是正文\n'
        '第三段含 "双引号" 与 ## 井号'
    )
    stub = testing_stubs.PromptStubModel(
        dialogue_lines={'林雪': raw_speech},
        action_intents={'林雪': _INTENT},
    )
    call_to_action = (
        story_config.make_speech_and_action_action_spec().call_to_action.format(
            name='林雪'
        )
    )
    prompt = (
        'Question: What kind of person is 林雪?\nAnswer: x\n'
        f'Exercise: {call_to_action}\n'
        'Answer: '
    )
    response = stub.sample_text(prompt)
    speech, action_intent = story_action.decode_action(response)
    self.assertEqual(speech, raw_speech)
    self.assertEqual(action_intent, _INTENT)

  def test_final_output_uses_supplied_action_evidence(self):
    # Two final prompts differing ONLY in the actual evidence (the
    # adjudicated ACTION_SUCCEEDED vs ACTION_FAILED result) must yield
    # different dual-track outputs, each containing only its own actual
    # result. The stub's `action_result` configuration serves the core
    # attempt question alone: without current evidence it must not
    # conjure any result into the final output.
    stub = _RecordingStub(
        action_result=_CONFIG_ONLY_RESULT,
        stage_directions='',
    )
    speech = '遗嘱的封印从未核验。'
    succeeded = json.loads(
        stub.sample_text(_final_prompt(speech, action_result=_SUCCEEDED_RESULT))
    )
    failed = json.loads(
        stub.sample_text(_final_prompt(speech, action_result=_FAILED_RESULT))
    )
    self.assertNotEqual(succeeded['resolved_event'], failed['resolved_event'])
    self.assertIn(_SUCCEEDED_RESULT, succeeded['resolved_event'])
    self.assertNotIn(_FAILED_RESULT, succeeded['resolved_event'])
    self.assertIn(_FAILED_RESULT, failed['resolved_event'])
    self.assertNotIn(_SUCCEEDED_RESULT, failed['resolved_event'])
    for resolved in (succeeded['resolved_event'], failed['resolved_event']):
      self.assertNotIn('CONFIG_ONLY', resolved)
    # A stage-1 prompt without any evidence section conjures nothing
    # from configuration alone.
    legacy = json.loads(stub.sample_text(_final_prompt(speech)))
    self.assertNotIn('CONFIG_ONLY', legacy['resolved_event'])
    # The configured answer still serves the core attempt_to_result
    # question itself.
    attempt_prompt = (
        'Question: What happens as a result of the attempted action?'
        ' Take into account the location and status of each player.\n'
        'Answer: '
    )
    self.assertEqual(stub.sample_text(attempt_prompt), _CONFIG_ONLY_RESULT)
    self.assertEqual(len(stub.text_answers), 4)

  def test_push_is_not_emitted_without_current_intervention(self):
    # The first two turns carry no intervention evidence, so the preset
    # push events configured for the core repetition helper must never
    # surface in the final dual-track output: not through configuration
    # alone, and not through what the push's own model calls generated.
    stub = _RecordingStub(
        repetition_answer='Yes',
        push_events=_PRESET_PUSH_EVENT,
        push_merged=_PUSH_MERGE_SUFFIX,
        stage_directions='',
    )
    speech = '遗嘱的封印从未核验。'
    for turn in (1, 2):
      putative = f'林雪: {speech}（第{turn}轮）'
      document = interactive_document.InteractiveDocument(stub)
      candidate = event_resolution_lib.maybe_inject_narrative_push(
          document, putative, '林雪'
      )
      # Positive control: the push machinery did run and the merged
      # candidate carries the actual putative event and the configured
      # push facts, so the assertions below cannot pass vacuously.
      self.assertIn(putative, candidate)
      self.assertIn(_PRESET_PUSH_EVENT, candidate)
      self.assertIn(_PUSH_MERGE_SUFFIX, candidate)
      final = json.loads(
          stub.sample_text(
              _final_prompt(speech, action_result=_SUCCEEDED_RESULT)
          )
      )
      self.assertIn(_SUCCEEDED_RESULT, final['resolved_event'])
      self.assertIn(speech, final['resolved_event'])
      self.assertNotIn('预设新事件', final['resolved_event'])
      self.assertNotIn('敲门声', final['resolved_event'])
    # Two text calls drive the push (suggestion and merge) and one more
    # answers the final prompt, per turn.
    self.assertEqual(len(stub.text_answers), 6)


class PromptStubRoutingTest(unittest.TestCase):
  """Question-semantic routing of the new stub parameters."""

  def test_scoring_answer_is_stored_and_used(self):
    # An explicitly configured scoring answer must be stored on the
    # instance and returned for the scoring question itself; historical
    # choice markers (an earlier completion question, beat context and
    # a perception question) must not pollute this routing.
    for score in ('5', '9'):
      stub = testing_stubs.PromptStubModel(scoring_answer=score)
      self.assertEqual(
          stub._scoring_answer, score  # pylint: disable=protected-access
      )
      self.assertEqual(stub.sample_text(_scoring_prompt()), score)
    # Without the new parameter the old default behavior is preserved:
    # the scoring question falls through to the default line.
    default_stub = testing_stubs.PromptStubModel()
    self.assertEqual(
        default_stub.sample_text(_scoring_prompt()),
        default_stub._default_line,  # pylint: disable=protected-access
    )

  def test_last_choice_question_controls_repetition_route(self):
    # The repetition answer routes by the LAST choice question of the
    # prompt, mapped semantically to option text (letters shuffle), and
    # neither historical repetition blocks nor historical beat context
    # may hijack the decision.
    stub = testing_stubs.PromptStubModel(
        repetition_answer='Yes',
        completion_answers={'b1': 'No'},
        choice_answer='No',
    )
    historical = (
        'Current beat "b1": 开场\n'
        'Question: 节拍 b1 的完成条件是否已经出现？\n'
        '  (A) Yes\n  (B) No\nAnswer: (A)\n'
    )
    repetition_last = (
        historical
        + 'Question: Is the story traced out by the above list of events'
        ' repetitive?\n'
        '  (A) No\n  (B) Yes\nAnswer: ('
    )
    self.assertEqual(
        stub.sample_choice(repetition_last, ['A', 'B']), (1, 'B', {})
    )
    swapped = repetition_last.replace('(A) No\n  (B) Yes', '(A) Yes\n  (B) No')
    self.assertEqual(stub.sample_choice(swapped, ['A', 'B']), (0, 'A', {}))
    # When the beat-completion question is the last choice question, the
    # historical repetition block must not steal its answer.
    completion_last = (
        'Question: Is the story traced out by the above list of events'
        ' repetitive?\n'
        '  (A) No\n  (B) Yes\nAnswer: (A)\n'
        'Current beat "b1": 开场\n'
        'Question: 节拍 b1 的完成条件是否已经出现？\n'
        '  (A) Yes\n  (B) No\nAnswer: ('
    )
    self.assertEqual(
        stub.sample_choice(completion_last, ['A', 'B']), (1, 'B', {})
    )


if __name__ == '__main__':
  unittest.main()
