# 故事 enactment 探针计账与 Sol 验证收尾记录

日期：2026-10-21
状态：待审阅（停在停止点，见文末声明）
设计文档：`docs/superpowers/specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md`

## 工作区记录（实施前）

```
2026-10-21T05:30:48Z
33714b2855224831f0ea3c1814693c3748937bb1 fix(story): pure-action fidelity and schema-2 mode validation
 M concordia/contrib/components/README.md
 M concordia/contrib/components/game_master/director_note.py
 M concordia/contrib/components/game_master/director_note_test.py
 M concordia/contrib/components/game_master/drama_director.py
 M concordia/contrib/components/game_master/drama_director_test.py
 M concordia/contrib/components/game_master/story_action.py
 M concordia/contrib/components/game_master/story_action_adjudication.py
 M concordia/contrib/components/game_master/story_action_adjudication_test.py
 M concordia/contrib/components/game_master/story_resolution.py
 M concordia/contrib/components/game_master/story_resolution_test.py
 M concordia/contrib/prefabs/README.md
 M concordia/contrib/prefabs/game_master/story_dramaturgic.py
 M concordia/contrib/prefabs/game_master/story_prefabs_test.py
 M examples/story_enactment/README.md
 M examples/story_enactment/provenance.py
 M examples/story_enactment/provenance_test.py
 M examples/story_enactment/run.py
 M examples/story_enactment/run_provenance_test.py
 M examples/story_enactment/story_config.py
 M examples/story_enactment/story_config_test.py
 M examples/story_enactment/story_enactment_test.py
 M examples/story_enactment/story_isolation_test.py
 M examples/story_enactment/testing_stubs.py
 M examples/story_enactment/testing_stubs_test.py
?? concordia/contrib/components/game_master/repetition_push.py
?? concordia/contrib/components/game_master/repetition_push_test.py
?? examples/story_enactment/adjudication_chain_probe.py
?? examples/story_enactment/adjudication_chain_probe_test.py
?? examples/story_enactment/fixed_model_probe.py
?? examples/story_enactment/fixed_model_probe_test.py
 ?? examples/story_enactment/three_model_review_probe.py
 ?? examples/story_enactment/three_model_review_probe_test.py
```

## 实际触碰文件（实施后差集）

实施后 `git status` 与上文实施前快照的差集，即本计划实际新增/修改的
全部文件：

```
?? examples/story_enactment/request_ledger.py
?? examples/story_enactment/request_ledger_test.py
?? examples/story_enactment/world_response_probe_closeout.py
?? examples/story_enactment/world_response_probe_closeout_test.py
?? examples/story_enactment/world_response_rev_probe.py
?? examples/story_enactment/world_response_rev_probe_test.py
 M examples/story_enactment/README.md（新增三工具说明，实施前已为 M，
    本次在其上追加本计划段落）
 M docs/superpowers/notes/2026-10-21-story-enactment-probe-accounting-closeout.md（本文件）
?? run_results/story_enactment/world_response_fixed_probe_closeout/20261021T090457Z/
    （工具生成的勘误输出，下文「本批纠正值」的出处）
```

源批次 `run_results/story_enactment/world_response_fixed_probe/20261020T075501Z/`
全程只读：执行前后独立校验 389 个文件的总校验和一致
（`05eb9674…2465`），且与勘误输出内 `input_manifest.json` 逐文件
sha256 复验零差异。未提交、未清理、未改动任何历史产物。

## 工程验证层（测试文件 → 设计 §8 验收条目）

全量结果：`pytest examples -x -q` 577 passed + 124 subtests（exit 0）；
`pytest concordia -q` 1487 passed / 33 skipped（exit 0，回归确认）；
`pylint --errors-only examples/story_enactment` 零 error（exit 0）；
`pyrefly check examples/story_enactment` 23 error，全部位于本计划未
触碰的四个既有文件（`adjudication_chain_probe.py` 5 项、
`story_config_test.py` 1 项、`story_enactment_test.py` 14 项、
`three_model_review_probe.py` 3 项，均为实施前工作区已存在的未提交
改动，见上文快照），属既有问题、单列不修复；本计划六个新文件
pyrefly 零诊断。

