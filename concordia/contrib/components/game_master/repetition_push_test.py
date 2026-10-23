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

"""Tests for the repetition-push thought step.

The step is the thin contrib adapter of the core
``maybe_inject_narrative_push`` helper (spec section 7.1): it gates on
the drama director's committed repetition state, builds one independent
interactive document carrying the ordered committed history, the
not-yet-committed candidate, the current adjudicated action result and
the GM-only boundaries, and calls the core helper unchanged. The
fixtures therefore commit two real story turns through the entity
lifecycle before opening the third (the interval's check turn), and the
language model routes on the last question block of the REAL core
prompts: one yes/no choice for the repetition judgment, one open
question with a single suggestion line (keeping the core's
``random.choice`` deterministic), and one merge question answered by
quoting the actual numbered candidate plus a suffix.
"""

import re
from typing import Any
import unittest
from unittest import mock

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import repetition_push
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE
)

BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=9,
        max_turns=12,
    ),
)

# Fingerprints of the core helper's questions, matched against the last
# question block of each prompt (the core helper's own texts).
_REPETITION_MARK = (
    'Is the story traced out by the above list of events repetitive?'
)
_SUGGEST_MARK = 'Suggest five plausible events'
_MERGE_MARK = 'How does 2 modify'
_MERGE_GIVEN = re.compile(r'Given:\n(.*?)\n\nHow does 2 modify', re.S)
_CHOICE_LINE = re.compile(r'^  \(([^)]+)\) (.+)$', re.M)
_LAST_BLOCK_OPENER = re.compile(r'^(?:Question|Exercise): ', re.M)

# Context fingerprints of the step's independent document.
_HISTORY_MARK = '## Committed public events (oldest first)'
_CANDIDATE_MARK = '## Current candidate (not yet committed)'
_ACTION_RESULT_MARK = "## Current turn's adjudicated action result"
_BOUNDARIES_MARK = 'GM-only facts and information boundaries'

_PUSH_EVENT = '一位不速的堂兄冒雨到访，手里挥着一封自称律师函的信。'
_MERGE_SUFFIX = '堂外的雨骤然密了。'
_ACTION_RESULT = '林雪用钥匙开柜：柜门纹丝不动，须两把钥匙同时转动才能开启。'

_DIRECTOR_PARAMS = dict(
    eval_interval=0,
    feedback_enabled=True,
    repetition_push_enabled=True,
    allowed_environment_events=('a courier arrives', 'a storm cuts power'),
    immutable_facts=('the house is old', 'the will is sealed'),
    player_names=('Alice', 'Bob'),
    character_constraints={'Alice': 'Never confess touching the will.'},
    world_constraints=('Bob secretly holds a recording.',),
)


def _last_question_block(prompt: str) -> str:
  """Returns the prompt from its last Question/Exercise block on."""
  matches = list(_LAST_BLOCK_OPENER.finditer(prompt))
  if not matches:
    return ''
  return prompt[matches[-1].start() :]


