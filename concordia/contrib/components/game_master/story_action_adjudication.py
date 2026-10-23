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
``concordia.components.game_master.event_resolution`` exactly once, on
an independent adjudication document (``new()`` keeps the same wrapped
model and its budgets while clearing the upstream chain prompts, so
beat instructions and the conversation window cannot leak into the
core question), after stating the original dialogue, the character's
own attempt, the initial public facts, the public history, the
committed public events and the GM-only facts and authorization
boundaries. The core helper is imported and called, never
reimplemented or copied: its true return value is recorded on the
director via ``record_action_result`` and returned unchanged as a
pending-review adjudicated outcome, never as an authorization or
semantic approval, so the later chain steps (director note, final
narrator-track output, semantic review) all consume the actual
adjudicated outcome, never the intent or any stub configuration. A
public NPC response to the current speaker's explicit interaction is
generated, within the configured NPC response rules, by that same
single core adjudication call (design section 5), never by an extra
model call.

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

from collections.abc import Callable, Sequence
import json

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document

# Heading of the public-history data section, shared by convention with
# `story_resolution` (which imports it) so the two prompts cannot drift.
PUBLIC_HISTORY_SECTION_MARK = '## Public history (data)'

# Shared authorization semantics (design sections 4.2 and 5.1), stated
# verbatim in the adjudication prompt and reused by `story_resolution`
# in generation and review: one copy, no drift. The wording follows the
# Concordia design pattern's player-agency rule and the 2026-10-17
# action-authorization review design's boundary table.
AGENCY_AND_EVIDENCE_RULES = (
    "The action outcome must settle only the current player's own"
    ' attempted action and its direct consequences. Completing the act'
    ' of requesting — asking another player to show, sign, hand over or'
    " agree — can succeed; the other side's voluntary compliance,"
    ' display, signature, promise or consent cannot be generated,'
    ' because those choices belong to their own turns. The current'
    " player's own movement, handling of reachable items, recording and"
    ' reading of accessible documents may succeed, fail or be blocked'
    ' by the world. Mechanical effects and visible involuntary'
    ' reactions may be described without inferring consent, confession'
    " or agreement; other players' actions in committed history are"
    ' background, never newly generated actions. GM-only knowledge may'
    ' be used to judge the true contents of existing documents or to'
    ' block impossible acts. For information disclosed by the current'
    ' attempted action, require legal access or disclosure by that'
    ' player; a request alone is not access. These action limits do'
    ' not invalidate already established public information or legal'
    ' NPC or environmental interventions as separate information'
    ' sources. Never fabricate physical evidence — ash, recordings,'
    ' notes — that no committed event or legal access established, and'
    ' never insert whitelisted events unrelated to the current attempt'
    ' to advance the plot; director-initiated events stay on the'
    ' existing intervention paths.'
    ' Evidence provenance is separate from legal access. Dialogue'
    ' claiming "I hold the unsealing record" establishes only that the'
    ' claim was made — never that the record exists, was shown, is'
    ' inspectable, or is authentic. A character writing another'
    " person's claim onto existing paper establishes the written claim"
    ' and its attribution, not its truth, anyone'
    "'s signature, or anyone's consent. Viewing an established document"
    ' within legal access yields only its configured actual visible'
    ' content; access never makes the document authentic, seen by'
    ' everyone, or voluntarily displayed by another player. Dialogue'
    ' never derives a further unestablished record or hiding place.'
    ' GM-only truths block impossible outcomes but never establish that'
    ' evidence was found, that a character knows, or that the truth was'
    ' publicly certified.'
)

