# Story enactment：行动裁决授权与语义评审边界修补设计

日期：2026-10-17。
状态：已实施（确定性工程验收，实施记录见
../notes/2026-10-17-story-enactment-action-authorization-review.md）；
真实模型评测（设计 §9）未执行，属后续独立工作。

承接 [canonical review 设计](2026-10-16-story-enactment-canonical-review-design.md)
及 [run4 修复设计](2026-10-17-story-enactment-run4-review-and-progress-design.md)。
本设计补充后者阶段 A/B 的真实验证发现，不启动阶段 C／原计划 Task 7。
单字段生成、评审前确定性组装、获准后原样提交、预算和恢复边界继续有效。

## 1. 结论与目标

Concordia 已有可组合的行动裁决与玩家自主权机制；本项目也确实复用了
`attempt_to_result`。当前问题是：核心 helper 的自然语言返回值被过度
当作已经获准的事实，同时现有模型复核既会漏检越权，也会误拒绝合法改述。
这不是接入一个尚未使用的组件就能自动解决的问题。

推荐以现有链路做最小修补：

1. 用独立 `InteractiveDocument` 构造行动裁决上下文，明确初始公开事实、
   已提交事件、当前玩家的自身尝试和 GM-only 限制，排除上游节拍提示等
   无关指令的继承。
2. 核心原始返回值仍原样传递、记录和受审；明确它不是授权凭证。现有一次
   最终复核同时检查裁决结果本身是否合法，以及正式事件是否忠实保留合法结果。
3. 为改述、其他玩家自主行为、物证来源、记录行为与事实认证建立平衡的
   正反语义边界，并复用到裁决、最终生成和复核中。
4. 用确定性回归证明接线和失败边界，再用冻结样本检验模型判断质量，最后
   重跑动作模式对照。

不新增裁决前置评审调用、投票、自动重生成、世界状态数据库或重复检测器。
普通非空动作轮仍是一次核心裁决、一次最终生成、一次独立复核；已有干预、
角色生成和提交后评分调用另计。无动作意图仍跳过行动裁决。

## 2. 真实证据与结论边界

证据目录只读保留：

- [规则 8 固定夹具报告](../../../run_results/story_enactment/rule8_review_probe/report.json)。
- [动作模式对照报告](../../../run_results/story_enactment/action_mode_control/README.md)。
- [Gemini 动作组](../../../run_results/story_enactment/action_mode_control/gemini-3.8-flash-high_speech_and_action/summary.json)。
- [Luna 动作组](../../../run_results/story_enactment/action_mode_control/gpt-6-luna_speech_and_action/summary.json)
  与 [已提交回合](../../../run_results/story_enactment/action_mode_control/gpt-6-luna_speech_and_action/story_turns.jsonl)。

### 2.1 固定评审样本

每个夹具的两个模型提示字节相同，各运行一次，共 14 次评审。
Gemini 7/7，Luna 6/7。两模型均接受 F1：保留停电和烛光、省略已有僵局重述。
Luna 却拒绝 F2 的“暴雨更急”，声称它不属于“雷雨骤紧”的白名单事件。
这是明确误拒绝；按预先固定的双模型零错误标准，第一轮不通过。
它只证明这组样本的表现，不给出模型稳定错误率。

### 2.2 动作模式运行

四组归档的 20 文件源码快照完全一致，source version 均为
`0cd90d1701e3fa2683f571f5e8fe91d8146c607a23cddd20f595f3e8f109a8ec`。
配置仅在模型名称、顶层/GM 动作模式及随模式改变的场景 ActionSpec 上不同。

| 模型 | 模式 | 已提交回合 | 结果 |
|---|---|---:|---|
| Gemini | speech_only | 13 | b2 beat_timeout |
| Gemini | speech_and_action | 3 | 第 4 回合王婶行动，语义复核拒绝 |
| Luna | speech_only | 13 | b2 beat_timeout |
| Luna | speech_and_action | 7 | 第 8 回合周律师行动，语义复核拒绝 |

