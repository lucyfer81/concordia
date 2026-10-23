# Contrib Prefabs

This directory hosts community-contributed prefabs — ready-made `Prefab`
configurations that wire core and contrib components into complete entities.
It mirrors the core layout of `concordia/prefabs`: an `entity` prefab builds
one player/agent, a `game_master` prefab builds the adjudicating game
master. Contrib prefabs compose freely with the core component libraries.

**Rule:** prefabs in this installed package must never import from
`examples/`. Anything a prefab needs at build time (codecs, helpers) belongs
here; examples hold only scenario content and display configuration.

## Story Enactment Prefabs

These prefabs assemble the story-enactment components
([`concordia/contrib/components`](../components/README.md)) and are
demonstrated by
[`examples/story_enactment`](../../../examples/story_enactment/README.md).

- `game_master/story_dramaturgic.py` — a game master prefab that enacts a
  story through dramaturgic beats. It follows the scene-based assembly of the
  core `dialogic_and_dramaturgic` prefab and swaps in the story-enactment
  components: a `DramaDirector` (fed scene and beat dictionaries plus the
  scoring, feedback and repetition configuration), an optional
  action-adjudication step, a `director_note` fusion step, a `repetition_push`
  step (gated by the director's committed repetition state),
  single-field stage-directions `story_resolution`, and a
  `SceneOrStoryTerminator`. Resolutions are
  conditioned on the beat, the world constraints (game-master-only facts and
  secrets), and the immutable facts. GM-only `character_constraints` maps all
  player names to their hard constraints; `public_facts` distinguishes initial
  public background from hidden truth. The final output and its independent
  semantic review receive these constraints and the environment whitelist in
  every mode. The `npc_response_rules` parameter — validated at build time
  as a sequence of non-empty strings, with an empty default that preserves
  the old authorization boundary — forwards the story's public-NPC
  response authorization to both the action-adjudication and the
  final-output factories. Inside `build`, the DisplayEvents pre-act value
  (`display_events.get_pre_act_value`) is bound as the public-history
  getter of both the action-adjudication and final-output steps, so the
  adjudication's isolation from the upstream chain does not shrink the
  history to the director's scoring window; no new serializable
  configuration field is added (getters never enter `params`). Players
  receive only their own private brief and public
  events. Failed-turn resolution diagnostics (intervention candidates,
  traces, review rejections) stay in the director's transient state and
  the example's private per-invocation process summaries — never in
  player observations or script exports.
- `entity/story_character.py` — a story character prefab built on the basic
  three-question actor: it delegates to the core `basic.Entity` prefab via
  `extra_components`, layering a `CharacterBrief` constant of the character's
  public data and, when configured, a `PrivateSecret` constant visible only
  to this character. Character-depth params add an `EmotionalStance`
  commitment and a cached `RelationshipsToOthers` view. All components are
  created inside `build`, so the prefab's params only ever hold JSON-safe
  values — which is what makes run configs checkpointable.
- `game_master/story_scene.py` — a shared codec mapping `SceneSpec` objects
  to and from plain dictionaries (including action specs and per-participant
  premises). It lives in contrib (not in the example) so the installed
  prefabs never depend on `examples/`; the example only stores scene
  dictionaries in its run config and decodes them at build time.

## Catalog

Other contributed prefabs (see each module's docstring for details):

| Module | Summary |
|--------|---------|
| `entity/basic_with_forum_browser.py` | Basic entity with forum browsing. |
| `entity/basic_with_image.py` | Entity generating text and image outputs. |
| `entity/conversations_with_ai_companions.py` | Entity prefabs for the AI-companion project. |
| `game_master/async_social_media_with_moderation.py` | Asynchronous social media with moderation. |
| `game_master/dial_dyad_initializer.py` | Initializer setting dyad memories. |
| `game_master/persona_initializer.py` | The persona-generation initializer. |
| `game_master/simultaneous_resolution_gm.py` | Simultaneous-resolution game master. |
| `game_master/space_ship.py` | Spaceship game master. |
| `game_master/tool_use_gm.py` | Game master enabling tool use via MCP. |