设计 §8 七条确定性验收 → 覆盖测试：

1. started 未 finished 仍计数 / finished 不重复计数 / 同 slot 重复
   started 拒绝 / 恢复由账本重建各级一致计数——
   `request_ledger_test.py`：`AuditEventsTest`、`RebuildCountsTest`、
   `RequestLedgerTest.test_resume_rebuild_matches_ledger_at_all_levels`、
   `test_corrupt_ledger_refuses_resume`。
2. 真实重现本批形状（ACT03__m1 第一步完成第二步无结果，重建得到
   ACT=22、Gemini=39、总数=82）——`request_ledger_test.py`
   `BatchShapeReproductionTest`；真实批次执行复核见 Task 10 勘误输出。
3. 第二请求只计首请求 / 冻结槽与 No 分支不计费 / 预算满先停 / 未决
   不隐式重发——`request_ledger_test.py`
   `test_second_request_block_keeps_first_billed_only_once`、
   `test_frozen_and_skipped_and_blocked_add_nothing`、
   `test_over_cap_refuses_before_writing_anything_but_blocked_event`、
   `test_started_without_finish_occupies_budget_across_restart`；
   `world_response_rev_probe_test.py`：`SlotDisciplineTest`、
   `RunStageTest.test_budget_full_stops_before_sending`、
   `test_pending_slot_blocks_resume_until_authorized_skip`。
4. 截断/冲突日志、源漂移、缺输入明确失败不猜零——
   `request_ledger_test.py` `LoadEventsTest`（截断尾行带行号拒绝）；
   `world_response_probe_closeout_test.py` `IoDisciplineTest`
   （漂移检测、缺输入、截断账本均显式失败）。
5. 预期拒绝为通过负例 / 夹具缺陷单列 / 人类标签缺项不确认——
   `world_response_rev_probe_test.py`
   `SummarizeStageTest.test_expected_rejection_is_passing_negative_and_defects_are_separate`、
   `test_human_confirmation_requires_human_labels`；
   `world_response_probe_closeout_test.py` `HumanLabelAuditTest`、
   三层状态中 REV01/02 夹具缺陷单列。
6. 离线勘误不建真实客户端/不读凭据/不写旧目录/符号链接拒绝/前后
   清单一致——`world_response_probe_closeout_test.py`
   `IoDisciplineTest`（输出位于源内含符号链接拒绝、已存在目录拒绝、
   清单全文件排序哈希、事后漂移检测）与
   `test_main_happy_path_writes_manifest_and_reverifies`；真实执行
   （Task 10）再次验证：工具零客户端构造，`new_real_api_calls = 0`。
7. 八例 REV 走真实最终工厂、canonical 与严格解析 / 恰一次真实评审
   转发、生成零成本 / 正反 verdict、格式错误、API 错误、未决与额度
   上限全覆盖——`world_response_rev_probe_test.py`：
   `FixturesTest`（八例与字段级 diff 冻结）、`SlotDisciplineTest`
   （`test_review_slot_forwards_exactly_once`、
   `test_frozen_generation_slot_spends_nothing`）、`RunStageTest`
   （正反 verdict、格式错误、API 错误、未决、额度五类路径）、
   `FreezeStageTest` / `DryRunStageTest` / `SummarizeStageTest`。

另：REV probe 无网络 dry-run 证据（Task 10 实测）：
`--stage=dry_run` exit 0，`dry_run_report.json` 显示 8 attempts
（REV01–08__m4 全 completed）、16 个提示文件（每例 s1_generation +
s2_review 各一 `.prompt.txt`）、零真实客户端构造（伪提供方）、
`findings` 为空。

## 本批纠正值与原结论保留

