# Story enactment 世界回应与有效进展（阶段 A）交付记录

日期：2026-10-20。设计：
`../specs/2026-10-19-story-enactment-world-response-and-progress-design.md`（约束
权威），实施计划：
`../plans/2026-10-20-story-enactment-world-response-and-progress.md`。
按用户授权执行 Task 1–11（基线快照、A1 角色一步选择、A2 公开 NPC 回应、
A3 物证来源边界、A4 有效进展、确定性整体验收与收尾）。全程未执行任何
git commit/stash/clean/checkout：收尾 `git status --porcelain`（32 行）与开工
基线（30 行，`/tmp/opencode/world-response-baseline/git-status.txt`）逐行
一致，仅新增两行本计划修改、开工时干净的文件（`examples/story_enactment/
story_config.py`、`examples/story_enactment/story_isolation_test.py`）；同事
未提交的进行中修改全部保留。计划文件的勾选由控制器维护。新增真实模型
调用 0；阶段 B/C 均未执行（见第 6/7 节）。

## 1. 改动文件清单

开工前快照（Task 1，`/tmp/opencode/world-response-baseline/files/`，20 个
文件）与当前版本逐个比对，全部存在差异，无计划外文件被触碰：

- `examples/story_enactment/`：`story_config.py`（T2 字段/校验/示例邻居规则
  + T7 A1 指引）、`story_config_test.py`（T2/T7）、`story_isolation_test.py`
  （T7 隔离 guard）、`run.py`（T3 `make_gm_params` 双通道记录）、
  `story_enactment_test.py`（T3 + T10 端到端/拒绝/恢复）、`README.md`
  （T11 新增 "NPC responses and world events" 小节）。
- `concordia/contrib/components/game_master/`：
  `story_action_adjudication.py` + `_test.py`（T4 共享规则扩展 +
  `PUBLIC_NPC_RESPONSE_SEMANTICS` + `npc_response_rules` 参数）、
  `story_resolution.py` + `_test.py`（T5 规则 5 双通道区分 + 生成/评审共享
  NPC 语义）、`repetition_push.py` + `_test.py`（T8 §7.1/§7.2）、
  `director_note.py` + `_test.py` 与 `drama_director.py` + `_test.py`
  （T9 §7.2 建议聚焦）。
- `concordia/contrib/prefabs/game_master/`：`story_dramaturgic.py`（T6 参数
  校验与两工厂接线）、`story_prefabs_test.py`（T6）；`README.md`（T11）。
- `concordia/contrib/components/README.md`（T11 五条一句式增补）。
- 新增：本交付记录自身。

明细证据见台账 `/tmp/opencode/world-response-baseline/progress.md` 与
`reports/task-{1..10}-report.md`（评审记录在 `reviews/`）。未删除任何测试；
最终套件计数：`examples/story_enactment` 349 passed + 121 subtests，
`concordia/contrib` 524 passed + 89 subtests（开工基线 335+97 / 509+84）。

## 2. RED/GREEN 证据（每任务关键反例）

| 任务 | RED（实现前） | GREEN（实现后） |
|---|---|---|
| T2 配置层 | `pytest examples/story_enactment/story_config_test.py::NpcResponseRulesConfigTest -v` → 21 failed / 3 passed（`TypeError`：未知字段 `npc_response_rules`） | 同命令所在文件 → 19 passed + 20 subtests |
| T3 run 接线 | `pytest .../story_enactment_test.py::MakeRunConfigTest::test_npc_response_rules_and_two_event_channels_recorded -v` → `KeyError: 'npc_response_rules'` | `1 passed`；套件 342+115 |
| T4 裁决语义 | `pytest concordia/.../story_action_adjudication_test.py -k NpcResponseRulesWiring -v` → 4 failed（`TypeError` 意外关键字 + 无 `PUBLIC_NPC_RESPONSE_SEMANTICS` 属性等） | `4 passed`；contrib 513+84 |
| T5 生成/评审 | `pytest ... -k "NpcRulesAndEvidenceProvenance" -v` → 新测试失败 | `5 passed, 2 subtests passed`；contrib 518+86 |
| T6 prefab | `pytest ... -k NpcResponseRulesWiring -v` → 4 failed / 2 passed（坏结构未 `ValueError`、规则未达提示） | `3 passed, 3 subtests passed`；contrib 521+89 |
| T7 A1 指引 | 新测试 RED→GREEN；`test_speech_only_spec_stays_dialogue_only` 从一开始即 GREEN（guard 测试，计划允许并已注明） | 套件 345+115；路由指纹保留 |
| T8 重复判断 | `pytest .../repetition_push_test.py -v` → 2 failed / 7 passed | `9 passed, 3 subtests passed` |
| T9 建议聚焦 | 两文件新测试各自 RED | `director_note` 17+2、`drama_director` 60+16、`Repetition` 13+6 |
| T10 整体验收 | 计划明确 Step 2 预期 GREEN（接线验证，失败回溯前序任务）：`PublicNpcResponseIntegrationTest` 首跑即 3 passed + 6 subtests；恢复测试首跑因测试构造不精确失败（`隔壁邻居` 断言误伤白名单文本），修正断言后通过——属测试构造修复，非生产问题 | 新类 + 恢复测试 `4 passed, 6 subtests`；套件 349+121、contrib 524+89 |

