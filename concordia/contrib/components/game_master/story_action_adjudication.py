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

"""Action-adjudication thought step reusing the core resolution helper.

In ``speech_and_action`` mode a character turn carries, besides any
spoken dialogue, an ``action_intent`` describing an action the character
proposes to take themself. The drama director decodes and holds both
for the current turn; this step adjudicates the attempt by calling the
core ``attempt_to_result`` helper from
``concordia.components.game_master.event_resolution`` once, on the
same adjudication document the surrounding chain uses, after stating
the original dialogue, the character's own attempt, the committed
public history and the GM-only facts and authorization boundaries. The
core helper is imported and called, never reimplemented or copied: its
true return value is recorded on the director via
``record_action_result`` and returned unchanged, so the later chain
steps (director note, final dual-track output, semantic review) all
consume the actual adjudicated outcome, never the intent or any stub
configuration.

The prompt constraints follow the Concordia design pattern's
player-agency rule (the game master never decides voluntary actions
for player characters): only the current character's own voluntarily
proposed attempt is adjudicated; success, failure or being blocked is
judged against the configuration and the committed public history; no
voluntary action is decided for any other player character; and no
physical evidence is fabricated that no committed event established. A
failed or blocked attempt still receives one definite actual outcome,
never a deferral to later verification.

Turns without an action intent (``speech_only`` mode, or an empty
``action_intent`` field) have nothing to adjudicate: the step replaces
the raw JSON premise with the clean attributed dialogue of the turn,
makes zero language-model calls, and infers no past action from spoken
claims (a recording mentioned in speech is not an established
recording). API and budget errors propagate uncaught, unretried and
unwrapped: adjudication runs during the game master's RESOLVE act,
before the resolution commits, so budget exhaustion takes the plan's
path A and ``set_validated_resolution`` is never reached early.
"""

from collections.abc import Callable

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document

_ADJUDICATION_RULES = (
    'Adjudicate only the attempted action the current speaker proposed'
    ' for themself this turn. Judge from the GM-only facts and the committed'
    ' public events whether the attempt succeeds, fails or is blocked. Never'
    ' decide a new voluntary action for any other player character, and never'
    ' fabricate physical evidence that no committed event established. A'
    ' failed or blocked attempt still gets one definite actual result; never'
    ' defer the outcome to a later verification.'
)


def make_action_adjudication_step(
    director: drama_director.DramaDirector,
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the action-adjudication step of an event-resolution chain.

  Args:
    director: the drama director holding the current turn's decoded
      speech and action intent, which receives the adjudicated result.

  Returns:
    A thought step with the EventResolution step signature
    ``(document, premise, active_player_name) -> str``. A turn with an
    action intent is adjudicated by exactly one call to the core
    ``attempt_to_result`` helper, whose true return value is recorded
    on the director and returned unchanged. A turn without one has its
    raw JSON premise replaced by the clean attributed dialogue, with
    zero model calls.
  """

  def step(
      chain_of_thought: interactive_document.InteractiveDocument,
      candidate_event: str,
      active_player_name: str,
  ) -> str:
    """Adjudicates the current character's own action attempt, if any."""
    action_intent = director.get_current_action_intent()
    if not action_intent:
      return _clean_speech_premise(
          director=director,
          candidate_event=candidate_event,
          active_player_name=active_player_name,
      )
    original_speech = director.get_current_original_speech()
    _state_adjudication_context(
        chain_of_thought=chain_of_thought,
        director=director,
        active_player_name=active_player_name,
        original_speech=original_speech,
        action_intent=action_intent,
    )
    # The core helper runs as-is, once: it appends its question to the
    # adjudication document and makes exactly one model call. API and
    # budget errors propagate uncaught (path A: nothing commits).
    result = event_resolution_lib.attempt_to_result(
        chain_of_thought,
        action_intent,
        active_player_name,
    )
    # Record the core's true return value before returning the same
    # value, so final generation and review consume the actual
    # adjudicated outcome, never the intent or stub configuration.
    director.record_action_result(result)
    return result

  return step


def _clean_speech_premise(
    director: drama_director.DramaDirector,
    candidate_event: str,
    active_player_name: str,
) -> str:
  """Returns the turn's clean attributed dialogue, with zero model calls.

  Args:
    director: the drama director holding the current turn's speech.
    candidate_event: the premise the chain would otherwise carry on.
    active_player_name: name of the player whose turn it currently is.

  Returns:
    The speaker-attributed original speech, replacing the raw JSON
    action contract as the premise for the rest of the chain. Without
    turn dialogue (e.g. outside a story turn) the premise passes
    through unchanged rather than inventing dialogue; no past action
    is ever inferred from spoken claims.
  """
  original_speech = director.get_current_original_speech()
  if not original_speech:
    return candidate_event
  return f'{active_player_name}: {original_speech}'


def _state_adjudication_context(
    *,
    chain_of_thought: interactive_document.InteractiveDocument,
    director: drama_director.DramaDirector,
    active_player_name: str,
    original_speech: str | None,
    action_intent: str,
) -> None:
  """States the context the core adjudication question conditions on.

  Args:
    chain_of_thought: the adjudication document shared with the chain.
    director: the director supplying public history and GM-only
      boundaries.
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.
    action_intent: the character's own attempted action this turn.
  """
  chain_of_thought.statement('# Action adjudication (current turn only)')
  chain_of_thought.statement(f'## Current speaker\n{active_player_name}')
  chain_of_thought.statement(
      f'## Original speech (verbatim)\n{original_speech or "(none)"}'
  )
  chain_of_thought.statement(
      "## The speaker's own attempted action (adjudicate only this)\n"
      f'{action_intent}'
  )
  chain_of_thought.statement('## Committed public events (oldest first)')
  events = director.get_committed_events()
  for event in events:
    chain_of_thought.statement(f'- {event}')
  if not events:
    chain_of_thought.statement('(none)')
  chain_of_thought.statement(
      '## GM-only facts and authorization boundaries (never disclose these)'
  )
  for fact in director.immutable_facts:
    chain_of_thought.statement(f'- immutable fact: {fact}')
  for name, rule in director.character_constraints.items():
    chain_of_thought.statement(f'- {name}: {rule}')
  for constraint in director.world_constraints:
    chain_of_thought.statement(f'- {constraint}')
  players = ', '.join(director.player_names) or '(none)'
  chain_of_thought.statement(
      'Player characters whose voluntary actions only they can choose:'
      f' {players}'
  )
  chain_of_thought.statement(f'## Adjudication rules\n{_ADJUDICATION_RULES}')
