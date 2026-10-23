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

In the final generation step the model writes narration only: a strict
single-field JSON response with `stage_directions`. The canonical event is
assembled by code from the acting player's original speech and that
narration before any review, so the review judges exactly the event that
will be published. Each resolution receives one such independent semantic
review before it can enter memory, player observations, or a checkpoint.
The review checks player agency, character constraints, private
information, established facts, and environmental permissions. A rejected
or malformed review stops the run with `error`; it does not rewrite the
story or retry generation, and after approval the reviewed event is
committed verbatim — nothing is rewritten past the review. This adds one
budgeted model request per resolved turn. Review is a fallible model
judgment and still requires live human acceptance. The screenplay exports
consume the same `stage_directions` the canonical event was assembled
from, giving both consumers the same narrator facts.

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
| `beats_only` | 1. 契约先行 (contract first) | Fixed cast and information allocation, beat advancement, story-turn output with single-field stage directions, screenplay export, resume. Scoring disabled. |
| `tension_observe` | 2. 张力观察 (tension observation) | Tension is scored every `eval_interval` turns and recorded only; no feedback. Compare scores with human judgment before enabling the director. |
| `director` | 2. 张力观察 (tension observation) | Scoring plus feedback: a deviating score generates a directive that is fused into the next resolution through the story's whitelisted environment events (with a cooldown). Also enables the repetition intervention: every `repetition_check_interval`-th turn the core narrative-push helper may add a whitelisted environmental event when the committed history repeats; a check that changes nothing records a check only. |
| `character_depth` | 3. 角色深度 (character depth) | The director setup plus per-character emotion (`EmotionalStance`) and cached relationship perception (`RelationshipsToOthers`); the acting character's emotion is collected per turn. |

Both director interventions (tension directives and repetition pushes) share one
intervention path per turn — a pending tension directive wins — and one
execution cooldown. A candidate is only a proposal: the executed flag, the
execution counters and the cooldown advance exclusively when the reviewed
resolution commits, so a rejected, interrupted or budget-exhausted turn
consumes nothing. The switches are recorded explicitly in
`director_params` (`repetition_push_enabled`, `repetition_check_interval`,
default `3` and required positive); configs from before the switches —
including schema-2 director checkpoints — resume with the repetition
intervention off regardless of the mode. The repetition helper normally adds
one choice call per check turn, plus a suggestion and a merge call when it
judges the story repetitive; all are billed as real model attempts.

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
Each invocation appends its current source snapshot to `run_manifest.json`
under a unique `invocation_id` and later publishes its own immutable
`process_summaries/<invocation_id>.json`; earlier snapshots and summaries
are retained byte-for-byte. Resuming an older run without a manifest sets
`history_complete` to `false`, because its original prompt sources cannot
be reconstructed from the checkpoint; legacy manifest entries that predate
invocation ids are kept as-is and never backfilled. Old committed events
are retained as saved; the new review does not retroactively certify them.

Checkpoints and committed history written by the earlier dual-field output
contract are read unchanged. The new generation path rejects the legacy
two-field model responses outright, so a resume from an old checkpoint
continues with the single-field `stage_directions` contract: a
cross-version resume (canonical-review design spec section 7.3), never an
exact replay of the old version.

Schema-2 run configs require an explicit top-level `action_mode` matching
the game master's effective mode. If `gm_params.action_mode` is omitted,
the prefab defaults to `speech_only`; a recorded `speech_and_action` mode
then fails validation before model construction or checkpoint writes.

## Action Modes

The `--action_mode` flag selects the characters' action contract (the
default stays `speech_only`): `speech_and_action` additionally elicits a
JSON object with `speech` and `action_intent`, and the game master
adjudicates each non-empty intent through the core `attempt_to_result`
helper before the final narrator track; success, failure and blocked
results are narrated and exported like any other committed fact. The
adjudication never decides another player's cooperation for them.

The core helper's return value is a pending-review adjudicated outcome,
never an authorization: adjudication runs on an isolated document (upstream
beat instructions and the conversation window are not inherited) that is
conditioned on the initial public facts and the full DisplayEvents public
history, and the single final semantic review checks the adjudicated result
itself (agency, secrecy and evidence boundaries) and the canonical event's
fidelity to it at the same time — an illegal raw outcome rejects the whole
turn even when the final narration omits it. Adjudication traces and review
rejections are archived privately in checkpoint/JSONL private diagnostic
fields and the per-invocation process summaries. The deterministic tests in
this example prove this wiring and its failure boundaries; they do not
prove the semantic quality of a real model's judgments.

