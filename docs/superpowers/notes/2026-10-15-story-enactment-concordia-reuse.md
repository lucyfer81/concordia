# Story enactment Concordia 复用实施记录（Phase C）

日期：2026-10-16。方案：
`../plans/2026-10-15-story-enactment-concordia-reuse.md`（Task 10–12），
设计 §7–9：
`../specs/2026-10-15-story-enactment-concordia-reuse-design.md`。
按用户授权执行 Phase C；Phase A/B 及其审阅阻塞项已关闭；开工时工作区
保留的未提交 GM 默认模式修复及其测试、README 全部原样保留（见
`examples/story_enactment/run.py` 的 `_validate_action_mode_consistency`
与 `story_enactment_test.py` 的 schema-2 缺省 gm 模式两测）。未提交 git；
未修改 Concordia 核心代码；未发起真实模型调用。

## Task 10：干预状态与提交语义

改动：`concordia/contrib/components/game_master/drama_director.py`、
`director_note.py` 及其测试。

- `DramaDirector` 新增 `repetition_push_enabled=False`、
  `repetition_check_interval=3`（正整数校验）；新增
  `should_attempt_repetition_push()`（只读门控：反馈开启、重复开关开启、
  故事未终止、`turn_id = story_turn + 1` 满足间隔、共享冷却结束、本轮
  开始快照无 pending/integrating 张力指令、本轮未记录重复结果）与
  `record_repetition_result(before, after, trace)`（仅记 checked/
  attempted/proposed_change；无 executed 入参）。
- 检查轮次按已提交 `story_turn + 1` 计算（间隔 3 → 第 3、6、9……轮），
  不以 recent_events 长度代替。
- `set_validated_resolution` 只存本轮获准状态（tension_approved 需本轮
  真实融合出 tension 候选，遗留 integrating 不算；repetition_approved
  需本轮 proposed_change），删除提前 `commit_directive()` 的路径与该
  方法；`post_observe` 在事件观察后构造完整 pending commit（指令消费、
  repetition_check_count/execution_count、`last_intervention={turn_id,
  reason}`、共享冷却基线、投影后的指令状态），`update` 幂等提升。
- 后处理用本轮获准干预的投影冷却：repetition 当轮执行后同轮不再生成
  张力指令；节拍完成步不生成旧节拍指令。
- 候选/失败 trace 进入 `resolution_diagnostics_history`，可同时查询；
  成功轮的候选与 trace 进入 StoryTurn 私有 `director_diagnostics`
  （intervention、repetition、resolution_diagnostics），不广播。
- `get_state/set_state` 保存 repetition_check_count、
  repetition_execution_count、last_intervention；旧 state 缺字段按
  0/0/None 读取且保留 `last_directive_executed_turn` 冷却基线。
- `director_note` 融合后经 `record_intervention_candidate(reason='tension')`
  记录 before/after/trace，不消费指令；融合失败分支记诊断。

RED（实现前）：`drama_director_test.py::RepetitionStateTest` 14 failed；
`director_note_test.py` 3 failed（含按新提交语义更新的既有断言）。
GREEN：定向 `drama_director_test.py director_note_test.py`
71 passed, 18 subtests。

## Task 11：核心重复 helper 直接复用

新增：`concordia/contrib/components/game_master/repetition_push.py`、
`repetition_push_test.py`；修改 `examples/story_enactment/testing_stubs.py`
及测试。

- `make_repetition_push_step(director, model)`：门控不满足时恒等、零
  模型调用；满足时用同一包装模型建独立 InteractiveDocument，显式写入
  有序已提交公开历史、未提交候选、本轮行动结果、白名单、不可变事实、
  玩家名单、GM 私密边界与判定规则（近期争执≠重复；新事实/行动结果/
  有效回应构成进展；一次性事件不可重复；未来节拍与隐藏真相不是进展），
  原样调用 `event_resolution.maybe_inject_narrative_push`，真实返回值
  原样传播并经 `record_repetition_result` 记录。
- 不复制检测器、不另生成建议、不重抽；建议列表在核心内部副本上生成、
  随机选择保留生产行为——外层只归档可见 trace 与选中/融合结果（before/
  after 可核查选中事实），不为此修改核心。
- API/预算异常前部分 trace 经 `record_resolution_diagnostics(
  stage='repetition_push')` 留暂态后原样抛出；失败不记成功、不占门控，
  同轮未提交重试可再检查（choice/suggestion/merge 三位置 subTest）。
- stub：merge 答案引用题面真实编号候选（保留真实行动结果）+ 可选后缀；
  `push_events` 配置即启用建议/融合路由；真实核心问题联测补齐
  （`test_real_core_push_prompts_route_end_to_end`），评分路由不被截获。
  测试固定 `stage_directions=''`，不预埋预期干预；确定性用单条建议。

RED：模块缺失 ImportError。GREEN：`repetition_push_test.py
testing_stubs_test.py` 14 passed, 3 subtests。

## Task 12：接线、配置与恢复

改动：`concordia/contrib/prefabs/game_master/story_dramaturgic.py`、
`examples/story_enactment/run.py` 及测试。

