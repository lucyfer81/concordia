# AI Agent 故事演绎平台 — Phase 1 设计文档

日期：2026-10-13（v3，完成接口与执行边界修订）
2026-10-15 导出修订：用户同意以 FDX 作为逐字保真交换格式，保留 Fountain 兼容导出。
状态：设计基线，供工程师编写实施方案；实现尚未验收
2026-10-15 真实模型修订：按用户授权实施
`../plans/2026-10-15-story-enactment-live-fixes.md`：GM 接通全角色底线和
初始公开事实；每步提交前独立结构化语义复核；通过后以原始对白与旁白
单源构造正式事件。复核每步增加一次预算内调用，违规报错且不提交；
模型复核不替代人工验收。陈母的认罪结局改为无需认罪的协商路径；
结局各项必须已经成立。每轮新增不广播的导演诊断，运行新增源码/提示
与脱敏配置归档，恢复保留此前版本。以下接口与行为以此修订为补充。
基线仓库：Concordia fork（google-deepmind/concordia 二次开发）
审阅记录：`docs/superpowers/notes/2026-10-13-story-enactment-phase1-review.md`
（历史审阅建议；最终执行契约以本文为准）

## 1. 背景与总体路线

在 Concordia（生成式社会模拟库，GM + 实体-组件架构）基础上构建 AI Agent
故事演绎平台。完整平台含四种参与模式：①纯自动演绎 ②用户参演主角
③用户当导演 ④UGC 创作平台。四种模式共享同一地基——"会讲故事的故事
引擎"，因此按依赖顺序分四个 Phase 交付：

```
Phase 1  自动演绎 MVP（本文档）← 地基
Phase 2  导演模式：复用 StepController 干预 API + 导演指令协议
Phase 3  用户参演：复用 HumanActComponent + HumanInput 协议 + 实时通道
Phase 4  UGC 平台化：prefab 模板化 + 多租户运行服务
```

Phase 2/3 依赖的 StepController（play/pause/step/stop/paused_boundary）、
HumanActComponent、HumanInput 协议本仓库均已存在，标注为**复用对象**；
其新增工作仅为导演指令协议、实时通道与产品集成。

总体架构采用 **方案 B**：Concordia 作内核引擎（本 fork），平台层独立
仓库，后续演进为方案 C（通用组件按 Concordia 规范开发，可回馈上游）。

## 2. Phase 1 目标与非目标

### 目标

- 单场景群像短剧：4-6 个角色、一个冲突场景、几十步，全自动演绎
- 混合式剧情控制：节拍大纲（叙事条件式）+ 张力反馈（受规则约束的导演
  指令）
- 剧本体输出（FDX 保真格式；Fountain 兼容格式含中文角色名强制标记）
- 全部新代码符合 Concordia 规范（组件生命周期、测试、日志、checkpoint）
- 实施分三个验证阶段（见第 14 节），每阶段用相同配置与评价表，以便
  判断新增机制是否改善结果

### 非目标

- 非言语动作的结构化输出（`speech`/`action_intent` 双通道）——首版
  角色仅输出对白；引入非言语动作时再定义结构化契约
- 多幕长剧 / 系列连载（跨集记忆、记忆压缩）
- 任何用户实时干预接口（Phase 2/3）
- 平台层（Web 服务、用户系统——独立仓库）
- 修改 Concordia 核心代码（引擎、typing、已有组件）

## 3. 总体架构与文件布局

全部新代码进入本 fork 的约定扩展区：

```
concordia/contrib/components/game_master/
  drama_director.py        # 新增：剧情控制组件（节拍+张力+导演指令，
                           #   单一组件，见第 4 节）
  director_note.py         # 新增：思维链步骤工厂，绑定 DramaDirector
  story_resolution.py      # 新增：最终双轨输出、验证与字符串适配步骤
  story_terminator.py      # 新增：SceneOrStoryTerminator + 终止原因
concordia/contrib/components/agent/
  relationships.py         # 新增：派生式关系感知（自持缓存）
concordia/contrib/prefabs/game_master/
  story_dramaturgic.py     # 新增：故事戏剧化 GM 组装
concordia/contrib/language_models/
  budget_wrapper.py       # 新增：BudgetGuard 与 BudgetExhaustedError
examples/story_enactment/
  run.py                   # 演示入口
  story_config.py          # 人工编写的故事输入配置（世界事实/角色/信息分配）
  collector.py             # StoryTurn 采集适配器（输出 JSON）
  renderer.py              # 剧本渲染器（只读 JSON，纯函数；Phase 2 移平台层）
```

角色侧复用现有 `basic` prefab 与 `EmotionalStance`
（contrib/components/agent/emotional_stance.py）。

