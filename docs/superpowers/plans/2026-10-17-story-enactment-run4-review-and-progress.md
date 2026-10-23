# Story enactment run4 修复实施计划

> **For agentic workers:** 本计划按任务逐项执行，步骤用 checkbox（`- [ ]`）跟踪。若环境提供 superpowers:subagent-driven-development 或 superpowers:executing-plans 技能，推荐用其逐任务执行并在任务边界审查；未提供时按任务顺序内联执行即可——每个任务以验证命令的输出与**退出码**为完成判据。

**Goal:** 按设计修复 run4 暴露的问题：验证已接入的 `speech_and_action` 行动模式（阶段 A，确定性验证）、细化规则 8 的干预事实边界并校准候选提示（阶段 B）。阶段 C（重复判断校准）按设计 §6 以阶段 A **真实对照**证据为触发条件，本轮**不实施**，仅保留条件任务模板；收尾记录如实记载 C 未实施。全程确定性测试、零真实模型调用。

**Architecture:** 只改 contrib/example 层的提示文案、测试、验证配置和文档。规则 8 是 `story_resolution.py` 中生成与评审共享的单一字符串；候选聚焦指引加入 `director_note.py` 的建议/融合提示；`repetition_push.py` 本轮保持基线不动（阶段 C 是条件任务，仅保留 gate 满足后的实施模板）。核心 `EventResolution`、`attempt_to_result`、`maybe_inject_narrative_push`、执行引擎、预算包装器与提交边界一律不动。

**Tech Stack:** Python 3.12+ / pytest / absl-py / MockModel 与 `examples.story_enactment.testing_stubs.PromptStubModel`（CI 无 API key）。格式化 pyink（80 列、2 空格缩进）+ isort（Google profile）；pylint --errors-only；pyrefly。

**Spec:** `docs/superpowers/specs/2026-10-17-story-enactment-run4-review-and-progress-design.md`（本计划从该设计推导；执行者须同时阅读设计与本计划）

## Global Constraints

以下约束来自设计 §7–§9，每个任务默认全部适用：

- **不提交 git**：工作区已有同事未提交的修改（`git status` 可见 20 个修改文件 + 2 个未跟踪文件），且与本计划触碰的文件重叠。全程 **不 commit、不 stash、不 clean、不回滚** 既有修改（设计 §8）。任务以"运行验证命令并记录结果"收尾，代替提交步骤。
- **核心零修改**：`git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` 在收尾时必须为空；`concordia/components/game_master/event_resolution.py` 的 `attempt_to_result`、`maybe_inject_narrative_push` 不改。
- **零真实模型调用**：所有测试使用 `MockModel` / `PromptStubModel`。设计 §4.2/§5.4/§6 的真实调用评测是后续独立工作，本计划不执行。
- **阶段 C 本轮不实施**：`repetition_push.py` 与 `repetition_push_test.py` 保持当前基线状态（未跟踪文件，不触碰）。Task 7 是条件任务，gate = 阶段 A 真实对照（后续独立工作）显示行动模式下**仍存在相似停滞**，**且**用户明确授权；两者缺一即跳过（设计 §6/§8）。收尾记录必须如实写明 C 未实施及原因。
- **默认行为模式不变**：`make_run_config` 的 `action_mode` 默认仍是 `speech_only`；不在旧状态目录恢复时切换动作模式；不从旧对白追认已完成的动作。
- **评审契约不变**：规则 8 是生成/评审共享的同一字符串；评审返回仍为严格 `{"valid": bool, "reason": str}`；一次否决即停止；不新增生成重试、第二评审模型或多数投票。
- **候选不经程序删句**：`director_note.py` 融合返回值原样透传，不做程序删句、重写或"核心句"抽取。
- **干预边界不变**：一轮至多一路干预、张力优先、获准且提交后才消费指令；私有诊断不入玩家观察或剧本；`run_results/` 只读。
- **环境**：先 `source venv/bin/activate`（若 venv 不存在按 AGENTS.md 用 uv 创建并安装 `.[dev]`）。所有命令用 Bash。
- **风格**：改动遵守 pyink 80 列 / 2 空格缩进；不新增注释除非复述科学/设计意图（本仓库惯例是带意图注释，跟随现有文件风格）。

---

### Task 1: 开工基线快照与预检

**Files:**
- Create: `/tmp/opencode/run4-baseline/git-status.txt`、`/tmp/opencode/run4-baseline/git-diff.patch`、`/tmp/opencode/run4-baseline/run_results-hashes.txt`（均在仓库外，不污染工作区）
- 无仓库内文件改动

**Interfaces:**
- Consumes: 当前 dirty worktree、`run_results/` 目录。
- Produces: 基线证据文件，Task 8 的收尾核对（run_results 只读验证、RED/GREEN 前后对照）依赖它们。没有基线清单就无法按设计 §9 声称 run_results 只读。

- [x] **Step 1: 确认虚拟环境可用**

```bash
source venv/bin/activate && python -c "import concordia; print('ok')"
```

Expected: 输出 `ok`。若 venv 不存在，先按 AGENTS.md 执行 `uv venv venv && uv pip install --editable .[dev]`。

- [x] **Step 2: 记录 dirty worktree 基线快照**

```bash
mkdir -p /tmp/opencode/run4-baseline
git status --short > /tmp/opencode/run4-baseline/git-status.txt
git diff > /tmp/opencode/run4-baseline/git-diff.patch
wc -l /tmp/opencode/run4-baseline/git-status.txt /tmp/opencode/run4-baseline/git-diff.patch
```

Expected: status 至少包含 20 个 ` M` 条目与 2 个 `??` 条目（`repetition_push.py`、`repetition_push_test.py`）。之后任何任务不得清空或回滚这些修改。

- [x] **Step 3: 记录 run_results 只读基线（清单+哈希）**

```bash
find run_results -type f -print0 | sort -z | xargs -0 sha256sum \
  > /tmp/opencode/run4-baseline/run_results-hashes.txt
wc -l /tmp/opencode/run4-baseline/run_results-hashes.txt
```

Expected: 记录至少覆盖 `story_enactment/gemini-3.8-flash-high_run4/` 与 `story_enactment/gpt-6-luna_run4/` 下的文件。若 `run_results/` 不存在，记录该限制（设计 §9：没有基线清单时明确记录，不补造证明）。