- RESOLVE 链固定为：自身行动裁决 → director_note → repetition_push →
  最终双轨生成 → 独立语义复核 → canonical event → 既有广播、采集与
  checkpoint。repetition 步骤使用传入 prefab 的同一包装模型。
- 配置矩阵：`beats_only`/`tension_observe` 显式记录 `repetition_push_
  enabled=False`；`director`/`character_depth` 默认 True 且可显式关闭；
  恢复缺开关（含 A/B schema-2 checkpoint）一律按 False 读取，mode 不
  自动启用；间隔默认 3，`_normalize_run_config` 与 `DramaDirector`
  双重正整数校验。开关与间隔进入有效 run config → gm_params → prefab
  → director。恢复仍使用保存的模式、模型身份和原始预算。
- 集成测试用真实 run()/引擎/collector/下一玩家观察/FDX；故障用按调用
  定位脚本（第三轮复核才非法），恢复保持同一 model_config（含
  prompt_stub），仅更换注入 stub 对象的故障行为。

RED：prefab 2 failed + MakeRunConfig 2 failed + 集成 10 failed。
GREEN：`story_enactment_test.py story_prefabs_test.py`
124 passed, 27 subtests。

## 运行时变异验证

`/tmp/opencode/mutation_check.py`：wraps-spy 证明核心 helper 真实执行
（真实返回值含新环境事件），但将其返回值替换为不含新事件的非空候选后，
turn-3 stage_directions 不再含新事件——传播测试断言必然 RED；去除变异
后同一场景 GREEN（checked-in 测试即覆盖）。固定批准的 stub 只证明流程，
不代表真实语义判断准确。

## 场景 → 测试映射

| 场景 | 测试 |
|---|---|
| 真实提交两轮后第三轮门控（非塞 recent_events） | RepetitionStateTest::test_third_turn_gate_uses_two_committed_turns |
| 评分 5、历史重复 → 第三轮实际调用核心 helper，新环境事实首现该轮 | RepetitionPipelineIntegrationTest::test_tension_five_repetition_can_push |
| 近期有进展，helper 判不重复 → 只记检查、无执行冷却 | test_fresh_progress_does_not_force_push |
| 评分 9 真实生成 pending → 下一轮只走 tension、重复调用为零 | test_pending_tension_strictly_suppresses_repetition |
| tension 融合失败本轮不回退 repetition | RepetitionStateTest::test_pending_tension_wins_even_if_fusion_fails |
| helper 改候选但最终旁白丢弃 → reviewer 收全据拒绝、无消费 | test_dropped_push_is_rejected_without_consumption |
| helper 原样返回 → 不计执行不进冷却 | RepetitionStateTest::test_no_change_only_commits_check；RepetitionPushStepTest::test_unchanged_result_records_check_only |
| 行动结果与干预同轮并存（final/review/canonical/观察/JSONL/checkpoint/FDX） | test_tension_five_repetition_can_push；RepetitionPushStepTest::test_action_result_survives_push_merge |
| 第三轮复核拒绝/非法 JSON → 前两轮真实提交、第三轮不提交 | test_rejection_on_turn_three_keeps_two_commits |
| 第三轮 checkpoint 后中断 → 恢复沿用原上限 6、编号连续、不重放干预 | test_interruption_after_turn_three_resumes_with_original_limit |
| choice/建议/融合预算耗尽（path A）/ 获准后完成判断耗尽（path B） | test_budget_paths_cover_repetition_calls |
| 共享冷却保存恢复、旧状态加载 | RepetitionStateTest::test_shared_cooldown_and_reason_survive_restore、test_old_state_preserves_tension_cooldown |
| 同身份恢复续跑 / 身份与预算负例 | test_same_identity_resume_continues_after_targeted_rejection、test_resume_does_not_change_model_identity_or_raise_budget |
| JSON 动作模式恢复省略参数不被当对白 | test_json_resume_without_action_mode_preserves_contract |
| 模式/开关矩阵与 RESOLVE 实际路径 | MakeRunConfigTest::test_repetition_switch_follows_the_mode_matrix、test_old_config_without_repetition_switches_reads_off；StoryRepetitionWiringTest |

## 检查结果（每项独立运行，格式化后复跑）

| 检查 | 退出码 | 结果 |
|---|---|---|
| 逐任务定向（Task 10/11/12） | 0/0/0 | 71+18s / 14+3s / 124+27s passed |
| pytest concordia | 0 | 1447 passed, 33 skipped, 101 subtests（基线 1425+33） |
| pytest examples | 0 | 362 passed, 73 subtests（基线 349） |
| pylint --errors-only -j 1 concordia | 0 | 无错误 |
| pyrefly check concordia | 1 | 仅既有 4 项依赖错误（vllm×3、FastMCP×1），无新增源码错误 |
| pyink/isort（本次修改的 12 个 Python 文件） | 0 | 全部通过 |
| git diff --check | 0 | 通过 |

核心文件零修改（`git status` 无 `concordia/components/`、
`concordia/document/`、`concordia/language_model/` 改动）；contrib 无
examples 依赖；私有 trace 只进 director_diagnostics/暂态，不进广播。

## 遗留与边界

- 未提交干预消费、门控、单路执行、提交/恢复、秘密隔离、预算正确性问
  题在本轮全部修复，无 minor 延后项。
