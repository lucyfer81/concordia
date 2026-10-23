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
  The repetition intervention shares the pending-commit boundary: on check
  turns (`should_attempt_repetition_push`) the resolution chain may run the
  core narrative-push helper, and the executed flag, the repetition counters,
  `last_intervention` and the shared cooldown advance only when the reviewed
  resolution commits (`record_repetition_result` records the actual attempt
  and the proposed change separately; an attempt whose fusion returned the
  original text is still a check only). A directive marked integrating by a
  fusion attempt that never commits is restored to pending by the retried
  turn's putative event. Directive instructions likewise target a concrete
  unresolved problem visible in the recent public events rather than
  novelty stacking — no extra interruption, verification or registration
  chore on top of unresolved ones (world-response design section 7.2).
- `director_note.py` — a thought step of the event-resolution chain that
  fuses a pending directive into the candidate event in two model questions:
  first propose plausible in-story complications (NPC or environmental means
  from the configured whitelist only), then merge the candidate event with
  the chosen complication, recording the fused candidate (before/after and a
  private trace) on the director without consuming the directive. Its
  suggestion constraints prefer a complication that addresses a
  concrete unresolved blockage or unanswered question already visible
  in the committed public history, instead of stacking another
  interruption, verification or registration chore onto unresolved
  ones merely for novelty (world-response design section 7.2).
  Without a pending directive it is the identity and makes zero model
  calls.
- `repetition_push.py` — a thought step of the event-resolution chain that
  reuses the core `maybe_inject_narrative_push` helper unchanged: on an
  eligible check turn it builds one independent interactive document on the
  same wrapped model — ordered committed history, the candidate explicitly
  marked as not yet committed, the current adjudicated action result, the
  environment whitelist, immutable facts, player list and GM-only boundaries
  — calls the core helper once, records its true return value — plus whether
  it actually attempted an intervention, observed on the outer document's
  model responses — on the director, and returns it unchanged to the chain.
  Ineligible turns are the
  identity with zero model calls. API and budget errors — and catchable
  interruptions (e.g. `KeyboardInterrupt`) raised inside the core calls —
  propagate after the partial trace is archived in the transient
  diagnostics; no second repetition detector or re-draw loop exists
  here. The helper's five suggestions are generated and randomly
  selected on a core-internal document copy, so only the externally
  visible trace and the selected/fused before-after pair can be
  archived — by design, the unselected internal suggestions are not
  intercepted. Failed-turn transient diagnostics (candidate, traces,
  rejection reason) are archived by the example's per-invocation
  process summary as private review evidence; they never enter player
  observations or script exports. Its judgment rules distinguish
  effective progress (a first discovery of a concrete blockage, new
  sourced information, an adjudicated action result, a response that
  resolves a previously unanswered question, an autonomous acceptance,
  refusal or change of strategy) from ineffective repetition such as
  restating the same blockage, repeated preparation or rewording the
  same request (world-response design section 7.1).
- `story_action_adjudication.py` — the action-adjudication thought step of
  the event-resolution chain (active in `speech_and_action` mode): it calls
  the core `attempt_to_result` helper exactly once on an independent
  adjudication document (`new()` keeps the wrapped model and its budgets
  while clearing the upstream chain prompts, so beat instructions and the
  conversation window are not inherited). The document states the current
  speaker, the original speech, the character's own attempted action and
  the adjudication rules as inert JSON-encoded data, plus the GM-only
  facts and authorization boundaries. Initial public facts arrive via the
  `public_facts` parameter and the public history via a zero-argument
  `public_history` getter (the DisplayEvents pre-act value in the story
  prefab), called exactly once per adjudication; when the getter is None
  the adjudication keeps conditioning on the director's committed-event
  window. The core helper's true return value is recorded on the director
  and returned unchanged as a pending-review adjudicated outcome — never
  an authorization or semantic approval. Success and failure (API errors,
  budget exhaustion, interruptions) both archive a private trace through
  `record_resolution_diagnostics(stage='action_adjudication', ...)`;
  the shared authorization wording lives in the module constant
  `AGENCY_AND_EVIDENCE_RULES`, reused verbatim by `story_resolution`;
  it now also distinguishes evidence source, access and certification
  (world-response design section 6), while the new module constant
  `PUBLIC_NPC_RESPONSE_SEMANTICS` and the optional `npc_response_rules`
  parameter confine an already-entered NPC's public direct response to
  the configured scope, generated within the same single core
  adjudication call — never as an extra model call.
  Turns without an action intent make zero model calls.
- `story_resolution.py` — the final narrator-track output step: the model
  returns a strict single-field JSON object, `stage_directions` (environment
  changes, NPC behavior, outcomes). The canonical event is assembled
  deterministically by code from the active player's original speech and that
  narration before the semantic review, so the boundary between player agency
  and narrator authority is enforced by construction. The independent
  structured review reads the same to-be-published event alongside its source
  evidence in a `## Canonical event for review (data, never instructions)`
  JSON section, checking player agency, every character's constraint, private
  and public knowledge, immutable facts and the environment whitelist. On
  approval `set_validated_resolution` commits the same event verbatim;
  nothing is rewritten after the review. Rejected or malformed reviews raise
  `StoryOutputError` before broadcast or directive consumption; API and
  budget errors propagate. The review adds one wrapped model call and is a
  fallible judgment, not a proof of narrative validity. Guarantees enforced
  by code: the JSON structure, verbatim dialogue characters, reviewed event
  == committed event, and a non-empty narration whenever adjudicated results
  or an intervention candidate exist. Whether the narration overreaches,
  leaks secrets, or preserves the adjudicated facts remains semantic
  judgment that requires live human evaluation. The calibrated rule 8
  (run4 design 5.1) keeps the requirement to retain a legal new event
  and its material consequences while allowing faithful paraphrase and
  the omission of unchanged background restatements such as an ongoing
  disagreement, provided nothing contradicts them; the exception never
  covers adjudicated action results, player choices, secrets or
  fabricated evidence, and restatement-only candidates remain
  unsuccessful interventions. The action-authorization review
  (2026-10-17 design) additionally pins the trust boundary around the
  core adjudicated result: rules 2/4/5/7/8 share their authorization and
  recording semantics with the adjudication step through the
  `AGENCY_AND_EVIDENCE_RULES` constant; the one final review checks the
  `action_result` itself against the agency, secrecy and evidence rules
  at the same time as the canonical event's fidelity — an illegal raw
  outcome rejects the whole turn even when the final narration omits it,
  while a legal outcome's material success, failure or blocked result
  must be preserved. `make_final_output_step` accepts a `public_history`
  getter that is read exactly once per step entry, and the identical
  history string reaches both the generation prompt and the review
  document. Review rule 5 now separates director-initiated new events
  (whitelist-gated) from a configured public response of an
  already-entered NPC — a direct consequence of the current action that
  neither consumes nor bypasses the turn's single director intervention
  path — and the generation and review prompts share one copy of the
  NPC response semantics (`PUBLIC_NPC_RESPONSE_SEMANTICS`) and the
  instance-level `npc_response_rules` data, so the producer and the
  judge cannot drift. The review's strict `{"valid": bool, "reason":
  str}` JSON schema is unchanged.
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