- [x] **Step 4: 记录预改测试基线**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master concordia/contrib/prefabs -q 2>&1 | tail -3; echo "exit-contrib=$?"
pytest examples/story_enactment -q 2>&1 | tail -3; echo "exit-examples=$?"
```

Expected: 两个范围全部 PASS 且 `exit-contrib=0`、`exit-examples=0`（2026-10-16 交付记录的 GREEN 状态）。若有失败，先停下向用户报告，不要在未知基线上开工。

---

### Task 2: 阶段 A——director+行动模式配置与接线验证

设计 §4.1：用现有工厂显式构造 `speech_and_action` 配置（零模型调用），核对顶层配置、GM 参数、场景 ActionSpec 同一模式、共享 codec 可解析；补上缺失的「director 模式 + 行动裁决」组合的端到端接线检查。设计明确"沿用已有行动链路测试，缺少的场景才补测试；不以新增重复测试代替实际接线检查"——本任务新增的两个场景（工厂配置一致性、director+行动组合）目前不存在，其余数据流由既有测试覆盖（见 Step 5 对照表）。

**Files:**
- Modify: `examples/story_enactment/story_enactment_test.py`（`MakeRunConfigTest` 之后新增一个配置测试类；`ActionModeRunTest` 之后、`_ActionChainTestCase` 体系内新增一个接线测试类）
- Modify: `examples/story_enactment/README.md`（Stages and Modes / Resuming 附近补行动模式对照说明）
- Test: 同文件（测试即交付物）

**Interfaces:**
- Consumes: `run_lib.make_run_config`、`run_lib.run`、`run_lib._normalize_run_config`、`story_config.make_example_config`、`story_config.make_speech_and_action_action_spec`、`story_action.decode_action`、`testing_stubs.PromptStubModel(action_intents=, action_result=, scoring_answer=, stage_directions='')`、既有助手 `_fixture_config`、`_beat`、`_StateDirTestCase`、`_ActionChainTestCase._run_with_stub`、`_ATTEMPT_QUESTION`、`_last_question_block`、`_fdx_paragraphs`、`_REPETITION_QUESTION`。
- Produces: 无新生产接口；README 的对照协议说明供 Task 8 交付记录引用。

- [x] **Step 1: 写配置一致性测试（预期立即 GREEN——这是接线验证，不是缺陷修复）**

在 `MakeRunConfigTest` 类定义之后新增（放在 `class BuildSimulationTest` 之前）：

```python
class DirectorActionModeConfigTest(unittest.TestCase):
  """Phase A of the run4 design: the comparison config is consistent.

  Building the run4 comparison configuration records one action
  contract everywhere: the top-level ``action_mode``, the game
  master's params and the scene's canonical elicitation text all name
  ``speech_and_action``, and the shared codec decodes the JSON
  contract that text elicits. Constructing the config performs no
  model calls.
  """

  def test_director_mode_records_one_contract_everywhere(self):
    config = run_lib.make_run_config(
        story_config.make_example_config(),
        mode='director',
        action_mode='speech_and_action',
        total_turn_limit=24,
        total_model_attempt_limit=400,
    )
    self.assertEqual(config['mode'], 'director')
    self.assertEqual(config['action_mode'], 'speech_and_action')
    self.assertEqual(config['gm_params']['action_mode'], 'speech_and_action')
    spec = config['scene_dicts'][0]['scene_type']['action_spec']
    self.assertEqual(
        spec['call_to_action'],
        story_config.make_speech_and_action_action_spec().call_to_action,
    )
    # The director stage keeps its intervention switches alongside the
    # action contract, and the consistency gate accepts the config.
    self.assertTrue(config['director_params']['repetition_push_enabled'])
    self.assertEqual(config['director_params']['repetition_check_interval'], 3)
    normalized = run_lib._normalize_run_config(config)  # pylint: disable=protected-access
    self.assertEqual(normalized['action_mode'], 'speech_and_action')

  def test_shared_codec_decodes_the_elicited_contract(self):
    raw = json.dumps(
        {
            'speech': '第一段。\n\n第二段 "引号"',
            'action_intent': '林雪用自己持有的那把钥匙去转动保险柜的锁。',
        },
        ensure_ascii=False,
    )
    speech, intent = story_action.decode_action(raw)
    self.assertEqual(speech, '第一段。\n\n第二段 "引号"')
    self.assertEqual(intent, '林雪用自己持有的那把钥匙去转动保险柜的锁。')