- 核心内部五条建议不可访问（内部副本随机选择），按设计不为此修改核心；
  归档口径为外层 trace + before/after。
- 失败候选暂态与进程摘要归档属 Phase D（Task 13），本阶段保留暂态接口。
- 停在 Phase C 验收点：未进入 Phase D，未运行真实模型批量测试，未提交
  git，未覆盖既有真实运行产物。

## Phase C 验收后修复（2026-10-16，仅修复不进 Phase D）

全量回归与 Phase C 交付一致，但额外探针复现 3 个未覆盖问题；按
RED→GREEN 逐项修复，未改核心代码，未发起真实模型调用，未提交 git。

### 修复 1（P1）：失败续跑不再回退已消耗的模型预算

- 根因：resume 的 `used_at_start` 只读剧情 checkpoint
  （`examples/story_enactment/run.py` 的 resume 分支），而 checkpoint 只在
  提交边界保存；失败回合已发生的调用随进程消失。实测宽上限下首进程
  真实消耗 24、checkpoint 记 15，续跑又从 15 起步，累计口径 43 却被报告
  为 34。
- 修复：新增 sidecar `process_usage.json`
  （`_write_process_usage`/`_load_process_usage`），在 play 的
  `finally`（含 BaseException 崩溃路径）原子写入本进程最终累计
  `used_model_attempts`/`used_api_retries`；resume 取 checkpoint 与
  sidecar 两者的较大值恢复预算，剧情 checkpoint 边界不动，path A/B
  语义不变。
- RED：`story_enactment_test.py::
  RepetitionPipelineIntegrationTest::test_resume_accumulates_usage_of_failed_turn`
  宽上限子场景 `34 != 43`（续跑汇总未按真实累计报告）。GREEN：该测试
  通过；紧上限子场景同时证明续跑只在原上限内获得剩余额度（真实累计触
  顶即 budget_limit 停在已提交边界）。
- Task 13（Phase D）的进程摘要归档可在此基础上扩展；本次只落最小
  usage 记录。

### 修复 2（P1）：融合被拒后同实例同轮重试不再卡死

- 根因：`mark_directive_integrating` 直接改写已提交指令状态
  （drama_director.py），复核拒绝后无提交也无回滚；重试轮
  `get_pending_directive()` 为 None、融合零调用、重复门控 False，本轮
  干预路径全灭。
- 修复：`pre_observe` 的新 putative event 先经
  `_rollback_uncommitted_integration` 把上一未提交尝试留下的
  integrating 指令恢复为该尝试开始时的 pending（按 beat_id+
  generated_at_turn 校验同一指令）；failed/consumed/invalidated 等终态
  与已提交迁移不回滚。已提交状态与提交语义本身未变。
- RED：`drama_director_test.py::RepetitionStateTest::
  test_rejected_fusion_attempt_restores_pending_on_retry` 与
  `director_note_test.py::ConsumptionTest::
  test_rejection_then_same_instance_retry_refuses_again` 均在重试轮
  `get_pending_directive()` 断言 None 失败（复现卡死）。GREEN：两测通
  过——pending 恢复、真实链路重新融合（complication/merge 各 +1）、
  重试轮正常提交并恰好消费一次（executed_at_turn=2）；磁盘 checkpoint
  重建路径原有测试不变。

### 修复 3（P2）：attempted 与候选变化分别记录

- 根因：`record_repetition_result` 直接令 `attempted=proposed_change`
  （drama_director.py）；核心判重复、完成建议与融合但融合返回原文时
  误记 attempted=False。
- 修复：`record_repetition_result` 新增显式必填 `attempted` 入参并独立
  存储；`repetition_push` 步骤用外层文档的模型回复条目数（MODEL_TAG
  计数差）判定真实尝试——仅判定 +1、判定+融合 +2（建议在核心内部副本
  上，不落外层文档），与提示词措辞无关，未修改核心。执行计数、执行/
  冷却仍只由 proposed_change+获准提交驱动，保持不变。
- RED：`repetition_push_test.py::RepetitionPushStepTest::
  test_attempted_but_unchanged_records_attempt_not_change`
  `attempted: False != True`；`drama_director_test.py::
  RepetitionStateTest::test_attempted_but_unchanged_records_attempt_not_change`
  同口径；既有 9 处 `record_repetition_result` 调用/断言按新签名更新
  （其中 12 项定向失败全为新 kwarg/新语义，非误改）。
- GREEN：上述测试通过；融合返回原文场景记
  `{checked: True, attempted: True, proposed_change: False, executed:
  False}`，只计检查、不进候选、不占冷却（第六轮可再查）。

### 新增场景 → 测试映射

| 场景 | 测试 |
|---|---|
| 失败回合调用跨恢复累计（宽上限累计口径 / 紧上限触顶即停） | story_enactment_test::test_resume_accumulates_usage_of_failed_turn |
| 融合后复核拒绝 → 同实例同轮重试恢复 pending（状态机层/真实链路层） | RepetitionStateTest::test_rejected_fusion_attempt_restores_pending_on_retry；ConsumptionTest::test_rejection_then_same_instance_retry_refuses_again |
| 核心判重复+融合返回原文：attempted 与 proposed_change 分记、执行/冷却不变 | RepetitionPushStepTest::test_attempted_but_unchanged_records_attempt_not_change；RepetitionStateTest::test_attempted_but_unchanged_records_attempt_not_change |

