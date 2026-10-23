# Story enactment 世界回应与有效进展（阶段 A）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实施设计 §4–§7 的阶段 A 修补——A1 角色本轮可完成一步的动作指引、A2 公开 NPC 回应授权与配置接线、A3 物证来源/访问/认证边界、A4 导演有效进展上下文校准——并交付确定性工程验证与文档收尾。

**Architecture:** 纯 contrib/example 提示与配置层修补。新增 `StoryConfig.npc_response_rules` → run config（`gm_params`）→ GM prefab 显式参数 → 动作裁决文档与最终生成/评审：共享规则常量一份（生产者/评审不漂移），实例规则以 JSON 数据区传入三个提示。A1 只改场景 ActionSpec 文案；A4 修改 `repetition_push` 的判断规则与建议/融合上下文、`director_note` 建议约束、`drama_director._generate_directive` 约束。零新增每轮模型调用，零核心引擎改动。

**Tech Stack:** Python 3.12、pytest（`MockModel`/`PromptStubModel` 确定性 stub）、pyink/isort/pylint/pyrefly。

**Spec:** `docs/superpowers/specs/2026-10-19-story-enactment-world-response-and-progress-design.md`（下称"设计"；本计划从设计出发论证，执行者须同时阅读设计原文）。

## Global Constraints

设计 §2/§8/§9 的全局约束，每个任务隐含遵守：

1. 不修改 `concordia/components`、`concordia/environment`、`concordia/document` 或模型包装器的生产实现；`attempt_to_result`、`maybe_inject_narrative_push` 核心 helper 原样导入调用。
2. 阶段 A **不增加每轮模型调用**：NPC 公开回应在同一次核心裁决内生成，不另调 NPC 模型；纯对白仍零次动作裁决。
3. 输出契约不变：角色严格双字段 `speech`/`action_intent`（不能同时为空）；最终输出单字段 `stage_directions`；评审严格 `{"valid": bool, "reason": str}`；不新增生产模型结构化字段。
4. original speech 逐字保留；canonical 评审前确定性组装、获准后不重写；拒绝即停止，无自动修稿、二审或换模型续写；未获准结果不投递、不提交、不消费干预。
5. 预算 path A/B、一轮至多一路导演干预、白名单、张力范围、评分标准、节拍时限、冷却、间隔、张力优先与重复门控均不变。
6. 不改节拍含义、结局条件、预算或回合上限；默认动作模式仍 `speech_only`；旧配置缺失 `npc_response_rules` 读取为空，不改写旧产物，不在恢复时换成新示例默认。
7. 不新增：世界状态数据库、通用任务图、第二套停滞检测器、模型多数投票、候选重抽/程序排序、"连续 N 轮等待强制行动"程序规则；不接入 `AccountForAgencyOfOthers`、`Plan`、`WorldState`。
8. 保留 dirty worktree、既有过程工作区与运行产物：**全程不执行任何 git commit/stash/clean/checkout，不创建 worktree**（设计 §8.8 明令，覆盖本技能模板的逐任务 commit 惯例）；实施记录提供实际文件范围与前后快照。
9. contrib 不得反向导入 examples；不执行任何真实模型调用；阶段 B（固定真实样本）与阶段 C（私密 NPC 受众设计）不在本计划内，报告须持续列明私密通话限制。
10. 每个任务完成时对触碰文件跑 `pytest`（对应范围）与 `pyink --check`/`isort --check-only`；收尾跑全量 `pytest concordia`、`pytest examples`、`pylint --errors-only -j 1 concordia`、`pyrefly check concordia`、`git diff --check`（既有依赖/类型错误与新增问题分列）。

---

### Task 1: 开工基线快照与预检

**Files:**
- Create: `/tmp/opencode/world-response-baseline/`（全部在仓库外，不污染工作区）
- 无仓库内文件改动

**Interfaces:**
- Consumes: 当前 dirty worktree、`run_results/` 目录、venv。
- Produces: 基线证据（status/diff、生产文件前后快照源、run_results 只读哈希、测试计数及 lint/type 输出与退出码基线），Task 11 收尾核对依赖它们。设计 §8.8 要求实施记录提供实际文件范围与前后快照，无基线则无法声称只读。

- [x] **Step 1: 确认虚拟环境可用**

```bash
source venv/bin/activate && python -c "import concordia; print('ok')"
```

Expected: 输出 `ok`。若 venv 不存在，先按 AGENTS.md 执行 `uv venv venv && uv pip install --editable .[dev]`。

- [x] **Step 2: 记录 dirty worktree 基线快照**

```bash
mkdir -p /tmp/opencode/world-response-baseline
git status --short > /tmp/opencode/world-response-baseline/git-status.txt
git diff > /tmp/opencode/world-response-baseline/git-diff.patch
wc -l /tmp/opencode/world-response-baseline/git-status.txt \
  /tmp/opencode/world-response-baseline/git-diff.patch
```

Expected: 两条记录非空。之后任何任务不得清空或回滚这些修改。

- [x] **Step 3: 快照本计划触碰的生产与测试文件（前后对照源）**

```bash
mkdir -p /tmp/opencode/world-response-baseline/files
for f in \
  examples/story_enactment/story_config.py \
  examples/story_enactment/run.py \
  examples/story_enactment/story_config_test.py \
  examples/story_enactment/story_enactment_test.py \
  examples/story_enactment/story_isolation_test.py \
  examples/story_enactment/README.md \
  concordia/contrib/prefabs/game_master/story_dramaturgic.py \
  concordia/contrib/prefabs/game_master/story_prefabs_test.py \
  concordia/contrib/components/game_master/story_action_adjudication.py \
  concordia/contrib/components/game_master/story_action_adjudication_test.py \
  concordia/contrib/components/game_master/story_resolution.py \
  concordia/contrib/components/game_master/story_resolution_test.py \
  concordia/contrib/components/game_master/repetition_push.py \
  concordia/contrib/components/game_master/repetition_push_test.py \
  concordia/contrib/components/game_master/director_note.py \
  concordia/contrib/components/game_master/director_note_test.py \
  concordia/contrib/components/game_master/drama_director.py \
  concordia/contrib/components/game_master/drama_director_test.py \
  concordia/contrib/components/README.md \
  concordia/contrib/prefabs/README.md ; do
  mkdir -p "/tmp/opencode/world-response-baseline/files/$(dirname "$f")"
  cp "$f" "/tmp/opencode/world-response-baseline/files/$f"
done
find /tmp/opencode/world-response-baseline/files -type f | wc -l
```

Expected: 20 个文件，包含 Task 7 将修改的 `story_isolation_test.py`。若执行时确需扩展文件范围，先补该文件的修改前快照并记录原因，再修改；不能收尾时补造基线。

- [x] **Step 4: 记录 run_results 只读基线（清单+哈希）**

```bash
find run_results -type f -print0 | sort -z | xargs -0 sha256sum \
  > /tmp/opencode/world-response-baseline/run_results-hashes.txt 2>/dev/null \
  || true
wc -l /tmp/opencode/world-response-baseline/run_results-hashes.txt
```

Expected: 记录覆盖 `story_enactment/full_story_control/20261019T032409Z` 等批次。若 `run_results/` 不存在或为空，按设计 §10.1 明确记录该限制，不补造证明。

- [x] **Step 5: 记录预改测试与 lint/type 基线**

```bash
source venv/bin/activate
set +e
pytest concordia/contrib -q > /tmp/opencode/world-response-baseline/pytest-contrib.log 2>&1
task_pytest_contrib_rc=$?
pytest examples/story_enactment -q > /tmp/opencode/world-response-baseline/pytest-story.log 2>&1
task_pytest_story_rc=$?
pylint --errors-only -j 1 concordia > /tmp/opencode/world-response-baseline/pylint.log 2>&1
task_pylint_rc=$?
pyrefly check concordia > /tmp/opencode/world-response-baseline/pyrefly.log 2>&1
task_pyrefly_rc=$?
printf 'pytest-contrib=%s\npytest-story=%s\npylint=%s\npyrefly=%s\n' \
  "$task_pytest_contrib_rc" "$task_pytest_story_rc" \
  "$task_pylint_rc" "$task_pyrefly_rc" \
  > /tmp/opencode/world-response-baseline/check-exit-codes.txt
cat /tmp/opencode/world-response-baseline/check-exit-codes.txt
tail -2 /tmp/opencode/world-response-baseline/pytest-contrib.log
tail -2 /tmp/opencode/world-response-baseline/pytest-story.log
```

Expected: 两个 pytest 范围退出码均为 0。若有失败，先停下向用户报告基线状态，不要在未知基线上开工。检查完整 lint/type 日志，按本次实测记录既有诊断，不预设仍为历史“4 项依赖错误”；未识别的新阻塞先报告。把两个 pytest 计数记入 `/tmp/opencode/world-response-baseline/test-counts.txt`。`set +e` 仅用于收集所有退出码，不代表允许忽略失败；不得用 `tail` 的退出码替代被检查命令的退出码。

---

### Task 2: `StoryConfig.npc_response_rules` 字段、校验与示例邻居规则（A2 配置层）

设计 §5.2：为 `StoryConfig` 增加可选 `npc_response_rules: tuple[str, ...] = ()`；每条规则由场景作者写出 NPC 名称、登场依据、交互触发、允许回应、知识限制及公开范围；配置加载验证字符串类型、非空内容与序列结构；空或缺失规则维持旧能力边界；`to_dict/from_dict` 完整记录；首批示例仅加入范围明确的公开邻居回应（设计引文）。选择字符串序列以复用现有配置模式，不新增 NPC agent、权限 DSL、运行期事实抽取器或 NPC 状态数据库。

**Files:**
- Modify: `examples/story_enactment/story_config.py`（`StoryConfig` 字段/docstring/`to_dict`/`from_dict`/`validate`/`make_example_config`）
- Test: `examples/story_enactment/story_config_test.py`

**Interfaces:**
- Consumes: 现有 `StoryConfig` dataclass 与 `validate` 约定。
- Produces: `StoryConfig.npc_response_rules: tuple[str, ...]`（默认 `()`）；`to_dict()['npc_response_rules']`（list）；`from_dict` 缺键读 `()`；`validate` 对结构问题返回含 `npc_response_rules` 的描述；`make_example_config()` 返回一条邻居规则（Task 3 的 `make_gm_params`、Task 10 的往返/恢复测试依赖）。

- [x] **Step 1: 写失败测试**

在 `story_config_test.py` 的 `StoryConfigTest` 类定义之后新增：

