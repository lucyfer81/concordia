# Story enactment 行动授权与语义评审边界修补实施计划

> **For agentic workers:** 在用户授权的实施范围内逐任务执行；若环境提供 superpowers:subagent-driven-development 或 superpowers:executing-plans，可按其适用流程组织工作。技能缺失不构成额外前置条件；是否使用子代理服从用户及当前会话指令。步骤使用 checkbox（`- [ ]`）记录。

**Goal:** 按设计修补适配层对核心裁决结果的信任边界：行动裁决在独立文档上进行（不再继承上游节拍/Conversation 提示）、显式接线初始公开事实与 DisplayEvents 公开历史、把核心返回值明确为**待审结果**而非授权凭证，并让现有一次最终复核同时检查裁决结果本身的合法性与正式事件的保真性。

**Architecture:** 只改 contrib/example 层。`story_action_adjudication.py` 的裁决步骤改为在 `chain_of_thought.new()` 生成的独立文档上陈述上下文（当前玩家、规则、初始公开事实、公开历史 getter、director 窗口、GM-only 限制）并调用一次原核心 `attempt_to_result`，成功与失败都经 `director.record_resolution_diagnostics(stage='action_adjudication', ...)` 归档私有 trace；共享授权文案固化为该模块的命名常量 `AGENCY_AND_EVIDENCE_RULES`，由 `story_resolution.py` 复用进生成与评审共享的 `_SEMANTIC_RULES`（规则 2/4/5/7/8 更新，JSON schema 不变）；`make_final_output_step` 增加入口取值一次的 `public_history` getter，同一份历史同时交给生成与复核；story prefab 在 build 内把 `display_events.get_pre_act_value` 绑定为两个步骤的历史 getter，不新增可序列化配置字段。不接入 `AccountForAgencyOfOthers`、`WorldState`、`attempt_to_most_likely_outcome`、`restore_direct_quote`，不新增裁决前置评审、投票或自动重生成。

**Tech Stack:** Python 3.12+ / pytest / absl-py / unittest.mock；CI 零真实模型调用，一律 `concordia.testing.mock_model.MockModel` 与 `examples.story_enactment.testing_stubs.PromptStubModel`。格式 pyink（80 列、2 空格缩进）+ isort（Google profile）；`pylint --errors-only`；pyrefly。

**Spec:** `docs/superpowers/specs/2026-10-17-story-enactment-action-authorization-review-design.md`（本计划从该设计推导；执行者须同时阅读设计与本计划）

## Global Constraints

以下约束来自设计 §1/§6/§7/§8，每个任务默认全部适用：

- **不提交 git**：工作区已有同事未提交的修改（`git status` 可见 20 个 ` M` 条目 + 2 个 `??` 条目，含本计划触碰的文件）。全程 **不 commit、不 stash、不 clean、不回滚** 既有修改（设计 §7）。每个任务以"运行验证命令并记录退出码"收尾，代替提交步骤。
- **核心零修改**：`git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` 在收尾时必须为空；`concordia/components/game_master/event_resolution.py` 的 `attempt_to_result`（event_resolution.py:663）、`maybe_inject_narrative_push`、`DisplayEvents` 一律不改。
- **零真实模型调用**：所有测试用 `MockModel` / `PromptStubModel`。设计 §9 的冻结样本真实评测（F8–F16、24 次裁决输入、四组对照运行）是**后续独立工作，本计划不执行**。
- **阶段 C 保持 gate**：不启动旧计划的 Task 7／重复判断校准；`repetition_push.py` 与其测试内容保持开工基线。本计划 Task 7 仅指文档与验收收尾。
- **调用协议不变**：普通非空动作轮仍是一次核心裁决、一次最终生成、一次独立复核；无动作意图时仅行动裁决为零调用，最终生成与复核仍正常执行；评审返回仍为严格 `{"valid": bool, "reason": str}`；一次否决即停止；同一正常动作轮的裁决/生成/复核调用数不得增加（设计 §1/§8.8）。
- **待审结果语义**：`action_result` 字段继续表示**核心实际返回的待审裁决结果**，不是授权凭证；合法的成功/失败/受阻结果必须保真；原始结果越权则整轮拒绝，不得由最终生成删句后宣称有效（设计 §4.3）。
- **诊断私有**：裁决 trace 只进 `record_resolution_diagnostics` 的私有归档（checkpoint/JSONL 的私有诊断字段、process summary 的 `uncommitted_resolution`），不进玩家观察、FDX、剧本正文或后续生成链（设计 §6）。
- **历史范围不缩小**：公开历史用现有 `DisplayEvents.get_pre_act_value`（默认 100 条）显式保留；director 窗口（`get_committed_events`，`max(scoring_window, 20)` 条）继续用于评分/推进与既有兼容调用；getter 在 build 内绑定，不进可序列化 params；getter 缺省时沿用 director 窗口（设计 §3/§4.1）。
- **run_results 只读**：不覆写任何历史运行产物；旧报告勘误以追加方式写入新的交付 notes（设计 §2.2/§7）。
- **新增回归不依赖 `run_results/`**：测试自带全部夹具文本，CI 无该目录也能跑（设计 §7）。
- **默认行为不变**：`speech_only` 默认模式行为不变；旧状态恢复维持动作模式、模型身份与总预算。
- **环境**：先 `source venv/bin/activate`（若 venv 不存在按 AGENTS.md 用 `uv venv venv && source venv/bin/activate && uv pip install --editable '.[dev]'` 创建）。所有命令用 Bash。
- **风格**：pyink 80 列 / 2 空格缩进；意图注释跟随现有文件风格（本仓库惯例是带设计引用的注释）。

---

### Task 1: 开工基线快照与预检

**Files:**
- Create: `/tmp/opencode/action-auth-baseline/` 下的 `git-status.txt`、`git-diff.patch`、`content-hashes.json`、`files/` 内容快照与 `run_results-hashes.txt`（均在仓库外）
- 无仓库内文件改动

**Interfaces:**
- Consumes: 当前 dirty worktree、`run_results/` 目录。
- Produces: 基线证据文件，Task 7 的收尾核对（run_results 只读验证、dirty 基线未被回滚）依赖它们。

- [x] **Step 1: 确认虚拟环境可用**

```bash
source venv/bin/activate && python -c "import concordia; print('ok')"
```

Expected: 输出 `ok`。若 venv 不存在，先按 AGENTS.md 执行 `uv venv venv && source venv/bin/activate && uv pip install --editable '.[dev]'`。

- [x] **Step 2: 记录 dirty worktree 基线快照**

```bash
source venv/bin/activate
python - <<'PY'
import hashlib
import json
from pathlib import Path
import subprocess

root = Path.cwd()
baseline = Path('/tmp/opencode/action-auth-baseline')
# Refuse to overwrite an earlier evidence chain. If this directory
# exists, select a fresh path and use it consistently throughout.
baseline.mkdir(parents=True, exist_ok=False)
for filename, command in (
    ('git-status.txt', ['git', 'status', '--short']),
    ('git-diff.patch', ['git', 'diff', '--binary']),
):
  (baseline / filename).write_bytes(subprocess.check_output(command))
names = set(filter(None, subprocess.check_output([
    'git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
]).decode().split('\0')))
# Docs are Git-ignored in this workspace; include their actual bytes.
names.update(str(p) for p in Path('docs/superpowers').rglob('*') if p.is_file())
manifest = {}
for name in sorted(names):
  source = root / name
  if not source.is_file():
    manifest[name] = None
    continue
  data = source.read_bytes()
  manifest[name] = hashlib.sha256(data).hexdigest()
  target = baseline / 'files' / name
  target.parent.mkdir(parents=True, exist_ok=True)
  target.write_bytes(data)
(baseline / 'content-hashes.json').write_text(
    json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
)
print(f'Snapshotted {len(manifest)} tracked/untracked/doc paths')
PY
```

Expected: 保存所有已跟踪文件、未跟踪源码（包括两个 repetition 文件）及被忽略 docs 的原内容与哈希；status 仅供定位，不用其行数证明内容完整。之后任何任务不得清空或回滚既有修改。基线目录已存在时保留它，改用新的基线路径，并同步本计划后续命令中的路径。

- [x] **Step 3: 记录 run_results 只读基线（清单+哈希）**

```bash
source venv/bin/activate
set -o pipefail
if test -d run_results; then
  find run_results -type f -print0 | sort -z | xargs -0 -r sha256sum \
    > /tmp/opencode/action-auth-baseline/run_results-hashes.txt
  wc -l /tmp/opencode/action-auth-baseline/run_results-hashes.txt
else
  printf 'run_results absent at baseline\n' \
    > /tmp/opencode/action-auth-baseline/run-results-unavailable.txt
fi
```

Expected: 清单覆盖 `story_enactment/action_mode_control/` 等目录。若 `run_results/` 不存在，记录该限制，不补造证明。

- [x] **Step 4: 记录预改测试基线**

```bash
source venv/bin/activate
set -o pipefail
action_auth_contrib_rc=0
pytest concordia/contrib/components/game_master concordia/contrib/prefabs -q 2>&1 | tail -3 || action_auth_contrib_rc=$?
printf 'exit-contrib=%s\n' "$action_auth_contrib_rc"
action_auth_examples_rc=0
pytest examples/story_enactment -q 2>&1 | tail -3 || action_auth_examples_rc=$?
printf 'exit-examples=%s\n' "$action_auth_examples_rc"
test "$action_auth_contrib_rc" -eq 0 && test "$action_auth_examples_rc" -eq 0
```

Expected: 两范围全部 PASS 且退出码 0（2026-10-17 run4 交付的 GREEN 状态）。若有失败，先停下向用户报告，不要在未知基线上开工。

---

### Task 2: RED——裁决上下文与共享授权规则的失败断言

设计 §7 顺序 1：先在两个测试文件加入新上下文、共享授权规则和正反样本的失败断言，记录旧文案/接线的 RED。本任务的测试在 Task 3（裁决）与 Task 5（评审）转 GREEN；`story_prefabs_test.py` 的接线断言属 Task 4。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_action_adjudication_test.py`
- Modify: `concordia/contrib/components/game_master/story_resolution_test.py`

**Interfaces:**
- Consumes: 既有助手 `_make_director`、`_open_turn`、`_spy_text`、`_step_call`、`_BudgetExhaustedModel`、常量 `_CORE_QUESTION`/`_BOUNDARIES_MARK`/`_INTENT`/`_OUTCOME`/`_GM_BOUNDARIES`（adjudication 测试）；`_make`/`_run`/`_read_evidence_section`/`_APPROVAL`/`_ACTION_RESULT`/`_ACTION_INTENT`/`_EMPTY_EVIDENCE`（resolution 测试）。
- Produces: Task 3/5 实现必须满足的契约——`make_action_adjudication_step(director, *, public_facts=(), public_history=None)`、模块常量 `story_action_adjudication.AGENCY_AND_EVIDENCE_RULES`、提示指纹（本任务逐字定义）、`make_final_output_step(..., public_history=None)`、`stage='action_adjudication'` 诊断记录、公开历史段标记 `## Public history (data)`。

- [x] **Step 1: 在 `story_action_adjudication_test.py` 增加模块常量**

在 `_GM_BOUNDARIES` 定义之后追加：