### 检查结果（每项独立运行并单独捕获退出码）

| 检查 | 退出码 | 结果 |
|---|---|---|
| pytest concordia | 0 | 1451 passed, 33 skipped, 101 subtests（Phase C 基线 1447+33，+4 新测试） |
| pytest examples | 0 | 363 passed, 73 subtests（基线 362，+1 新测试） |
| pylint --errors-only -j 1 concordia | 0 | 无错误 |
| pyrefly check concordia | 1 | 仅既有 4 项依赖错误（vllm×3、FastMCP×1），无新增源码错误 |
| pyink/isort（本次修改的 7 个 Python 文件） | 0 | 全部通过 |
| git diff --check | 0 | 通过 |

修改文件：`drama_director.py`、`repetition_push.py` 及其测试、
`director_note_test.py`、`examples/story_enactment/run.py`、
`story_enactment_test.py`、两个 README。核心目录
（`concordia/components/`、`concordia/document/`、
`concordia/language_model/`）零改动。停在修复验收点：未进入
Phase D，未运行真实模型，未提交 git。

# Story enactment Concordia 复用实施记录（Phase D）

日期：2026-10-16。依据：计划 Task 13–14
（`../plans/2026-10-15-story-enactment-concordia-reuse.md`）。
按用户授权仅执行 Task 13–14；保留 A/B/C 及三个审阅修复的全部修改，
未 reset/stash/覆盖；未提交 git；未修改 Concordia 核心代码；未发起
真实模型调用。

## Task 13：源码、候选与独立进程摘要归档

修改：`examples/story_enactment/provenance.py`、`provenance_test.py`、
`run.py`、`run_provenance_test.py`、`examples/story_enactment/README.md`、
`concordia/contrib/components/README.md`、`concordia/contrib/prefabs/README.md`。

### provenance.py

- 源码快照补齐 `story_action.py`、`story_action_adjudication.py`、
  `repetition_push.py` 与最终收口 `story_terminator.py`；核对 run、
  provenance、配置（story_config）、stub（testing_stubs）、director、
  director_note、story_resolution、prefab（story_dramaturgic）原已在
  清单内；每项存实际源码文本 + SHA-256。
- `archive_invocation(...) -> str` 返回唯一 `invocation_id`（微秒 UTC
  时间戳 + uuid4 随机后缀，跨恢复唯一）；新 manifest 条目同时记录
  `invocation_id` 与相对 `summary_path`（`process_summaries/<id>.json`）。
  旧条目缺 ID/摘要保持原样，不补造历史；`history_complete` 语义不变。
- 新增 `write_process_summary(state_dir, *, invocation_id, summary)`：
  临时文件完整写入（含 fsync）后用 `os.link` 原子发布——目标已存在时
  `FileExistsError` 转为 ValueError 拒绝，原文件字节不变；未使用会覆盖
  目标的 `os.replace` 式发布。写入前经 `_without_credentials` 清洗
  （覆盖配置与新增异常/诊断数据；故事秘密保留）。
- 新增 `read_invocation(state_dir, invocation_id)` 供收尾对齐 manifest。

### run.py

- invocation 生命周期：archive（首次模型请求前，含 premise 注入与
  初始 checkpoint 之后、play 之前）→ 执行 → finally 收尾。收尾对
  以下全部路径各产出一份独立摘要：正常完成、path A/B 预算停止、
  StoryOutputError、API 重试耗尽（均为现有 `except Exception` →
  RunResult(reason='error')）、KeyboardInterrupt/SimulatedInterruption
  等可捕获中断（记 `interrupted` 后原异常继续传播）、零步恢复。
  当前传播的 BaseException 依旧原样传播；SIGKILL/断电不保证收尾，
  文档明示、不虚构摘要。
- 收尾步骤逐个独立守护（诊断深复制 → usage sidecar → 读 committed →
  读 manifest 条目 → 写摘要），前一步失败不跳过后续；有原异常时收尾
  错误仅打 stderr 不遮蔽原异常，无原异常时抛 RuntimeError 明确报错。
  `_write_process_usage` 从 play 内 finally 移入统一收尾（覆盖面不变
  且新增零步/中断路径写入）。
- 摘要 schema_version=1：invocation_id/resume/source_version（与
  manifest 对应）、started/finished_at_utc、monotonic
  `process_wall_time_seconds`（仅本次 invocation，区别于 play 耗时）、
  reason（正常沿用现有终止原因，中断记 interrupted）、exception
  （type/message 或 null）、committed_turn_id_at_start/end（读实际提交
  边界）、used_model_attempts/used_api_retries 起止累计值与本次增量
  （guard/retry wrapper 终值 − 恢复起点，恢复起点取 checkpoint 与
  process_usage.json 的较大值；不用 checkpoint 调用数之差）、原始两项
  总预算（恢复不得提高）、`uncommitted_resolution`（暂态清除前深复制：
  action_result、干预 reason+candidate before/after、trace history、
  最终复核拒绝原因/失败阶段；无未提交尝试时为 null）。
