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

"""Game master component directing a story through dramaturgic beats.

The DramaDirector structures a story enactment as a sequence of beats, the
dramaturgic units of Freytag's pyramid (setup, rising action, climax,
falling action, resolution). It counts story turns, keeps a window of
resolved events, and decides after each turn whether the current beat is
complete: once a beat's minimum turn count is reached, the model is asked
the beat's completion question, and on completion the story advances by at
most one beat. A beat that reaches its maximum turn count without
completing is marked timed out.

A story turn is the exchange in which a player proposes an action (a
putative event), the game master resolves it into a validated resolution,
and the resolved event is published back to all entities. To keep results
consistent even when intermediate `update` calls happen between these
steps, the director uses a pending-commit state machine: per-turn results
are computed in `post_observe` into `_pending_commit` and only promoted to
committed state by `update`, so exactly one commit happens per story turn.

Stage 1 scope: lifecycle, story-turn bookkeeping and beat advancement.
Tension scoring and director directives are added in later stages.
"""

from collections.abc import Sequence
import dataclasses
from typing import Any, cast, override

from concordia.components.agent import action_spec_ignored
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.language_models import budget_wrapper
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY = '__drama_director__'
DEFAULT_DRAMA_DIRECTOR_PRE_ACT_LABEL = '\nCurrent beat'

_PUTATIVE_EVENT_PREFIX = f'{event_resolution_lib.PUTATIVE_EVENT_TAG} '
_EVENT_PREFIX = f'{event_resolution_lib.EVENT_TAG} '

# Inclusive bounds of the 1-10 dramatic tension scale of a beat.
_MIN_TENSION = 1
_MAX_TENSION = 10

# Number of resolved events kept in the local window that conditions the
# beat-completion question.
_MAX_RECENT_EVENTS = 20


@dataclasses.dataclass(frozen=True)
class Beat:
  """One dramaturgic beat of the story, a unit of Freytag's pyramid.

  Attributes:
    id: Unique identifier of the beat.
    description: Natural-language description of what happens in the beat.
    tension_range: Inclusive (lower, upper) bounds of the dramatic tension
      the beat should exhibit, on a 1-10 scale.
    done_question: Yes/no question deciding whether the beat is complete.
    min_turns: Minimum number of story turns before the completion question
      may be asked.
    max_turns: Maximum number of story turns before the beat times out.
  """

  id: str
  description: str
  tension_range: tuple[int, int]
  done_question: str
  min_turns: int
  max_turns: int

  def to_dict(self) -> dict[str, Any]:
    """Returns a JSON-friendly dictionary view of the beat.

    Returns:
      A dictionary with all fields, with tuples converted to lists.
    """
    return {
        'id': self.id,
        'description': self.description,
        'tension_range': list(self.tension_range),
        'done_question': self.done_question,
        'min_turns': self.min_turns,
        'max_turns': self.max_turns,
    }

  @classmethod
  def from_dict(cls, d: dict[str, Any]) -> 'Beat':
    """Rebuilds a beat from its dictionary view.

    Args:
      d: A dictionary produced by `to_dict`.

    Returns:
      The decoded beat.
    """
    lower, upper = d['tension_range']
    return cls(
        id=d['id'],
        description=d['description'],
        tension_range=(lower, upper),
        done_question=d['done_question'],
        min_turns=d['min_turns'],
        max_turns=d['max_turns'],
    )


