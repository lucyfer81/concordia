# Story Enactment Example

This example enacts an improvised multi-character drama. A cast of characters
— each with public facts, a private goal, and a secret only they (and the game
master) know — plays out a conflict start toward one of the allowed endings.
A dramaturgic game master advances the story through narrative beats, scores
the dramatic tension of recent events, and may issue director directives that
enter the story only through whitelisted NPC or environmental events. The
game master never decides voluntary actions for player characters: the acting
player's speech is preserved verbatim in the canonical event, while
everything the narrator adds lives on a separate stage-directions track.

The example implements the design in
[docs/superpowers/specs/2026-10-13-story-enactment-phase1-design.md](../../docs/superpowers/specs/2026-10-13-story-enactment-phase1-design.md).

## Quick Start

### Testing (no LLM required)

```bash
python -m examples.story_enactment.run \
  --state_dir /tmp/story-demo \
  --disable_language_model \
  --mode beats_only
```

The deterministic `PromptStubModel` (`testing_stubs.py`) exercises the full
plumbing — beats, resolution, checkpointing, exports — without any API key.
Two stub limitations are expected: no beat ever completes (runs end in
`beat_timeout`), and the stub does not answer the tension-scoring question,
so `tension` stays `null` in `tension_observe` runs.

### Using a live model

```bash
python -m examples.story_enactment.run \
  --state_dir /tmp/story-demo \
  --api_type=openai \
  --model_name=gpt-4o \
  --api_key=$OPENAI_API_KEY \
  --mode director
```

## Stages and Modes

The `--mode` flag selects the implementation stage (spec section 14). Each
stage reuses the same example story and review checklist:

| Mode | Stage | What it adds |
|------|-------|--------------|
| `beats_only` | 1. 契约先行 (contract first) | Fixed cast and information allocation, beat advancement, dual-track story-turn output, screenplay export, resume. Scoring disabled. |
| `tension_observe` | 2. 张力观察 (tension observation) | Tension is scored every `eval_interval` turns and recorded only; no feedback. Compare scores with human judgment before enabling the director. |
| `director` | 2. 张力观察 (tension observation) | Scoring plus feedback: a deviating score generates a directive that is fused into the next resolution through the story's whitelisted environment events (with a cooldown). |
| `character_depth` | 3. 角色深度 (character depth) | The director setup plus per-character emotion (`EmotionalStance`) and cached relationship perception (`RelationshipsToOthers`); the acting character's emotion is collected per turn. |

## Resuming

A run can be interrupted and continued; the checkpoint is the commit
boundary, so a resumed run never duplicates or loses committed turns:

```bash
python -m examples.story_enactment.run \
  --state_dir /tmp/story-demo \
  --disable_language_model \
  --mode beats_only \
  --resume
```

The resumed run rebuilds the simulation from the configuration recorded in
the checkpoint. Explicitly passing limits or a mode that differ from the
saved ones is rejected, and a budget-exhausted run is never auto-resumed.

## Budgets

| Flag | Default | Meaning |
|------|---------|---------|
| `--total_turn_limit` | 24 | Total story-turn budget. |
| `--total_model_attempt_limit` | 400 | Total model-attempt budget (one unit per `sample_text`/`sample_choice` call attempt, including retries). |

Exhaustion before a resolution commits stops the run with reason
`budget_limit` and leaves the previous checkpoint in place. Exhaustion after
the resolution (during completion judgment or tension scoring) still commits
that step exactly once, then stops. Both paths are recorded in
`summary.json` (`reason`, `budget_exhausted`, `budget_error`).

## Output

All files are written into `--state_dir`:

- `story_turns.jsonl` — the committed story turns (schema below).
- `checkpoint.json` — checkpoint envelope: run config, saved component
  state, `committed_turn_id`, `used_model_attempts`, both total limits.
- `summary.json` — final reason (priority: `error > beats_completed >
  beat_timeout > scene_limit > step_limit > budget_limit`), committed turn
  count, attempts used, wall time, per-step durations.
- `script.fountain` — Fountain screenplay (forced `@` cues for CJK names,
  parenthetical emotion, action lines from `stage_directions`).
- `script.txt` — plain-text transcript of the same turns.

Only turns confirmed by the latest checkpoint are exported. The tension
curve is rendered separately:

```python
import json
from examples.story_enactment import renderer

turns = [json.loads(line) for line in open('/tmp/story-demo/story_turns.jsonl') if line.strip()]
renderer.render_tension_curve(turns, '/tmp/story-demo/tension_curve.png')
```

### StoryTurn record

Each line of `story_turns.jsonl` holds one committed story turn:

| Field | Meaning |
|-------|---------|
| `turn_id` | Global story-turn number, starting at 1 (survives resume). |
| `scene_id` | Scene type name of the enacting scene. |
| `actor` | Name of the acting player character. |
| `speech` | The actor's authored dialogue, verbatim. |
| `resolved_event` | Canonical event, preserving the speech. |
| `stage_directions` | Narrator track: environment changes, NPC behavior, outcomes. |
| `emotion` | The actor's emotion label (`character_depth` only, else `null`). |
| `beat_id_at_action` | Beat current when the actor acted. |
| `beat_id_after_event` | Beat current after resolution (`null` once all beats completed). |
| `tension` | Tension score 1-10 on scored turns, else `null`. |
| `budget_stopped` | Diagnostic: the budget ended this turn's scoring. |

## Human Acceptance Workflow

Spec section 13.2 defines a manual review with live models;
`acceptance.py` hosts its three fixed scenarios (appendix C of the plan):

1. 老宅遗嘱 — the default estate-dispute story (same as `story_config`).
2. 终面疑云 — a fraud-tainted final interview.
3. 婚礼上的旧歌 — a wedding shadowed by an old flame.

Run each scenario three times with a live model, e.g.:

```python
from examples.story_enactment import acceptance, run as run_lib

result = run_lib.run(
    '/tmp/story-s2-run1',
    mode='director',
    model_config={
        'api_type': 'openai',
        'model_name': 'gpt-4o',
        'api_key': ...,
    },
    run_config=run_lib.make_run_config(
        acceptance.make_scenario_two_config(), mode='director'
    ),
)
```

Each run is reviewed per dimension, annotating findings with the `turn_id`
they concern:

```bash
python -c "
from examples.story_enactment import acceptance
acceptance.print_review_checklist('终面疑云')
"
```

| Dimension | Question |
|-----------|----------|
| 角色区分 | Can the speaker be blind-guessed from the line alone? |
| 因果连续 | Are events referenced later rather than forgotten? |
| 信息泄漏 | Does a secret leak to characters who should not know it? |
| 对白重复 | Do adjacent turns repeat themselves? |
| 导演越权 | Does `resolved_event` act for a player character? |
| 结尾状态 | Does the termination reason match the story? |

Pass thresholds are fixed by the user before release; beat progression and
tension-in-range never substitute for these checks.

## File Structure

```
examples/story_enactment/
├── run.py             # Entry point: run, resume, budgets
├── collector.py       # StoryTurn JSONL + checkpoint save boundary
├── renderer.py        # Fountain / plain-text / tension-curve rendering
├── story_config.py    # Story data model and the default example story
├── acceptance.py      # Appendix-C scenarios and the review checklist
├── testing_stubs.py   # Deterministic PromptStubModel
└── *_test.py          # CI tests (no API keys needed)
```