Gemini 第 4 回合原始 `action_result` 已含“花瓶破碎，露出烧灼纸灰残片”。
纸灰不在本轮自身尝试、公开物证或授权干预来源内；`intervention` 为 null。
最终生成保留它，复核正确拒绝。不能把这个问题仅归为最终旁白擅自新增线索。

Luna 第 1 回合的当前玩家是林雪。她请求律师记录、走向保险柜，旁白却替
周律师执行“展示遗嘱封套”，并公开其中信息。即使信息与 GM 设定一致，
也不能据此授权另一玩家在林雪回合主动展示文件。该旁白已获准并提交，
是明确的自主权漏检。现有成功回合归档没有单独保存该次行动裁决 trace，
不能仅凭最终旁白断定这句话首次产生于核心裁决还是最终生成。

Luna 第 8 回合需谨慎标注：

- “各方已同意暂停处理”在律师原始对白里；旁白没有原样断言各方一致同意。
- 旁白与裁决结果都写律师补记“今晚暂停处理”，并加上“记录仅反映已发生的情况”。
- 拒绝理由将记录行为与事实成立联系起来，但“写下自己的主张”“律师暂停
  自己的办理工作”“所有人已经达成协议”是不同命题。
- “记录仅反映已发生的情况”又可能构成对未核实内容的背书。因此原完整样本
  保留为待人工定案的歧义样本；先用含义明确的正反对照确立规则，不将它
  直接算作正确拒绝，也不预先强定为必须通过。

动作组已提交轮次均有非空意图与旁白，失败候选未进入正式回合或 FDX；
checkpoint 分别停在 3、7，usage sidecar 保留失败调用消耗。这证明行动模式
有实际结果流、失败提交边界有效，不证明语义质量合格。

原对照 README 把已提交轮数 3、7 写成失败回合号，并把 Luna 旁白概括为
“all parties agreed”。后续交付 notes 应追加勘误及原产物链接，不覆写历史
运行文件。两组动作运行过早停止，不足以证明行动模式仍存在相似长期停滞。

## 3. Concordia 源码探索与复用决策

主要实现位置：
[核心 event_resolution.py](../../../concordia/components/game_master/event_resolution.py)、
[Document](../../../concordia/document/document.py)、
[InteractiveDocument](../../../concordia/document/interactive_document.py)、
[WorldState](../../../concordia/components/game_master/world_state.py)。

| 现有能力 | 实际行为 | 本次决策 |
|---|---|---|
| `EventResolution` / `run_chain_of_thought` | 依次把字符串交给可组合 thought steps；本身不审查玩家授权 | 保留核心和 thought-step 接口 |
| `attempt_to_result` | 一次 `open_question` 询问行动后果，返回 `action_attempt + Because of that + result`；没有另一个合法性验证步骤 | 继续调用一次、原样保留返回；收窄其输入并复核其内容 |
| `AccountForAgencyOfOthers` | 模型识别他人自主行为，再对相关玩家调用 `act(Yes/No)`；拒绝时还会生成替代后果 | 不接入：会增加其他玩家 act、改变一人一轮与恢复契约；不是无副作用的纯检查器 |
| `attempt_to_most_likely_outcome` | 位置、意图、至少三种后果、最可能后果，共四次文本调用；提示虽提醒自主权，仍是模型生成 | 不替换现有 helper，不能用额外生成冒充授权保证 |
| `restore_direct_quote` | 用模型重新融合引用 | 不接入：原始对白已有确定性组装保证 |
| `InteractiveDocument.new()` | 通过 `copy()` 清空内容；`InteractiveDocument.copy()` 保留同一个模型对象和 RNG | 复用，隔离裁决上下文并保留已有预算/重试包装器 |
| `DisplayEvents.get_pre_act_value()` | 从事件记忆展示最近事件，默认上限 100；该组件不调用模型 | 显式复用公开历史，不在隔离时退化为仅有 director 的 8 条窗口 |
| `WorldState` | 在 RESOLVE 后由模型从事件抽取变量并更新字符串映射 | 不接入：不是事实来源或授权验证器，可能把已污染事件继续记忆 |
| `maybe_inject_narrative_push` | 模型判断重复，建议、随机选取、融合事件 | 保持原样，本设计不校准重复判断 |
| contrib `NarrativeQualityValidator` | 统计引号、词数和角色名 | 不适用：不验证行动来源、授权或事实真假 |