```python
# Fingerprints of the isolated adjudication context (2026-10-17
# authorization-review design, sections 4.1 and 8.1). The upstream
# marker shares the phrase "the true heir" with the GM-only fact so the
# isolation test proves context separation, not string dissimilarity.
_UPSTREAM_BEAT_MARKER = (
    'UPSTREAM future task: reveal the true heir by forging a letter'
)
_SIMILAR_GM_ONLY_FACT = (
    'The will has a true heir, revealed only by a legal disclosure.'
)
_PUBLIC_FACT = 'The sealed will envelope lies on the ancestral table.'
_OLD_DISPLAYED_FACT = '周律师早在众人面前展示过遗嘱封套。'
_HISTORY_SECTION_MARK = '## Public history (data)'
_HISTORY_NOT_EVIDENCE_MARK = (
    'remains attributed speech, not objective evidence'
)
_SHARED_SCOPE_MARK = "settle only the current player's own attempted action"
```

- [x] **Step 2: 增加隔离文档与历史接线的失败测试**

在 `ActionAdjudicationStepTest` 内（`test_core_return_is_recorded_and_returned_unchanged` 之后）追加四个测试：

```python
  def test_isolated_document_excludes_upstream_chain_content(self):
    # The adjudication must run on its own document: beat prompts, the
    # conversation window and any other upstream instruction inherited
    # by the shared chain document must not reach the core question,
    # while GM-only constraint text that merely resembles them stays
    # (design 4.1, 8.1).
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(
        model,
        action_mode='speech_and_action',
        immutable_facts=(_SIMILAR_GM_ONLY_FACT,),
    )
    _open_turn(gm, 'I will tidy the study.', _INTENT)
    document = interactive_document.InteractiveDocument(model)
    document.statement(
        f'Current beat\nsetup (beat b1): {_UPSTREAM_BEAT_MARKER}'
    )
    document.statement(
        'Conversation\n0). Alice: we should rehearse the ending.'
    )
    story_action_adjudication.make_action_adjudication_step(director)(
        document, 'premise', 'Alice'
    )
    prompt = spy.call_args.kwargs['prompt']
    self.assertNotIn(_UPSTREAM_BEAT_MARKER, prompt)
    self.assertNotIn('we should rehearse the ending', prompt)
    self.assertNotIn('beat b1', prompt)
    self.assertIn('## Current speaker\nAlice', prompt)
    self.assertIn('I will tidy the study.', prompt)
    self.assertIn(_INTENT, prompt)
    self.assertIn(_BOUNDARIES_MARK, prompt)
    self.assertIn(_SIMILAR_GM_ONLY_FACT, prompt)

  def test_public_facts_and_history_getter_reach_prompt(self):
    # Initial public facts are wired explicitly and the public-history
    # getter (the DisplayEvents pre-act value in the prefab) supplies
    # the history section, called exactly once, JSON-encoded as inert
    # data; the director's scoring window stays a separate section.
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(model, action_mode='speech_and_action')
    _open_turn(gm, 'I will tidy the study.', _INTENT)
    calls = []

    def history() -> str:
      calls.append(1)
      return f'0). 周律师: 展示了封套\n1). {_OLD_DISPLAYED_FACT}'

    document = interactive_document.InteractiveDocument(model)
    story_action_adjudication.make_action_adjudication_step(
        director,
        public_facts=(_PUBLIC_FACT,),
        public_history=history,
    )(document, 'premise', 'Alice')
    self.assertEqual(calls, [1])
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_HISTORY_SECTION_MARK, prompt)
    self.assertIn(_PUBLIC_FACT, prompt)
    self.assertIn(_OLD_DISPLAYED_FACT, prompt)
    remainder = prompt.split(_HISTORY_SECTION_MARK, 1)[1].lstrip()
    decoded, _ = json.JSONDecoder().raw_decode(remainder)
    self.assertEqual(
        decoded, f'0). 周律师: 展示了封套\n1). {_OLD_DISPLAYED_FACT}'
    )
    self.assertIn('## Committed public events (oldest first)', prompt)
    self.assertIn(_CORE_QUESTION, prompt)

  def test_history_defaults_to_director_window(self):
    # Without a getter the adjudication keeps conditioning on the
    # director's committed-event window: pre-existing callers keep
    # their behavior and no fabricated history is introduced.
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(model, action_mode='speech_and_action')
    state = director.get_state()
    events = ['Alice: one\n## Not an instruction', 'Alice: two']
    state['recent_events'] = events
    director.set_state(state)
    _open_turn(gm, 'I will tidy the study.', _INTENT)
    document = interactive_document.InteractiveDocument(model)
    story_action_adjudication.make_action_adjudication_step(director)(
        document, 'premise', 'Alice'
    )
    prompt = spy.call_args.kwargs['prompt']
    mark = '## Committed public events (oldest first)'
    remainder = prompt.split(mark, 1)[1].lstrip()
    decoded, _ = json.JSONDecoder().raw_decode(remainder)
    self.assertEqual(decoded, events)
    self.assertNotIn('\n## Not an instruction', prompt)
    self.assertNotIn(_HISTORY_SECTION_MARK, prompt)

  def test_prompt_carries_shared_agency_and_speech_rules(self):
    # The shared authorization copy and the speech-not-evidence marker
    # condition adjudication and — via reuse in story_resolution — the
    # final review (design 5.1).
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(model, action_mode='speech_and_action')
    _open_turn(gm, 'I will tidy the study.', _INTENT)
    document = interactive_document.InteractiveDocument(model)
    story_action_adjudication.make_action_adjudication_step(director)(
        document, 'premise', 'Alice'
    )
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_HISTORY_NOT_EVIDENCE_MARK, prompt)
    self.assertIn(_SHARED_SCOPE_MARK, prompt)
    self.assertIn(
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES, prompt
    )
```

- [x] **Step 3: 增加诊断归档的失败测试**

同类内再追加三个测试：

```python
  def test_success_archives_action_adjudication_trace(self):
    # A successful core call archives its private trace with error
    # None; the recorded action result and the returned value stay
    # identical and the trace stays out of the broadcast-facing
    # evidence (design 6).
    model = mock_model.MockModel(_OUTCOME)
    director, gm = _make_director(model, action_mode='speech_and_action')
    _open_turn(gm, 'I will tidy the study.', _INTENT)
    returned = _step_call(model, director, 'premise')
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'action_adjudication')
    self.assertIsNone(diagnostics['error'])
    self.assertIn(_CORE_QUESTION, diagnostics['trace'])
    self.assertIn(_OUTCOME, diagnostics['trace'])
    self.assertEqual(len(diagnostics['history']), 1)
    self.assertEqual(
        director.get_current_resolution_evidence()['action_result'],
        returned,
    )
    self.assertNotIn(
        'trace', director.get_current_resolution_evidence()
    )

  def test_budget_error_archives_partial_trace_then_raises(self):
    # Path A at the adjudication position: the visible partial trace is
    # archived with the error, no result is recorded, nothing commits
    # (design 6).
    model = _BudgetExhaustedModel()
    director, gm = _make_director(model, action_mode='speech_and_action')
    _open_turn(gm, 'Here goes nothing.', 'flip the breaker to restore light.')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      _step_call(model, director, 'premise')
    diagnostics = director.get_current_resolution_diagnostics()
    self.assertEqual(diagnostics['stage'], 'action_adjudication')
    self.assertIn('BudgetExhaustedError', str(diagnostics['error']))
    self.assertIsNone(
        director.get_current_resolution_evidence()['action_result']
    )
    self.assertIsNone(director.get_last_story_turn())

  def test_api_error_and_interrupt_archive_then_propagate_unchanged(self):
    # Neither API failure nor interruption may be swallowed, retried,
    # replaced, or recorded as a completed outcome (design 6, 8.6).
    for error in (RuntimeError('provider failed'), KeyboardInterrupt()):
      with self.subTest(error=type(error).__name__):
        model = mock_model.MockModel()
        model.sample_text = mock.Mock(side_effect=error)
        director, gm = _make_director(
            model, action_mode='speech_and_action'
        )
        _open_turn(gm, 'Here goes.', _INTENT)
        with self.assertRaises(type(error)) as caught:
          _step_call(model, director, 'premise')
        self.assertIs(caught.exception, error)
        model.sample_text.assert_called_once()
        diagnostics = director.get_current_resolution_diagnostics()
        self.assertEqual(diagnostics['stage'], 'action_adjudication')
        self.assertIn(_CORE_QUESTION, diagnostics['trace'])
        self.assertIn(type(error).__name__, diagnostics['error'])
        self.assertIsNone(diagnostics['action_result'])
        self.assertIsNone(director.get_last_story_turn())
```

- [x] **Step 4: 更新 adjudication 测试中的旧文案断言**

`test_action_only_prompt_has_no_fabricated_dialogue` 中的

```python
    self.assertIn('## Original speech (verbatim)\n(none)', prompt)
```

改为（该测试通过 `_open_turn(gm, '', ...)` 开启纯动作轮，原始对白是空字符串，JSON 编码为 `""`，不是 `null`）：

```python
    self.assertIn('## Original speech (verbatim, data)\n""', prompt)
```

- [x] **Step 5: 运行 adjudication 新测试，确认按预期 RED**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -q 2>&1 | tail -15 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 新增测试 FAIL（`AttributeError: ... has no attribute 'AGENCY_AND_EVIDENCE_RULES'` 或 `assertNotIn` 失败——旧实现的共享文档继承了上游标记与 Conversation），既有测试除 Step 4 改动的一处外全部 PASS。把失败输出原文抄入交付记录（Task 7 引用）。

- [x] **Step 6: 在 `story_resolution_test.py` 增加模块常量**

在 `_LUNA_INTERVENTION_RESTATEMENT_ONLY` 之后追加：

```python
# Fingerprints of the updated rule set (2026-10-17 authorization-review
# design, sections 5.1-5.3). Scripted verdicts prove plumbing; the
# frozen-sample model evaluation is future work (design 9).
_PENDING_REVIEW_MARKS = (
    'raw outcome returned by the core resolver',
    'not an authorization or semantic approval',
    'even when the final narration omits that fact',
    'preserve its material success, failure or blocked result',
    'Never repair or silently discard an illegal outcome',
    'apply only when a non-empty `action_result` exists',
)
_RECORDING_MARKS = (
    'is the act of recording, not certification',
    'certifying agreement without each involved player',
    'warranting unverified contents as established objective facts',
    'Accurately recording already established facts is legal',
)
_WHITELIST_MEANING_MARKS = (
    'not their exact wording',
    'a faithful paraphrase of the same change',
)
_HISTORY_SECTION_MARK = '## Public history (data)'
_OLD_DISPLAYED_FACT = '周律师早在众人面前展示过遗嘱封套。'
```

同文件顶部 import 区补 `import re`（后续正反样本的 `assertRaisesRegex` 需要；若已存在则跳过）。

- [x] **Step 7: 增加共享授权规则与历史接线的失败测试**

在 `ActionResolutionTest` 与 `SemanticReviewTest` 之间新增一个测试类：

