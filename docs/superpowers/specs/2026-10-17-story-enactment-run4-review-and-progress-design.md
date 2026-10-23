# Story enactment run4 修复设计：干预事实边界、行动模式与停滞判断

日期：2026-10-17。
状态：阶段 A 确定性验证与阶段 B 已实施（工程验收，实施记录见
../notes/2026-10-17-story-enactment-run4-review-and-progress.md）；
阶段 C 未实施（待阶段 A 真实对照证据与授权后另行决定）；
真实模型评测未执行。

承接 [Concordia 复用设计](2026-10-15-story-enactment-concordia-reuse-design.md)、
[正式事件组装与评审设计](2026-10-16-story-enactment-canonical-review-design.md)
及其 [实施记录](../notes/2026-10-16-story-enactment-canonical-review.md)。
本文补充 run4 的故障归因、规则 8 的细化及后续验证顺序；单字段生成、
评审前确定性组装、获准后原样提交等已完成的契约继续有效。

## 1. 结论与范围

run4 暴露三种不同问题，不能都归因于模型能力、框架缺陷或漏用核心组件：

| 现象 | 归因 | 本次处理 |
|---|---|---|
| Luna 保留停电却因省略“仍未达成一致”被拒绝 | 我们的干预事实边界不够明确，评审过严 | 细化共享规则 8，补正反例和语义评测 |
| Gemini 连续准备开柜，没有实际开柜结果 | 测试使用 `speech_only`，未启用已接入的行动裁决 | 用已有 `speech_and_action` 配置做独立对照，不另造行动系统 |
| 多轮未产生有效变化，重复检查一直为 No | 已复用核心 helper，但模型判断不足；适配提示需校准 | 先评测行动模式，再校准重复判断的上下文规则 |

本次只调整 contrib/example 层的规则、测试、验证配置和文档。保留核心
`EventResolution`、`InteractiveDocument`、`attempt_to_result`、
`maybe_inject_narrative_push`、现有执行引擎、预算包装器与提交边界。
不新增通用进度引擎、事实抽取调用、证据数据库或另一套重复检测器。

## 2. run4 证据及其解释边界

证据路径相对于仓库根目录，以下目录只读保留：

- [Gemini 摘要](../../../run_results/story_enactment/gemini-3.8-flash-high_run4/summary.json)
  与 [回合记录](../../../run_results/story_enactment/gemini-3.8-flash-high_run4/story_turns.jsonl)。
- [Luna 摘要](../../../run_results/story_enactment/gpt-6-luna_run4/summary.json)
  与 [失败诊断](../../../run_results/story_enactment/gpt-6-luna_run4/process_summaries/20261016T234046298658Z-40faaf07fc99.json)。

两次 source version 相同：
`9829c87ebbc27c111f03c138cae89490a8f884ed7dadf113d92b612fb9e12eb6`。
两次均为 `mode=director`、`action_mode=speech_only`；张力反馈和重复干预
均已开启，重复检查间隔为 3，冷却为 3，张力容差为 1。

### 2.1 Gemini

提交 16 回合，119 attempts，约 532 秒，`beat_timeout`。
b1 在第 3 回合结束，b2 在第 6 回合结束；b3 的第 7–16 回合共 10 步，
达到其 `max_turns=10`，超时终止符合配置。

16 回合旁白均为空；5 次核心重复检查均未尝试干预；无张力指令。
第 7–16 回合反复提出进入侧室、准备钥匙、听口令转锁，未得到实际开柜
结果。人工阅读显示推进停滞，但不能因“没有动作”直接断定所有这些轮次
都是重复：新的有效回应、角色自主选择或有来源的信息披露也可能是进展。

### 2.2 Luna

已提交 18 回合，144 attempts，约 493 秒，`error`。失败发生在**第 19
回合**王婶发言后，不能把 `committed_turn_id=18` 当作失败回合号。

第 18 回合张力为 5，触发 b3 的 pending 张力指令：
“雷雨骤紧，老宅骤然断电，堂内只剩烛光”。第 19 回合融合候选在该事件
之后补了“雨声压住片刻争论，众人仍未就遗嘱或老宅去向达成一致”。

最终旁白为：

> 雷雨骤紧，老宅骤然断电，堂内只剩烛光；雨声压过片刻的争论。

程序组装的 canonical event 已包含这份旁白。评审认可停电合法，却把
省略“仍未达成一致”判为规则 8 违规。按本项目设计意图，该句是已有僵局
的重述，不是干预产生的新事实；省略它没有宣称达成一致，应判为误拒绝。

失败边界正常：checkpoint 停在第 18 回合、136 attempts；sidecar 与
进程摘要记录累计 144 attempts；候选未入剧本，指令未被消费。

