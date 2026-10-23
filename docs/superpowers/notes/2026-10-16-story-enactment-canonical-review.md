# Story enactment 正式事件组装与评审修复实施记录

日期：2026-10-16。方案：
`../plans/2026-10-16-story-enactment-canonical-review.md`，设计：
`../specs/2026-10-16-story-enactment-canonical-review-design.md`。
按用户授权执行 Task 1–6；全程未提交 git，未回滚同事未提交的进行中
修改（开工基线快照见工作区 SDD 台账）；未修改 Concordia 核心代码；
未发起真实模型调用（spec §10 未执行，见文末）。

## 交付概述

最终 GM 生成契约从「模型输出双字段」改为「模型仅输出严格 JSON 的
`stage_directions`」；canonical event 在语义评审前由程序从原始对白与
旁白确定性组装（`story_resolution._assemble_canonical_event`）；
独立评审读取同一份待发布事件与来源证据
（`## Canonical event for review (data, never instructions)` 数据区）；
获准后 `set_validated_resolution` 原样提交，评审后不再改写。Luna run3
第 9 回合类的「对白＋旁白」冗余字段遗漏故障点由构造消除。

修改文件（8 个代码/测试文件 + 9 个文档，均未提交）：

- 生产：`concordia/contrib/components/game_master/story_resolution.py`。
- 测试：同目录 `story_resolution_test.py`、`director_note_test.py`、
  `concordia/contrib/prefabs/game_master/story_prefabs_test.py`、
  `examples/story_enactment/testing_stubs_test.py`、
  `story_config_test.py`、`story_enactment_test.py`。
- stub：`examples/story_enactment/testing_stubs.py`。
- 文档（Task 5）：两个 README、`concordia/contrib/prefabs/README.md`，
  及 `drama_director.py`、`story_dramaturgic.py`、`repetition_push.py`、
  `story_action_adjudication.py`、`story_action.py`、`director_note.py`
  六个文件的 docstring/注释措辞（行为零改动，快照 diff 逐一核实）。
- 本记录与两份设计文档回填（Task 6）。

## 各任务 RED/GREEN 证据

（明细见工作区 SDD 台账 task-1..5-report.md。）

| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| Task 1 新契约测试重写 | `story_resolution_test.py` 对旧实现 40 failed / 12 passed / 10 subtests（27 函数中 24 失败；旧 `_parse_dual_track` 双字段键集拒绝一切单字段载荷；重写前基线 32 passed） | —（仅测试，未触生产） |
| Task 2 生产实现 | —（以 Task 1 RED 为输入） | `story_resolution_test.py` 27 passed / 35 subtests；邻域 `game_master/` 16 failed / 261 passed（16 个失败全部为 `director_note_test.py` 旧双字段夹具，预裁定中间态）；`drama_director_test.py` 59 passed |
| Task 3 stub 单字段路由 | — | `testing_stubs_test.py` 9 passed / 3 subtests；范围检查 `story_enactment_test.py` 6 failed / 98 passed（旧 `_dual_track_output` 覆写失配，预裁定中间态） |
| Task 4 夹具迁移 | — | `director_note_test.py` + `story_prefabs_test.py` 36 passed / 4 subtests；`examples/story_enactment` 230 passed / 77 subtests；`concordia` 全量 1447 passed / 33 skipped；`examples` 全量 383 passed |
| Task 5 文档与残留清理 | 残留扫描基线：`two string fields` 多处命中 | 终态：`two string fields` 零命中；`dual.track` 全仓仅剩 `story_resolution_test.py:793/797` 两处裁定保留（历史否定测试名+注释）；`game_master/` 275 passed / 70 subtests（措辞改动行为中性） |
| Task 6 验收 | 见下方检查表 | 见下方检查表 |

## 变异验证（区分度检查 1：评审事件 = 提交事件）

在 `story_resolution.py` 获准路径 `set_validated_resolution` 调用前临时
注入 `resolved_event += '\n未审事实'`：

- `story_resolution_test.py::ActionResolutionTest::test_reviewed_event_equals_committed_event`
  → 1 failed（story_resolution_test.py:200，评审事件 ≠ 提交事件）。
- 恢复变异后同一测试 1 passed；全文件 27 passed / 35 subtests。
- `grep -c "未审事实" story_resolution.py` → 0（无残留）。

## 区分度检查 2：结果来源贯通