```

- [x] **Step 2: 运行确认 PASS**

```bash
source venv/bin/activate
set -o pipefail
pytest "examples/story_enactment/story_enactment_test.py::DirectorActionModeConfigTest" -v 2>&1 | tail -5; echo "exit=$?"
```

Expected: 2 passed，`exit=0`。若 FAIL：这是真实接线缺陷（配置不一致或 codec 故障），停下记录并向用户报告，不得为过测试改生产代码。

- [x] **Step 3: 写 director+行动组合端到端接线测试（预期 GREEN）**

在 `_ActionChainTestCase` 基类定义**之后**新增（建议紧随基类之后、`ActionPipelineIntegrationTest` 之前）。**位置警告**：`_WILL_DATE`、提示指纹助手与 `_ActionChainTestCase` 基类本身都在 `ActionModeRunTest` 之后才定义（story_enactment_test.py:2115–2203），把本类放在 `ActionModeRunTest` 之后会在模块加载时 `NameError: name '_ActionChainTestCase' is not defined`（审阅者已实测）；方法体内引用的 `_ATTEMPT_QUESTION`、`_REPETITION_QUESTION` 等模块常量在运行期解析，晚于类定义不影响：

```python
class DirectorActionRunTest(_ActionChainTestCase):
  """The run4 configuration shape: director mode with adjudicated action.

  Phase A of the run4 design verifies the already-integrated action
  adjudication under the director stage switch (scoring and feedback
  active, repetition gate on its own interval): the blocked two-key
  adjudication is conditioned on the public world constraint, and its
  result flows to the turn log and the FDX exactly as under
  beats_only. No new action system is introduced.
  """

  _INTENT = '林雪用自己持有的那把钥匙去开堂屋侧室的保险柜。'
  _BLOCKED = (
      '保险柜纹丝不动：它须两把钥匙同时转动才能开启，林雪手里只有一把，'
      '柜门没有开启。'
  )

  def test_director_mode_adjudicates_action_result_end_to_end(self):
    config = _fixture_config(
        total_turn_limit=2,
        total_model_attempt_limit=200,
        beats=(_beat('b1', max_turns=99),),
        completion_answers={},
        scene_rounds=99,
        action_mode='speech_and_action',
    )
    config['mode'] = 'director'
    story = story_config.make_example_config()
    config['director_params'] = {
        'eval_interval': 1,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': True,
        'cooldown_turns': 3,
        'allowed_environment_events': list(story.allowed_environment_events),
        'immutable_facts': list(story.immutable_facts),
        'repetition_push_enabled': True,
        'repetition_check_interval': 3,
    }
    stub = testing_stubs.PromptStubModel(
        action_intents={'林雪': self._INTENT},
        action_result=self._BLOCKED,
        scoring_answer='5',
        stage_directions='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 2)
    self.assertEqual(self.summary()['mode'], 'director')
    self.assertEqual(self.summary()['action_mode'], 'speech_and_action')
    # The blocked adjudication result reached the committed turn.
    record = self.turn_log().load()[0]
    self.assertEqual(record['actor'], '林雪')
    self.assertEqual(record['action_intent'], self._INTENT)
    core_result = f'{self._INTENT} Because of that, {self._BLOCKED}'
    self.assertEqual(record['stage_directions'], core_result)
    self.assertIn('柜门没有开启', record['resolved_event'])
    # The core adjudication question was conditioned on the public
    # two-key constraint, ran exactly once, and nobody decided the
    # other key holder's choice for her.
    adjudications = [
        p
        for p in stub.text_prompts
        if _ATTEMPT_QUESTION in _last_question_block(p)
    ]
    self.assertEqual(len(adjudications), 1)
    self.assertIn('保险柜须两把钥匙同时转动才能开启', adjudications[0])
    for fabricated in ('交出', '递上', '陈母'):
      self.assertNotIn(fabricated, record['resolved_event'])
    self.assertEqual(_fdx_paragraphs(self.state_dir, 'Action'), [core_result])
    # The repetition gate stayed on its own schedule: with two turns
    # and interval three, no repetition check interleaved.
    self.assertEqual(
        len([
            p
            for p in stub.choice_prompts
            if _REPETITION_QUESTION in _last_choice_block(p)
        ]),
        0,
    )
```

- [x] **Step 4: 运行确认 PASS**

```bash
source venv/bin/activate
set -o pipefail
pytest "examples/story_enactment/story_enactment_test.py::DirectorActionRunTest" -v 2>&1 | tail -5; echo "exit-new=$?"
pytest examples/story_enactment -q 2>&1 | tail -3; echo "exit-all=$?"
```

Expected: 新测试 1 passed（`exit-new=0`）；全 examples/story_enactment 套件 PASS（`exit-all=0`）。若 FAIL：真实接线缺陷，记录并报告，不得改生产代码迁就。

- [x] **Step 5: 对照设计 §4.1 四项数据流，确认既有测试覆盖（不新增重复测试）**

运行并记录以下既有测试通过（它们就是四项数据流的既有覆盖）：

```bash
source venv/bin/activate
set -o pipefail
pytest "examples/story_enactment/story_enactment_test.py::ActionPipelineIntegrationTest" -v 2>&1 | tail -5; echo "exit=$?"
```

| 设计 §4.1 数据流 | 既有覆盖 |
|---|---|
| 1. 玩家明确提出用自己钥匙尝试转锁 | `test_blocked_result_flows_to_observation_and_fdx`（林雪保险柜意图） |
| 2. 核心 `attempt_to_result` 依历史/世界条件给结果 | 同上（裁决问题含公开两钥约束断言）+ `test_success_result_flows_to_observation_and_fdx` |
| 3. 缺另一把钥匙受阻；不替另一玩家决定配合 | `test_blocked_result_flows_to_observation_and_fdx`（无 交出/递上/第二把钥匙/陈母 断言） |
| 4. 成功/受阻贯穿 evidence、旁白、canonical、观察、JSONL、checkpoint、FDX | `test_success_result_flows_to_observation_and_fdx`、`test_blocked_result_flows_to_observation_and_fdx`、`test_changing_only_core_result_changes_final_event` |

Expected: ActionPipelineIntegrationTest 全部 PASS。该表原文抄入 Task 8 交付记录。

- [x] **Step 6: README 补行动模式对照说明**

在 `examples/story_enactment/README.md` 的 "Resuming" 小节之后（"Schema-2 run configs require..." 段落之后）新增一个小节：

```markdown
## Action Modes

The `--action_mode` flag selects the characters' action contract (the
default stays `speech_only`): `speech_and_action` additionally elicits a
JSON object with `speech` and `action_intent`, and the game master
adjudicates each non-empty intent through the core `attempt_to_result`
helper before the final narrator track; success, failure and blocked
results are narrated and exported like any other committed fact. The
adjudication never decides another player's cooperation for them.

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
```

- [x] **Step 7: 记录本任务结果**

无提交步骤（Global Constraints）。在任务报告中记录：两个新测试名、RED/GREEN 状态（预期双 GREEN，这是验证不是修复）、Step 5 对照表结论。

---

### Task 3: 阶段 B1——规则 8 正反回归测试（RED）

设计 §5.4：将 Luna 第 19 回合缩减为随仓库交付的测试文本（不依赖 `run_results/`），覆盖正反对照表。其中**规则文案指纹测试**是对旧规则 RED 的驱动；脚本化判决测试证明管道（评审输入=返回=提交、拒绝无副作用），不证明真实模型语义判断——三类证据不得互相替代（设计 §9）。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution_test.py`（文件末尾 `SemanticReviewTest` 之后新增一个测试类与 Luna 常量）
- 不改生产代码（本任务只交付测试，RED 状态就是交付物）

**Interfaces:**
- Consumes: `story_resolution.make_final_output_step`、既有助手 `_read_canonical_section`、常量 `_APPROVAL`、`mock_model.MockModel`。
- Produces: `Rule8InterventionBoundaryTest` 与常量 `_LUNA_*`，被 Task 4 用作 GREEN 判据。

- [x] **Step 1: 在文件顶部常量区（`_BROADCAST_STAGE` 之后）新增 Luna 缩减夹具**

```python
# Luna run4 turn 19, reduced for the in-repo regression (run4 design
# 5.4): the pending tension directive fused a blackout event after the
# speech and appended a restatement of the ongoing disagreement. The
# final narration kept the blackout and its candlelight consequence but
# omitted that unchanged background — legal under the calibrated rule 8.
_LUNA_SPEECH = '这宅子的事，你们吵到天亮也吵不出结果。'
_LUNA_STAGE_KEPT = '雷雨骤紧，老宅骤然断电，堂内只剩烛光；雨声压过片刻的争论。'
_LUNA_STAGE_PARAPHRASE = '暴雨更急，整座老宅陷入停电，堂内只剩烛光微明。'
_LUNA_INTERVENTION = {
    'reason': 'tension',
    'candidate_before': f'王婶: {_LUNA_SPEECH}',
    'candidate_after': (
        f'王婶: {_LUNA_SPEECH} 雷雨骤紧，老宅骤然断电，堂内只剩烛光；'
        '雨声压住片刻争论，众人仍未就遗嘱或老宅去向达成一致。'
    ),
}
# The committed history the reduced turn replays against: the ongoing
# disagreement is established public state, so restating it adds no new
# fact, and omitting such a restatement stays legal under the
# calibrated rule 8.
_LUNA_COMMITTED_HISTORY = (
    '周律师: 各方就遗嘱与老宅去向多轮争执，至今仍未达成一致。',
)
# The negative control of design 5.4 row 5: the candidate's only
# addition restates the stalemate already established in the committed
# history above — no environmental description at all (even a line
# like "雨声压住片刻争论" could read as a new environmental effect),
# so it carries no legal new event and cannot count as a successful
# intervention.
_LUNA_INTERVENTION_RESTATEMENT_ONLY = {
    'reason': 'tension',
    'candidate_before': f'王婶: {_LUNA_SPEECH}',
    'candidate_after': (
        f'王婶: {_LUNA_SPEECH} 众人仍未就遗嘱或老宅去向达成一致。'
    ),
}
```

- [x] **Step 2: 在 `SemanticReviewTest` 之后、`if __name__` 之前新增测试类**

