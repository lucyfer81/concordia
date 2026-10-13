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

"""Agent-side relationship perception refreshed from observed events.

Characters derive their view of the other story participants (warmth,
trust, most recent interaction) only from the adjudicated events they
have actually received, never from game-master state or other agents'
internals. The derived view is cached across steps and refreshed with a
single model call only once `refresh_interval` new adjudicated events
have been observed, mirroring the bounded updating of social attitudes:
impressions persist until enough new evidence accumulates, rather than
being recomputed on every step (see the Concordia design pattern,
arXiv:2507.08892).

Adjudicated events reach the agent as observation batches produced by
the game master's `MakeObservation` queue: consecutive queue items are
joined by a triple-newline separator, and each resolved event published
by the event-resolution chain carries the reserved `Event: ` prefix
(that chain's pre-act label). Public premises and ordinary background
observations never use the reserved prefix, so they cannot inflate the
refresh cursor.
"""

from collections.abc import Sequence
from typing import override

from concordia.components.agent import action_spec_ignored
from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.document import interactive_document
from concordia.language_model import language_model
from concordia.typing import entity_component

DEFAULT_RELATIONSHIPS_PRE_ACT_LABEL = 'Relationships to others'

# Separator the observation queue uses between consecutive items.
_OBSERVATION_SEPARATOR = '\n\n\n'

# Reserved prefix of adjudicated-event blocks, reused from the event
# resolution chain's pre-act label (plan appendix D-4).
_EVENT_BLOCK_PREFIX = (
    f'{event_resolution_lib.DEFAULT_RESOLUTION_PRE_ACT_LABEL}: '
)


class RelationshipsToOthers(
    action_spec_ignored.ActionSpecIgnored,
    entity_component.ComponentWithLogging,
):
  """Cached view of the agent's relationships with the other players.

  The component holds only this agent's actually-received adjudicated
  events. In `_make_pre_act_value` it returns the cached relationship
  summary, or, once `refresh_interval` new events have been committed
  since the last refresh, asks the model once (plan appendix B,
  prompt P6) and records a new refresh cursor. The parent class clears
  its pre-act cache on every `update`, which is precisely why the
  refresh cursor and the derived summary are kept here as component
  state.
  """

  def __init__(
      self,
      model: language_model.LanguageModel,
      other_player_names: Sequence[str],
      *,
      refresh_interval: int = 3,
      pre_act_label: str = DEFAULT_RELATIONSHIPS_PRE_ACT_LABEL,
  ):
    """Initializes the component.

    Args:
      model: The language model asked for the relationship summary.
      other_player_names: Names of the other story participants, as
        publicly known.
      refresh_interval: Number of newly observed adjudicated events
        between relationship refreshes; must be at least 1.
      pre_act_label: Prefix for this component's section of the pre-act
        context.

    Raises:
      ValueError: If `refresh_interval` is below 1.
    """
    super().__init__(pre_act_label=pre_act_label)
    if refresh_interval < 1:
      raise ValueError(
          f'refresh_interval must be >= 1, got {refresh_interval}.'
      )
    self._model = model
    self._other_player_names = tuple(other_player_names)
    self._refresh_interval = refresh_interval

    # Committed state, restored by set_state.
    self._recent_events: list[str] = []
    self._observed_count = 0
    self._last_refresh = 0
    self._cache: str | None = None

    # Pending state, committed by update.
    self._pending_batch: list[str] = []
    self._pending_events: list[str] = []

  @override
  def pre_observe(
      self,
      observation: str,
  ) -> str:
    # Split the observation queue's output into its blocks and keep
    # only complete adjudicated-event blocks: the reserved prefix is
    # the resolution chain's, and premises never use it. Queued events
    # end with a newline of their own, so blocks after the first start
    # with a leftover newline where that newline meets the separator;
    # stripping the separator artifacts recovers the event blocks.
    self._pending_batch = [
        block
        for block in (
            raw.strip() for raw in observation.split(_OBSERVATION_SEPARATOR)
        )
        if block.startswith(_EVENT_BLOCK_PREFIX)
    ]
    return ''

  @override
  def post_observe(self) -> str:
    self._pending_events.extend(self._pending_batch)
    self._pending_batch = []
    return ''

  @override
  def update(self) -> None:
    if self._pending_events:
      self._recent_events.extend(self._pending_events)
      self._observed_count += len(self._pending_events)
      self._pending_events = []
    if self._observed_count - self._last_refresh >= self._refresh_interval:
      self._cache = None
    # Always clear the parent's pre-act cache; the derived summary kept
    # in `_cache` is what survives across steps.
    super().update()

  @override
  def _make_pre_act_value(self) -> str:
    if self._cache is not None:
      return self._cache
    result = self._query_relationships()
    self._cache = result
    self._last_refresh = self._observed_count
    return result

  @override
  def get_state(self) -> entity_component.ComponentState:
    """Returns the component's state."""
    return {
        'cache': self._cache,
        'observed_count': self._observed_count,
        'last_refresh': self._last_refresh,
        'recent_events': list(self._recent_events),
        'refresh_interval': self._refresh_interval,
    }

  @override
  def set_state(self, state: entity_component.ComponentState) -> None:
    """Sets the component's state.

    Args:
      state: A state returned by `get_state`.
    """
    self._cache = state['cache']  # pyrefly: ignore[bad-assignment]
    self._observed_count = state['observed_count']  # pyrefly: ignore[bad-assignment]
    self._last_refresh = state['last_refresh']  # pyrefly: ignore[bad-assignment]
    self._recent_events = list(  # pyrefly: ignore[bad-assignment]
        state['recent_events']  # pyrefly: ignore[bad-argument-type]
    )
    self._refresh_interval = state[  # pyrefly: ignore[bad-assignment]
        'refresh_interval'
    ]

  def _query_relationships(self) -> str:
    """Asks the model for the relationship summary (prompt P6).

    The prompt carries only this agent's own observed event window and
    the other participants' names, and asks for one short line per
    person describing warmth, trust and the most recent interaction.

    Returns:
      The model's relationship summary.
    """
    name = self.get_entity().name
    document = interactive_document.InteractiveDocument(self._model)
    document.statement(f'# Observed story events of {name} (oldest first)\n')
    for event in self._recent_events:
      document.statement(f'- {event}')
    document.statement('## People present')
    for other_name in self._other_player_names:
      document.statement(f'- {other_name}')
    document.statement('## Output')
    result = document.open_question(
        question=(
            'For each person present, write one short line summarizing '
            f"{name}'s current relationship with them: the warmth, the "
            'trust, and their most recent interaction. Base every line '
            'only on the observed events above.'
        ),
        max_tokens=3000,
        terminators=(),
    )
    self._logging_channel({
        'Key': self.get_pre_act_label(),
        'Value': result,
        'Prompt': document.view().text().splitlines(),
    })
    return result