For a controlled comparison of the two modes (run4 design section 4.2):
build a fresh state directory per model and mode with
`--mode director --action_mode speech_and_action`, keep the story, beats,
feedback parameters and budget limits fixed, and change only the action
mode. The action mode inherently changes prompts and possibly the number
of calls; record actual non-empty intents, core adjudications,
success/blocked results and outcomes honestly instead of claiming
identical prompts or consumption. Never switch the action mode when
resuming an existing state directory, and never retroactively authorize
actions from old speech-only dialogue.

### NPC responses and world events

Two separate channels govern figures beyond the players. The director
may initiate NEW external events (an unannounced caller, a courier, a
knock, a blackout) only from the story's `allowed_environment_events`
whitelist, at most one intervention path per turn. Separately, an NPC
that already entered the story through a legal committed event may
answer the current speaker's explicit interaction publicly and
directly, as a direct consequence of the current action, generated
within the same single core adjudication call — but only within the
story's `npc_response_rules` configuration (GM-only content
authorization recorded in the run config; empty by default keeps the
old boundary, and old configs resume with empty rules). Configured NPC
responses grant no GM-only knowledge, no evidence, no certification,
and never promise anything on a player's behalf. A grounded refusal or
limited response is allowed only under an applicable rule authorizing
that NPC's public response to the current interaction; the rule need
not prescribe its exact words. Empty rules, no applicable rule, or an
unmet trigger authorize no new NPC response through the action channel.

Known limitation: this pipeline has no per-recipient content. All
resolved events are public to every scene participant, so a private
phone call or whisper cannot receive its far-side reply here (design
section 5.3 and the section-11 gate). Private NPC conversations need a
separate audience-design before they can count as supported; the
public-neighbor acceptance below must not be reported as closing that
gap.

Unsupported private interaction is an engineering limitation, not an
in-story fact that the other side is silent, refuses, disconnects, or
fails identity verification. Do not invent those outcomes or force a
player to hang up or enable speakerphone to bypass the limitation.

## Budgets

| Flag | Default | Meaning |
|------|---------|---------|
| `--total_turn_limit` | 24 | Total story-turn budget. |
| `--total_model_attempt_limit` | 400 | Total model-attempt budget (one unit per `sample_text`/`sample_choice` call attempt, including retries). |

Exhaustion before a resolution commits stops the run with reason
`budget_limit` and leaves the previous checkpoint in place. Exhaustion after
the resolution (during completion judgment or tension scoring) still commits
that step exactly once, then stops. Both paths are recorded in
`summary.json` (`reason`, `budget_exhausted`, `budget_error`). Attempts
consumed by a turn that failed mid-flight are never given back: every
process records its final cumulative usage in `process_usage.json`, and a
resume restores the budget from the larger of that record and the
checkpoint's count, so the original attempt limit holds in aggregate across
failed-and-resumed processes while the story checkpoint stays at the
committed boundary.

## Output

All files are written into `--state_dir`:

- `story_turns.jsonl` — the committed story turns (schema below).
- `checkpoint.json` — checkpoint envelope: run config, saved component
  state, `committed_turn_id`, `used_model_attempts`, `used_api_retries`,
  both total limits. This is the only story commit boundary.
- `process_usage.json` — sidecar record of the last process's final
  cumulative `used_model_attempts` / `used_api_retries`; a resume takes
  the larger of this record, the checkpoint's counts and the counters
  recorded in `process_summaries/` so attempts consumed by a failed
  turn are never replayed for free — even when the sidecar's own write
  failed but the summary survived. It is a latest cumulative snapshot,
  not a history: per-invocation evidence lives in `process_summaries/`.
  Undeterminable consumption refuses the resume rather than silently
  falling back to a stale checkpoint count: that covers a summary that
  cannot be parsed or holds out-of-range counters, and equally an
  invocation that declared a summary in the manifest whose file is
  missing (both records failed to write, or a kill skipped the
  finalization). Legacy manifest entries that predate summary
  declarations demand nothing and stay resumable from their checkpoint.
