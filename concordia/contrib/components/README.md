# Contrib Components

This directory hosts community-contributed components that extend the core
component library in
[`concordia/components`](../../components/README.md). It mirrors the core
layout — `agent` components shape an entity's reasoning, `game_master`
components shape the simulation's adjudication — and follows the same
`EntityComponent` lifecycle contracts, so contrib components compose with
core ones. Unlike the core library, components here may target niche or
research-specific setups and often come in cooperating groups (for example,
the story-enactment components below).

## Story Enactment Components

These components implement generative story enactment: a dramaturgic game
master advances a multi-character drama through narrative beats while
preserving player agency. They are wired together by the prefabs in
[`concordia/contrib/prefabs`](../prefabs/README.md) and demonstrated by
[`examples/story_enactment`](../../../examples/story_enactment/README.md).

### Game master components (`game_master/`)

- `drama_director.py` — `DramaDirector` structures the story as a sequence of
  beats (Freytag's pyramid). It counts story turns (one player proposal plus
  its validated resolution), judges beat completion once its minimum turn
  count is reached, scores dramatic tension on an anchored 1-10 scale every
  `eval_interval` turns, and — when feedback is enabled — generates director
  directives when the score deviates from the beat's target range. A
  pending-commit state machine guarantees exactly one commit per story turn
  even when `update` calls interleave. Exposes `get_state`/`set_state` for
  checkpointing and `set_validated_resolution` for the resolution chain.
  `get_committed_events()` exposes a read-only public event window for the
  resolution review. Per-turn `director_diagnostics` records directive
  generation, execution, invalidation and cooldown in checkpoints and exports
  without adding them to player observations. Tension anchors distinguish
  repeated arguments from consequential new risk; completion must be supported
  by established public events, rather than proposals or assumed consent.
- `director_note.py` — a thought step of the event-resolution chain that
  fuses a pending directive into the candidate event in two model questions:
  first propose plausible in-story complications (NPC or environmental means
  from the configured whitelist only), then merge the candidate event with
  the chosen complication. Without a pending directive it is the identity and
  makes zero model calls.
- `story_resolution.py` — the final dual-track output step: the model returns
  a JSON object with `resolved_event` (preserving the active player's speech
  verbatim) and `stage_directions` (environment changes, NPC behavior,
  outcomes). A separate structured semantic review checks both tracks against
  player agency, every character's constraint, private and public knowledge,
  immutable facts and the environment whitelist. Rejected or malformed reviews
  raise `StoryOutputError` before broadcast or directive consumption; API and
  budget errors propagate. The review adds one wrapped model call and is a
  fallible judgment, not a proof of narrative validity. After approval, the
  canonical event is assembled from original speech and the exact narrator
  track used by screenplay export, then stored in `DramaDirector`.
- `story_terminator.py` — `SceneOrStoryTerminator` centralizes termination:
  scene schedule exhausted, all beats completed, a beat timed out, or the
  model-attempt budget spent. The module-level `resolve_final_reason`
  combines the run loop's reason with the component's reason under a fixed
  priority (`error > beats_completed > beat_timeout > scene_limit >
  step_limit > budget_limit`), using the committed story-turn count that
  survives resumes rather than engine step numbers.
- `observation_queue.py` — a boundary-preserving encoding for the game
  master's observation queue. The core `MakeObservation` joins queued items
  with a triple-newline separator, which makes an event that itself contains
  a triple newline indistinguishable from two items. `BoundaryEncodingMakeObservation`
  (a `MakeObservation` subclass used by the story prefabs) encodes every
  queued item so it contains no triple newline — runs of three or more
  newlines become a paragraph-separator escape plus their length and a
  colon terminator (so digits opening the next paragraph can never merge
  into the length), literal escapes are doubled — and `decode_observation`
  restores the exact items and content on the agent side. Ordinary items
  pass through unchanged.

Dependencies: `director_note` and `story_resolution` read the pending
directive and store the validated pair through `DramaDirector`'s public
accessors; `story_terminator` reads the `DramaDirector`'s stop state and is
called by the core `SceneTracker` when the scene schedule runs out. None of
them imports from `examples/`.

### Agent components (`agent/`)

- `relationships.py` — `RelationshipsToOthers` derives the character's view
  of the other participants (warmth, trust, most recent interaction) only
  from adjudicated events the character actually received. The view is cached
  and refreshed with a single model call once `refresh_interval` new
  adjudicated events have been observed, mirroring the bounded updating of
  social attitudes. Recognizes adjudicated events by the reserved `Event: `
  prefix after decoding the observation with
  `game_master/observation_queue.py`, so multi-paragraph events are stored
  whole and premises and ordinary observations never inflate the cursor.

## Catalog

Other contributed components (see each module's docstring for details):

| Module | Summary |
|--------|---------|
| `agent/adhd_topic_drift.py` | Models human user topic drift. |
| `agent/choice_of_component.py` | Selects a component and calls `get_pre_act_value` on it. |
| `agent/emotional_stance.py` | Emotion-commitment component for reasoning pipelines. |
| `agent/situation_representation_via_narrative.py` | Represents the current situation via narrative. |
| `game_master/controlled_profile_presentation.py` | Controlled entity profile presentation in experiments. |
| `game_master/day_in_the_life_initializer.py` | Day-in-the-life initializer for dyadic simulations. |
| `game_master/death.py` | Spaceship-crew death state. |
| `game_master/forum_browser.py` | Pull-based forum access via tool calling. |
| `game_master/forum.py` | Thread-safe Reddit-like discussion board. |
| `game_master/gm_working_memory.py` | Working-memory narrative of the simulation. |
| `game_master/location_based_filter.py` | Location-based partial observability. |
| `game_master/marketplace.py` | Agents, goods, and orders for market simulations. |
| `game_master/mcp_tool_executor.py` | Executes MCP tools based on agent actions. |
| `game_master/narrative_event_resolution.py` | Resolves events via a narrative then individual observations. |
| `game_master/npc_event_generator.py` | Generates random NPC events. |
| `game_master/persona_generation.py` | Persona generation protocol and initializer. |
| `game_master/social_media_moderation_components.py` | Components for moderated social media simulations. |
| `game_master/spaceship_system.py` | Spaceship systems state. |
| `game_master/thread_safe_generative_clock.py` | Clock incrementing only after all players act. |