```python
class Rule8InterventionBoundaryTest(unittest.TestCase):
  """The calibrated rule-8 boundary on the reduced Luna turn-19 case.

  Scripted verdicts prove the plumbing: prompt content, distinctness of
  the reviewed/returned/committed event, and rejection without side
  effects. The rule-text fingerprints prove the calibrated boundary is
  what generation and review share. Neither asserts a real model's
  semantic judgment (run4 design 5.4).
  """

  def _make(
      self,
      *,
      stage: str,
      review: str,
      intervention: dict[str, Any] | None = None,
  ):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = _LUNA_SPEECH
    director.get_current_action_intent.return_value = None
    director.get_current_resolution_evidence.return_value = {
        'action_result': None,
        'intervention': intervention or _LUNA_INTERVENTION,
    }
    director.get_committed_events.return_value = _LUNA_COMMITTED_HISTORY
    model = mock_model.MockModel()
    model.sample_text = mock.Mock(
        side_effect=[json.dumps({'stage_directions': stage}), review]
    )
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(
        director, allowed_environment_events=('雷雨骤紧，老宅断电',)
    )
    return director, model, chain, step

  def test_calibrated_rule8_text_is_shared_by_generation_and_review(self):
    director, model, chain, step = self._make(
        stage=_LUNA_STAGE_KEPT, review=_APPROVAL
    )
    step(chain, f'王婶: {_LUNA_SPEECH}', '王婶')
    generation, review_prompt = [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]
    for prompt in (generation, review_prompt):
      flat = ' '.join(prompt.split())
      self.assertIn('faithful paraphrase is allowed', flat)
      self.assertIn('unchanged background states such as an ongoing'
                    ' disagreement', flat)
      self.assertIn('Omitting such a restatement is not dropping the'
                    ' intervention', flat)
      self.assertIn('containing only restatements is not a successful'
                    ' intervention', flat)
      # The exception stays fenced: adjudicated results and player
      # choices are never omissible background.
      self.assertIn('Never use this exception to omit an adjudicated'
                    ' action result', flat)

  def test_omitting_unchanged_background_restatement_is_legal(self):
    # The Luna run4 misrejection, reduced: keeping the blackout and its
    # candlelight consequence while omitting the "仍未达成一致"
    # restatement is legal; the reviewed event equals the returned and
    # committed event.
    director, model, chain, step = self._make(
        stage=_LUNA_STAGE_KEPT, review=_APPROVAL
    )
    result = step(chain, f'王婶: {_LUNA_SPEECH}', '王婶')
    expected = f'王婶: {_LUNA_SPEECH}\n{_LUNA_STAGE_KEPT}'
    self.assertEqual(result, expected)
    _, review_prompt = [
        call.kwargs['prompt'] for call in model.sample_text.call_args_list
    ]
    canonical = _read_canonical_section(review_prompt)
    self.assertEqual(canonical['resolved_event'], result)
    director.set_validated_resolution.assert_called_once_with(
        result, _LUNA_STAGE_KEPT
    )

  def test_faithful_paraphrase_is_legal(self):
    # 同一干预事实的忠实改述不因措辞不等同而拒绝（设计 5.1 第 2 行）。
    director, _, chain, step = self._make(
        stage=_LUNA_STAGE_PARAPHRASE, review=_APPROVAL
    )
    result = step(chain, f'王婶: {_LUNA_SPEECH}', '王婶')
    self.assertEqual(result, f'王婶: {_LUNA_SPEECH}\n{_LUNA_STAGE_PARAPHRASE}')
    director.set_validated_resolution.assert_called_once()

  def test_dropping_reversing_or_inventing_facts_is_rejected(self):
    # 只留雨声遗漏停电 / 写成恢复供电 / 凭空集体同意：脚本化注入
    # reviewer 拒绝，未提交、未消费指令（设计 5.4 表 3–4 行）。
    cases = (
        (
            'blackout dropped',
            '雨声压过片刻的争论。',
            'The blackout was silently dropped',
        ),
        (
            'blackout reversed',
            '雷雨骤紧，老宅的供电随即恢复，堂内灯火通明。',
            'The established blackout was reversed',
        ),
        (
            'invented agreement',
            '雷雨骤紧，老宅骤然断电，堂内只剩烛光；众人一致同意把老宅过户给林雪。',
            'An invented joint agreement',
        ),
    )
    for label, stage, reason in cases:
      with self.subTest(label=label):
        director, model, chain, step = self._make(
            stage=stage,
            review=json.dumps({'valid': False, 'reason': reason}),
        )
        with self.assertRaisesRegex(
            story_resolution.StoryOutputError, reason
        ):
          step(chain, f'王婶: {_LUNA_SPEECH}', '王婶')
        self.assertEqual(model.sample_text.call_count, 2)
        director.set_validated_resolution.assert_not_called()
        director.commit_directive.assert_not_called()

  def test_restatements_only_is_not_a_successful_intervention(self):
    # 候选本身只重述既有僵局、没有合法新事件（设计 5.4 表 5 行）：
    # 通过 intervention 参数换掉默认夹具，candidate_after 仅复述
    # _LUNA_COMMITTED_HISTORY 已确立的"争执未决"，不含停电，也不含
    # 任何可能被解读为新环境效果的描述——测试的是"候选无新事件"，
    # 而非"旁白丢了停电"（后者已由 dropping 用例覆盖）。评审拒绝为
    # 脚本化注入，只证明拒绝分支管道，不证明夹具语义；边界正确性
    # 由夹具与已确立历史的对应关系保证。与既有
    # test_rephrasing_does_not_count_as_intervention 同界，中文夹具对齐。
    director, model, chain, step = self._make(
        stage='众人仍未就遗嘱或老宅去向达成一致。',
        review=json.dumps({
            'valid': False,
            'reason': 'Only an unchanged-state restatement',
        }),
        intervention=_LUNA_INTERVENTION_RESTATEMENT_ONLY,
    )
    with self.assertRaisesRegex(
        story_resolution.StoryOutputError, 'unchanged-state restatement'
    ):
      step(chain, f'王婶: {_LUNA_SPEECH}', '王婶')
    director.set_validated_resolution.assert_not_called()
    director.commit_directive.assert_not_called()
```

- [x] **Step 3: 运行确认 RED（规则文案测试失败，脚本化测试通过）**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master/story_resolution_test.py -q 2>&1 | tail -5; echo "exit-file=$?"
pytest "concordia/contrib/components/game_master/story_resolution_test.py::Rule8InterventionBoundaryTest" -v 2>&1 | tail -10; echo "exit-class=$?"
```

Expected: `exit-file=1`、`exit-class=1`（退出码非零来自且仅来自 RED）：`test_calibrated_rule8_text_is_shared_by_generation_and_review` **FAIL**（旧规则 8 缺少新指纹：`AssertionError: 'faithful paraphrase is allowed' not found`）；其余新测试 PASS；既有测试（含 `test_review_rejects_dropped_or_reversed_action_result`、`test_rephrasing_does_not_count_as_intervention` 等行动结果与提交边界测试）全部 PASS。记录 RED 输出原文与退出码。

---

### Task 4: 阶段 B2a——替换 `story_resolution.py` 规则 8

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution.py:327-331`（仅 `_SEMANTIC_RULES` 的规则 8 段）
- Test: Task 3 已交付（本任务以它为 GREEN 判据）

