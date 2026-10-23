# Story enactment 行动授权与语义评审边界修补实施记录

日期：2026-10-17。方案：
../plans/2026-10-17-story-enactment-action-authorization-review.md，设计：
../specs/2026-10-17-story-enactment-action-authorization-review-design.md。
按用户授权执行 Task 1–7；全程未提交 git，未回滚同事未提交的进行中修改
（开工基线快照：/tmp/opencode/action-auth-baseline/，含 git status、git diff、
已跟踪/未跟踪源码与 docs 内容快照及 run_results 哈希清单）；未修改
Concordia 核心代码；未发起真实模型调用。

## 交付概述

行动裁决（`story_action_adjudication.py`）改为在 `chain_of_thought.new()`
生成的独立文档上陈述上下文并调用一次核心 `attempt_to_result`（不继承上游
节拍/Conversation 提示），新增 `public_facts` 参数与 `public_history`
getter（缺省回退 director 已提交事件窗口），成功与失败都经
`record_resolution_diagnostics(stage='action_adjudication', ...)` 归档私有
trace；共享授权文案固化为 `AGENCY_AND_EVIDENCE_RULES` 常量。
`story_resolution.py` 的 `make_final_output_step` 增加入口取值一次的
`public_history` getter（同一份历史进生成与复核文档），`_SEMANTIC_RULES`
规则 2/4/5/7/8 更新并复用共享常量，评审 JSON schema 不变。
`story_dramaturgic.py` 在 build 内把 `display_events.get_pre_act_value`
绑定为裁决与最终步骤的历史 getter（不新增可序列化配置字段）。
`testing_stubs.py` 新增 `stage_omissions`（纯增量）。端到端
`ActionAuthorizationBoundaryTest` 验证提交/拒绝/预算/恢复边界。修改文件
清单：

- 生产：`concordia/contrib/components/game_master/story_action_adjudication.py`、
  `concordia/contrib/components/game_master/story_resolution.py`、
  `concordia/contrib/prefabs/game_master/story_dramaturgic.py`、
  `examples/story_enactment/testing_stubs.py`。
- 测试：同目录 `story_action_adjudication_test.py`、
  `story_resolution_test.py`、`story_prefabs_test.py`、
  `examples/story_enactment/testing_stubs_test.py`、
  `examples/story_enactment/story_enactment_test.py`。
- 文档：`concordia/contrib/components/README.md`（新增 `story_action_adjudication`
  词条、扩写 `story_resolution` 词条）、`concordia/contrib/prefabs/README.md`
  （`story_dramaturgic` 词条补历史 getter 接线）、`examples/story_enactment/README.md`
  （Action Modes 节补待审结果/隔离/一次复核/诊断私有段）、设计文档第 4 行
  状态回填、本记录、计划 checkbox 勾选。

`drama_director.py` 零改动（其 docstring 未宣称语义批准，无需变更）。
`repetition_push.py` / `repetition_push_test.py` 保持开工基线未触碰。

## 各任务 RED/GREEN 证据

（明细见工作区 SDD 台账 task-2..6-report.md。）

| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| Task 2 裁决断言 | `story_action_adjudication_test.py`：`9 failed, 4 passed in 2.05s` exit=1——`TypeError: make_action_adjudication_step() got an unexpected keyword argument 'public_facts'`；`'UPSTREAM future task: …' unexpectedly found`（链文档泄漏上游节拍）；`KeyError: 'stage'`（无诊断归档）；`'remains attributed speech, not objective evidence' not found` | Task 3 后 `11 passed, 2 subtests passed in 1.99s` exit=0 |
| Task 2 评审断言 | `story_resolution_test.py`：`7 failed, 34 passed, 45 subtests passed in 2.15s` exit=1——`'is the act of recording, not certification' not found`（:882）；`AttributeError: module …story_action_adjudication has no attribute 'AGENCY_AND_EVIDENCE_RULES'`；`TypeError: make_final_output_step() got an unexpected keyword argument 'public_history'` | Task 5（含两轮裁定修复）后 `41 passed, 45 subtests passed in 2.06s` exit=0 |
| Task 4 接线断言 | `story_prefabs_test.py`：`2 failed, 5 passed in 2.15s` exit=1——`'Both players saw the sealed envelope.' not found`、`'周律师早在众人面前展示过遗嘱封套。' not found`（裁决 prompt 无 `## Public history (data)`、`public_facts` 为空）；接线后中间态（Task 5 前窗口）`6 failed, 1 passed in 2.10s` exit=1，全部为 build 时 `TypeError: make_final_output_step() got an unexpected keyword argument 'public_history'`（story_dramaturgic.py:328） | Task 5 后 `24 passed, 2 subtests passed in 2.20s` exit=0 |
| Task 6 端到端 | — | `ActionAuthorizationBoundaryTest` `5 passed in 2.51s` exit=0；`pytest examples -q` `392 passed, 80 subtests passed in 21.96s` exit=0 |

