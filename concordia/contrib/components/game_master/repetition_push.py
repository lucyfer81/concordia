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

"""Repetition-push thought step reusing the core narrative-push helper.

When the drama director's committed repetition state allows a check on
the current turn (feedback and the repetition switch enabled, story
running, check interval reached, shared cooldown over, no tension
directive in flight; spec section 7.2), this step builds one
independent ``InteractiveDocument`` on the same budget- and
retry-wrapped model the surrounding chain uses, states the evidence the
repetition judgment may rely on, and calls the core
``maybe_inject_narrative_push`` helper of
``concordia.components.game_master.event_resolution`` unchanged. The
helper is imported and called, never reimplemented: no second
repetition detector, no alternative suggestion generation and no
re-draw loop exist here.

The document explicitly separates the ordered committed public history
from the current turn's candidate (marked as not yet committed), the
adjudicated action result of the current turn, the whitelisted
environmental means, the immutable facts, the player list and the
GM-only boundaries. Future beats, allowed endings and hidden truths are
never stated as established progress; recent conflict alone is not
repetition when new facts, action results or effective responses
occurred.

The judgment rules distinguish effective progress from ineffective
repetition (design section 7.1): a first discovery of a concrete
blockage, new sourced information, an adjudicated action result, a
response resolving a previously unanswered question, or an autonomous
acceptance, refusal or change of strategy each counts as progress,
while restating the same blockage, repeated preparation or rewording
the same request does not; new conflict, absence of physical change and
blocked actions are not stagnation. A ``## Suggestion and fusion
constraints`` statement (design section 7.2) is added to the document
before the helper call, so the core's internal ``copy()`` carries it
into the suggestion and fusion prompts with no new parameters and no
new model calls: both focus any complication on an already-committed,
concrete unresolved blockage instead of stacking new chores. No extra
"is it stagnating" call is added, the core's yes/no judgment is never
overridden, and no turn-count-based forced intervention exists.

The helper's true return value is returned to the chain unchanged and
recorded on the director via ``record_repetition_result``: whether the
helper actually attempted an intervention (judged the story repetitive
and ran its suggestion-and-fusion path) is observed on the outer
document's model responses and recorded separately from the candidate
change — an unchanged candidate still records the attempt but means a
check only, never a proposal, while a changed one is a proposed
intervention candidate that still faces the single-field
stage-directions final generation and the independent review. The
helper may generate several suggestions on an internal copy of the
document and pick one at
random; only the actually selected and merged result (the before/after
the caller can observe) and the outer document's trace are archived,
and the core is never modified or intercepted to expose the
inaccessible internal candidates. API and budget errors — and equally
catchable interruptions such as ``KeyboardInterrupt`` raised inside the
core calls — propagate uncaught after the partial trace is archived in
the transient resolution diagnostics (plan section 11.3, path A): a
failed check is never recorded as a successful or unchanged return.
"""

from collections.abc import Callable

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document
from concordia.language_model import language_model

_JUDGMENT_RULES = (
    'Judge repetition only against the committed public events above. A'
    ' recent quarrel or repeated disagreement alone is NOT repetition'
    ' when new facts, adjudicated action results or effective responses'
    ' to each other occurred. Judge effective progress, not novelty: a'
    ' first discovery of a concrete blockage, new sourced information,'
    ' an adjudicated action result, a response that resolves a'
    ' previously unanswered question, and an autonomous acceptance,'
    ' refusal or change of strategy can each be progress.'
    ' Restating the same blockage, repeated preparation, endlessly'
    ' demanding the same registration, or rewording the same request is'
    ' not new progress; distinguish a first meaningful procedural'
    ' request from unfounded repeated escalation. Movement, lighting or'
    ' registration can be necessary progress when it moves a current'
    ' question toward a definite outcome. New conflict is not'
    ' stagnation, absence of physical change is not stagnation, and a'
    ' blocked action is still an outcome a character may act on.'
    ' One-time events that already happened cannot happen again. The'
    ' current candidate is a proposal for this turn, not an event that'
    ' already occurred; future plans, possible endings and any GM-only'
    ' knowledge are not established progress.'
)

_SUGGESTION_RULES = (
    ' For every suggested complication and its fusion, prefer a legal'
    ' change tied to a concrete unresolved blockage or unanswered'
    ' question in the committed public history. Do not stack another'
    ' interruption, verification or registration chore onto unresolved'
    ' ones merely for novelty. Never instruct a player to change their'
    ' goal, confess, cooperate, or pick a specific ending. Remain within'
    ' the allowed means and information boundaries; these preferences'
    ' never authorize an otherwise illegal event.'
)