- 失败候选不进 StoryTurn/观察/剧本；核心 helper 内部五条建议不可访问
  的限制未变，未修改核心、未拦截模型。

### RED → GREEN（真实命令输出）

RED（实现前，`pytest examples/story_enactment/provenance_test.py
examples/story_enactment/run_provenance_test.py -q`）：
`19 failed, 6 passed in 3.13s`（SourceArchiveTest 缺三个新源码模块、
manifest 无 invocation_id、无 process_summaries、重复 ID 未拒绝、
中断/零步/收尾失败路径无摘要、usage 口径断言失败等；其中
`test_usage_and_summary_agree_after_failed_resume` 的 24/15 断言先于
KeyError 通过，证实基线数值）。

GREEN（实现后，同一命令）：
`21 passed, 4 subtests passed in 3.37s`（含强化后的既有 3 测：setUp
钩子现要求首个模型请求前最新 invocation 已带 ID 与 summary_path）。
实施中发现并修正两处测试预期：成功 push 的诊断 stage 为
`intervention_repetition`（`repetition_push` 仅 API/预算失败路径）；
legacy manifest 剥离 ID 后首次运行的孤儿摘要文件保留且不被续跑改写。

### 场景 → 测试映射（run_provenance_test.py::ProcessSummaryTest 等）

| 场景 | 测试 |
|---|---|
| 三个新适配器 + 最终收口源码与 hash 入档 | provenance_test::SourceArchiveTest::test_new_adapters_have_content_and_hashes |
| 首次模型调用前 manifest 已有完整新 invocation | ProcessSummaryTest::test_archive_exists_before_first_request；RunProvenanceTest setUp 钩子（全部模式） |
| 成功/失败各得独立摘要，旧摘要字节不变、manifest 可寻址 | test_success_and_failure_each_get_immutable_summary |
| 重复 ID 拒绝写入、原摘要字节不变 | test_duplicate_summary_id_cannot_overwrite |
| 失败候选与部分 trace 归档且不提交：checkpoint 停两轮、JSONL/FDX 无候选 | test_failed_candidate_and_partial_trace_are_archived_without_commit |
| 可捕获中断（SimulatedInterruption/KeyboardInterrupt）保存摘要后原样重抛 | test_caught_interruption_preserves_summary_and_rethrows |
| 零步恢复：新 ID、新摘要、增量 0、无模型调用 | test_zero_step_resume_gets_summary_without_model_calls |
| 首进程 24、checkpoint 15、上限 30；续跑仅剩 6、累计报告与真实调用均 30 | test_usage_and_summary_agree_after_failed_resume |
| 完整归档复制到新目录后恢复不回退旧 checkpoint 数值；归档九项齐全 | test_archive_roundtrip_preserves_failed_turn_usage |
| 收尾 I/O 错误不遮蔽原异常、其他收尾不被跳过；无原异常时归档失败明确报错 | test_cleanup_failure_does_not_mask_original_exception、test_archive_failure_without_original_exception_is_raised |
| 假凭据不入任何归档文件；私有 trace 归档但不进剧本 | test_credentials_and_private_trace_do_not_leak |
| 旧 manifest 缺 ID/摘要 + 缺 process_usage 兼容读取、不伪造历史 | test_legacy_manifest_and_missing_usage_remain_compatible |

Phase C 回归（预算累计、同实例重试、attempted/proposed_change 分离）
随全量套件通过（concordia 1451 + examples 376，含
`test_resume_accumulates_usage_of_failed_turn`、
`test_rejected_fusion_attempt_restores_pending_on_retry`、
`test_attempted_but_unchanged_records_attempt_not_change`）。

### 完整归档与恢复验证

`test_archive_roundtrip_preserves_failed_turn_usage` 实测：首轮失败后
state_dir 含全部九项（run_manifest.json、checkpoint.json、
story_turns.jsonl、process_usage.json、process_summaries/、summary.json、
script.fdx、script.fountain、script.txt）；`shutil.copytree` 到新目录后
恢复，预算从 sidecar 累计 24 起步（非 checkpoint 15），仅剩 6 次调用
触顶 30 停止（reason=budget_limit）；源目录 manifest 历史保持 1 条不
变。未产生文件的场景（如首次失败前的 summary.json 缺失）按实际缺失
处理，不伪造补齐。

## Task 14：全量检查

每条命令独立运行、单独捕获退出码（输出存
`/tmp/opencode/phase_d_checks/`），未用 `&&` 串连：

| 检查 | 退出码 | 结果 |
|---|---|---|
| pytest provenance_test.py run_provenance_test.py -q | 0 | 21 passed, 4 subtests |
| pytest concordia | 0 | 1451 passed, 33 skipped, 101 subtests（开工基线 1451+33 持平） |
| pytest examples | 0 | 376 passed, 73 subtests（基线 363，+13 新测试） |
| pylint --errors-only -j 1 concordia | 0 | 无错误 |
| pyrefly check concordia | 1 | 仅既有 4 项依赖错误（vllm×3、FastMCP×1），无新增源码错误（基线问题，非本次引入） |
| git diff --check | 0 | 通过 |
| pyink/isort（本次修改的 4 个 Python 文件逐一检查） | 0 | provenance.py、provenance_test.py、run.py、run_provenance_test.py 全部通过 |