执行中两条控制器裁定，各记一行：

1. Task 5：计划新规则 7 文本相对旧文案漏掉 "current"，既有指纹断言
   （`story_resolution_test.py` 的
   `test_action_evidence_reaches_generation_and_review`，:630）按计划自带的
   指纹更新授权，从 `"current speaker's own attempted action"` 更新为
   `"the speaker's own attempted action"`，并采用同文件既有展平匹配惯例
   （`flat = ' '.join(prompt.split())`）；生产规则文本逐字保留未改。
2. Task 2 Step 8：简报正文"三个测试"与其代码块四个测试不一致，按代码块
   为准全部转录（报告已记录）。

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
| `pytest concordia` | 0 | `1472 passed, 33 skipped in 17.46s` |
| `pytest examples` | 0 | `392 passed in 25.10s` |
| `pylint --errors-only -j 1 concordia` | 0 | 无错误（无输出） |
| `pyrefly check concordia` | 1 | 仅既有 4 项依赖错误（`4 errors (875 suppressed)`）：`vllm_model.py` vllm missing-import ×3、`file_reader_server.py:28` FastMCP missing-attribute ×1；无本次源码错误（基线分类与 2026-10-16/17 记录一致，属依赖未装入 venv） |
| `git diff --check` | 0 | 通过（无输出） |

## 格式检查（内容差异得出的全部 Python 文件）

对内容差异得出的 9 个 Python 文件（`story_action_adjudication.py`、
`story_action_adjudication_test.py`、`story_resolution.py`、
`story_resolution_test.py`、`story_dramaturgic.py`、`story_prefabs_test.py`、
`testing_stubs.py`、`testing_stubs_test.py`、`story_enactment_test.py`）：

| 检查 | 退出码 | 结果 |
|---|---|---|
| `pyink --check <changed-python-files.txt>` | 0 | `9 files would be left unchanged.` |
| `isort --check-only <changed-python-files.txt>` | 0 | 通过（无输出） |

## 验收确认清单

| 确认项 | 命令 / 证据 | 结论 |
|---|---|---|
| 核心目录零修改 | `git diff --exit-code --quiet -- concordia/components concordia/document concordia/environment concordia/typing` | 通过（退出码 0，无输出） |
| run_results 只读 | `find run_results -type f -print0 \| sort -z \| xargs -0 sha256sum \| diff - /tmp/opencode/action-auth-baseline/run_results-hashes.txt` | 通过：输出 `READ-ONLY-OK`，退出码 0（182 个文件哈希与开工基线一致） |
| 既有 dirty 基线保留与修改范围 | Task 7 Step 5 的内容快照差异包；未授权文件哈希不变；逐项审阅允许文件的基线到当前 diff | 通过：内容差异共 15 个文件（允许清单 16 项中的 15 项），`unexpected` 为空；`drama_director.py` 零改动（不在差异包），`repetition_push*.py` 哈希与基线一致；逐文件审阅 `task-changes.patch`（2137+ 行）确认各 hunk 均属本计划（Task 2 测试常量/新测试/两处受权指纹更新、Task 3 独立文档与诊断、Task 4 接线、Task 5 规则 2/4/5/7/8 与 public_history、Task 6 stub 与端到端、Task 7 三 README 段落与计划 checkbox 勾选、设计状态行、本记录），除计划明确替换的旧规则文本/旧标题断言/裁定指纹外无任何基线内容被删除或回滚 |
| 调用数未增加 | `ActionAuthorizationBoundaryTest::test_normal_action_turn_call_counts_are_unchanged` | 通过（Task 6 GREEN：`5 passed` exit=0，随 `pytest examples` 全量通过） |
| 拒绝停在上一提交 | `test_illegal_raw_result_rejected_even_when_stage_omits_it` | 通过（同上） |
