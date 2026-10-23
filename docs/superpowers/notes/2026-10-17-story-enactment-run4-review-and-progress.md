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

规则 8 校准（`story_resolution.py` 生产文案替换 + `story_resolution_test.py`
六行正反对照回归，含生成/评审共享指纹、忠实转述合法、省略未变化背景
合法、dropped/reversed/invented 拒绝、仅重述不算成功干预）；候选聚焦
指引（`director_note.py` 两个融合 prompt 注入 `_CANDIDATE_FOCUS`，纯指引
无程序删句 + 对应测试）；阶段 A 验证测试与 README 对照协议
（`story_enactment_test.py` 新增配置/接线/端到端三类测试 +
examples README 的 Action Modes 节）；以及阶段 C 未实施的记录
（见下节）。修改文件清单：

- 生产：`concordia/contrib/components/game_master/story_resolution.py`（规则 8 段落）、
  `concordia/contrib/components/game_master/director_note.py`（`_CANDIDATE_FOCUS`）。
- 测试：同目录 `story_resolution_test.py`、`director_note_test.py`、
  `examples/story_enactment/story_enactment_test.py`。
- 文档：`examples/story_enactment/README.md`（Action Modes 节）、
  `concordia/contrib/components/README.md`（仅 `story_resolution.py` 词条追加
  一句校准说明，`repetition_push.py` 词条本轮不动）、设计文档状态行（设计文档
  第 4 行回填）。
- 本记录。

`repetition_push.py` / `repetition_push_test.py` 保持基线未触碰（未跟踪文件，
当前 sha256 `65d730ca…` / `7d51db2e…`，与控制器开工记录一致）。

## 各任务 RED/GREEN 证据

（明细见工作区 SDD 台账 task-1..6-report.md；开工基线 21 个 ` M` + 2 个
`??`，与本记录同批未提交修改共存。）

| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| Task 2 阶段 A 验证 | 预期双 GREEN（验证既有接线，非缺陷修复）：Step2 `2 passed in 2.03s` exit=0；Step4 新测试 `1 passed in 2.36s` exit-new=0、范围 `233 passed, 77 subtests passed in 11.88s` exit-all=0 | 同左 |
| Task 3 规则 8 回归 | `Rule8InterventionBoundaryTest::test_calibrated_rule8_text_is_shared_by_generation_and_review` FAILED（story_resolution_test.py:904）：`AssertionError: 'faithful paraphrase is allowed' not found`；全文件 `1 failed, 31 passed, 38 subtests passed` exit-file=1，类内 `1 failed, 4 passed, 3 subtests passed` exit-class=1 | （由 Task 4 落地）全文件 `32 passed, 38 subtests passed` exit=0 |
| Task 4 规则 8 替换 | 以 Task 3 RED 为输入（改前复现 `1 failed, 31 passed` exit=1） | `story_resolution_test.py` `32 passed, 38 subtests passed in 2.12s` exit=0；`game_master/` 目录 `280 passed, 73 subtests passed in 2.36s` exit=0 |
| Task 5 候选聚焦 | `FusionStepTest::test_prompts_focus_added_content_on_new_events_only` FAILED（director_note_test.py:431）：`'chosen whitelisted event' not found`，`1 failed in 2.03s` exit=1 | `director_note_test.py` `16 passed, 2 subtests passed in 2.02s` exit-file=0；`game_master/` 目录 `281 passed, 73 subtests passed in 2.30s` exit-dir=0 |
| Task 6 跨套件 | — | `pytest concordia -q` `1453 passed, 33 skipped, 112 subtests passed` exit-concordia=0；`pytest examples -q` `386 passed, 80 subtests passed` exit-examples=0（pyink 修复 `story_resolution_test.py` 2 处纯排版后复跑，计数不变） |

## 阶段 C：未实施

按设计 §6/§8，重复判断上下文校准以阶段 A 真实对照证据为触发条件
（行动模式下是否仍存在相似停滞）加用户授权；本轮两者均未满足，
未修改 repetition_push.py / repetition_push_test.py（保持开工基线）。
触发条件与实施模板见计划 Task 7。

## 设计 §4.1 数据流对照（Task 2 Step 5 表原文抄录）