```python
class NpcResponseRulesConfigTest(unittest.TestCase):
  """Design 5.2: optional NPC response rules on StoryConfig."""

  def test_example_config_has_public_neighbor_rule(self):
    rules = story_config.make_example_config().npc_response_rules
    self.assertTrue(rules)
    joined = '\n'.join(rules)
    self.assertIn('隔壁邻居', joined)
    self.assertIn('白名单敲门事件合法登场', joined)
    self.assertIn('不披露屋内秘密', joined)
    self.assertIn('仅"准备告知"不算已经说出', joined)

  def test_roundtrip_keeps_rules_and_default_is_empty(self):
    config = story_config.make_example_config()
    d = config.to_dict()
    self.assertIn('npc_response_rules', d)
    self.assertEqual(story_config.StoryConfig.from_dict(d), config)
    empty = dataclasses.replace(config, npc_response_rules=())
    self.assertEqual(empty.to_dict()['npc_response_rules'], [])

  def test_old_dict_missing_key_reads_empty(self):
    d = story_config.make_example_config().to_dict()
    del d['npc_response_rules']
    config = story_config.StoryConfig.from_dict(d)
    self.assertEqual(config.npc_response_rules, ())

  def test_validate_rejects_bad_rule_structures(self):
    config = story_config.make_example_config()
    for bad in (
        '邻居规则', {'邻居可回应': '不是规则序列'}, None, 42,
        ['  '], [42], ['邻居可回应', None],
    ):
      with self.subTest(bad=bad):
        # 直接构造 dataclass 也要验证；类型注解不是运行期校验。
        invalid = dataclasses.replace(config, npc_response_rules=bad)
        problems = ' '.join(story_config.validate(invalid))
        self.assertIn('npc_response_rules', problems)

  def test_from_dict_rejects_before_tuple_conversion(self):
    base = story_config.make_example_config().to_dict()
    for bad in (
        '邻居规则', {'邻居可回应': '不是规则序列'}, None, 42,
        ['  '], [42], ['邻居可回应', None],
    ):
      with self.subTest(bad=bad):
        with self.assertRaisesRegex(ValueError, 'npc_response_rules'):
          story_config.StoryConfig.from_dict(
              {**base, 'npc_response_rules': bad}
          )

  def test_from_dict_accepts_list_tuple_and_empty_sequences(self):
    base = story_config.make_example_config().to_dict()
    for raw in ([], (), ['邻居的公开回应规则'], ('邻居的公开回应规则',)):
      with self.subTest(raw=raw):
        loaded = story_config.StoryConfig.from_dict(
            {**base, 'npc_response_rules': raw}
        )
        self.assertEqual(loaded.npc_response_rules, tuple(raw))
```

- [x] **Step 2: 运行确认 RED**

```bash
source venv/bin/activate
pytest examples/story_enactment/story_config_test.py::NpcResponseRulesConfigTest -v
```

Expected: FAIL（`StoryConfig` 无 `npc_response_rules` 属性，`AttributeError`/`TypeError`）。

- [x] **Step 3: 最小实现**

`examples/story_enactment/story_config.py`：

(a) `StoryConfig` docstring Attributes 段与字段，在 `allowed_endings` 之后加：

```python
    npc_response_rules: tuple[str, ...] = ()
```

docstring 增补：

```python
    npc_response_rules: GM-only content authorization for public direct
      responses of NPCs that already legally entered the story; each
      rule names the NPC, its entrance basis, the interaction trigger,
      the allowed responses, knowledge limits and the public scope. An
      empty tuple keeps the old boundary (no NPC responses beyond the
      existing director paths); the rules are authorization to generate
      within scope, not established facts and not a broadcast.
```

(b) `to_dict` 返回字典末尾加：

```python
        'npc_response_rules': list(self.npc_response_rules),
```

(c) `from_dict` 在构造 `StoryConfig` **之前**检查原始容器及元素，不能先用 `tuple()` 擦除错误结构。补充 docstring 的 `Raises: ValueError`（错误必须含字段名）：

```python
    raw_npc_rules = d.get('npc_response_rules', ())
    if not isinstance(raw_npc_rules, (list, tuple)) or any(
        not isinstance(rule, str) or not rule.strip()
        for rule in raw_npc_rules
    ):
      raise ValueError(
          'npc_response_rules must be a list or tuple of non-empty strings'
      )
```

通过后，构造参数末尾加：

```python
        npc_response_rules=tuple(raw_npc_rules),
```

(d) `validate` 在 `allowed_environment_events` 检查之后加：

```python
  if not isinstance(config.npc_response_rules, (list, tuple)):
    problems.append('npc_response_rules must be a list or tuple')
  else:
    for rule in config.npc_response_rules:
      if not isinstance(rule, str) or not rule.strip():
        problems.append(
            f'npc_response_rules must be non-empty strings, got: {rule!r}'
        )
```

(e) `make_example_config` 的 `StoryConfig(...)` 构造中，`allowed_endings` 之后加（设计 §5.2 首批示例原文语义）：

```python
      npc_response_rules=(
          (
              '邻居（隔壁邻居）：仅在其已因白名单敲门事件合法登场之后，'
              '才可作为已登场 NPC 回应；当当前角色实际隔门告知暂不需要帮助时，'
              '邻居可公开表示听到了、愿意在被请求时提供普通帮助，'
              '或按该告知离开；不披露屋内秘密，不作法律认证，'
              '不替屋内任何玩家同意，不制造证据；'
              '仅"准备告知"不算已经说出，不触发邻居按告知作出反应；'
              '该回应对在场所有场景参与者公开，不要求固定台词或必定合作。'
          ),
      ),
```

- [x] **Step 4: 运行确认 GREEN + 既有回归**

```bash
pytest examples/story_enactment/story_config_test.py -v
pytest examples/story_enactment/story_enactment_test.py -q
pytest examples/story_enactment/story_isolation_test.py -q
```

Expected: 全部 PASS（`story_enactment_test` 的 `test_records_story_scenes_beats_params_and_limits` 等若对 story dict 键集做精确断言而失败，按"新增可选键不改旧断言"原则放宽该断言并在记录中说明；不得删除既有断言）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check examples/story_enactment/story_config.py examples/story_enactment/story_config_test.py
isort --check-only examples/story_enactment/story_config.py examples/story_enactment/story_config_test.py
```

Expected: 均通过。在 `/tmp/opencode/world-response-baseline/progress.md` 记录本任务 RED/GREEN 证据（下同，不再重复）。

---

### Task 3: run.py 配置接线——`make_gm_params` 记录规则并消除世界限制双通道歧义

设计 §5.2：`to_dict/from_dict`、run config、GM prefab params、manifest 中完整记录此配置（run config 的 `gm_params` 随 checkpoint envelope/run manifest 归档，故记录于 `gm_params` 即覆盖 manifest）；同步消除 `run.py` 世界限制中的歧义——导演主动发起的新外部事件受白名单限制；已登场 NPC 的配置内直接回应受回应规则限制，不保留一句"所有 NPC 的新行为都只能逐字来自事件白名单"再靠例外提示与其冲突。

**Files:**
- Modify: `examples/story_enactment/run.py`（`make_gm_params`：环境事件约束拆为双通道措辞 + 返回字典新增键）
- Test: `examples/story_enactment/story_enactment_test.py`（`MakeRunConfigTest`）

**Interfaces:**
- Consumes: Task 2 的 `story.npc_response_rules`。
- Produces: `run_config['gm_params']['npc_response_rules']`（list[str]，Task 6 prefab 读取）；世界约束含双通道措辞（Task 4/5 的提示语义与之一致）。

- [x] **Step 1: 写失败测试**

在 `MakeRunConfigTest` 的 `test_gm_world_constraints_carry_hidden_truth_and_catalog` 之后新增：

```python
  def test_npc_response_rules_and_two_event_channels_recorded(self):
    config = run_lib.make_run_config(self.story, total_turn_limit=2)
    self.assertEqual(
        config['gm_params']['npc_response_rules'],
        list(self.story.npc_response_rules),
    )
    constraints = '\n'.join(config['gm_params']['world_constraints'])
    # 双通道：导演新外部事件走白名单；已登场 NPC 的公开直接回应走
    # 配置的回应规则，不算导演新事件（设计 5.2）。
    self.assertIn('导演主动发起的新 NPC 或环境事件', constraints)
    self.assertIn('已合法登场的 NPC', constraints)
    self.assertIn('npc_response_rules', constraints)
    self.assertIn('；'.join(self.story.allowed_environment_events), constraints)
    # 非空规则只授权公开范围，不进入角色 premise 或广播队列。
    premises = config['scene_dicts'][0]['premise']
    public_text = '\n'.join(
        line for lines in premises.values() for line in lines
    )
    self.assertNotIn('隔壁邻居被争执声惊动', public_text)
    self.assertNotIn('npc_response_rules', public_text)
    for rule in self.story.npc_response_rules:
      self.assertNotIn(rule, public_text)
```

- [x] **Step 2: 运行确认 RED**

```bash
pytest "examples/story_enactment/story_enactment_test.py::MakeRunConfigTest::test_npc_response_rules_and_two_event_channels_recorded" -v
```

Expected: FAIL（`gm_params` 无 `npc_response_rules` 键，`KeyError`）。

- [x] **Step 3: 最小实现**

`examples/story_enactment/run.py` `make_gm_params`：将现有单条 `'环境事件只能从以下选项中选择：' + ...` 约束（约 429–430 行）替换为：

```python
  constraints += (
      (
          '导演主动发起的新 NPC 或环境事件只能从以下选项中选择，'
          '不得引入范围外事件：'
          + '；'.join(story.allowed_environment_events)
      ),
      (
          '已合法登场的 NPC 对当前角色明确交互的公开直接回应另受'
          '配置的 NPC 回应规则（npc_response_rules）约束：它是该动作的'
          '直接后果，不算导演新事件，也不得借此授予 GM-only 知识、'
          '物证、电话号码、机构认证或法律结论。只有适用的 NPC 回应规则'
          '已授权该 NPC 在当前交互中的公开回应时，才可在授权范围内'
          '给出有依据的有限回应或拒绝；规则未逐句规定答案不等于禁止'
          '所有回应。规则为空、没有适用规则或触发条件未满足时，不得'
          '通过这条动作后果通道生成 NPC 回应，不得替任何玩家配合或承诺。'
      ),
  )
```

并在返回字典（`'allowed_environment_events': ...` 之后）加：

```python
      'npc_response_rules': list(story.npc_response_rules),
```

- [x] **Step 4: 运行确认 GREEN + 既有回归**

```bash
pytest examples/story_enactment/story_enactment_test.py -q
```

Expected: 全部 PASS（`test_gm_world_constraints_carry_hidden_truth_and_catalog` 断言的事件目录 `'；'.join(...)` 仍在新措辞中，应保持 GREEN）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check examples/story_enactment/run.py
isort --check-only examples/story_enactment/run.py
```

---

### Task 4: 共享授权语义常量与动作裁决接线（A2/A3 共享规则）

设计 §5.1/§5.2/§6：三种权限（其他玩家新选择留其自身回合；已合法登场 NPC 对当前明确交互的公开直接回应可在配置范围内作为当前动作的直接后果、由同一次核心裁决生成；创建新 NPC/来电/送信/敲门等新外部事件只走导演白名单）以同一份共享规则陈述于裁决、生成、评审三个提示，实例内容作为 JSON 数据传入，避免生产者与评审漂移。§6：继续使用现有共享 `AGENCY_AND_EVIDENCE_RULES`，补足物证存在、访问与认证的来源区别（对白声称≠物件存在；自己记录主张≠主张获证实；合法查看已建立文书只给已配置可见内容；对白不得推导未建立的记录；私密真相只约束不可能结果）。§5.3：现有广播链无私密内容拆分，公开是本管线 NPC 回应的构造性边界。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_action_adjudication.py`（`AGENCY_AND_EVIDENCE_RULES` 扩展 + 新常量 `PUBLIC_NPC_RESPONSE_SEMANTICS` + `make_action_adjudication_step` 新参 + `_state_adjudication_context` 数据区）
- Test: `concordia/contrib/components/game_master/story_action_adjudication_test.py`

**Interfaces:**
- Consumes: 现有 `make_action_adjudication_step(director, *, public_facts, public_history)`、`_state_adjudication_context`。
- Produces:
  - `story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS: str`（Task 5 的 `_SEMANTIC_RULES` 引用）。
  - `make_action_adjudication_step(..., npc_response_rules: Sequence[str] = ())`（Task 6 prefab 传参）。
  - 裁决文档新增 `## Configured NPC response rules (GM-only authorization, data)` JSON 数据区与 `## Public NPC response semantics` 数据区标题不变部分。