```python
class SharedAuthorizationRuleTest(unittest.TestCase):
  """The shared authorization copy and updated rules 2/4/5/7/8."""

  def _prompts(self, **step_kwargs):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = '我先看看那个花瓶。'
    director.get_current_action_intent.return_value = '取下花瓶。'
    director.get_current_resolution_evidence.return_value = {
        'action_result': _ACTION_RESULT,
        'intervention': None,
    }
    director.get_committed_events.return_value = ()
    model = mock_model.MockModel()
    model.sample_text = mock.Mock(
        side_effect=[
            json.dumps({'stage_directions': 'The lock holds.'}),
            _APPROVAL,
        ]
    )
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(director, **step_kwargs)
    step(chain, 'candidate', 'Alice')
    return [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]

  def test_pending_review_rule7_fingerprints_shared_by_both_prompts(self):
    generation, review = self._prompts()
    for prompt in (generation, review):
      flat = ' '.join(prompt.split())
      for mark in _PENDING_REVIEW_MARKS:
        self.assertIn(mark, flat)

  def test_recording_and_whitelist_semantics_fingerprints(self):
    generation, review = self._prompts()
    for prompt in (generation, review):
      flat = ' '.join(prompt.split())
      for mark in _RECORDING_MARKS + _WHITELIST_MEANING_MARKS:
        self.assertIn(mark, flat)

  def test_shared_adjudication_rules_reach_generation_and_review(self):
    from concordia.contrib.components.game_master import (
        story_action_adjudication,
    )

    generation, review = self._prompts()
    for prompt in (generation, review):
      self.assertIn(
          story_action_adjudication.AGENCY_AND_EVIDENCE_RULES, prompt
      )

  def test_public_history_frozen_once_reaches_both_prompts(self):
    # The getter is read exactly once per step entry and the identical
    # history string reaches generation and review as inert JSON data
    # (design 4.1: never a long history for generation and a shorter director
    # window for the review).
    calls = []

    def history() -> str:
      calls.append(1)
      return f'0). 周律师: 早已公开展示\n1). {_OLD_DISPLAYED_FACT}'

    generation, review = self._prompts(public_history=history)
    self.assertEqual(calls, [1])
    expected = json.dumps(
        f'0). 周律师: 早已公开展示\n1). {_OLD_DISPLAYED_FACT}',
        ensure_ascii=False,
    )
    for prompt in (generation, review):
      self.assertIn(_HISTORY_SECTION_MARK, prompt)
      self.assertIn(expected, prompt)
```

- [x] **Step 8: 增加 F8/F11/F12 式拒绝管道与正反样本的失败测试**

在 `ActionResolutionTest` 内追加三个测试：

```python
  def test_rejection_covers_illegal_raw_result_even_when_stage_omits_it(self):
    # F11/F12 (design 9.1): the raw adjudicated outcome fabricates paper
    # ash; the stage directions delete the bad sentence; the scripted
    # rejection still refuses the whole turn. The verdict is scripted:
    # this proves rejection plumbing and evidence routing, not that a
    # model detects the violation.
    raw = '林雪尝试取下花瓶。 Because of that, 花瓶碎裂，露出烧灼纸灰残片。'
    director, model, chain, step = self._make(
        speech='我先看看那个花瓶。',
        responses=[
            json.dumps({'stage_directions': '林雪取下了空花瓶。'}),
            json.dumps({
                'valid': False,
                'reason': 'The adjudicated outcome fabricated paper ash.',
            }),
        ],
        action_intent='取下花瓶。',
        evidence={'action_result': raw, 'intervention': None},
    )
    with self.assertRaisesRegex(
        story_resolution.StoryOutputError, 'fabricated paper ash'
    ):
      step(chain, 'candidate', '林雪')
    self.assertEqual(model.sample_text.call_count, 2)
    _, review = [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]
    self.assertEqual(_read_evidence_section(review)['action_result'], raw)
    self.assertIn('纸灰', review)
    director.set_validated_resolution.assert_not_called()
    director.commit_directive.assert_not_called()

  def test_positive_action_semantics_scripted_approval_commits(self):
    # F9/F10/F14 positives (design 9.1): the speaker's own legal
    # display of an accessible document, restating an already-public
    # display, and recording one's own attributed unverified claim.
    # Scripted approvals commit; the prompts carry the evidence needed
    # to judge each case.
    cases = (
        (
            'own display', '周律师', '展示手边已启封的原件。',
            '周律师展示原件，落款日期可见。',
            ('已启封的遗嘱原件在周律师手边，落款为公开可见内容。',),
        ),
        (
            'public restatement', '林雪', None,
            '封套此前已当众展示，状态未变。',
            ('周律师在自己的回合展示了封套，此后封套状态未变。',),
        ),
        (
            'attributed recording', '林雪',
            '记下自己提出的暂停主张，注明未经各方确认。',
            '林雪在笔记中记下自己的暂停主张，注明未经各方确认。',
            (),
        ),
        (
            'grounded agreement record', '周律师',
            '记下林雪与陈默先前各自明确同意暂停开启保险柜。',
            '周律师记下林雪与陈默已各自明确同意暂停开启保险柜。',
            ('林雪: 我同意暂停开启保险柜。',
             '陈默: 我也同意暂停开启保险柜。'),
        ),
    )
    for label, actor, intent, stage, history in cases:
      with self.subTest(label=label):
        evidence = {
            'action_result': stage if intent else None,
            'intervention': None,
        }
        director, model, chain, step = self._make(
            speech='我先确认一下。',
            responses=[json.dumps({'stage_directions': stage}), _APPROVAL],
            action_intent=intent,
            evidence=evidence,
        )
        director.get_committed_events.return_value = history
        result = step(chain, 'candidate', actor)
        self.assertEqual(result, f'{actor}: 我先确认一下。\n{stage}')
        director.set_validated_resolution.assert_called_once_with(
            result, stage
        )
        review = model.sample_text.call_args_list[1].kwargs['prompt']
        decoded = _read_evidence_section(review)
        self.assertEqual(decoded['active_player_name'], actor)
        self.assertEqual(decoded['action_intent'], intent)
        self.assertEqual(decoded['action_result'], evidence['action_result'])
        for event in history:
          self.assertIn(event, review)

  def test_legal_broadcast_uses_intervention_not_player_disclosure(self):
    # A whitelisted NPC/environmental broadcast is a separate legal
    # information source, including on a turn with no action intent.
    # The shared action limits must not require a player to utter it.
    event = '堂屋的旧收音机自己响了起来，播报暴雨红色预警'
    intervention = {
        'reason': 'tension',
        'candidate_before': '王婶: 我先听听。',
        'candidate_after': f'王婶: 我先听听。 {event}',
    }
    director, model, chain, _ = self._make(
        speech='我先听听。',
        responses=[json.dumps({'stage_directions': event}), _APPROVAL],
        evidence={'action_result': None, 'intervention': intervention},
    )
    step = story_resolution.make_final_output_step(
        director,
        player_names=('王婶',),
        public_facts=('堂屋里有一台旧收音机。',),
        allowed_environment_events=(event,),
    )
    result = step(chain, intervention['candidate_after'], '王婶')
    self.assertEqual(result, f'王婶: 我先听听。\n{event}')
    review = model.sample_text.call_args_list[1].kwargs['prompt']
    self.assertEqual(_read_evidence_section(review)['intervention'], intervention)
    self.assertIn('legal NPC or environmental interventions', review)

  def test_negative_action_semantics_scripted_rejection_blocks(self):
    # F8/F15/F16 negatives (design 9.1): another player's new voluntary
    # display inside the raw result, narration certifying collective
    # agreement without sources, blanket certification of unverified
    # notes. Scripted rejections leave nothing committed.
    cases = (
        (
            'other players new display in raw result',
            {
                'action_result': '周律师主动展示遗嘱封套，并公开了其中的内容。',
                'intervention': None,
            },
            'Another player acted voluntarily in the adjudicated outcome.',
        ),
        (
            'narration certifies collective agreement',
            {
                'action_result': '律师暂停了自己的办理工作。',
                'intervention': None,
            },
            'The narration certified that all parties agreed.',
        ),
        (
            'blanket certification of unverified notes',
            {
                'action_result': '律师在笔记中记下各方的说法。',
                'intervention': None,
            },
            'The narration warranted unverified notes as established facts.',
        ),
    )
    for label, evidence, reason in cases:
      with self.subTest(label=label):
        director, model, chain, step = self._make(
            speech='我先说说我的看法。',
            responses=[
                json.dumps({
                    'stage_directions': (
                        '各方一致同意暂停处理；笔记内容均为已证实的客观事实。'
                    ),
                }),
                json.dumps({'valid': False, 'reason': reason}),
            ],
            action_intent='记录各方的说法。',
            evidence=evidence,
        )
        with self.assertRaisesRegex(
            story_resolution.StoryOutputError, re.escape(reason)
        ):
          step(chain, 'candidate', '周律师')
        director.set_validated_resolution.assert_not_called()
        director.commit_directive.assert_not_called()
```

在 `SemanticReviewTest` 内追加：

```python
  def test_review_question_asks_for_specific_offending_text(self):
    # Design 5.1: a rejection reason must name the offending text, its
    # origin (action result or narration) and the missing source.
    director, model, chain, step = self._setup()
    step(chain, 'candidate', 'Alice')
    review = model.sample_text.call_args_list[1].kwargs['prompt']
    flat = ' '.join(review.split())
    self.assertIn('name the specific offending text', flat)
    self.assertIn(
        'whether it comes from the action result or the narration', flat
    )
```

- [x] **Step 9: 更新 resolution 测试中受规则改写影响的两处旧指纹**

`test_rules_allow_adjudicated_own_action_as_fact_source` 中的

```python
      self.assertIn("other players' new choices", flat)
```

与

```python
      self.assertIn('secrets no one accessed', flat)
```

改为（对齐 Task 5 的新规则 7 措辞）：

```python
      self.assertIn("another player's new voluntary act", flat)
      self.assertIn('an undisclosed secret', flat)
```

- [x] **Step 10: 运行 resolution 新测试，确认按预期 RED**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master/story_resolution_test.py -q 2>&1 | tail -15 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 新增测试 FAIL（`TypeError: make_final_output_step() got an unexpected keyword argument 'public_history'`、指纹 `assertIn` 失败或 `AttributeError`），既有测试除 Step 9 改动的两处外全部 PASS。抄录失败输出供 Task 7 引用。

---

### Task 3: `story_action_adjudication.py` 独立行动文档实现

设计 §7 顺序 2：独立行动文档、`public_facts` 参数、明确自身动作范围、原始返回保真、诊断归档。本任务把 Task 2 的 adjudication 断言转 GREEN。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_action_adjudication.py`（模块 docstring、常量区、`make_action_adjudication_step`、`_state_adjudication_context`）

**Interfaces:**
- Consumes: `interactive_document.InteractiveDocument.new()`（保留同一包装模型与 RNG，清空内容）、`event_resolution_lib.attempt_to_result`（原样调用一次）、`director.record_action_result`、`director.record_resolution_diagnostics(stage, trace, error)`、`director.get_committed_events()`、`director.immutable_facts/character_constraints/world_constraints/player_names`。
- Produces: `AGENCY_AND_EVIDENCE_RULES: str`（模块常量，Task 5 复用）、`PUBLIC_HISTORY_SECTION_MARK = '## Public history (data)'`（Task 5 复用）、`make_action_adjudication_step(director, *, public_facts: Sequence[str] = (), public_history: Callable[[], str] | None = None)`。

- [x] **Step 1: 重写模块 docstring 与常量区**

模块 docstring 中"on the same adjudication document the surrounding chain uses"一段改为描述独立文档；"its true return value is recorded ... and returned unchanged"一段补一句：返回值是**待审裁决结果**（pending review），不是授权或语义批准（design 4.3）。在 import 区改/增：

```python
from collections.abc import Callable, Sequence
import json

from concordia.components.game_master import event_resolution as event_resolution_lib
from concordia.contrib.components.game_master import drama_director
from concordia.document import interactive_document
```

在 `_ADJUDICATION_RULES` 之前定义两个导出常量与改写后的规则：

```python
# Heading of the public-history data section, shared by convention with
# `story_resolution` (which imports it) so the two prompts cannot drift.
PUBLIC_HISTORY_SECTION_MARK = '## Public history (data)'