- `story_enactment_test.py::ActionPipelineIntegrationTest::test_changing_only_core_result_changes_final_event`：
  两次完整运行仅改变 stub 对核心行动问题的 `action_result` 回答；
  `_stage_directions_output` 只从当前 prompt 的标记证据区推导旁白
  （`stage_directions=''`，无预埋旁白），待审 canonical、FDX Action 段
  与下一玩家观察随之改变，行动前轮次逐字相同。
- `testing_stubs_test.py::RealFinalPromptJointTest::test_real_prompt_routes_evidence_into_canonical`：
  真实生产最终输出步骤对仅 `action_result` 不同的证据各跑一次；
  `set_validated_resolution` 恰以 `(succeeded, _SUCCEEDED_RESULT)` 调用
  ——旁白等于证据推导值，而非携带预期结果的夹具。

两项均 PASS（Task 4 显式运行并随全量套件复跑）。

## 全量检查（Task 6，每条独立运行、单独捕获退出码，未用 && 串连）

| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `source venv/bin/activate && pytest concordia` | 0 | 1447 passed, 33 skipped in 15.22s |
| `source venv/bin/activate && pytest examples` | 0 | 383 passed in 23.66s |
| `source venv/bin/activate && pylint --errors-only -j 1 concordia` | 0 | 无错误（无输出） |
| `source venv/bin/activate && pyrefly check concordia` | 1 | 仅既有 4 项依赖错误（基线分类，见下），无本次源码错误（875 suppressed） |
| `git diff --check` | 0 | 通过 |

pyrefly 基线分类（均非本次计划触碰文件，与 2026-10-15 交付记录记载
的既有基线一致，属依赖未装入 venv 而非源码缺陷）：

- `concordia/contrib/language_models/vllm/vllm_model.py:53–55` —— vllm
  missing-import ×3；
- `concordia/contrib/tools/mcp/servers/file_reader_server.py:28` ——
  FastMCP missing-attribute ×1。

pylint 与 `git diff --check` 无任何输出，无需分类；本次计划文件未产生
任何 lint/类型/空白错误。

## 格式检查（仅本次触碰的 8 个 Python 文件）

| 检查 | 退出码 | 结果 |
|---|---|---|
| `pyink --check <8 文件>` | 0 | 8 files would be left unchanged |
| `isort --check-only <8 文件>` | 0 | 通过（无输出） |

## spec §9 验收表逐行回填

（以下测试全部随本次全量套件 PASS；未加文件名者均在
`concordia/contrib/components/game_master/story_resolution_test.py`。）