**能力承诺校准**（对应审阅 2.6）：新 prefab 可被 catalog 发现、状态可
检查；GUI 全功能动态编辑不在本 Phase 承诺内（需 `get_dynamic_state()`
及输入验证，另行验收）；复杂配置由 Python 示例提供。

## 4. 剧情控制组件 DramaDirector

### 4.1 为什么是单一组件

张力目标来自节拍、导演指令依赖两者，三者紧耦合。源码事实
（entity_agent.py:174,204）：组件 `update()` 在每次 act/observe 后并行
执行，多组件 update 之间无顺序保证。将节拍、张力、指令状态合并进单一
`DramaDirector`，其判定流水线由已裁决事件触发并在内部以固定顺序执行
（见 4.2、4.4 节）。节拍、张力和指令之间的依赖在组件内部处理；
该决策不改变 Memory、SceneTracker 等现有组件的并行生命周期。

### 4.2 story_turn 语义与事件捕获（计数单位）

- 定义 **story_turn**：一个角色动作经 GM 裁决产生已裁决事件 = 1 个
  剧情步
- 以下**不计数**：premise 注入、`[putative_event]`、GM 管理查询
  （终止/观察/选角/action-spec）
- **事件触发点与生命周期（统一路径）**：
  1. `pre_act(action_spec)`：识别本步 GM 的 act 是否为 RESOLVE（记住
     spec，供后续判断），并保留父类预动作上下文的生成路径。
     管理查询不运行剧情评估。
  2. `post_act(action_attempt)`：若刚经历 RESOLVE 且第 4.6 节的最终
     输出已经验证成功，置“等待裁决事件”标志，保存本次返回的事件字符串。
  3. `pre_observe(observation)`：只在标志置位、观察以 `[event]` 开头且
     去除标签后的内容与本次返回字符串一致时，捕获本地待处理快照。
     快照包含已验证裁决数据、动作所属节拍，以及本组件自持的最近事件窗口。
     此时消费等待标志；无标志或不匹配的观察不触发评估。
  4. `post_observe()`：**无 observation 参数**。读取上一步捕获的本地
     快照，执行第 4.4 节流水线。LLM 调用只使用快照，不读 Memory、
     不读其他组件的预动作值。后处理预算耗尽按第 11.3 节处理。
  5. `update()`：只提交本地已计算状态，清除本次待处理快照；覆写时调用
     `super().update()`（清理 `ActionSpecIgnored` 父类缓存，
     action_spec_ignored.py:85-87）
- “等待裁决事件”标志必须跨 RESOLVE act 末尾的 `update()` 保留，直至
  对应观察被捕获；不得把它当作预动作缓存清空。一次事件只消费一次。
- **premise 排除**：引擎以 `[event]` 标签注入 premise
  （sequential.py:244-246），仅凭标签不能排除——本方案用"等待裁决
  事件"标志作判别：premise 在任何 RESOLVE act 之前到达，不置标志，
  天然被排除；恢复运行时 premise 不重新注入（见第 12 节），规则不变
- 无新事件时整条流水线立即返回（零 LLM 调用）
- `[putative_event]` 可用于捕获当前角色的原始对白，但不计数、不评估。
  普通背景观察不进入评分窗口。评分窗口由 DramaDirector 持有并保存到
  checkpoint，避免从 UPDATE 阶段读取 Memory。
- 计数单位与场景预算对齐：SceneTracker 按 RESOLVE 次数计数
  （scene_tracker.py:33,109-116），故 SceneSpec 的 `num_rounds` 解释为
  裁决次数；两套预算用同一 story_turn 单位

### 4.3 节拍（Beat）模式

```python
@dataclasses.dataclass
class Beat:
  id: str                  # 稳定标识（日志/指令/渲染引用）
  description: str         # 叙事条件（不是角色选择，见 4.3.1）
  tension_range: tuple[int, int]   # 目标张力区间 (1-10)
  done_question: str       # 完成判定问题（须引用已发生事件）
  min_turns: int           # 达到后才允许按判定推进
  max_turns: int           # 达到仍未完成 → beat_timeout 终止
```

每个 story_turn 记录：`beat_id_at_action`（动作生成时所属节拍）与
`beat_id_after_event`（处理完成后节拍；全部完成时为空）。张力分数与
`beat_id_at_action` 的目标区间比较。

节拍推进规则：

- `min_turns` 未到不允许推进；每个 story_turn 最多推进一拍
- 完成判定必须引用已发生事件，不得把角色意图或未来大纲当完成证据
- `max_turns` 仍未完成 → 终止，原因记 `beat_timeout`；**不默默跳拍，
  不强制角色作预定选择**
- 节拍转换使旧节拍的待执行导演指令失效

#### 4.3.1 节拍描述叙事条件，不预定角色选择