真实 S1 勘误（零网络，只读解析旧批次）输出目录：
`run_results/story_enactment/world_response_fixed_probe_closeout/20261021T090457Z/`
（含 `input_manifest.json`、`corrected_report.json`、`corrections.json`、
`README.md`；工具拒绝已存在目录，执行后复验源批次逐文件哈希一致）。

`corrections.json` 共 5 项纠正（old → corrected，均以
`requests.jsonl` 账本为唯一口径）：

- `report.counts.real_calls`：80 → **82**（唯一 started 槽，中断恢复
  漏计）；
- `report.counts.by_model.m1`：37 → **39**；
- `report.counts.by_group.ACT`：20 → **22**；
- `report.counts.second_request_blocks`：0 → **2**（两次隐藏重试在
  到达提供方前被阻止）；
- `readme.frozen_generation_slots`：12 → **16**（README 声称值与账本
  `frozen_stub` 事件数不符；16 槽均零真实调用）。

`corrected_report.json` 关键值复核：total=82、by_model
{m4:43, m1:39}、by_group {ACT:22, CHAIN:36, REV:16, REP:8}、
responses_observed=81、no_result_requests=[ACT03__m1__s2_self_perception]
（及其下游 6 个 skipped_by_upstream_failure 槽，不计尝试）、
format_failures=[REP01__m1__s1_judgment, REP02__m1__s1_judgment]、
not_needed_no_branch=6（REP02/03 的 s2/s3 正常 No 分支，不计费）。

原结论保留（不倒改）：

- `overall_pass_preserved = false`：原批次整体不通过的结论不变；
- REV 冻结标签 12/4 不变：matched=12、unmatched=4，
  `frozen_labels_unchanged = true`；REV01/02 四次 unmatched 按设计
  §2.2 标注为夹具缺陷（旁白与已裁定 action_result 矛盾），不作为
  模型误拒绝证据，expected 标签原样冻结；
- ACT03__m1 无结果不判 Gemini 能力：仅记为 started_no_result 与
  下游跳过，不进入任何模型能力结论；
- Gemini 两处格式失败（`b.`/`b)`）仅作离线意向解读（均与预期一致），
  协议判定仍为 failed、未计通过；
- 人类确认缺位：40 项标签均为 agent 初评，`human_confirmation =
  "pending"`，不视作人类确认。

## 未证明事项

- Gemini 格式兼容未进生产：G 项离线方案（选项意向解读 + 提示哈希
  校验）仅保留在 closeout 输出与设计 §7，未改动生产适配器。
- Sol REV 八例未执行：`world_response_rev_probe` 仅完成 dry-run
  证据（8 attempts / 16 提示文件 / 零真实调用），freeze 与 run
  （恰一次真实评审转发）待授权。
- 完整故事（S3）未开协议：设计 §9 的逐轮评价与结局条件核验尚未
  制定执行协议，原双模型 gate 结论继续不变。
- 私密 NPC 通道、联合行动仍未实现：属产品路线后续能力，本计划
  未涉及。

## 停止点声明

本计划全部步骤零真实模型调用（Task 10 Step 3 只读解析旧批次）。
S2 的 `freeze`（在 `run_results` 下新建批次目录并冻结提示与账本
快照）与 `run`（最多八次真实评审请求）不在本计划内执行——真实
执行需另行授权，授权后按设计 §6.2 另立执行点。不提交、不清理，
等待审阅。

## 2026-10-23 外部审阅修复（本节更新前述验收结论）

用户授权按外部审阅发现直接修改；本轮未创建 worktree、未 commit、未调用
真实模型、未在 run_results 冻结或执行 S2。既有生产代码与历史批次不改。
开工前相关文件快照保存在 `/tmp/concordia-probe-review-fix-20261023/`；
原 SDD 工作区与此前记录继续保留。

观察到的障碍是账本缺失可清零、汇总可在缺失证据时误判通过、REV01 正例
仍遗漏已执行动作。本轮修复使后续世界回应/动作保留能力的评测依据可信，
没有新增故事机制。复用现有 RequestLedger、生产最终工厂与严格评审解析；
工程证据为下列回归与历史数据核验，语义能力仍待授权后的真实 S2 验证。