核心 `generic`、`situated`、`physically_situated_and_dramaturgic` prefab
确实接入了 `AccountForAgencyOfOthers`。它们采用的征询式交互与本项目的
严格单人回合不同，不能为了复用组件而静默改变实验协议。

本次真正补足的现有能力使用是 **文档隔离**：当前
`story_action_adjudication` 直接在 `EventResolution` 的共享文档上追加约束；
而 prefab 上游已把当前 beat、Conversation 和 world constraints 放进该文档。
可参照已存在的独立最终复核与独立 repetition 文档组织方式，显式构造裁决输入。
节拍上下文是否促成了某次幻觉仍是待验证假设，不能仅由源码认定因果。

## 4. 行动裁决输入与原始结果契约

### 4.1 独立行动文档

修改 `make_action_adjudication_step`，增加仅关键字可选参数
`public_facts: Sequence[str] = ()` 与
`public_history: Callable[[], str] | None = None`。story prefab 分别传入已有
`self.params.get('public_facts', ())` 和
`display_events.get_pre_act_value`，不新增 run config 字段或历史存储。
公开历史 getter 在 build 内绑定，不放入可序列化配置。

非空意图时：

1. `document = chain_of_thought.new()`；不复制上游文档文本回来。
2. 用 Markdown 明确列出当前玩家、规则、初始公开事实、公开历史、director
   当前事件窗口及 GM-only 限制。调用 getter 一次得到本次历史字符串；
   getter 缺省时沿用 director 窗口。对白、意图、历史等数据以 JSON 编码，
   不能作为指令解释。
3. 明确标识：已提交事件中引述的发言仍是发言，不因进入历史就成为客观证据。
4. 在这个独立文档上直接调用一次原核心 `attempt_to_result`，不包装替代模型，
   不复制核心问句或自己拼造另一个结果。
5. 用现有 `record_action_result` 记录核心真实返回，并将同一返回交给后续步骤。

初始公开事实必须显式接线，不能只保留 GM-only 设定。现有 example 的
`gm_params.public_facts` 已含冲突起点、公开世界事实及角色公开事实，可直接
复用。调用方未提供时保持空序列兼容，不从 GM-only 自动补公开事实。

历史范围不能因隔离缩小。当前 prefab 的 `DisplayEvents` 默认最多展示
100 条事件，而 `director.get_committed_events()` 的事件保留窗口为
`max(scoring_window, 20)`，默认 20 条；默认张力评分窗口本身是 8 条。
隔离只移除混杂的上游提示，必须用已有 DisplayEvents getter 显式
保留前者；不能解析共享 chain 的 Conversation 文本来反推历史，也不能复制
核心事件扫描代码或创建新摘要。历史文本可能含场景起点，按原公开来源处理。

`make_final_output_step` 同样增加可选的 `public_history` getter，prefab
传入同一实例的同一方法。每次最终步骤入口取值一次，作为数据同时交给最终
生成和独立复核，不能生成时用长历史、复核时只用 director 的短窗口。行动裁决与
最终步骤之间尚无新事件提交，其公开历史应相同，由接线测试核实。
原 director 窗口继续保留用于现有评分/推进及兼容调用，不改变窗口参数。
getter 的缓存/计算不增加模型调用；仍以核心当前取回上限为边界，不谎称
全量历史。不在可用历史中的过去条件不能靠模型编造授权来补齐。

### 4.2 当前玩家与因果范围

行动结果只解决当前玩家明确提出的自身尝试及直接后果：

- 向他人请求展示、签字、交钥匙：可以完成发出请求；不能生成对方已经
  展示、签字、交付或配合。其他玩家的决定留给其自身回合。
- 当前玩家的移动、取放自己可触及的物品、记录、阅读已有可访问文件，
  可以依据世界条件成功、失败或受阻。