- [x] **Step 1: 写失败测试**

在 `story_action_adjudication_test.py` 顶部指纹区（`_SHARED_SCOPE_MARK` 之后）加：

```python
_NPC_RULES_DATA_MARK = '## Configured NPC response rules (GM-only'
_NPC_SEMANTICS_MARK = 'PUBLIC_NPC_RESPONSE_SEMANTICS'
_EVIDENCE_PROVENANCE_MARK = 'Dialogue claiming'
_NEIGHBOR_RULE = (
    'Neighbor: only after entering via the whitelisted knock event; when'
    ' the current character actually tells them through the door that no'
    ' help is needed, the neighbor may publicly acknowledge, offer'
    ' ordinary help when asked, or leave accordingly; no house secrets,'
    ' no legal certification, no consent on any player'
    "'s behalf, no fabricated evidence; mere preparation of the"
    ' announcement does not trigger the response.'
)
```

在 `ActionAdjudicationStepTest` 之后新增：

```python
class NpcResponseRulesWiringTest(unittest.TestCase):

  def test_rules_and_shared_semantics_reach_the_adjudication_document(self):
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(
        model, action_mode='speech_and_action', **_GM_BOUNDARIES
    )
    _open_turn(gm, 'I speak to the neighbor.', 'I tell the neighbor we are fine.')
    step = story_action_adjudication.make_action_adjudication_step(
        director, npc_response_rules=(_NEIGHBOR_RULE,)
    )
    document = interactive_document.InteractiveDocument(model)
    step(document, 'candidate', 'Alice')
    self.assertEqual(spy.call_count, 1)
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_NPC_RULES_DATA_MARK, prompt)
    self.assertIn(
        json.dumps([_NEIGHBOR_RULE], ensure_ascii=False), prompt
    )
    self.assertIn(
        story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS, prompt
    )
    self.assertIn(
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES, prompt
    )

  def test_empty_rules_keep_old_boundary_with_zero_rules_data(self):
    model = mock_model.MockModel(_OUTCOME)
    spy = _spy_text(model)
    director, gm = _make_director(
        model, action_mode='speech_and_action', **_GM_BOUNDARIES
    )
    _open_turn(gm, 'I speak.', 'I try the door.')
    step = story_action_adjudication.make_action_adjudication_step(director)
    document = interactive_document.InteractiveDocument(model)
    step(document, 'candidate', 'Alice')
    prompt = spy.call_args.kwargs['prompt']
    self.assertIn(_NPC_RULES_DATA_MARK, prompt)
    self.assertIn('[]', prompt)
    self.assertIn('If the rules are empty', prompt)
    self.assertIn('do not generate a new NPC response', prompt)
    # 空规则不改变旧行为：仍恰一次核心调用。
    self.assertEqual(spy.call_count, 1)

  def test_public_npc_scope_has_no_unconfigured_fallback(self):
    semantics = story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS
    self.assertIn('only under an applicable NPC response rule', semantics)
    self.assertIn('If the rules are empty', semantics)
    self.assertIn('or its trigger has not occurred', semantics)
    self.assertIn('do not generate a new NPC response', semantics)
    self.assertIn('Do not invent far-side silence', semantics)

  def test_evidence_provenance_semantics_in_shared_rules(self):
    # A3（设计 6）：物证存在/访问/认证区别进入共享规则常量。
    self.assertIn(
        'Dialogue claiming', story_action_adjudication.AGENCY_AND_EVIDENCE_RULES
    )
    self.assertIn(
        'never that the record exists',
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES,
    )
    self.assertIn(
        'never establish that evidence was found',
        story_action_adjudication.AGENCY_AND_EVIDENCE_RULES,
    )
```

- [x] **Step 2: 运行确认 RED**

```bash
pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -k NpcResponseRulesWiring -v
```

Expected: FAIL（无 `PUBLIC_NPC_RESPONSE_SEMANTICS` 属性，`AttributeError`）。

- [x] **Step 3: 最小实现**

`story_action_adjudication.py`：

(a) `AGENCY_AND_EVIDENCE_RULES` 末尾（`director-initiated events stay on the existing intervention paths.` 之后）追加一段（A3，设计 §6 表）：

```python
    ' Evidence provenance is separate from legal access. Dialogue'
    ' claiming "I hold the unsealing record" establishes only that the'
    ' claim was made — never that the record exists, was shown, is'
    ' inspectable, or is authentic. A character writing another'
    " person's claim onto existing paper establishes the written claim"
    ' and its attribution, not its truth, anyone'
    "'s signature, or anyone's consent. Viewing an established document"
    ' within legal access yields only its configured actual visible'
    ' content; access never makes the document authentic, seen by'
    ' everyone, or voluntarily displayed by another player. Dialogue'
    ' never derives a further unestablished record or hiding place.'
    ' GM-only truths block impossible outcomes but never establish that'
    ' evidence was found, that a character knows, or that the truth was'
    ' publicly certified.'
```

(b) 模块级新常量（置于 `AGENCY_AND_EVIDENCE_RULES` 之后）：

```python
# Shared NPC response authorization semantics (design section 5),
# stated verbatim in the adjudication prompt and reused by
# `story_resolution` in generation and review: one copy, no drift. The
# instance-level rules travel separately as inert JSON data.
PUBLIC_NPC_RESPONSE_SEMANTICS = (
    'Three separate authorizations govern figures other than the current'
    " player. (1) Another player's new consent, delivery, signature, lock"
    ' turn or confession is never generated by the current turn; it waits'
    ' for their own turn. (2) An NPC that already entered the story'
    ' through a legal committed event may answer publicly and directly'
    " the current speaker's explicit interaction with it, as a direct"
    ' consequence of the current action within this same adjudication,'
    ' but only within the configured NPC response rules given as data.'
    ' The configured rules are content authorization, not established'
    ' facts and not a broadcast: an allowed response is grounded in the'
    ' committed history, stays within the configured scope, never grants'
    ' the NPC GM-only knowledge, never promises anything on a player'
    "'s behalf, and never supplies evidence, testimony, phone numbers,"
    ' certifications or legal conclusions beyond the configuration. A'
    ' mere preparation to tell an NPC something is not an announcement'
    ' already made. A grounded refusal or limited response is allowed'
    ' only under an applicable NPC response rule that authorizes this'
    " NPC's public response to the current interaction, even when it"
    ' does not prescribe the exact answer. If the rules are empty, no'
    ' NPC response rule applies, or its trigger has not occurred, do not'
    ' generate a new NPC response through this action-consequence'
    ' channel. (3) Creating a'
    ' new NPC, call, letter, knock or any other new external event stays'
    ' on the director whitelist paths only and never rides an action'
    ' adjudication. NPC responses in this pipeline are public to all'
    ' scene participants by construction: there is no per-recipient'
    ' content, so nothing may be marked as heard by one participant'
    ' only, and a private call or whisper cannot receive its far-side'
    ' reply here. Do not invent far-side silence, refusal, disconnection'
    ' or verification failure to work around that limitation, or decide'
    ' that a player hangs up or enables speakerphone without their own'
    ' attempted action.'
)
```

(c) `make_action_adjudication_step` 签名加关键字参数（docstring 同步）：

```python
def make_action_adjudication_step(
    director: drama_director.DramaDirector,
    *,
    public_facts: Sequence[str] = (),
    public_history: Callable[[], str] | None = None,
    npc_response_rules: Sequence[str] = (),
) -> ...:
```

并在 `step` 内把 `npc_response_rules=npc_response_rules` 传入 `_state_adjudication_context`（其签名与 docstring 同步增加该参数）。

(d) `_state_adjudication_context` 在 `## Committed public events` 数据区之前加：

```python
  document.statement(
      '## Configured NPC response rules (GM-only authorization, data)'
  )
  document.statement(
      json.dumps(list(npc_response_rules), ensure_ascii=False)
  )
```

规则区语句改为：

```python
  document.statement(
      f'## Adjudication rules\n{_ADJUDICATION_RULES}\n'
      f'## Authorization semantics\n{AGENCY_AND_EVIDENCE_RULES}\n'
      f'## Public NPC response semantics\n{PUBLIC_NPC_RESPONSE_SEMANTICS}'
  )
```

模块 docstring 补一句：NPC 公开回应由同一次核心裁决在配置范围内生成（设计 §5），不另调模型。

- [x] **Step 4: 运行确认 GREEN + 既有回归**

```bash
pytest concordia/contrib/components/game_master/story_action_adjudication_test.py -v
pytest concordia/contrib/components/game_master/story_resolution_test.py -q
```

Expected: 全部 PASS（`story_resolution_test` 引用 `AGENCY_AND_EVIDENCE_RULES` 整串断言仍成立——只追加不改动既有句子；若某测试对常量做首尾精确匹配失败，按"追加段落"原则更新该断言并记录）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check concordia/contrib/components/game_master/story_action_adjudication.py concordia/contrib/components/game_master/story_action_adjudication_test.py
isort --check-only concordia/contrib/components/game_master/story_action_adjudication.py concordia/contrib/components/game_master/story_action_adjudication_test.py
```

---

### Task 5: `story_resolution` 生成与评审接线（规则 5/8 修订 + NPC 数据 + A3 正反对照）

设计 §5.2：GM prefab 将规则作为显式参数传给最终生成/评审工厂；授权语义同一份共享规则、实例内容作为 JSON 数据传入提示；消除规则 5/8 与共享授权规则的歧义（不能保留"所有 NPC 的新行为都只能逐字来自事件白名单"再靠例外提示冲突）；回应规则不能覆盖不可变事实、玩家自主权或信息受众。§6：评审必须同时检查原始核心结果与 canonical；Gemini 第 3 回合保留为原样案例，另建来源完全明确的正反夹具验证规则（未核实声称/新记录行为/合法查看构成正反对照；原始裁决非法但最终旁白删掉该句时整轮仍拒绝）。§7.2 限制：NPC 有限直接回应属动作后果，不计作又一次导演干预；不要求最终评审因"推进不够"拒绝本来合法的事件。

**Files:**
- Modify: `concordia/contrib/components/game_master/story_resolution.py`（`_SEMANTIC_RULES` 规则 5/8 修订、授权语义段追加共享常量、`make_final_output_step`/`_constraints_context` 新参）
- Test: `concordia/contrib/components/game_master/story_resolution_test.py`

**Interfaces:**
- Consumes: Task 4 的 `PUBLIC_NPC_RESPONSE_SEMANTICS`。
- Produces: `make_final_output_step(..., npc_response_rules: Sequence[str] = ())`（Task 6 prefab 传参）；生成与评审提示均含 `npc_response_rules` JSON 数据（经 `_constraints_context`）与共享 NPC 语义。

- [x] **Step 1: 写失败测试**

在 `story_resolution_test.py` 现有指纹测试（`test_shared_adjudication_rules_reach_generation_and_review` 一类）之后新增测试类（沿用该文件既有的步骤构建辅助；`_NEIGHBOR_RULE` 等指纹常量按该文件既有命名风格置于类前）：

```python
_NEIGHBOR_RULE = (
    'Neighbor: after the whitelisted knock, may publicly acknowledge a'
    ' through-the-door "no help needed" announcement, offer ordinary'
    ' help when asked, or leave; no secrets, no certification, no'
    ' consent on players'
    "' behalf, no evidence; preparation alone triggers nothing."
)