- `run_manifest.json` — invocation history with effective configuration,
  related source and prompt snapshots, per-file SHA-256 hashes, a combined
  `source_version`, the starting committed turn, and — per invocation —
  the unique `invocation_id` with the relative `summary_path` of its
  process summary. Credential fields are excluded. Keep this file together
  with checkpoint, JSONL, summary and scripts when archiving a live
  review; story secrets remain in the private archive.
- `process_summaries/<invocation_id>.json` — one immutable summary per
  invocation (see below); a duplicate write of the same id is rejected and
  never overwrites the existing bytes.
- `summary.json` — final reason (priority: `error > beats_completed >
  `beat_timeout > scene_limit > step_limit > budget_limit`), committed turn
  count, attempts used, API retries (`used_api_retries` cumulative across
  resumes, `api_retries_used_this_process` for this process only; blocked
  requests and output-parse re-asks never count as API retries — parse
  re-asks of the tension-scoring question are reported separately as
  `used_scoring_parse_retries` / `scoring_parse_retries_used_this_process`),
  wall time, per-step durations. It keeps the latest invocation's normal
  result semantics; it is not the failure-evidence archive.
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

Only turns confirmed by the latest checkpoint are exported; failed or
uncommitted candidates never reach the JSONL or any script export.

### Process summaries

Every invocation — first run, resume, zero-step resume, normal
completion, either budget path, `StoryOutputError`, API-retry
exhaustion, and catchable interruptions (`KeyboardInterrupt`,
`SimulatedInterruption`) — archives exactly one
`process_summaries/<invocation_id>.json` before `run()` returns or the
original exception propagates. The summary carries schema version 1,
the `invocation_id` / `resume` / `source_version` recorded in the
manifest, UTC start/finish timestamps, the monotonic
`process_wall_time_seconds` of **this invocation only** (never the
whole experiment's accumulated time), the final `reason` (`interrupted`
for catchable interruptions), the `exception` type and message when one
was caught, the committed turn id at start and end, the cumulative
model-attempt and API-retry counters at start and end with this
process's deltas, and the two original total budgets (which a resume
can never raise).

The `uncommitted_resolution` field is the failed turn's private
diagnostics, taken as a deep copy before any transient state is
cleared: the core-adjudicated action result, the intervention reason
with candidate before/after, the retained trace history (an
intervention candidate trace and a later review-rejection trace stay
side by side), and the final review's rejection reason or failure
stage. It is `null` when no uncommitted attempt left evidence.

Privacy and limits:

- The process summary is archive-only review evidence. Traces may
  contain GM-private facts; they never enter character observations,
  the observation queue, `summary.json`, or any script export, and a
  failed candidate never becomes a `StoryTurn`.
- The core narrative-push helper generates its five suggestions on an
  internal document copy and randomly selects one, so the archive holds
  the externally visible trace plus the selected/fused before-after
  pair — the unselected internal suggestions are not accessible without
  modifying core, which this example deliberately does not do.
- Credentials are scrubbed from the manifest and every summary — both
  credential dictionary fields and this process's actual credential
  values with no length exemption, so an exception message quoting even
  a short API key back is redacted (`[REDACTED]`); only the secret's
  own occurrences change, so committed dialogue and story facts stay
  byte-identical. Story secrets stay in the private archive; API keys
  never enter any archived file.
- `SIGKILL` or a power loss cannot guarantee the finalization code
  runs: such exits leave the last committed checkpoint and whatever the
  usage sidecar already held, and no summary is fabricated for them.

A complete archive for review therefore consists of
`run_manifest.json`, `checkpoint.json`, `story_turns.jsonl`,
`process_usage.json`, `process_summaries/`, `summary.json`,
`script.fdx`, `script.fountain` and `script.txt`. Files that a run has
not produced yet (e.g. no `summary.json` before the first finalization)
are simply absent rather than synthesized.

### Three statistics files, one boundary

The three usage-bearing files have distinct, non-interchangeable roles:
`checkpoint.json` is the story commit boundary and resets its counters
only at committed turns; `process_usage.json` is the latest cumulative
usage snapshot (including attempts a failed turn consumed); each
`process_summaries/<id>.json` is the immutable per-invocation record
whose deltas were computed as the final guard/retry-counter values
minus the resume start point (the largest of checkpoint, usage sidecar
and recorded summary counters). Per-process deltas are therefore never
derived from checkpoint counts alone, which would miss failed turns.


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
| `resolved_event` | Canonical event assembled by code from the speech and the stage directions (field name kept for comparability with older runs). |
| `stage_directions` | Narrator track authored by the model: environment changes, NPC behavior, outcomes. |
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

## Fixed-Model Validation Probe

`fixed_model_probe.py` runs the frozen-protocol live-model validation of
the 2026-10-17 fixed-model plan (review group R: F01–F16, 16 × 2 × 3 =
96 real semantic reviews; adjudication group A: A01–A04, 4 × 2 × 3 = 24
real core adjudications). Every probed call drives the production
thought-step factories on deterministic director fixtures; the group-R
generation slot is answered synthetically and never reaches the API.
Offline rehearsal (`--stage=dry_run`) captures the actual prompts with
zero real client constructions; `freeze` writes protocol, fixtures and
source snapshots into a fresh batch directory; `run_r` / `run_a` execute
only unstarted planned entries (no retries, 120-call cap);
`summarize` classifies group R with the production review semantics and
collects group A for human labeling. Tests in `fixed_model_probe_test.py`
run fully offline.

```
python -m examples.story_enactment.fixed_model_probe --stage=dry_run \
    --dry_run_dir=/tmp/...dry_run
python -m examples.story_enactment.fixed_model_probe --stage=freeze \
    --batch_dir=run_results/story_enactment/action_authorization_fixed_probe/<UTC时间戳>
python -m examples.story_enactment.fixed_model_probe --stage=run_r --batch_dir=<同上>
python -m examples.story_enactment.fixed_model_probe --stage=run_a --batch_dir=<同上>
python -m examples.story_enactment.fixed_model_probe --stage=summarize --batch_dir=<同上>
```

## Three-Model Review Probe

`three_model_review_probe.py` runs the frozen-protocol three-model
review-reliability comparison (2026-10-17 protocol): group R only, 16
cases × 3 models (`m1`/`m2`/`m4` from `AI_MODEL_1`/`AI_MODEL_2`/
`AI_MODEL_4`; `AI_MODEL_3` is never read) × 3 repetitions = 144 real
semantic reviews under a 144-call cap, executed in repetition → case
→ m1/m2/m4 order. It reuses the production final-output path and the
wrappers of `fixed_model_probe` (the original two-model R/A
configuration stays untouched), freezes a calibrated F10 (history
event 1 is an explicit committed canonical event; the F10 narration is
a pure restatement of that unchanged state), archives requested vs
provider-returned model identifiers and usage through a read-only
client observation proxy, and summarizes with separated
planned/started/completed/not_started/started_no_result accounting
plus the manual reason review of all 144 reasons in the round gate.
Tests in `three_model_review_probe_test.py` run fully offline.

```
python -m examples.story_enactment.three_model_review_probe --stage=dry_run \
    --dry_run_dir=/tmp/...dry_run
python -m examples.story_enactment.three_model_review_probe --stage=freeze \
    --batch_dir=run_results/story_enactment/action_authorization_three_model_review/<UTC时间戳>
python -m examples.story_enactment.three_model_review_probe --stage=run_r --batch_dir=<同上>
python -m examples.story_enactment.three_model_review_probe --stage=summarize --batch_dir=<同上>
```

## Adjudication-Chain Probe

`adjudication_chain_probe.py` runs the frozen-protocol two-model
chain-fidelity validation (2026-10-18 protocol) over the two models that
passed the three-model review batch (`m1` Gemini / `m4` Sol) under the
public history calibrated by that batch: stage A re-runs the four frozen
adjudication inputs (4 × 2 × 3 = 24 real core calls); stage FINAL injects
each archived `action_result` as-is into the production
`make_final_output_step` of the same model for one real generation plus
one real review (≤ 48 calls; a malformed generation stops at the
production path with no compensating review); stage E replays four frozen
evidence injections E01–E04 (≤ 48 calls; E03 is the archived batch-2
English-self-talk sample byte-for-byte, label frozen after engineer
confirmation) through the same real path, recorded separately from the
A-group outputs. Per-stage caps 24/48/48 sum to the 120-call global cap;
an observation proxy archives the exact SDK call-boundary kwargs per
request (request-level timeout 60s; top_p/top_k are not sent). Stages:
`dry_run`, `freeze`, `run_a`, `run_final`, `run_e`, `worksheets`
(anonymized human-label worksheets), `summarize` (label-derived machine
classification of stage FINAL plus all three stage gates). Tests in
`adjudication_chain_probe_test.py` run fully offline.

The `summarize` stage is strict: it re-verifies every snapshotted source
hash, so it cannot process a batch after the summary tool itself changes.
For that case `resummarize_offline` re-runs the same fixed summary logic
over a frozen batch without credentials, clients, or stage execution: it
verifies a pre-work input-file manifest, allows source diffs only for
the approved tool files (production files must stay byte-identical to
the batch's experiment snapshot), and writes a provenance-annotated
corrected report (`offline_resummarize` block with
`new_real_api_calls=0`) into a new output directory. The original
`report.json` always keeps the experiment-time summary; the two report
versions must not be conflated. The resolved output path must be
outside the source batch, including when a symlink points into it;
invalid output paths are rejected before any files are written.

```
python -m examples.story_enactment.adjudication_chain_probe --stage=dry_run \
    --dry_run_dir=/tmp/...dry_run
python -m examples.story_enactment.adjudication_chain_probe --stage=freeze \
    --batch_dir=run_results/story_enactment/action_authorization_chain_probe/<UTC时间戳>
python -m examples.story_enactment.adjudication_chain_probe --stage=run_a --batch_dir=<同上>
python -m examples.story_enactment.adjudication_chain_probe --stage=run_final --batch_dir=<同上>
python -m examples.story_enactment.adjudication_chain_probe --stage=run_e --batch_dir=<同上>
python -m examples.story_enactment.adjudication_chain_probe --stage=summarize --batch_dir=<同上>
python -m examples.story_enactment.adjudication_chain_probe \
    --stage=resummarize_offline --batch_dir=<已冻结批次> \
    --output_dir=<新目录，不得已存在> \
    --input_manifest=<开工前保存的批次输入哈希清单>
```

## World-Response Probe Closeout (S1)

`world_response_probe_closeout.py` closes out the frozen
20261020T075501Z world-response batch offline: zero real calls (no
credentials, no `.env` read) and strictly read-only on the source
batch — a sha256 manifest of the whole batch is verified around the
semantic stage, so the source batch directory must remain untouched.
Counts come from the request ledger's unique started slots, never from
the old `report.json`, and each attempt is re-reported in three
layers: execution status (`completed` / `review_rejected` /
`format_error` / `started_no_result`), frozen-protocol match (frozen
labels never rewritten) and the semantic-fixture annotation (design
§2.2 REV01/02 fixture defect). It writes four files —
`input_manifest.json`, `corrected_report.json`, `corrections.json`,
`README.md` — into a fresh output directory disjoint from the source
batch even through symlinks; missing inputs fail explicitly.
Historical pending and second-request-blocked slots may lack archived
prompts: closeout lists each such evidence gap as a warning without
recreating the prompt, refunding requests or changing the failed verdict.
This exception is confined to offline closeout; paid resume and S2
summarization require the saved prompts and matching hashes.

```
python -m examples.story_enactment.world_response_probe_closeout \
    --source_batch_dir=run_results/story_enactment/world_response_fixed_probe/20261020T075501Z \
    --output_dir=/tmp/...closeout
```

## Sol REV Re-Validation Probe (S2)

`world_response_rev_probe.py` re-validates the eight REV samples of
the archived world-response batch with Sol only (`AI_MODEL_4` /
gpt-6.1-sol): at most one real review request per sample under a
global cap of 8 (model/group/sample caps 8/8/1), with the generation
slot answered by the frozen stub at zero cost. The request ledger
pre-debits the budget at `started`, so a `started_no_result` pending
slot is never resent or refunded — re-entering the run stage demands
explicit `--authorize_skip_pending` authorization and books a scoped
deviation. Stages `dry_run`, `freeze`, `run`, `summarize` follow the
frozen-protocol discipline; real `freeze`/`run` execution requires
separate authorization per design §6.2, under a new batch directory
(最多八次真实评审请求). Tests in `world_response_rev_probe_test.py`
run fully offline.

Resume audits started-sidecar contents and prompt hashes even when the
ledger file is missing; orphan evidence cannot reset spent budget to zero.
Offline summarization rechecks the freeze and ledger, binds each usable
verdict to its planned review request, terminal record and raw response,
and rejects duplicate or unknown human labels. An integrity failure clears
any saved passing summary and reports the error; it never sends requests.
REV01's revised narration retains both the lawyer's executed notice and
the neighbor's authorized reply and departure.

```
python -m examples.story_enactment.world_response_rev_probe --stage=dry_run \
    --dry_run_dir=/tmp/...dry_run
python -m examples.story_enactment.world_response_rev_probe --stage=freeze \
    --batch_dir=run_results/story_enactment/world_response_rev_probe/<UTC时间戳>
python -m examples.story_enactment.world_response_rev_probe --stage=run --batch_dir=<同上>
python -m examples.story_enactment.world_response_rev_probe --stage=summarize --batch_dir=<同上>
```

## S3 Diagnostic Story Runner

`s3_diagnostic_story.py` executes the frozen S3 diagnostic story
protocol
([docs/superpowers/plans/2026-10-23-sol-s3-diagnostic-story-protocol.md](../../docs/superpowers/plans/2026-10-23-sol-s3-diagnostic-story-protocol.md),
user-confirmed 2026-10-23): two independent full runs (R1/R2) of the
production enactment pipeline over Sol only (`AI_MODEL_4` /
gpt-6.1-sol), each with 24 story turns and a 400 model-attempt budget
under a global ledger cap of 800. Production `run.py` stays
byte-identical: the `LedgerLanguageModel` is injected by
module-attribute replacement with try/finally restore, and every
forwarded Concordia model attempt occupies one request-ledger slot. The
ledger pre-debits budget at `started` — API errors, timeouts and
no-result calls spend budget — and interrupted evidence is never
resent, refunded, skipped or re-generated: an interrupted run (bare
checkpoint without a summary, or started-without-finish ledger slots)
is marked `interrupted` and the batch pauses for user review; there
is no auto-resume — per protocol §5 the runner implements no
mid-process recovery.

Budget enforcement is two-layered (protocol §4): besides the
Concordia attempts ledger, every SDK `chat.completions.create` is
pre-debited against a per-run hard cap of 400 before the provider is
contacted; the two ledgers are rebuilt separately from their own
started events (associated per call by attempt id, never summed into
one number), and either budget being exhausted stops that run.
Concurrent calls keep their own thread-local observation state. Both
ledgers share a short bookkeeping lock for numbering, pre-debit and
archival; the lock is released while waiting for the provider. Frozen
source/protocol hashes and both ledgers are re-audited before each run
and each model/SDK forwarding boundary. A provider, archival or script
fault remains a batch stop even when production returns it as
`reason=error`; only an explicit semantic review rejection permits the
next run after an error. Archival failures are never silently ignored.
On production exit, the wrapper closes admission and drains already
admitted calls, including component work left by the fail-fast executor,
before the batch records its final result. Late calls cannot write new
ledger entries or reach the provider after closure.

Stages `dry_run`, `freeze`, `run`, `summarize` follow the
frozen-protocol discipline. The offline `dry_run` freezes a batch into
the dry-run directory, executes both runs through the deterministic
stub (`--disable_language_model` path of the production runner: zero
network, no `.env` read), summarizes, and records each batch invariant
(terminal reasons, ledger/production attempt equality, archived
prompts, caps, stub-only request stages) in `dry_run_report.json`.
Real `freeze`/`run` execution requires separate user authorization per
protocol §8, under a new batch directory (真实 `freeze`/`run` 需按协议
§8 另获授权); the protocol never initiates calls by itself. The batch
is a diagnostic, not an acceptance gate: the summary answers "where is
the obstacle" and declares no MVP quality verdict (诊断无验收门).
Tests in `s3_diagnostic_story_test.py` run fully offline.

```
python -m examples.story_enactment.s3_diagnostic_story --stage=dry_run \
    --dry_run_dir=/tmp/...dry_run
python -m examples.story_enactment.s3_diagnostic_story --stage=freeze \
    --batch_dir=run_results/story_enactment/s3_diagnostic_story/<UTC时间戳>
python -m examples.story_enactment.s3_diagnostic_story --stage=run --batch_dir=<同上>
python -m examples.story_enactment.s3_diagnostic_story --stage=summarize --batch_dir=<同上>
```

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
├── fixed_model_probe.py  # Frozen-protocol live-model validation probe
├── three_model_review_probe.py  # Three-model R-only review comparison probe
├── request_ledger.py  # Append-only request ledger (started = pre-debit)
├── world_response_probe_closeout.py  # Offline read-only batch errata (S1)
├── world_response_rev_probe.py  # Sol-only REV re-validation probe (S2)
└── *_test.py          # CI tests (no API keys needed)
```