def _validate_beats(beats: Sequence[Beat]) -> None:
  """Checks that the beat configuration describes a playable story.

  Args:
    beats: The beats to validate.

  Raises:
    ValueError: If the beats are empty, ids are not unique, a tension range
      is not within 1-10 with lower <= upper, or turn bounds do not satisfy
      0 <= min_turns <= max_turns with max_turns > 0.
  """
  if not beats:
    raise ValueError('A story must contain at least one beat.')
  ids = [beat.id for beat in beats]
  if len(set(ids)) != len(ids):
    raise ValueError(f'Beat ids must be unique, got: {ids}.')
  for beat in beats:
    lower, upper = beat.tension_range
    if not _MIN_TENSION <= lower <= upper <= _MAX_TENSION:
      raise ValueError(
          f'Beat {beat.id!r} has invalid tension range '
          f'{beat.tension_range}; expected 1 <= lower <= upper <= 10.'
      )
    if not 0 <= beat.min_turns <= beat.max_turns or beat.max_turns <= 0:
      raise ValueError(
          f'Beat {beat.id!r} has invalid turn bounds: '
          f'min_turns={beat.min_turns}, max_turns={beat.max_turns}; '
          'expected 0 <= min_turns <= max_turns and max_turns > 0.'
      )


class DramaDirector(
    action_spec_ignored.ActionSpecIgnored,
    entity_component.ComponentWithLogging,
):
  """Directs a story through its beats, one story turn at a time.

  The component participates in the game master's resolve cycle: a
  `[putative_event]` observation opens a turn context attributing the
  speech to the currently active player, the resolution pipeline supplies a
  validated resolution via `set_validated_resolution`, and the `[event]`
  observation carrying the awaited action result closes the turn. The
  per-turn pipeline in `post_observe` computes the step's results into a
  pending commit, and `update` promotes them to committed state exactly
  once per story turn. Intermediate `update` calls never discard pending
  per-turn state.
  """

  def __init__(
      self,
      model: language_model.LanguageModel,
      beats: Sequence[Beat],
      *,
      next_acting_component_key: str = (
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY
      ),
      pre_act_label: str = DEFAULT_DRAMA_DIRECTOR_PRE_ACT_LABEL,
  ):
    """Initializes the director.

    Args:
      model: The language model used for the beat-completion questions.
      beats: The beats of the story, in narrative order.
      next_acting_component_key: Key of the component that reports the
        currently active player, e.g. `NextActing`.
      pre_act_label: Prefix for this component's section of the pre-act
        context.

    Raises:
      ValueError: If the beat configuration is invalid. The model is not
        called.
    """
    super().__init__(pre_act_label=pre_act_label)
    _validate_beats(beats)
    self._model = model
    self._beats = tuple(beats)
    self._next_acting_component_key = next_acting_component_key

    # Committed state, restored by set_state.
    self._beat_index = 0
    self._turns_in_beat = 0
    self._story_turn = 0
    self._recent_events: list[str] = []
    self._beat_timeout = False
    self._budget_stopped = False
    self._last_story_turn: dict[str, Any] | None = None

    # Per-turn state machine (pending until committed by update).
    self._turn_context: dict[str, Any] | None = None
    self._validated: dict[str, str] | None = None
    self._awaiting_event = False
    self._awaited_act_result: str | None = None
    self._snapshot: dict[str, Any] | None = None
    self._pending_commit: dict[str, Any] | None = None
    self._last_output_type: entity_lib.OutputType | None = None

  @override
  def pre_act(
      self,
      action_spec: entity_lib.ActionSpec,
  ) -> str:
    self._last_output_type = action_spec.output_type
    return super().pre_act(action_spec)

  @override
  def _make_pre_act_value(self) -> str:
    beat = self._current_beat()
    if beat is None:
      return 'All story beats are complete; bring the story to its ending.'
    return f'{beat.description} (beat {beat.id})'

  @override
  def pre_observe(
      self,
      observation: str,
  ) -> str:
    if observation.startswith(_PUTATIVE_EVENT_PREFIX):
      actor = self._get_active_player()
      if actor is not None:
        beat = self._current_beat()
        content = observation.removeprefix(_PUTATIVE_EVENT_PREFIX)
        self._turn_context = {
            'actor': actor,
            # Strip the speaker prefix once to recover the raw speech.
            'original_speech': content.removeprefix(f'{actor}: '),
            'beat_id_at_action': beat.id if beat is not None else None,
        }
    elif (
        self._awaiting_event
        and self._awaited_act_result is not None
        and self._turn_context is not None
        and self._validated is not None
        and observation == f'{_EVENT_PREFIX}{self._awaited_act_result}'
    ):
      self._snapshot = {
          **self._turn_context,
          'resolved_event': self._validated['resolved_event'],
          'stage_directions': self._validated['stage_directions'],
      }
      self._awaiting_event = False
    return ''

  @override
  def post_act(
      self,
      action_attempt: str,
  ) -> str:
    if (
        self._last_output_type == entity_lib.OutputType.RESOLVE
        and self._validated is not None
    ):
      self._awaiting_event = True
      self._awaited_act_result = action_attempt
    return ''

  @override
  def post_observe(self) -> str:
    snapshot = self._snapshot
    if snapshot is None:
      return ''

    # Compute this step's results locally. Committed state is only
    # overwritten when update promotes the pending commit.
    beat = self._current_beat()
    beat_index = self._beat_index
    turns_in_beat = self._turns_in_beat + 1
    beat_timeout = self._beat_timeout
    budget_stopped = self._budget_stopped
    recent_events = [
        *self._recent_events,
        snapshot['resolved_event'],
    ][-_MAX_RECENT_EVENTS:]

    completed = False
    # The completion question runs only after the beat's minimum turn
    # count, and never again once the budget is spent.
    if beat is not None and not budget_stopped:
      if turns_in_beat >= beat.min_turns:
        try:
          completed = self._query_beat_completion(beat, recent_events)
        except budget_wrapper.BudgetExhaustedError:
          # Budget path B: record the diagnostic, make no further model
          # calls, do not advance the beat on an unreturned completion
          # decision, but still check the timeout below with the turns
          # already consumed. Results computed so far are kept and this
          # step still commits once via update.
          budget_stopped = True

    if completed:
      # Advance by at most one beat and reset the per-beat turn count.
      beat_index = min(beat_index + 1, len(self._beats))
      turns_in_beat = 0
      beat_timeout = False
    elif beat is not None and turns_in_beat >= beat.max_turns:
      beat_timeout = True

    story_turn = {
        'turn_id': self._story_turn + 1,
        'actor': snapshot['actor'],
        'speech': snapshot['original_speech'],
        'beat_id_at_action': snapshot['beat_id_at_action'],
        'beat_id_after_event': (
            self._beats[beat_index].id
            if beat_index < len(self._beats)
            else None
        ),
        'resolved_event': snapshot['resolved_event'],
        'stage_directions': snapshot['stage_directions'],
        'tension': None,  # Placeholder; scored in a later stage.
        'budget_stopped': budget_stopped,
    }
    self._pending_commit = {
        'beat_index': beat_index,
        'turns_in_beat': turns_in_beat,
        'recent_events': recent_events,
        'beat_timeout': beat_timeout,
        'budget_stopped': budget_stopped,
        'story_turn': story_turn,
    }
    return ''

  @override
  def update(self) -> None:
    commit = self._pending_commit
    if commit is not None:
      story_turn = commit['story_turn']
      self._beat_index = commit['beat_index']
      self._turns_in_beat = commit['turns_in_beat']
      self._story_turn = story_turn['turn_id']
      self._recent_events = commit['recent_events']
      self._beat_timeout = commit['beat_timeout']
      self._budget_stopped = commit['budget_stopped']
      self._last_story_turn = story_turn
      self._pending_commit = None
      # Clear the per-turn state machine; the turn is fully committed.
      self._turn_context = None
      self._validated = None
      self._snapshot = None
      self._awaited_act_result = None
      self._logging_channel({'StoryTurn': story_turn})
    # Without a pending commit, keep the putative context, validated
    # resolution and awaiting flags across intermediate updates. Always
    # clear the parent class' pre-act cache.
    super().update()

  def set_validated_resolution(
      self,
      resolved_event: str,
      stage_directions: str,
  ) -> None:
    """Supplies the validated resolution of the current turn.

    Args:
      resolved_event: The resolved event to publish for the current turn.
      stage_directions: Stage directions accompanying the event.
    """
    self._validated = {
        'resolved_event': resolved_event,
        'stage_directions': stage_directions,
    }

  def get_last_story_turn(self) -> dict[str, Any] | None:
    """Returns the most recently committed story turn, if any."""
    return self._last_story_turn

  def get_current_original_speech(self) -> str | None:
    """Returns the original speech of the turn in progress, if any."""
    if self._turn_context is None:
      return None
    return self._turn_context['original_speech']

  def is_story_stopped(self) -> bool:
    """Returns whether the story should not continue any further."""
    return self.is_beats_done() or self._beat_timeout or self._budget_stopped

  def is_beats_done(self) -> bool:
    """Returns whether all beats of the story have been completed."""
    return self._beat_index >= len(self._beats)

  def beat_timeout(self) -> bool:
    """Returns whether the current beat timed out without completing."""
    return self._beat_timeout

  @override
  def get_state(self) -> entity_component.ComponentState:
    """Returns the state of the component."""
    return {
        'beats': [beat.to_dict() for beat in self._beats],
        'beat_index': self._beat_index,
        'turns_in_beat': self._turns_in_beat,
        'story_turn': self._story_turn,
        'recent_events': list(self._recent_events),
        'beat_timeout': self._beat_timeout,
        'budget_stopped': self._budget_stopped,
        'last_story_turn': self._last_story_turn,
    }

  @override
  def set_state(self, state: entity_component.ComponentState) -> None:
    """Sets the state of the component.

    Args:
      state: A state returned by `get_state`.
    """
    beats = cast('Sequence[dict[str, Any]]', state['beats'])
    self._beats = tuple(Beat.from_dict(beat) for beat in beats)
    self._beat_index = state['beat_index']  # pyrefly: ignore[bad-assignment]
    self._turns_in_beat = state['turns_in_beat']  # pyrefly: ignore[bad-assignment]
    self._story_turn = state['story_turn']  # pyrefly: ignore[bad-assignment]
    self._recent_events = list(  # pyrefly: ignore[bad-assignment]
        state['recent_events']  # pyrefly: ignore[bad-argument-type]
    )
    self._beat_timeout = state['beat_timeout']  # pyrefly: ignore[bad-assignment]
    self._budget_stopped = state['budget_stopped']  # pyrefly: ignore[bad-assignment]
    self._last_story_turn = state['last_story_turn']  # pyrefly: ignore[bad-assignment]

  def _current_beat(self) -> Beat | None:
    """Returns the beat currently in progress, or None if all are done."""
    if self._beat_index < len(self._beats):
      return self._beats[self._beat_index]
    return None

  def _get_active_player(self) -> str | None:
    """Returns the player currently taking their turn, if known."""
    component = self.get_entity().get_component(self._next_acting_component_key)
    get_player = getattr(component, 'get_currently_active_player', None)
    if get_player is None:
      return None
    return get_player()

  def _query_beat_completion(
      self,
      beat: Beat,
      recent_events: Sequence[str],
  ) -> bool:
    """Asks the model whether the current beat is complete.

    Args:
      beat: The beat to evaluate.
      recent_events: Window of resolved events conditioning the question.

    Returns:
      Whether the model judged the beat complete. An invalid model choice
      is treated as "not completed".

    Raises:
      budget_wrapper.BudgetExhaustedError: If the attempt budget is spent;
        propagated to the caller for budget path B handling. Any other
        model error propagates uncaught.
    """
    prompt = interactive_document.InteractiveDocument(self._model)
    prompt.statement('Recent events of the story, in order:\n')
    for event in recent_events:
      prompt.statement(f'- {event}')
    prompt.statement(f'Current beat "{beat.id}": {beat.description}\n')
    try:
      return prompt.yes_no_question(question=beat.done_question)
    except language_model.InvalidResponseError:
      # The model failed to pick one of the offered choices; treat the
      # beat as not completed rather than guessing.
      return False