- 机械作用和可见的非自主反应可描述；不能据此推断同意、认罪、承诺或
  主动配合。既有历史中的他人行动可以作为背景，不重新生成一次新行动。
- GM-only 文本可以用于判断已有文件的真实内容或阻挡不可能行为，但只有
  合法的当前访问／披露动作才使相应信息公开。“请求律师展示”不等于访问。
- 不为了满足 beat 的“新证据”要求生成纸灰、录音、字条等物证；也不在
  行动裁决中额外插入与自身尝试无关的白名单事件来推进剧情。自主发生的
  导演事件仍交给已有 director/repetition 路径，保留其门控和冷却。
- 自身动作直接造成的普通物理后果与额外导演事件要区分。例如主动放下
  自己手中的物品可以发生；不能借此凭空制造关键信件或让另一玩家主动行动。

这些是共享的授权语义，不新建人物决策器或用姓名/关键词匹配来判定。

### 4.3 原始返回值不是授权凭证

继续使用 `action_result` 字段，不新增 approved/raw 双字段或 checkpoint 状态。
更新相关 docstring 与提示：它表示 **核心实际返回的待审裁决结果**，不是
“已通过语义审查的真实事实”。只有它符合授权、信息与世界约束且整个事件通过最终
评审后，才允许正式提交。

合法的成功、失败、受阻结果仍必须保留，不得把受阻写成成功、把已执行
写回未来计划，也不得无声丢弃返回值。若原始结果本身越权，整轮拒绝；
不得由最终生成删掉坏句再宣称本次核心裁决有效。

## 5. 一次最终评审，分别检查合法性与保真性

### 5.1 消除规则 2／4／7 的信任歧义

在现有共享 `_SEMANTIC_RULES` 中明确：

1. 原始 `action_result` 也受玩家自主权、物证来源、保密和世界约束审查。
   来自核心 helper 不会豁免这些规则。
2. 只有当前玩家自身合法尝试的合法结果，才可作为本轮事实来源。保真要求
   不优先于授权边界。
3. 最终复核既看结果本身，也看 canonical event。即使最终旁白删掉了
   原始结果里的违规内容，也不能放过有问题的本轮裁决。
4. 先前已提交的玩家行为、单纯提及他人的名字和非自主反应，不自动构成
   新增他人选择；应按动作是否新发生、是否自主判断。

这些行动结果检查以存在非空 `action_result` 为前提；无动作意图时不能
要求模型虚构裁决或把单纯对白误判成缺少行动证据。

裁决与最终生成/复核需要的共同授权文案放在现有
`story_action_adjudication.py` 的命名常量中，由 `story_resolution.py` 复用；
不要三处复制后各自漂移。最终评审专属的保真要求继续留在
`story_resolution.py`。依赖方向保持 `story_resolution -> story_action_adjudication
-> drama_director`，不引入反向导入或新的通用 policy 模块。

提示可采用以下核心含义，具体措辞须由实现与评审统一：

```text
The action_result is the raw outcome returned by the core resolver,
not an authorization or semantic approval. Check that outcome itself
against the speaker's own attempted action, public evidence and all
agency, secrecy and evidence constraints. Reject the turn if it contains
an unauthorized fact, even when the final narration omits that fact.
For a legal outcome, preserve its material success, failure or blocking
result in the canonical event. Never repair or silently discard an
illegal outcome to obtain approval.
```

复核仍返回严格 `{"valid": bool, "reason": str}`。拒绝理由应指出违规的
具体文本、来自裁决结果还是旁白、缺少何种来源；不能仅引用对白中的
未核实主张就判旁白已经认证它。维持一次调用，不增加程序依赖的理由解析。

### 5.2 记录行为与事实认证