class _RepetitionModel(mock_model.MockModel):
  """Routes the real core repetition prompts semantically.

  Attributes:
    text_prompts: every prompt passed to `sample_text`, in call order.
    choice_prompts: every prompt passed to `sample_choice`, in order.
  """

  def __init__(
      self,
      *,
      repetition_answer: str = 'Yes',
      push_event: str = _PUSH_EVENT,
      merge_suffix: str = _MERGE_SUFFIX,
      merge_echo_candidate: bool = False,
      budget_error_at: str | None = None,
      interrupt_at: str | None = None,
  ):
    super().__init__('unused')
    self._repetition_answer = repetition_answer
    self._push_event = push_event
    self._merge_suffix = merge_suffix
    self._merge_echo_candidate = merge_echo_candidate
    self._budget_error_at = budget_error_at
    self._interrupt_at = interrupt_at
    self.text_prompts: list[str] = []
    self.choice_prompts: list[str] = []

  def sample_choice(self, prompt, responses, *, seed=None):
    del seed
    self.choice_prompts.append(prompt)
    if self._budget_error_at == 'choice':
      raise budget_wrapper.BudgetExhaustedError('budget spent at choice')
    if self._interrupt_at == 'choice':
      raise KeyboardInterrupt
    block = _last_question_block(prompt)
    wanted = self._repetition_answer if _REPETITION_MARK in block else 'No'
    pairs = _CHOICE_LINE.findall(block)
    letter = next(key for key, option in pairs if option.strip() == wanted)
    return responses.index(letter), letter, {}

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    block = _last_question_block(prompt)
    if _SUGGEST_MARK in block:
      if self._budget_error_at == 'suggestion':
        raise budget_wrapper.BudgetExhaustedError('budget spent mid-list')
      if self._interrupt_at == 'suggestion':
        raise KeyboardInterrupt
      # One suggestion line keeps the core's random.choice deterministic.
      return self._push_event
    if _MERGE_MARK in block:
      if self._budget_error_at == 'merge':
        raise budget_wrapper.BudgetExhaustedError('budget spent at merge')
      if self._interrupt_at == 'merge':
        raise KeyboardInterrupt
      given = _MERGE_GIVEN.search(block)
      assert given is not None
      if self._merge_echo_candidate:
        # The fusion ran but returned the original candidate verbatim.
        return given.group(1).split('\n', 1)[0].removeprefix('1. ')
      return f'{given.group(1)}\n{self._merge_suffix}'
    return 'unused'


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _CommittingAct(entity_component.ActingComponent):
  """Resolves each turn once, like the drama-director test harness."""

  def __init__(self, director):
    self._director = director

  def get_action_attempt(self, contexts, action_spec):
    if action_spec.output_type != entity_lib.OutputType.RESOLVE:
      return 'No'
    speech = self._director.get_current_original_speech()
    event = f'Alice: {speech}'
    self._director.set_validated_resolution(event, '')
    return f'Event: {event}\n'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_director(model, **params):
  merged: dict[str, Any] = {**_DIRECTOR_PARAMS, **params}
  director = drama_director.DramaDirector(model=model, beats=BEATS, **merged)
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_CommittingAct(director),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
      },
  )
  return director, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


def _open_third_turn(gm, action_result: str | None = None):
  """Commits two real turns, then opens the interval's check turn."""
  gm.observe('[putative_event] Alice: one')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  gm.observe('[putative_event] Alice: two')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  gm.observe('[putative_event] Alice: three')
  if action_result is not None:
    director = gm.get_component('__drama_director__')
    director.record_action_result(action_result)


def _step_call(model, director, candidate='Alice: three'):
  document = interactive_document.InteractiveDocument(model)
  return repetition_push.make_repetition_push_step(director, model)(
      document, candidate, 'Alice'
  )