# A3 正反夹具（设计 6 表，来源完全明确，不依赖 run_results 原例）。
_CLAIM_SPEECH = '我手上有启封记录，可供查验。'
_CLAIM_INTENT = ''
_RECORD_INTENT = '我在自己带来的纸上写下陈默刚才的主张并注明出处。'
_RECORD_RESULT = (
    '纸上新增一行：陈默主张遗嘱程序有疑问（记录人：周律师）。'
)
_VIEW_INTENT = '我翻开面前已当众启封的遗嘱原件，逐字查看落款。'
_VIEW_RESULT = '原件落款为二〇一九年三月十二日，有签名与手印，无见证人。'
_ILLEGAL_ESCALATION = '启封记录客观存在且可供查验，众人已认可其真实性。'


class NpcRulesAndEvidenceProvenanceTest(unittest.TestCase):
  """Design 5.2/6: shared NPC semantics and evidence provenance."""

  def test_rules_data_and_shared_semantics_reach_both_prompts(self):
    # 按该文件既有方式构建 director + 步骤并驱动一次纯对白回合，
    # 断言生成与评审提示均含：
    #   1) constraints JSON 中的 npc_response_rules 实例数据；
    #   2) PUBLIC_NPC_RESPONSE_SEMANTICS 共享文本；
    #   3) 修订后的规则 5 双通道表述。
    ...  # 构建代码复用文件内既有 helper，见 Step 3 的接线说明
    for prompt in (generation_prompt, review_prompt):
      self.assertIn('"npc_response_rules"', prompt)
      self.assertIn(_NEIGHBOR_RULE, prompt)
      self.assertIn(
          story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS, prompt
      )
      self.assertIn('a public direct response by an NPC', prompt)
      self.assertIn(
          'never supplies evidence, testimony, phone numbers', prompt
      )

  def test_rule8_counts_configured_npc_response_as_action_consequence(self):
    self.assertIn(
        'not a director intervention',
        story_resolution._SEMANTIC_RULES,  # pylint: disable=protected-access
    )

  def test_evidence_claim_stays_attributed_speech(self):
    # 反例夹具：对白声称持有记录 + 空意图 → 评审批准的事件里，
    # 声称只保留为 attributed speech，不得升级为物件存在或可供查验。
    ...  # 复用既有纯对白回合路径，scripted reviewer 批准
    self.assertIn(_CLAIM_SPEECH, committed_event)
    self.assertNotIn('记录客观存在', committed_event)

  def test_own_recording_and_legal_view_are_legal_consequences(self):
    # 正例夹具：自己记录主张（结果只有书面主张与出处）、
    # 合法查看已建立文书（结果只有已配置可见内容）→ scripted reviewer
    # 批准、事件保真携带结果；变异检查：把结果换成越权升级句后
    # scripted reviewer 拒绝路径（复用既有拒绝断言模板）不提交不消费。
    ...

  def test_illegal_raw_escalation_rejects_even_when_stage_omits_it(self):
    # A3：原始裁决含"记录客观存在且可供查验"，最终旁白删掉该句，
    # 整轮仍拒绝（复用既有 test_rejection_covers_illegal_raw_result_
    # even_when_stage_omits_it 的构建方式，换成本夹具文本）。
    ...
```

注：`...` 处的实现按该文件既有测试的构建方式补全（director 构建、`gm.observe('[putative_event] ...')` 开回合、`make_final_output_step(...)` 驱动、`_PromptRecordingModel`/scripted reviewer 模式），断言必须落在真实提示与提交路径上；夹具文本用上面常量，不得引用 `run_results/`。

另外用实际生成与评审提示断言 Task 4 的空规则/无适用规则/触发未满足禁令均到达；不能只断言模块常量。有限回应与拒绝仍须有适用授权，规则 5/8 的例外不得扩大该范围。Task 10 负责对应非法候选的注入拒绝回归；真实模型是否正确应用规则留到阶段 B。

- [x] **Step 2: 运行确认 RED**

```bash
pytest concordia/contrib/components/game_master/story_resolution_test.py -k "NpcRulesAndEvidenceProvenance" -v
```

Expected: FAIL（`make_final_output_step` 无 `npc_response_rules` 参数，`TypeError`；`_SEMANTIC_RULES` 无新表述）。

- [x] **Step 3: 最小实现**

`story_resolution.py`：

(a) `_SEMANTIC_RULES` 规则 5 整段替换为（保留原白名单语义，追加 NPC 回应通道区分）：

```text
5. A NEW external event — creating an NPC, an incoming call, a letter,
   a knock, a weather or power change — must use an explicitly
   whitelisted means, grounded in the world and history. An empty
   whitelist permits no new event. Whitelisting never authorizes player
   choices or otherwise forbidden leaks. The whitelist governs which
   events and material consequences may occur, not their exact wording:
   a faithful paraphrase of the same change (heavier rain for a
   strengthening storm) is legal, while adding material consequences
   beyond the whitelisted event (a lightning injury, a recovered
   document fragment) remains illegal. Separately, a public direct
   response by an NPC that already legally entered the story to the
   current speaker's explicit interaction is an action consequence
   governed by the shared NPC response semantics below and the
   configured `npc_response_rules` data, not a new director event: it
   neither consumes nor bypasses the turn's single director
   intervention path, and it may never exceed the configured scope.
```

(b) 规则 8 末尾追加一句：

```text
   A public NPC direct response within the configured rules is an
   action consequence, not a director intervention; only creating a
   new external event would require the director gate.
```

(c) `_SEMANTIC_RULES` 尾部授权语义段改为：

```python
## Authorization semantics (shared with the adjudication step)
{story_action_adjudication.AGENCY_AND_EVIDENCE_RULES}
{story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS}
```

(d) `make_final_output_step` 签名与 docstring 加 `npc_response_rules: Sequence[str] = ()`，调用 `_constraints_context` 时传入；`_constraints_context` 签名加同名参数，JSON 字典加：

```python
          'npc_response_rules': list(npc_response_rules),
```

模块 docstring 补一句：生成与评审共享同一份 NPC 回应语义与实例规则数据（设计 §5.2），评审不得因"推进不够"拒绝本来合法的事件（设计 §7.2）。

- [x] **Step 4: 运行确认 GREEN + 既有回归**

```bash
pytest concordia/contrib/components/game_master/story_resolution_test.py -v
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py -q
```

Expected: 全部 PASS。若既有指纹测试（如 `test_calibrated_rule8_text_is_shared_by_generation_and_review`、`test_shared_adjudication_rules_reach_generation_and_review`）因文本变化失败：这些测试断言的是"生成与评审共享同一份文本"这一不变量，应让它们继续断言共享性（两侧提示均含新常量），并按追加/替换后的规则文本更新子串断言；在记录中列出每个改动断言的前后对照。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check concordia/contrib/components/game_master/story_resolution.py concordia/contrib/components/game_master/story_resolution_test.py
isort --check-only concordia/contrib/components/game_master/story_resolution.py concordia/contrib/components/game_master/story_resolution_test.py
```

---

### Task 6: GM prefab 接线与构建期校验（`story_dramaturgic.py`）

设计 §5.2：GM prefab 将规则作为显式参数传给动作裁决与最终生成/评审工厂；配置加载验证字符串类型、非空内容与序列结构，错误结构在建模前（任何模型调用前）明确失败；无需新增 `DramaDirector` 持久化状态（规则不进 director，不进 checkpoint 状态，只作为提示参数）；空或缺失规则维持旧能力边界。§10.1-2：NPC 配置往返、缺省为空、参数传递及归档一致。

**Files:**
- Modify: `concordia/contrib/prefabs/game_master/story_dramaturgic.py`（params 默认值 + build 校验 + 传参）
- Test: `concordia/contrib/prefabs/game_master/story_prefabs_test.py`（`StoryActionModeWiringTest` 之后新增测试类）

**Interfaces:**
- Consumes: Task 4/5 的两个工厂新参数；`_build_game_master` 测试辅助（该文件既有）。
- Produces: prefab param `npc_response_rules`（默认 `()`）；`GameMaster.build` 中结构校验 `ValueError`；规则传入 `make_action_adjudication_step` 与 `make_final_output_step`。无新 `DramaDirector` 状态、无 checkpoint 字段。

- [x] **Step 1: 写失败测试**

在 `story_prefabs_test.py` 的 `StoryActionModeWiringTest` 之后新增（沿用 `_build_game_master`、`_PromptRecordingModel`、`_FINAL_OUTPUT_MARK`、`_ATTEMPT_QUESTION_MARK` 等既有助手与指纹）：

```python
class NpcResponseRulesWiringTest(absltest.TestCase):
  """Design 5.2: rules validated at build and forwarded to both factories."""

  _RULE = (
      'Neighbor: after the whitelisted knock, may publicly acknowledge'
      ' a through-the-door announcement; no secrets, no certification,'
      ' no consent on players'  "' behalf, no evidence."
  )

  def test_bad_structure_rejected_at_build_before_any_model_call(self):
    players = [_build_character(name) for name in _PLAYERS]
    for bad in (('   ',), (42,), '一条被误写成裸字符串的邻居规则'):
      with self.subTest(bad=bad):
        prefab = story_dramaturgic.GameMaster(
            params={
                'scene_dicts': _SCENE_DICTS,
                'beat_dicts': _BEAT_DICTS,
                'action_mode': 'speech_and_action',
                'npc_response_rules': bad,
            }
        )
        prefab.entities = players
        with self.assertRaisesRegex(ValueError, 'npc_response_rules'):
          prefab.build(
              model=mock_model.MockModel(),
              memory_bank=associative_memory.AssociativeMemoryBank(
                  sentence_embedder=_embedder
              ),
          )

  def test_rules_reach_adjudication_generation_and_review_prompts(self):
    players = [
        _build_character(name, response=_CONTRACT_RESPONSE)
        for name in _PLAYERS
    ]
    model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    gm = _build_game_master(
        model,
        players,
        action_mode='speech_and_action',
        npc_response_rules=(self._RULE,),
    )
    sequential.Sequential().run_loop(
        game_masters=[gm], entities=players, max_steps=1
    )
    adjudication = next(
        p for p in model.prompts if _ATTEMPT_QUESTION_MARK in _last_block(p)
    )
    final = next(p for p in model.prompts if _FINAL_OUTPUT_MARK in p)
    review = next(
        p for p in model.prompts if '# Story resolution semantic review' in p
    )
    for prompt in (adjudication, final, review):
      self.assertIn(self._RULE, prompt)
      self.assertIn(
          story_action_adjudication.PUBLIC_NPC_RESPONSE_SEMANTICS, prompt
      )
    # 与同配置、空 npc_response_rules 的独立对照运行比较 GM 模型
    # 调用数与任务类型；角色使用自己的模型，不把角色调用计入此 model。
    # 对照必须新建 players/model/GM，禁止复用已提交的组件状态。
    control_players = [
        _build_character(name, response=_CONTRACT_RESPONSE)
        for name in _PLAYERS
    ]
    control_model = _PromptRecordingModel(
        _ACTION_STAGE_RESPONSE, attempt=_CORE_ATTEMPT_ANSWER
    )
    control_gm = _build_game_master(
        control_model, control_players,
        action_mode='speech_and_action', npc_response_rules=(),
    )
    sequential.Sequential().run_loop(
        game_masters=[control_gm], entities=control_players, max_steps=1
    )
    self.assertEqual(len(model.prompts), len(control_model.prompts))

  def test_default_empty_rules_keep_old_boundary_and_json_safe_params(self):
    players = [_build_character(name) for name in _PLAYERS]
    prefab = story_dramaturgic.GameMaster(
        params={
            'scene_dicts': _SCENE_DICTS,
            'beat_dicts': _BEAT_DICTS,
        }
    )
    prefab.entities = players
    _build_prefab(prefab)
    json.dumps(prefab.params)
    self.assertEqual(prefab.params.get('npc_response_rules', ()), ())
```