节拍写"出现可供角色质疑的证据"，不写"角色甲认罪"；写"角色有机会回应
已揭示的事实"，不写"角色乙原谅甲"。完成条件允许角色作出不同回应
（对应审阅 3.2）。

### 4.4 判定流水线（事件触发，固定顺序）

在 `pre_observe(observation)` 捕获快照后，由 `post_observe()` 执行；
无待处理快照立即返回。以下状态先写入本步计算结果，最后由 `update()`
提交：

1. story_turn += 1；当前节拍已消耗步数 += 1；将本步完整事件加入窗口
2. 节拍完成判定（`yes_no_question`，仅引用快照及已发生事件；
   `min_turns` 门槛）→ 最多推进一拍；推进则失效旧指令，并记
   `beat_transitioned = True`。完成判定优先于本拍超时检查；本步完成
   则推进，否则达到 `max_turns` 时标记 `beat_timeout`。
3. 张力评估（每 `eval_interval` 个 story_turn；窗口=最近
   `scoring_window` 个已裁决事件）：
   - 评分提示只接收事件文本与统一量表；**与目标区间的比较在程序中
     进行**（与 `beat_id_at_action` 的目标区间比较）
   - 量表锚点：明确 1 / 5 / 10 分对应的可观察叙事特征
   - 偏离判定：低于 `lower - tolerance` 或高于 `upper + tolerance` 才
     触发（等于阈值不触发）
   - 评分失败 → 本次为空，不触发任何后续动作（见 11.1）
4. 导演指令生成，**前置条件**（全部满足才生成）：
   - 本步未发生节拍转换（`beat_transitioned` 为假）
   - 故事仍在运行（未完成、未超时）
   - 本步评分有效且偏离目标区间
   - 不在冷却期（执行一条指令后 `cooldown_turns` 内不生成）

   **规则**：本步发生节拍转换、故事完成或超时时，评分照常记录，但
   **不生成指令**——避免"刚按旧节拍失效指令、又依旧节拍评分生成新
   指令"或指令与评分依据的节拍不一致。只有节拍未变且故事仍在运行
   时，才生成绑定当前节拍的指令。

### 4.5 导演指令生命周期与约束

- 指令字段：`beat_id`、`generated_at_turn`、`content`、
  `status`（pending/consumed/invalidated/failed）
- 只执行一次；节拍转换后失效；执行失败记录原因
- 成功融入最终已验证事件后才标记 consumed，并记录执行步号供冷却计算。
  最终输出无效时不提交消费状态；本次未完成裁决按 error 停止。
- 指令内容受配置约束（见第 9 节允许的环境事件清单）：**只用 NPC/环境
  手段；不改写既有事实、不凭空修改角色记忆、不替角色生成主动选择**
- `director_note`（`director_note.py`）是思维链步骤**工厂**：绑定
  DramaDirector 实例产出符合三参数签名的步骤函数（无模块级全局状态）；
  有 pending 指令时仿 `maybe_inject_narrative_push`
  （event_resolution.py:1144）模式融入事件，无指令时恒等返回
  （零额外 LLM 开销）

### 4.6 事件裁决接线与最终输出适配

- `SwitchAct._resolve` 直接返回裁决组件结果（switch_act.py:240-241），
  `component_order` 不进入裁决提示
- 因此 prefab 中显式配置：
  `EventResolution(components=['display_events', 'drama_director', ...])`
  ——裁决模型收到的提示确实包含当前节拍与目标、世界约束键
- DramaDirector 实现 `get_pre_act_value()/get_pre_act_label()`
  （复用 `ActionSpecIgnored` 基类），满足 EventResolution 的依赖读取
- 裁决链包含普通裁决步骤（处理行动意图 → 产出实际结果）；导演步骤
  只追加受约束的环境/NPC 事件
- **裁决步骤顺序固定为**：普通裁决 → `director_note` → 最终输出适配。
  前两步返回候选事件字符串；最终步骤才生成、验证并保存双轨数据，
  确保导演追加的事实同时进入完整事件和舞台指示。
- `story_resolution.py` 提供绑定 DramaDirector 的步骤工厂，产出既有
  三参数签名的 callable。最终步骤通过 InteractiveDocument 请求 JSON
  对象，恰含两个字符串字段：
  - `resolved_event`：**完整事件**——保留发言者、对白与实际发生的
    事实；供 GM 记忆、节拍评估与角色观察
  - `stage_directions`：**渲染视图**——只含环境变化/NPC 行为/行动
    结果，不重复本步对白；供剧本渲染
- 验证成功后，最终步骤将两个字段连同当前角色、原始对白和节拍标识存入
  DramaDirector 的本次裁决记录；**返回值仅为 `resolved_event` 字符串**，
  不返回字典或 JSON 文本。EventResolution 可继续按现有逻辑添加 `Event:`
  前缀；引擎与 SendEventToRelevantPlayers 只接收完整事件字符串。