| 设计 §4.1 数据流 | 既有覆盖 |
|---|---|
| 1. 玩家明确提出用自己钥匙尝试转锁 | `test_blocked_result_flows_to_observation_and_fdx`（林雪保险柜意图） |
| 2. 核心 `attempt_to_result` 依历史/世界条件给结果 | 同上（裁决问题含公开两钥约束断言）+ `test_success_result_flows_to_observation_and_fdx` |
| 3. 缺另一把钥匙受阻；不替另一玩家决定配合 | `test_blocked_result_flows_to_observation_and_fdx`（无 交出/递上/第二把钥匙/陈母 断言） |
| 4. 成功/受阻贯穿 evidence、旁白、canonical、观察、JSONL、checkpoint、FDX | `test_success_result_flows_to_observation_and_fdx`、`test_blocked_result_flows_to_observation_and_fdx`、`test_changing_only_core_result_changes_final_event` |

## 全量检查（每条独立运行、单独捕获退出码，未用 && 串连）

| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `source venv/bin/activate && pytest concordia` | 0 | `1453 passed, 33 skipped in 20.30s`（-q 口径另含 112 subtests passed，见 Task 6 记录） |
| `source venv/bin/activate && pytest examples` | 0 | `386 passed in 32.82s`（-q 口径另含 80 subtests passed，见 Task 6 记录） |
| `source venv/bin/activate && pylint --errors-only -j 1 concordia` | 0 | 无错误（无输出） |
| `source venv/bin/activate && pyrefly check concordia` | 1 | 仅既有 4 项依赖错误（基线分类，见下），无本次源码错误（875 suppressed） |
| `git diff --check` | 0 | 通过（无输出） |

pyrefly 基线分类（均非本次计划触碰文件，与 2026-10-16 记录的既有基线
一致，属依赖未装入 venv 而非源码缺陷）：

- `concordia/contrib/language_models/vllm/vllm_model.py:53–55` —— vllm
  missing-import ×3；
- `concordia/contrib/tools/mcp/servers/file_reader_server.py:28` ——
  FastMCP missing-attribute ×1。

pylint 与 `git diff --check` 无任何输出，无需分类；本次计划文件未产生
任何 lint/类型/空白错误。

## 格式检查（仅触碰的 Python 文件）

对本次触碰的 5 个 Python 文件（`story_enactment_test.py`、
`story_resolution.py`、`story_resolution_test.py`、`director_note.py`、
`director_note_test.py`；不含 `repetition_push*`）：

| 检查 | 退出码 | 结果 |
|---|---|---|
| `pyink --check <5 文件>` | 0 | `5 files would be left unchanged.` |
| `isort --check-only <5 文件>` | 0 | 通过（无输出） |

## 验收确认清单

| 确认项 | 命令 / 证据 | 结论 |
|---|---|---|
| 核心目录零修改 | `git diff --name-only concordia/components concordia/document concordia/environment concordia/typing` | 空（退出码 0，无任何输出） |
| run_results 只读 | `find run_results -type f -print0 \| sort -z \| xargs -0 sha256sum \| diff - /tmp/opencode/run4-baseline/run_results-hashes.txt` | 无差异，`READ-ONLY-OK`（退出码 0，109 文件哈希一致） |
| 既有 dirty 基线未被回滚 | `diff <(git status --short) /tmp/opencode/run4-baseline/git-status.txt`（仅允许新增本计划文件造成的追加行） | 输出与基线完全一致（diff 退出码 0，零差异）：`docs/` 属 .gitignore 忽略路径，本记录不进入 git status；21 个 ` M` + 2 个 `??` 基线行原样保留 |
| 规则 8 生成/评审一致 | `Rule8InterventionBoundaryTest::test_calibrated_rule8_text_is_shared_by_generation_and_review` | 通过（定向运行 PASSED，并随全量套件通过） |
| 拒绝无副作用 | `test_dropping_reversing_or_inventing_facts_is_rejected` 等 | 通过（定向运行 `3 passed, 3 subtests passed` exit=0，含 restatements-only 用例；并随全量套件通过） |

## 真实模型复验：未执行

设计 §4.2/§5.4/§6/§9 的真实调用评测（阶段 A 对照、规则 8 固定样本
语义评测、重复判断质量评测、三场景整体复验）均未发起，属后续独立工作；
其中阶段 A 对照结果同时是阶段 C（计划 Task 7）的触发证据。本记录全部
结果来自 MockModel/PromptStubModel 确定性测试，证明分支与接线，
不构成真实模型语义质量验收。