# Shared NPC response authorization semantics (design section 5),
# stated verbatim in the adjudication prompt and reused by
# `story_resolution` in generation and review: one copy, no drift. The
# instance-level rules travel separately as inert JSON data.
PUBLIC_NPC_RESPONSE_SEMANTICS = (
    'Three separate authorizations govern figures other than the current'
    " player. (1) Another player's new consent, delivery, signature, lock"
    ' turn or confession is never generated by the current turn; it waits'
    ' for their own turn. (2) An NPC that already entered the story'
    ' through a legal committed event may answer publicly and directly'
    " the current speaker's explicit interaction with it, as a direct"
    ' consequence of the current action within this same adjudication,'
    ' but only within the configured NPC response rules given as data.'
    ' The configured rules are content authorization, not established'
    ' facts and not a broadcast: an allowed response is grounded in the'
    ' committed history, stays within the configured scope, never grants'
    ' the NPC GM-only knowledge, never promises anything on a player'
    "'s behalf, and never supplies evidence, testimony, phone numbers,"
    ' certifications or legal conclusions beyond the configuration. A'
    ' mere preparation to tell an NPC something is not an announcement'
    ' already made. A grounded refusal or limited response is allowed'
    ' only under an applicable NPC response rule that authorizes this'
    " NPC's public response to the current interaction, even when it"
    ' does not prescribe the exact answer. If the rules are empty, no'
    ' NPC response rule applies, or its trigger has not occurred, do not'
    ' generate a new NPC response through this action-consequence'
    ' channel. (3) Creating a'
    ' new NPC, call, letter, knock or any other new external event stays'
    ' on the director whitelist paths only and never rides an action'
    ' adjudication. NPC responses in this pipeline are public to all'
    ' scene participants by construction: there is no per-recipient'
    ' content, so nothing may be marked as heard by one participant'
    ' only, and a private call or whisper cannot receive its far-side'
    ' reply here. Do not invent far-side silence, refusal, disconnection'
    ' or verification failure to work around that limitation, or decide'
    ' that a player hangs up or enables speakerphone without their own'
    ' attempted action.'
)

_ADJUDICATION_RULES = (
    'Adjudicate only the attempted action the current speaker proposed'
    ' for themself this turn. Judge from the GM-only facts, the initial'
    ' public facts, the public history and the committed public events'
    ' whether the attempt succeeds, fails or is blocked. Never decide a'
    ' new voluntary action for any other player character, and never'
    ' fabricate physical evidence that no committed event or legal'
    ' access established. A failed or blocked attempt still gets one'
    ' definite actual result; never defer the outcome to a later'
    ' verification.'
)