## 3. 全量检查表（Task 11 Step 4，逐条独立运行、命令结束后立即取 `$?`）

| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `pytest concordia -q` | 0 | `1487 passed, 33 skipped, 126 subtests passed` |
| `pytest examples -q` | 0 | `502 passed, 124 subtests passed` |
| `pylint --errors-only -j 1 concordia` | 0 | 无输出（无错误） |
| `pyrefly check concordia` | 1 | 与 Task 1 本轮实测日志逐项一致，**无新增**（见下） |
| `pyink --check`（本计划 17 个 py 文件逐一列出） | 0 | `17 files would be left unchanged.` |
| `isort --check-only`（同 17 文件） | 0 | 通过（无输出） |
| `git diff --check` | 0 | 无输出 |

pyrefly 逐项对照（closeout 日志 vs Task 1 `pyrefly.log`，按文件/位置/原因）：
`concordia/contrib/language_models/vllm/vllm_model.py:53:1`、`:54:1`、`:55:1`
missing-import ×3；`concordia/contrib/tools/mcp/servers/file_reader_server.py:28:7`
missing-attribute ×1；汇总行 `4 errors (875 suppressed, 21 warnings not
shown)` 亦一致。全部为依赖未装入 venv 的既有错误，均在计划触碰文件之外；
诊断行 diff 为空（退出码 0）。故不宣称"全部静态检查通过"，保留退出码 1。
完整输出与退出码：`/tmp/opencode/world-response-baseline/closeout-*.log/.rc`。

run_results 只读核对（Step 5，Task 1 哈希基线而非空 `git status`）：
重算 2253 行 sha256 与 `run_results-hashes.txt` diff 为空（退出码 0，
`closeout-run-results-diff.log` 0 字节）——原始运行产物字节级零改动。

## 4. 旧报告勘误披露（设计 §1 三项，只追加不覆写）

以下三项针对证据批次
`run_results/story_enactment/full_story_control/20261019T032409Z` 的旧
README/protocol 记录，**仅在本新 notes 中披露**，不修改旧 README、protocol
或批次产物：

1. `speech_only` 不能直接执行物理动作，但不能据此判定所有允许结局不可达；
   自主协商也可能形成结局，必须检查其具体条件。
2. speech-only 可以有合法导演旁白；Sol speech-only 第 15 回合就有电话事件。
3. protocol 中 repetition 开关与实际配置不一致：实际四组均启用，间隔和
   冷却均为 3。组间该参数一致，但预注册记录有偏差，必须披露。

## 5. 尚未支持的私密交互、阶段 C gate 与联合行动授权

- 现有管线无按接收者区分内容的能力：`story_dramaturgic` 给
  `SendEventToRelevantPlayers` 的是场景参与者过滤器，
  `BoundaryEncodingMakeObservation` 只保护条目边界；所有 resolved events
  对全部场景参与者公开。贴耳、未开免提电话的对端答复**不支持**（设计
  §5.3），需要阶段 C 的独立受众设计（§11 六项 gate：公开/私密内容分离、
  一次批准与原子提交、知识隔离、调用与 schema 成本、复用现有队列、验收
  场景）——**均未实施**。
- 因此本计划的公开邻居回应验收通过**不等于**全部世界交互闭环；报告必须
  持续列出私密通话限制。
- 不支持的私密交互是工程限制，不是剧中事实（对方沉默、拒绝、挂断、身份
  核验失败）；不得编造这些结果，也不得强迫玩家挂断或开免提绕过限制。
- 联合行动授权同样未新增（设计 §4 末段）：另一玩家的新动作仍留到其自身
  回合；过去一句"我愿意配合"不足以授权其本轮转锁；不能把本批尚未尝试
  插钥匙的轨迹当作联合操作机制已失效的证据。

## 6. 验收结论分列（工程链路 ≠ 语义样本 ≠ 完整故事质量）