### 2.3 已修复与尚未证明的事项

旧的双字段广播遗漏未复现；Luna 的失败稿表明 canonical 与旁白确实
来自同一字符串。这不证明评审判断已可靠。Gemini 没有执行干预，也不能
用其正常退出证明它能处理相同干预场景。两次均未完成结局，不构成完整
真实模型质量验收；也不足以估计任一模型的稳定错误率。

## 3. Concordia 能力与使用边界

核心引用：[event_resolution.py](../../../concordia/components/game_master/event_resolution.py)。

| 能力 | 当前情况及决策 |
|---|---|
| `attempt_to_result` | 已经在行动适配步骤中直接复用；本轮因 `speech_only` 无行动意图而跳过，先启用已有模式验证 |
| `maybe_inject_narrative_push` | 实际执行过重复判断；不是漏接线，其 Yes/No 本身依赖模型，不保证识别所有停滞 |
| `AccountForAgencyOfOthers` | 可征询其他玩家，但会额外调用玩家 `act`、改变当前单人回合边界；本次不接入，也不复制 |
| `restore_direct_quote` | 我们已有原始对白及确定性组装，不需要模型再次恢复引用 |
| `WorldState` | 可供后续状态记录设计复用，但自身依赖模型更新；不能自动提供行动授权或解决规则 8 误拒绝，本次不新增 |

保留“玩家提出自身尝试，GM 裁决后果，其他玩家的新选择留到其自身回合”
的边界。启用行动模式不会自动授权联合行动，也不保证每个节拍必然完成。

## 4. 阶段 A：先验证已接入的行动模式

### 4.1 配置与确定性验证

使用现有工厂生成配置，显式指定动作模式。例如只构造配置、不调用模型：

```python
from examples.story_enactment import run as run_lib
from examples.story_enactment import story_config

config = run_lib.make_run_config(
    story_config.make_example_config(),
    mode='director',
    action_mode='speech_and_action',
    total_turn_limit=24,
    total_model_attempt_limit=400,
)
```

核对顶层配置、GM 参数和场景 ActionSpec 均采用同一模式；使用共享 codec
解析 `speech` 与 `action_intent`。不改变默认 `speech_only`，不在旧状态
目录恢复时切换动作模式，不从旧对白中追认已经完成的动作。

先用确定性测试确认如下数据流：

1. 玩家明确提出“用自己的一把钥匙尝试转锁”。
2. 核心 `attempt_to_result` 根据已提交历史与世界条件给出结果。
3. 缺少另一把钥匙或协作条件时可受阻；不得替另一玩家决定配合。
4. 成功/受阻结果贯穿 evidence、旁白、待审 canonical、下一玩家观察、
   JSONL、checkpoint 与 FDX；不能又写成“以后再核验”的计划。

沿用已有行动链路测试，缺少的场景才补测试；不以新增重复测试代替实际
接线检查。若发现玩家始终输出空 `action_intent`，将其记录为行动选择
质量问题，不能未经单独设计就强制角色行动。

### 4.2 真实对照的控制变量

在获得真实调用授权后，用当前源码为两个模型各建新状态目录，比较
`speech_only` 与 `speech_and_action`。固定故事、节拍、反馈参数、预算
上限及各模型的接口参数，仅改变动作模式；动作模式天然改变提示与
可能的调用数，需要如实记录，不能声称两组提示字节相同或消耗相同。

冻结预定运行次数、评价指标与人工通过阈值后才执行。记录实际非空意图、
核心裁决次数、成功/受阻结果、后续回应、重复和结局；不只比较轮数。
完整运行的轨迹不相同，不能将两次的差别直接当作严格因果证明。

本阶段不改生产提示。后续阶段 B 的确定性修复可以独立推进，不要求
为实施一个已知修复先发起付费调用；阶段 C 的真实效果归因则需以阶段 A
的结果为对照。

## 5. 阶段 B：细化规则 8 的干预事实边界

### 5.1 判定原则

生成和独立评审使用同一份规则，区分以下内容：

| 候选中的内容 | 最终旁白要求 |
|---|---|
| 合法且新增的 NPC/环境变化 | 必须保留其事实含义，允许忠实改述 |
| 新变化的实质后果，例如停电后仅剩烛光 | 必须保留，不能逆转或只写无关背景 |
| 已有历史或原对白的复述 | 可以省略；对白仍由原始字段保真，不在旁白重复 |
| 未变化的背景状态，例如“仍未达成一致” | 可以省略，但不得写成相反的新结果 |
| 候选仅有既有状态重述，没有合法新事件 | 不能算成功干预；保持拒绝边界 |
| 候选包含他人新选择、泄密或伪造证据 | 仍需拒绝，不得借“背景描述”豁免 |