核心文件零修改（`git status` 于 `concordia/components/`、
`concordia/document/`、`concordia/language_model/`、`concordia/prefabs/`
均为空）；contrib 无 examples 依赖（grep 仅命中注释）；私有诊断只进
process_summaries 与 director_diagnostics，未广播、未进剧本。

### 计划验收矩阵（逐项实际状态）

| 设计要求/场景 | 任务与关键测试 | 状态 |
|---|---|---|
| 严格 JSON、重复键、空字段、原对白空白 | Task 1/2/3/5；story_action/story_resolution 编解码与结构测试 | 通过（pytest concordia） |
| 查看原件成功，结果抵达观察与 FDX | Task 7/8/9；test_success_result_flows_to_observation_and_fdx | 通过 |
| 一把钥匙行动受阻、无他人强制配合 | Task 9；test_blocked_result_flows_to_observation_and_fdx | 通过 |
| 核心调用结果不可被丢弃，成功/失败有差异 | Task 5/8/9；test_changing_only_core_result_changes_final_event | 通过 |
| 他人签字/认罪、本人同意、过去录像自述 | Task 9；ActionBoundaryIntegrationTest | 通过 |
| 私密债务断言不变事实、未知文书内容不编造 | Task 3/8/9；ActionBoundaryIntegrationTest | 通过 |
| Gemini 草稿压平空白，canonical/FDX 精确保真 | Task 8/9；test_multiline_speech_is_preserved_end_to_end | 通过 |
| 纯动作无伪对白，stage/canonical 不为空 | Task 4/8/9；story_action 纯动作测试 | 通过 |
| Luna 连续等待核对、张力5仍可查重复 | Task 11/12；test_tension_five_repetition_can_push | 通过 |
| 近期新事实/有效回应不强制干预 | Task 11/12；test_fresh_progress_does_not_force_push | 通过 |
| pending 优先、一轮一路、共享冷却 | Task 10/12；test_pending_tension_strictly_suppresses_repetition | 通过 |
| 候选改变≠执行，丢弃候选须拒绝 | Task 8/10/12；test_dropped_push_is_rejected_without_consumption | 通过 |
| 提交前/后预算路径覆盖新增调用 | Task 7/9/11/12；test_budget_paths_cover_repetition_calls | 通过 |
| 真正 schema1 恢复、JSON 模式省略参数恢复 | Task 6/12；ActionModeRunTest | 通过 |
| 原预算/身份恢复、第三轮故障、无重复提交 | Task 12；test_rejection_on_turn_three_keeps_two_commits、test_interruption_after_turn_three_resumes_with_original_limit、test_same_identity_resume_continues_after_targeted_rejection、test_resume_does_not_change_model_identity_or_raise_budget | 通过 |
| 源码/候选/失败摘要不可覆盖，checkpoint 齐全 | Task 13；ProcessSummaryTest 全部 12 测 | 通过（本次新增） |
| 角色底线/秘密与观测隔离回归 | Task 9/13；story_isolation_test.py | 通过 |

### 真实模型验收：未执行

本阶段全部结果来自确定性 stub（PromptStubModel）。计划中两个真实
缩减场景（Luna 持续“等核对日期”须兑现合法查看行动；Gemini “先声称
录像、再补防伪构图”须保持自述/证据边界）及 3×3 真实运行对照
（固定故事/模型/源码版本/预算，先 `speech_and_action +
tension_observe` 再同条件开 director，人工质量阈值与记录字段待用户
在批量调用前固定）均**未执行**，未发起任何真实模型批量调用，确定性
测试通过不构成真实模型质量验收。

停止在 Phase D（Task 13–14）工程验收点，待整体复审。

## Phase D 验收后修复（2026-10-16，三个故障注入问题）

工程验收暂不通过：故障注入复现三个未满足交付要求的问题。逐项
RED→GREEN 修复，未改核心代码，未发起真实模型调用，未提交 git。

### 修复 1（P1）：usage 写入失败后恢复不再超预算

- 根因：resume 只从 checkpoint 与 usage sidecar 恢复消耗
  （`examples/story_enactment/run.py` 的 resume 分支）；sidecar 写入
  失败但独立摘要已保存 24 时，续跑退回 checkpoint 的 15，多授予
  15 次调用——实测续跑后累计真实调用 39、报告 30，超过原上限 30。
- 修复：新增 `provenance.restore_recorded_usage(state_dir, *,
  attempt_limit)`，对 `process_summaries/` 内每个已发布摘要（跳过
  未发布的 `.` 临时文件）严格校验 schema 与累计计数范围后取最大
  值；任一摘要无法解析或计数越界（含 > 上限、重试 > 调用）视为
  消耗不可确定，直接 ValueError 拒绝恢复，绝不静默退回旧
  checkpoint。resume 基线改为 checkpoint、sidecar、摘要记录三者
  取最大。