# Shared authorization semantics (design sections 4.2 and 5.1), stated
# verbatim in the adjudication prompt and reused by `story_resolution`
# in generation and review: one copy, no drift. The wording follows the
# Concordia design pattern's player-agency rule and the 2026-10-17
# action-authorization review design's boundary table.
AGENCY_AND_EVIDENCE_RULES = (
    "The action outcome must settle only the current player's own"
    ' attempted action and its direct consequences. Completing the act'
    ' of requesting — asking another player to show, sign, hand over or'
    " agree — can succeed; the other side's voluntary compliance,"
    ' display, signature, promise or consent cannot be generated,'
    ' because those choices belong to their own turns. The current'
    " player's own movement, handling of reachable items, recording and"
    ' reading of accessible documents may succeed, fail or be blocked'
    ' by the world. Mechanical effects and visible involuntary'
    ' reactions may be described without inferring consent, confession'
    ' or agreement; other players\' actions in committed history are'
    ' background, never newly generated actions. GM-only knowledge may'
    ' be used to judge the true contents of existing documents or to'
    ' block impossible acts. For information disclosed by the current'
    ' attempted action, require legal access or disclosure by that'
    ' player; a request alone is not access. These action limits do'
    ' not invalidate already established public information or legal'
    ' NPC or environmental interventions as separate information'
    ' sources. Never fabricate physical evidence — ash, recordings,'
    ' notes — that no committed event or legal access established, and'
    ' never insert whitelisted events unrelated to the current attempt'
    ' to advance the plot; director-initiated events stay on the'
    ' existing intervention paths.'
)

_ADJUDICATION_RULES = (
    'Adjudicate only the attempted action the current speaker proposed'
    ' for themself this turn. Judge from the GM-only facts, the initial'
    ' public facts, the public history and the committed public events'
    ' whether the attempt succeeds, fails or is blocked. Never decide a'
    ' new voluntary action for any other player character, and never'
    ' fabricate physical evidence that no committed event or legal'
    ' access established. A failed or blocked attempt still gets one'
    ' definite actual result; never defer the outcome to a later'
    ' verification.'
)
```

- [x] **Step 2: 重写 `make_action_adjudication_step` 与 `_state_adjudication_context`**

完整替换两个函数（`_clean_speech_premise` 不变）：

```python
def make_action_adjudication_step(
    director: drama_director.DramaDirector,
    *,
    public_facts: Sequence[str] = (),
    public_history: Callable[[], str] | None = None,
) -> Callable[[interactive_document.InteractiveDocument, str, str], str]:
  """Creates the action-adjudication step of an event-resolution chain.

  Args:
    director: the drama director holding the current turn's decoded
      speech and action intent, which receives the adjudicated result.
    public_facts: initial publicly established background stated on
      the adjudication document; an empty sequence adds no initial
      facts and retains the explicit history and GM-only constraints.
    public_history: zero-argument getter supplying the public history
      (the DisplayEvents pre-act value in the story prefab); it is
      called exactly once per adjudication and its return travels as
      inert JSON data. When None the adjudication keeps conditioning
      on the director's committed-event window.

  Returns:
    A thought step with the EventResolution step signature
    ``(document, premise, active_player_name) -> str``. A turn with an
    action intent is adjudicated on an independent document — nothing
    is inherited from the upstream chain prompts — by exactly one call
    to the core ``attempt_to_result`` helper, whose true return value
    is recorded on the director and returned unchanged as a
    pending-review outcome, never as an authorization. A turn without
    an intent has its raw JSON premise replaced by the clean
    attributed dialogue, with zero model calls.
  """

  def step(
      chain_of_thought: interactive_document.InteractiveDocument,
      candidate_event: str,
      active_player_name: str,
  ) -> str:
    """Adjudicates the current character's own action attempt, if any."""
    action_intent = director.get_current_action_intent()
    if not action_intent:
      return _clean_speech_premise(
          director=director,
          candidate_event=candidate_event,
          active_player_name=active_player_name,
      )
    original_speech = director.get_current_original_speech()
    # The adjudication document is independent of the shared chain:
    # `new()` keeps the same wrapped model (and its budgets) while
    # clearing the upstream prompts, so beat instructions and the
    # conversation window cannot leak into the core question (design
    # 4.1). The history getter is read exactly once, here.
    document = chain_of_thought.new()
    _state_adjudication_context(
        document=document,
        director=director,
        active_player_name=active_player_name,
        original_speech=original_speech,
        action_intent=action_intent,
        public_facts=public_facts,
        public_history=public_history() if public_history else None,
    )
    # The core helper runs as-is, once: it appends its question to the
    # isolated adjudication document and makes exactly one model call.
    # On any catchable failure the visible partial trace is archived
    # first (path A: nothing commits), then the error propagates
    # uncaught, unretried and unwrapped.
    try:
      result = event_resolution_lib.attempt_to_result(
          document,
          action_intent,
          active_player_name,
      )
    except BaseException as error:
      director.record_resolution_diagnostics(
          stage='action_adjudication',
          trace=document.view().text(),
          error=f'{type(error).__name__}: {error}',
      )
      raise
    # Record the core's true return value before returning the same
    # value, so final generation and review consume the actual
    # adjudicated outcome, never the intent or stub configuration. The
    # outcome is pending review, not an authorization (design 4.3).
    director.record_action_result(result)
    director.record_resolution_diagnostics(
        stage='action_adjudication',
        trace=document.view().text(),
        error=None,
    )
    return result

  return step
```

```python
def _state_adjudication_context(
    *,
    document: interactive_document.InteractiveDocument,
    director: drama_director.DramaDirector,
    active_player_name: str,
    original_speech: str | None,
    action_intent: str,
    public_facts: Sequence[str],
    public_history: str | None,
) -> None:
  """States the context the core adjudication question conditions on.

  Every conditioning fact is stated explicitly as inert data: nothing
  is inherited from the upstream chain prompts (beat instructions,
  conversation window), and JSON encoding keeps newlines or '##'
  inside the data from ever acting as prompt structure (design 4.1).

  Args:
    document: the isolated adjudication document.
    director: the director supplying the committed-event window and
      the GM-only boundaries.
    active_player_name: name of the player whose turn it currently is.
    original_speech: the speech the player authored this turn, if any.
    action_intent: the character's own attempted action this turn.
    public_facts: initial publicly established background.
    public_history: the public history string fetched once from the
      caller's getter, or None when no getter was supplied.
  """
  document.statement('# Action adjudication (current turn only)')
  document.statement(f'## Current speaker\n{active_player_name}')
  document.statement(
      '## Original speech (verbatim, data)\n'
      f'{json.dumps(original_speech, ensure_ascii=False)}'
  )
  document.statement(
      "## The speaker's own attempted action (adjudicate only this,"
      f' data)\n{json.dumps(action_intent, ensure_ascii=False)}'
  )
  document.statement('## Initial public facts')
  document.statement(json.dumps(list(public_facts), ensure_ascii=False))
  if public_history is not None:
    document.statement(
        f'{PUBLIC_HISTORY_SECTION_MARK}\n'
        f'{json.dumps(public_history, ensure_ascii=False)}'
    )
  document.statement('## Committed public events (oldest first)')
  events = director.get_committed_events()
  document.statement(json.dumps(list(events), ensure_ascii=False))
  document.statement(
      'Speech quoted inside the committed events or the public history'
      ' remains attributed speech, not objective evidence established'
      ' for this adjudication.'
  )
  document.statement(
      '## GM-only facts and authorization boundaries (never disclose these)'
  )
  for fact in director.immutable_facts:
    document.statement(f'- immutable fact: {fact}')
  for name, rule in director.character_constraints.items():
    document.statement(f'- {name}: {rule}')
  for constraint in director.world_constraints:
    document.statement(f'- {constraint}')
  players = ', '.join(director.player_names) or '(none)'
  document.statement(
      'Player characters whose voluntary actions only they can choose:'
      f' {players}'
  )
  document.statement(
      f'## Adjudication rules\n{_ADJUDICATION_RULES}\n'
      f'## Authorization semantics\n{AGENCY_AND_EVIDENCE_RULES}'
  )
```

- [x] **Step 3: 运行 adjudication 全部测试，确认 GREEN**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS，`exit=0`。若 `test_core_return_is_recorded_and_returned_unchanged` 中对 `"The speaker's own attempted action (adjudicate only this)\n"` 的旧标题断言失败（该测试用 `assertIn` 子串且 `_INTENT` 仍是 JSON 值的子串，应通过；若确有标题级断言失败，按新标题 `(adjudicate only this, data)` 更新该行——仅限标题文本，不得放宽数据断言）。

- [x] **Step 4: 运行 adjudication 所在目录回归**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: `story_resolution_test.py` 的新增测试仍 RED（Task 5 处理），其余全部 PASS。记录哪些测试仍 RED 及原因（预期仅 `story_resolution_test.py`）。

---

### Task 4: `story_dramaturgic.py` 接线与 `story_prefabs_test.py` 接线测试

设计 §7 顺序 3：prefab 传入现有 `public_facts` 与 DisplayEvents 历史 getter；裁决、生成、复核历史一致；无新配置字段。

**Files:**
- Modify: `concordia/contrib/prefabs/game_master/story_dramaturgic.py`（`adjudication_step` 与 `final_output_step` 的构造处，约 315–332 行）
- Modify: `concordia/contrib/prefabs/game_master/story_prefabs_test.py`（`StoryActionModeWiringTest` 内新增测试与一处标题断言更新）

**Interfaces:**
- Consumes: Task 3 的 `make_action_adjudication_step(director, *, public_facts, public_history)`；`display_events.get_pre_act_value`（`ActionSpecIgnored` 缓存取值，无模型调用；RESOLVE 链在 PRE_ACT 阶段内运行，getter 合法）；`gm.get_component('__memory__').add('[event] ...')`（DisplayEvents 扫描 `EVENT_TAG`）；`director.set_state` 的 `recent_events` 字段。
- Produces: 无新生产接口；接线行为（隔离 + 历史 + 无新配置字段）由测试固定。

- [x] **Step 1: 写接线失败测试（RED）**

在 `story_prefabs_test.py` 的 import 区补：

```python
from concordia.contrib.components.game_master import story_action_adjudication
```

在 `StoryActionModeWiringTest` 类内追加三个测试：

```python
  _OLD_SHOWN_FACT = '周律师早在众人面前展示过遗嘱封套。'

  def test_adjudication_isolated_from_upstream_beat_and_conversation(self):
    # The isolated adjudication document carries no upstream beat
    # instruction, no conversation window and no world-constraints
    # component section, while its own conditioning — speaker, initial
    # public facts, GM-only boundaries, the shared authorization rules
    # and the DisplayEvents history — stays complete (design 4.1). The
    # final generation keeps its existing shared-document context.
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        player_names=list(_PLAYERS),
        public_facts=('Both players saw the sealed envelope.',),
    )
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    self.assertNotIn('(beat b1)', adjudication)
    self.assertNotIn('Conversation', adjudication)
    self.assertNotIn('\nWorld constraints', adjudication)
    self.assertIn('## Current speaker\nAlice', adjudication)
    self.assertIn('## Initial public facts', adjudication)
    self.assertIn('Both players saw the sealed envelope.', adjudication)
    self.assertIn('## Public history (data)', adjudication)
    self.assertIn(
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES, adjudication
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    self.assertIn('(beat b1)', final)

  def test_display_history_beyond_director_window_reaches_all_prompts(self):
    # Design 8.1: a committed display fact outside the director's
    # stored window but within DisplayEvents' retrieval range must
    # reach the actual adjudication, generation and review prompts —
    # via the marked history data section, not the window bullets —
    # and generation with review must read the identical history
    # frozen at step entry.
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE) for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    scoring_window = 8
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        player_names=list(_PLAYERS),
        scoring_window=scoring_window,
        public_facts=('Both players saw the sealed envelope.',),
    )
    # Seed the GM memory with 24 committed events: the shown-envelope
    # fact sits at position 1, outside the director's stored window
    # (the last max(scoring_window, 20) events) but well within
    # DisplayEvents' retrieval. Use the actual configured window.
    memory = gm.get_component('__memory__')
    for i in range(24):
      text = self._OLD_SHOWN_FACT if i == 1 else f'第{i}轮排练照常进行。'
      memory.add(f'[event] {text}')
    director = gm.get_component(
        drama_director_lib.DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY,
        type_=drama_director_lib.DramaDirector,
    )
    state = dict(director.get_state())
    window = max(scoring_window, 20)
    state['recent_events'] = [
        f'第{i}轮排练照常进行。' for i in range(max(0, 24 - window), 24)
    ]
    director.set_state(state)
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )

    def read_history(prompt: str) -> str:
      mark = '## Public history (data)'
      index = prompt.index(mark) + len(mark)
      start = prompt.index('"', index)
      value, _ = json.JSONDecoder().raw_decode(prompt[start:])
      return value

    for prompt in (adjudication, final, review):
      self.assertIn(self._OLD_SHOWN_FACT, prompt)
      self.assertIn('## Public history (data)', prompt)
    # The old fact arrives via the history data section, never as a
    # director-window entry, and the director window itself stays.
    mark = '## Committed public events (oldest first)'
    window_data = adjudication.split(mark, 1)[1].lstrip()
    decoded_window, _ = json.JSONDecoder().raw_decode(window_data)
    self.assertNotIn(self._OLD_SHOWN_FACT, decoded_window)
    self.assertIn('第16轮排练照常进行。', adjudication)
    self.assertIn(self._OLD_SHOWN_FACT, read_history(final))
    self.assertEqual(read_history(adjudication), read_history(final))
    self.assertEqual(read_history(final), read_history(review))

  def test_history_getter_is_wired_in_build_not_params(self):
    # The public-history getter is bound at build time to the built
    # DisplayEvents instance; the serializable params keep no callable
    # and no new config field exists (design 4.1).
    params = {
        'name': 'story rules',
        'scene_dicts': _SCENE_DICTS,
        'beat_dicts': _BEAT_DICTS,
        'public_facts': ('Both players saw the sealed envelope.',),
    }
    prefab = story_dramaturgic.GameMaster(params=params)
    prefab.entities = [_build_character(name) for name in _PLAYERS]
    _build_prefab(prefab)
    json.dumps(prefab.params)
    self.assertNotIn('public_history', prefab.params)
