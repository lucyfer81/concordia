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

"""Final dual-track output step for story-enactment event resolution.

The final step of the event-resolution chain of thought produces a
dual-track output: the canonical resolved event, which must preserve the
active player's authored speech verbatim, and a separate stage-directions
track recording environment changes, NPC behavior, and outcomes. Keeping
the two tracks separate makes the boundary between player agency (what
the character said) and narrator authority (everything else) explicit,
following the Concordia design pattern's rule that the game master must
never decide voluntary actions for player characters. The validated pair
is stored in the DramaDirector via `set_validated_resolution`, and only
the resolved event is returned to the caller, matching the
EventResolution thought-step convention
`(document, premise, active_player_name) -> str`.
"""

from collections.abc import Callable, Sequence
import json

from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document

# The exact key set required of the model's JSON output.
_DUAL_TRACK_KEYS = frozenset({'resolved_event', 'stage_directions'})


class StoryOutputError(Exception):
  """Raised when the model's final output is not a valid dual-track JSON.

  Only JSON decoding and content-validation failures raise this error.
  Language-model API errors and budget exhaustion propagate uncaught so
  that callers can distinguish malformed output from simulation faults.
  """


def make_final_output_step(
    director: drama_director.DramaDirector,
    *,
    immutable_facts: Sequence[str] = (),
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the final dual-track output step of an event-resolution chain.

  Args:
    director: the drama director holding the current turn's original
      speech, which receives the validated dual-track resolution.
    immutable_facts: facts the resolution must not contradict, listed in
      the prompt's constraints section.

  Returns:
    A thought step with the EventResolution step signature
    `(document, premise, active_player_name) -> str` that asks the model
    for a dual-track JSON object, validates it, stores it in the
    director, and returns only the resolved event string.
  """

  def step(
      chain: interactive_document.InteractiveDocument,
      candidate_event: str,
      active_player_name: str,
  ) -> str:
    """Asks for and validates the dual-track resolution of the event."""
    original_speech = director.get_current_original_speech()
    response = chain.open_question(
        question=_build_prompt(
            candidate_event=candidate_event,
            active_player_name=active_player_name,
            original_speech=original_speech,
            immutable_facts=immutable_facts,
        ),
        max_tokens=2000,
        terminators=(),
    )
    resolved_event, stage_directions = _parse_dual_track(
        response,
        active_player_name=active_player_name,
        original_speech=original_speech,
    )
    director.set_validated_resolution(resolved_event, stage_directions)
    return resolved_event

  return step


def _build_prompt(
    candidate_event: str,
    active_player_name: str,
    original_speech: str | None,
    immutable_facts: Sequence[str],
) -> str:
  """Fills the P5 final-output prompt template (stage-1 plan, appendix B).

  Args:
    candidate_event: the event to resolve, from the previous chain step.
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.
    immutable_facts: facts the resolution must not contradict.

  Returns:
    The prompt asking the model for the dual-track JSON object.
  """
  speech = original_speech if original_speech is not None else '(none)'
  facts = '; '.join(immutable_facts) if immutable_facts else '(none)'
  return (
      '# Candidate event\n'
      f'{candidate_event}\n'
      '\n'
      '## Current speaker\n'
      f'{active_player_name}\n'
      '\n'
      '## Original speech (verbatim)\n'
      f'{speech}\n'
      '\n'
      '## Constraints\n'
      'Preserve the original speech and its speaker. Do not create '
      'voluntary\n'
      'actions for any player character. Do not contradict immutable '
      'facts.\n'
      f'Immutable facts: {facts}\n'
      '\n'
      '## Output\n'
      'Output a JSON object with exactly two string fields:\n'
      '- "resolved_event": the full canonical event, preserving the '
      "speaker's\n"
      '  exact dialogue verbatim, plus what actually happened.\n'
      '- "stage_directions": visible environment changes, NPC behavior, '
      'and\n'
      '  outcomes only. Do NOT repeat the dialogue. Use an empty string '
      'if\n'
      '  there is nothing beyond speech.\n'
      'Output only the JSON object, no other text.\n'
  )


def _parse_dual_track(
    response: str,
    active_player_name: str,
    original_speech: str | None,
) -> tuple[str, str]:
  """Parses and validates the model's dual-track JSON response.

  Args:
    response: the raw model output.
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.

  Returns:
    A (resolved_event, stage_directions) pair of strings.

  Raises:
    StoryOutputError: if the response is not valid JSON, is not an object
      whose key set is exactly {'resolved_event', 'stage_directions'},
      has non-string field values, or, when the turn had speech, drops
      the speaker or does not contain the original speech verbatim.
  """
  try:
    payload = json.loads(response)
  except json.JSONDecodeError as e:
    raise StoryOutputError(
        f'Model output is not valid JSON: {response!r}'
    ) from e
  if not isinstance(payload, dict) or set(payload) != _DUAL_TRACK_KEYS:
    raise StoryOutputError(
        'Model output must be a JSON object with exactly the keys '
        f'{sorted(_DUAL_TRACK_KEYS)}, got: {response!r}'
    )
  resolved_event = payload['resolved_event']
  stage_directions = payload['stage_directions']
  if not isinstance(resolved_event, str) or not isinstance(
      stage_directions, str
  ):
    raise StoryOutputError(
        'Both fields of the dual-track output must be strings, got: '
        f'{response!r}'
    )
  if original_speech is not None:
    # NOTE: these substring checks are cheap structural guards against
    # dialogue rewrites and speaker loss. They cannot prove that the
    # resolution respected character agency in the deeper semantic sense;
    # that is enforced by the prompt constraints and verified by the
    # scenario acceptance tests (plan tasks 6 and 10).
    if active_player_name not in resolved_event:
      raise StoryOutputError(
          f'The resolved event lost the speaker {active_player_name!r}: '
          f'{resolved_event!r}'
      )
    if original_speech not in resolved_event:
      raise StoryOutputError(
          'The resolved event rewrote the original speech '
          f'{original_speech!r}: {resolved_event!r}'
      )
  return resolved_event, stage_directions