**Interfaces:**
- Consumes: Task 3 的 `Rule8InterventionBoundaryTest`。
- Produces: 校准后的规则 8 文案（生成与评审共享，来自设计 §5.2 的推荐文案）；Task 6 的跨套件验证以其为输入。

- [x] **Step 1: 用设计 §5.2 文案替换规则 8**

在 `_SEMANTic_RULES`（实际常量名 `_SEMANTIC_RULES`）中，将这一段：

```text
8. When the resolution evidence carries an intervention candidate, the
   candidate must contain a legal new fact (a whitelisted NPC or environmental
   means grounded in the world and history), and that fact must be retained in
   the final stage directions. A purely verbal restatement is not a successful
   intervention; silently dropping the candidate fact is a rejection.
```

替换为（规则 1–7 一字不动）：

```text
8. An intervention candidate must introduce a legal new NPC or
   environmental event grounded in the whitelist, world and committed
   history. Retain that new event and its material consequences in the
   final stage directions; faithful paraphrase is allowed. You need
   not repeat original speech, established history, or unchanged
   background states such as an ongoing disagreement. Omitting such a
   restatement is not dropping the intervention, provided the final
   event does not contradict it. Never use this exception to omit an
   adjudicated action result, invent player choices, disclose secrets,
   fabricate evidence or remove a material new consequence. A candidate
   containing only restatements is not a successful intervention.
```

注意保持该多行字符串的既有缩进风格（行首 3 空格续行）。

- [x] **Step 2: 运行 Task 3 测试确认 GREEN**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master/story_resolution_test.py -q 2>&1 | tail -3; echo "exit=$?"
```

Expected: 全部 PASS（含新规则文案测试与全部既有测试），`exit=0`。

- [x] **Step 3: 运行同目录邻域回归**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master -q 2>&1 | tail -3; echo "exit=$?"
```

Expected: 全部 PASS，`exit=0`。若 `director_note_test.py` 或其他文件因文案指纹失败，属于 Task 6 范围的真实影响，记录后由 Task 6 统一处理（不要在本任务顺手改其他测试）。

---

### Task 5: 阶段 B2b——`director_note.py` 候选聚焦指引

设计 §5.3：建议/融合提示说明新增内容聚焦所选白名单事件及可见后果，不追加"仍在争执"类无变化总结，不宣布角色改变态度；保留完整原对白的融合要求；融合返回值原样透传（无程序删句）。

**Files:**
- Modify: `concordia/contrib/components/game_master/director_note.py`（`_AGENCY_AND_NOVELTY` 附近新增常量，`_complication_question` 与 `_merge_question` 注入）
- Test: `concordia/contrib/components/game_master/director_note_test.py`（`FusionStepTest` 内新增测试）

**Interfaces:**
- Consumes: `_DirectorFlowModel`、`_make_director`、`_complete_turn`、`_step_call`、常量 `_FUSED_EVENT`。
- Produces: 候选聚焦指引文案；Task 6 跨套件验证输入。

- [x] **Step 1: 写失败测试（RED）**

在 `FusionStepTest` 中（`test_fuses_first_candidate_with_constrained_prompts` 之后）新增：

```python
  def test_prompts_focus_added_content_on_new_events_only(self):
    # Run4 design 5.3: the suggestion and fusion prompts say the added
    # content focuses on the chosen whitelisted event and its visible
    # consequences, never unchanged-state summaries ("still arguing")
    # or announced attitude changes. The fused return value passes
    # through verbatim: no programmatic sentence trimming exists.
    model = _DirectorFlowModel()
    director, gm = _make_director(model)
    _complete_turn(gm, 'I hid the letter.')
    before = len(model.text_prompts)
    self.assertEqual(_step_call(model, director), _FUSED_EVENT)
    complication_prompt, merge_prompt = model.text_prompts[before:]
    for prompt in (complication_prompt, merge_prompt):
      self.assertIn('chosen whitelisted event', prompt)
      self.assertIn('visible consequences', prompt)
      self.assertIn('unchanged-state summaries', prompt)
      self.assertIn('changed their attitude', prompt)
    self.assertIn('verbatim', merge_prompt)
```

- [x] **Step 2: 运行确认 RED**

```bash
source venv/bin/activate
set -o pipefail
pytest "concordia/contrib/components/game_master/director_note_test.py::FusionStepTest::test_prompts_focus_added_content_on_new_events_only" -v 2>&1 | tail -5; echo "exit=$?"
```

Expected: FAIL（`'chosen whitelisted event' not found`），`exit=1`。记录 RED 输出。

- [x] **Step 3: 实现提示文案**

在 `director_note.py` 的 `_AGENCY_AND_NOVELTY` 常量之后新增：

```python
# Run4 design 5.3: candidates focus on the new event itself, so the
# reviewer never has to rescue a legal omission of unchanged background
# the producer should not have appended in the first place. Guidance
# only — the fused return value is never trimmed programmatically.
_CANDIDATE_FOCUS = (
    ' Focus the added content on the chosen whitelisted event and its'
    ' visible consequences. Do not append unchanged-state summaries'
    ' such as "still arguing" or "still no agreement", and do not'
    ' announce that a character changed their attitude or position.'
)
```

在 `_complication_question` 返回串的 `'Only NPC or environmental events; never assume any player '` 行之后拼入 `{_CANDIDATE_FOCUS}\n`；在 `_merge_question` 返回串的 `' Do not contradict immutable facts.\n'` 之后同样拼入 `{_CANDIDATE_FOCUS}\n`。两处均保持其余文本与顺序不变（完整原对白 verbatim 要求已存在于 merge 提示，保留）。