- `resolved_event` 必须保留本步原始对白，不替角色改写发言。
  `stage_directions` 可以为空字符串；普通裁决不能为填充舞台指示而
  凭空添加其他玩家角色的行为。JSON 无效、字段类型错误或对白被改写时
  报告明确的输出错误，按 error 停止，不广播无效事件。
- DramaDirector 提供 `get_last_story_turn()`：仅返回最近一次已提交剧情步
  的数据副本，包含第 7 节的计数、角色、对白、裁决、节拍与张力字段。
  场景显示配置和角色日志由 collector 读取。collector 用此接口补齐
  StepData 缺失的数据；
  不在 READY 阶段调用 `get_pre_act_value()`，不从日志文本重新解析裁决。

### 4.7 终止与原因（story_terminator.py）

`SceneOrStoryTerminator`：保留 SceneTracker 所需 `terminate()` 行为，
追加节拍完成/超时/预算停止判定，并**公开可读取的终止原因**。
step_limit 由示例层根据已提交剧情步数在运行返回后判定；error 或裁决前
的 budget_limit 由 run.py 的异常路径记录，不依赖引擎再次查询终止组件。
原因枚举：

```
beats_completed | beat_timeout | scene_limit | step_limit |
budget_limit | error
```

**优先级**（同一步同时满足多个条件时，按此序取首个）：
`error` > `beats_completed` > `beat_timeout` > `scene_limit` >
`step_limit` > `budget_limit`。即：最后一拍完成同时达到场景上限时记
`beats_completed`（故事结局优先于上限截断）；错误最优先（状态可信度
问题）。

只有 `beats_completed` 标记故事完成；截断路径不得标记为完成。

## 5. 角色侧组件

### 5.1 EmotionalStance（复用，语义边界）

- 选择情绪 + 语境记忆改写：改写结果**仅作角色主观解释**，不回写为 GM
  世界事实
- 渲染只读取当前发言角色的情绪标签；括号提示显示与否由导出选项控制

### 5.2 RelationshipsToOthers（relationships.py）

- 派生式：仅从**该角色观察到的事件**派生"我如何看待在场他人"；不维护
  新的权威关系事实，不与 GM 状态同步
- 自持缓存与刷新计数（`ActionSpecIgnored` 每次 update 清缓存
  ——action_spec_ignored.py:85-87，故不能依赖其实现周期刷新）；刷新
  单位=该角色收到的已裁决事件数（每 K 个刷新）；缓存写入 checkpoint

## 6. 数据流（一个 story_turn）

```
① SceneTracker 定场景 → NextActingFromSceneSpec 选中角色
② 角色 act：三问链 + EmotionalStance(情绪) + RelationshipsToOthers(关系)
   → ConcatActComponent(prefix_entity_name=False) → 仅对白正文
③ GM 裁决（思维链，经 components= 显式接线）：
   DisplayEvents(对话史) + DramaDirector(当前节拍) + 世界约束
   → 普通裁决 → director_note(如有 pending 指令)
   → 最终双轨生成/验证/缓存 → 返回 resolved_event 字符串
④ GM observe 到 `[event]`：pre_observe(observation) 捕获匹配快照
   → post_observe() 执行流水线（4.4 节）
   → update() 提交剧情步；后处理预算停止也走完此提交路径
⑤ step_callback：采集适配器保存 StoryTurn JSON，再提交 checkpoint
   （见第 12 节）；本回调不调用 LLM
⑥ 终止：SceneOrStoryTerminator（原因枚举+优先级），max_steps 兜底
```

LLM 组装：`RetryLanguageModel` 叠加 API 重试；预算见第 11.3 节。

## 7. StoryTurn 采集适配器（collector.py）

源码事实（step_controller.py:29-36）：StepData 仅含步号/当前角色/角色
动作/角色日志/GM 名，**不含**裁决结果、节拍、张力。故：

- 采集适配器接受 StepData，经 DramaDirector 的 `get_last_story_turn()`
  补齐裁决、节拍与张力字段；只从本步行动角色的日志读取情绪标签。
  StepData.step 仅用于本次运行的诊断，不能作为恢复后的全局 turn_id。
- collector 校验角色与对白一致、turn_id 为上一已提交编号加一，再保存
  一条 JSON 记录；同一 turn_id 的重复回调不得重复追加。
- 渲染器只读 JSON 文件，不访问运行中的 Simulation（停止后仍可导出已
  完成剧情步）

**StoryTurn 契约**：

