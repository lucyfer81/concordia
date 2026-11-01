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

"""Deterministic two-view delivery of one resolved story turn.

This is the story-specific replacement of the core
``SendEventToRelevantPlayers`` sender inside the story prefabs: where
the generic sender broadcast one event text to the relevant players
(and deduplicated it by string replacement against what it sent
before), this adapter delivers the *audience-selected* view of the
turn that the drama director already validated — the full resolved
event to the acting character, the public resolved event to every
other participant of the current scene (design section 6.1). It calls
no model and rewrites no text: each recipient receives the reviewed
view verbatim, with no fragment-level dedup or replacement, so
repeated wording inside a view survives exactly as it was approved.

Delivery happens at the existing RESOLVE ``post_act`` point of the
game master's act cycle, and exactly-once delivery and crash recovery
ride the existing turn and checkpoint boundaries rather than adding a
new protocol: the adapter forwards only data the director validated
for the current turn (the getter returns None once a later putative
event clears it), and it holds no state of its own that could survive
a checkpoint. Every check — contract version, field presence and
types, actor binding, match between the engine's return value and the
validated event, and the legality of every recipient — completes
before the first queue write, so no failure path can deliver a partial
audience split, and there is never a fallback to broadcasting the full
event. The underlying ``MakeObservation.add_to_queue`` accepts any
player name and interprets a lowercase ``'all'`` as broadcast, so
recipient legality (configured, unique, non-broadcast-alias, actor
present) is validated here, on the whole recipient list at once.
"""

from collections.abc import Callable, Sequence

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.components.game_master import make_observation as make_observation_lib
from concordia.contrib.components.game_master import drama_director as drama_director_lib
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

# The string fields every validated delivery payload must carry.
_DELIVERY_STRING_FIELDS = (
    'actor',
    'resolved_event',
    'stage_directions',
    'public_resolved_event',
    'public_stage_directions',
)

# The core `EventResolution` act component returns the resolved event
# wrapped as ``'Event: {event}\n'`` (its default pre-act label plus one
# trailing newline); that wrapped form is the engine's echo the adapter
# must match exactly against the validated event.
_ENGINE_ECHO_PREFIX = (
    f'{event_resolution_lib.DEFAULT_RESOLUTION_PRE_ACT_LABEL}: '
)


def engine_echo_of(resolved_event: str) -> str:
  """Returns the engine's canonical RESOLVE return value of an event.

  Args:
    resolved_event: the validated full resolved event.

  Returns:
    The exact string the core event-resolution act component returns
    for this event, so callers can compare the engine's echo strictly.
  """
  return f'{_ENGINE_ECHO_PREFIX}{resolved_event}\n'