def make_repetition_push_step(
    director: drama_director.DramaDirector,
    model: language_model.LanguageModel,
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the repetition-push step of an event-resolution chain.

  Args:
    director: the drama director holding the committed repetition state
      (the gate), the public history and the GM-only boundaries, which
      receives the helper's actual result.
    model: the language model of the surrounding chain — the same
      budget- and retry-wrapped model — used for the independent
      repetition document.

  Returns:
    A thought step with the EventResolution step signature
    ``(document, premise, active_player_name) -> str``. On an ineligible
    turn it returns the candidate unchanged with zero model calls; on an
    eligible turn it calls the core ``maybe_inject_narrative_push``
    helper once on its own document, records the actual result on the
    director, and returns the helper's true return value unchanged.
  """

  def step(
      chain_of_thought: interactive_document.InteractiveDocument,
      candidate_event: str,
      active_player_name: str,
  ) -> str:
    """Maybe pushes the current candidate past a repetitive stretch."""
    del chain_of_thought
    if not director.should_attempt_repetition_push():
      return candidate_event
    document = interactive_document.InteractiveDocument(model)
    _state_repetition_context(
        document=document,
        director=director,
        candidate_event=candidate_event,
        active_player_name=active_player_name,
    )
    responses_before = _count_model_responses(document)
    try:
      pushed = event_resolution_lib.maybe_inject_narrative_push(
          document,
          candidate_event,
          active_player_name,
      )
    except BaseException as error:
      # Path A: archive the partial trace visible before the failure,
      # then propagate the original error untouched. The failed check
      # records no result, so an uncommitted retry may check again.
      # BaseException keeps catchable interruptions (KeyboardInterrupt,
      # SimulatedInterruption) covered too: the trace already holds
      # whatever the core prompt showed before the interrupt — e.g. the
      # merge question quoting the selected event — and losing it would
      # leave an interrupted turn without evidence. Recording here is
      # diagnostics only: no check success, no attempt, no execution.
      director.record_resolution_diagnostics(
          stage='repetition_push',
          trace=document.view().text(),
          error=f'{type(error).__name__}: {error}',
      )
      raise
    # An attempt is observable on the outer document without touching
    # the core: a judgment of "not repetitive" adds exactly one model
    # response (the yes/no choice) and returns, while an attempt adds a
    # second one (the fusion question; the suggestion list is generated
    # on the helper's internal copy and never reaches this document).
    attempted = _count_model_responses(document) - responses_before >= 2
    director.record_repetition_result(
        candidate_before=candidate_event,
        candidate_after=pushed,
        attempted=attempted,
        trace=document.view().text(),
    )
    return pushed

  return step


def _count_model_responses(
    document: interactive_document.InteractiveDocument,
) -> int:
  """Counts the model-generated response entries of the document.

  Args:
    document: the independent repetition document shared with the core
      helper.

  Returns:
    The number of contents the document tags as model responses.
  """
  return len(
      document.view(include_tags=(interactive_document.MODEL_TAG,)).contents()
  )


def _state_repetition_context(
    *,
    document: interactive_document.InteractiveDocument,
    director: drama_director.DramaDirector,
    candidate_event: str,
    active_player_name: str,
) -> None:
  """States the evidence the core repetition judgment conditions on.

  Args:
    document: the independent repetition document shared with the core
      helper.
    director: the director supplying the committed history, the current
      turn's adjudicated action result and the GM-only boundaries.
    candidate_event: the current turn's candidate, not yet committed.
    active_player_name: name of the player whose turn it currently is.
  """
  del active_player_name  # The core helper receives it unchanged too.
  document.statement('# Repetition check (current turn candidate)')
  document.statement('## Committed public events (oldest first)')
  events = director.get_committed_events()
  for event in events:
    document.statement(f'- {event}')
  if not events:
    document.statement('(none)')
  document.statement(
      f'## Current candidate (not yet committed)\n{candidate_event}'
  )
  evidence = director.get_current_resolution_evidence()
  action_result = evidence.get('action_result')
  document.statement("## Current turn's adjudicated action result")
  document.statement(action_result if action_result else '(none)')
  document.statement(
      '## Judgment rules\n'
      f'{_JUDGMENT_RULES}\n'
      'Allowed means for any new event: '
      f"{'; '.join(director.allowed_environment_events) or '(none)'}\n"
      'Immutable facts: '
      f"{'; '.join(director.immutable_facts) or '(none)'}\n"
      'Player characters whose voluntary actions only they can choose: '
      f"{', '.join(director.player_names) or '(none)'}"
  )
  document.statement(
      '## GM-only facts and information boundaries (never disclose these)'
  )
  for name, rule in director.character_constraints.items():
    document.statement(f'- {name}: {rule}')
  for constraint in director.world_constraints:
    document.statement(f'- {constraint}')
  # Stated before the helper call so the core's internal copy() carries
  # it into the suggestion and fusion prompts (design section 7.2).
  document.statement(
      '## Suggestion and fusion constraints\n' + _SUGGESTION_RULES
  )
