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

"""Tests using the actual entity component lifecycle."""

import dataclasses
import json
import re
from typing import Any
import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.agent import memory as memory_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_action
from concordia.contrib.language_models import budget_wrapper
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE
)
# NOTE: TERMINATE is a choice-type output, so options are mandatory
# (cf. scene_tracker_dynamic_test.py); the brief omitted them.
_TERMINATE_SPEC = entity_lib.ActionSpec(
    call_to_action='terminate',
    output_type=entity_lib.OutputType.TERMINATE,
    options=('Yes', 'No'),
)

BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=1,
        max_turns=5,
    ),
    drama_director.Beat(
        id='b2',
        description='resolution',
        tension_range=(1, 3),
        done_question='Is the fallout addressed?',
        min_turns=1,
        max_turns=5,
    ),
)


class _BeatModel(mock_model.MockModel):
  """Selects Yes or No by semantic option text, despite shuffled letters."""

  def __init__(self, completed):
    super().__init__('unused')
    self.completed = completed
    self.choice_calls = 0
    self.choice_prompts: list[str] = []

  def sample_choice(self, prompt, responses, *, seed=None):
    self.choice_calls += 1
    self.choice_prompts.append(prompt)
    choices = dict(
        re.findall(r'^  \(([^)]+)\) (Yes|No)$', prompt, re.MULTILINE)
    )
    wanted = 'Yes' if self.completed else 'No'
    label = next(key for key, value in choices.items() if value == wanted)
    return responses.index(label), label, {}


class _ApiErrorModel(mock_model.MockModel):
  """Fails completion queries with a generic API error."""

  def sample_choice(self, prompt, responses, *, seed=None):
    del prompt, responses, seed
    raise RuntimeError('API unavailable')


class _BudgetExhaustedModel(mock_model.MockModel):
  """Fails completion queries by exhausting the attempt budget."""

  def sample_choice(self, prompt, responses, *, seed=None):
    del prompt, responses, seed
    raise budget_wrapper.BudgetExhaustedError('budget spent')


class _ScoringModel(_BeatModel):
  """Adds recorded open-question answers to the beat-choice stub.

  The unit-test harness resolves turns without model calls, so every
  `sample_text` call is a tension-scoring question.
  """

  def __init__(self, completed, text_answer):
    super().__init__(completed)
    self.text_answer = text_answer
    self.text_prompts = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    return self.text_answer


class _BudgetExhaustedTextModel(_BeatModel):
  """Fails scoring questions by exhausting the attempt budget."""

  def __init__(self):
    super().__init__(False)
    self.text_prompts = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    raise budget_wrapper.BudgetExhaustedError('budget spent at scoring')


class _ApiErrorTextModel(_BeatModel):
  """Fails scoring questions with a generic API error."""

  def __init__(self):
    super().__init__(False)

  def sample_text(self, prompt, **kwargs):
    del prompt, kwargs
    raise RuntimeError('API unavailable')


# Fingerprints of the director prompts (plan appendix B, P2 and P3),
# matched against the last question block of each prompt.
_SCORING_MARK = 'Respond with a single integer from 1 to 10'
_DIRECTIVE_MARK = 'Write one short director instruction'
_LAST_BLOCK_OPENER = re.compile(r'^(?:Question|Exercise): ', re.M)

_DIRECTIVE_INSTRUCTION = 'Interrupt the scene with a sudden storm.'

# Director feedback configuration shared by the directive tests.
_DIRECTOR_FEEDBACK_PARAMS = dict(
    eval_interval=1,
    feedback_enabled=True,
    allowed_environment_events=('a storm cuts the power', 'a courier arrives'),
    immutable_facts=('the house is old', 'the will is sealed'),
    player_names=('Alice', 'Bob'),
)


def _last_question_block(prompt: str) -> str:
  """Returns the prompt from its last Question/Exercise block on."""
  matches = list(_LAST_BLOCK_OPENER.finditer(prompt))
  if not matches:
    return ''
  return prompt[matches[-1].start() :]


class _DirectiveModel(_BeatModel):
  """Adds director-pipeline text routing to the beat-choice stub.

  The scoring question returns a configured integer answer and the
  directive-generation question a configured instruction, optionally
  raising a configured error instead.
  """

  def __init__(
      self,
      completed,
      *,
      score: str = '7',
      instruction: str = _DIRECTIVE_INSTRUCTION,
      generation_error: Exception | None = None,
  ):
    super().__init__(completed)
    self._score = score
    self._instruction = instruction
    self._generation_error = generation_error
    self.text_prompts: list[str] = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    block = _last_question_block(prompt)
    if _DIRECTIVE_MARK in block:
      if self._generation_error is not None:
        raise self._generation_error
      return self._instruction
    if _SCORING_MARK in block:
      return self._score
    return 'unused'


class _MemoryProbe(entity_component.ContextComponent):
  """Spy standing in for the game master's memory component.

  Any attribute access beyond the component lifecycle hooks is recorded,
  so a scoring path that read the entity's memory would leave a trace
  even if the prompt text never mentioned it.
  """

  def __init__(self):
    self.accesses = []

  def __getattr__(self, name):
    if not name.startswith('_'):
      self.accesses.append(name)

    def _recorded(*args, **kwargs):
      del args, kwargs
      return []

    return _recorded

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _ResolutionAct(entity_component.ActingComponent):
  """Replaces only resolution generation; lifecycle remains real."""

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


def _make_director(model, beats=BEATS, extra_context=None, **director_params):
  director = drama_director.DramaDirector(
      model=model, beats=beats, **director_params
  )
  context_components = {
      '__drama_director__': director,
      next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
  }
  if extra_context:
    context_components.update(extra_context)
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules',
      act_component=_ResolutionAct(director),
      context_components=context_components,
  )
  return director, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