class AudienceDelivery(
    entity_component.ContextComponent, entity_component.ComponentWithLogging
):
  """Delivers the validated full and public views to their audiences.

  The component consumes the drama director's validated two-view data
  of the current turn and queues the full event for the acting
  character and the public event for the other scene participants
  through the game master's make-observation component. An empty
  public event queues nothing for the others (a purely private turn),
  and characters outside the current scene receive nothing.
  """

  def __init__(
      self,
      *,
      player_names: Sequence[str],
      make_observation_component_key: str,
      player_filter: Callable[[], Sequence[str]],
      drama_director_component_key: str,
      pre_act_label: str = 'Audience delivery',
  ):
    """Initializes the delivery adapter.

    Args:
      player_names: every configured character; the actor and each
        recipient must be among them.
      make_observation_component_key: key of the make-observation
        component whose per-player queues are written.
      player_filter: callback returning the current scene's
        participants, e.g. the scene tracker's ``get_participants``.
      drama_director_component_key: key of the drama director holding
        the current turn's validated two-view data.
      pre_act_label: label of the component's logging channel entries.
    """
    super().__init__()
    self._player_names = tuple(player_names)
    self._make_observation_component_key = make_observation_component_key
    self._player_filter = player_filter
    self._drama_director_component_key = drama_director_component_key
    self._pre_act_label = pre_act_label
    self._last_action_spec: entity_lib.ActionSpec | None = None

  def pre_act(self, action_spec: entity_lib.ActionSpec) -> str:
    """Records the action spec and contributes no context."""
    self._last_action_spec = action_spec
    return ''

  def get_state(self) -> entity_component.ComponentState:
    """Returns the component's (empty) checkpoint state.

    Delivery completes within one RESOLVE act cycle — validation and
    queue writes all happen in ``post_act`` — and checkpoints are only
    taken between steps, so the component deliberately persists
    nothing: exactly-once delivery and recovery ride the director's
    turn and checkpoint boundaries, never a delivery-local state.
    """
    return {}

  def set_state(self, state: entity_component.ComponentState) -> None:
    """Restores the (empty) checkpoint state; see `get_state`."""
    del state

  def post_act(self, action_attempt: str) -> str:
    """Delivers the validated views at the RESOLVE post-act point.

    All validation happens before the first queue write: a mismatched
    engine echo, missing or stale validated data, a wrong contract
    version, an unbound actor or any illegal recipient raises
    ``ValueError`` with nothing queued, and there is no fallback to
    sending the full event to everyone.

    Args:
      action_attempt: the engine's return value of the RESOLVE step,
        which must be the engine's canonical wrapped form of the
        validated full resolved event (see `engine_echo_of`).

    Returns:
      An empty string; delivery contributes no act result.

    Raises:
      ValueError: when the validated data is absent, stale or
        inconsistent, or when any recipient is illegal.
    """
    if (
        self._last_action_spec is None
        or self._last_action_spec.output_type != entity_lib.OutputType.RESOLVE
    ):
      return ''

    director = self.get_entity().get_component(
        self._drama_director_component_key,
        type_=drama_director_lib.DramaDirector,
    )
    data = director.get_validated_delivery_data()
    if data is None:
      raise ValueError(
          'Audience delivery found no validated delivery data: the'
          ' turn was not validated (or its approval was cleared) at'
          ' the RESOLVE post-act point; refusing to broadcast.'
      )
    version = data.get('audience_delivery_version')
    if version != drama_director_lib.AUDIENCE_DELIVERY_VERSION:
      raise ValueError(
          'Audience delivery contract version mismatch: expected'
          f' {drama_director_lib.AUDIENCE_DELIVERY_VERSION}, got'
          f' {version!r}; refusing to deliver.'
      )
    missing = [
        field
        for field in _DELIVERY_STRING_FIELDS
        if not isinstance(data.get(field), str)
    ]
    if missing:
      raise ValueError(
          'Audience delivery data has missing or non-string (e.g. None)'
          f' fields {missing}; refusing to deliver.'
      )
    actor = data['actor']
    resolved_event = data['resolved_event']
    public_event = data['public_resolved_event']
    if actor not in self._player_names:
      raise ValueError(
          f'Audience delivery actor {actor!r} is not a configured player;'
          f' configured players: {list(self._player_names)}.'
      )
    if action_attempt != engine_echo_of(resolved_event):
      raise ValueError(
          'Audience delivery engine echo mismatch: the RESOLVE return'
          ' value is not the canonical engine form of the validated'
          ' resolved event; refusing to deliver either view.'
      )
    participants = self._player_filter()
    # The whole recipient list is validated before any write: the
    # underlying queue accepts any name and treats a lowercase 'all'
    # as broadcast, so an illegal recipient must fail here, up front.
    seen: set[str] = set()
    for name in participants:
      if not isinstance(name, str):
        raise ValueError(
            f'Audience delivery recipient {name!r} is not a string.'
        )
      if name.lower().strip() == 'all':
        raise ValueError(
            "Audience delivery recipient 'all' is a broadcast alias of"
            ' the observation queue; explicit per-recipient delivery'
            ' only.'
        )
      if name not in self._player_names:
        raise ValueError(
            f'Audience delivery recipient {name!r} is not a configured'
            f' player; configured players: {list(self._player_names)}.'
        )
      if name in seen:
        raise ValueError(
            f'Audience delivery recipient {name!r} appears more than'
            ' once in the scene participants.'
        )
      seen.add(name)
    if actor not in seen:
      raise ValueError(
          f'Audience delivery actor {actor!r} is not among the scene'
          f' participants {list(participants)}; the actor cannot be'
          ' delivered around the scene boundary.'
      )

    make_observation = self.get_entity().get_component(
        self._make_observation_component_key,
        type_=make_observation_lib.MakeObservation,
    )
    recipient_views: dict[str, dict[str, object]] = {}
    for name in participants:
      if name == actor:
        make_observation.add_to_queue(name, resolved_event)
        recipient_views[name] = {'view': 'full', 'length': len(resolved_event)}
      elif public_event:
        make_observation.add_to_queue(name, public_event)
        recipient_views[name] = {'view': 'public', 'length': len(public_event)}
      else:
        recipient_views[name] = {'view': 'skipped', 'length': 0}
    self._logging_channel({
        'Key': self._pre_act_label,
        'Summary': (
            f'delivered to {len(recipient_views)} recipients: {recipient_views}'
        ),
        'Value': recipient_views,
        'Recipients': recipient_views,
    })
    return ''
