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

"""A game master prefab that enacts a story through dramaturgic beats.

The game master follows the scene-based assembly of the
`dialogic_and_dramaturgic` prefab and swaps in the story-enactment
components of `concordia.contrib.components.game_master`: a
`DramaDirector` advances the story through its beats and periodically
scores the dramatic tension of the recent events, event resolution
produces the final narrator-track output that separates player speech
(agency) from stage directions (narrator authority), and a
`SceneOrStoryTerminator` stops the run when either the scene schedule
or the story itself ends.
Every resolution is conditioned on the current beat, the world constraints,
and all character constraints, with a separate model review before
publication.

In `speech_and_action` mode (the `action_mode` prefab param, defaulting
to pure speech) the resolution chain first adjudicates the current
character's own action attempt through the core `attempt_to_result`
helper; its true return value is recorded as resolution evidence that
the director-note step, the single-field stage-directions final
generation and the semantic review all consume, so only adjudicated
outcomes — never unadjudicated intents — reach the public event.

Configured public NPC response rules (the `npc_response_rules` prefab
param, design section 5.2) are validated at build time and forwarded as
inert data to the adjudication and final-output factories: authorized
NPC responses are generated within the configured scope by the same
single core adjudication call, adding no model calls.
"""

from collections.abc import Mapping, Sequence
import dataclasses
from typing import Any

from concordia.agents import entity_agent_with_logging
from concordia.associative_memory import basic_associative_memory as associative_memory
from concordia.components import agent as actor_components
from concordia.components import game_master as gm_components
from concordia.components.game_master import event_resolution as thought_chains_lib
from concordia.contrib.components.game_master import director_note
from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import observation_queue
from concordia.contrib.components.game_master import repetition_push
from concordia.contrib.components.game_master import story_action
from concordia.contrib.components.game_master import story_action_adjudication
from concordia.contrib.components.game_master import story_resolution
from concordia.contrib.components.game_master import story_terminator
from concordia.contrib.prefabs.game_master import story_scene
from concordia.language_model import language_model
from concordia.typing import prefab as prefab_lib

DEFAULT_NAME = 'story rules'

# Key of the Constant component that carries the world constraints.
WORLD_CONSTRAINTS_COMPONENT_KEY = 'world_constraints'
WORLD_CONSTRAINTS_PRE_ACT_LABEL = '\nWorld constraints'


