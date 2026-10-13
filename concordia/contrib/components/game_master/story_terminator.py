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

"""Game master component that terminates a story enactment and reports why.

The `SceneOrStoryTerminator` centralizes the game master's termination
decision for story enactments. A run should stop when its scene schedule
is exhausted (tracked by the `SceneTracker`), or when the story itself
ends: all dramaturgic beats completed, a beat timed out, or the
language-model attempt budget was spent (all tracked by the
`DramaDirector`). When the scene schedule runs out, the `SceneTracker`
calls `terminate()` on this component, so the scene budget flows through
both components' decisions.

Termination reasons follow a fixed priority (spec section 4.7): at the
component level `beats_completed > beat_timeout > scene_limit >
budget_limit`, and at the run level `error` takes precedence over
everything while `step_limit` ranks between `scene_limit` and
`budget_limit`. The module-level `resolve_final_reason` combines the
reason recorded by the run loop (an exception or budget exhaustion) with
the component's reason and the global story-turn limit into the single
final reason of the run. It deliberately uses the drama director's
committed story-turn count, which survives pauses and resumes, instead
of engine step numbers, which reset after a resume.
"""

from concordia.components.game_master import scene_tracker as scene_tracker_lib
from concordia.contrib.components.game_master import drama_director as drama_director_lib
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

# Termination reasons of a story run (spec section 4.7).
REASON_ERROR = 'error'
REASON_BEATS_COMPLETED = 'beats_completed'
REASON_BEAT_TIMEOUT = 'beat_timeout'
REASON_SCENE_LIMIT = 'scene_limit'
REASON_STEP_LIMIT = 'step_limit'
REASON_BUDGET_LIMIT = 'budget_limit'

# Final-reason priority, highest first (spec section 4.7).
_REASON_PRIORITY = (
    REASON_ERROR,
    REASON_BEATS_COMPLETED,
    REASON_BEAT_TIMEOUT,
    REASON_SCENE_LIMIT,
    REASON_STEP_LIMIT,
    REASON_BUDGET_LIMIT,
)


class SceneOrStoryTerminator(
    entity_component.ContextComponent, entity_component.ComponentWithLogging
):
  """Terminates a story enactment on scene-budget or story-level grounds.

  The component cooperates with two siblings of the same game master: the
  `SceneTracker`, which budgets the run as a schedule of scenes, and the
  `DramaDirector`, which advances the story through its beats. Either
  sibling may be absent or not expose the queried methods, in which case
  its conditions simply never hold.
  """

  def __init__(
      self,
      scene_tracker_component_key: str = (
          scene_tracker_lib.DEFAULT_SCENE_TRACKER_COMPONENT_KEY
      ),
      drama_director_component_key: str = (
          drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY
      ),
  ):
    """Initializes the terminator.

    Args:
      scene_tracker_component_key: Key of the scene tracker component whose
        scene budget limits the run.
      drama_director_component_key: Key of the drama director component
        tracking the story's beats and model budget.
    """
    super().__init__()
    self._scene_tracker_component_key = scene_tracker_component_key
    self._drama_director_component_key = drama_director_component_key
    self._terminate_now = False

  def pre_act(self, action_spec: entity_lib.ActionSpec) -> str:
    """Returns 'Yes' on TERMINATE when the simulation should stop.

    The answer is 'Yes' when `terminate` was called, when the drama
    director stopped the story (beats completed, beat timed out or budget
    spent), or when the scene tracker exhausted its scene budget.

    Args:
      action_spec: The action spec for the action attempt.

    Returns:
      'Yes' or 'No' for TERMINATE actions, 'No' for any other action.
    """
    if action_spec.output_type == entity_lib.OutputType.TERMINATE:
      if (
          self._terminate_now
          or self._sibling_flag(
              self._drama_director_component_key, 'is_story_stopped'
          )
          or self._sibling_flag(self._scene_tracker_component_key, 'is_done')
      ):
        return 'Yes'
    return 'No'

  def terminate(self) -> None:
    """Flags the simulation for termination, whatever the siblings say."""
    self._terminate_now = True

  def termination_reason(self) -> str:
    """Returns why the simulation should stop, by component-level priority.

    The priority is `beats_completed > beat_timeout > scene_limit >
    budget_limit`: completing the story outranks a stalled beat, running
    out of scenes outranks running out of model budget.

    Returns:
      The dominant termination reason, or '' while no termination
      condition holds yet.
    """
    if self._sibling_flag(self._drama_director_component_key, 'is_beats_done'):
      return REASON_BEATS_COMPLETED
    if self._sibling_flag(self._drama_director_component_key, 'beat_timeout'):
      return REASON_BEAT_TIMEOUT
    if self._sibling_flag(self._scene_tracker_component_key, 'is_done'):
      return REASON_SCENE_LIMIT
    if self._sibling_flag(
        self._drama_director_component_key, 'is_story_stopped'
    ):
      return REASON_BUDGET_LIMIT
    return ''

  def get_state(self) -> entity_component.ComponentState:
    """Returns the state of the component."""
    return {
        'terminate_now': self._terminate_now,
    }

  def set_state(self, state: entity_component.ComponentState) -> None:
    """Sets the state of the component.

    Args:
      state: A state returned by `get_state`.
    """
    self._terminate_now = state['terminate_now']  # pyrefly: ignore[bad-assignment]

  def _sibling_flag(self, component_key: str, method_name: str) -> bool:
    """Queries a boolean flag on a sibling component, if it exposes one.

    Args:
      component_key: Registry key of the sibling component.
      method_name: Name of the parameterless boolean method to query.

    Returns:
      The method's result, or False when the sibling is not registered or
      does not expose the method.
    """
    try:
      component = self.get_entity().get_component(component_key)
    except KeyError:
      return False
    method = getattr(component, method_name, None)
    if method is None:
      return False
    return bool(method())


def resolve_final_reason(
    exception_reason: str | None,
    component_reason: str | None,
    committed_turn_id: int,
    total_turn_limit: int,
) -> str:
  """Resolves the single final reason why a story run ended.

  Args:
    exception_reason: Reason recorded when the run aborted with an
      exception, typically 'error', or 'budget_limit' when the attempt
      budget was exhausted.
    component_reason: Reason reported by `termination_reason`, if any.
    committed_turn_id: Number of story turns the drama director committed;
      unlike engine step numbers it survives pauses and resumes.
    total_turn_limit: Global cap on story turns across the whole run.

  Returns:
    The highest-priority candidate among the given reasons and the
    step-limit candidate implied by `committed_turn_id >=
    total_turn_limit`, with priority error > beats_completed >
    beat_timeout > scene_limit > step_limit > budget_limit. 'error' when
    no candidate applies.
  """
  candidates = {
      reason for reason in (exception_reason, component_reason) if reason
  }
  if committed_turn_id >= total_turn_limit:
    candidates.add(REASON_STEP_LIMIT)
  for reason in _REASON_PRIORITY:
    if reason in candidates:
      return reason
  return REASON_ERROR
