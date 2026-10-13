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

Stage 1 covered the lifecycle, story-turn bookkeeping and beat
advancement. Stage 2 adds tension evaluation (spec section 4.4, step 3):
every `eval_interval` story turns the model rates the dramatic tension of
the most recent `scoring_window` resolved events on an anchored 1-10
scale, the score is compared in program code against the target range of
the beat that was current when the player acted, and the deviation is
recorded. Stage 2 also adds director directives (spec sections 4.4 step 4
and 4.5): when feedback is enabled and a valid score deviates while no
beat transitioned and the story keeps running outside any cooldown, the
model is asked for a one-sentence instruction moving the tension back
toward the range, constrained to NPC/environmental means only. The
directive is stored as pending, bound to its beat, executed at most once
by the `director_note` resolution step, and only marked consumed after
the dual-track final output validates successfully.
"""

from collections.abc import Sequence
import dataclasses
import re
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

# Number of resolved events the beat-completion question needs in its
# window. The stored event window keeps the larger of this need and
# `scoring_window`, so it serves both pipeline steps 2 and 3.
_COMPLETION_QUERY_EVENTS = 20

# A scoring answer must be exactly one integer in the scale bounds.
_TENSION_ANSWER = re.compile(r'\s*(\d{1,2})\s*')

# Fixed wording of the tension-scale anchors and the output instruction
# of the scoring prompt (plan appendix B, prompt P2).
_TENSION_QUESTION = (
    'Rate the current dramatic tension of the story on a scale from 1 to 10.'
)
_TENSION_ANCHORS = (
    '1 = calm routine, no conflict or stakes in motion.',
    '5 = clear conflict or stakes present, but pressure is steady.',
    '10 = open confrontation or crisis, the outcome hangs on this moment.',
)
_TENSION_OUTPUT_INSTRUCTION = (
    'Respond with a single integer from 1 to 10 and nothing else.'
)

# Status values of the director directive lifecycle (spec section 4.5).
_DIRECTIVE_PENDING = 'pending'
_DIRECTIVE_INTEGRATING = 'integrating'
_DIRECTIVE_CONSUMED = 'consumed'
_DIRECTIVE_INVALIDATED = 'invalidated'
_DIRECTIVE_FAILED = 'failed'
_ACTIVE_DIRECTIVE_STATUSES = (_DIRECTIVE_PENDING, _DIRECTIVE_INTEGRATING)

# Default cooldown, in story turns, after a directive executes before
# the next one may be generated (plan appendix C).
_DEFAULT_COOLDOWN_TURNS = 3


def _parse_tension_score(answer: str) -> int | None:
  """Parses a model answer into a tension score on the 1-10 scale.

  Args:
    answer: The raw model response to the scoring question.

  Returns:
    The score, or None if the answer is not a single integer within the
    scale bounds.
  """
  match = _TENSION_ANSWER.fullmatch(answer)
  if match is None:
    return None
  score = int(match.group(1))
  if _MIN_TENSION <= score <= _MAX_TENSION:
    return score
  return None


def _is_deviation(
    score: int,
    tension_range: tuple[int, int],
    tolerance: int,
) -> bool:
  """Decides whether a score deviates from a beat's target range.

  Only scores strictly below `lower - tolerance` or strictly above
  `upper + tolerance` deviate; scores equal to a threshold do not
  (spec section 4.4, step 3).

  Args:
    score: The observed tension score.
    tension_range: Inclusive (lower, upper) target bounds of the beat.
    tolerance: Allowed slack on both sides of the range.

  Returns:
    Whether the score deviates from the target range.
  """
  lower, upper = tension_range
  return score < lower - tolerance or score > upper + tolerance


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
      eval_interval: int = 2,
      scoring_window: int = 8,
      tolerance: int = 1,
      cooldown_turns: int = _DEFAULT_COOLDOWN_TURNS,
      feedback_enabled: bool = False,
      allowed_environment_events: Sequence[str] = (),
      immutable_facts: Sequence[str] = (),
      player_names: Sequence[str] = (),
  ):
    """Initializes the director.

    Args:
      model: The language model used for the beat-completion questions,
        the tension-scoring questions, the directive-generation
        questions and (via the `director_note` step) directive fusion.
      beats: The beats of the story, in narrative order.
      next_acting_component_key: Key of the component that reports the
        currently active player, e.g. `NextActing`.
      pre_act_label: Prefix for this component's section of the pre-act
        context.
      eval_interval: The story is scored every `eval_interval`-th story
        turn; other turns keep `tension` unset. Zero disables scoring.
      scoring_window: Number of most recent resolved events conditioning
        the scoring question.
      tolerance: Slack around a beat's target tension range; scores
        within `lower - tolerance` and `upper + tolerance` (inclusive)
        do not deviate.
      cooldown_turns: Number of story turns after a directive executes
        during which no new directive may be generated.
      feedback_enabled: Whether tension deviations generate director
        directives; the tension-observation mode records scores only.
      allowed_environment_events: Whitelist of NPC/environmental means
        the director's instructions may draw on.
      immutable_facts: Facts the director's instructions must never
        contradict.
      player_names: Names of the player characters, whose voluntary
        actions no directive may decide.

    Raises:
      ValueError: If the beat configuration is invalid or the scoring
        or feedback configuration is out of range. The model is not
        called.
    """
    super().__init__(pre_act_label=pre_act_label)
    _validate_beats(beats)
    if eval_interval < 0:
      raise ValueError(f'eval_interval must be >= 0, got {eval_interval}.')
    if scoring_window < 1:
      raise ValueError(f'scoring_window must be >= 1, got {scoring_window}.')
    if tolerance < 0:
      raise ValueError(f'tolerance must be >= 0, got {tolerance}.')
    if cooldown_turns < 0:
      raise ValueError(f'cooldown_turns must be >= 0, got {cooldown_turns}.')
    self._model = model
    self._beats = tuple(beats)
    self._next_acting_component_key = next_acting_component_key
    self._eval_interval = eval_interval
    self._scoring_window = scoring_window
    self._tolerance = tolerance
    self._cooldown_turns = cooldown_turns
    self._feedback_enabled = feedback_enabled
    self._allowed_environment_events = tuple(allowed_environment_events)
    self._immutable_facts = tuple(immutable_facts)
    self._player_names = tuple(player_names)
    # One stored window serves both the completion question (up to
    # `_COMPLETION_QUERY_EVENTS` events) and the scoring question (the
    # last `scoring_window` events), so it keeps the larger need.
    self._window_size = max(scoring_window, _COMPLETION_QUERY_EVENTS)

    # Committed state, restored by set_state.
    self._beat_index = 0
    self._turns_in_beat = 0
    self._story_turn = 0
    self._recent_events: list[str] = []
    self._beat_timeout = False
    self._budget_stopped = False
    self._last_story_turn: dict[str, Any] | None = None
    self._tension_eval_count = 0
    self._tension_history: list[dict[str, Any]] = []
    self._directive: dict[str, Any] | None = None
    self._last_directive_executed_turn: int | None = None

    # Per-turn state machine (pending until committed by update).
    self._turn_context: dict[str, Any] | None = None
    self._validated: dict[str, Any] | None = None
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
    turn_id = self._story_turn + 1
    recent_events = [
        *self._recent_events,
        snapshot['resolved_event'],
    ][-self._window_size :]

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

    # Pipeline step 3: tension evaluation (spec section 4.4). Scheduled
    # every eval_interval-th story turn and skipped entirely once the
    # budget is spent. The score is compared in program code against the
    # target range of the beat that was current when the player acted;
    # the scoring prompt never sees the target range. Deviations are
    # only recorded here; acting on them is a later pipeline step.
    tension = None
    deviated = False
    tension_eval_count = self._tension_eval_count
    tension_entry = None
    if (
        not budget_stopped
        and self._eval_interval > 0
        and turn_id % self._eval_interval == 0
    ):
      tension_eval_count += 1
      try:
        tension = self._score_tension(recent_events)
      except budget_wrapper.BudgetExhaustedError:
        # Budget path B at the scoring position (spec 11.3-2): the
        # score stays unset, no further model calls happen this step,
        # and the step still commits once via update.
        budget_stopped = True
      if tension is not None:
        target = self._target_range_of(snapshot['beat_id_at_action'])
        deviated = target is not None and _is_deviation(
            tension, target, self._tolerance
        )
      tension_entry = {
          'turn_id': turn_id,
          'beat_id': snapshot['beat_id_at_action'],
          'tension': tension,
          'deviated': deviated,
      }

    # Pipeline step 4: director directives (spec sections 4.4 step 4
    # and 4.5). A beat transition invalidates any still-active
    # directive of the old beat; a new directive is generated only when
    # every precondition holds: no transition this step, the story
    # still running, this step's score valid and deviating, feedback
    # enabled, no active directive awaiting execution, and outside the
    # cooldown since the last execution.
    directive = dict(self._directive) if self._directive is not None else None
    if (
        completed
        and directive is not None
        and (directive['status'] in _ACTIVE_DIRECTIVE_STATUSES)
    ):
      directive = {**directive, 'status': _DIRECTIVE_INVALIDATED}
    generate = (
        not budget_stopped
        and self._feedback_enabled
        and tension is not None
        and deviated
        and not completed
        and beat_index < len(self._beats)
        and not beat_timeout
        and not self._in_cooldown(turn_id)
        and (
            directive is None
            or directive['status'] not in _ACTIVE_DIRECTIVE_STATUSES
        )
    )
    if generate:
      try:
        directive = self._generate_directive(
            score=tension,
            beat_id=snapshot['beat_id_at_action'],
            turn_id=turn_id,
        )
      except budget_wrapper.BudgetExhaustedError:
        # Budget path B at the directive-generation position (spec
        # 13.1-9): no directive is created and no further model calls
        # happen, but the step still commits once via update.
        budget_stopped = True

    story_turn = {
        'turn_id': turn_id,
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
        'tension': tension,
        'budget_stopped': budget_stopped,
    }
    self._pending_commit = {
        'beat_index': beat_index,
        'turns_in_beat': turns_in_beat,
        'recent_events': recent_events,
        'beat_timeout': beat_timeout,
        'budget_stopped': budget_stopped,
        'story_turn': story_turn,
        'tension_eval_count': tension_eval_count,
        'tension_history_entry': tension_entry,
        'directive': directive,
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
      self._tension_eval_count = commit['tension_eval_count']
      tension_entry = commit['tension_history_entry']
      if tension_entry is not None:
        self._tension_history.append(tension_entry)
      directive = commit['directive']
      if directive != self._directive:
        self._logging_channel({'DirectorDirective': directive})
      self._directive = directive
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

    A directive being integrated into this resolution is committed as
    consumed here, because only the validated final output proves the
    directive reached the story (spec 4.5). An invalid final output
    raises before this method is reached, leaving the directive
    unconsumed.

    Args:
      resolved_event: The resolved event to publish for the current turn.
      stage_directions: Stage directions accompanying the event.
    """
    integrating = (
        self._directive is not None
        and self._directive['status'] == _DIRECTIVE_INTEGRATING
    )
    if integrating:
      self.commit_directive()
    self._validated = {
        'resolved_event': resolved_event,
        'stage_directions': stage_directions,
        'directive_integrating': integrating,
    }

  def get_pending_directive(self) -> dict[str, Any] | None:
    """Returns a copy of the pending directive, if any.

    Returns:
      A copy of the directive dictionary (beat_id, generated_at_turn,
      content, status) while it awaits execution, or None when no
      directive is pending (including integrating, consumed,
      invalidated and failed ones).
    """
    if (
        self._directive is not None
        and self._directive['status'] == _DIRECTIVE_PENDING
    ):
      return dict(self._directive)
    return None

  def mark_directive_integrating(self) -> None:
    """Moves the pending directive to the integrating status.

    Called by the director-note step when it starts fusing the
    directive into a candidate event. A directive that is not pending
    is left untouched, so a repeated call cannot resurrect one.
    """
    if (
        self._directive is not None
        and self._directive['status'] == _DIRECTIVE_PENDING
    ):
      self._directive = {
          **self._directive,
          'status': _DIRECTIVE_INTEGRATING,
      }

  def commit_directive(self) -> None:
    """Commits the integrating directive as consumed, once.

    Records the story turn being resolved as the execution turn for
    the cooldown computation. A directive that is not integrating is
    left untouched, so a second resolution never re-consumes one.
    """
    if (
        self._directive is None
        or self._directive['status'] != _DIRECTIVE_INTEGRATING
    ):
      return
    self._directive = {**self._directive, 'status': _DIRECTIVE_CONSUMED}
    # The turn in progress is one past the last committed story turn.
    self._last_directive_executed_turn = self._story_turn + 1
    self._logging_channel({'DirectorDirective': self._directive})

  def mark_directive_failed(self, reason: str) -> None:
    """Records an active directive as failed, with its reason.

    Failure degrades to no directive and never interrupts the
    simulation (spec 11.1).

    Args:
      reason: Why the directive could not be executed.
    """
    if (
        self._directive is not None
        and self._directive['status'] in _ACTIVE_DIRECTIVE_STATUSES
    ):
      self._directive = {
          **self._directive,
          'status': _DIRECTIVE_FAILED,
          'reason': reason,
      }
      self._logging_channel({'DirectorDirective': self._directive})

  @property
  def allowed_environment_events(self) -> tuple[str, ...]:
    """Whitelist of NPC/environmental means directives may draw on."""
    return self._allowed_environment_events

  @property
  def immutable_facts(self) -> tuple[str, ...]:
    """Facts directives must never contradict."""
    return self._immutable_facts

  @property
  def player_names(self) -> tuple[str, ...]:
    """Player characters whose voluntary actions directives may not decide."""
    return self._player_names

  def get_last_story_turn(self) -> dict[str, Any] | None:
    """Returns a copy of the most recently committed story turn, if any."""
    if self._last_story_turn is None:
      return None
    return dict(self._last_story_turn)

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
        'last_story_turn': (
            dict(self._last_story_turn)
            if self._last_story_turn is not None
            else None
        ),
        'tension_eval_count': self._tension_eval_count,
        'tension_history': [dict(entry) for entry in self._tension_history],
        'directive': (
            dict(self._directive) if self._directive is not None else None
        ),
        'last_directive_executed_turn': self._last_directive_executed_turn,
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
    last_story_turn = cast('dict[str, Any] | None', state['last_story_turn'])
    self._last_story_turn = (
        dict(last_story_turn) if last_story_turn is not None else None
    )
    # The tension fields postdate the stage-1 state schema; older
    # checkpoints (and fixtures built against them) resume with an
    # empty evaluation history.
    self._tension_eval_count = state.get(  # pyrefly: ignore[bad-assignment]
        'tension_eval_count', 0
    )
    tension_history = cast(
        'Sequence[dict[str, Any]]',
        state.get('tension_history', ()),
    )
    self._tension_history = [dict(entry) for entry in tension_history]
    # The directive fields postdate the stage-1 state schema; older
    # checkpoints resume with no directive and no execution recorded.
    directive = cast('dict[str, Any] | None', state.get('directive', None))
    self._directive = dict(directive) if directive is not None else None
    self._last_directive_executed_turn = state.get(  # pyrefly: ignore[bad-assignment]
        'last_directive_executed_turn', None
    )

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

    The prompt is the P1 template (plan appendix B): the current beat
    and its completion question head it, the event window follows, and
    the criteria block closes it, anchoring the judgment to events that
    actually happened rather than intentions or plans.

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
    prompt.statement('# Current story beat')
    # The beat-id line is also the fingerprint the story enactment
    # example's prompt stub routes completion answers by.
    prompt.statement(f'Current beat "{beat.id}": {beat.description}\n')
    prompt.statement(f'Completion question: {beat.done_question}\n')
    prompt.statement('## Recent events (oldest first)')
    for event in recent_events:
      prompt.statement(f'- {event}')
    prompt.statement('## Completion criteria')
    prompt.statement(
        'Judging only by events that have actually happened (not'
        " intentions or future plans), is the beat's completion"
        ' question satisfied?'
    )
    try:
      return prompt.yes_no_question(question=beat.done_question)
    except language_model.InvalidResponseError:
      # The model failed to pick one of the offered choices; treat the
      # beat as not completed rather than guessing.
      return False

  def _target_range_of(self, beat_id: str | None) -> tuple[int, int] | None:
    """Returns the target tension range of the beat with the given id.

    Args:
      beat_id: The beat that was current when the player acted.

    Returns:
      The beat's inclusive (lower, upper) tension bounds, or None if no
      beat matches (e.g. the story was already complete).
    """
    if beat_id is None:
      return None
    for beat in self._beats:
      if beat.id == beat_id:
        return beat.tension_range
    return None

  def _score_tension(self, recent_events: Sequence[str]) -> int | None:
    """Asks the model to rate the story's dramatic tension on 1-10.

    The prompt carries only the anchored scale and the most recent
    `scoring_window` resolved events of the director's own window; the
    target range of the beat is deliberately absent, because the
    deviation judgment is made in program code. An unparseable answer is
    retried exactly once (spec section 11.1).

    Args:
      recent_events: The step's updated event window; the last
        `scoring_window` events condition the question.

    Returns:
      The parsed score, or None if both answers were invalid.

    Raises:
      budget_wrapper.BudgetExhaustedError: If the attempt budget is spent;
        propagated to the caller for budget path B handling. Any other
        model error propagates uncaught.
    """
    window = list(recent_events[-self._scoring_window :])
    document = interactive_document.InteractiveDocument(self._model)
    document.statement(f'{_TENSION_QUESTION}\n')
    document.statement('## Scale anchors')
    for anchor in _TENSION_ANCHORS:
      document.statement(anchor)
    document.statement('## Recent events (oldest first)')
    for event in window:
      document.statement(f'- {event}')
    document.statement('## Output')
    score = _parse_tension_score(
        document.open_question(_TENSION_OUTPUT_INSTRUCTION)
    )
    if score is None:
      # Retry once on an unparseable answer only; API and budget errors
      # are never treated as parse failures.
      score = _parse_tension_score(
          document.open_question(_TENSION_OUTPUT_INSTRUCTION)
      )
    return score

  def _in_cooldown(self, turn_id: int) -> bool:
    """Decides whether the given story turn falls in the cooldown.

    Args:
      turn_id: The story turn being processed.

    Returns:
      Whether fewer than `cooldown_turns` + 1 turns have passed since
      the last directive execution (the executing turn itself counts
      as the first cooldown turn).
    """
    last = self._last_directive_executed_turn
    return last is not None and turn_id - last <= self._cooldown_turns

  def _generate_directive(
      self,
      *,
      score: int,
      beat_id: str,
      turn_id: int,
  ) -> dict[str, Any]:
    """Asks the model for a directive moving the tension toward range.

    The prompt is the P3 template (plan appendix B): it carries the
    deviation, the direction of correction and the constraints that
    only whitelisted NPC/environmental means may be used and immutable
    facts must hold, so directives can never decide player characters'
    voluntary actions.

    Args:
      score: This step's valid, deviating tension score.
      beat_id: The beat the directive binds to, the one current when
        the player acted.
      turn_id: The story turn generating the directive.

    Returns:
      The new directive dictionary. An empty model answer is recorded
      as a generation failure with its reason (spec 11.1).

    Raises:
      budget_wrapper.BudgetExhaustedError: If the attempt budget is
        spent; propagated to the caller for budget path B handling.
        Any other model error propagates uncaught.
    """
    lower, upper = self._target_range_of(beat_id) or (0, 0)
    raise_or_lower = 'raise' if score < lower - self._tolerance else 'lower'
    events = (
        '; '.join(self._allowed_environment_events)
        if self._allowed_environment_events
        else '(none)'
    )
    facts = (
        '; '.join(self._immutable_facts) if self._immutable_facts else '(none)'
    )
    document = interactive_document.InteractiveDocument(self._model)
    document.statement(
        f'Current tension ({score}) is outside the target range '
        f'({lower}, {upper}) for the current beat. The story needs '
        f'{raise_or_lower} dramatic pressure.\n'
    )
    document.statement('## Constraints')
    document.statement(
        "You may ONLY use these means, never player characters' "
        f'voluntary actions: {events}'
    )
    content = document.open_question(
        'Write one short director instruction (one sentence) describing '
        f'an event or NPC action that moves tension {raise_or_lower}. '
        f'Never contradict immutable facts: {facts}'
    ).strip()
    if not content:
      return {
          'beat_id': beat_id,
          'generated_at_turn': turn_id,
          'content': '',
          'status': _DIRECTIVE_FAILED,
          'reason': 'empty directive instruction',
      }
    return {
        'beat_id': beat_id,
        'generated_at_turn': turn_id,
        'content': content,
        'status': _DIRECTIVE_PENDING,
    }