```

同时更新既有 `test_speech_and_action_decodes_and_adjudicates_before_final` 中的两处标题断言：

```python
    self.assertIn(f'## Original speech (verbatim)\n{_SPEECH}', adjudication)
```

改为

```python
    self.assertIn(
        '## Original speech (verbatim, data)\n'
        + json.dumps(_SPEECH, ensure_ascii=False),
        adjudication,
    )
```

```python
    self.assertIn(
        "## The speaker's own attempted action (adjudicate only this)\n"
        + _ACTION_INTENT,
        adjudication,
    )
```

改为

```python
    self.assertIn(
        "## The speaker's own attempted action (adjudicate only this,"
        ' data)\n' + json.dumps(_ACTION_INTENT, ensure_ascii=False),
        adjudication,
    )
```

- [x] **Step 2: 运行新测试，确认按预期 RED**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py::StoryActionModeWiringTest -q 2>&1 | tail -10 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 三个新测试中前两个 FAIL（prefab 尚未传 `public_facts`/`public_history`：提示缺少历史数据段或实际公开事实内容；初始事实标题本身可能已存在），第三个 PASS（无新字段是现状）。抄录失败输出。

- [x] **Step 3: 修改 `story_dramaturgic.py` 接线**

`adjudication_step` 构造（原 315–317 行）改为：

```python
    # The adjudication is conditioned on the initial public facts and,
    # via the DisplayEvents pre-act value (bound here, never serialized
    # into params), the retrieved public history — isolation must not shrink
    # the history to the director's scoring window (design 4.1).
    adjudication_step = story_action_adjudication.make_action_adjudication_step(
        director,
        public_facts=self.params.get('public_facts', ()),
        public_history=display_events.get_pre_act_value,
    )
```

`final_output_step` 构造（原 322–332 行）在既有参数后追加同一 getter：

```python
    final_output_step = story_resolution.make_final_output_step(
        director,
        immutable_facts=self.params.get('immutable_facts', ()),
        character_constraints=self.params.get('character_constraints', {}),
        player_names=player_names,
        world_constraints=self.params.get('world_constraints', ()),
        allowed_environment_events=self.params.get(
            'allowed_environment_events', ()
        ),
        public_facts=self.params.get('public_facts', ()),
        public_history=display_events.get_pre_act_value,
    )
```

注：`make_final_output_step` 的 `public_history` 形参由 Task 5 实现；本任务先完成 prefab 侧接线，测试的最终 GREEN 以 Task 5 Step 5 为准。因此本任务验证命令只断言"失败原因不再是接线缺失"——若报 `unexpected keyword argument 'public_history'`（来自 `make_final_output_step`），属预期中间态，记录即可。

- [x] **Step 4: 运行接线测试记录中间态**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py::StoryActionModeWiringTest -q 2>&1 | tail -10 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: `test_adjudication_isolated_from_upstream_beat_and_conversation` 与 `test_display_history_beyond_director_window_reaches_all_prompts` 的失败点移动到最终步骤的历史段（`## Public history (data)` 缺失于 final/review），或因 `make_final_output_step` 尚无 `public_history` 形参而 TypeError——两者均为预期中间态，抄录输出。

---

### Task 5: `story_resolution.py` 共享授权规则与规则 2/4/5/7/8 更新

设计 §7 顺序 4：共享授权规则；更新规则 2/4/5/7/8 的边界、记录语义和复核任务；不改变 JSON schema。本任务把 Task 2 的 resolution 断言与 Task 4 的接线断言全部转 GREEN。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution.py`（import 区、`_SEMANTIC_RULES`、`make_final_output_step`、`_build_prompt`、复核 open_question 文案、模块与函数 docstring）

**Interfaces:**
- Consumes: `story_action_adjudication.AGENCY_AND_EVIDENCE_RULES` 与 `story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK`（import，依赖方向 `story_resolution -> story_action_adjudication -> drama_director`，不引入反向导入）。
- Produces: `make_final_output_step(..., public_history: Callable[[], str] | None = None)`（getter 入口取值一次，同一字符串交给生成与复核）；更新后的 `_SEMANTIC_RULES`（Task 2 的全部指纹来自此文本）。

- [x] **Step 1: 更新 import 区与规则文本**

import 区补：

```python
from concordia.contrib.components.game_master import story_action_adjudication
```

完整替换 `_SEMANTIC_RULES`（保留规则 1/3/6 的既有校准文本，更新 2/4/5/7/8 并追加共享段）：

```python
_SEMANTIC_RULES = f"""## Rules
1. Preserve the active player's original speech and speaker. Speech is an
   utterance, not proof of the claims it contains. A request, accusation,
   proposal, threat, intention or alleged past action must not become an
   established fact or executed action merely because it is spoken.
2. Player characters' voluntary choices are their own. Narration must not
   invent any player's voluntary action, confession, promise, consent,
   agreement, signature, evidence handover, movement or additional
   dialogue. The only exception is the current speaker's own action: an
   action they explicitly proposed this turn and that was adjudicated in
   the resolution evidence, together with its result when that result is
   itself legal under these rules (rule 7 governs it). An involuntary
   visible reaction may be described without inferring consent.
   Previously committed actions of other players, merely naming a
   player, and non-autonomous reactions do not by themselves constitute
   a new choice: judge by whether a new voluntary act occurs. A
   narration asserting that several players agreed, consented, promised
   or signed requires each named player's own committed choice or
   attributed speech; collective agreement without such sources is
   invented.
3. GM-only knowledge is not public knowledge. Do not reveal a character's
   private information through narration or another character's invented
   speech. A character may voluntarily disclose their own secret in their
   original speech only if this also respects their hard constraint. Do not
   reject mere unverified accusations or denials that remain attributed speech.
4. Respect every character's hard constraint, immutable facts, world rules
   and committed public events. Public knowledge is grounded only in
   initial public facts, the public history, committed public events,
   attributed original speech, and the adjudicated result of the current
   speaker's own explicitly proposed action recorded in the resolution
   evidence when that result is itself legal. GM-only constraints are not
   public evidence. Candidate events and future beats are not evidence.
   Fidelity to recorded evidence never overrides an authorization
   boundary.
5. Any new NPC or environmental event must use an explicitly whitelisted
   means, grounded in the world and history. An empty whitelist permits
   no new event. Whitelisting never authorizes player choices or
   otherwise forbidden leaks. The whitelist governs which events and
   material consequences may occur, not their exact wording: a faithful
   paraphrase of the same change (heavier rain for a strengthening
   storm) is legal, while adding material consequences beyond the
   whitelisted event (a lightning injury, a recovered document fragment)
   remains illegal.
6. The `resolved_event` in the canonical-event data section was
   assembled programmatically from the original speech and the listed
   stage directions, so its speech portion is verbatim by construction.
   Programmatic assembly is not semantic approval: judge whether the
   stage directions, and the assembled event as a whole, are legal
   under the resolution evidence and every rule above. Stage
   directions must not repeat or paraphrase dialogue, certify spoken
   claims as true, infer joint agreement, or convert a proposal into
   an executed action. Empty stage directions are valid when only
   speech occurs.
7. The `action_result` in the resolution evidence is the raw outcome
   returned by the core resolver, not an authorization or semantic
   approval. Check that outcome itself against the speaker's own
   attempted action, the public evidence and every agency, secrecy and
   evidence constraint above; coming from the core resolver exempts it
   from nothing. Reject the turn if it contains an unauthorized fact —
   another player's new voluntary act, fabricated physical evidence, an
   undisclosed secret — even when the final narration omits that fact.
   These action-result checks apply only when a non-empty
   `action_result` exists; a turn without action intent must not be
   faulted for lacking adjudication, and pure dialogue must not be
   misjudged as missing action evidence. For an outcome that is legal,
   preserve its material success, failure or blocked result in the
   canonical event: never drop, soften or reverse it, and never write
   an executed act back as a future plan. Never repair or silently
   discard an illegal outcome to obtain approval; if the outcome itself
   overreaches, the whole turn is rejected. Recording one's own claim
   with a clear attribution is the act of recording, not certification:
   writing down an unverified claim with its source is legal, subject
   to every character's hard constraints. However, certifying
   agreement without each involved player's own sourced choice, or
   warranting unverified contents as established objective facts,
   is an illegal fact escalation. Accurately recording already
   established facts is legal; do not reject a grounded agreement
   merely because it is recorded or certified.
8. An intervention candidate must introduce a legal new NPC or
   environmental event grounded in the whitelist, world and committed
   history. Retain that new event and its material consequences in the
   final stage directions; faithful paraphrase is allowed — the rule-5
   meaning boundary applies, not word-for-word matching. You need
   not repeat original speech, established history, or unchanged
   background states such as an ongoing disagreement. Omitting such a
   restatement is not dropping the intervention, provided the final
   event does not contradict it. Never use this exception to omit an
   adjudicated action result, invent player choices, disclose secrets,
   fabricate evidence or remove a material new consequence. A candidate
   containing only restatements is not a successful intervention.

## Authorization semantics (shared with the adjudication step)
{story_action_adjudication.AGENCY_AND_EVIDENCE_RULES}
"""
```

- [x] **Step 2: `make_final_output_step` 增加 `public_history`**

函数签名在 `public_facts` 之后追加关键字参数并在 docstring 的 Args 区补一条：

```python
    public_history: Callable[[], str] | None = None,