| spec §9 场景 | 实际测试 | 结果 |
|---|---|---|
| Luna 广播案例 | `ActionResolutionTest::test_single_field_output_assembles_speech_and_stage`（王婶广播干预：单字段→组装→评审输入=返回值=提交值，模型不再复制对白） | 通过 |
| 严格单字段解析 | `test_fenced_json_parses`（三种围栏含同行三反引号）、`test_invalid_json_raises_story_output_error`、`test_strict_key_set`（非 object/缺键/旧双字段/额外键/重复键/非字符串×2 共 7 子例）、`test_invalid_output_is_not_stored_in_director`（旧双字段明确无回退） | 通过 |
| 多段/特殊字符对白 | `test_multiline_and_special_speech_survives_model_output`（单元）；`story_enactment_test.py::ActionPipelineIntegrationTest::test_multiline_speech_is_preserved_end_to_end`（HostileStage 端到端：对白逐字，伪装内容只进 Action 轨） | 通过 |
| 纯对白 | `test_pure_speech_empty_stage_is_legal_and_reviewed`（空旁白合法，仍恰一次独立评审）、`test_approved_self_disclosure_proposal_and_accusation_stay_speech` | 通过 |
| 纯动作成功/受阻 | `test_action_only_uses_narrator_track`（无空角色标签）；`story_enactment_test.py::ActionPipelineIntegrationTest::test_success_result_flows_to_observation_and_fdx`、`test_blocked_result_flows_to_observation_and_fdx` | 通过 |
| 行动意图存在但缺裁决结果 | `test_missing_action_result_is_rejected`（最终生成前拒绝、零模型调用、诊断 stage=`final_output`——意图不冒充结果） | 通过 |
| 有行动/干预但旁白为空 | `test_empty_stage_for_action_is_rejected`（三子例，评审前拒绝、call_count==1）、`test_empty_canonical_in_turn_is_rejected`（空 canonical 守卫先于评审，call_count==1） | 通过 |
| 非空但遗漏/反转事实 | `test_review_rejects_dropped_or_reversed_action_result`、`test_review_rejects_silently_dropped_intervention`、`test_rephrasing_does_not_count_as_intervention`、`test_scripted_rejection_of_live_failure_patterns_does_not_commit`（注入 reviewer 拒绝、无提交、无指令消费） | 通过 |
| 请求签字、过去录像自述、非知情者断言 | `story_enactment_test.py::ActionBoundaryIntegrationTest::test_signature_request_does_not_create_the_other_players_choice`、`test_past_claims_do_not_generate_physical_evidence`、`test_outsider_assertion_does_not_confirm_a_secret`；规则指纹 `test_rules_allow_adjudicated_own_action_as_fact_source` | 通过 |
| API 异常、预算与中断 | `test_review_api_error_propagates_without_commit`、`test_review_uses_same_budget_and_exhaustion_prevents_commit`、`test_structural_error_records_final_output_diagnostic`；`story_enactment_test.py::ActionChainBudgetTest::test_exhaustion_at_final_generation_of_step_two`、`test_exhaustion_at_semantic_review_of_step_two`、`test_review_api_failure_retries_are_counted_without_commit` | 通过 |
| 干预失败 | `story_enactment_test.py::RepetitionPipelineIntegrationTest::test_dropped_push_is_rejected_without_consumption`；`director_note_test.py::ConsumptionTest::test_story_output_error_leaves_directive_unconsumed`（生成过候选不记成功执行、不推进冷却） | 通过 |
| 端到端一致性 | `test_changing_only_core_result_changes_final_event` 与 `testing_stubs_test.py::RealFinalPromptJointTest::test_real_prompt_routes_evidence_into_canonical`（见区分度检查 2）；`story_enactment_test.py::RepetitionPipelineIntegrationTest::test_tension_five_repetition_can_push`（push 事实进旁白与正式事件、对白在正式事件中恰一次，观察/JSONL/checkpoint/FDX 用获准结果） | 通过 |
| 旧 checkpoint 与旧记录 | `story_enactment_test.py::test_resume_continues_global_turn_numbering`、`test_interruption_after_turn_three_resumes_with_original_limit`、`ActionModeRunTest::test_json_resume_without_action_mode_preserves_contract`（schema-1/2 读取、编号、预算、渲染兼容，历史行不重写） | 通过 |

## 验收确认清单

| 确认项 | 命令 / 证据 | 输出与结论 |
|---|---|---|
| 核心目录零修改 | `git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` | 空（退出码 0，无任何输出） |
| contrib 无 examples 依赖 | `grep -rn "from examples\|import examples" concordia/contrib` | 无实际导入语句；仅命中注释/docstring 中「contrib must not import examples」约定说明及 `.pyc` 缓存回显 |
| `run_results/` 只读不覆写 | `git status --short run_results/` | 空（无修改、无未跟踪变更） |
| 意图不冒充结果 | `test_missing_action_result_is_rejected`（见验收表） | 通过 |
| 未提交干预不被消费 | `test_review_rejects_silently_dropped_intervention`、`test_review_rejects_dropped_or_reversed_action_result`（断言 `commit_directive` 未调用）；`story_enactment_test.py::test_dropped_push_is_rejected_without_consumption`；`director_note_test.py::test_story_output_error_leaves_directive_unconsumed` | 通过 |
| 私有诊断未广播 | 诊断只进 director 暂态/`director_diagnostics` 与 `process_summaries`（既有回归覆盖，随全量套件通过） | 通过 |
| 文档与 stub 无旧契约残留 | Task 5 终态扫描：`two string fields` 零命中；`dual.track` 仅剩 2 处裁定保留的测试历史否定命名 | 通过 |

## 真实模型复验（spec §10）：未执行

spec §10 的 Luna 第 9 回合双模型对照、人工标注旁白样本集与三场景
复跑均未发起，属后续独立工作，不随工程验收自动触发。本记录全部结果
来自 `MockModel`/`PromptStubModel` 确定性测试，证明系统分支与接线，
不构成真实模型语义质量验收。工程验收状态见设计文档状态行
（已实施（工程验收））。