| 字段 | 含义 |
|---|---|
| `turn_id` | 全局剧情步序号；恢复后继续递增 |
| `scene_id` | 场景标识 |
| `actor` | 本步唯一行动角色（由运行数据确定） |
| `speech` | 本步实际说出的对白 |
| `resolved_event` | GM 确认发生的事件（完整：发言者+对白+事实，供观察传播） |
| `stage_directions` | 渲染视图（不含对白复述的舞台指示） |
| `emotion` | 当前发言角色情绪标签；未启用为空 |
| `beat_id_at_action` | 动作生成时所属节拍 |
| `beat_id_after_event` | 处理完成后节拍；全部完成时为空 |
| `tension` | 本步有效评分；未评估或失败为空 |

场景显示信息（`interior`/`exterior`、`location`、`time_label`）在
story_config 单独配置（SceneSpec 无这些字段，不假定可读）。

验收：JSON 保存/加载后渲染结果一致；无重复对白；不把其他角色上一轮
日志当本轮台词或情绪；停止后可导出已完成步。

## 8. 对白与舞台事件分工（首版契约）

- 角色 `prefix_entity_name=False`（concat_act_component.py 支持）：
  **角色只输出自己的对白正文**——不含角色标签、他人对白、旁白、推理
- 采集器从 StepData 取发言者，只剥离引擎添加的一次确定性前缀
- **双轨契约**（见 4.6 节）：
  - `resolved_event` 保留发言者、对白与事实——其他角色经
    `SendEventToRelevantPlayers` 收到它，**发言由此传播**
  - `stage_directions` 只描述可观察环境变化、NPC 行为与行动结果，
    **不重复对白**——供渲染
  - 未发生的行动意图不进入二者
- 引入非言语动作时再启用 `speech`/`action_intent` 结构化输出（非目标）
- **对白传播测试**：甲说出一条此前未知的信息 → 乙的下一次行动上下
  文（观察/记忆）必须包含该发言

### Fountain 导出规则

- Fountain 是兼容视图；部分导入器会显示字面标记前的转义反斜杠。
  逐字保真要求以 FDX 验证，不能通过项目自身解析器的反转义宣称外部兼容。
- 角色行/对白行/括号提示/场景标题的标记规则显式定义
- **中文角色名用 `@角色名` 强制角色标记**；场景标题用 `INT.`/`EXT.`
  或强制场景标记
- 解析测试断言**元素类型与内容**，不只断言非空
- 参考 fountain.io 官方语法

### FDX 保真导出规则（2026-10-15 用户确认）

- `script.fdx` 使用 Final Draft XML 的显式段落类型：Scene Heading、
  Character、Parenthetical、Dialogue、Action。
- `speech` 始终进入 Dialogue，保留字面括号、反斜杠、空白和换行；
  XML 保留字符只在序列化时转义，独立 XML 解析后必须与输入逐字一致。
- 只有启用的 emotion 生成 Parenthetical；stage_directions 生成 Action。
  不从 resolved_event 复述对白，不以 Fountain 中间结果构造 FDX。
- 各种导出均只读取最近完整 checkpoint 确认的 StoryTurn。

## 9. 故事输入配置（story_config.py，人工编写）

包含：

- **世界事实**：地点、时间、在场人物、已有证据、允许的环境事件清单
  （导演指令的约束来源）
- **每角色**：身份、目标、底线、秘密、初始关系、表达风格
- **信息分配**：哪些事实公开、哪些仅 GM 知道（含未来节拍与导演指令
  ——绝不作为观察广播）、哪些仅指定角色知道
- **冲突起点、允许的结局范围、本场景不可改变的事实**

角色只接收自己的信息。测试至少含一个"角色尚未获知秘密"的信息隔离
场景（对应审阅 3.1）。

## 10. 剧本渲染器（renderer.py）

- 只读 StoryTurn JSON；确定性纯函数，零 LLM 调用
- 元素映射：场景标题 ← story_config；台词 ← `speech`；情绪提示
  （可选导出开关）← `emotion`；舞台指示 ← `stage_directions`
  （**不是** `resolved_event`，避免复述对白）
- 输出：FDX、纯文本与 Fountain（规则见第 8 节）；附张力曲线（matplotlib）

## 11. 错误处理与预算

### 11.1 降级路径

| 失败点 | 策略 |
|---|---|
| 张力评分非数字/失败 | 重试 1 次 → 仍失败**本次评分为空且不触发新指令**；上一有效值仅用于展示，不参与控制 |
| 节拍完成判定失败 | 默认"未完成"（保守不跳拍） |
| 导演指令生成失败 | 记录 failed 原因，降级无指令，不中断模拟 |
| 最终裁决 JSON 无效或对白被改写 | 记录输出错误并按 error 停止；本步不保存、不广播 |
| API 重试耗尽 | **明确报告 error**，不用"未完成/无指令"掩盖模型服务持续不可用 |

以上“失败降级”只处理输出解析或内容验证错误。API 异常与预算异常不作为
普通解析失败捕获；预算异常按 11.3 节的专用停止路径处理。