```

```python
      public_history: zero-argument getter supplying the public history
        (the DisplayEvents pre-act value in the story prefab); it is
        read exactly once per step entry and the identical string is
        passed to both the generation prompt and the review as inert
        JSON data. When None the step keeps conditioning on the
        director's committed-event window only.
```

`step` 函数体开头（读取 evidence 之后、生成调用之前）加一次性取值：

```python
    # Fetched once at step entry: generation and review must see the
    # identical history, never a long history for one and the
    # director's short window for the other (design 4.1).
    history = public_history() if public_history is not None else None
```

`chain.open_question` 的 `_build_prompt(...)` 调用追加 `public_history=history`。复核文档构造处（`review.statement('## Committed public events...')` 之后、`_EVIDENCE_SECTION_MARK` 之前）插入：

```python
    if history is not None:
      review.statement(story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK)
      review.statement(json.dumps(history, ensure_ascii=False))
```

复核 `open_question` 文案替换为（JSON schema 不变）：

```python
    verdict = review.open_question(
        'Evaluate all rules above. Output only a JSON object with exactly '
        '"valid" (a JSON boolean) and "reason" (a string explaining the '
        'decision). Reject if any rule fails. Do not rewrite the event. '
        'If rejecting, name the specific offending text, whether it comes '
        'from the action result or the narration, and which source is '
        'missing.',
        max_tokens=1000,
        terminators=(),
    )
```

`_build_prompt` 签名追加 `public_history: str | None = None`，docstring 补参数说明；在 `'## Initial public facts'` 段之后、`'## Committed public events'` 段之前插入：

```python
  if public_history is not None:
    parts.append(
        f'{story_action_adjudication.PUBLIC_HISTORY_SECTION_MARK}\n'
        f'{json.dumps(public_history, ensure_ascii=False)}\n'
    )
```

（实现时按该文件现有的 f-string 拼接风格融入，`parts` 仅为示意——以现有 return 表达式的相邻段落写法为准。）

模块 docstring 与 `make_final_output_step` docstring 中把 `action_result` 描述为 "the core-adjudicated action result recorded by the adjudication step" 的句子补明确：它是**核心实际返回的待审裁决结果**（pending review），仅在本身合法且整轮通过评审后才能提交。

- [x] **Step 3: 运行 resolution 测试，确认 GREEN**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master/story_resolution_test.py -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS（含 Task 2 Step 7–9 的新测试与改指纹测试）。

- [x] **Step 4: 运行 adjudication 回归**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -q 2>&1 | tail -3 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS（import 方向 `story_resolution -> story_action_adjudication` 不得造成循环导入）。

- [x] **Step 5: 运行 prefab 接线测试，确认 GREEN**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS（含 Task 4 的三个新测试；`test_display_history_beyond_director_window_reaches_all_prompts` 证明裁决/生成/复核三方收到窗口外的历史且生成与复核读取同一冻结字符串）。

- [x] **Step 6: 运行 contrib 全量回归**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest concordia/contrib -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS，`exit=0`。

---

### Task 6: 端到端提交/拒绝/预算/恢复验证

设计 §7 顺序 5 + §8 条目 4/6/7/8：真实核心路径与当前证据驱动的端到端验证，保留现有核心变异验证。

**Files:**
- Modify: `examples/story_enactment/testing_stubs.py`（`PromptStubModel.__init__` 增加 `stage_omissions`；`_stage_directions_output` 应用删除）
- Modify: `examples/story_enactment/testing_stubs_test.py`（新增 omission 路由测试）
- Modify: `examples/story_enactment/story_enactment_test.py`（import 区补 `import pathlib`；`ActionChainBudgetTest` 之后新增测试类）

**Interfaces:**
- Consumes: Task 3/5 的生产接口；既有助手 `_StateDirTestCase`、`_ActionChainTestCase._action_config/_run_with_stub`、`_ATTEMPT_QUESTION`、`_FINAL_OUTPUT_MARK`、`_REVIEW_MARK`、`_last_question_block`、`_prompts_routed_to`、`_prompts_containing`、`_fdx_paragraphs`、`_entity_memory_text`、`self.turn_log/envelope/summary/read_script/restored`；`budget_wrapper.BudgetExhaustedError`（已 import）。
- Produces: `PromptStubModel(..., stage_omissions: Collection[str] = ())`（F12 式"旁白删句"探针）；example 层的提交边界、诊断并存、预算与恢复断言。

- [x] **Step 1: `testing_stubs.py` 增加 `stage_omissions`**

`__init__` 关键字参数区（`action_result` 之后）追加，并同步 docstring：

```python
      stage_omissions: Collection[str] = (),
```

```python
      stage_omissions: substrings deleted from the adjudicated action
        result when the stub narrates it in the stage-directions track
        — the F12 probe of a narration that drops the offending
        sentence while the raw outcome keeps it. Everything else stays
        evidence-derived.
```

`self._stage_omissions = tuple(stage_omissions)` 存入实例。`_stage_directions_output` 中 narrate action_result 的分支改为：

```python
      action_result = evidence.get('action_result')
      if isinstance(action_result, str) and action_result:
        narrated = action_result
        for omission in self._stage_omissions:
          narrated = narrated.replace(omission, '')
        if narrated.strip():
          parts.append(narrated)
```

- [x] **Step 2: `testing_stubs_test.py` 增加 omission 测试**

在 `PromptStubRoutingTest` 类内追加：

```python
  def test_stage_omissions_remove_configured_sentences(self):
    stub = testing_stubs.PromptStubModel(
        stage_directions='',
        stage_omissions=('露出烧灼纸灰残片。',),
    )
    evidence = {
        'speech': '我先看看那个花瓶。',
        'action_intent': '取下花瓶。',
        'action_result': (
            '取下花瓶。 Because of that, 花瓶碎裂，露出烧灼纸灰残片。'
        ),
        'intervention': None,
    }
    prompt = (
        'Exercise: Output a JSON object with exactly one string field:\n'
        '- "stage_directions": visible outcomes only.\n'
        f'{testing_stubs.EVIDENCE_SECTION_MARK}\n'
        f'{json.dumps(evidence, ensure_ascii=False)}\n'
        'Output only the JSON object, no other text.'
    )
    response = stub.sample_text(prompt)
    self.assertEqual(
        json.loads(response)['stage_directions'],
        '取下花瓶。 Because of that, 花瓶碎裂，',
    )