注：`_build_game_master` 已将 overrides 并入 params。同时比较核心裁决、最终生成、语义评审各自次数，而非只比总数。两组均应恰一次核心裁决，不新增独立 NPC 调用；此对照只测接线与调用，不据 scripted reviewer 推断空规则允许 NPC 回应。

- [x] **Step 2: 运行确认 RED**

```bash
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py -k NpcResponseRulesWiring -v
```

Expected: FAIL（prefab 忽略未知参数：无 `ValueError`、提示无规则文本、计数断言失败）。

- [x] **Step 3: 最小实现**

`story_dramaturgic.py`：

(a) params 默认字典（`'allowed_environment_events': (),` 之后）加：

```python
          # GM-only content authorization for public direct responses of
          # already-introduced NPCs (design section 5.2); validated at
          # build and forwarded to the adjudication and final output
          # factories. Empty keeps the old boundary.
          'npc_response_rules': (),
```

(b) `build` 中（`action_mode` 校验之后）加：

```python
    raw_npc_rules = self.params.get('npc_response_rules', ())
    if isinstance(raw_npc_rules, str) or not isinstance(
        raw_npc_rules, (list, tuple)
    ):
        raise ValueError(
            'npc_response_rules must be a sequence of non-empty strings,'
            f' got {raw_npc_rules!r}.'
        )
    npc_response_rules = tuple(raw_npc_rules)
    for rule in npc_response_rules:
        if not isinstance(rule, str) or not rule.strip():
            raise ValueError(
                'npc_response_rules must be a sequence of non-empty'
                f' strings, got element {rule!r}.'
            )
```

(c) `adjudication_step = story_action_adjudication.make_action_adjudication_step(director, ..., npc_response_rules=npc_response_rules,)` 与 `final_output_step = story_resolution.make_final_output_step(director, ..., npc_response_rules=npc_response_rules,)`。

模块 docstring 补一句：公开 NPC 回应规则经同一次核心裁决在配置范围内生成，不增加模型调用（设计 §5）。

- [x] **Step 4: 运行确认 GREEN + 既有回归**

```bash
pytest concordia/contrib/prefabs/game_master/story_prefabs_test.py -v
pytest concordia/contrib -q
```

Expected: 全部 PASS。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check concordia/contrib/prefabs/game_master/story_dramaturgic.py concordia/contrib/prefabs/game_master/story_prefabs_test.py
isort --check-only concordia/contrib/prefabs/game_master/story_dramaturgic.py concordia/contrib/prefabs/game_master/story_prefabs_test.py
```

---

### Task 7: A1 角色动作选择指引（`_SPEECH_AND_ACTION_CALL_TO_ACTION`）

设计 §4：修改现有 speech-and-action ActionSpec，不增加 JSON 字段、不新增角色推理组件；输出继续严格为两个字符串字段，两者不能同时为空；新增指引表达完整含义（回应尚未解决的问题；选择本轮可尝试的一步、描述尝试本身、不预写成功或他人配合；区分执行/等待/拒绝；等待说清缺少谁的什么回应或哪个条件；条件已满足时不无理由重提同一前置条件；符合目标底线的等待/保留/拒绝仍允许；不必每轮新增核验、登记、签认；"准备核验"只授权准备；疑问得到回应后选择接受/追问/改换/拒绝；不把将来承诺写成已执行）。角色只读自己的合法观察和私人设定，不收导演节拍/张力/结局/他人秘密。`speech_only` 只校准具体回应，不从对白追认物理动作。不增加"连续 N 轮等待强制行动"程序规则；行为质量指引不变成语义评审新硬约束（不改动 `_SEMANTIC_RULES`）。双钥匙联合操作本轮不新增授权机制。

**硬约束（stub 路由兼容）**：新文案必须保留 `根据你已知的信息，{name} 现在说什么、做什么？`（`testing_stubs._SPEAKER_IN_CALL` 正则与 `_SPEECH_ACTION_CALL_MARK = '现在说什么、做什么'` 路由指纹）、`JSON`/`speech`/`action_intent`/`两者不能同时为空` 片段。

**Files:**
- Modify: `examples/story_enactment/story_config.py`（`_SPEECH_AND_ACTION_CALL_TO_ACTION`）
- Test: `examples/story_enactment/story_config_test.py`、`examples/story_enactment/story_isolation_test.py`

**Interfaces:**
- Consumes: `entity_lib.free_action_spec`、stub 路由指纹。
- Produces: 新版双字段召唤语（Task 10 端到端与 `run.py` `_CANONICAL_CALL_TO_ACTION_MODES` 的一致性键——文案变化后该映射自动取新值，无代码改动）。

- [x] **Step 1: 写失败测试**

`story_config_test.py` `ActionSpecModeTest` 内新增：

```python
  def test_speech_and_action_spec_guides_one_completable_step(self):
    spec = story_config.make_scene_action_spec('speech_and_action')
    prompt = spec.call_to_action
    # A1（设计 4）：执行/等待/拒绝区分与不预写结果。
    for fragment in (
        '区分正在执行、等待条件和明确拒绝',
        '缺少谁的什么回应或哪个尚未满足的条件',
        '不要无理由重新提出同一前置条件',
        '不预写成功结果',
        '两者不能同时为空',
    ):
      self.assertIn(fragment, prompt)
    # 程序克制：不必每轮新增程序步骤；"准备"只授权准备。
    self.assertIn('不必每轮新增核验、登记或签认步骤', prompt)
    self.assertIn('"准备核验"只是准备', prompt)
    # 将来承诺不写成已执行。
    self.assertIn('不把对白中的将来承诺写成已执行动作', prompt)
    # stub 路由指纹保持（testing_stubs 依赖）。
    self.assertIn('现在说什么、做什么', prompt)
    self.assertRegex(prompt, r'根据你已知的信息，.+? 现在说什么')

  def test_speech_only_spec_stays_dialogue_only(self):
    spec = story_config.make_scene_action_spec('speech_only')
    self.assertNotIn('action_intent', spec.call_to_action)
    self.assertNotIn('区分正在执行', spec.call_to_action)
```

`story_isolation_test.py` 新增（沿用该文件既有的 ActionSpec 提取方式）：

```python
  def test_action_spec_carries_no_hidden_facts_or_endings(self):
    # 设计 4：角色只读自己的合法观察与私人设定；ActionSpec 是共享
    # 召唤语，不得携带导演节拍、张力、允许结局或他人秘密。
    prompt = story_config.make_scene_action_spec(
        'speech_and_action'
    ).call_to_action
    config = story_config.make_example_config()
    for character in config.characters:
      if character.secret:
        self.assertNotIn(character.secret, prompt)
    for fact in config.gm_only_facts:
      self.assertNotIn(fact, prompt)
    for ending in config.allowed_endings:
      self.assertNotIn(ending, prompt)
    self.assertNotIn('张力', prompt)
    self.assertNotIn('beat', prompt)
```

（若该文件已有等价断言则按其命名风格合并，不重复。）

- [x] **Step 2: 运行确认 RED**

```bash
pytest examples/story_enactment/story_config_test.py::ActionSpecModeTest -v
pytest examples/story_enactment/story_isolation_test.py -k action_spec_carries_no_hidden -v
```

Expected: 新指引断言 FAIL（旧文案无这些片段）；isolation 断言可能直接 GREEN（守卫性质，允许）。

- [x] **Step 3: 最小实现**

`story_config.py` 将 `_SPEECH_AND_ACTION_CALL_TO_ACTION` 整体替换为（保留契约与路由指纹，追加 A1 指引；注释块同步说明设计 §4）：

```python
_SPEECH_AND_ACTION_CALL_TO_ACTION = (
    '根据你已知的信息，{name} 现在说什么、做什么？只输出一个 JSON 对象，'
    '且仅包含两个字符串字段："speech" 为 {name} 现在的对白正文'
    '（无对白时为空字符串），"action_intent" 为 {name} 现在尝试的自身动作'
    '（无动作时为空字符串），两者不能同时为空。'
    '对白不含角色名、引号包裹、他人台词、旁白或推理过程；'
    '行动意图只描述此刻自己尝试的动作，不预写成功结果、物件内容、'
    '他人的决定或过去已完成的动作。'
    '根据你实际收到的观察，回应尚未解决的问题；若决定行动，'
    '选择自己本轮可以尝试的一步，描述尝试本身。'
    '区分正在执行、等待条件和明确拒绝：等待时说清缺少谁的什么回应'
    '或哪个尚未满足的条件；已观察到条件满足时，不要无理由重新提出'
    '同一前置条件，可以继续行动、改变方案或明确拒绝。'
    '符合自身目标和底线的等待、保留意见与拒绝仍然允许。'
    '不必每轮新增核验、登记或签认步骤；"准备核验"只是准备，'
    '不等于查看自己可访问的文书；必要的程序必须有当前问题作为依据。'
    '自己的疑问得到回应后，选择接受、追问其中具体缺口、改换方案或拒绝。'
    '不把对白中的将来承诺写成已执行动作。'
    '没有新事实时不要编造证据或声称他人已经同意。只输出该 JSON 对象。'
)
```

- [x] **Step 4: 运行确认 GREEN + stub 回归**

```bash
pytest examples/story_enactment/story_config_test.py -v
pytest examples/story_enactment/testing_stubs_test.py -q
pytest examples/story_enactment/story_enactment_test.py -q
```

Expected: 全部 PASS（stub 按指纹路由，文案变化不断路；`ActionModeRunTest` 的 canonical-mode 一致性读自工厂，自动跟随新文案）。若 `story_enactment_test` 有对旧文案子串的精确断言，按新文案更新并记录。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check examples/story_enactment/story_config.py
isort --check-only examples/story_enactment/story_config.py
```

---

### Task 8: A4 重复判断与建议/融合上下文校准（`repetition_push`）

设计 §7.1（落实旧 run4 设计 §6）：修改适配规则，原核心 helper 不变；区分可能是进展的情形（首次发现明确阻塞、新的有来源信息、真实动作结果、解决先前问题的回应、自主接受/拒绝或改变策略）与不自动算新进展的情形（多次重述同一阻塞、重复准备、不断要求同样登记、只更换措辞；首次有意义的程序要求与无依据重复加码分别判断）；移动/照明/登记有时是必要进展；有新冲突≠停滞、没有物理变化≠停滞、行动受阻≠无进展；当前未提交候选、未来节拍和私密真相不是已经发生的公开进展。不增加"是否停滞"调用，不把模型 No 程序改为 Yes，不按轮数强制干预。保持核心真实调用、原返回值贯通、检查/执行分别计数的测试成立。

**Files:**
- Modify: `concordia/contrib/components/game_master/repetition_push.py`（`_JUDGMENT_RULES`、新 `_SUGGESTION_RULES`、`_state_repetition_context` 中的建议/融合约束 + 模块 docstring）
- Test: `concordia/contrib/components/game_master/repetition_push_test.py`

**Interfaces:**
- Consumes: 既有 `make_repetition_push_step(director, model)`（签名不变）、`_state_repetition_context`。
- Produces: 校准后的 `_JUDGMENT_RULES` 与 `_SUGGESTION_RULES` 文本；后者进入传给核心的文档，随 `copy()` 到达真实建议调用，并到达融合调用（无新参数、无新调用）。

- [x] **Step 1: 写失败测试**

`repetition_push_test.py` 在 `test_core_helper_is_called_with_constrained_context` 之后新增：