@dataclasses.dataclass
class GameMaster(prefab_lib.Prefab):
  """A prefab game master that enacts a story through dramaturgic beats."""

  supports_extra_components = True

  description: str = (  # pyrefly: ignore[bad-override]
      'A game master that enacts a story through dramaturgic beats. Scenes '
      'structure the schedule, a drama director advances the story, and '
      'event resolution preserves player speech verbatim while respecting '
      'world constraints and immutable facts.'
  )
  params: Mapping[str, Any] = dataclasses.field(
      default_factory=lambda: {
          'name': DEFAULT_NAME,
          # The characters' action contract: 'speech_only' treats a
          # raw action as pure speech, 'speech_and_action' expects
          # the strict JSON object of `story_action` (spec section
          # 3). Validated at build time and forwarded to the
          # director and the adjudication chain.
          'action_mode': story_action.ACTION_MODE_SPEECH_ONLY,
          # Scene schedule, as dictionaries encoded by
          # `concordia.contrib.prefabs.game_master.story_scene`.
          'scene_dicts': (),
          # Story beats, as dictionaries encoded by `Beat.to_dict`.
          'beat_dicts': (),
          # Drama director scoring configuration (plan appendix C):
          # score every eval_interval-th story turn against the last
          # scoring_window resolved events, with the given tolerance
          # around each beat's target tension range. JSON-safe ints.
          'eval_interval': 2,
          'scoring_window': 8,
          'tolerance': 1,
          # Director feedback configuration (plan appendix C): with
          # feedback enabled, deviating scores generate directives that
          # the director-note resolution step weaves into events using
          # only the whitelisted NPC/environmental means, never
          # contradicting the immutable facts. JSON-safe values.
          'cooldown_turns': 3,
          'feedback_enabled': False,
          'allowed_environment_events': (),
          # GM-only content authorization for public direct responses of
          # already-introduced NPCs (design section 5.2); validated at
          # build and forwarded to the adjudication and final output
          # factories. Empty keeps the old boundary.
          'npc_response_rules': (),
          # Repetition intervention (spec section 7.2): with the switch
          # enabled, every `repetition_check_interval`-th story turn the
          # resolution chain may run the core narrative-push helper on
          # the current candidate; the interval must be a positive
          # integer. The switch defaults off, so pre-existing configs
          # keep their behavior until they opt in.
          'repetition_push_enabled': False,
          'repetition_check_interval': 3,
          # World constraints shown to the game master in every resolution.
          'world_constraints': (),
          # All player names and their hard constraints (GM-only).
          'player_names': (),
          'character_constraints': {},
          # Initial public background is evidence, unlike GM-only secrets.
          'public_facts': (),
          # Facts no resolved event may contradict.
          'immutable_facts': (),
          'extra_components': {},
          # A mapping from component name to the index at which to insert it
          # in the component order. If not specified, the extra components
          # will be inserted at the end of the component order.
          'extra_components_index': {},
      }
  )
  entities: Sequence[entity_agent_with_logging.EntityAgentWithLogging] = ()

  def build(
      self,
      model: language_model.LanguageModel,
      memory_bank: associative_memory.AssociativeMemoryBank,
  ) -> entity_agent_with_logging.EntityAgentWithLogging:
    """Build a game master that enacts a story through its beats.

    Args:
      model: The language model to use for game master.
      memory_bank: provide a memory_bank.

    Returns:
      A game master entity.
    """
    self.check_extra_components()
    name = self.params.get('name', DEFAULT_NAME)

    extra_components = self.params.get('extra_components', {})
    extra_components_index = self.params.get('extra_components_index', {})

    if extra_components_index and extra_components:
      if extra_components_index.keys() != extra_components.keys():
        raise ValueError(
            'extra_components_index must have the same keys as'
            ' extra_components.'
        )

    player_names = list(
        self.params.get('player_names')
        or [entity.name for entity in self.entities]
    )

    scenes = [
        story_scene.scene_from_dict(encoded)
        for encoded in self.params.get('scene_dicts', ())
    ]
    beats = [
        drama_director.Beat.from_dict(encoded)
        for encoded in self.params.get('beat_dicts', ())
    ]

    instructions_key = 'instructions'
    instructions = gm_components.instructions.Instructions()

    player_characters_key = 'player_characters'
    player_characters = gm_components.instructions.PlayerCharacters(
        player_characters=player_names,
    )

    observation_to_memory_key = 'observation_to_memory'
    observation_to_memory = actor_components.observation.ObservationToMemory()

    observation_component_key = (
        actor_components.observation.DEFAULT_OBSERVATION_COMPONENT_KEY
    )
    observation = actor_components.observation.LastNObservations(
        history_length=1000,
    )

    display_events_key = 'display_events'
    display_events = gm_components.event_resolution.DisplayEvents(
        model=model,
        pre_act_label='Conversation',
    )

    memory_component_key = actor_components.memory.DEFAULT_MEMORY_COMPONENT_KEY
    memory = actor_components.memory.AssociativeMemory(memory_bank=memory_bank)  # pyrefly: ignore[bad-argument-type]

    make_observation_key = (
        gm_components.make_observation.DEFAULT_MAKE_OBSERVATION_COMPONENT_KEY
    )
    # Only explicitly queued information reaches the players: no shared
    # external queue and no LLM fallback, so observations are limited to
    # scene premises and resolved events sent to the participants. The
    # boundary-encoding subclass keeps multi-paragraph events from being
    # confused with the queue's triple-newline item separator.
    make_observation = observation_queue.BoundaryEncodingMakeObservation(
        model=model,
        player_names=player_names,
        components=[
            observation_component_key,
            display_events_key,
        ],
        external_queue=None,
        allow_llm_fallback=False,
    )

    scene_tracker = gm_components.scene_tracker.SceneTracker(
        model=model,
        scenes=scenes,
        observation_component_key=make_observation_key,
    )

    # SendEventToRelevantPlayers handles notifying players about events.
    # Use scene tracker's get_participants as a filter to limit
    # notifications to the current scene's participants.
    send_events_to_players_key = (
        gm_components.event_resolution.DEFAULT_SEND_PRE_ACT_VALUES_TO_PLAYERS_PRE_ACT_LABEL
    )
    scene_tracker_key = (
        gm_components.next_game_master.DEFAULT_NEXT_GAME_MASTER_COMPONENT_KEY
    )
    send_events_to_players = (
        gm_components.event_resolution.SendEventToRelevantPlayers(
            model=model,
            player_names=player_names,
            make_observation_component_key=make_observation_key,
            player_filter=scene_tracker.get_participants,
        )
    )

    next_actor_key = gm_components.next_acting.DEFAULT_NEXT_ACTING_COMPONENT_KEY
    next_action_spec_key = (
        gm_components.next_acting.DEFAULT_NEXT_ACTION_SPEC_COMPONENT_KEY
    )

    next_actor = gm_components.next_acting.NextActingFromSceneSpec(
        scene_tracker_component_key=scene_tracker_key,
    )
    next_action_spec = gm_components.next_acting.NextActionSpecFromSceneSpec(
        scene_tracker_component_key=scene_tracker_key,
    )

    # The action contract is validated here and forwarded to the
    # director, which decodes each turn's raw action per the mode; the
    # adjudication chain step consumes the decoded intent. The default
    # keeps the pure-speech stage-1 behavior (spec sections 3 and 8).
    action_mode = self.params.get(
        'action_mode', story_action.ACTION_MODE_SPEECH_ONLY
    )
    story_action.validate_action_mode(action_mode)

    # Design 5.2: the NPC response rules are validated here, before any
    # component is wired and thus before any model call, so a
    # misconfigured run fails loudly at build time. A bare string is a
    # common configuration mistake and is rejected whole, as is any
    # non-sequence; blank or non-string elements are rejected per
    # element.
    raw_npc_rules = self.params.get('npc_response_rules', ())
    if isinstance(raw_npc_rules, str) or not isinstance(
        raw_npc_rules, (list, tuple)
    ):
      raise ValueError(
          'npc_response_rules must be a sequence of non-empty strings,'
          f' got {raw_npc_rules!r}.'
      )
    npc_response_rules = tuple(raw_npc_rules)
    for rule in npc_response_rules:
      if not isinstance(rule, str) or not rule.strip():
        raise ValueError(
            'npc_response_rules must be a sequence of non-empty'
            f' strings, got element {rule!r}.'
        )

    # The drama director advances the story through its beats; its pre-act
    # value exposes the current beat to every resolution prompt. The
    # scoring, feedback and repetition configuration comes from the
    # JSON-safe prefab params.
    drama_director_key = drama_director.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY
    repetition_push_enabled = self.params.get('repetition_push_enabled', False)
    repetition_check_interval = self.params.get('repetition_check_interval', 3)
    director = drama_director.DramaDirector(
        model=model,
        beats=beats,
        eval_interval=self.params.get('eval_interval', 2),
        scoring_window=self.params.get('scoring_window', 8),
        tolerance=self.params.get('tolerance', 1),
        cooldown_turns=self.params.get('cooldown_turns', 3),
        feedback_enabled=self.params.get('feedback_enabled', False),
        allowed_environment_events=self.params.get(
            'allowed_environment_events', ()
        ),
        immutable_facts=self.params.get('immutable_facts', ()),
        player_names=player_names,
        character_constraints=self.params.get('character_constraints', {}),
        world_constraints=self.params.get('world_constraints', ()),
        action_mode=action_mode,
        repetition_push_enabled=repetition_push_enabled,
        repetition_check_interval=repetition_check_interval,
    )

    world_constraints = actor_components.constant.Constant(
        state='\n'.join(self.params.get('world_constraints', ())),
        pre_act_label=WORLD_CONSTRAINTS_PRE_ACT_LABEL,
    )

    # Stage-1 resolution chain: strip the putative-event prefix, then (in
    # speech_and_action mode) adjudicate the current character's own
    # action attempt through the core `attempt_to_result` helper, whose
    # true return value the later steps consume as resolution evidence;
    # weave in any pending director directive (identity when none is
    # pending); on an eligible turn run the repetition push through the
    # core `maybe_inject_narrative_push` helper on its own document
    # (identity when the director's gate is closed — the gate keeps a
    # turn to at most one intervention path); finally ask for the
    # single-field stage-directions output that respects the immutable
    # facts, with the verbatim speech entering the canonical event by
    # assembly. Turns without an action intent skip the adjudication
    # model call; the repetition step uses the same budget- and
    # retry-wrapped model as the rest of the chain.
    remove_prefix = thought_chains_lib.RemoveSpecificText(
        substring_to_remove='Putative event to resolve: '
    )
    # The adjudication is conditioned on the initial public facts and,
    # via the DisplayEvents pre-act value (bound here, never serialized
    # into params), the retrieved public history — isolation must not shrink
    # the history to the director's scoring window (design 4.1).
    adjudication_step = story_action_adjudication.make_action_adjudication_step(
        director,
        public_facts=self.params.get('public_facts', ()),
        public_history=display_events.get_pre_act_value,
        npc_response_rules=npc_response_rules,
    )
    director_note_step = director_note.make_director_note_step(director)
    repetition_push_step = repetition_push.make_repetition_push_step(
        director, model
    )
    final_output_step = story_resolution.make_final_output_step(
        director,
        immutable_facts=self.params.get('immutable_facts', ()),
        character_constraints=self.params.get('character_constraints', {}),
        player_names=player_names,
        world_constraints=self.params.get('world_constraints', ()),
        allowed_environment_events=self.params.get(
            'allowed_environment_events', ()
        ),
        npc_response_rules=npc_response_rules,
        public_facts=self.params.get('public_facts', ()),
        public_history=display_events.get_pre_act_value,
    )

    event_resolution_key = (
        gm_components.switch_act.DEFAULT_RESOLUTION_COMPONENT_KEY
    )
    event_resolution = gm_components.event_resolution.EventResolution(
        model=model,
        event_resolution_steps=(
            remove_prefix,
            adjudication_step,
            director_note_step,
            repetition_push_step,
            final_output_step,
        ),
        components=[
            display_events_key,
            drama_director_key,
            WORLD_CONSTRAINTS_COMPONENT_KEY,
        ],
    )

    terminator_key = gm_components.terminate.DEFAULT_TERMINATE_COMPONENT_KEY
    terminator = story_terminator.SceneOrStoryTerminator(
        scene_tracker_component_key=scene_tracker_key,
        drama_director_component_key=drama_director_key,
    )

    components_of_game_master = {
        terminator_key: terminator,
        instructions_key: instructions,
        player_characters_key: player_characters,
        observation_component_key: observation,
        observation_to_memory_key: observation_to_memory,
        display_events_key: display_events,
        WORLD_CONSTRAINTS_COMPONENT_KEY: world_constraints,
        drama_director_key: director,
        make_observation_key: make_observation,
        memory_component_key: memory,
        scene_tracker_key: scene_tracker,
        next_actor_key: next_actor,
        next_action_spec_key: next_action_spec,
        event_resolution_key: event_resolution,
    }
    components_of_game_master[send_events_to_players_key] = (
        send_events_to_players  # pyrefly: ignore[unsupported-operation]
    )

    component_order = list(components_of_game_master.keys())

    self.validate_extra_components(components_of_game_master)
    if extra_components:
      components_of_game_master.update(extra_components)
      if extra_components_index:
        for component_name in extra_components.keys():
          if component_name in component_order:
            component_order.remove(component_name)
          component_order.insert(
              extra_components_index[component_name],
              component_name,
          )
      else:
        component_order = list(components_of_game_master.keys())

    act_component = gm_components.switch_act.SwitchAct(
        model=model,
        entity_names=player_names,
        component_order=component_order,
    )

    game_master = entity_agent_with_logging.EntityAgentWithLogging(
        agent_name=name,
        act_component=act_component,
        context_components=components_of_game_master,
        measurements=self.params.get('measurements'),
    )

    return game_master