### 11.2 张力分数定位

启发式指标，不作为故事质量已提升的证明（不写入任何"质量保证"结论）。

### 11.3 预算与停止路径（请求前检测）

一个剧情步含多次模型调用；步末检测不足以阻止步内空响应
（`CallLimitLanguageModel` 超限静默返回空文本/首选项——
call_limit_wrapper.py:71,101）。因此：

- contrib 新增 **BudgetGuard 包装器**，实现 LanguageModel 接口，与
  `BudgetExhaustedError` 放在同一模块，供 GM、角色和示例层共同引用。
  **每次请求前**在锁内检查并占用一次调用额度，超限抛出该专用异常，
  不调用底层模型。请求失败也消耗该次额度；被拒绝的请求不消耗额度。
- 单次运行的所有角色、GM 和初始化调用共享同一个 BudgetGuard。
  首版硬预算单位为 `sample_text/sample_choice` 的底层模型调用尝试数，
  不宣称等价于供应商的实际 HTTP 请求数或精确 token 费用。
- 包装顺序固定为 `RetryLanguageModel(BudgetGuard(provider_model))`，
  使每次 API 重试都经过预算检查。可重试异常使用供应商对应的瞬时 API
  异常白名单；**禁止使用默认 `(Exception,)` 或包含预算异常的基类**。
  明确不重试 `BudgetExhaustedError`，也不把它当作 API 服务错误报告。
- 11.1 节的普通降级处理不捕获预算异常。按调用发生的阶段执行以下两条
  **专用控制路径**：

  1. **裁决未完成**：包括角色动作、普通裁决、导演追加、最终输出生成
     或其他管理调用。异常传播到 run.py，记录 `budget_limit`；当前步
     不生成 StoryTurn、不覆盖上一完整 checkpoint。角色、场景游标或
     指令可能已发生局部变化，不能把该运行中状态当作可恢复提交。
  2. **裁决已完成**：对应已验证完整事件的 `post_observe()` 后处理。
     DramaDirector 专门捕获该异常，将其转换为预算停止标志，不再进行
     本步或后续步的任何模型调用。保留已成功计算的节拍结果；尚未得到
     完成判定时不推进节拍，仍按本拍已消耗步数检查超时。未取得的张力
     评分为空；若已有有效评分，则保留它。指令生成中断不创建新指令。
     随后正常返回，执行 `update()`、引擎日志收集和 `step_callback`，
     保存该已裁决剧情步及一致的 checkpoint，再由终止组件停止。

- 后处理预算停止是控制信号，不是普通评分失败；不能伪造模型回答继续
  演绎。run.py 汇总终止原因时仍按 4.7 节优先级取值；若最后一拍已完成，
  可记 `beats_completed`，同时保留预算耗尽诊断。

## 12. 序列化与恢复

- Beat、场景与角色构建参数采用明确的 JSON 编解码。保存可重建的
  `run_config`，不保存组件实例或工厂函数；恢复时先按配置创建新的
  Simulation 及组件，再加载保存的实体和 GM 状态。
- DramaDirector 状态保存：节拍配置与游标、当前节拍已消耗步数、全局
  story_turn、评估计数、最近事件窗口、导演指令与执行步号、停止标志、
  最近已提交剧情步。角色侧保存关系缓存与刷新计数；现有组件通过标准
  Simulation checkpoint 保存记忆、场景游标、选角状态和观察队列。
- 正常 checkpoint 只在已裁决事件后处理完成且 StoryTurn 已保存的边界
  创建；此时等待裁决事件标志与待处理快照均为空，不恢复半个生命周期。
- 首次运行在构建阶段注入角色初始信息和 GM 的公开 premise；该 premise
  不计剧情步。先保存空 StoryTurn 文件与 `committed_turn_id=0` 的初始
  checkpoint，再调用 `play(premise='', ...)`。这样首次裁决失败也有
  明确的恢复起点，且恢复 turn 0 时不需要重新注入 premise。