| 情况 | 判定 |
|---|---|
| 律师在原始对白中说“各方已同意”，旁白没有认证 | 不因这句话本身就认定集体同意；仍检查底线等其他规则 |
| 律师明确尝试在自己的笔记里记录一个带来源、未核实的主张，旁白只确认写入 | 可合法；发生的是记录行为，不是主张被证实 |
| 律师决定暂停自己的办理工作，且自身权限/世界规则允许 | 不自动要求其他角色也作出同意；不得升级为集体协议 |
| 旁白宣布各方一致同意、承诺或签字，历史没有这些角色各自的选择 | 非法 |
| 记录含未核实主张，旁白却保证其中内容均为已证实客观事实 | 非法的事实升级 |
| 以“只是记笔记”为由伪造物证、假签名或违反角色硬约束 | 仍非法 |

生成结果应具体写“记录了谁提出的什么主张”，避免“记录仅反映已发生的
情况”这类没有逐项依据的笼统认证。不在程序中替模型改字、补免责声明或
修改原始对白；不强迫角色以某种语气或立场发言。

### 5.3 白名单按事件含义判定

规则 5 与规则 8 共同明确：白名单限定允许发生的事件及实质后果，不要求
词面一致。“雷雨骤紧”与“暴雨更急”可表达同一变化，不能仅因换词拒绝。
应平衡提供“强度改述合法”与“擅增后果非法”的对照，例如停电烛光保持
合法，额外写雷击伤人或发现遗嘱残片仍非法。

继续保留旧规则 8 的边界：新增事件和必要后果不能漏掉、反转；已有背景
重述可省略；仅重述没有新事件不算成功干预。白名单允许花瓶碎裂，不等于
允许花瓶里生成与秘密相关的证据。

## 6. 诊断、预算与提交边界

独立行动文档的 trace 不能因上下文隔离而丢失。复用现有
`director.record_resolution_diagnostics`：

- 核心成功返回：保存 `stage='action_adjudication'` 的私有 trace，
  `error=None`，原始结果仍由 `record_action_result` 原样记录。
- API 错误、预算耗尽或可捕获中断：归档当时可见的 partial trace，然后
  原异常原样抛出；不登记成功结果、不消费指令。可参照当前 repetition
  step 的 `except BaseException: record; raise`，不吞异常、不重试。
- 成功诊断记录与后续干预、最终复核拒绝记录按现有 history 并存；失败摘要
  最新 stage 仍应反映真正失败的位置。
- 私有 trace 仅进入已有诊断归档、checkpoint/JSONL 的私有诊断字段，
  不追加到模型可读的后续生成链、玩家观察或剧本正文。

不新增模型包装器，不在 `new()` 后重新创建裸模型。生成/复核前耗尽继续
走 path A，提交后评分耗尽继续走 path B。审查拒绝即停止，没有自动修稿。
模型语义拒绝不属于 API 重试。旧状态恢复维持动作模式、模型身份与总预算；
新版源码只按现有 provenance 记录，不能宣称跨版本精确重放。

## 7. 最小文件范围与实施顺序

先固化 dirty worktree 和被忽略运行目录的基线，保留所有既有修改；不提交、
不清理、不回滚既有工作。新增回归文本随测试交付，不依赖 CI 存在 `run_results/`。

| 顺序 | 文件 | 交付要求 |
|---|---|---|
| 1 | `story_action_adjudication_test.py`、`story_resolution_test.py` | 加入新上下文、共享授权规则和正反样本的失败断言；记录旧文案/接线 RED |
| 2 | `story_action_adjudication.py` | 独立行动文档、public_facts 参数、明确自身动作范围、原始返回保真、诊断归档 |
| 3 | `story_dramaturgic.py`、`story_prefabs_test.py` | 接线现有 public_facts 与 DisplayEvents 历史 getter；裁决、生成、复核历史一致，无新配置字段 |
| 4 | `story_resolution.py`、对应测试 | 共享授权规则；更新规则 2/4/5/7/8 的边界、记录语义和复核任务；不改变 JSON schema |
| 5 | `examples/story_enactment/story_enactment_test.py`、按需 `testing_stubs.py`/对应测试 | 真实核心路径与当前证据驱动的端到端提交/拒绝/预算/恢复验证，保留现有核心变异验证 |
| 6 | contrib components/prefabs README、example README、新的交付 notes | 说明待审裁决结果、诊断边界、真实质量局限；追加旧报告勘误及新评测协议 |