“可以省略”只针对已知、未变化的背景，不是允许丢弃核心事实。若候选
包含新的明确结果，例如查阅已授权原件后发现日期缺失，该结果不能降格
为背景省略；若候选写了从未获角色授权的集体同意，其合法性仍须审查。

当前玩家自身行动结果继续按规则 7 严格检查，成功/失败/受阻均不得丢弃、
反转或以意图替代。本设计不改变其来源、授权与保真要求。

### 5.2 推荐规则文案

在 `story_resolution.py` 的共享 `_SEMANTIC_RULES` 中替换规则 8，保留
规则 1–7 的既有边界。实施文案应表达以下含义：

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

这是一条语义规则，不能通过关键词命中或字符串等值判断代替。评审返回
仍为严格 `{"valid": bool, "reason": str}`；一次否决继续停止，不改成
“忽略错误继续”，不新增生成重试、第二评审模型或多数投票。

### 5.3 减少候选中的歧义

在 `director_note.py` 的建议/融合提示中说明：新增加的内容聚焦所选
白名单事件及可见后果，不追加“仍在争执”等无变化总结，不宣布角色改变态度。
保留完整原对白的现有融合要求；最终旁白仍由单字段生成步骤产出。

不得对融合返回值做程序删句、重写、抽取“核心句”后再冒充原始候选。
重复路径仍原样调用核心 helper；适配上下文可说明同样的事实边界，但
不修改核心的建议、随机选择或融合实现。即使候选仍含背景重述，最终
评审也必须按 §5.1 允许合法省略，而非依赖生产者永不生成该句。

### 5.4 回归与真实语义检查

将 Luna 第 19 回合缩减为随仓库交付的测试文本，不依赖 CI 中存在
`run_results/`。至少包含以下正反对照：

| 场景 | 期望 |
|---|---|
| 停电与烛光保留，省略“仍未达成一致” | 合法；待审事件与返回、提交事件一致 |
| 同一候选的忠实改述 | 不因措辞不等同而拒绝 |
| 只留雨声，遗漏停电 | 非法，注入 reviewer 拒绝，未提交、未消费 |
| 写成恢复供电，或凭空集体同意 | 非法，保留事实/自主权拒绝路径 |
| 仅重述僵局，没有新事件 | 不记成功干预 |
| 核心行动受阻却旁白写成功 | 规则 7 继续拒绝，不受规则 8 的省略例外影响 |

确定性测试验证提示、证据、组装与拒绝分支，不能证明模型真实语义检测率。
对真实模型需用同一固定上下文和上述人工标注的有效/无效旁白分别评审，
不让生成随机性混入评审误拒绝率。只改规则 8 的对照中保持其他提示与
采样参数不变；另测减少候选冗余的生产者提示，分别记录效果。

## 6. 阶段 C：在核心 helper 外校准重复判断上下文

先保留原检查次数与门控，核实阶段 A 的实际行动结果是否改善停滞；
不要把动作模式和重复提示同时改动后的效果归因于单一修复。

若行动模式下仍有相似停滞，修改 `repetition_push.py` 的既有
`_JUDGMENT_RULES`，提供更清晰的“有效进展”说明：

- 反复催促查证、提出同样开柜步骤、重复条件承诺、增加情绪强度或更换
  措辞，本身不证明发生了新结果。未来“将核对”不是已经核对。
- 新的有来源信息披露、被裁决的行动结果、新的自主立场/选择，以及
  实质回应并解决此前未回应的问题，可以是进展；不要求每轮都有物理变化。
- 第一次有意义的协作提议与后续多次重复同一提议要区分；不得把一切
  协商都归为无效，也不得把“已承诺”当作“已执行”。
- 行动受阻仍是有效结果，角色可能据此换策略；没有成功不等于没有进展。
- 当前未提交候选不算已经发生的事实。GM-only 信息、未来节拍和允许
  结局不计作公开进展；重复新提同一候选也不能补造公开历史。

可使用成对的短例子帮助理解，保持正反平衡。例如“连续催促转锁、未有
尝试结果”对照“尝试后明确发现另一把钥匙缺席，角色据此改变要求”。
例子不得泄露故事私密事实或要求角色选择某个结局。

继续把按序已提交事件、当前候选、当前行动结果和约束交给原核心 helper；
不增加独立的“是否停滞”调用，不把模型 No 用程序改成 Yes，不因到达
某个轮数就强制执行干预，不放宽白名单、冷却或玩家自主权。