### 修复内容

1. `request_ledger.py`：账本不存在时仍检查孤立 started 旁侧文件，不能
   将有消费痕迹的目录当成零计数新批次；逐字段核对旁侧 started JSON，
   校验请求正文 SHA-256。重复 finished、与 started 冲突的终态身份也拒绝。
2. `world_response_rev_probe.py`：summarize 先核验冻结文件、源快照与账本，
   将可用结果绑定到注册的单次评审请求、completed 终态、原始响应及一致的
   record，并要求一次冻结生成记录。复用生产严格解析校验 raw verdict 与
   报告结果一致；重复/未知人工标签拒绝。审计失败会保存 overall_pass=false、
   summary=null 与错误说明，再报错，旧通过状态不会残留。缺项/agent 标签
   仍保持 pending，不因补做审计而获得语义通过。
3. REV01：保留修正后的完整 action_result，stage_directions 改为：

   > 周律师走到门边，隔着门把话说完。门外传来邻居的应声："知道了，有需要随时叫我一声"，随后脚步声在雨里渐渐远去。

   REVISION_RECORD 同时记录 action_result、stage_directions 两个修订字段。
   已修正实施计划中“旁白原样保留（本就忠实）”及相关继承检查；README
   同步说明恢复、汇总边界。REV02 不再改动，REV03–08 九字段逐项保持原样。

### 历史证据缺口与只读兼容

对旧批次增强审计后，发现三份请求正文原本未存档：

- ACT03__m1__s2_self_perception（started_no_result）；
- REP01__m1__s1_judgment（SecondRequestBlocked）；
- REP02__m1__s1_judgment（SecondRequestBlocked）。

仅 S1 只读 closeout 显式允许这两类历史失败槽缺少 prompt，并将每项缺口
写入 warnings 与输出 README。它不补造输入、不退款、不改变失败状态。
正常完成槽缺正文、现有正文哈希漂移、旁侧元数据冲突仍拒绝。
RequestLedger 恢复和 S2 汇总不启用该离线兼容参数，继续要求完整请求正文。
本轮只在内存重新构造报告以核验结果，没有覆写旧 closeout 目录；旧产物
因此没有这三项新 warning，本节记录该新增证据限制。

### 验证与停止点

- 新增失败探针先 RED（16 个失败项，含 subtest），修复后该批三个测试文件
  合计 **84 passed + 14 subtests**，比原 75 个测试新增 9 个测试方法。
  覆盖账本缺失/清空/重复、旁侧元数据与 prompt 漂移、单例请求/响应/终态/
  冻结记录缺失、重复/未知标签、raw verdict 与报告不一致、历史缺正文披露。
- `pytest examples -q`：**586 passed + 138 subtests**。
- `pytest concordia -q`：**1487 passed / 33 skipped + 126 subtests**。
- 六个变更 Python 文件 pylint、pyrefly、pyink/isort 通过；
  `pylint --errors-only concordia` 通过。全库 `pyrefly check concordia` 的
  4 项既有问题仍保留：vllm 可选依赖缺失 3 项、FastMCP 属性 1 项。
- 离线 CLI dry-run：**8 attempts、16 prompts、0 findings、零真实调用**，
  产物位于 `/tmp/concordia-probe-review-fix-20261023/dry-run/`。
- 旧批次 389 文件哈希复验一致；重新构造的计数、三层状态、Gemini 离线
  意向分析、人类标签审计与旧勘误产物相同。82/43/39、ACT 22、原整体失败
  结论均不变；新增限制仅为上述三份历史请求正文缺失的披露。
- AST 只读比对验证 REV03–08 九字段、所有继承字段以及 REVISION_RECORD
  的 old/new 文本均与各自来源一致，未导入旧 probe。

工程修复已验证；真实 Sol REV 八例、完整故事质量、私密 NPC 通道及联合
行动仍未证明。停止在审阅点；真实冻结与最多八次调用仍须另行授权。