```python
  def test_judgment_rules_distinguish_progress_from_repetition(self):
    # A4（设计 7.1，落实 run4 设计 6）：有效进展与无效重复的区分
    # 进入判断规则文本；门控与调用数不变。
    rules = repetition_push._JUDGMENT_RULES  # pylint: disable=protected-access
    for positive in (
        'a first discovery of a concrete blockage',
        'an adjudicated action result',
        'an autonomous acceptance, refusal or change of strategy',
        'a blocked action is still an outcome',
    ):
      self.assertIn(positive, rules)
    for negative in (
        'Restating the same blockage',
        'repeated preparation',
        'rewording the same request is not new progress',
        'not stagnation',
    ):
      self.assertIn(negative, rules)
    # 旧边界保留：候选是提案、未来计划/GM-only 不是已发生进展。
    self.assertIn('not an event that already occurred', rules)
    self.assertIn('not established progress', rules)
```

并扩展既有上下文测试（`test_core_helper_is_called_with_constrained_context`）。该测试已调用真实 helper 并捕获 `document_text`、`model.text_prompts`，追加以下断言，不使用未定义的 `prompt` 变量：

```python
    self.assertIn('a first discovery of a concrete blockage', document_text)
    self.assertIn('New conflict is not stagnation', document_text)
    suggestion_prompt = next(
        p for p in model.text_prompts
        if _SUGGEST_MARK in _last_question_block(p)
    )
    merge_prompt = next(
        p for p in model.text_prompts
        if _MERGE_MARK in _last_question_block(p)
    )
    for actual_prompt in (suggestion_prompt, merge_prompt):
      self.assertIn('concrete unresolved blockage', actual_prompt)
      self.assertIn('verification or registration chore', actual_prompt)
      self.assertIn('Never instruct a player to change their goal', actual_prompt)
    self.assertEqual(len(model.choice_prompts), 1)
    self.assertEqual(len(model.text_prompts), 2)
```

- [x] **Step 2: 运行确认 RED**

```bash
pytest concordia/contrib/components/game_master/repetition_push_test.py -v
```

Expected: 新断言 FAIL（旧 `_JUDGMENT_RULES` 无这些表述）。

- [x] **Step 3: 最小实现**

`repetition_push.py` 将 `_JUDGMENT_RULES` 替换为：

```python
_JUDGMENT_RULES = (
    'Judge repetition only against the committed public events above. A'
    ' recent quarrel or repeated disagreement alone is NOT repetition'
    ' when new facts, adjudicated action results or effective responses'
    ' to each other occurred. Judge effective progress, not novelty: a'
    ' first discovery of a concrete blockage, new sourced information,'
    ' an adjudicated action result, a response that resolves a'
    ' previously unanswered question, and an autonomous acceptance,'
    ' refusal or change of strategy can each be progress.'
    ' Restating the same blockage, repeated preparation, endlessly'
    ' demanding the same registration, or rewording the same request is'
    ' not new progress; distinguish a first meaningful procedural'
    ' request from unfounded repeated escalation. Movement, lighting or'
    ' registration can be necessary progress when it moves a current'
    ' question toward a definite outcome. New conflict is not'
    ' stagnation, absence of physical change is not stagnation, and a'
    ' blocked action is still an outcome a character may act on.'
    ' One-time events that already happened cannot happen again. The'
    ' current candidate is a proposal for this turn, not an event that'
    ' already occurred; future plans, possible endings and any GM-only'
    ' knowledge are not established progress.'
)
```

同一模块新增适配层常量（设计 §7.2）：

```python
_SUGGESTION_RULES = (
    ' For every suggested complication and its fusion, prefer a legal'
    ' change tied to a concrete unresolved blockage or unanswered'
    ' question in the committed public history. Do not stack another'
    ' interruption, verification or registration chore onto unresolved'
    ' ones merely for novelty. Never instruct a player to change their'
    ' goal, confess, cooperate, or pick a specific ending. Remain within'
    ' the allowed means and information boundaries; these preferences'
    ' never authorize an otherwise illegal event.'
)
```

在 `_state_repetition_context` 构造传给核心 helper 的文档时，加一条 `document.statement('## Suggestion and fusion constraints\n' + _SUGGESTION_RULES)`。位置在调用 helper 之前；不能仅记录到诊断或最终生成文档。核心 `copy()` 保留这些上下文，故建议阶段无需修改核心也能收到约束；融合同样使用原文档中的该约束。不改变核心建议问题、随机选择、融合、门控或次数。

模块 docstring 补充：判断规则区分有效进展与无效重复（设计 §7.1），建议与融合聚焦已有阻塞（§7.2）；不新增“是否停滞”调用、不改核心 Yes/No、不按轮数强制干预。

- [x] **Step 4: 运行确认 GREEN + 门控/计数回归**

```bash
pytest concordia/contrib/components/game_master/repetition_push_test.py -v
pytest concordia/contrib/components/game_master/drama_director_test.py -q
```

Expected: 全部 PASS（真实核心调用、原返回值贯通、检查/执行分别计数的既有断言不变）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check concordia/contrib/components/game_master/repetition_push.py concordia/contrib/components/game_master/repetition_push_test.py
isort --check-only concordia/contrib/components/game_master/repetition_push.py concordia/contrib/components/game_master/repetition_push_test.py
```

---

### Task 9: A4 导演建议上下文（`director_note` + `drama_director._generate_directive`）

设计 §7.2：在 `drama_director._generate_directive`、`director_note` 及 repetition 的建议约束中表达——基于已有历史中的具体阻塞，优先提出与当前可行动问题有关的合法变化；不要仅为新颖而继续堆叠打断和核验任务；不得指挥玩家改变目标、认罪、配合或选择某个结局。白名单、张力范围、评分标准、节拍时限、冷却、间隔、张力优先与一轮至多一路导演干预均不变；不增加候选重抽/程序排序/新规划调用；原 pending 指令可以无合法建议（零建议路径原样保留）；不要求最终评审因"推进不够"拒绝本来合法的事件。不要求导演保证收敛：玩家自主拒绝形成的僵局允许正常超时。

repetition 路径的建议/融合约束已由 Task 8 写入 contrib 上下文，并验证到达真实核心的建议调用；核心 helper 本身不变。本任务补齐其余两条路径，不能以“不改核心”为由跳过 repetition 建议上下文。

**Files:**
- Modify: `concordia/contrib/components/game_master/director_note.py`（新常量 `_PROGRESS_FOCUS` 并入两个问题）
- Modify: `concordia/contrib/components/game_master/drama_director.py`（`_generate_directive` 约束语句追加）
- Test: `concordia/contrib/components/game_master/director_note_test.py`、`concordia/contrib/components/game_master/drama_director_test.py`

**Interfaces:**
- Consumes: 既有 `_complication_question`/`_merge_question`/`_generate_directive`。
- Produces: 无新签名；两个提示文本新增进展聚焦约束。

- [x] **Step 1: 写失败测试**

`director_note_test.py` 在 `test_prompts_focus_added_content_on_new_events_only` 之后新增：

```python
  def test_prompts_prefer_progress_over_novelty_stacking(self):
    # A4（设计 7.2）：建议聚焦已有历史的具体阻塞，不为新颖堆叠
    # 打断/核验任务，不指挥玩家目标、认罪、配合或结局。
    merged = self._fuse_with_pending_directive()  # 按该文件既有构建方式
    for prompt in self.captured_prompts:  # complication + merge 两问
      self.assertIn('concrete unresolved blockage', prompt)
      self.assertIn('never instruct a player to change their goal', prompt)
      self.assertIn('verification or registration chore', prompt)
```

（`_fuse_with_pending_directive`/`captured_prompts` 按该文件既有的 director/文档构建 helper 命名；若名称不同，用文件内等价方式驱动一次融合并捕获两问文本。）

`drama_director_test.py` 找到断言 `_generate_directive` 约束文本的既有测试（指令生成 prompt 相关用例）之后新增：

```python
  def test_directive_generation_prefers_actionable_progress(self):
    directive = self._generate_directive_with_deviation()  # 既有构建方式
    prompt = self.recorded_generation_prompt
    self.assertIn('concrete unresolved problem', prompt)
    self.assertIn('novelty for its own sake', prompt)
    self.assertIn('verification or registration chore', prompt)
    self.assertIn('Never make players agree', prompt)  # 旧边界保留
```

（同样按该文件既有 helper 命名；若该文件尚无记录生成 prompt 的方式，用 spy 包 `InteractiveDocument.open_question` 捕获。）

- [x] **Step 2: 运行确认 RED**

```bash
pytest concordia/contrib/components/game_master/director_note_test.py -k progress_over_novelty -v
pytest concordia/contrib/components/game_master/drama_director_test.py -k actionable_progress -v
```

Expected: FAIL（无这些表述）。

- [x] **Step 3: 最小实现**

(a) `director_note.py` 在 `_CANDIDATE_FOCUS` 之后加常量：

```python
# A4 (design 7.2): suggestions target actionable progress tied to the
# existing blockage, not novelty stacking. Guidance only — the fused
# return value is never rewritten programmatically and no candidate is
# re-drawn or re-ranked.
_PROGRESS_FOCUS = (
    ' Prefer a complication that addresses a concrete unresolved'
    ' blockage or unanswered question visible in the committed public'
    ' history. Do not stack another interruption, verification or'
    ' registration chore onto unresolved ones merely for novelty, and'
    ' never instruct a player to change their goal, confess, cooperate,'
    ' or pick a specific ending.'
)
```

并在 `_complication_question`（`{_CANDIDATE_FOCUS}` 之后）与 `_merge_question`（`{_CANDIDATE_FOCUS}` 之后）各追加 `f'{_PROGRESS_FOCUS}\n'`。

(b) `drama_director.py` `_generate_directive` 的 `## Constraints` 第二条 statement（`'Never make players agree...'` 一段）末尾追加：

```python
        ' Base the instruction on a concrete unresolved problem visible'
        ' in the recent public events rather than novelty for its own'
        ' sake; do not add another interruption, verification or'
        ' registration chore on top of unresolved ones.'
```

两处 docstring/注释补一句设计 §7.2 出处。

- [x] **Step 4: 运行确认 GREEN + 优先级/冷却回归**

```bash
pytest concordia/contrib/components/game_master/director_note_test.py -v
pytest concordia/contrib/components/game_master/drama_director_test.py -v
pytest examples/story_enactment/story_enactment_test.py -k Repetition -q
```