- **保存边界与顺序**：引擎先执行 `checkpoint_callback` 再执行
  `step_callback`，两间中断会出现
  checkpoint 已前进而 StoryTurn 未保存。因此：
  - run.py 调用 Simulation.play 时不设置自动 `checkpoint_path` 或
    `get_state_callback`。统一在 `step_callback` 内**先保存 StoryTurn
    JSON，再调用 `Simulation.make_checkpoint_data()` 并保存 checkpoint**。
    本回调仅操作数据和文件，不调用 LLM。
  - 示例层 checkpoint 包含 `schema_version`、`run_config`、
    `simulation_state`、`committed_turn_id`、`used_model_attempts` 和
    原始总步数及调用预算。`simulation_state` 是标准 Simulation
    checkpoint；恢复时只将此字段传给 `load_from_checkpoint()`。
  - `committed_turn_id` 必须等于本次完整保存的最大 turn_id。
    StoryTurn 文件和 checkpoint 各自使用同目录临时文件写入并原子替换，
    不能直接覆盖已提交文件；两份文件之间的提交边界以 checkpoint 为准。
  - 恢复时按该编号核对 StoryTurn 记录：文件中超出
    `committed_turn_id` 的记录清理；已提交范围内缺行、重复或编号不连续
    时明确报错，不能靠续写补齐。
  - 恢复运行显式使用 `play(premise='', max_steps=remaining_turns)`。
    不能省略 premise 或传 None，否则 Simulation 会使用 default_premise。
    首版只使用一个故事 GM，不设置额外 initializer GM；角色初始信息由
    示例构建阶段注入，随后加载 checkpoint 的状态替换初始状态。
  - 剩余剧情步数：`total_turn_limit - committed_turn_id`。
    剩余模型额度：`total_model_attempt_limit - used_model_attempts`。
    两个计数独立；不得按剧情步数估算模型调用。剩余值为零时直接完成
    终止汇总，不再次进入引擎循环。
    BudgetGuard 恢复已保存的 used_model_attempts，并使用原始总调用上限，
    不能将新进程计数从零开始后再用原始总上限。
  - 正常保存的调用计数覆盖重试与已失败请求。裁决前预算终止或进程中断
    后，仅承诺从上一完整 checkpoint 恢复剧情；不承诺未提交请求的费用
    可由剧情 checkpoint 重建。预算耗尽的运行不能通过重置计数自动续跑。
- 两项**分开验收**：
  1. **回放确定性**：读取已保存事件渲染 → 同一剧本（确定）
  2. **恢复续写**：checkpoint 经 `json.dumps/loads` 后在新 Simulation
     中恢复并继续运行——不承诺真实模型续写内容相同
- 恢复验收：已提交范围内的 StoryTurn 不重复、不丢失；后续 turn_id
  连续；已提交为 consumed 的指令不再执行。超出提交边界的事件允许
  重新演绎，不承诺其真实模型输出与中断前相同。

## 13. 测试与验收

### 13.1 确定性测试（CI，无 API key）

- stub 按**提示类型**返回结果（非全固定回答，避免每拍自动完成）；
  固定 embedder；选择题随机化影响断言时显式关闭或语义化选择
- 覆盖清单：
  1. GM 管理查询不触发剧情计数/评估；一个已裁决事件只评估一次；
     premise（`[event]` 标签）不计数；并行组件调用不改变节拍与评分
     归属
  2. 裁决提示包含节拍与世界约束（EventResolution 接线）
  3. 完成/不完成/超时/评分无效/各类上限终止，均返回明确原因并按
     4.7 节优先级取值
  4. 导演指令只执行一次、跳拍失效、恢复后不重复消费；**节拍转换/
     完成/超时步不生成指令**（评分照常记录）
  5. 角色信息隔离（含"秘密未泄露"）、对白与舞台事件分离、中文
     Fountain 元素类型解析、FDX 独立 XML 解析后的类型与原文一致性；
     **对白传播**（甲揭示未知信息 → 乙下一
     步上下文含该发言）
  6. 新建 Simulation 的 JSON checkpoint 恢复与连续 turn_id；恢复不重
     注 premise；超出 `committed_turn_id` 的 StoryTurn 记录被清理；
     中断-恢复不重复、不丢失已提交剧情步；覆盖 turn 0 的恢复。
  7. 生命周期签名符合框架：`pre_observe(observation)` 捕获、
     `post_observe()` 计算；无事件不调用模型；RESOLVE act 的 update
     不清除等待标志，重复事件不重复提交。
  8. 最终输出适配：返回值为完整事件字符串；JSON 两字段保存到公开
     记录，导演追加事实同时进入两个输出；无效 JSON 不广播、不落盘。
  9. BudgetGuard 请求前检查、并发计数不超额、失败与重试均计数；重试
     白名单不匹配预算异常。裁决前超限由 run.py 停止且不覆盖完整
     checkpoint；后处理超限转换停止标志，仍正常提交并保存本步一次，
     不再发起模型调用。覆盖节拍判定、张力评分、指令生成三个超限位置。
  10. 保存与恢复：分别在 StoryTurn 写入前、写入后但 checkpoint 提交前、
      checkpoint 提交后模拟中断；验证原子文件、记录对齐和连续编号。
      恢复时剧情步预算与模型调用预算分别扣减，不使用重置的 StepData.step
      作为全局编号。
- 单元测试 colocated `*_test.py`（含 `get_state/set_state` round-trip）
- 验收命令：`pytest concordia && pytest examples &&
  pylint --errors-only concordia && pyrefly check concordia`