- RED：`run_provenance_test.py::
  ProcessSummaryTest::test_resume_restores_budget_from_summaries_when_usage_write_fails`
  断言续跑增量 6，实测 `15 != 6`（与验收故障注入一致）；GREEN：
  该测试通过——首进程摘要保存 24、sidecar 缺失，续跑仅剩 6、累计
  报告与真实调用均为 30、reason=budget_limit。
  `provenance_test.py::RecordedUsageTest` 补充：多摘要取最大、临时
  文件忽略、corrupt/缺计数/越界/重试>调用四类一律拒绝。

### 修复 2（P1）：异常消息中的凭据脱敏

- 根因：`provenance.py` 的 `_without_credentials` 只删凭据字典键，
  字符串原样保留；提供方异常消息引用假 API key 时，该值经
  `error_message` 同时进入 summary.json 与独立进程摘要。
- 修复：新增 `provenance.scrub_credentials(value, *,
  secret_texts=())`（键删除 + 递归文本替换 `[REDACTED]`）；
  `run.py` 新增 `_runtime_secret_texts`（从有效 model config 的凭据
  键收集值，长度 ≥8 才参与替换，避免玩具键大面积改写故事文本），
  `_write_outputs` 与 `_finalize_invocation` 写档前统一 scrub。
  返回给调用方的 `RunResult.error_message` 保持原样（调用方即密钥
  持有者），仅归档脱敏。
- RED：`ProcessSummaryTest::test_exception_text_credentials_are_redacted_in_archives`
  假 key 同时出现在两份归档；GREEN：secret 不在两档、`[REDACTED]`
  在两档、返回结果仍含原始诊断。

### 修复 3（P2）：核心调用内中断保留部分 trace

- 根因：`concordia/contrib/components/game_master/repetition_push.py`
  的核心 helper 调用仅 `except Exception`；KeyboardInterrupt 等可捕
  获 BaseException 直接穿透，暂态诊断未记录，摘要
  `uncommitted_resolution=None`，而融合提示已含选中事件、证据本可
  保存。
- 修复：改 `except BaseException`——先 `record_resolution_diagnostics
  (stage='repetition_push')` 归档外层可见 trace 再原样重抛；仍不调
  用 `record_repetition_result`（无检查成功、无 attempt、无执行计
  数，门控不占用），模块 docstring 同步说明。
- RED：`repetition_push_test.py::RepetitionPushStepTest::
  test_interrupted_core_call_keeps_partial_trace`（单元层，
  KeyboardInterrupt 于 merge 位）与
  `run_provenance_test.py::ProcessSummaryTest::
  test_interrupted_core_call_preserves_partial_trace`（真实 run 层）
  均失败；GREEN：两测通过——摘要 reason=interrupted、
  uncommitted_resolution 含 `repetition_push` trace（含选中事件与
  KeyboardInterrupt error）、checkpoint 停在第二轮、无候选无门控
  占用、原异常继续传播。

### 新增场景 → 测试映射

| 场景 | 测试 |
|---|---|
| sidecar 写失败、摘要记录 24：续跑从摘要恢复仅剩 6、不超原上限 | ProcessSummaryTest::test_resume_restores_budget_from_summaries_when_usage_write_fails |
| 摘要取最大/临时文件忽略/不可确定消耗一律拒绝 | RecordedUsageTest::test_max_across_summaries_restores_true_cumulative_usage、test_undeterminable_usage_refuses_instead_of_falling_back |
| 异常文本含假凭据：归档脱敏、返回值保留 | ProcessSummaryTest::test_exception_text_credentials_are_redacted_in_archives |
| 核心融合调用内 KeyboardInterrupt：trace 归档后原样重抛、无成功/执行计数 | RepetitionPushStepTest::test_interrupted_core_call_keeps_partial_trace；ProcessSummaryTest::test_interrupted_core_call_preserves_partial_trace |

### 检查结果（每项独立运行并单独捕获退出码，格式化后复跑）

| 检查 | 退出码 | 结果 |
|---|---|---|
| 定向 provenance_test.py run_provenance_test.py -q | 0 | 26 passed, 8 subtests |
| pytest concordia | 0 | 1452 passed, 33 skipped, 101 subtests（Phase D 基线 1451+33，+1 新测试） |
| pytest examples | 0 | 381 passed, 77 subtests（基线 376，+5 新测试） |
| pylint --errors-only -j 1 concordia | 0 | 无错误 |
| pyrefly check concordia | 1 | 仅既有 4 项依赖错误（vllm×3、FastMCP×1），无新增源码错误 |
| git diff --check | 0 | 通过 |
| pyink/isort（本次修改的 6 个 Python 文件逐一检查） | 0 | 全部通过 |

修改文件：`provenance.py`、`provenance_test.py`、`run.py`、
`run_provenance_test.py`、`repetition_push.py`、
`repetition_push_test.py`、`examples/story_enactment/README.md`、
`concordia/contrib/components/README.md`。核心目录
（`concordia/components/`、`concordia/document/`、
`concordia/language_model/`、`concordia/prefabs/`）零改动。真实模型
验收仍为**未执行**。停止在修复验收点，待整体复审。

## Phase D 二轮验收修复（2026-10-16，剩余两项）

上一轮三个问题已确认通过；本轮修复故障注入复现的剩余两项，
RED→GREEN，未改核心代码，未发起真实模型调用，未提交 git。