Expected: 全部 PASS（导演优先级、冷却、至多一路干预、零建议路径的既有断言不变；`RepetitionPipelineIntegrationTest` 的既有场景不因文案变化失败——若其对建议文本有精确断言则按新文案更新并记录）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check concordia/contrib/components/game_master/director_note.py concordia/contrib/components/game_master/drama_director.py
isort --check-only concordia/contrib/components/game_master/director_note.py concordia/contrib/components/game_master/drama_director.py
```

---

### Task 10: 确定性整体验收——公开邻居回应端到端、非法候选拒绝、往返/恢复与调用计数

设计 §10.1：至少覆盖——(3) 已登场邻居的公开有限回应经过真实核心调用、原样 evidence、生成、复核、下一玩家观察、JSONL、checkpoint 和 FDX，未丢弃或降格核心结果；(4) 邻居未登场、仅准备告知、替另一玩家配合、私密内容被全员广播等非法候选，注入 reviewer 拒绝后不提交、不投递、不消费；(6) 一次原核心调用、原返回值变化能改变结果的变异检查继续成立；NPC 功能无新增独立调用，纯对白仍零次动作裁决；(7) 公开 NPC 直接回应不作为独立导演干预、不暗中创建未获准新事件；(8) 多段对白、非空旁白、来源规则、拒绝、预算 path A/B、API 错误与恢复消耗契约按实际受影响范围回归（多为既有套件重跑）。配置往返与恢复：旧配置缺失读取为空、归档一致（§5.2）。

**Files:**
- Test: `examples/story_enactment/story_enactment_test.py`（`ActionBoundaryIntegrationTest` 之后新增 `PublicNpcResponseIntegrationTest(_ActionChainTestCase)`；`ActionModeRunTest`/`ResumeContinuationTest` 内补恢复断言）

**Interfaces:**
- Consumes: Task 2–9 交付（example config 邻居规则、gm_params、prefab 接线、共享语义与导演上下文）；既有助手 `_fixture_config`、`_ActionChainTestCase._run_with_stub`、`testing_stubs.PromptStubModel(action_intents=, action_result=, review_response=, stage_directions=)`、`_ATTEMPT_QUESTION`、`_last_question_block`、`_fdx_paragraphs`、`self.envelope()`、`self.turn_log()`。
- Produces: 端到端验收证据（Task 11 交付记录引用）。

- [x] **Step 1: 写端到端失败测试**

新增（结构对齐既有 `test_success_result_flows_to_observation_and_fdx`：场景顺序为 林雪→陈默→周律师→王婶，故跑 4 回合、律师第 3 回合行动、王婶第 4 回合观察；断言助手用该文件既有的 `_prompts_routed_to`、`_exercise_prompts_of`、`_entity_memory_text`、`_fdx_paragraphs`、`self.restored(config)`）。正例必须先通过真实干预与提交路径在第 2 回合建立敲门历史，不能把规则中的条件句当作已发生事实，也不直接向 director 内部状态塞一个邻居登场标记。下面的 fixture 复用 `RepetitionPipelineIntegrationTest._director_config` 的参数模式，interval=2、cooldown=3，确保第 2 回合登场、第 3 回合回应、第 4 回合不重复登场；真实语义识别质量仍留到阶段 B。

```python
class PublicNpcResponseIntegrationTest(_ActionChainTestCase):
  """Design 5: public, in-scope NPC responses ride the one core call.

  The stub's `action_result` stands for the one core adjudication's
  return (which may include the configured neighbor response); these
  tests prove the plumbing — evidence, generation, review, observation,
  exports, call counts — never real semantic judgment.
  """

  _INTENT = '周律师走到门边，隔着门对邻居说明今晚家事暂不需要帮助。'
  _KNOCK = '隔壁邻居被争执声惊动，前来敲门探问'
  _RESPONSE = (
      '邻居在门外公开应声表示听到了，说若有需要随时可以叫他，'
      '随后按告知离开。'
  )

  def _neighbor_config(self):
    config = self._action_config()
    story = story_config.make_example_config()
    config['mode'] = 'director'
    config['director_params'] = {
        'eval_interval': 1,
        'scoring_window': 8,
        'tolerance': 1,
        'feedback_enabled': True,
        'cooldown_turns': 3,
        'allowed_environment_events': list(story.allowed_environment_events),
        'immutable_facts': list(story.immutable_facts),
        'repetition_push_enabled': True,
        'repetition_check_interval': 2,
    }
    return config

  def test_public_neighbor_response_flows_end_to_end(self):
    config = self._neighbor_config()
    stub = testing_stubs.PromptStubModel(
        action_intents={'周律师': self._INTENT},
        action_result=self._RESPONSE,
        stage_directions='',
        scoring_answer='5',
        repetition_answer='Yes',
        push_events=self._KNOCK,
        push_merged='',
    )
    result = self._run_with_stub(self.state_dir, config, stub)
    self.assertEqual(result.reason, 'step_limit')
    self.assertEqual(result.committed_turn_id, 4)
    records = self.turn_log().load()
    self.assertNotIn(self._KNOCK, records[0]['resolved_event'])
    self.assertIn(self._KNOCK, records[1]['resolved_event'])
    self.assertTrue(
        records[1]['director_diagnostics']['repetition']['executed']
    )
    # 恰一次核心裁决。既验证规则 JSON 数据，也解码历史数据区，
    # 确认第 2 回合已提交事件是本次回应的事实来源。
    adjudications = _prompts_routed_to(stub, _ATTEMPT_QUESTION)
    self.assertEqual(len(adjudications), 1)
    self.assertIn('隔壁邻居', adjudications[0])
    self.assertIn('白名单敲门事件合法登场', adjudications[0])
    self.assertIn('不披露屋内秘密', adjudications[0])
    self.assertIn(
        '## Configured NPC response rules (GM-only authorization, data)',
        adjudications[0],
    )
    self.assertIn(
        json.dumps(config['gm_params']['npc_response_rules'], ensure_ascii=False),
        adjudications[0],
    )
    history_section = adjudications[0].split(
        '## Public history (data)\n', 1
    )[1]
    history, _ = json.JSONDecoder().raw_decode(history_section.lstrip())
    self.assertIsInstance(history, str)
    self.assertIn(self._KNOCK, history)
    # 核心结果原样贯通 evidence、提交事件、下一玩家观察、JSONL、
    # checkpoint、FDX（变异契约：改 _RESPONSE 文本则事件随之变化）。
    lawyer = records[2]
    core_result = f'{self._INTENT} Because of that, {self._RESPONSE}'
    self.assertEqual(lawyer['stage_directions'], core_result)
    self.assertIn('随后按告知离开', lawyer['resolved_event'])
    # 下一玩家（王婶）在实际 action prompt 中观察到公开回应。
    wang_prompts = _exercise_prompts_of(stub, '王婶')
    self.assertEqual(len(wang_prompts), 1)
    self.assertIn('随后按告知离开', wang_prompts[0])
    # 恢复后的 director 状态与玩家记忆携带该公开事实。
    sim_after, director_after = self.restored(config)
    recent = cast('list[str]', director_after.get_state()['recent_events'])
    self.assertTrue(any('随后按告知离开' in e for e in recent))
    self.assertIn('随后按告知离开', _entity_memory_text(sim_after, '王婶'))
    self.assertIn('随后按告知离开', self.read_script('script.txt'))
    self.assertEqual(
        _fdx_paragraphs(self.state_dir, 'Action'),
        [records[1]['stage_directions'], core_result],
    )
    # 无新增独立 NPC 调用：空规则对照仍发生同一次合法敲门，但
    # 核心只返回律师自己的告知，不生成任何 NPC 回应。
    control_config = self._neighbor_config()
    control_config['story']['npc_response_rules'] = []
    control_config['gm_params']['npc_response_rules'] = []
    control_stub = testing_stubs.PromptStubModel(
        action_intents={'周律师': self._INTENT},
        action_result='周律师完成了自己的隔门告知。',
        stage_directions='',
        scoring_answer='5',
        repetition_answer='Yes',
        push_events=self._KNOCK,
        push_merged='',
    )
    control_dir = os.path.join(self._tmp.name, 'without-npc-rules')
    control = self._run_with_stub(control_dir, control_config, control_stub)
    self.assertEqual(control.reason, 'step_limit')
    self.assertEqual(control.committed_turn_id, 4)
    self.assertEqual(len(stub.text_prompts), len(control_stub.text_prompts))
    self.assertEqual(len(stub.choice_prompts), len(control_stub.choice_prompts))
    self.assertEqual(
        len(_prompts_routed_to(control_stub, _ATTEMPT_QUESTION)), 1
    )

  def test_review_rejection_of_illegal_candidates_commits_nothing(self):
    # 设计 10.1-4：非法候选（邻居未登场、仅准备告知、替另一玩家
    # 配合、私密内容全员广播，以及规则为空/没有适用规则）
    # 注入 reviewer 拒绝后不提交、不投递、
    # 不消费。复用 SemanticReviewFailureTest 的拒绝断言模板，逐候选
    # subTest：仅在目标轮的语义评审返回
    # '{"valid": false, "reason": "..."}'，断言
    # committed_turn_id 不含该步、队列无 Event、指令/干预未消费。
    # 原四类候选分别构造：action_result 写邻居从未登场却回应、
    # action_intents 用"准备告知"措辞而 action_result 已含邻居按
    # 告知反应、action_result 含替陈默/林雪答应、action_result 含
    # "仅律师听见"的来电者答复。
    # 空规则或不适用规则时也不能以“有限回应/拒绝”为由生成回应。
    # 对私密限制另加：不得为绕过限制虚构对方沉默、拒绝、挂断或
    # 身份核验失败。此处只验证 scripted rejection 边界。
    # 对合法登场的反例，沿用 _neighbor_config 的真实敲门提交；
    # 未登场反例用 _action_config，禁止暗中给它补登场历史。
    # 前两轮 reviewer 批准，只在律师第 3 轮拒绝；预期 checkpoint
    # 停在 2，不是 0。相对失败前状态证明本步未新增投递、提交或
    # 消费；已经提交的第 2 轮合法敲门不会因此回滚。
    ...

  def test_npc_response_is_not_a_director_intervention(self):
    # 设计 7.2/10.1-7：公开 NPC 直接回应不产生 director 干预记录；
    # 第 2 轮敲门确有一次已执行干预；第 3 轮律师回应的
    # director_diagnostics.intervention 为 None，且不因回应新增
    # 执行计数或刷新最后执行回合。检查第 3/4 轮没有第二次干预，
    # 不错误要求整个运行的干预总数为 0。
    ...
```

（`...` 处按文件内既有断言/helper 补全。）

- [x] **Step 2: 运行确认 RED→GREEN 判定**

```bash
pytest examples/story_enactment/story_enactment_test.py::PublicNpcResponseIntegrationTest -v
```

Expected: Task 2–6 已合并时端到端应 GREEN（接线验证，不是缺陷修复——若 FAIL，按失败点回到对应任务修正，不在本任务改生产代码）。非法候选拒绝子测试通过注入 reviewer 拒绝验证既有失败边界，预期 GREEN。

- [x] **Step 3: 补配置往返/恢复测试**

在 `ActionModeRunTest` 类内新增，复用它实际已有的 `_json_fixture`、`_dir`、`_commit_after_turn_two`、`_envelope_of`，不引用只存在于 `_ActionChainTestCase` 的 `_action_config`：

```python
  def test_old_config_without_npc_rules_resumes_empty(self):
    # 设计 5.2：旧配置缺失 npc_response_rules 读取为空，恢复不换
    # 示例默认；新目录新配置才带非空规则。
    config = self._json_fixture(total_turn_limit=4)
    config['gm_params'].pop('npc_response_rules', None)
    story_dict = config['story']
    story_dict.pop('npc_response_rules', None)
    # 用 _commit_after_turn_two(directory, config) 在第 2 轮后中断，
    # 再 resume 实际执行到第 4 轮。恢复后的 gm_params 仍无该键，
    # 重建 GM 的裁决/生成/评审规则数据为空，不注入示例邻居规则。
    # 同时检查归档与传入的原 config 未被写入非空默认；禁止用一个
    # 已达 total_turn_limit 的空跑恢复代替此测试。
    ...