- [x] **Step 4: 运行确认 GREEN + 邻域**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master/director_note_test.py -q 2>&1 | tail -3; echo "exit-file=$?"
pytest concordia/contrib/components/game_master -q 2>&1 | tail -3; echo "exit-dir=$?"
```

Expected: 全部 PASS，两个退出码均为 0。

---

### Task 6: 阶段 B3——跨套件接线验证与格式检查

设计 §8 B3：`story_prefabs_test.py`、`examples/story_enactment/*_test.py` 按实际影响更新；stub 继续从本轮证据推导旁白（不改 stub 行为）。预检已确认旧规则 8 指纹（`purely verbal restatement`、`silently dropping the candidate fact`、`legal new fact`）在测试与文档中无断言引用，预期零破坏；若出现失败，只允许更新提示指纹字符串，不允许改行为。

**Files:**
- Modify: 按实际影响——候选 `concordia/contrib/prefabs/game_master/story_prefabs_test.py`、`examples/story_enactment/testing_stubs_test.py`、`examples/story_enactment/story_enactment_test.py`
- Test: 全量套件即验证

**Interfaces:**
- Consumes: Task 4 的新规则 8、Task 5 的候选聚焦指引。
- Produces: 阶段 B 的 GREEN 全量证据（Task 8 交付记录引用）。

- [x] **Step 1: 运行 contrib 与 examples 全量**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia -q 2>&1 | tail -3; echo "exit-concordia=$?"
pytest examples -q 2>&1 | tail -3; echo "exit-examples=$?"
```

Expected: 全部 PASS（2026-10-16 基线：concordia 1447 passed / 33 skipped 量级；examples 383+ passed 量级，Task 2 新增 3 个测试）。若失败：定位是否提示指纹失配；只改断言字符串对齐新文案，逐条记录改动理由；任何需要改生产逻辑的失败立即停下报告。

- [x] **Step 2: 对已触碰的 Python 文件跑格式检查**

```bash
source venv/bin/activate
pyink --check examples/story_enactment/story_enactment_test.py \
  concordia/contrib/components/game_master/story_resolution.py \
  concordia/contrib/components/game_master/story_resolution_test.py \
  concordia/contrib/components/game_master/director_note.py \
  concordia/contrib/components/game_master/director_note_test.py
isort --check-only examples/story_enactment/story_enactment_test.py \
  concordia/contrib/components/game_master/story_resolution.py \
  concordia/contrib/components/game_master/story_resolution_test.py \
  concordia/contrib/components/game_master/director_note.py \
  concordia/contrib/components/game_master/director_note_test.py
```

Expected: 两者通过（无输出 / `would be left unchanged`）。若 pyink 报格式，运行 `pyink <文件>` 修复后复跑 Step 1 相关测试。

---

### Task 7: 阶段 C——重复判断上下文校准（条件任务：本轮不执行）

**触发条件（gate）**：设计 §6 要求先核实阶段 A 的**真实对照**结果——行动模式下是否仍存在相似停滞——然后才修改重复判断上下文；§8 进一步允许"阶段 A 显示无需修改重复提示时，记录证据后不改生产代码"。本任务全部步骤仅在「阶段 A 真实对照（设计 §4.2 的后续独立工作）显示行动模式下**仍存在相似停滞**，**且**用户明确授权实施」后执行；任一条件不满足即保持跳过，`repetition_push.py` / `repetition_push_test.py` 维持开工基线不动。**本轮默认 gate 不满足：直接跳过本任务，Task 8 按"未实施"记录。**run4 在 speech_only 下的停滞与 5 次检查全 No（设计 §2.1）只说明问题存在，不构成绕过 gate 的证据。

gate 满足并授权后，按以下步骤实施：只改 `repetition_push.py` 的 `_JUDGMENT_RULES` 上下文说明，保留原检查次数、门控与核心调用；不新增独立"是否停滞"调用、不把模型 No 程序改成 Yes、不按轮数强制干预。

**Files:**
- Modify: `concordia/contrib/components/game_master/repetition_push.py:67-75`（`_JUDGMENT_RULES`）
- Modify: `concordia/contrib/components/README.md`（`repetition_push.py` 词条，见 Step 6）
- Test: `concordia/contrib/components/game_master/repetition_push_test.py`（新增测试类）

**Interfaces:**
- Consumes: 既有 `_RepetitionModel`、`_make_director`、`_open_third_turn`、`_step_call`、常量 `_ACTION_RESULT`。
- Produces: 校准后的 `_JUDGMENT_RULES`（核心 helper 调用签名不变）。

- [x] **Step 0: 核对 gate**

本轮默认跳过：确认「阶段 A 真实对照证据 + 用户授权」均不满足，勾选本步并在任务报告写明"Task 7 未实施（gate 未满足）"，直接进入 Task 8。以下步骤仅供 gate 满足后的授权实施使用。

- [ ] **Step 1: 写失败测试（RED）**

在 `RepetitionPushStepTest` 之后新增：

```python
class CalibratedJudgmentContextTest(unittest.TestCase):
  """Run4 design 6: clearer effective-progress guidance, same core call."""

  def test_judgment_rules_state_effective_progress_boundary(self):
    model = _RepetitionModel()
    director, gm = _make_director(model)
    _open_third_turn(gm, action_result=_ACTION_RESULT)
    core = mock.Mock(
        wraps=event_resolution_lib.maybe_inject_narrative_push
    )
    with mock.patch.object(
        event_resolution_lib, 'maybe_inject_narrative_push', core
    ):
      _step_call(model, director, f'Alice: three\n{_ACTION_RESULT}')
    core.assert_called_once()
    flat = ' '.join(core.call_args.args[0].view().text().split())
    # Promises and repeated urging are not outcomes...
    self.assertIn('not by itself a new outcome', flat)
    self.assertIn('a future "will check" is not a check that happened', flat)
    # ...while sourced disclosure, adjudicated results and new
    # autonomous stances can be progress.
    self.assertIn('an adjudicated action result, a new autonomous'
                  ' stance', flat)
    # A blocked action is still a real outcome.
    self.assertIn('A blocked action is still a real outcome', flat)
    # Uncommitted candidates never count as public progress.
    self.assertIn('not an event that happened', flat)
    # The pre-existing boundary fingerprints survive the calibration.
    self.assertIn('new facts', flat)
    self.assertIn('effective responses', flat)
```

- [ ] **Step 2: 运行确认 RED**

```bash
source venv/bin/activate
set -o pipefail
pytest "concordia/contrib/components/game_master/repetition_push_test.py::CalibratedJudgmentContextTest" -v 2>&1 | tail -5; echo "exit=$?"
```

Expected: FAIL（`'not by itself a new outcome' not found`），`exit=1`。记录 RED 输出。

- [ ] **Step 3: 替换 `_JUDGMENT_RULES`**

将 `repetition_push.py` 的 `_JUDGMENT_RULES`（第 67–75 行）整体替换为（保留原三句边界语义，追加设计 §6 的有效进展说明与正反平衡的成对例子；例子用通用锁/钥匙措辞，不泄露任何故事私密事实、不指向特定结局）：

```python
_JUDGMENT_RULES = (
    'Judge repetition only against the committed public events above. A'
    ' recent quarrel or repeated disagreement alone is NOT repetition'
    ' when new facts, adjudicated action results or effective responses'
    ' to each other occurred. One-time events that already happened'
    ' cannot happen again. The current candidate is a proposal for this'
    ' turn, not an event that already occurred; future plans, possible'
    ' endings and any GM-only knowledge are not established progress.\n'
    'Deciding what counts as effective progress: repeatedly urging'
    ' verification, proposing the same step again, repeating conditional'
    ' promises, raising emotional intensity or rewording is not by'
    ' itself a new outcome, and a future "will check" is not a check'
    ' that happened. New sourced disclosure of information, an'
    ' adjudicated action result, a new autonomous stance or choice, and'
    ' substantively answering a previously unanswered concern can be'
    ' progress; physical change is not required every turn.'
    ' Distinguish a first meaningful cooperative proposal from repeating'
    ' the same proposal: never treat all negotiation as ineffective, and'
    ' never treat a promise as executed. A blocked action is still a'
    ' real outcome the character may respond to; no success is not no'
    ' progress. The current uncommitted candidate is not an event that'
    ' happened, GM-only information, future beats and allowed endings'
    ' never count as public progress, and re-proposing the same'
    ' candidate adds no public history. Paired example: urging again'
    ' and again that a lock be turned, with no attempted result, is'
    ' repetition; trying it and clearly finding the second key absent,'
    ' with the character changing their demand accordingly, is progress.'
)
```

- [ ] **Step 4: 运行确认 GREEN + 既有门控/计数回归**

```bash
source venv/bin/activate
set -o pipefail
pytest concordia/contrib/components/game_master/repetition_push_test.py -q 2>&1 | tail -3; echo "exit=$?"
```

Expected: 全部 PASS，`exit=0`——含既有的 `test_core_helper_is_called_with_constrained_context`（核心函数被调用、原返回值贯通）、`test_unchanged_result_records_check_only` / `test_attempted_but_unchanged_records_attempt_not_change`（检查与执行分别计数）。这些正是设计 §6"测试要确认实际核心函数被调用、原返回值贯通、检查与执行分别计数"的既有覆盖。

- [ ] **Step 5: 格式检查**

```bash
source venv/bin/activate
pyink --check concordia/contrib/components/game_master/repetition_push.py \
  concordia/contrib/components/game_master/repetition_push_test.py
isort --check-only concordia/contrib/components/game_master/repetition_push.py \
  concordia/contrib/components/game_master/repetition_push_test.py
```

Expected: 通过。

- [ ] **Step 6: 更新 contrib README 的 repetition_push.py 词条**

`concordia/contrib/components/README.md` 的 `repetition_push.py` 词条末尾追加一句（与 Task 8 Step 1 的 story_resolution 词条分开，本句只随本任务交付）：

```markdown
The judgment context (run4 design 6) states what counts as effective
progress — sourced disclosures, adjudicated action results (blocked
ones included), new autonomous stances and substantive answers —
against promises, repeated urging and rewording, while the core helper,
its gating and its counters stay unchanged.
```

---

### Task 8: 收尾——文档、交付记录与全量检查

**Files:**
- Modify: `concordia/contrib/components/README.md`（仅 `story_resolution.py` 词条补一句校准说明；`repetition_push.py` 词条随 Task 7 交付，本轮不动）
- Modify: `docs/superpowers/specs/2026-10-17-story-enactment-run4-review-and-progress-design.md`（状态行回填）
- Create: `docs/superpowers/notes/2026-10-17-story-enactment-run4-review-and-progress.md`（交付记录）

**Interfaces:**
- Consumes: Task 1 基线快照、Task 2 的 §4.1 对照表、Task 3–6 的 RED/GREEN 记录（Task 7 未实施时无其记录，交付记录写明原因）。
- Produces: 设计 §9 要求的工程验收证据（含阶段 C 未实施的如实记载）。

- [x] **Step 1: 更新 contrib README 的 story_resolution 词条**

`concordia/contrib/components/README.md` 的 `story_resolution.py` 词条末尾追加一句（融合进现有段落，保持条目长度克制）：

```markdown
The calibrated rule 8 (run4 design 5.1) keeps the requirement to retain
a legal new event and its material consequences while allowing faithful
paraphrase and the omission of unchanged background restatements such as
an ongoing disagreement, provided nothing contradicts them; the
exception never covers adjudicated action results, player choices,
secrets or fabricated evidence, and restatement-only candidates remain
unsuccessful interventions.
```

注意：`repetition_push.py` 词条本轮**不改**（对应句子已移至 Task 7 Step 6，仅随 gate 满足后的阶段 C 交付）。

- [x] **Step 2: 回填设计文档状态行**

将该设计文档第 4 行 `状态：待实施与验证。本次只交付设计，不表示代码已修改，不发起真实模型调用。` 改为：

```markdown
状态：阶段 A 确定性验证与阶段 B 已实施（工程验收，实施记录见
../notes/2026-10-17-story-enactment-run4-review-and-progress.md）；
阶段 C 未实施（待阶段 A 真实对照证据与授权后另行决定）；
真实模型评测未执行。
```

- [x] **Step 3: 写交付记录**

创建 `docs/superpowers/notes/2026-10-17-story-enactment-run4-review-and-progress.md`，结构对齐 `2026-10-16-story-enactment-canonical-review.md`：

```markdown
# Story enactment run4 修复实施记录

日期：2026-10-17。方案：
`../plans/2026-10-17-story-enactment-run4-review-and-progress.md`，设计：
`../specs/2026-10-17-story-enactment-run4-review-and-progress-design.md`。
按用户授权执行 Task 1–6 与 Task 8；Task 7（阶段 C）为条件任务，gate
（阶段 A 真实对照证据 + 用户授权）未满足，未实施；全程未提交 git，
未回滚同事未提交的进行中修改
（开工基线快照：/tmp/opencode/run4-baseline/，含 git status、git diff 与
run_results 哈希清单）；未修改 Concordia 核心代码；未发起真实模型调用。

## 交付概述
（一段：规则 8 校准、候选聚焦指引、阶段 A 验证测试与 README 对照协议，
以及阶段 C 未实施的记录；列出修改文件清单——预期为
story_resolution.py、director_note.py 及各自测试、
story_enactment_test.py、contrib/components README 的 story_resolution
词条、examples README、设计状态行与本记录；
repetition_push.py / repetition_push_test.py 保持基线未触碰。）

## 各任务 RED/GREEN 证据
| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| Task 2 阶段 A 验证 | 预期双 GREEN（验证既有接线，非缺陷修复），粘贴实际输出与退出码 | 同左 |
| Task 3 规则 8 回归 | 文案指纹测试 FAIL 输出原文 | 全部 PASS |
| Task 4 规则 8 替换 | 以 Task 3 RED 为输入 | story_resolution_test 全 PASS |
| Task 5 候选聚焦 | FAIL 输出原文 | director_note_test 全 PASS |
| Task 6 跨套件 | — | concordia / examples 全量结果 |

## 阶段 C：未实施
按设计 §6/§8，重复判断上下文校准以阶段 A 真实对照证据为触发条件
（行动模式下是否仍存在相似停滞）加用户授权；本轮两者均未满足，
未修改 repetition_push.py / repetition_push_test.py（保持开工基线）。
触发条件与实施模板见计划 Task 7。

## 设计 §4.1 数据流对照（Task 2 Step 5 表原文抄录）

## 全量检查（每条独立运行、单独捕获退出码，未用 && 串连）
| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `source venv/bin/activate && pytest concordia` | ? | ? |
| `source venv/bin/activate && pytest examples` | ? | ? |
| `source venv/bin/activate && pylint --errors-only -j 1 concordia` | ? | ? |
| `source venv/bin/activate && pyrefly check concordia` | ? | ?（既有 4 项依赖错误单列：vllm missing-import ×3、FastMCP missing-attribute ×1） |
| `git diff --check` | ? | ? |

## 格式检查（仅触碰的 Python 文件）
pyink --check / isort --check-only 结果。

## 验收确认清单
| 确认项 | 命令 / 证据 | 结论 |
|---|---|---|
| 核心目录零修改 | `git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` | 预期空输出 |
| run_results 只读 | `find run_results -type f -print0 \| sort -z \| xargs -0 sha256sum \| diff - /tmp/opencode/run4-baseline/run_results-hashes.txt` | 预期无差异 |
| 既有 dirty 基线未被回滚 | `diff <(git status --short) /tmp/opencode/run4-baseline/git-status.txt`（仅允许新增本计划文件造成的追加行） | ? |
| 规则 8 生成/评审一致 | `Rule8InterventionBoundaryTest::test_calibrated_rule8_text_is_shared_by_generation_and_review` | 通过 |
| 拒绝无副作用 | `test_dropping_reversing_or_inventing_facts_is_rejected` 等 | 通过 |

## 真实模型复验：未执行
设计 §4.2/§5.4/§6/§9 的真实调用评测（阶段 A 对照、规则 8 固定样本
语义评测、重复判断质量评测、三场景整体复验）均未发起，属后续独立工作；
其中阶段 A 对照结果同时是阶段 C（计划 Task 7）的触发证据。本记录全部
结果来自 MockModel/PromptStubModel 确定性测试，证明分支与接线，
不构成真实模型语义质量验收。
```

（占位符 `?` 由执行者以实际输出回填；表格结构保持。）

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
git diff --check
```

每条单独运行、单独记录退出码。Expected: 前两条 0；pylint 0（无输出）；pyrefly 允许仅既有 4 项依赖错误（`vllm_model.py` missing-import ×3、`file_reader_server.py` FastMCP missing-attribute ×1，与 2026-10-16 记录的基线一致），出现任何**新** pyrefly 错误即回改；`git diff --check` 0。

- [x] **Step 5: 核心零修改与 run_results 只读验证**

```bash
git diff --name-only concordia/components concordia/document concordia/environment concordia/typing
find run_results -type f -print0 | sort -z | xargs -0 sha256sum | diff - /tmp/opencode/run4-baseline/run_results-hashes.txt && echo READ-ONLY-OK
```

Expected: 第一条无输出；第二条输出 `READ-ONLY-OK`。结果抄入交付记录。

- [x] **Step 6: 格式检查收尾**

对全部触碰过的 Python 文件统一跑一次 `pyink --check` 与 `isort --check-only`（文件清单以 `git status --short` 中本计划触碰的 `.py` 为准，与 Task 1 基线 diff 得出；Task 7 未执行时清单**不含** `repetition_push.py` / `repetition_push_test.py`）。Expected: 全部通过。

---

## Self-Review 记录

- **Spec 覆盖**：设计 §4.1（配置/数据流验证）→ Task 2；§4.2（README 对照协议、不改默认）→ Task 2 Step 6 + Global Constraints；§5.1/§5.2（规则 8 判定原则与文案）→ Task 3/4；§5.3（候选聚焦、无程序删句）→ Task 5；§5.4（六行正反对照表）→ Task 3（第 1 行=文案测试+omission 测试、第 2 行=paraphrase、第 3–4 行=dropped/reversed/invented、第 5 行=restatements-only[候选本身无新事件，经 intervention 参数换夹具]、第 6 行规则 7=既有测试保留并在 Task 3 Step 3 验证）；§6（重复判断校准）→ Task 7 **条件任务：本轮不执行**（gate=阶段 A 真实对照证据+用户授权，模板保留在任务内）；§7 工程边界 → Global Constraints；§8 顺序 A/B1/B2/B3/C/收尾 → Task 2/3/4+5/6/7（条件，跳过）/8，收尾按"C 未实施"记录；§9 检查与记录 → Task 1 + Task 8。真实调用评测（§4.2/§5.4/§6/§9 的模型评测）按设计明确排除在本次工程交付之外。
- **占位符扫描**：Task 8 Step 3 的交付记录模板中 `?` 是要求执行者回填实测值的记录表占位，属记录行为而非实现缺口；其余任务无 TBD/TODO 类占位。
- **类型/名称一致性**：`make_speech_and_action_action_spec`（story_config.py:212）、`decode_action`（story_action.py:68）、`_normalize_run_config`（run.py:625）、`_ATTEMPT_QUESTION`/`_REPETITION_QUESTION`/`_last_question_block`/`_fdx_paragraphs`/`_fixture_config`/`_beat`/`_ActionChainTestCase._run_with_stub`（story_enactment_test.py 既有）、`_read_canonical_section`/`_APPROVAL`（story_resolution_test.py 既有）、`_DirectorFlowModel`/`_make_director`/`_complete_turn`/`_step_call`/`_FUSED_EVENT`（director_note_test.py 既有）、`_RepetitionModel`/`_open_third_turn`/`_ACTION_RESULT`（repetition_push_test.py 既有）均已核实存在且签名匹配。旧规则 8 指纹全仓无测试断言引用（仅生产文件与一处注释），Task 6 预期零破坏已据此论证。
- **审阅修正记录**（2026-10-17 评审后回改）：① Task 7 由"本轮实施、事后可还原"改为条件任务——设计 §6/§8 的触发条件是阶段 A **真实对照**证据，speech_only 下的停滞不构成绕过依据；收尾记录、设计状态行、contrib README 与格式检查清单同步改为 C 未实施。② `DirectorActionRunTest` 的插入位置从"`ActionModeRunTest` 之后（`_WILL_DATE` 之前）"改为"`_ActionChainTestCase` 基类（story_enactment_test.py:2174）之后"——原位置在模块加载时会 `NameError`（评审者已实测；基类与指纹助手均在 2115–2203 才定义）。③ `test_restatements_only_is_not_a_successful_intervention` 改用 `_LUNA_INTERVENTION_RESTATEMENT_ONLY`（candidate_after 不含停电的新夹具，`_make` 增加 `intervention` 参数）——原稿实际测试的是"旁白丢了停电"，与 dropping 用例重复，未覆盖设计 5.4 表第 5 行"候选本身仅重述"。④ 头部 sub-skill 依赖改为可选；⑤ 全部 `pytest … | tail` 命令统一 `set -o pipefail` + 退出码回显，避免管道掩盖失败。⑥ 第二轮评审：`_LUNA_INTERVENTION_RESTATEMENT_ONLY` 的 candidate_after 去掉"雨声压住片刻争论"（该句可解读为新的环境干扰效果，不能明确代表"只有既有状态重述"），仅保留对僵局的重述；新增 `_LUNA_COMMITTED_HISTORY` 并作为 `_make` 默认 committed history（明确确立"多轮争执至今仍未达成一致"），使该用例的"重述已有状态"与省略合法性用例的"未变化背景"都有既定历史支撑，测试只覆盖目标边界。