### 修复 4（P1）：双消耗记录写入失败后预算不再回退

- 根因：`provenance.restore_recorded_usage` 在无摘要目录时直接返回
  零消耗，未识别 manifest 中已声明 `summary_path` 但从未发布的
  invocation。同时注入 usage sidecar 与进程摘要写入失败时：首进程
  真实消耗 24、checkpoint 15、manifest 已有 invocation 条目但两份
  记录均缺失，续跑把缺失当零、退回 checkpoint 15，多授予 15 次——
  实际累计 39、报告 30。
- 修复：新增 `_require_declared_summaries(state_dir)`，在
  `restore_recorded_usage` 入口先核对 manifest：条目声明了
  `summary_path` 而文件缺失（或 manifest 本身
  corrupt/schema 未知）→ ValueError 拒绝恢复（消耗不可确定）；
  旧格式条目（无 invocation_id/summary_path）不要求任何摘要，
  保持既有兼容；无 manifest 的更早运行同样不受影响。
- RED：`run_provenance_test.py::ProcessSummaryTest::
  test_resume_refused_when_both_usage_records_failed`（双写失败后续跑
  不抛 ValueError，实际把 15 次额度全部用尽触发 BudgetExhaustedError）
  与 `provenance_test.py::RecordedUsageTest::
  test_archived_invocation_without_summary_refuses`（archive 后无摘要
  仍返回零）；GREEN：两测通过——续跑在
  ValueError('cannot be determined') 处拒绝、committed checkpoint
  保持两轮供人工恢复；剥除声明字段的旧格式条目返回零、可正常恢复。

### 修复 5（P2）：短凭据同样脱敏

- 根因：`run.py::_runtime_secret_texts` 自行加入长度 ≥8 才脱敏的
  豁免（未经约定）；27 字符假凭据已脱敏，6 字符假凭据仍随
  `error_message` 进入 summary.json 与独立摘要。
- 修复：移除长度豁免——凡非空凭据值（CREDENTIAL_KEYS 键下）一律
  参与递归文本替换为 `[REDACTED]`；替换只改秘密自身出现处，原对白
  与剧情事实字节不变（story_turns.jsonl、剧本导出本身不经 scrub）。
- RED：`ProcessSummaryTest::test_exception_text_credentials_are_redacted_in_archives`
  改用 6 字符假凭据 `SK-42x`（第三轮复核处抛含 key 的
  RuntimeError，前两轮正常提交）——两份归档中出现明文；
  GREEN：secret 不入两档、`[REDACTED]` 在两档、返回结果保留原始
  诊断、已提交对白在 JSONL 与 script.txt 中逐字保留。

### 新增场景 → 测试映射

| 场景 | 测试 |
|---|---|
| usage 与摘要双写失败：续跑拒绝而非按零恢复，checkpoint 完好 | ProcessSummaryTest::test_resume_refused_when_both_usage_records_failed |
| manifest 声明的摘要缺失 → 拒绝；旧格式条目（无声明）→ 零、可恢复 | RecordedUsageTest::test_archived_invocation_without_summary_refuses |
| 6 字符凭据在异常消息中同样脱敏；对白/剧本逐字不变 | ProcessSummaryTest::test_exception_text_credentials_are_redacted_in_archives |

### 检查结果（每项独立运行并单独捕获退出码，格式化后复跑）

| 检查 | 退出码 | 结果 |
|---|---|---|
| 定向 provenance_test.py run_provenance_test.py -q | 0 | 28 passed, 8 subtests |
| pytest concordia | 0 | 1452 passed, 33 skipped, 101 subtests（与上轮持平） |
| pytest examples | 0 | 383 passed, 77 subtests（上轮 381，+2 新测试） |
| pylint --errors-only -j 1 concordia | 0 | 无错误 |
| pyrefly check concordia | 1 | 仅既有 4 项依赖错误（vllm×3、FastMCP×1），无新增源码错误 |
| git diff --check | 0 | 通过 |
| pyink/isort（本次修改的 4 个 Python 文件逐一检查） | 0 | 全部通过 |

修改文件：`provenance.py`、`provenance_test.py`、`run.py`、
`run_provenance_test.py`、`examples/story_enactment/README.md`。
核心目录零改动。真实模型验收仍为**未执行**。停止在修复验收点，
待整体复审。

## 契约变更勘误（2026-10-16 回填）

本记录 Phase A–D 各节描述的末端契约「最终双轨生成 → 独立语义复核 →
程序构造 canonical event」已被取代：自 2026-10-16 起，最终 GM 输出为
单字段严格 JSON 的 `stage_directions`，canonical event 在语义评审前由
程序从原始对白与旁白确定性组装，评审通过后原样提交、评审后不再改写。
变更依据与权威表述见
[正式事件组装与评审修复设计](../specs/2026-10-16-story-enactment-canonical-review-design.md)，
实施与验收证据见
[对应交付记录](2026-10-16-story-enactment-canonical-review.md)
（含旧设计 §6/§11 的契约回填）。除末端生成契约及其测试/stub 措辞外，
本文其余记录（行动授权、干预状态与提交语义、预算、恢复、归档与脱敏）
继续有效。