class LifecycleTest(unittest.TestCase):

  def test_premise_event_not_counted(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    gm.observe('[event] the initial premise')
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_management_queries_do_not_evaluate(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    for _ in range(3):
      gm.act(_TERMINATE_SPEC)
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_full_turn_commits_once(self):
    model = _BeatModel(True)
    director, gm = _make_director(model)
    gm.observe('[putative_event] Alice: I hid the letter.')
    # observe 已完成 post_observe/update；原始对白仍须保留。
    self.assertEqual(
        director.get_current_original_speech(), 'I hid the letter.'
    )
    self.assertEqual(model.choice_calls, 0)
    result = gm.act(_RESOLVE_SPEC)
    # act 已完成 update；等待匹配事件的状态仍须保留。
    self.assertIsNone(director.get_last_story_turn())
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertEqual(turn['actor'], 'Alice')
    self.assertEqual(turn['speech'], 'I hid the letter.')
    self.assertEqual(turn['beat_id_at_action'], 'b1')
    self.assertEqual(turn['beat_id_after_event'], 'b2')
    gm.observe(f'[event] {result}')
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)
    self.assertEqual(model.choice_calls, 1)

  def test_min_turns_blocks_advancement(self):
    model = _BeatModel(True)
    director, gm = _make_director(
        model, beats=(dataclasses.replace(BEATS[0], min_turns=2), BEATS[1])
    )
    _complete_turn(gm, 'one')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b1'
    )
    self.assertEqual(model.choice_calls, 0)
    _complete_turn(gm, 'two')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b2'
    )

  def test_completion_prompt_anchors_plan_p1_template(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)
    prompt = model.choice_prompts[0]
    # Plan appendix B, prompt P1: the beat block and its completion
    # question come first, the event window follows, and the criteria
    # block closes the prompt so the model judges only by events that
    # actually happened, never by intentions or future plans.
    self.assertIn('# Current story beat', prompt)
    self.assertIn('Current beat "b1": setup', prompt)
    self.assertIn('Completion question: Is the evidence revealed?', prompt)
    self.assertIn('## Recent events (oldest first)', prompt)
    self.assertIn('- Alice: I hid the letter.', prompt)
    self.assertIn('## Completion criteria', prompt)
    self.assertIn(
        'Judge only public events that have actually happened.',
        prompt,
    )
    self.assertIn('accusations, requests, proposals, commands', prompt)
    self.assertIn('count GM-imposed voluntary choices', prompt)
    self.assertIn('if evidence is missing, answer No', prompt)
    self.assertLess(
        prompt.index('Completion question:'),
        prompt.index('## Recent events'),
    )
    self.assertLess(
        prompt.index('## Completion criteria'),
        prompt.index('Question:'),
    )

  def test_max_turns_marks_timeout_without_advancing(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    director, gm = _make_director(_BeatModel(False), beats=(beat,))
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    self.assertFalse(director.is_beats_done())
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)

  def test_state_roundtrip(self):
    director, gm = _make_director(_BeatModel(False))
    _complete_turn(gm, 'hi')
    state = director.get_state()
    fresh, _ = _make_director(_BeatModel(False))
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)

  def test_api_error_propagates_from_completion_query(self):
    director, gm = _make_director(_ApiErrorModel())
    gm.observe('[putative_event] Alice: I show the letter.')
    result = gm.act(_RESOLVE_SPEC)
    with self.assertRaises(RuntimeError):
      gm.observe(f'[event] {result}')
    self.assertIsNone(director.get_last_story_turn())

  def test_budget_exhaustion_keeps_timeout_and_budget_flag(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    director, gm = _make_director(_BudgetExhaustedModel(), beats=(beat,))
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    self.assertTrue(director.get_state()['budget_stopped'])
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertTrue(turn['budget_stopped'])

  def test_last_story_turn_is_returned_as_a_copy(self):
    director, gm = _make_director(_BeatModel(False))
    _complete_turn(gm, 'hi')
    turn = director.get_last_story_turn()
    turn['speech'] = 'tampered'
    self.assertEqual(director.get_last_story_turn()['speech'], 'hi')
    self.assertEqual(director.get_state()['last_story_turn']['speech'], 'hi')
    state = director.get_state()
    state['last_story_turn']['speech'] = 'tampered'
    self.assertEqual(director.get_state()['last_story_turn']['speech'], 'hi')

  def test_public_history_excludes_uncommitted_resolution(self):
    director, gm = _make_director(_BeatModel(False))
    _complete_turn(gm, 'first')
    history = director.get_committed_events()
    self.assertEqual(history, ('Alice: first',))
    gm.observe('[putative_event] Alice: second')
    result = gm.act(_RESOLVE_SPEC)
    self.assertEqual(director.get_committed_events(), history)
    gm.observe(f'[event] {result}')
    self.assertEqual(
        director.get_committed_events(), ('Alice: first', 'Alice: second')
    )

  def test_no_directive_diagnostics_and_legacy_restore(self):
    director, gm = _make_director(_BeatModel(False), eval_interval=0)
    _complete_turn(gm, 'hi')
    diagnostics = director.get_last_story_turn()['director_diagnostics']
    self.assertEqual(
        diagnostics,
        {
            'directive_at_start': None,
            'directive_after_resolution': None,
            'directive_after_turn': None,
            'cooldown_active': False,
            'last_executed_turn': None,
            'last_intervention': None,
            'repetition': None,
            'intervention': None,
            'resolution_diagnostics': [],
        },
    )
    diagnostics['directive_at_start'] = 'tampered'
    self.assertIsNone(
        director.get_last_story_turn()['director_diagnostics'][
            'directive_at_start'
        ]
    )
    state = director.get_state()
    del state['last_story_turn']['director_diagnostics']
    fresh, fresh_gm = _make_director(_BeatModel(False), eval_interval=0)
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)
    _complete_turn(fresh_gm, 'second')
    self.assertIn('director_diagnostics', fresh.get_last_story_turn())