```

（若该文件的测试以 `self.stub` fixture 风格组织，按既有风格融入；断言值不变。）

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest examples/story_enactment/testing_stubs_test.py -q 2>&1 | tail -3 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS。

- [x] **Step 3: `story_enactment_test.py` 新增 `ActionAuthorizationBoundaryTest`**

import 区补 `import pathlib`（json/os/tempfile/mock/budget_wrapper 均已存在）。在 `ActionChainBudgetTest` 类之后新增：

```python
class ActionAuthorizationBoundaryTest(_ActionChainTestCase):
  """Pending-review adjudication outcomes and the commit boundary.

  Scripted stub verdicts prove the plumbing — what reaches the review,
  what a rejection freezes, which traces are archived — never that a
  real model detects the violation (design sections 4.3, 8 and 9.1).
  """

  _INTENT = '林雪取下堂屋条案上的旧花瓶。'
  _LEGAL_ANSWER = '花瓶被取下，完好无损。'
  _LEGAL_RAW = f'{_INTENT} Because of that, 花瓶被取下，完好无损。'
  _SECOND_INTENT = '陈默走到堂屋门边停下。'
  _ILLEGAL_SENTENCE = '花瓶碎裂，露出烧灼纸灰残片。'
  _ILLEGAL_RAW = f'{_SECOND_INTENT} Because of that, {_ILLEGAL_SENTENCE}'
  _REJECT = json.dumps({
      'valid': False,
      'reason': 'The adjudicated outcome fabricated paper ash.',
  })
  _APPROVE = '{"valid": true, "reason": "scripted approval"}'

  class _TwoTurnStub(testing_stubs.PromptStubModel):
    """Legal first adjudication and review; illegal second raw outcome."""

    def __init__(self, **kwargs):
      super().__init__(
          action_intents={
              '林雪': ActionAuthorizationBoundaryTest._INTENT,
              '陈默': ActionAuthorizationBoundaryTest._SECOND_INTENT,
          },
          stage_directions='',
          **kwargs,
      )
      self._attempt_answers = 0

    def _route_text(self, prompt):
      block = _last_question_block(prompt)
      if _ATTEMPT_QUESTION in block:
        self._attempt_answers += 1
        if self._attempt_answers == 1:
          return ActionAuthorizationBoundaryTest._LEGAL_ANSWER
        return ActionAuthorizationBoundaryTest._ILLEGAL_SENTENCE
      if '# Story resolution semantic review' in prompt:
        if self._attempt_answers > 1:
          return ActionAuthorizationBoundaryTest._REJECT
      return super()._route_text(prompt)

  def test_illegal_raw_result_rejected_even_when_stage_omits_it(self):
    # F11/F12 end to end (design 9.1): turn 1's legal outcome commits
    # and flows through every export; turn 2's raw adjudicated outcome
    # fabricates paper ash, the stage directions delete the bad
    # sentence, and the scripted review still rejects the whole turn —
    # the turn stops at the last commit with directives and cooldown
    # untouched (design 8.7).
    config = self._action_config(total_turn_limit=2)
    stub = self._TwoTurnStub(
        review_response=self._APPROVE,
        stage_omissions=(self._ILLEGAL_SENTENCE,),
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'error')
    self.assertIn('fabricated paper ash', result.error_message or '')
    self.assertEqual(result.committed_turn_id, 1)
    records = self.turn_log().load()
    self.assertEqual([r['turn_id'] for r in records], [1])
    self.assertEqual(self.envelope()['committed_turn_id'], 1)
    # Turn 1's approved outcome flowed through canonical event, JSONL,
    # checkpoint and FDX; the illegal candidate reached none of them.
    self.assertIn('花瓶被取下', records[0]['resolved_event'])
    self.assertEqual(_fdx_paragraphs(self.state_dir, 'Action')[-1], self._LEGAL_RAW)
    self.assertNotIn('纸灰', self.read_script('script.fdx'))
    sim, director = self.restored(config)
    self.assertNotIn('纸灰', _entity_memory_text(sim, '王婶'))
    state = director.get_state()
    self.assertIsNone(state['directive'])
    self.assertIsNone(state['last_directive_executed_turn'])
    self.assertEqual(state['repetition_check_count'], 0)
    # The second review judged the evidence carrying the illegal raw
    # result — the core question ran once per action turn, exactly.
    reviews = _prompts_containing(stub, _REVIEW_MARK)
    self.assertEqual(len(reviews), 2)
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 2)
    second_evidence = testing_stubs.read_evidence_section(reviews[1])
    assert second_evidence is not None
    self.assertEqual(second_evidence['action_result'], self._ILLEGAL_RAW)
    self.assertIn('纸灰', second_evidence['action_result'])

  def test_normal_action_turn_call_counts_are_unchanged(self):
    # Design 8.8: a normal non-empty action turn costs one core
    # adjudication, one final generation and one independent review —
    # the rewiring added no calls. Budget assertions align on call
    # positions, not legacy global hard-coded totals.
    config = self._action_config(total_turn_limit=2)
    # A separate always-legal stub: do not reuse the fixture whose
    # second outcome and second review deliberately reject the turn.
    stub = testing_stubs.PromptStubModel(
        action_intents={
            '林雪': self._INTENT,
            '陈默': self._SECOND_INTENT,
        },
        action_result='当前角色完成了自己提出的动作，没有额外事件。',
        stage_directions='',
        review_response=self._APPROVE,
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 2)
    self.assertEqual(len(_prompts_routed_to(stub, _FINAL_OUTPUT_MARK)), 2)
    self.assertEqual(len(_prompts_containing(stub, _REVIEW_MARK)), 2)
    total_calls = len(stub.text_prompts) + len(stub.choice_prompts)
    self.assertEqual(result.used_model_attempts, total_calls)
    self.assertEqual(self.envelope()['used_model_attempts'], total_calls)

  def test_budget_error_at_core_archives_partial_trace_path_a(self):
    # Design 8.6/8.8: exhaustion at the core question leaves a partial
    # `action_adjudication` trace in the private archive, no fabricated
    # result, path A (nothing commits), and the failed call's
    # consumption survives in the usage sidecar. A budget-exhausted
    # directory must refuse resume under the existing runner contract.
    class _BudgetAtCore(testing_stubs.PromptStubModel):

      def _route_text(self, prompt):
        if _ATTEMPT_QUESTION in _last_question_block(prompt):
          raise budget_wrapper.BudgetExhaustedError(
              'budget spent at adjudication'
          )
        return super()._route_text(prompt)

    config = self._action_config(total_turn_limit=2)
    stub = _BudgetAtCore(
        action_intents={'林雪': self._INTENT}, stage_directions=''
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'budget_limit')
    self.assertTrue(result.budget_exhausted)
    self.assertEqual(result.committed_turn_id, 0)
    self.assertEqual(self.turn_log().load(), [])
    self.assertGreaterEqual(result.used_model_attempts, 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 0)
    usage = json.loads(
        pathlib.Path(self.state_dir, 'process_usage.json').read_text(
            encoding='utf-8'
        )
    )
    self.assertGreaterEqual(usage['used_model_attempts'], 1)
    summary = json.loads(
        sorted(
            pathlib.Path(self.state_dir, 'process_summaries').glob('*.json')
        )[-1].read_text(encoding='utf-8')
    )
    uncommitted = summary['uncommitted_resolution']
    assert uncommitted is not None
    self.assertEqual(uncommitted['stage'], 'action_adjudication')
    self.assertIn('BudgetExhaustedError', uncommitted['error'])
    self.assertNotIn('花瓶', self.read_script('script.fdx'))
    # Do not turn a budget stop into a resumable API error or reset its
    # budget. Resume refusal precedes even building the next model.
    good = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._INTENT},
        action_result=self._LEGAL_ANSWER,
        stage_directions='',
    )
    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(good, ())
    ) as factory:
      with self.assertRaisesRegex(ValueError, 'budget-exhausted'):
        run_lib.run(self.state_dir, run_config=config, resume=True)
    factory.assert_not_called()

  def test_api_error_at_core_preserves_usage_on_resume(self):
    # A non-budget API error is resumable. Its failed call remains in
    # the sidecar and the next invocation's initial cumulative usage.
    class _APIAtCore(testing_stubs.PromptStubModel):

      def _route_text(self, prompt):
        if _ATTEMPT_QUESTION in _last_question_block(prompt):
          raise testing_stubs.TransientAPIError('provider failed at core')
        return super()._route_text(prompt)

    config = self._action_config(total_turn_limit=2)
    stub = _APIAtCore(
        action_intents={'林雪': self._INTENT}, stage_directions=''
    )
    first = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(first.reason, 'error')
    self.assertFalse(first.budget_exhausted)
    self.assertEqual(first.committed_turn_id, 0)
    self.assertEqual(len(_prompts_routed_to(stub, _ATTEMPT_QUESTION)), 1)
    self.assertEqual(self.envelope()['used_model_attempts'], 0)
    usage = json.loads(
        pathlib.Path(self.state_dir, 'process_usage.json').read_text(
            encoding='utf-8'
        )
    )
    self.assertEqual(usage['used_model_attempts'], first.used_model_attempts)
    self.assertGreater(first.used_model_attempts, 0)
    good = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._INTENT},
        action_result=self._LEGAL_ANSWER,
        stage_directions='',
    )
    with mock.patch.object(
        run_lib, 'make_language_model', return_value=(good, ())
    ):
      second = run_lib.run(self.state_dir, run_config=config, resume=True)
    self.assertEqual(second.reason, 'step_limit')
    self.assertEqual(second.committed_turn_id, 2)
    last_summary = json.loads(
        sorted(
            pathlib.Path(self.state_dir, 'process_summaries').glob('*.json')
        )[-1].read_text(encoding='utf-8')
    )
    self.assertEqual(
        last_summary['used_model_attempts_at_start'], first.used_model_attempts
    )

  def test_success_trace_and_review_failure_trace_coexist(self):
    # Design 8.6: the successful adjudication trace and the later
    # review-failure trace stay side by side in the private history,
    # the latest stage reflecting the true failure position.
    config = self._action_config(total_turn_limit=1)
    stub = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._INTENT},
        action_result=self._LEGAL_ANSWER,
        stage_directions='',
        review_response=self._REJECT,
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'error')
    self.assertEqual(result.committed_turn_id, 0)
    summary = json.loads(
        sorted(
            pathlib.Path(self.state_dir, 'process_summaries').glob('*.json')
        )[-1].read_text(encoding='utf-8')
    )
    uncommitted = summary['uncommitted_resolution']
    assert uncommitted is not None
    self.assertEqual(
        [record['stage'] for record in uncommitted['history']],
        ['action_adjudication', 'semantic_review'],
    )
    self.assertIsNone(uncommitted['history'][0]['error'])
    self.assertIsNotNone(uncommitted['history'][-1]['error'])
    self.assertIn('花瓶被取下', uncommitted['action_result'])
    # Private diagnostics never reach the players or the script.
    self.assertNotIn('action_adjudication', self.read_script('script.fdx'))
```

- [x] **Step 4: 运行新端到端测试**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest examples/story_enactment/story_enactment_test.py::ActionAuthorizationBoundaryTest -q 2>&1 | tail -8 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 五个测试全部 PASS。预算耗尽路径验证拒绝恢复、模型工厂零调用；非预算 API 错误路径验证允许恢复且累计计入失败调用。不得修改 runner、重置预算或把 `budget_limit` 改回 `error` 来让测试通过。

- [x] **Step 5: 运行 example 全量回归**

```bash
source venv/bin/activate
set -o pipefail
action_auth_test_rc=0
pytest examples -q 2>&1 | tail -5 || action_auth_test_rc=$?
printf 'exit=%s\n' "$action_auth_test_rc"
exit "$action_auth_test_rc"
```

Expected: 全部 PASS（既有核心变异验证——`ActionPipelineIntegrationTest`、`ActionBoundaryIntegrationTest`、`ActionChainBudgetTest` 等——不受影响；若既有预算测试因提示文本变化失败，核对失败原因确属提示指纹而非调用数变化后更新指纹，调用数断言一律不得放宽）。

---

### Task 7: 文档、勘误、交付记录与全量检查

设计 §7 顺序 6 + §8 收尾。

**Files:**
- Modify: `concordia/contrib/components/README.md`（story_action_adjudication 与 story_resolution 词条）
- Modify: `concordia/contrib/prefabs/README.md`（story_dramaturgic 词条）
- Modify: `examples/story_enactment/README.md`（行动裁决与评审边界说明）
- Modify: `docs/superpowers/specs/2026-10-17-story-enactment-action-authorization-review-design.md`（第 4 行状态）
- Create: `docs/superpowers/notes/2026-10-17-story-enactment-action-authorization-review.md`（交付记录，含勘误与新评测协议指引）

**Interfaces:**
- Consumes: Task 1 的基线快照；各任务的 RED/GREEN 输出记录。
- Produces: 交付记录（含 action_mode_control README 勘误、设计 §9 后续评测协议指引）。

- [x] **Step 1: 更新三个 README**

`concordia/contrib/components/README.md`：`story_action_adjudication` 词条补——独立裁决文档（不继承上游节拍/Conversation）、`public_facts`/`public_history` 参数与 DisplayEvents getter 约定、返回值为待审裁决结果而非授权、成功与失败均归档 `stage='action_adjudication'` 私有 trace；`story_resolution` 词条补——规则 2/4/5/7/8 的授权与记录语义、复核同时检查裁决结果本身与 canonical event、`public_history` 入口冻结一次、评审 JSON schema 不变。

`concordia/contrib/prefabs/README.md`：`story_dramaturgic` 词条补——build 内把 `display_events.get_pre_act_value` 绑定为裁决与最终步骤的公开历史 getter，历史范围不因隔离缩小，无新增可序列化配置字段。

`examples/story_enactment/README.md`：在行动模式/resolution 说明附近补一段——核心返回值是待审结果；隔离裁决上下文；一次最终复核同时检查合法性与保真性；诊断私有归档于 checkpoint/JSONL 私有字段与 process summary；确定性测试证明接线与失败边界，不证明真实模型语义质量。

- [x] **Step 2: 准备设计文档状态回填**

先准备以下状态文本；只有本任务 Step 4–6 的检查与差异审查完成并符合验收条件后，才替换设计文档第 4 行的“设计建议，未实施”。失败或未完成时保留未验收状态，不提前宣布工程验收通过：

```markdown
状态：已实施（确定性工程验收，实施记录见
../notes/2026-10-17-story-enactment-action-authorization-review.md）；
真实模型评测（设计 §9）未执行，属后续独立工作。
```

- [x] **Step 3: 写交付记录（含勘误）**

创建 `docs/superpowers/notes/2026-10-17-story-enactment-action-authorization-review.md`，结构对齐 `2026-10-17-story-enactment-run4-review-and-progress.md`：

```markdown
# Story enactment 行动授权与语义评审边界修补实施记录

日期：2026-10-17。方案：
../plans/2026-10-17-story-enactment-action-authorization-review.md，设计：
../specs/2026-10-17-story-enactment-action-authorization-review-design.md。
按用户授权执行 Task 1–7；全程未提交 git，未回滚同事未提交的进行中修改
（开工基线快照：/tmp/opencode/action-auth-baseline/，含 git status、git diff、
已跟踪/未跟踪源码与 docs 内容快照及 run_results 哈希清单）；未修改
Concordia 核心代码；未发起真实模型调用。

## 交付概述
（一段：独立裁决文档、public_facts/public_history 接线、待审结果语义、
共享授权常量与规则 2/4/5/7/8 更新、诊断归档、端到端回归；列出修改文件
清单。）

## 各任务 RED/GREEN 证据
| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| Task 2 裁决断言 | 粘贴实际失败输出与退出码 | Task 3 后全 PASS |
| Task 2 评审断言 | 粘贴实际失败输出与退出码 | Task 5 后全 PASS |
| Task 4 接线断言 | 粘贴实际失败输出 | Task 5 后全 PASS |
| Task 6 端到端 | — | ActionAuthorizationBoundaryTest 全 PASS |

## 勘误：action_mode_control 对照报告
原 `run_results/story_enactment/action_mode_control/README.md`（只读保留，
不覆写）存在两处需更正的表述：
1. 其把已提交轮数 3（Gemini）与 7（Luna）写作失败回合号；实际是
   checkpoint 停留的已提交轮数，失败发生在**下一**回合（第 4/第 8 回合
   的语义复核拒绝），失败候选未进入正式回合或 FDX。原产物链接：
   `summary.json`、`story_turns.jsonl`、`process_summaries/`。
2. 其把 Luna 第 8 回合旁白概括为 "all parties agreed"；实际旁白与裁决
   结果写的是律师补记"今晚暂停处理"，"各方已同意"仅出现在律师原始对白
   中。该回合按设计 §2.2 保留为待人工定案的歧义样本，不计入固定样本
   通过率。
两组动作运行过早停止，不足以证明行动模式仍存在相似长期停滞。