class RepetitionPushStepTest(unittest.TestCase):
  """The adapter reuses the core helper under the director's gate."""

  def test_ineligible_path_has_zero_calls(self):
    # The gate is read from committed state: without the interval (the
    # first turn of an interval-three run) the step is the identity
    # with zero model calls.
    model = _RepetitionModel()
    director, gm = _make_director(model)
    gm.observe('[putative_event] Alice: one')
    self.assertEqual(_step_call(model, director, 'Alice: one'), 'Alice: one')
    self.assertEqual(model.choice_prompts, [])
    self.assertEqual(model.text_prompts, [])
    # The switch off blocks even a satisfied interval.
    model2 = _RepetitionModel()
    disabled, disabled_gm = _make_director(
        model2, repetition_push_enabled=False, repetition_check_interval=1
    )
    disabled_gm.observe('[putative_event] Alice: one')
    self.assertEqual(_step_call(model2, disabled, 'Alice: one'), 'Alice: one')
    self.assertEqual(model2.choice_prompts, [])
    self.assertEqual(model2.text_prompts, [])

  def test_core_helper_is_called_with_constrained_context(self):
    model = _RepetitionModel()
    director, gm = _make_director(model)
    _open_third_turn(gm, action_result=_ACTION_RESULT)
    candidate = f'Alice: three\n{_ACTION_RESULT}'
    real_core = event_resolution_lib.maybe_inject_narrative_push
    core = mock.Mock(wraps=real_core)
    with mock.patch.object(
        event_resolution_lib, 'maybe_inject_narrative_push', core
    ):
      returned = _step_call(model, director, candidate)
    core.assert_called_once()
    call = core.call_args
    self.assertIsInstance(
        call.args[0], interactive_document.InteractiveDocument
    )
    self.assertEqual(call.args[1], candidate)
    self.assertEqual(call.args[2], 'Alice')
    # The independent document carries the ordered committed history,
    # the candidate explicitly marked as not yet committed, the current
    # adjudicated action result, the whitelist, the immutable facts,
    # the player list and the GM-only boundaries.
    document_text = call.args[0].view().text()
    self.assertIn(_HISTORY_MARK, document_text)
    self.assertIn('- Alice: one', document_text)
    self.assertIn('- Alice: two', document_text)
    self.assertLess(
        document_text.index('- Alice: one'),
        document_text.index('- Alice: two'),
    )
    self.assertIn(f'{_CANDIDATE_MARK}\n{candidate}', document_text)
    self.assertIn('not yet committed', document_text)
    self.assertIn(f'{_ACTION_RESULT_MARK}\n{_ACTION_RESULT}', document_text)
    self.assertIn('a courier arrives', document_text)
    self.assertIn('a storm cuts power', document_text)
    self.assertIn('the house is old', document_text)
    self.assertIn('the will is sealed', document_text)
    self.assertIn('Alice, Bob', document_text)
    self.assertIn(_BOUNDARIES_MARK, document_text)
    self.assertIn('Never confess touching the will.', document_text)
    self.assertIn('Bob secretly holds a recording.', document_text)
    # The judgment constraints distinguish progress (new facts, action
    # results, effective responses) from mere repeated conflict.
    self.assertIn('new facts', document_text)
    self.assertIn('effective responses', document_text)
    # The core question is conditioned on the statements above it.
    repetition_prompt = model.choice_prompts[0]
    self.assertIn(_REPETITION_MARK, repetition_prompt)
    self.assertLess(
        repetition_prompt.index(_CANDIDATE_MARK),
        repetition_prompt.index(f'Question: {_REPETITION_MARK}'),
    )
    # The helper's true return value propagates unchanged.
    self.assertEqual(
        returned, f'1. {candidate}\n2. {_PUSH_EVENT}\n{_MERGE_SUFFIX}'
    )
    self.assertIn('a first discovery of a concrete blockage', document_text)
    self.assertIn('New conflict is not stagnation', document_text)
    suggestion_prompt = next(
        p
        for p in model.text_prompts
        if _SUGGEST_MARK in _last_question_block(p)
    )
    merge_prompt = next(
        p for p in model.text_prompts if _MERGE_MARK in _last_question_block(p)
    )
    for actual_prompt in (suggestion_prompt, merge_prompt):
      self.assertIn('concrete unresolved blockage', actual_prompt)
      self.assertIn('verification or registration chore', actual_prompt)
      self.assertIn(
          'Never instruct a player to change their goal', actual_prompt
      )
    self.assertEqual(len(model.choice_prompts), 1)
    self.assertEqual(len(model.text_prompts), 2)

  def test_judgment_rules_distinguish_progress_from_repetition(self):
    # A4（设计 7.1，落实 run4 设计 6）：有效进展与无效重复的区分
    # 进入判断规则文本；门控与调用数不变。
    rules = repetition_push._JUDGMENT_RULES  # pylint: disable=protected-access
    for positive in (
        'a first discovery of a concrete blockage',
        'an adjudicated action result',
        'an autonomous acceptance, refusal or change of strategy',
        'a blocked action is still an outcome',
    ):
      self.assertIn(positive, rules)
    for negative in (
        'Restating the same blockage',
        'repeated preparation',
        'rewording the same request is not new progress',
        'not stagnation',
    ):
      self.assertIn(negative, rules)
    # 旧边界保留：候选是提案、未来计划/GM-only 不是已发生进展。
    self.assertIn('not an event that already occurred', rules)
    self.assertIn('not established progress', rules)

  def test_unchanged_result_records_check_only(self):
    model = _RepetitionModel(repetition_answer='No')
    director, gm = _make_director(model)
    _open_third_turn(gm)
    returned = _step_call(model, director, 'Alice: three')
    self.assertEqual(returned, 'Alice: three')
    # One choice (the judgment), no suggestion or fusion calls.
    self.assertEqual(len(model.choice_prompts), 1)
    self.assertEqual(model.text_prompts, [])
    # A check without change records no candidate and no execution.
    evidence = director.get_current_resolution_evidence()
    self.assertIsNone(evidence['intervention'])
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'repetition_check')
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(
        turn['director_diagnostics']['repetition'],
        {
            'reason': 'repetition',
            'checked': True,
            'attempted': False,
            'proposed_change': False,
            'executed': False,
        },
    )
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(state['repetition_execution_count'], 0)

  def test_attempted_but_unchanged_records_attempt_not_change(self):
    # 核心判定重复并完成建议与融合，但融合返回原文：实际尝试与候选
    # 变化分别记录——attempted=True、proposed_change=False——因此只
    # 计检查，不进候选、不执行、不占共享冷却（spec §7.2）。
    model = _RepetitionModel(merge_echo_candidate=True)
    director, gm = _make_director(model)
    _open_third_turn(gm)
    returned = _step_call(model, director, 'Alice: three')
    self.assertEqual(returned, 'Alice: three')
    # 判定、建议、融合三次模型调用确实发生（建议在核心内部副本上）。
    self.assertEqual(len(model.choice_prompts), 1)
    self.assertEqual(len(model.text_prompts), 2)
    # 无候选变化：无干预候选进入证据。
    evidence = director.get_current_resolution_evidence()
    self.assertIsNone(evidence['intervention'])
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(
        turn['director_diagnostics']['repetition'],
        {
            'reason': 'repetition',
            'checked': True,
            'attempted': True,
            'proposed_change': False,
            'executed': False,
        },
    )
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(state['repetition_execution_count'], 0)
    self.assertIsNone(state['last_intervention'])
    self.assertIsNone(state['last_directive_executed_turn'])
    # 无执行即无冷却：第六轮（间隔命中）仍可检查。
    for speech in ('four', 'five'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: six')
    self.assertTrue(director.should_attempt_repetition_push())

  def test_changed_result_records_candidate_and_trace_only(self):
    model = _RepetitionModel()
    director, gm = _make_director(model)
    _open_third_turn(gm)
    record = mock.Mock(wraps=director.record_repetition_result)
    with mock.patch.object(director, 'record_repetition_result', record):
      returned = _step_call(model, director, 'Alice: three')
    expected = f'1. Alice: three\n2. {_PUSH_EVENT}\n{_MERGE_SUFFIX}'
    self.assertEqual(returned, expected)
    record.assert_called_once_with(
        candidate_before='Alice: three',
        candidate_after=expected,
        attempted=True,
        trace=mock.ANY,
    )
    # The candidate is a proposal: before/after recorded, executed
    # still False before the validated commit.
    evidence = director.get_current_resolution_evidence()
    self.assertEqual(
        evidence['intervention'],
        {
            'reason': 'repetition',
            'candidate_before': 'Alice: three',
            'candidate_after': expected,
        },
    )
    state = director.get_state()
    self.assertEqual(state['repetition_execution_count'], 0)
    # The independent document's outer trace is archived privately. The
    # suggestion list is generated on the helper's internal document
    # copy and is deliberately not promised here (spec 7.3); the
    # actually selected suggestion is checkable from the merged result.
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'intervention_repetition')
    self.assertIn(_REPETITION_MARK, diagnostics['trace'])
    self.assertIn(_MERGE_MARK, diagnostics['trace'])
    self.assertIn(_PUSH_EVENT, diagnostics['trace'])
    self.assertNotIn(_REPETITION_MARK, evidence)

  def test_action_result_survives_push_merge(self):
    # A turn with both an adjudicated action result and a repetition
    # push: the merged candidate keeps the action result and adds the
    # new environmental fact; the evidence carries both.
    model = _RepetitionModel()
    director, gm = _make_director(model)
    _open_third_turn(gm, action_result=_ACTION_RESULT)
    candidate = f'Alice: three\n{_ACTION_RESULT}'
    returned = _step_call(model, director, candidate)
    self.assertIn(_ACTION_RESULT, returned)
    self.assertIn(_PUSH_EVENT, returned)
    evidence = director.get_current_resolution_evidence()
    self.assertEqual(evidence['action_result'], _ACTION_RESULT)
    intervention = evidence['intervention']
    assert intervention is not None
    self.assertIn(_ACTION_RESULT, intervention['candidate_after'])
    self.assertIn(_PUSH_EVENT, intervention['candidate_after'])
    # The merge answered from the real numbered candidate: the question
    # quoted the actual action result, never a configuration value.
    merge_prompt = next(
        p for p in model.text_prompts if _MERGE_MARK in _last_question_block(p)
    )
    self.assertIn(_ACTION_RESULT, merge_prompt)
    self.assertIn(_PUSH_EVENT, merge_prompt)

  def test_partial_trace_survives_budget_error(self):
    # Budget exhaustion at each core position propagates unwrapped
    # (path A): the partial trace is archived in the transient
    # diagnostics, no success is recorded, and the uncommitted turn may
    # retry the check.
    for position in ('choice', 'suggestion', 'merge'):
      with self.subTest(position=position):
        model = _RepetitionModel(budget_error_at=position)
        director, gm = _make_director(model)
        _open_third_turn(gm)
        record = mock.Mock(wraps=director.record_repetition_result)
        with mock.patch.object(director, 'record_repetition_result', record):
          with self.assertRaises(budget_wrapper.BudgetExhaustedError):
            _step_call(model, director, 'Alice: three')
        record.assert_not_called()
        diagnostics = director.get_current_resolution_diagnostics()
        self.assertEqual(diagnostics['stage'], 'repetition_push')
        self.assertIn('BudgetExhaustedError', diagnostics['error'])
        # The partial trace holds everything outer-visible before the
        # error; the suggestion list lives on the helper's internal
        # copy and is not part of the outer trace (spec 7.3).
        self.assertIn(_HISTORY_MARK, diagnostics['trace'])
        if position != 'choice':
          self.assertIn(_REPETITION_MARK, diagnostics['trace'])
        if position == 'merge':
          self.assertIn(_MERGE_MARK, diagnostics['trace'])
        self.assertIsNone(
            director.get_current_resolution_evidence()['intervention']
        )
        # The failed attempt consumed neither the gate nor a candidate.
        self.assertTrue(director.should_attempt_repetition_push())

  def test_interrupted_core_call_keeps_partial_trace(self):
    # KeyboardInterrupt is not an Exception: it must still archive the
    # outer-visible partial trace (the merge prompt already holds the
    # selected event) before propagating untouched, without recording a
    # successful check or consuming the gate.
    model = _RepetitionModel(interrupt_at='merge')
    director, gm = _make_director(model)
    _open_third_turn(gm)
    record = mock.Mock(wraps=director.record_repetition_result)
    with mock.patch.object(director, 'record_repetition_result', record):
      with self.assertRaises(KeyboardInterrupt):
        _step_call(model, director, 'Alice: three')
    record.assert_not_called()
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'repetition_push')
    self.assertIn('KeyboardInterrupt', diagnostics['error'])
    self.assertIn(_MERGE_MARK, diagnostics['trace'])
    self.assertIn(_PUSH_EVENT, diagnostics['trace'])
    self.assertIsNone(
        director.get_current_resolution_evidence()['intervention']
    )
    self.assertTrue(director.should_attempt_repetition_push())


if __name__ == '__main__':
  unittest.main()