`drama_director.py` 的接口和状态无需改动；若 docstring 仍将结果描述为
已获语义批准，只做准确性说明。`director_note.py`、`repetition_push.py`、
核心组件和引擎不在本轮生产修改范围。禁止为解决循环导入而把 contrib
逻辑放入 examples 或复制核心。

## 8. 确定性测试验收

使用 `MockModel` 或现有 `PromptStubModel`，不在 CI 调真实模型：

1. 在传入共享 chain 放置唯一的 beat/未来任务标记；断言实际核心模型 prompt
   没有继承它，但包含初始公开事实、当前玩家、原始输入、公开历史、窗口和限制。
   GM-only 中合法用于约束的结局文字不因字符串相似就误报为继承污染。
   另放一条在 director 的 `max(scoring_window, 20)` 条窗口之外、仍在 DisplayEvents 范围内的已提交
   展示/持有事实，断言实际裁决、生成和复核 prompt 均收到它；getter 缺省
   的既有单元调用仍工作。对最终生成/复核断言读取的是入口冻结的同一份历史。
2. spy 包裹真实核心 helper：非空意图恰调用一次；真实返回值与 evidence、
   后续生成/复核读取值完全一致；无意图不调用核心。不以伪造 helper 通过此项。
3. 合法成功、受阻与纯动作结果均保真；仅修改合法核心结果会改变最终事件
   或导致拒绝，不能静默保持旧结果。保留已有相关变异检查。
4. 分别注入“裁决含他人新展示/伪造纸灰”和“旁白才新增这些内容”的拒绝。
   特别覆盖：raw result 非法、stage 已删掉坏句、review 仍拒绝整轮。
   这些 stub 验证的是规则输入和拒绝处理，不证明模型实际能发现越权。
5. 正例覆盖当前律师自己的合法展示/查阅、已发生展示的背景重述、纯记录
   一个明确归属的主张；反例覆盖越权展示、无来源物证、旁白认证集体同意。
6. 核心成功 trace 与后续失败 trace 并存；核心预算/API/中断只留 partial trace，
   不伪造结果、不吞异常；玩家观察/FDX 不含诊断标记或失败候选。
7. 至少一个获准动作流经 canonical、下一玩家真实观察、JSONL、checkpoint、
   XML 解码 FDX；至少一个拒绝停在上一提交，指令和冷却不前进。
8. 新链路继续覆盖 path A 与 path B、恢复累计计入失败调用，且同一正常动作
   轮的裁决/生成/复核调用数没有增加。预算测试按调用位置而非旧全局硬编码
   次数对齐；不能用提高预算掩盖额外调用。

新增诊断会改变部分无干预轮次的私有 history，测试应核对语义字段而非假设
列表始终为空。不要修改 stub 为所有新夹具返回 valid=true 来冒充语义验证。

实施后运行原计划完整检查：`pytest concordia`、`pytest examples`、
`pylint --errors-only -j 1 concordia`、`pyrefly check concordia`，以及实际
修改文件的 pyink/isort、`git diff --check`。记录退出码，既有依赖问题单列。
纯文档设计交付不要求将这些检查的历史结果冒充本次实施结果。

## 9. 真实模型验证协议建议

本节是后续运行的预定协议，不在本次设计交付中执行。需要冻结夹具、人工
标签、模型参数、运行次数、预算和通过标准后再调用，不能失败后择优采样。

### 9.1 固定复核样本

保留原 F1–F7，新增以下九例，形成 16 例固定集：