```

（构建方式复用 `ActionModeRunTest` 内既有 first-run + resume 模板。）

```bash
pytest examples/story_enactment/story_enactment_test.py -k "old_config_without_npc_rules" -v
```

Expected: GREEN（Task 6 的 `params.get(..., ())` 与 Task 2 的 `from_dict` 缺省已保证；若 FAIL 回对应任务修正）。

- [x] **Step 4: 全范围回归（设计 10.1-6/8 的既有套件重跑）**

```bash
pytest examples/story_enactment -q
pytest concordia/contrib -q
```

Expected: 全部 PASS；把两个计数与 Task 1 基线对照写入进度记录（新增测试使计数增加；无既有测试被删除）。

- [x] **Step 5: 格式检查（不 commit）**

```bash
pyink --check examples/story_enactment/story_enactment_test.py
isort --check-only examples/story_enactment/story_enactment_test.py
```

---

### Task 11: 收尾——README/notes、全量检查与只读核对

设计 §9 收尾：contrib components/prefabs README、example README、新 notes；记录旧报告勘误（设计 §1 三项：`speech_only` 不能直接执行物理动作不等于所有允许结局不可达、speech-only 可有合法导演旁白、protocol 的 repetition 开关记录与实际配置不一致须披露）与尚未支持的私密交互（设计 §5.3/§11：阶段 C gate 未实施，公开邻居回应通过不等于全部世界交互闭环）。§10.1 代码检查全量执行；`run_results` 只读核对用 Task 1 哈希基线，不用空 `git status` 充当证据。

**Files:**
- Modify: `examples/story_enactment/README.md`（Action Modes 节后新增 "NPC responses and world events" 小节：双通道、`npc_response_rules` 配置说明、私密交互限制）
- Modify: `concordia/contrib/components/README.md`（`story_action_adjudication.py`/`story_resolution.py`/`repetition_push.py`/`director_note.py`/`drama_director.py` 条目各补一句新语义）
- Modify: `concordia/contrib/prefabs/README.md`（story prefab 条目补 `npc_response_rules` 参数）
- Create: `docs/superpowers/notes/2026-10-20-story-enactment-world-response-and-progress.md`
- Create: `docs/superpowers/plans/2026-10-20-story-enactment-world-response-and-progress.md` 各任务勾选与 RED/GREEN 补记（即本文件）

**Interfaces:**
- Consumes: Task 1 基线快照与 Task 2–10 的 RED/GREEN 记录（`/tmp/opencode/world-response-baseline/progress.md`）。
- Produces: 交付记录（改动文件清单、RED/GREEN 证据、全量检查退出码、run_results 只读核对、勘误与限制披露、真实模型复验状态=未执行）。

- [x] **Step 1: 更新 example README**

在 `## Action Modes` 节末尾（"never retroactively authorize actions..." 段后）新增小节（要点，中英与该 README 现有风格一致）：

```markdown
### NPC responses and world events

Two separate channels govern figures beyond the players. The director
may initiate NEW external events (an unannounced caller, a courier, a
knock, a blackout) only from the story's `allowed_environment_events`
whitelist, at most one intervention path per turn. Separately, an NPC
that already entered the story through a legal committed event may
answer the current speaker's explicit interaction publicly and
directly, as a direct consequence of the current action, generated
within the same single core adjudication call — but only within the
story's `npc_response_rules` configuration (GM-only content
authorization recorded in the run config; empty by default keeps the
old boundary, and old configs resume with empty rules). Configured NPC
responses grant no GM-only knowledge, no evidence, no certification,
and never promise anything on a player's behalf. A grounded refusal or
limited response is allowed only under an applicable rule authorizing
that NPC's public response to the current interaction; the rule need
not prescribe its exact words. Empty rules, no applicable rule, or an
unmet trigger authorize no new NPC response through the action channel.

Known limitation: this pipeline has no per-recipient content. All
resolved events are public to every scene participant, so a private
phone call or whisper cannot receive its far-side reply here (design
section 5.3 and the section-11 gate). Private NPC conversations need a
separate audience-design before they can count as supported; the
public-neighbor acceptance below must not be reported as closing that
gap.

Unsupported private interaction is an engineering limitation, not an
in-story fact that the other side is silent, refuses, disconnects, or
fails identity verification. Do not invent those outcomes or force a
player to hang up or enable speakerphone to bypass the limitation.
```

- [x] **Step 2: 更新 contrib 两个 README**

`concordia/contrib/components/README.md`：
- `story_action_adjudication.py` 条目补：`AGENCY_AND_EVIDENCE_RULES` 现含证据来源/访问/认证区别（设计 §6），新常量 `PUBLIC_NPC_RESPONSE_SEMANTICS` 与可选 `npc_response_rules` 参数把已登场 NPC 的公开直接回应限制在配置范围内（同一次核心裁决，不新增调用）。
- `story_resolution.py` 条目补：规则 5 区分导演新事件（白名单）与已登场 NPC 配置内公开回应（动作后果，非导演干预）；生成与评审共享同一份 NPC 语义与实例规则数据。
- `repetition_push.py` 条目补：判断规则区分有效进展与无效重复（设计 §7.1）。
- `director_note.py`、`drama_director.py` 条目补：建议/指令聚焦已有历史的具体可行动阻塞，不为新颖堆叠打断与核验任务（设计 §7.2）。

`concordia/contrib/prefabs/README.md`：story GM prefab 条目补 `npc_response_rules` 参数（构建期校验非空字符串序列，缺省空维持旧边界）。

- [x] **Step 3: 写交付 notes**

`docs/superpowers/notes/2026-10-20-story-enactment-world-response-and-progress.md`，结构沿用 `2026-10-19-story-enactment-chain-summary-closeout.md`：

1. 头部：日期、设计/计划链接、执行的任务范围、未执行 git commit/stash/clean/checkout（与开工基线 `git status --porcelain` 逐行一致，除本计划新增/修改文件）、新增真实模型调用 0。
2. 改动文件清单（对照 `/tmp/opencode/world-response-baseline/files/` 前后快照，列实际差异）。
3. RED/GREEN 证据：每任务的关键先失败后通过命令与输出摘录。
4. 全量检查表（Step 4 的退出码逐条）。
5. 旧报告勘误披露（设计 §1 三项原文语义，注明只追加新 notes、不覆写旧 README/protocol/batch 产物）。
6. 尚未支持的私密交互与阶段 C gate 状态（设计 §5.3/§11）；联合行动授权同样未新增（设计 §4 末段）。
7. 验收结论分列：工程链路通过（本计划范围）；固定语义样本（阶段 B）与完整故事质量（阶段 B 后半）未执行、未宣称。
8. Self-Review 记录（下节内容誊入）。

- [x] **Step 4: 全量检查（每条独立运行、单独捕获退出码，不用 `&&` 串连）**

```bash
source venv/bin/activate
pytest concordia -q
pytest examples -q
pylint --errors-only -j 1 concordia
pyrefly check concordia
pyink --check <本计划触碰的全部 py 文件，逐一列出>
isort --check-only <同上>
git diff --check
```

每条命令将完整输出和真实退出码分别保存到本次收尾日志；仿照 Task 1 Step 5 用命令结束后立即取得的 `$?`，不得用管道最后一项的退出码。Expected: pytest 两范围 PASS；pylint 无新增错误；pyrefly 与 Task 1 本轮实测日志相比**无新增**错误，既有诊断按文件/位置/原因逐项对照，不把历史“4 项”写成固定基线。若某项非零但可归因于既有问题，保留非零退出码与对照证据，不能标成 GREEN。pyink/isort 通过；`git diff --check` 无输出。任何新增失败先修复再重跑，不带着新增失败收尾。

- [x] **Step 5: run_results 只读核对**

```bash
find run_results -type f -print0 | sort -z | xargs -0 sha256sum \
  > /tmp/opencode/world-response-baseline/run_results-hashes-after.txt 2>/dev/null || true
diff /tmp/opencode/world-response-baseline/run_results-hashes.txt \
     /tmp/opencode/world-response-baseline/run_results-hashes-after.txt
```

Expected: 无差异（只读保留）。若 Task 1 未建立基线（目录不存在），在 notes 中明确记录该限制，不补造证明。

- [x] **Step 6: 勾选本计划任务并输出交付摘要**

把本文件各任务 `- [ ]` 勾选为 `- [x]`，向用户输出摘要：改动范围、RED/GREEN 与全量检查结果、勘误与限制披露、阶段 B/C 待授权状态。不启动任何真实模型运行。

---

## Self-Review 记录

1. **Spec coverage**（设计章节 → 任务）：§4（A1）→ Task 7；§5.1 三种权限 → Task 4 常量 + Task 5 规则 5/8；§5.2 配置/prefab/接线/歧义消除 → Task 2/3/5/6；§5.3 私密限制 → Task 4 常量末句 + Task 10 非法候选 + Task 11 README/notes 披露（不修引擎，阶段 C 另立）；§6（A3）→ Task 4 常量扩展 + Task 5 正反夹具与拒绝测试；§7.1 → Task 8；§7.2 → Task 8 的 repetition 建议/融合上下文 + Task 9 其余两路 + Task 5 规则 8 句（非导演干预）+ Task 10 `test_npc_response_is_not_a_director_intervention`；§8 不变契约 → Global Constraints + 各任务回归步骤；§9 文件范围 → Tasks 1–11 逐一对应（基线夹具→1，A1→7，A2→2/3/4/5/6，A2/A3 生成评审→4/5，A4→8/9，集成归档→10，收尾→11）；§10.1 确定性测试 8 项 → Task 7（项 1）、Task 2/6/10（项 2）、Task 10（项 3/4/7）、Task 5（项 5）、Task 10 与既有套件（项 6）、Task 10 Step 4 全范围回归（项 8）；§10.2/§10.3（阶段 B）与 §11（阶段 C）明确不在本计划（Global Constraint 9/10 + Task 11 结论分列）。设计 §12 执行次序（基线→共享规则与 NPC 接线→角色指引→导演上下文→确定性验收）与任务顺序一致。
2. **Placeholder scan**：Task 5/10 的 `...` 均为"按该文件既有 helper 补全构建代码"的显式指令，且断言目标、夹具文本常量、期望行为均已给出——构建方式依赖目标文件内部命名，执行者须先读目标测试文件的既有构建模式再补全；生产代码步骤无占位。无 "TBD"/"类似 Task N" 转写。
3. **Type consistency**：`npc_response_rules: Sequence[str] = ()` 在 `make_action_adjudication_step`、`make_final_output_step`、prefab 传参三处一致；`StoryConfig.npc_response_rules: tuple[str, ...]`、`to_dict()['npc_response_rules']`（list）、`from_dict` 缺省 `()`、`gm_params['npc_response_rules']`（list）、prefab `params['npc_response_rules']`（默认 `()`）链路类型一致。`from_dict` 必须在 `tuple()` 前校验原始 list/tuple 及非空字符串元素，直接构造 dataclass 和直接构建 prefab 的入口也分别校验，避免裸字符串/字典被转换后绕过检查。`PUBLIC_NPC_RESPONSE_SEMANTICS` 命名在 Task 4/5/6 测试引用一致；A1 文案保留 stub 路由指纹（Task 7 硬约束节）。
4. **Authorization consistency**：Task 3 世界限制、Task 4 共享语义、Task 5 规则 5/8、Task 11 README 一致要求先有适用的 NPC 回应授权。有限回应/拒绝不能充当空规则或未满足触发条件的兜底豁免；私密通道未支持不被改写成对方沉默/拒绝/挂断/核验失败。
5. **Fixture validity**：Task 10 第 2 回合通过真实 repetition 干预和正式提交建立合法敲门历史，第 3 回合才回应；裁决历史数据区必须含该已提交事件，不能仅检查白名单或规则文字。空规则调用数对照只生成当前玩家自己的告知结果。失败注入只在目标轮发生，既有合法提交保留。规则数据断言使用实际 Markdown 数据区与 JSON 内容，不依赖不存在的字段名指纹。
6. **Evidence baseline**：Task 1 先快照 20 个计划触碰文件（包括 isolation test）并采集本轮 pytest/lint/type 输出与真实退出码；Task 11 按实测诊断对照，不引用历史错误数作为当前基线。后续新增触碰范围须先补修改前快照。