class TensionScoringTest(unittest.TestCase):
  """Stage-2 tension evaluation (spec sections 4.4 step 3, 11.1, 11.3)."""

  def test_scoring_runs_only_every_eval_interval_step(self):
    model = _ScoringModel(False, '7')
    director, gm = _make_director(model, eval_interval=2, scoring_window=3)
    for speech in ('one', 'two', 'three', 'four'):
      _complete_turn(gm, speech)
      turn = director.get_last_story_turn()
      expected = 7 if turn['turn_id'] % 2 == 0 else None
      self.assertEqual(turn['tension'], expected, turn['turn_id'])
    # Only turns 2 and 4 asked the scoring question.
    self.assertEqual(len(model.text_prompts), 2)
    # The second scoring prompt's window holds only the most recent
    # `scoring_window` events, oldest first.
    prompt = model.text_prompts[1]
    self.assertNotIn('Alice: one', prompt)
    self.assertIn('- Alice: two', prompt)
    self.assertIn('- Alice: three', prompt)
    self.assertIn('- Alice: four', prompt)
    self.assertLess(prompt.index('- Alice: two'), prompt.index('- Alice: four'))

  def test_deviating_score_recorded_against_beat_at_action(self):
    # BEATS[0] targets (1, 3); a score of 7 with tolerance 1 deviates.
    model = _ScoringModel(False, '7')
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_last_story_turn()['tension'], 7)
    self.assertEqual(
        director.get_state()['tension_history'],
        [{
            'turn_id': 1,
            'beat_id': 'b1',
            'tension': 7,
            'deviated': True,
        }],
    )

  def test_deviation_uses_tolerance_inclusively(self):
    # Scores equal to lower - tolerance or upper + tolerance do not
    # deviate; only strictly outside values do (spec 4.4 step 3).
    beat = dataclasses.replace(BEATS[0], tension_range=(3, 5))
    for score, deviated in (
        ('6', False),
        ('7', True),
        ('2', False),
        ('1', True),
    ):
      with self.subTest(score=score):
        model = _ScoringModel(False, score)
        director, gm = _make_director(model, beats=(beat,), eval_interval=1)
        _complete_turn(gm, 'one')
        self.assertEqual(
            director.get_state()['tension_history'][0]['deviated'], deviated
        )

  def test_invalid_score_retried_once_then_degrades(self):
    model = _ScoringModel(False, 'quite tense')
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    turn = director.get_last_story_turn()
    self.assertIsNone(turn['tension'])
    # Exactly one retry on an unparseable answer, then no further
    # scoring actions (spec 11.1). The re-ask is counted as an
    # output-parse retry, apart from API retries (spec 13.3).
    self.assertEqual(len(model.text_prompts), 2)
    self.assertEqual(director.get_state()['scoring_parse_retries'], 1)
    self.assertEqual(
        director.get_state()['tension_history'],
        [{
            'turn_id': 1,
            'beat_id': 'b1',
            'tension': None,
            'deviated': False,
        }],
    )

  def test_parse_retry_counter_accumulates_and_restores(self):
    model = _ScoringModel(False, 'quite tense')
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    _complete_turn(gm, 'two')
    state = director.get_state()
    self.assertEqual(state['scoring_parse_retries'], 2)
    fresh, _ = _make_director(_ScoringModel(False, 'quite tense'))
    fresh.set_state(state)
    self.assertEqual(fresh.get_state()['scoring_parse_retries'], 2)

  def test_valid_scores_never_count_parse_retries(self):
    model = _ScoringModel(False, '7')
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_state()['scoring_parse_retries'], 0)

  def test_budget_exhausted_at_scoring_still_commits_step(self):
    model = _BudgetExhaustedTextModel()
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertIsNone(turn['tension'])
    self.assertTrue(turn['budget_stopped'])
    self.assertTrue(director.is_story_stopped())
    # Budget path B at the scoring position: no retry, no further calls.
    self.assertEqual(len(model.text_prompts), 1)

  def test_api_error_propagates_from_scoring(self):
    director, gm = _make_director(_ApiErrorTextModel(), eval_interval=1)
    gm.observe('[putative_event] Alice: I show the letter.')
    result = gm.act(_RESOLVE_SPEC)
    with self.assertRaises(RuntimeError):
      gm.observe(f'[event] {result}')
    self.assertIsNone(director.get_last_story_turn())

  def test_scoring_prompt_anchors_window_and_no_memory_read(self):
    model = _ScoringModel(False, '5')
    probe = _MemoryProbe()
    director, gm = _make_director(
        model,
        eval_interval=1,
        scoring_window=2,
        extra_context={memory_lib.DEFAULT_MEMORY_COMPONENT_KEY: probe},
    )
    _complete_turn(gm, 'one')
    _complete_turn(gm, 'two')
    prompt = model.text_prompts[1]
    self.assertIn('## Scale anchors', prompt)
    self.assertIn('1 = calm routine, no conflict or stakes in motion.', prompt)
    self.assertIn(
        '5 = unresolved conflict or stakes, including repeated arguments,',
        prompt,
    )
    self.assertIn(
        '10 = an immediate crisis with new consequential risk',
        prompt,
    )
    self.assertIn('volume and repetition alone do not warrant', prompt)
    self.assertIn(
        'Do not assume characters abandon their private goals', prompt
    )
    self.assertIn('## Recent events (oldest first)', prompt)
    self.assertIn('- Alice: one', prompt)
    self.assertIn('- Alice: two', prompt)
    self.assertIn('## Output', prompt)
    # The target range never enters the scoring prompt: the comparison
    # with the beat's range happens in program code.
    self.assertNotIn('b1', prompt)
    self.assertNotIn('setup', prompt)
    self.assertNotIn('(1, 3)', prompt)
    # Scoring conditions only on the director's own event window; the
    # entity's memory component is never read (verified by spy).
    self.assertEqual(probe.accesses, [])

  def test_invalid_scoring_config_rejected(self):
    model = mock_model.MockModel()
    invalid_configs: tuple[dict[str, Any], ...] = (
        {'eval_interval': -1},
        {'scoring_window': 0},
        {'tolerance': -1},
    )
    for params in invalid_configs:
      with self.subTest(params=params):
        with self.assertRaises(ValueError):
          drama_director.DramaDirector(model=model, beats=BEATS, **params)

  def test_state_roundtrip_preserves_tension_history(self):
    model = _ScoringModel(False, '7')
    director, gm = _make_director(model, eval_interval=1)
    _complete_turn(gm, 'one')
    _complete_turn(gm, 'two')
    state = director.get_state()
    fresh, _ = _make_director(_ScoringModel(False, '7'), eval_interval=1)
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)
    self.assertEqual(len(fresh.get_state()['tension_history']), 2)