### 13.2 真实模型人工验收（CI 之外）

- 固定三份冲突场景配置，各运行 3 次；保存配置、模型标识、提示版本、
  输出
- 逐项记录并标注对应 turn_id：角色区分、因果连续、信息泄漏、对白重
  复、导演越权、结尾状态
- 发布前确定各项通过标准；节拍推进/张力入区间**不能替代**这些检查

### 13.3 成本记录

- 记录：模型调用数、token 用量（模型不提供时记**空**不记零）、单步
  与总耗时、重试次数、终止原因；预算统计覆盖重试请求

## 14. 实施阶段（Phase 1 内部三阶段）

1. **契约先行**：固定角色与信息分配、单场景、节拍推进、StoryTurn 输
   出契约、剧本导出、恢复测试（不含张力）
2. **张力观察**：启用张力评分仅记录，与人工评审比较后，再启用导演反
   馈指令
3. **角色深度**：动态关系感知与可选情绪提示，验证对对白与角色区分的
   影响

每阶段用相同示例配置与评价表。

## 15. 验收标准

1. 验收命令全绿；13.1 覆盖清单全部有测试
2. 真实模型人工验收通过发布前定制的标准（13.2）
3. 渲染：同一 JSON 输入产出确定、非空的剧本；FDX 段落类型正确、
   XML 解析后正文逐字一致，Fountain 保留为兼容视图
4. 不修改 Concordia 核心代码（diff 仅含 contrib/ 与 examples/ 新增）
5. 终止路径全部返回明确原因；截断不标记完成

### 15.1 实施方案的交付要求

工程师据本文编写实施方案时，应给出以下具体内容，不再重新选择本文已经
确定的执行路径：

1. 按第 14 节划分任务与依赖；列出每项修改的模块、组件键、构造参数和
   公开方法。角色扩展组件必须由每次构建的工厂创建新实例。
2. 给出 DramaDirector 的待裁决、待后处理、已提交及停止状态转换表，
   列出配置字段和 checkpoint 字段；给出两个预算停止路径的调用顺序。
3. 给出裁决步骤工厂、最终 JSON 校验、StoryTurn 采集与恢复函数的接口，
   标明它们在哪个生命周期阶段运行、由哪个模块拥有状态。
4. 将 13.1 节每个验收项映射到具体测试文件和输入/预期结果；给出各阶段
   可运行示例的命令、输出文件与完成条件。
5. 给出三份人工验收场景、评分量表锚点、导演参数默认值和人工评审表。
   提示模板在实施方案中提交审阅，不能只写“按需调用 LLM”。
6. 如果实现需要偏离本文的接口、保存边界或核心代码限制，明确列出原因
   与替代方案；不能在实现过程中默默改变设计契约。

## 16. 后续展望（另行设计）

- Phase 2 导演模式：复用 StepController（play/pause/step/stop +
  paused_boundary）+ 高层导演指令协议 + 分支/回放（checkpoint 分支续
  写与已保存事件回放分别设计验收）
- Phase 3 用户参演：复用 HumanActComponent + HumanInput 协议 + Web
  实时通道
- Phase 4 UGC：面向普通用户的创作抽象、剧本/世界模板、多租户服务
- 长剧记忆增强（时间戳/重要性/摘要压缩，resurrect 旧版三因子检索）

## 17. 科学依据（设计启发，非已验证模型）

以下参考支持的是**设计启发**，实现不因此构成已验证的心理学模型：

- March, J. G., & Olsen, J. P. (2011). *The Logic of Appropriateness*.
  —— basic prefab 三问链（仓库既有用法）
- Freytag, G. (1863). *Die Technik des Dramas*. —— 节拍与张力弧线
  （三幕/金字塔结构作为节拍表设计启发）
- Scherer, K. R. (2001). Appraisal considered as a process of dynamic
  multilevel checking. —— 情绪作为情境评价结果（EmotionalStance 的
  情绪选择启发）
- Vezhnevets, A. S., Agapiou, J. P., Aharon, A., Ziv, R., Matyas, J.,
  Dueñez-Guzmán, E. A., Cunningham, W. A., Osindero, S., Karmon, D., &
  Leibo, J. Z. (2023). *Generative Agent-Based Modeling with Actions
  Grounded in Physical, Social, or Digital Space Using Concordia*.
  arXiv:2312.03664.（Concordia Tech Report，架构基础）
- Vezhnevets, A. S., Matyas, J., Cross, L., Paglieri, D., Chang, M.,
  Cunningham, W. A., Osindero, S., Isaac, W. S., & Leibo, J. Z. (2025).
  *Multi-Actor Generative Artificial Intelligence as a Game Engine*.
  arXiv:2507.08892.（Concordia 设计模式与 GM/玩家能动性分工）