- **工程链路通过**（本计划范围）：第 2 节 RED/GREEN、第 3 节全量检查、
  T10 端到端/非法候选拒绝/往返恢复/调用计数。
- **固定语义样本（阶段 B 前半）**：未执行、未宣称。协议、调用上限、模型、
  采样参数、版本、来源哈希与人工标签均未冻结。
- **完整故事质量（阶段 B 后半）**：未执行、未宣称。"已演示故事可完成"
  至少需要一条允许结局全部条件真实成立的合法轨迹，本计划没有此类证据。
- 真实模型调用 0；不以后续 rerun 挑选有利轨迹。

## 7. Self-Review 记录（自计划誊入，附本计划执行偏差）

1. **Spec coverage**（设计章节 → 任务）：§4（A1）→ Task 7；§5.1 三种权限 →
   Task 4 常量 + Task 5 规则 5/8；§5.2 配置/prefab/接线/歧义消除 →
   Task 2/3/5/6；§5.3 私密限制 → Task 4 常量末句 + Task 10 非法候选 +
   Task 11 README/notes 披露（不修引擎，阶段 C 另立）；§6（A3）→ Task 4
   常量扩展 + Task 5 正反夹具与拒绝测试；§7.1 → Task 8；§7.2 → Task 8 的
   repetition 建议/融合上下文 + Task 9 其余两路 + Task 5 规则 8 句（非导演
   干预）+ Task 10 `test_npc_response_is_not_a_director_intervention`；
   §8 不变契约 → Global Constraints + 各任务回归步骤；§9 文件范围 →
   Tasks 1–11 逐一对应；§10.1 确定性测试 8 项 → Task 7（项 1）、Task
   2/6/10（项 2）、Task 10（项 3/4/7）、Task 5（项 5）、Task 10 与既有套件
   （项 6）、Task 10 Step 4 全范围回归（项 8）；§10.2/§10.3（阶段 B）与
   §11（阶段 C）明确不在本计划。设计 §12 执行次序与任务顺序一致。
2. **Placeholder scan**：Task 5/10 的 `...` 均为"按该文件既有 helper 补全
   构建代码"的显式指令，断言目标、夹具文本常量、期望行为均已给出；生产
   代码步骤无占位，无 "TBD"/"类似 Task N" 转写。
3. **Type consistency**：`npc_response_rules: Sequence[str] = ()` 在
   `make_action_adjudication_step`、`make_final_output_step`、prefab 传参
   三处一致；`StoryConfig.npc_response_rules: tuple[str, ...]`、
   `to_dict`（list）、`from_dict` 缺省 `()`、`gm_params`（list）、prefab
   `params`（默认 `()`）链路类型一致；`from_dict` 在 `tuple()` 前校验原始
   list/tuple 及非空字符串元素，直接构造 dataclass 与直接构建 prefab 的
   入口也分别校验。`PUBLIC_NPC_RESPONSE_SEMANTICS` 命名在 Task 4/5/6 测试
   引用一致；A1 文案保留 stub 路由指纹。
4. **Authorization consistency**：Task 3 世界限制、Task 4 共享语义、Task 5
   规则 5/8、Task 11 README 一致要求先有适用的 NPC 回应授权；有限回应/
   拒绝不能充当空规则或未满足触发条件的兜底豁免；私密通道未支持不被改写
   成对方沉默/拒绝/挂断/核验失败。
5. **Fixture validity**：Task 10 第 2 回合通过真实 repetition 干预和正式
   提交建立合法敲门历史，第 3 回合才回应；裁决历史数据区含该已提交事件，
   不只检查白名单或规则文字；空规则调用数对照只生成当前玩家自己的告知
   结果；失败注入只在目标轮发生；规则数据断言使用实际 Markdown 数据区与
   JSON 内容。
6. **Evidence baseline**：Task 1 先快照 20 个计划触碰文件并采集本轮
   pytest/lint/type 输出与真实退出码；Task 11 按实测诊断对照（第 3 节），
   不引用历史错误数作为当前基线。
7. **执行偏差记录（诚实披露）**：Task 3 初版实现含一行超出任务范围的
   `director_params` 修改，评审标记为 Important 后在 fix round 1 **删除**，
   复审干净（台账 Task 3 行）；最终交付不含该行。Task 10 恢复测试的一次
   首跑失败为测试构造问题（断言 `隔壁邻居` 误伤白名单文本），当任务内
   修正，非生产缺陷。此外各任务报告记录了若干 minor deferred 项（如 T2
   `from_dict` 对该字段的先行校验不对称、T5 规则 8 缩进描述不准确但无
   语义影响、T6 注释措辞过度声称等），均在对应报告中留痕，未在本计划内
   追加修复。
