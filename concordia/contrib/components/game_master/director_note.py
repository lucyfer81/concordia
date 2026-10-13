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

"""Director-note thought step fusing pending directives into events.

When the drama director holds a pending directive (generated because
the story's tension deviated from the current beat's target range), the
event-resolution chain weaves the directive into the candidate event
before the dual-track final output is produced. The step follows the
two-stage pattern of ``maybe_inject_narrative_push`` in
``concordia.components.game_master.event_resolution`` (plan appendix B,
prompt P4): first the model proposes plausible in-story complications
fulfilling the directive, then a second question merges the candidate
event and the chosen complication into one event.

Both questions carry the same constraints: only NPC or environmental
means from the configured whitelist may be introduced, immutable facts
must hold, and no player character may be assumed to act voluntarily,
protecting player agency (the Concordia design pattern's rule that the
game master never decides voluntary actions for player characters).

Without a pending directive the step is the identity and makes zero
language-model calls. The first valid complication candidate is used,
never an unsaved random choice, so results stay reproducible from the
checkpointed state. When the model proposes no valid candidate, the
directive is recorded as failed and the candidate event passes through
unchanged (spec section 11.1: degrade to no directive, never interrupt
the simulation). API and budget errors propagate uncaught: fusion runs
during the game master's RESOLVE act, before the resolution commits,
so budget exhaustion takes spec section 11.3's path A.
"""

from collections.abc import Callable, Sequence

from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document


def make_director_note_step(
    director: drama_director.DramaDirector,
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the director-note step of an event-resolution chain.

  Args:
    director: the drama director holding the directive state and the
      fusion constraints (allowed environment events, immutable facts,
      player names).

  Returns:
    A thought step with the EventResolution step signature
    ``(document, premise, active_player_name) -> str`` that returns the
    candidate event unchanged when no directive is pending, and the
    fused event otherwise.
  """

  def step(
      chain: interactive_document.InteractiveDocument,
      candidate_event: str,
      active_player_name: str,
  ) -> str:
    """Fuses the pending directive into the candidate event, if any."""
    directive = director.get_pending_directive()
    if directive is None:
      # No pending directive: identity, with zero model calls.
      return candidate_event
    director.mark_directive_integrating()
    complications = chain.open_question(
        question=_complication_question(directive['content'], director),
        max_tokens=1500,
        terminators=(),
    )
    candidates = [
        line.strip() for line in complications.split('\n') if line.strip()
    ]
    if not candidates:
      # Explicit generation-failure handling (spec 11.1): record the
      # failed directive, degrade to no directive, keep resolving.
      director.mark_directive_failed(
          'the model proposed no valid complication candidate'
      )
      return candidate_event
    # The first valid candidate is used; a random pick would introduce
    # unsaved state that a checkpoint resume could not reproduce.
    chosen = candidates[0]
    return chain.open_question(
        question=_merge_question(
            candidate_event=candidate_event,
            chosen_complication=chosen,
            active_player_name=active_player_name,
            director=director,
        ),
        max_tokens=1500,
        terminators=(),
    )

  return step


def _join(values: Sequence[str]) -> str:
  """Returns a readable list of the constraint values."""
  return '; '.join(values) if values else '(none)'


def _complication_question(
    directive_content: str,
    director: drama_director.DramaDirector,
) -> str:
  """Fills the first half of the P4 fusion prompt template.

  Args:
    directive_content: the pending directive's instruction.
    director: the director supplying the fusion constraints.

  Returns:
    The question asking for five plausible complications.
  """
  return (
      'Suggest five plausible in-story complications that fulfill this '
      f'director instruction: "{directive_content}". One per line, no '
      'numbering.\n'
      'Only NPC or environmental events; never assume any player '
      'character voluntarily does anything.\n'
      f'Allowed means: {_join(director.allowed_environment_events)}\n'
      f'Immutable facts: {_join(director.immutable_facts)}\n'
      'Player characters whose voluntary actions you cannot decide: '
      f'{", ".join(director.player_names) if director.player_names else "(none)"}'
  )


def _merge_question(
    candidate_event: str,
    chosen_complication: str,
    active_player_name: str,
    director: drama_director.DramaDirector,
) -> str:
  """Fills the second half of the P4 fusion prompt template.

  Args:
    candidate_event: the event to resolve, from the previous chain step.
    chosen_complication: the complication selected for weaving in.
    active_player_name: name of the player whose turn it currently is.
    director: the director supplying the fusion constraints.

  Returns:
    The question asking for the single merged event.
  """
  return (
      f'# Candidate event\n{candidate_event}\n\n## Chosen complication to weave'
      f' in\n{chosen_complication}\n\n## Current'
      f' speaker\n{active_player_name}\n\n## Constraints\nMerge the candidate'
      " event and the complication into one event.\nPreserve the player's"
      ' dialogue verbatim. Do not add any voluntary action\nfor any player'
      ' character. Do not contradict immutable facts.\nImmutable facts:'
      f' {_join(director.immutable_facts)}\nAllowed means for new events:'
      f' {_join(director.allowed_environment_events)}\nPlayer characters whose'
      ' voluntary actions you cannot decide:'
      f' {", ".join(director.player_names) if director.player_names else "(none)"}\n\n##'
      ' Output\nWrite the single merged event and nothing else.'
  )