class DirectiveGenerationTest(unittest.TestCase):
  """Stage-2 director directives (spec sections 4.4 step 4, 4.5, 11.1)."""

  def test_boundaries_only_enter_private_directive_prompt(self):
    model = _DirectiveModel(False)
    director, gm = _make_director(
        model,
        character_constraints={'Alice': 'Never confess touching the will.'},
        world_constraints=('Bob secretly holds a recording.',),
        **_DIRECTOR_FEEDBACK_PARAMS,
    )
    _complete_turn(gm, 'A courier arrives at the door.')
    generation = next(p for p in model.text_prompts if _DIRECTIVE_MARK in p)
    scoring = next(p for p in model.text_prompts if _SCORING_MARK in p)
    self.assertIn('Never confess touching the will.', generation)
    self.assertIn('Bob secretly holds a recording.', generation)
    self.assertIn('Alice: A courier arrives at the door.', generation)
    self.assertNotIn('Never confess touching the will.', scoring)
    self.assertNotIn('Bob secretly holds a recording.', scoring)
    self.assertNotIn(
        'Bob secretly holds a recording.', director.get_committed_events()[0]
    )

  def test_deviation_generates_pending_directive_bound_to_beat(self):
    # A score of 7 against beat b1's range (1, 3) with tolerance 1
    # deviates, so the pipeline's fourth step creates a pending
    # directive bound to the beat that was current when the player
    # acted.
    model = _DirectiveModel(False)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    state = director.get_state()
    self.assertEqual(
        state['directive'],
        {
            'beat_id': 'b1',
            'generated_at_turn': 1,
            'content': _DIRECTIVE_INSTRUCTION,
            'status': 'pending',
        },
    )
    self.assertIsNone(state['last_directive_executed_turn'])
    diagnostics = director.get_last_story_turn()['director_diagnostics']
    self.assertIsNone(diagnostics['directive_at_start'])
    self.assertIsNone(diagnostics['directive_after_resolution'])
    self.assertEqual(diagnostics['directive_after_turn'], state['directive'])
    # The P3 generation prompt carries the deviation, the direction,
    # the allowed means and the immutable facts (plan appendix B).
    prompt = next(p for p in model.text_prompts if _DIRECTIVE_MARK in p)
    self.assertIn(
        'Current tension (7) is outside the target range (1, 3)', prompt
    )
    self.assertIn('The story needs lower dramatic pressure.', prompt)
    self.assertIn(
        "You may ONLY use these means, never player characters' "
        'voluntary actions: a storm cuts the power; a courier arrives',
        prompt,
    )
    self.assertIn('moves tension lower', prompt)
    self.assertIn(
        '## Recent public events (oldest first)\n- Alice: one', prompt
    )
    self.assertIn('do not introduce an event already established', prompt)
    self.assertIn('Never make players agree, confess, disclose secrets', prompt)
    self.assertIn(
        'Never contradict immutable facts: the house is old; '
        'the will is sealed',
        prompt,
    )

  def test_directive_generation_prefers_actionable_progress(self):
    # A4 (design 7.2): the generation prompt grounds the instruction in
    # a concrete unresolved problem of the recent public events rather
    # than novelty stacking of interruptions or verification chores,
    # while the original player-agency boundary stays verbatim.
    model = _DirectiveModel(False)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_state()['directive']['status'], 'pending')
    prompt = next(p for p in model.text_prompts if _DIRECTIVE_MARK in p)
    self.assertIn('concrete unresolved problem', prompt)
    self.assertIn('novelty for its own sake', prompt)
    self.assertIn('verification or registration chore', prompt)
    self.assertIn('Never make players agree', prompt)

  def test_low_score_directive_asks_to_raise_tension(self):
    beat = dataclasses.replace(BEATS[0], tension_range=(6, 8))
    model = _DirectiveModel(False, score='2')
    director, gm = _make_director(
        model, beats=(beat,), **_DIRECTOR_FEEDBACK_PARAMS
    )
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_state()['directive']['status'], 'pending')
    prompt = next(p for p in model.text_prompts if _DIRECTIVE_MARK in p)
    self.assertIn('The story needs raise dramatic pressure.', prompt)
    self.assertIn('moves tension raise', prompt)

  def test_beat_transition_step_generates_no_directive(self):
    # The completion step records the score but never generates a
    # directive: the score belongs to the old beat (spec 4.4 step 4).
    model = _DirectiveModel(True)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['beat_id_after_event'], 'b2')
    self.assertEqual(turn['tension'], 7)
    self.assertEqual(
        director.get_state()['tension_history'][0]['deviated'], True
    )
    self.assertIsNone(director.get_state()['directive'])
    self.assertFalse(any(_DIRECTIVE_MARK in p for p in model.text_prompts))

  def test_final_completion_step_generates_no_directive(self):
    model = _DirectiveModel(True)
    director, gm = _make_director(
        model, beats=(BEATS[0],), **_DIRECTOR_FEEDBACK_PARAMS
    )
    _complete_turn(gm, 'one')
    self.assertTrue(director.is_beats_done())
    self.assertEqual(director.get_last_story_turn()['tension'], 7)
    self.assertIsNone(director.get_state()['directive'])

  def test_timeout_step_generates_no_directive(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    model = _DirectiveModel(False)
    director, gm = _make_director(
        model, beats=(beat,), **_DIRECTOR_FEEDBACK_PARAMS
    )
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    # The score is still recorded on the timeout step (spec 13.1-4).
    self.assertEqual(director.get_last_story_turn()['tension'], 7)
    self.assertIsNone(director.get_state()['directive'])

  def test_generation_follows_eval_interval(self):
    model = _DirectiveModel(False)
    director, gm = _make_director(model, eval_interval=2, feedback_enabled=True)
    _complete_turn(gm, 'one')
    self.assertIsNone(director.get_state()['directive'])
    _complete_turn(gm, 'two')
    self.assertEqual(director.get_state()['directive']['generated_at_turn'], 2)

  def test_invalid_score_generates_no_directive(self):
    model = _DirectiveModel(False, score='quite tense')
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    self.assertIsNone(director.get_last_story_turn()['tension'])
    self.assertIsNone(director.get_state()['directive'])

  def test_feedback_disabled_never_generates_directives(self):
    # The tension_observe mode only records scores: deviations never
    # produce directives (plan stage 2).
    model = _DirectiveModel(False)
    director, gm = _make_director(
        model, eval_interval=1, feedback_enabled=False
    )
    for speech in ('one', 'two', 'three'):
      _complete_turn(gm, speech)
    self.assertEqual(len(director.get_state()['tension_history']), 3)
    self.assertTrue(
        all(
            entry['deviated']
            for entry in director.get_state()['tension_history']
        )
    )
    self.assertIsNone(director.get_state()['directive'])
    self.assertFalse(any(_DIRECTIVE_MARK in p for p in model.text_prompts))

  def test_budget_exhausted_at_generation_creates_no_directive(self):
    # Budget path B at the directive-generation position (spec 13.1-9):
    # no directive is created, the stop flag is set, and the step still
    # commits with its valid score kept.
    model = _DirectiveModel(
        False,
        generation_error=budget_wrapper.BudgetExhaustedError('spent'),
    )
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertEqual(turn['tension'], 7)
    self.assertTrue(turn['budget_stopped'])
    self.assertIsNone(director.get_state()['directive'])
    self.assertTrue(director.is_story_stopped())

  def test_api_error_at_generation_propagates(self):
    # An API error must never silently become "no directive" (spec
    # 11.1): it propagates and the step does not commit.
    model = _DirectiveModel(
        False, generation_error=RuntimeError('API unavailable')
    )
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    gm.observe('[putative_event] Alice: I show the letter.')
    result = gm.act(_RESOLVE_SPEC)
    with self.assertRaises(RuntimeError):
      gm.observe(f'[event] {result}')
    self.assertIsNone(director.get_last_story_turn())
    self.assertIsNone(director.get_state()['directive'])

  def test_beat_transition_invalidates_pending_directive(self):
    # A pending directive restored from a checkpoint is invalidated by
    # the next beat transition (spec 4.3, 4.5). The plain harness
    # resolves without the director-note step, so the pending directive
    # survives until the transition.
    model = _DirectiveModel(False)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'pending')
    completing = _DirectiveModel(True)
    fresh, fresh_gm = _make_director(completing, **_DIRECTOR_FEEDBACK_PARAMS)
    fresh.set_state(state)
    _complete_turn(fresh_gm, 'two')
    directive = fresh.get_state()['directive']
    self.assertEqual(directive['status'], 'invalidated')
    self.assertEqual(directive['beat_id'], 'b1')
    self.assertEqual(directive['generated_at_turn'], 1)
    self.assertEqual(directive['invalidated_at_turn'], 2)
    diagnostics = fresh.get_last_story_turn()['director_diagnostics']
    self.assertEqual(diagnostics['directive_at_start']['status'], 'pending')
    self.assertEqual(
        diagnostics['directive_after_turn']['status'], 'invalidated'
    )
    # The transition step also generated no replacement directive.
    self.assertEqual(fresh.get_last_story_turn()['beat_id_after_event'], 'b2')
    self.assertFalse(any(_DIRECTIVE_MARK in p for p in completing.text_prompts))

  def test_state_roundtrip_preserves_pending_directive(self):
    model = _DirectiveModel(False)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    state = director.get_state()
    fresh, _ = _make_director(
        _DirectiveModel(False), **_DIRECTOR_FEEDBACK_PARAMS
    )
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)
    pending = fresh.get_pending_directive()
    self.assertEqual(pending['content'], _DIRECTIVE_INSTRUCTION)
    self.assertEqual(pending['beat_id'], 'b1')

  def test_invalid_directive_config_rejected(self):
    with self.assertRaises(ValueError):
      drama_director.DramaDirector(
          model=mock_model.MockModel(),
          beats=BEATS,
          feedback_enabled=True,
          cooldown_turns=-1,
      )


