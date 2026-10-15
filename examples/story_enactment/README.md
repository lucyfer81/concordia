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

Each resolution receives one independent semantic review before it can enter
memory, player observations, or a checkpoint. The review checks player agency,
character constraints, private information, established facts, environmental
permissions, and agreement between the two output tracks. A rejected or
malformed review stops the run with `error`; it does not rewrite the story or
retry generation. This adds one budgeted model request per resolved turn.
Review is a fallible model judgment and still requires live human acceptance.
After approval, code constructs the canonical event from the original speech
and the exact `stage_directions` used for export, giving both consumers the
same narrator facts.

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
The stub approves semantic reviews by construction: it exercises the review
boundary, rather than demonstrating reliable detection of semantic violations.

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
Each invocation appends its current source snapshot to `run_manifest.json`;
earlier snapshots are retained. Resuming an older run without a manifest sets
`history_complete` to `false`, because its original prompt sources cannot be
reconstructed from the checkpoint. Old committed events are retained as saved;
the new review does not retroactively certify them.

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
  state, `committed_turn_id`, `used_model_attempts`, `used_api_retries`,
  both total limits.
- `run_manifest.json` — invocation history with effective configuration,
  related source and prompt snapshots, per-file SHA-256 hashes, a combined
  `source_version`, and the starting committed turn. Credential fields are
  excluded. Keep this file together with checkpoint, JSONL, summary and scripts
  when archiving a live review; story secrets remain in the private archive.
- `summary.json` — final reason (priority: `error > beats_completed >
  `beat_timeout > scene_limit > step_limit > budget_limit`), committed turn
  count, attempts used, API retries (`used_api_retries` cumulative across
  resumes, `api_retries_used_this_process` for this process only; blocked
  requests and output-parse re-asks never count as API retries — parse
  re-asks of the tension-scoring question are reported separately as
  `used_scoring_parse_retries` / `scoring_parse_retries_used_this_process`),
  wall time, per-step durations.
- `script.fdx` — exact-text screenplay interchange (Final Draft XML).
  Explicit Scene Heading, Character, Parenthetical, Dialogue and Action
  paragraph types keep literal parentheses, backslashes, markup, whitespace
  and line breaks in dialogue unchanged. XML reserved characters are escaped
  only for serialization and restored on import. Use this export when
  reviewing or importing dialogue verbatim.
- `script.fountain` — compatibility screenplay (forced `@` cues for CJK names,
  parenthetical emotion, action lines from `stage_directions`; blank-line
  paragraph breaks inside one speech render as line breaks so the speech
  stays a single dialogue block; dialogue lines that would begin with a
  Fountain forcing marker (`!`, `@`, `(`, …) carry a backslash escape.
  Some importers, including Screenplain, display that backslash; Fountain
  is not the exact-text interchange format. Stage directions that begin
  like scene headings or cues are forced to action with `!`).
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
| `director_diagnostics` | Archive-only directive snapshots at action start, after resolution, and after turn processing, plus cooldown and last execution turn. Absent in older records; never broadcast to characters. |

## Human Acceptance Workflow

Spec section 13.2 defines a manual review with live models;
`acceptance.py` hosts its three fixed scenarios (appendix C of the plan):

1. 老宅遗嘱 — the default estate-dispute story (same as `story_config`).
2. 终面疑云 — a fraud-tainted final interview.
3. 婚礼上的旧歌 — a wedding shadowed by an old flame.

Run each scenario three times with a live model, constructing the run
config from the scenario **together with its matching beat outline**
(each scenario in `acceptance.py` exposes a beat factory whose closing
beat is derived from that scenario's allowed endings):

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
        acceptance.make_scenario_two_config(),
        beats=acceptance.make_scenario_two_beats(),
        mode='director',
    ),
)
```

A full 3×3 review batch (nine state dirs, one per scenario and run;
every dir keeps its own config, beats, model identity, summary and
script exports for archival):

```python
import pathlib
from examples.story_enactment import acceptance, run as run_lib

SCENARIOS = (
    ('s1', acceptance.make_scenario_one_config, acceptance.make_scenario_one_beats),
    ('s2', acceptance.make_scenario_two_config, acceptance.make_scenario_two_beats),
    ('s3', acceptance.make_scenario_three_config, acceptance.make_scenario_three_beats),
)
model_config = {'api_type': 'gemini', 'model_name': ..., 'api_key': ...}
for prefix, make_config, make_beats in SCENARIOS:
    for run_index in (1, 2, 3):
        run_lib.run(
            str(pathlib.Path('/tmp/story-review') / f'{prefix}-run{run_index}'),
            mode='director',
            model_config=model_config,
            run_config=run_lib.make_run_config(
                make_config(), beats=make_beats(), mode='director'
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

The default estate story preserves 陈母's constraint against admitting that
she touched the will. Its compromise ending permits an arrangement to protect
the house without requiring confession. Beat judgments require public events
and each required part of an ending to be established; requests to seal
evidence, investigate, agree, or leave are not proof that these outcomes
occurred. A story that fails to reach an ending still reports `beat_timeout`.
Dialogue prompts ask characters to respond to new information through their
own goals and styles; tension scoring distinguishes repeated shouting from a
new risk. These prompt changes require human comparison of new live runs.

## File Structure

```
examples/story_enactment/
├── run.py             # Entry point: run, resume, budgets
├── collector.py       # StoryTurn JSONL + checkpoint save boundary
├── provenance.py      # Configuration and source snapshots per invocation
├── renderer.py        # Fountain / plain-text / tension-curve rendering
├── story_config.py    # Story data model and the default example story
├── acceptance.py      # Appendix-C scenarios and the review checklist
├── testing_stubs.py   # Deterministic PromptStubModel
└── *_test.py          # CI tests (no API keys needed)
```