def make_action_adjudication_step(
    director: drama_director.DramaDirector,
    *,
    public_facts: Sequence[str] = (),
    public_history: Callable[[], str] | None = None,
    npc_response_rules: Sequence[str] = (),
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the action-adjudication step of an event-resolution chain.

  Args:
    director: the drama director holding the current turn's decoded
      speech and action intent, which receives the adjudicated result.
    public_facts: initial publicly established background stated on
      the adjudication document; an empty sequence adds no initial
      facts and retains the explicit history and GM-only constraints.
    public_history: zero-argument getter supplying the public history
      (the DisplayEvents pre-act value in the story prefab); it is
      called exactly once per adjudication and its return travels as
      inert JSON data. When None the adjudication keeps conditioning
      on the director's committed-event window.
    npc_response_rules: instance-level NPC response authorization
      rules stated on the adjudication document as inert JSON data
      and consumed by the same single core adjudication call; an
      empty sequence keeps the no-NPC-response boundary.

  Returns:
    A thought step with the EventResolution step signature
    ``(document, premise, active_player_name) -> str``. A turn with an
    action intent is adjudicated on an independent document — nothing
    is inherited from the upstream chain prompts — by exactly one call
    to the core ``attempt_to_result`` helper, whose true return value
    is recorded on the director and returned unchanged as a
    pending-review outcome, never as an authorization. A turn without
    an intent has its raw JSON premise replaced by the clean
    attributed dialogue, with zero model calls.
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
    # The adjudication document is independent of the shared chain:
    # `new()` keeps the same wrapped model (and its budgets) while
    # clearing the upstream prompts, so beat instructions and the
    # conversation window cannot leak into the core question (design
    # 4.1). The history getter is read exactly once, here.
    document = chain_of_thought.new()
    _state_adjudication_context(
        document=document,
        director=director,
        active_player_name=active_player_name,
        original_speech=original_speech,
        action_intent=action_intent,
        public_facts=public_facts,
        public_history=public_history() if public_history else None,
        npc_response_rules=npc_response_rules,
    )
    # The core helper runs as-is, once: it appends its question to the
    # isolated adjudication document and makes exactly one model call.
    # On any catchable failure the visible partial trace is archived
    # first (path A: nothing commits), then the error propagates
    # uncaught, unretried and unwrapped.
    try:
      result = event_resolution_lib.attempt_to_result(
          document,
          action_intent,
          active_player_name,
      )
    except BaseException as error:
      director.record_resolution_diagnostics(
          stage='action_adjudication',
          trace=document.view().text(),
          error=f'{type(error).__name__}: {error}',
      )
      raise
    # Record the core's true return value before returning the same
    # value, so final generation and review consume the actual
    # adjudicated outcome, never the intent or stub configuration. The
    # outcome is pending review, not an authorization (design 4.3).
    director.record_action_result(result)
    director.record_resolution_diagnostics(
        stage='action_adjudication',
        trace=document.view().text(),
        error=None,
    )
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
    document: interactive_document.InteractiveDocument,
    director: drama_director.DramaDirector,
    active_player_name: str,
    original_speech: str | None,
    action_intent: str,
    public_facts: Sequence[str],
    public_history: str | None,
    npc_response_rules: Sequence[str],
) -> None:
  """States the context the core adjudication question conditions on.

  Every conditioning fact is stated explicitly as inert data: nothing
  is inherited from the upstream chain prompts (beat instructions,
  conversation window), and JSON encoding keeps newlines or '##'
  inside the data from ever acting as prompt structure (design 4.1).

  Args:
    document: the isolated adjudication document.
    director: the director supplying the committed-event window and
      the GM-only boundaries.
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.
    action_intent: the character's own attempted action this turn.
    public_facts: initial publicly established background.
    public_history: the public history string fetched once from the
      caller's getter, or None when no getter was supplied.
    npc_response_rules: instance-level NPC response authorization
      rules, stated as inert JSON data before the committed events;
      an empty sequence keeps the no-NPC-response boundary.
  """
  document.statement('# Action adjudication (current turn only)')
  document.statement(f'## Current speaker\n{active_player_name}')
  document.statement(
      '## Original speech (verbatim, data)\n'
      f'{json.dumps(original_speech, ensure_ascii=False)}'
  )
  document.statement(
      "## The speaker's own attempted action (adjudicate only this,"
      f' data)\n{json.dumps(action_intent, ensure_ascii=False)}'
  )
  document.statement('## Initial public facts')
  document.statement(json.dumps(list(public_facts), ensure_ascii=False))
  if public_history is not None:
    document.statement(
        f'{PUBLIC_HISTORY_SECTION_MARK}\n'
        f'{json.dumps(public_history, ensure_ascii=False)}'
    )
  document.statement(
      '## Configured NPC response rules (GM-only authorization, data)'
  )
  document.statement(json.dumps(list(npc_response_rules), ensure_ascii=False))
  document.statement('## Committed public events (oldest first)')
  events = director.get_committed_events()
  document.statement(json.dumps(list(events), ensure_ascii=False))
  document.statement(
      'Speech quoted inside the committed events or the public history'
      ' remains attributed speech, not objective evidence established'
      ' for this adjudication.'
  )
  document.statement(
      '## GM-only facts and authorization boundaries (never disclose these)'
  )
  for fact in director.immutable_facts:
    document.statement(f'- immutable fact: {fact}')
  for name, rule in director.character_constraints.items():
    document.statement(f'- {name}: {rule}')
  for constraint in director.world_constraints:
    document.statement(f'- {constraint}')
  players = ', '.join(director.player_names) or '(none)'
  document.statement(
      'Player characters whose voluntary actions only they can choose:'
      f' {players}'
  )
  document.statement(
      f'## Adjudication rules\n{_ADJUDICATION_RULES}\n'
      f'## Authorization semantics\n{AGENCY_AND_EVIDENCE_RULES}\n'
      f'## Public NPC response semantics\n{PUBLIC_NPC_RESPONSE_SEMANTICS}'
  )