# Long-running beats for the repetition-gate tests: no early completion
# (min_turns high) and no timeout before the ninth turn.
_LONG_BEATS = (
    drama_director.Beat(
        id='b1',
        description='setup',
        tension_range=(1, 3),
        done_question='Is the evidence revealed?',
        min_turns=9,
        max_turns=12,
    ),
    drama_director.Beat(
        id='b2',
        description='resolution',
        tension_range=(1, 3),
        done_question='Is the fallout addressed?',
        min_turns=9,
        max_turns=12,
    ),
)

# A candidate change the repetition helper could propose on turn three.
_PUSH_EVENT = '窗外雷声大作，堂屋的烛火被穿堂风扑灭。'


class _ScriptedScoreModel(_BeatModel):
  """Answers each tension-scoring question from a scripted list.

  The unit-test harness resolves turns without model calls, so every
  `sample_text` call is a scoring question; the answers are consumed in
  call order (later calls reuse the last scripted score).
  """

  def __init__(self, completed, scores):
    super().__init__(completed)
    self._scores = list(scores)
    self.text_prompts = []

  def sample_text(self, prompt, **kwargs):
    del kwargs
    self.text_prompts.append(prompt)
    if self._scores:
      self._last = self._scores.pop(0)
    return self._last


class RepetitionStateTest(unittest.TestCase):
  """Repetition-intervention gating, state and commit semantics (spec 7.2)."""

  _REPETITION_PARAMS = dict(
      eval_interval=0,
      feedback_enabled=True,
      repetition_push_enabled=True,
  )

  def _make(self, model=None, beats=_LONG_BEATS, **params):
    merged = {**self._REPETITION_PARAMS, **params}
    return _make_director(model or _BeatModel(False), beats=beats, **merged)

  def test_third_turn_gate_uses_two_committed_turns(self):
    # The check interval counts real committed story turns plus one for
    # the turn in progress; stuffing the recent-events window is not a
    # substitute (spec 7.2). With interval three the check turns are
    # 3, 6, 9...; turns one and two never qualify.
    director, gm = self._make()
    for speech, eligible in (('one', False), ('two', False)):
      gm.observe(f'[putative_event] Alice: {speech}')
      self.assertFalse(director.should_attempt_repetition_push(), speech)
      result = gm.act(_RESOLVE_SPEC)
      gm.observe(f'[event] {result}')
    self.assertEqual(director.get_state()['story_turn'], 2)
    gm.observe('[putative_event] Alice: three')
    self.assertTrue(director.should_attempt_repetition_push())
    # Every gate precondition is checked against committed switches:
    # either feedback disabled, repetition disabled, or an interval that
    # this turn does not satisfy blocks the check.
    for overrides in (
        dict(feedback_enabled=False),
        dict(repetition_push_enabled=False),
        dict(repetition_check_interval=4),
    ):
      with self.subTest(params=overrides):
        blocked, blocked_gm = self._make(**overrides)
        blocked_gm.observe('[putative_event] Alice: one')
        self.assertFalse(blocked.should_attempt_repetition_push())
    # A stopped story never checks.
    stopped_director, stopped_gm = self._make()
    _complete_turn(stopped_gm, 'one')
    state = stopped_director.get_state()
    state['beat_timeout'] = True
    stopped_director.set_state(state)
    stopped_gm.observe('[putative_event] Alice: two')
    self.assertFalse(stopped_director.should_attempt_repetition_push())

  def test_no_change_only_commits_check(self):
    # The core helper returning the candidate unchanged records a check
    # only: no execution, no shared cooldown, so turn six checks again.
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after='Alice: three',
        attempted=False,
        trace='重复检查 trace：未见重复',
    )
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(state['repetition_execution_count'], 0)
    self.assertIsNone(state['last_intervention'])
    self.assertIsNone(state['last_directive_executed_turn'])
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
    # Without an execution there is no cooldown: turn six may check.
    for speech in ('four', 'five'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: six')
    self.assertTrue(director.should_attempt_repetition_push())

  def test_attempted_but_unchanged_records_attempt_not_change(self):
    # 实际尝试与候选变化分别记录：核心判定重复并完成建议与融合、
    # 但融合返回原文时，attempted=True 而 proposed_change=False——
    # 仍只计检查，不执行、不进共享冷却（spec §7.2）。
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after='Alice: three',
        attempted=True,
        trace='重复检查 trace：判定重复但融合返回原文',
    )
    self.assertIsNone(
        director.get_current_resolution_evidence()['intervention']
    )
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

  def test_changed_candidate_is_not_yet_execution(self):
    # A changed candidate is a proposal: execution is confirmed only by
    # the validated commit, and it then starts the shared cooldown.
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after=f'Alice: three\n{_PUSH_EVENT}',
        attempted=True,
        trace='重复检查 trace：建议并融合新事件',
    )
    result = gm.act(_RESOLVE_SPEC)
    # Validated but not yet committed: nothing executed, no cooldown.
    state = director.get_state()
    self.assertEqual(state['repetition_execution_count'], 0)
    self.assertIsNone(state['last_directive_executed_turn'])
    intervention = director.get_current_resolution_evidence()['intervention']
    self.assertIsNotNone(intervention)
    assert intervention is not None
    self.assertEqual(
        intervention['candidate_after'], f'Alice: three\n{_PUSH_EVENT}'
    )
    gm.observe(f'[event] {result}')
    state = director.get_state()
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(state['repetition_execution_count'], 1)
    self.assertEqual(
        state['last_intervention'], {'turn_id': 3, 'reason': 'repetition'}
    )
    self.assertEqual(state['last_directive_executed_turn'], 3)
    turn = director.get_last_story_turn()
    self.assertTrue(turn['director_diagnostics']['repetition']['executed'])
    # Shared cooldown: with cooldown three, turn six (6 - 3 <= 3) is
    # still suppressed even though the interval is satisfied.
    for speech in ('four', 'five'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: six')
    self.assertFalse(director.should_attempt_repetition_push())

  def test_validated_intervention_commits_once(self):
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after=f'Alice: three\n{_PUSH_EVENT}',
        attempted=True,
        trace='trace',
    )
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    # A repeated event observation cannot re-commit or double-count.
    gm.observe(f'[event] {result}')
    state = director.get_state()
    self.assertEqual(state['repetition_execution_count'], 1)
    self.assertEqual(state['repetition_check_count'], 1)
    self.assertEqual(
        director.get_last_story_turn()['director_diagnostics']['repetition'][
            'executed'
        ],
        True,
    )

  def test_uncommitted_retry_is_eligible(self):
    # An uncommitted check forms no persistent deduplication: a new
    # putative event of the same turn clears the old candidate and the
    # check may run again (spec 7.2: check is not execution).
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after=f'Alice: three\n{_PUSH_EVENT}',
        attempted=True,
        trace='第一次尝试的候选 trace',
    )
    self.assertIsNotNone(
        director.get_current_resolution_evidence()['intervention']
    )
    gm.observe('[putative_event] Alice: three')
    self.assertIsNone(
        director.get_current_resolution_evidence()['intervention']
    )
    self.assertTrue(director.should_attempt_repetition_push())
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after='Alice: three',
        attempted=False,
        trace='第二次尝试只计检查',
    )
    self.assertEqual(
        director.get_current_resolution_diagnostics()['stage'],
        'repetition_check',
    )

  def test_pending_tension_wins_even_if_fusion_fails(self):
    # A pending tension directive at the turn's start occupies the one
    # intervention path of the turn; even a failed fusion (or a mid-turn
    # status change) must not hand the turn to the repetition helper.
    director, gm = _make_director(
        _DirectiveModel(False),
        beats=_LONG_BEATS,
        repetition_push_enabled=True,
        **_DIRECTOR_FEEDBACK_PARAMS,
    )
    _complete_turn(gm, 'one')
    self.assertEqual(director.get_state()['directive']['status'], 'pending')
    gm.observe('[putative_event] Alice: two')
    self.assertFalse(director.should_attempt_repetition_push())
    director.mark_directive_integrating()
    self.assertFalse(director.should_attempt_repetition_push())
    director.mark_directive_failed('fusion found no valid complication')
    self.assertFalse(director.should_attempt_repetition_push())

  def test_rejected_fusion_attempt_restores_pending_on_retry(self):
    # 融合把指令标记为 integrating 后本轮被复核拒绝（未提交、未消
    # 费）：同一实例重试同一轮（新 putative event）必须把指令恢复到
    # 未提交前的 pending，重试轮才能重新融合并正常消费一次；重试轮
    # 的唯一干预路径仍由张力指令占据（spec §7.2）。
    director, gm = _make_director(
        _DirectiveModel(False),
        beats=_LONG_BEATS,
        repetition_push_enabled=True,
        **_DIRECTOR_FEEDBACK_PARAMS,
    )
    _complete_turn(gm, 'one')
    gm.observe('[putative_event] Alice: two')
    director.mark_directive_integrating()
    # 模拟复核拒绝：set_validated_resolution 未被调用，本轮不提交。
    gm.observe('[putative_event] Alice: two')
    pending = director.get_pending_directive()
    self.assertIsNotNone(pending)
    self.assertEqual(pending['status'], 'pending')
    self.assertFalse(director.should_attempt_repetition_push())
    # 重试轮重新融合（再次进入 integrating并记录候选）并提交，恰好
    # 消费一次。
    director.mark_directive_integrating()
    director.record_intervention_candidate(
        reason='tension',
        candidate_before='Alice: two',
        candidate_after='Alice: two 融合后事件',
        trace='重试轮融合 trace',
    )
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    state = director.get_state()
    self.assertEqual(state['directive']['status'], 'consumed')
    self.assertEqual(state['directive']['executed_at_turn'], 2)
    self.assertEqual(state['last_directive_executed_turn'], 2)
    self.assertEqual(director.get_last_story_turn()['turn_id'], 2)

  def test_shared_cooldown_and_reason_survive_restore(self):
    director, gm = self._make()
    for speech in ('one', 'two'):
      _complete_turn(gm, speech)
    gm.observe('[putative_event] Alice: three')
    director.record_repetition_result(
        candidate_before='Alice: three',
        candidate_after=f'Alice: three\n{_PUSH_EVENT}',
        attempted=True,
        trace='trace',
    )
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    state = director.get_state()
    fresh, fresh_gm = self._make()
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)
    # The restored shared baseline suppresses turn six (interval hit,
    # cooldown hit) exactly as before the roundtrip.
    for speech in ('four', 'five'):
      _complete_turn(fresh_gm, speech)
    fresh_gm.observe('[putative_event] Alice: six')
    self.assertFalse(fresh.should_attempt_repetition_push())

  def test_old_state_preserves_tension_cooldown(self):
    # Checkpoints from before repetition tracking lack the new fields:
    # they resume as 0/0/None while the old tension execution baseline
    # keeps its cooldown effect (spec section 9).
    director, gm = self._make()
    for speech in ('one', 'two'):
      gm.observe(f'[putative_event] Alice: {speech}')
      result = gm.act(_RESOLVE_SPEC)
      gm.observe(f'[event] {result}')
    state = director.get_state()
    state['directive'] = {
        'beat_id': 'b1',
        'generated_at_turn': 1,
        'content': 'a storm cuts the power',
        'status': 'consumed',
        'executed_at_turn': 2,
    }
    state['last_directive_executed_turn'] = 2
    del state['repetition_check_count']
    del state['repetition_execution_count']
    del state['last_intervention']
    fresh, fresh_gm = self._make()
    fresh.set_state(state)
    restored = fresh.get_state()
    self.assertEqual(restored['repetition_check_count'], 0)
    self.assertEqual(restored['repetition_execution_count'], 0)
    self.assertIsNone(restored['last_intervention'])
    # Turn three hits the interval but stays inside the restored
    # cooldown of the turn-two tension execution (3 - 2 <= 3).
    fresh_gm.observe('[putative_event] Alice: three')
    self.assertFalse(fresh.should_attempt_repetition_push())
    # Without the legacy baseline the same turn would check.
    no_baseline = dict(state)
    no_baseline['last_directive_executed_turn'] = None
    other, other_gm = self._make()
    other.set_state(no_baseline)
    other_gm.observe('[putative_event] Alice: three')
    self.assertTrue(other.should_attempt_repetition_push())

  def test_accepted_repetition_suppresses_same_turn_directive_generation(self):
    # A repetition execution confirmed in this turn's pending commit
    # projects the shared cooldown onto the same turn's post-processing,
    # so no tension directive is generated for the just-executed turn.
    for proposed_change in (True, False):
      with self.subTest(proposed_change=proposed_change):
        model = _ScriptedScoreModel(False, scores=['3', '3', '7'])
        director, gm = _make_director(
            model,
            beats=_LONG_BEATS,
            eval_interval=1,
            feedback_enabled=True,
            repetition_push_enabled=True,
        )
        _complete_turn(gm, 'one')
        _complete_turn(gm, 'two')
        gm.observe('[putative_event] Alice: three')
        after = (
            f'Alice: three\n{_PUSH_EVENT}'
            if proposed_change
            else ('Alice: three')
        )
        director.record_repetition_result(
            candidate_before='Alice: three',
            candidate_after=after,
            attempted=proposed_change,
            trace='trace',
        )
        result = gm.act(_RESOLVE_SPEC)
        gm.observe(f'[event] {result}')
        state = director.get_state()
        if proposed_change:
          # The projected cooldown suppressed the deviating score's
          # directive generation on the execution turn itself.
          self.assertIsNone(state['directive'])
          self.assertEqual(state['last_directive_executed_turn'], 3)
          self.assertTrue(
              director.get_last_story_turn()['director_diagnostics'][
                  'cooldown_active'
              ]
          )
        else:
          # A check without execution never starts the cooldown: the
          # deviating score still generates a pending directive.
          self.assertEqual(state['directive']['status'], 'pending')
          self.assertEqual(state['directive']['generated_at_turn'], 3)
          self.assertIsNone(state['last_directive_executed_turn'])

  def test_completed_beat_does_not_generate_old_beat_directive(self):
    # Completing a beat in the same turn that executed a repetition
    # intervention advances the story without generating a directive of
    # the old beat: completion and the projected cooldown both block.
    beats = (
        drama_director.Beat(
            id='b1',
            description='setup',
            tension_range=(1, 3),
            done_question='Is the evidence revealed?',
            min_turns=3,
            max_turns=8,
        ),
        drama_director.Beat(
            id='b2',
            description='resolution',
            tension_range=(1, 3),
            done_question='Is the fallout addressed?',
            min_turns=9,
            max_turns=12,
        ),
    )
    for proposed_change in (True, False):
      with self.subTest(proposed_change=proposed_change):
        model = _ScriptedScoreModel(True, scores=['3', '3', '7'])
        director, gm = _make_director(
            model,
            beats=beats,
            eval_interval=1,
            feedback_enabled=True,
            repetition_push_enabled=True,
        )
        _complete_turn(gm, 'one')
        _complete_turn(gm, 'two')
        gm.observe('[putative_event] Alice: three')
        after = (
            f'Alice: three\n{_PUSH_EVENT}'
            if proposed_change
            else ('Alice: three')
        )
        director.record_repetition_result(
            candidate_before='Alice: three',
            candidate_after=after,
            attempted=proposed_change,
            trace='trace',
        )
        result = gm.act(_RESOLVE_SPEC)
        gm.observe(f'[event] {result}')
        turn = director.get_last_story_turn()
        self.assertEqual(turn['beat_id_after_event'], 'b2')
        self.assertIsNone(director.get_state()['directive'])
        self.assertFalse(
            any(
                _DIRECTIVE_MARK in prompt
                for prompt in model.text_prompts  # pytype: disable=attribute-error
            )
        )

  def test_invalid_repetition_config_rejected(self):
    for interval in (0, -3):
      with self.subTest(interval=interval):
        with self.assertRaises(ValueError):
          drama_director.DramaDirector(
              model=mock_model.MockModel(),
              beats=BEATS,
              feedback_enabled=True,
              repetition_push_enabled=True,
              repetition_check_interval=interval,
          )