| 编号 | 对照场景 | 期望 |
|---|---|---|
| F8 | 林雪请求展示；raw result/stage 替律师主动展示 | 拒绝 |
| F9 | 当前玩家改为律师，自身明确尝试展示已有可访问文件，结果有设定来源 | 通过 |
| F10 | 律师早先已在自身回合展示；本轮只重述已有公开状态 | 通过 |
| F11 | 自身尝试与纸灰无关，raw result/stage 凭空制造纸灰物证 | 拒绝 |
| F12 | F11 同一 raw result，stage 删除纸灰后仅保留合法自身动作 | 仍拒绝该轮裁决 |
| F13 | 原始对白含未经证实的“各方已同意”；无行动、空 stage，不违反其他底线 | 通过 |
| F14 | 自身动作明确记录“我提出暂停、待各方确认”，只写入该未核实主张 | 通过 |
| F15 | 无各方选择来源，stage/raw result 直接认证“各方一致同意” | 拒绝 |
| F16 | 笔记含未核实的集体同意，stage 又保证其中均为已证实客观事实 | 拒绝 |

Luna 原始第 8 回合单列歧义诊断，不先计入上述通过率。人工定案时必须写明
其实际断言和授权来源；若加入计分集，标签在下一批运行前冻结。

建议每例每模型固定 3 次，共 `16 × 2 × 3 = 96` 次复核；每个模型要求
零误拒绝、零漏检、零格式错误、零 API 错误。它是有限样本发布门槛，不是
零错误保证。模型间同夹具 prompt 字节一致；固定旁白，不混入生成随机性。
需要比较旧/新提示效果时，两个版本各 96 次，样本和采样参数不变；旧版本
从其源码快照构造，不能拿不同样本的历史 14 次当严格前后对照。

### 9.2 固定行动裁决输入

冻结四种明确语义的当前尝试：请求另一玩家展示、自己展示已有文件、
王婶劝说但不接触物证、自己写入明确归属的未核实主张。
每种每模型固定 3 次，合计 24 次；只调用生产行动步骤及真实核心，不跑
其他玩家或节拍生成。人工检查每份实际返回是否越权、伪造物证、提前满足
未来剧情，及合法访问的信息是否有来源。每份都记录，不抽最佳样本。
要求上述明确非法行为为零；合法成功/失败/受阻均可，不能以成功率充当质量。

该步骤与固定复核分开，避免生成错误和审查错误互相遮蔽。若做上下文隔离
前后比较，两版使用同一输入和模型参数，分别报告；本设计不预先宣称新提示
一定消除幻觉。

### 9.3 完整动作模式对照与阶段 C gate

前两项通过后，以新目录重跑同源的双模型 × 双模式四组探索运行。
继续固定 director、24 回合、400 attempts、同一故事/反馈/模型参数；
每组预定一次。按回合人工标注：自身意图、核心结果、成功或受阻、后续自主
选择、有效进展、越权、物证与秘密来源、复核判断。报告结束原因、真实失败
回合、已提交轮数、预算、耗时和实际干预，不仅比较轮数或是否 error。

完整运行的语义硬门槛：已提交事件零明确越权/伪造证据/非法泄密；零已确认
评审误拒绝；遇到真正非法候选而停止应记为生成质量失败且边界保护成功，
不能算完整运行通过，也不能为跑完而忽略拒绝。没有结局但合理超时要单独
报告；不修改节拍上限、阈值或结局条件把它改成“成功”。

阶段 C 仍需“行动模式下仍存在相似停滞”的真实证据与用户明确授权两项。
若动作模式再次提前遇到上述质量错误，先修质量；不能用纯对白超时或
单次合理拒绝开柜替代该 gate。

## 10. 备选方案与剩余风险

不推荐立即增加一次行动结果预审。它会把普通动作轮由三次增至四次，仍然
依赖模型，且不能阻止最终旁白再次新增违规。若本方案的冻结评测仍达不到
门槛，再单独比较新增预审、不同评审模型或结构化授权结果的成本与收益。

不推荐直接挂接 `AccountForAgencyOfOthers`。如果未来希望同轮协商多角色
行动，应单独设计其玩家 act、副作用、选择记录、预算和恢复协议，而不是
在本次修补中把“下一轮才选择”改成“当前轮征询即执行”。

本方案提供确定性的输入隔离、原始结果保真、事件组装和提交边界；授权、
事实来源与忠实改述仍由模型判断，不能给出绝对语义保证。若有限样本持续
失败，应如实停止质量验收并依据新证据调整方案，而不是无限堆提示或重跑。