测试要确认实际核心函数被调用、原返回值贯通、检查与执行分别计数。
真实效果通过固定停滞/非停滞上下文的人工标签评测，不以脚本 Yes/No
证明模型能识别重复，也不以干预次数增加作为质量通过标准。

## 7. 不变的工程边界

- 最终模型只生成 `stage_directions`；正式事件在评审前由原始对白和
  同一旁白确定性组装。评审后不改写，存储 schema 和接口不变。
- 一轮至多一路干预；张力指令优先。候选不是执行，获准且提交后才消费
  指令、计执行并进入共享冷却。
- 生成/融合/复核耗尽仍是 path A；获准后评分/完成判断耗尽仍是 path B。
  所有调用继续走既有预算与 API 重试包装器，失败消耗不返还。
- 旧 checkpoint 的动作模式和预算不可在恢复时切换；源码变更按 invocation
  归档，跨版本续跑不声称旧版本精确重放。
- 私有约束、候选与失败 trace 仅入私有诊断归档，不入玩家观察或剧本。
  凭据继续脱敏，旧 run3/run4 证据不覆写。
- 不调整 b3 上限、张力目标、评分标准或结局条件来掩盖超时。合理僵局
  可以终止，但不自动算作某个允许结局已经成立。

## 8. 实施顺序与文件范围

| 顺序 | 工作 | 文件与完成条件 |
|---|---|---|
| A | 已有动作配置验证与对照准备 | `examples/story_enactment/README.md` 及按需补充的 action integration tests；不修改默认模式和核心 helper |
| B1 | 新增规则 8 正反回归 | `story_resolution_test.py`；确认旧规则文案测试 RED，保留行动结果与提交边界测试 |
| B2 | 规则与候选提示修正 | `story_resolution.py`、`director_note.py` 及对应测试；规则 8 由生成/评审共享，候选不经程序删句 |
| B3 | 接线与端到端验证 | `story_prefabs_test.py`、`examples/story_enactment/*_test.py` 按实际影响更新；stub 继续从本轮证据推导旁白 |
| C | 依据 A 结果校准重复上下文 | `repetition_push.py`、`repetition_push_test.py`；保留原核心调用与门控，不另写检测器 |
| 收尾 | 文档与证据记录 | contrib/examples README、新的交付 notes；列明修改范围、RED/GREEN、全量检查、真实复验状态 |

阶段 A 的真实调用、B 的固定样本语义评测、C 的质量评测均为后续独立
工作。本次文档交付不执行这些调用。若阶段 A 显示无需修改重复提示，
阶段 C 可记录证据后不改生产代码，不能把探索建议变成强制扩展。

实施前保留 dirty worktree 基线，不提交、不清理、不回滚既有修改。CI
使用 `MockModel`/`PromptStubModel`，按仓库约定测试和格式化。新增源文件
如确有必要应带 Apache 2.0 头；优先在现有模块中最小修改。

## 9. 验收条件与实验记录

工程验收需要：规则 8 的范围在生成与评审中一致，新增事实及必要后果
不丢失，既有背景重述允许省略；同一事件先评审后提交，拒绝无副作用；
行动模式接线、门控、预算、恢复、导出等原回归继续通过。

对语义正确性的断言要区分：提示包含规则、stub 注入拒绝能阻止提交、
真实模型按人工标签正确判断，是三类不同证据。不得互相替代。

实施后独立记录以下检查退出码和结果，既有依赖错误单列：

```bash
source venv/bin/activate
pytest concordia
pytest examples
pylint --errors-only -j 1 concordia
pyrefly check concordia
git diff --check
```

对实际修改文件运行项目配置的 pyink/isort 检查。源码 diff 必须确认
核心目录无变动；对已被 Git 忽略的 `run_results/`，用运行前后文件清单
和内容哈希验证只读，不用空的 `git status` 充当未改动证据。没有基线
清单时明确记录该限制，不补造历史证明。

真实质量验收需提前固定运行次数、样本集与人工阈值，分别报告：

1. 非空行动意图、实际裁决结果及后续选择，是否减少重复准备而无结果。
2. 规则 8 对有效旁白的误拒绝、对遗漏/反转/越权旁白的漏检。
3. 重复检查对真实停滞的漏检和对有效进展的误报，干预是否实际改善后续。
4. 角色自主权、信息边界、因果连续、结局、预算、耗时与 API 重试。

对照分开进行：先只改变动作模式；固定上下文只改变规则 8；再在已选定
动作模式与评审版本下只改变重复判断提示。最后按原三场景工作流整体
复验。不得把错误减少、评分合格、干预变多或结束更快直接等同于质量提高。