## 真实模型评测：未执行
设计 §9 的固定复核样本（F1–F16，16 × 2 × 3 = 96 次）、固定行动裁决输入
（4 × 2 × 3 = 24 次）与完整动作模式对照（阶段 C gate 另需用户授权）均未
发起，属后续独立工作。本记录全部结果来自 MockModel/PromptStubModel
确定性测试，证明分支与接线，不构成真实模型语义质量验收。

## 全量检查（每条独立运行、单独捕获退出码）
| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `pytest concordia` | ? | ? |
| `pytest examples` | ? | ? |
| `pylint --errors-only -j 1 concordia` | ? | ? |
| `pyrefly check concordia` | ? | ?（既有 4 项依赖错误单列：vllm missing-import ×3、FastMCP missing-attribute ×1） |
| `git diff --check` | ? | ? |

## 验收确认清单
| 确认项 | 命令 / 证据 | 结论 |
|---|---|---|
| 核心目录零修改 | `git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` | 预期空输出 |
| run_results 只读 | `find run_results -type f -print0 \| sort -z \| xargs -0 sha256sum \| diff - /tmp/opencode/action-auth-baseline/run_results-hashes.txt` | 预期无差异 |
| 既有 dirty 基线保留与修改范围 | Task 7 Step 5 的内容快照差异包；未授权文件哈希不变；逐项审阅允许文件的基线到当前 diff | ? |
| 调用数未增加 | `ActionAuthorizationBoundaryTest::test_normal_action_turn_call_counts_are_unchanged` | 通过 |
| 拒绝停在上一提交 | `test_illegal_raw_result_rejected_even_when_stage_omits_it` | 通过 |
```

（`?` 由执行者以实际输出回填；表格结构保持。）

- [x] **Step 4: 逐条独立运行全量检查并记录退出码**

```bash
source venv/bin/activate && pytest concordia
```

```bash
source venv/bin/activate && pytest examples
```

```bash
source venv/bin/activate && pylint --errors-only -j 1 concordia
```

```bash
source venv/bin/activate && pyrefly check concordia
```

```bash
source venv/bin/activate && git diff --check
```

每条单独运行、单独记录退出码。Expected: 前两条 0；pylint 0（无输出）；pyrefly 允许仅既有 4 项依赖错误（`vllm_model.py` missing-import ×3、`file_reader_server.py` FastMCP missing-attribute ×1），出现任何**新** pyrefly 错误即回改；`git diff --check` 0。

- [x] **Step 5: 内容差异、核心零修改与 run_results 只读验证**

以原文件内容比较，不能以 status 行是否新增推断修改范围。先运行下面的范围检查，产出基线到当前 diff 包与实际修改的 Python 文件清单：

```bash
source venv/bin/activate
python - <<'PY'
import difflib
import hashlib
import json
from pathlib import Path
import subprocess

baseline = Path('/tmp/opencode/action-auth-baseline')
before = json.loads((baseline / 'content-hashes.json').read_text(encoding='utf-8'))
names = set(filter(None, subprocess.check_output([
    'git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z',
]).decode().split('\0')))
names.update(str(p) for p in Path('docs/superpowers').rglob('*') if p.is_file())
after = {
    name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
    if Path(name).is_file() else None
    for name in names | before.keys()
}
changed = sorted(name for name in after if before.get(name) != after[name])
allowed = {
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/components/game_master/story_action_adjudication_test.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_resolution_test.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/prefabs/game_master/story_dramaturgic.py',
    'concordia/contrib/prefabs/game_master/story_prefabs_test.py',
    'examples/story_enactment/testing_stubs.py',
    'examples/story_enactment/testing_stubs_test.py',
    'examples/story_enactment/story_enactment_test.py',
    'concordia/contrib/components/README.md',
    'concordia/contrib/prefabs/README.md',
    'examples/story_enactment/README.md',
    'docs/superpowers/plans/2026-10-17-story-enactment-action-authorization-review.md',
    'docs/superpowers/specs/2026-10-17-story-enactment-action-authorization-review-design.md',
    'docs/superpowers/notes/2026-10-17-story-enactment-action-authorization-review.md',
}
unexpected = set(changed) - allowed
assert not unexpected, f'Unexpected content changes: {sorted(unexpected)}'
patch = []
for name in changed:
  old_file = baseline / 'files' / name
  old = old_file.read_text(encoding='utf-8', errors='replace') if old_file.is_file() else ''
  new_file = Path(name)
  new = new_file.read_text(encoding='utf-8', errors='replace') if new_file.is_file() else ''
  patch.extend(difflib.unified_diff(
      old.splitlines(keepends=True), new.splitlines(keepends=True),
      fromfile=f'baseline/{name}', tofile=f'current/{name}',
  ))
(baseline / 'task-changes.patch').write_text(''.join(patch), encoding='utf-8')
(baseline / 'changed-files.json').write_text(json.dumps(changed, indent=2), encoding='utf-8')
python_files = [name for name in changed if name.endswith('.py') and Path(name).is_file()]
(baseline / 'changed-python-files.txt').write_text(''.join(name + '\n' for name in python_files), encoding='utf-8')
print(f'Content changes: {len(changed)}; Python files: {len(python_files)}')
PY
```

Expected: 未授权文件（包含两个原本未跟踪的 repetition 文件、核心文件）内容不变。**范围检查通过不等于已证明允许文件没有误回滚**：审查者必须阅读 `task-changes.patch`，确认各 hunk 属于本计划且保留同事既有修改；`drama_director.py` 如发生变化，只能为本计划允许的 docstring 更正。交付记录列出实际审查结论，不能仅凭 status 相同勾选。

```bash
source venv/bin/activate
set -o pipefail
git diff --exit-code --quiet -- concordia/components concordia/document concordia/environment concordia/typing || exit $?
if test -f /tmp/opencode/action-auth-baseline/run_results-hashes.txt; then
  find run_results -type f -print0 | sort -z | xargs -0 -r sha256sum \
    | diff - /tmp/opencode/action-auth-baseline/run_results-hashes.txt || exit $?
  printf 'READ-ONLY-OK\n'
else
  cat /tmp/opencode/action-auth-baseline/run-results-unavailable.txt
  printf 'No historical run_results baseline; read-only verification unavailable\n'
fi
```

Expected: 核心目录检查退出 0；有运行基线时输出 `READ-ONLY-OK`。无基线时如实记录无法验证，不能声称已通过只读检查。结果抄入交付记录。

- [x] **Step 6: 格式检查收尾**

对 Step 5 按内容差异得出的全部 Python 文件运行检查，包括开工时已经标为 ` M` 的文件；不从 status 行差异推导清单：

```bash
source venv/bin/activate
python - <<'PY'
from pathlib import Path
import subprocess

files = Path('/tmp/opencode/action-auth-baseline/changed-python-files.txt').read_text(encoding='utf-8').splitlines()
assert files, 'Expected Python implementation changes; check the content audit'
codes = []
for command in (['pyink', '--check'], ['isort', '--check-only']):
  code = subprocess.run(command + files, check=False).returncode
  print(f'{command[0]} exit={code}')
  codes.append(code)
raise SystemExit(0 if all(code == 0 for code in codes) else 1)
PY
```

Expected: 全部通过；修复格式或其他内容后重跑 Step 5–6，更新实际文件清单与差异包，不能用修复前的差异包作最终证明。

---

## Self-Review 记录

- **2026-10-17 审阅后修订**：记录认证改为“缺少来源才拒绝”，补有来源协议记录及合法环境广播正例；逐例对齐角色/意图/结果/历史；修正 JSON 历史与空对白断言、窗口数据编码及 `max(scoring_window, 20)` 说明；两个动作回合分别配置角色自身意图，正常调用数使用独立合法 stub；预算耗尽验证拒绝恢复，API 错误另测累计消耗后恢复；新增 API 异常与 KeyboardInterrupt 的原异常/partial trace 验证；所有测试管道捕获失败退出码；基线覆盖未跟踪源码和被忽略 docs，以内容差异确定范围及格式检查清单。删除无效 `source` 命令，技能流程改为环境可用时选用。
- **本次修订验证边界**：仅修改本计划与设计文档的窗口说明，未实施生产代码。将计划中的行动实现/测试片段加载到临时进程，使用现有确定性 stub 和既有 runner 验证：行动及端到端 12 个测试通过，规则指纹及正反例路由 4 个测试通过；这是计划片段探针，不是生产实现、全量工程验收或真实模型质量验收。实施时仍须执行各任务的 RED/GREEN 和 Task 7 全量检查。
- **Spec 覆盖**：设计 §4.1（独立文档/公开事实/历史 getter/数据 JSON 编码/历史不缩小/getter 缺省回退/最终步骤入口冻结）→ Task 3 + Task 5 + Task 4 接线与 `test_public_history_frozen_once_reaches_both_prompts`、`test_display_history_beyond_director_window_reaches_all_prompts`；§4.2（自身动作因果范围）→ `AGENCY_AND_EVIDENCE_RULES` 文本（Task 3 Step 1）与 `_SHARED_SCOPE_MARK` 断言（Task 2 Step 2）；§4.3（待审结果、保真、整轮拒绝）→ Task 3 docstring/注释 + Task 5 规则 7 + Task 2/Task 6 拒绝测试；§5.1（规则 2/4/5/7/8、共享常量、依赖方向、复核理由指引、schema 不变）→ Task 5；§5.2（记录 vs 认证六行表）→ Task 5 规则 7 尾段 + `_RECORDING_MARKS` + 正负样本测试（Task 2 Step 8）；§5.3（白名单按含义、平衡对照）→ Task 5 规则 5/8 + `_WHITELIST_MEANING_MARKS`；§6（诊断、path A/B、无新包装器、恢复计入失败调用）→ Task 3 异常归档 + Task 6 预算/恢复/并存测试；§7 顺序 1–6 → Task 2/3/4/5/6/7；§8 条目 1→Task 2+4、2→既有 spy 测试 + Task 3 保留 `record_action_result` 原样 + Task 6 调用数测试、3→既有 `ActionPipelineIntegrationTest`/`test_review_rejects_dropped_or_reversed_action_result` 保留、4→Task 2 Step 8 + Task 6 F11/F12、5→Task 2 Step 8 正负样本、6→Task 3 诊断 + Task 6 并存/私有测试、7→Task 6 提交/拒绝测试、8→Task 6 调用数与预算/恢复测试；§9→Task 7 交付记录明确"未执行"。`drama_director.py` 无接口改动（其 docstring 未宣称语义批准，无需变更）。
- **占位符扫描**：Task 7 Step 3 的 `?` 为交付记录表的执行者回填占位（记录行为，非实现缺口）；其余步骤无 TBD/TODO；Task 5 Step 2 的 `parts.append` 示意已注明"以现有 return 表达式风格融入"并给出精确插入位置与内容。
- **类型/名称一致性**：`AGENCY_AND_EVIDENCE_RULES`、`PUBLIC_HISTORY_SECTION_MARK = '## Public history (data)'` 在 Task 3 定义、Task 5 import、Task 2/4 断言（字面量与常量一致）；`make_action_adjudication_step(director, *, public_facts=(), public_history=None)`（Task 3）与 Task 4 调用一致；`make_final_output_step(..., public_history=None)`（Task 5）与 Task 4 接线、Task 2 `self._prompts(public_history=history)` 一致；诊断 stage 字符串 `'action_adjudication'` 在 Task 2/3/6 一致；测试指纹（`_PENDING_REVIEW_MARKS` 等）逐字对应 Task 5 规则文本；既有助手名（`_make_director`、`_prompts_routed_to`、`_fdx_paragraphs`、`read_evidence_section` 等）均已核实存在且签名匹配；`story_resolution_test.py` 需补 `import re` 已在 Task 2 Step 6 注明。