class ActionContractParsingTest(unittest.TestCase):
  """Current-turn parsing of the speech-and-action contract (spec §3)."""

  def test_speech_and_action_turn_decodes_and_records_raw(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    raw = '{"speech": "我现在核对日期。", "action_intent": "拿出原件查看。"}'
    gm.observe(f'[putative_event] Alice: {raw}')
    self.assertEqual(director.get_current_original_speech(), '我现在核对日期。')
    self.assertEqual(director.get_current_action_intent(), '拿出原件查看。')
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['raw_action'], raw)
    self.assertEqual(turn['speech'], '我现在核对日期。')
    self.assertEqual(turn['action_intent'], '拿出原件查看。')

  def test_invalid_action_json_is_fatal_before_resolution(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    with self.assertRaises(story_action.ActionDecodeError):
      gm.observe('[putative_event] Alice: 我直接说话')
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_speech_only_mode_keeps_raw_speech_and_empty_intent(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    result = _complete_turn(gm, '我只有对白。')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['raw_action'], '我只有对白。')
    self.assertEqual(turn['speech'], '我只有对白。')
    self.assertEqual(turn['action_intent'], '')

  def test_unknown_action_mode_rejected_at_construction(self):
    with self.assertRaises(ValueError):
      drama_director.DramaDirector(
          model=_BeatModel(False), beats=BEATS, action_mode='mime'
      )

  def test_resolution_evidence_is_transient_and_copied(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    _complete_turn(gm, '第一句。')
    committed = director.get_committed_events()
    gm.observe('[putative_event] Alice: 第二句。')
    # 本轮初值为空。
    self.assertEqual(
        director.get_current_resolution_evidence(),
        {'action_result': None, 'intervention': None},
    )
    director.record_action_result('核心裁决返回的实际结果。')
    director.record_intervention_candidate(
        reason='tension',
        candidate_before='候选前',
        candidate_after='候选后',
        trace='候选 trace',
    )
    evidence = director.get_current_resolution_evidence()
    self.assertEqual(evidence['action_result'], '核心裁决返回的实际结果。')
    self.assertEqual(
        evidence['intervention'],
        {
            'reason': 'tension',
            'candidate_before': '候选前',
            'candidate_after': '候选后',
        },
    )
    # 修改返回字典不影响内部暂态。
    evidence['action_result'] = 'tampered'
    evidence['intervention']['reason'] = 'tampered'
    fresh = director.get_current_resolution_evidence()
    self.assertEqual(fresh['action_result'], '核心裁决返回的实际结果。')
    self.assertEqual(fresh['intervention']['reason'], 'tension')
    # 新 putative_event 清空暂态，已提交事件不变。
    gm.observe('[putative_event] Alice: 第三句。')
    self.assertEqual(
        director.get_current_resolution_evidence(),
        {'action_result': None, 'intervention': None},
    )
    self.assertEqual(director.get_committed_events(), committed)

  def test_intervention_candidate_does_not_consume_directive(self):
    # 记录候选只是提案：不消费指令、不记执行、不进冷却；同轮不
    # 得记录两路候选（spec §7.2）。
    model = _DirectiveModel(False)
    director, gm = _make_director(model, **_DIRECTOR_FEEDBACK_PARAMS)
    _complete_turn(gm, 'one')
    pending = director.get_pending_directive()
    self.assertIsNotNone(pending)
    gm.observe('[putative_event] Alice: two')
    director.record_intervention_candidate(
        reason='tension',
        candidate_before='候选前',
        candidate_after='候选后',
        trace='候选 trace',
    )
    # 候选 trace 只进私有诊断，不进对外依据。
    self.assertNotIn('trace', director.get_current_resolution_evidence())
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'intervention_tension')
    self.assertEqual(diagnostics['trace'], '候选 trace')
    # 指令仍是 pending，执行轮与冷却计数未前移。
    self.assertEqual(director.get_pending_directive(), pending)
    self.assertIsNone(director.get_state()['last_directive_executed_turn'])
    with self.assertRaises(ValueError):
      director.record_intervention_candidate(
          reason='repetition',
          candidate_before='候选前',
          candidate_after='候选后',
          trace='第二路候选 trace',
      )
    with self.assertRaises(ValueError):
      director.record_intervention_candidate(
          reason='vibes',
          candidate_before='候选前',
          candidate_after='候选后',
          trace='未知理由 trace',
      )
    self.assertEqual(
        director.get_current_resolution_evidence()['intervention']['reason'],
        'tension',
    )

  def test_diagnostics_are_not_public_context(self):
    # trace 只进私有归档接口，绝不进入已提交的公开事件（spec
    # §7.2）；正式广播隔离于 Task 9/13 再联测。
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    gm.observe(
        '[putative_event] Alice: {"speech": "我说对白。", '
        '"action_intent": "我做动作。"}'
    )
    director.record_resolution_diagnostics(
        stage='adjudication', trace='私有 trace：裁决中途', error=None
    )
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'adjudication')
    self.assertEqual(diagnostics['trace'], '私有 trace：裁决中途')
    self.assertIsNone(diagnostics['error'])
    self.assertIsNone(diagnostics['action_result'])
    self.assertIsNone(diagnostics['intervention'])
    diagnostics['trace'] = 'tampered'
    self.assertEqual(
        director.get_current_resolution_diagnostics()['trace'],
        '私有 trace：裁决中途',
    )
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertNotIn('私有 trace', turn['resolved_event'])
    self.assertNotIn('私有 trace', turn['stage_directions'])
    for public_event in director.get_committed_events():
      self.assertNotIn('私有 trace', public_event)

  def test_parse_failure_does_not_broadcast_or_commit(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    _complete_turn(gm, '{"speech": "已提交对白。", "action_intent": ""}')
    committed = director.get_committed_events()
    calls = model.choice_calls
    with self.assertRaises(story_action.ActionDecodeError):
      gm.observe('[putative_event] Alice: 不是 JSON')
    # 解析失败路径不广播、不提交：失败回合不留暂态，已提交事件、
    # 回合记录与计数不变，也没有任何新的模型调用。
    self.assertIsNone(director.get_current_original_speech())
    self.assertIsNone(director.get_current_action_intent())
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)
    self.assertEqual(director.get_last_story_turn()['speech'], '已提交对白。')
    self.assertEqual(director.get_committed_events(), committed)
    self.assertEqual(director.get_state()['story_turn'], 1)
    self.assertEqual(model.choice_calls, calls)

  def test_decoded_speech_whitespace_is_preserved_verbatim(self):
    model = _BeatModel(False)
    director, gm = _make_director(model, action_mode='speech_and_action')
    speech = '第一句，\n  第二句。'
    raw = json.dumps(
        {'speech': speech, 'action_intent': '意图。'}, ensure_ascii=False
    )
    gm.observe(f'[putative_event] Alice: {raw}')
    # 字段内空白不得规范化：解码保留原文；空白容错只属于 Task 8
    # 的模型草稿比对。
    self.assertEqual(director.get_current_original_speech(), speech)
    result = gm.act(_RESOLVE_SPEC)
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['speech'], speech)
    self.assertEqual(turn['raw_action'], raw)


if __name__ == '__main__':
  unittest.main()
