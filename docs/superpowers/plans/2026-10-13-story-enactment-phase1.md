# 故事演绎 Phase 1 实施计划

修订日期：2026-10-13；版本：v2（完成 v3 设计对照修订）。
2026-10-15 验收修订（F1/F2/F5）：`make_run_config` 显式接收节拍序列
（Task 10）；关系观察识别改为发送端边界编码 + 接收端解码（Task 14、
附录 D-4）；补充 API 重试次数的独立记录（Task 10 汇总字段）。
2026-10-15 导出修订：用户确认 FDX 保真导出；新增 render_fdx 与
script.fdx，Fountain 保留为兼容视图（Task 8 / Task 10）。

执行方式：按任务依赖逐项实现，使用 checkbox 跟踪；实施时遵循仓库
AGENTS.md。本文不依赖特定插件或子代理技能，工程师可直接执行。

**Goal:** 实现设计文档 v3 定义的单场景群像短剧自动演绎 MVP：DramaDirector 剧情控制、双轨裁决输出、剧本体渲染、预算防护与 checkpoint 恢复。

**Architecture:** 全部代码进 `concordia/contrib/`（组件/prefab/语言模型包装器）与 `examples/story_enactment/`（配置/采集/渲染/入口）。不修改 Concordia 核心代码。单一 DramaDirector GM 组件由已裁决事件触发内部固定顺序流水线；示例层统一 StoryTurn+checkpoint 保存边界。

**Tech Stack:** Python 3.12、Concordia 组件体系（ContextComponent/ActionSpecIgnored/EventResolution 思维链）、absl、numpy、matplotlib（仅张力曲线）、pytest。

**Spec:** `docs/superpowers/specs/2026-10-13-story-enactment-phase1-design.md`（v3 基线；执行时以 spec 契约为准，本计划是其任务化展开）

## Global Constraints

- **环境**：`source venv/bin/activate` 后开发；验收命令 `pytest concordia && pytest examples && pylint --errors-only concordia && pyrefly check concordia`。
- **许可证头**：每个新 .py 文件首部使用 emotional_stance.py 中的完整 Apache 2.0 头，包含最后一行 limitations under the License，不按不完整的行号范围截取。
- **风格**：pyink（80 列、2 空格缩进）、isort（Google profile）、Google 风格 docstring。
- **测试**：CI 无 API key——一切 LLM 依赖用 `concordia.testing.mock_model.MockModel` 或确定性 stub。固定 embedder 使用 `lambda text: np.ones(16, dtype=np.float32)`；选择题 stub 按提示中选项的语义返回正确的字母选项，不固定选第一项。LLM 完成判定由 `sample_choice` 驱动，不能靠 `sample_text='Yes'` 控制。
- **不修改核心**：Python 源码只新增于 concordia/contrib 与 examples/story_enactment；不修改已有核心源码。README 按 AGENTS.md 更新，允许修改已有 contrib 文档（该文档范围说明见附录 D）。
- **提交隔离**：保留工作区已有修改与暂存状态，不执行全局 restore/reset，不要求用户先清理暂存区。每个任务只 `git add --` 明确的文件路径，再 `git commit --only -m MESSAGE --` 同一组路径；禁止无路径的普通 commit、`git add -A` 和目录级 `git add -u`。已有同路径改动必须先检查，不能把他人的修改纳入任务提交。下文提交命令仅在该任务实现、测试通过后执行。
- **依赖方向**：contrib 不能导入 examples。共享场景编解码放在 `concordia/contrib/prefabs/game_master/story_scene.py`，示例和 prefab 共同调用。
- **关键源码事实**（任务直接依赖，已核实）：
  - 引擎 resolve（sequential.py:148-170）：`gm.observe(f'[putative_event] {action}')` → `gm.act(RESOLVE)` → `gm.observe(f'[event] {result}')`；premise 也以 `[event]` 注入（:244-246）。
  - 引擎把角色动作规范化为 `f'{name}: {raw}'`（sequential.py:317-322）。
  - EventResolution（event_resolution.py:40-247）：在 `pre_act(RESOLVE)` 内运行思维链 `run_chain_of_thought(thoughts, premise, document, active_player_name)`；步骤签名 `(InteractiveDocument, str, str) -> str`；返回 `f'Event: {event_statement}\n'`；`components=` 参数的各组件 `pre_act` 值拼接进裁决提示；`notify_observers=False`（用 SendEventToRelevantPlayers 广播）。
  - 组件钩子（entity_component.py:230-315）：`pre_act(action_spec)->str`、`post_act(action_attempt)->str`、`pre_observe(observation)->str`、`post_observe()->str`（无参）、`update()->None`；update 在每次 act/observe 后并行执行（entity_agent.py:174,204）。
  - ActionSpecIgnored.update() 清空 `_pre_act_value`（action_spec_ignored.py:85-87）。
  - Simulation（prefabs/simulation/generic.py:45-116,235-347）：`__init__(config, model, embedder, engine=Sequential(), override_agent_model=None, override_game_master_model=None)`；`play(premise=None→default_premise, max_steps=None→default_max_steps, ..., checkpoint_path=None, step_callback=None)`；`make_checkpoint_data()->dict`；`load_from_checkpoint(dict)`。
  - `Config(prefabs: Mapping[str, Prefab], instances: Sequence[InstanceConfig], default_premise='', default_max_steps=100)`；`InstanceConfig(prefab: str, role: Role, params: Mapping[str, Any])`。
  - MockModel（testing/mock_model.py）：`sample_text` 恒返回构造响应；`sample_choice` 恒返回 `(0, responses[0], {})`。
  - RetryLanguageModel（language_model/retry_wrapper.py:27-38）：`__init__(model, *, retry_on_exceptions=(Exception,), retry_tries=3, retry_delay=2.0, ...)`。
  - ActionSpecIgnored 会检查实体 phase。直接驱动该类时必须设置合法 phase；生命周期测试优先使用真实 EntityAgentWithLogging，不能照搬不含 phase 检查的 MagicMock 夹具。
  - SceneTracker 在场景结束的 TERMINATE 查询中调用 `terminator.terminate()`；自定义终止组件必须保留该方法。
  - `DEFAULT_CALL_TO_SPEECH` 要求输出角色标签，不适用于本设计的“仅对白正文”。使用 Task 1 的专用 action spec。
- **组件键常量**：使用各既有模块导出的默认键，不另写猜测的字符串。新增 `DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY = '__drama_director__'`、`EMOTIONAL_STANCE_COMPONENT_KEY = 'EmotionalStance'`、`RELATIONSHIPS_COMPONENT_KEY = 'RelationshipsToOthers'`；后两者在 story_character prefab 定义，collector 按同一常量读取。

## 契约速览（所有任务共用）

### DramaDirector 状态转换（spec §4.2/§12 交付）

| 状态字段 | 含义 | 进入 | 退出 |
|---|---|---|---|
| `_turn_context` | `{actor, original_speech, beat_id_at_action}` | `pre_observe` 收到 `[putative_event]` 且能取到 active player | `update()` 提交后清空 |
| `_validated` | `{resolved_event, stage_directions, directive_integrating}` | `story_resolution` 最终步骤验证成功时写入 | `update()` 提交后清空 |
| `_awaiting_event` + `_awaited_act_result` | 等待引擎回放 `[event]` | `post_act(RESOLVE)` 且 `_validated` 非空 | `pre_observe` 匹配成功（必须跨中间 `update()` 存活） |
| `_snapshot` | 匹配到的已裁决事件及窗口快照 | `pre_observe`：`observation == f'[event] {_awaited_act_result}'` | `update()` 提交后清空 |
| `_pending_commit` | 流水线输出的整步结果 | `post_observe()` 执行流水线 | `update()` 提交 → `_last_story_turn` |
| `_budget_stopped` | 后处理预算停止标志 | 流水线内捕获 `BudgetExhaustedError` | 终止（终局态） |

### 两条预算停止路径调用序列（spec §11.3 交付）

路径 A（裁决未完成）：
`BudgetGuard.sample_text/sample_choice`（锁内检查→抛 `BudgetExhaustedError`）→ 异常穿过组件 `pre_act`（不被 11.1 降级捕获）→ `RetryLanguageModel` 白名单不含该异常，不重试 → 引擎 → `sim.play` → `run.py` 捕获 → 记 `budget_limit` → 不写 StoryTurn/checkpoint（上一步的完整 checkpoint 仍在）。

路径 B（裁决已完成，后处理）：
`DramaDirector.post_observe()` 内 `try: 流水线 except BudgetExhaustedError: _budget_stopped=True` → 保留已算节拍结果（未完成判定的拍不推进）→ 正常返回 → `update()` 提交 → 引擎日志/`step_callback` 保存本步+checkpoint → 下一步 `SceneOrStoryTerminator.pre_act` 读 stop 标志返回 'Yes' → `run.py` 按优先级汇总。

### 接口所有权表（spec §15.1-3 交付）

| 接口 | 拥有模块 | 运行阶段 |
|---|---|---|
| `Beat` dataclass + `to_dict/from_dict` | `drama_director.py` | 构建期/恢复期 |
| `DramaDirector(model, beats, ...)` 全钩子 + `get_last_story_turn()` + `get_state/set_state` + `is_story_stopped()`/`is_beats_done()`/`beat_timeout()`（stage2：`get_pending_directive()` 等） | `drama_director.py` | 生命周期各阶段见上表 |
| `make_final_output_step(director, *, immutable_facts=())` → `(InteractiveDocument, str, str) -> str` | `story_resolution.py` | EventResolution 思维链末步 |
| `make_director_note_step(director)` → 同上签名 | `director_note.py`（stage2） | 思维链中步 |
| `StoryOutputError` | `story_resolution.py` | 校验失败（error 停止） |
| `SceneOrStoryTerminator(scene_tracker_key, drama_director_key)` + `terminate()` + `termination_reason()` + `resolve_final_reason(...)` | `story_terminator.py` | TERMINATE 查询 / run.py 汇总 |
| `StoryTurnLog`（append/trim_after/load/atomic write）与 `make_step_callback(...)` | `examples/story_enactment/collector.py` | step_callback（无 LLM） |
| `BudgetExhaustedError`、`BudgetGuardLanguageModel(model, total_attempt_limit, used_attempts=0)` + `restore(used, limit)` | `concordia/contrib/language_models/budget_wrapper.py` | 每次模型请求前 |
| `render_fdx(turns, scene_display) -> str`、`render_fountain(...)`、`render_plain_text(...)` | `examples/story_enactment/renderer.py` | 运行后（纯函数） |
| `StoryConfig`/`CharacterInfo` | `examples/story_enactment/story_config.py` | 构建期 |
| `scene_to_dict/scene_from_dict` | `concordia/contrib/prefabs/game_master/story_scene.py` | 构建期/恢复期 |

---

## Stage 0：基础件

### Task 1: 故事配置数据模型与编解码

**Files:**
- Create: `examples/story_enactment/story_config.py`
- Test: `examples/story_enactment/story_config_test.py`
- Create: `concordia/contrib/prefabs/game_master/story_scene.py`
- Test: `concordia/contrib/prefabs/game_master/story_scene_test.py`

**Interfaces (Produces):**
- `@dataclasses.dataclass CharacterInfo: name, identity, goal, hard_constraint, secret, public_facts: tuple[str,...], style, initial_relationship_notes: str`（除 public_facts 为 tuple[str,...] 外，其余为 str，secret 可为 `''`）
- `@dataclasses.dataclass StoryConfig: title, location, interior: bool, time_label, world_facts: tuple[str,...], gm_only_facts: tuple[str,...], allowed_environment_events: tuple[str,...], immutable_facts: tuple[str,...], characters: tuple[CharacterInfo,...], conflict_start, allowed_endings: tuple[str,...]`，方法 `to_dict()/from_dict(d)`
- `story_scene.scene_to_dict(scene: scene_lib.SceneSpec) -> dict` / `story_scene.scene_from_dict(d) -> scene_lib.SceneSpec`（覆盖 SceneTypeSpec 内的 ActionSpec：FREE 类型存 `call_to_action/output_type/tag`；SceneSpec 的 premise、participants、num_rounds 完整恢复）。此模块只导入 Concordia 与标准库，不能依赖 examples。
- `make_speech_action_spec()` 使用 `free_action_spec(call_to_action=...)`，提示固定为：“根据你已知的信息，{name} 现在说什么？只输出自己的对白正文；不要角色名、引号包裹、他人台词、旁白或推理过程。” 不使用 DEFAULT_CALL_TO_SPEECH。
- 校验函数 `validate(config) -> list[str]`：角色名唯一且非空；每角色至少 1 条 public/baseline 信息；`allowed_environment_events` 非空（导演约束来源）。

- [ ] **Step 1: 写失败测试**

```python
# examples/story_enactment/story_config_test.py（省略许可证头，实际文件必须有）
"""Tests for story_config data model and codecs."""
import dataclasses
import unittest

from concordia.contrib.prefabs.game_master import story_scene
from examples.story_enactment import story_config


class StoryConfigTest(unittest.TestCase):

  def test_roundtrip(self):
    config = story_config.make_example_config()
    d = config.to_dict()
    self.assertEqual(story_config.StoryConfig.from_dict(d), config)

  def test_validate_rejects_duplicate_names(self):
    config = story_config.make_example_config()
    broken = dataclasses.replace(
        config,
        characters=config.characters[:1] + config.characters[:1])
    self.assertIn('duplicate', ' '.join(story_config.validate(broken)))

  def test_scene_roundtrip(self):
    scenes = story_config.make_example_scenes(
        story_config.make_example_config(), num_rounds=24)
    for scene in scenes:
      rebuilt = story_scene.scene_from_dict(story_scene.scene_to_dict(scene))
      self.assertEqual(rebuilt.scene_type.name, scene.scene_type.name)
      self.assertEqual(rebuilt.participants, scene.participants)
      self.assertEqual(rebuilt.num_rounds, scene.num_rounds)
      self.assertEqual(rebuilt.premise, scene.premise)
      self.assertEqual(rebuilt.scene_type.action_spec, scene.scene_type.action_spec)


if __name__ == '__main__':
  unittest.main()
```

- [ ] **Step 2: 运行确认失败** — `pytest examples/story_enactment/story_config_test.py -v` → ModuleNotFoundError
- [ ] **Step 3: 实现** — 实现 dataclasses（from_dict 逐字段构造，tuple/list 互转）、共享场景编解码、`make_example_config()`（先返回最小 4 角色骨架，Task 9 填内容）、`make_example_scenes(config, *, num_rounds)`（单场景，action_spec 为上述专用正文提示；scene premise 对每角色给出公开 conflict_start，不含 GM-only 或他人秘密）。补齐专用提示与编码恢复断言；总剧情上限作为 num_rounds 参数传入，不硬编码 24。
- [ ] **Step 4: 运行测试通过**
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/story_config.py examples/story_enactment/story_config_test.py concordia/contrib/prefabs/game_master/story_scene.py concordia/contrib/prefabs/game_master/story_scene_test.py && git commit --only -m "feat(story): story config data model and codecs" -- examples/story_enactment/story_config.py examples/story_enactment/story_config_test.py concordia/contrib/prefabs/game_master/story_scene.py concordia/contrib/prefabs/game_master/story_scene_test.py`

### Task 2: BudgetGuard 语言模型包装器

**Files:**
- Create: `concordia/contrib/language_models/budget_wrapper.py`
- Test: `concordia/contrib/language_models/budget_wrapper_test.py`

**Interfaces (Produces):**
- `class BudgetExhaustedError(Exception)`
- `class BudgetGuardLanguageModel(language_model.LanguageModel)`：`__init__(self, model, *, total_attempt_limit: int, used_attempts: int = 0)`；属性 `used_attempts`、`total_attempt_limit`；`restore(used_attempts, total_attempt_limit)`；`sample_text/sample_choice` 签名与基类一致——**每次请求先在 `threading.Lock` 内检查并占用额度，超限抛 `BudgetExhaustedError`，不调用底层模型**；请求失败（底层异常）额度已消耗；`BudgetExhaustedError` 本身不消耗额度。

- [ ] **Step 1: 写失败测试**

```python
# concordia/contrib/language_models/budget_wrapper_test.py
"""Tests for BudgetGuardLanguageModel."""
import unittest
from unittest import mock

from concordia.contrib.language_models import budget_wrapper
from concordia.language_model import language_model
from concordia.testing import mock_model


def _inner():
  m = mock.MagicMock(spec=language_model.LanguageModel)
  m.sample_text.return_value = 'ok'
  m.sample_choice.return_value = (0, 'a', {})
  return m


class BudgetGuardTest(unittest.TestCase):

  def test_under_limit_passes_and_counts(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=2)
    self.assertEqual(guard.sample_text('p'), 'ok')
    self.assertEqual(guard.used_attempts, 1)

  def test_over_limit_raises_before_calling_model(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=1)
    guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p2')
    self.assertEqual(inner.sample_text.call_count, 1)
    self.assertEqual(guard.used_attempts, 1)

  def test_choice_and_text_share_budget(self):
    inner = _inner()
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=2)
    self.assertEqual(guard.sample_choice('p', ['a'])[1], 'a')
    guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_choice('p', ['a'])
    self.assertEqual(inner.sample_choice.call_count, 1)
    self.assertEqual(guard.used_attempts, 2)

  def test_failed_attempt_consumes_budget(self):
    inner = _inner()
    inner.sample_text.side_effect = RuntimeError('api down')
    guard = budget_wrapper.BudgetGuardLanguageModel(
        inner, total_attempt_limit=1)
    with self.assertRaises(RuntimeError):
      guard.sample_text('p')
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p')

  def test_thread_safe_no_oversubscribe(self):
    from concurrent import futures
    import threading
    calls = []
    lock = threading.Lock()

    class Counting(mock_model.MockModel):
      def sample_text(self, prompt, **kwargs):
        with lock:
          calls.append(1)
        return 'ok'

    guard = budget_wrapper.BudgetGuardLanguageModel(
        Counting(), total_attempt_limit=5)
    def attempt(_):
      try:
        guard.sample_text('p')
        return True
      except budget_wrapper.BudgetExhaustedError:
        return False

    with futures.ThreadPoolExecutor(max_workers=20) as executor:
      results = list(executor.map(attempt, range(20)))
    self.assertEqual(len(calls), 5)
    self.assertEqual(results.count(True), 5)
    self.assertEqual(results.count(False), 15)
    self.assertEqual(guard.used_attempts, 5)

  def test_restore(self):
    guard = budget_wrapper.BudgetGuardLanguageModel(
        _inner(), total_attempt_limit=10, used_attempts=9)
    guard.sample_text('p')  # 10th
    with self.assertRaises(budget_wrapper.BudgetExhaustedError):
      guard.sample_text('p')
    guard.restore(used_attempts=3, total_attempt_limit=10)
    self.assertEqual(guard.sample_text('p'), 'ok')


if __name__ == '__main__':
  unittest.main()
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**（实现 LanguageModel 两个抽象方法；锁内 `if self._used >= limit: raise`；`_used += 1` 后调用内层并把异常原样传播）
- [ ] **Step 4: 测试通过**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/language_models/budget_wrapper.py concordia/contrib/language_models/budget_wrapper_test.py && git commit --only -m "feat(contrib): budget guard language model wrapper" -- concordia/contrib/language_models/budget_wrapper.py concordia/contrib/language_models/budget_wrapper_test.py`

---

## Stage 1：契约先行（spec §14-1：不含张力/导演）

### Task 3: DramaDirector 核心——生命周期、story_turn、节拍推进

**Files:**
- Create: `concordia/contrib/components/game_master/drama_director.py`
- Test: `concordia/contrib/components/game_master/drama_director_test.py`

**Interfaces:**
- Consumes: `BudgetExhaustedError`（Task 2）、组件钩子、`NextActing.get_currently_active_player()`（键 `__next_acting__`）
- Produces:
  - `DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY = '__drama_director__'`
  - `@dataclasses.dataclass Beat`（`id, description, tension_range: tuple[int,int], done_question, min_turns, max_turns` + `to_dict()/from_dict()`）
  - `class DramaDirector(action_spec_ignored.ActionSpecIgnored, entity_component.ComponentWithLogging)`：构造 `__init__(self, model, beats: Sequence[Beat], *, next_acting_component_key=DEFAULT, pre_act_label='\nCurrent beat')`；公开 `get_last_story_turn() -> dict | None`、`get_current_original_speech() -> str | None`（供最终步骤校验）、`set_validated_resolution(resolved_event, stage_directions)`、`is_story_stopped() -> bool`、`is_beats_done() -> bool`、`beat_timeout() -> bool`、`get_state()/set_state()`；内部状态见“契约速览”表。构建时校验 beat id 唯一、1 <= lower <= upper <= 10、0 <= min_turns <= max_turns 且 max_turns > 0；配置不合法直接报错，不调用模型。

- [ ] **Step 1: 写失败测试**（使用真实 EntityAgentWithLogging 生命周期；完成判定用语义化选择 stub）

```python
# concordia/contrib/components/game_master/drama_director_test.py
"""Tests using the actual entity component lifecycle."""
import dataclasses
import re
import unittest

from concordia.agents import entity_agent_with_logging
from concordia.components.game_master import next_acting as next_acting_lib
from concordia.contrib.components.game_master import drama_director
from concordia.testing import mock_model
from concordia.typing import entity as entity_lib
from concordia.typing import entity_component

_RESOLVE_SPEC = entity_lib.ActionSpec(
    call_to_action='resolve', output_type=entity_lib.OutputType.RESOLVE)
_TERMINATE_SPEC = entity_lib.ActionSpec(
    call_to_action='terminate', output_type=entity_lib.OutputType.TERMINATE)

BEATS = (
    drama_director.Beat(
        id='b1', description='setup', tension_range=(1, 3),
        done_question='Is the evidence revealed?', min_turns=1, max_turns=5),
    drama_director.Beat(
        id='b2', description='resolution', tension_range=(1, 3),
        done_question='Is the fallout addressed?', min_turns=1, max_turns=5),
)


class _BeatModel(mock_model.MockModel):
  """Selects Yes or No by semantic option text, despite shuffled letters."""

  def __init__(self, completed):
    super().__init__('unused')
    self.completed = completed
    self.choice_calls = 0

  def sample_choice(self, prompt, responses, *, seed=None):
    self.choice_calls += 1
    choices = dict(re.findall(
        r'^  \(([^)]+)\) (Yes|No)$', prompt, re.MULTILINE))
    wanted = 'Yes' if self.completed else 'No'
    label = next(key for key, value in choices.items() if value == wanted)
    return responses.index(label), label, {}


class _FixedActor(entity_component.ContextComponent):
  """Supplies the active player without calling a model."""

  def get_currently_active_player(self):
    return 'Alice'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


class _ResolutionAct(entity_component.ActingComponent):
  """Replaces only resolution generation; lifecycle remains real."""

  def __init__(self, director):
    self._director = director

  def get_action_attempt(self, contexts, action_spec):
    if action_spec.output_type != entity_lib.OutputType.RESOLVE:
      return 'No'
    speech = self._director.get_current_original_speech()
    event = f'Alice: {speech}'
    self._director.set_validated_resolution(event, '')
    return f'Event: {event}\n'

  def get_state(self):
    return {}

  def set_state(self, state):
    del state


def _make_director(model, beats=BEATS):
  director = drama_director.DramaDirector(model=model, beats=beats)
  gm = entity_agent_with_logging.EntityAgentWithLogging(
      agent_name='story rules', act_component=_ResolutionAct(director),
      context_components={
          '__drama_director__': director,
          next_acting_lib.DEFAULT_NEXT_ACTING_COMPONENT_KEY: _FixedActor(),
      })
  return director, gm


def _complete_turn(gm, speech):
  gm.observe(f'[putative_event] Alice: {speech}')
  result = gm.act(_RESOLVE_SPEC)
  gm.observe(f'[event] {result}')
  return result


class LifecycleTest(unittest.TestCase):

  def test_premise_event_not_counted(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    gm.observe('[event] the initial premise')
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_management_queries_do_not_evaluate(self):
    model = _BeatModel(False)
    director, gm = _make_director(model)
    for _ in range(3):
      gm.act(_TERMINATE_SPEC)
    self.assertIsNone(director.get_last_story_turn())
    self.assertEqual(model.choice_calls, 0)

  def test_full_turn_commits_once(self):
    model = _BeatModel(True)
    director, gm = _make_director(model)
    gm.observe('[putative_event] Alice: I hid the letter.')
    # observe 已完成 post_observe/update；原始对白仍须保留。
    self.assertEqual(
        director.get_current_original_speech(), 'I hid the letter.')
    self.assertEqual(model.choice_calls, 0)
    result = gm.act(_RESOLVE_SPEC)
    # act 已完成 update；等待匹配事件的状态仍须保留。
    self.assertIsNone(director.get_last_story_turn())
    gm.observe(f'[event] {result}')
    turn = director.get_last_story_turn()
    self.assertEqual(turn['turn_id'], 1)
    self.assertEqual(turn['actor'], 'Alice')
    self.assertEqual(turn['speech'], 'I hid the letter.')
    self.assertEqual(turn['beat_id_at_action'], 'b1')
    self.assertEqual(turn['beat_id_after_event'], 'b2')
    gm.observe(f'[event] {result}')
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)
    self.assertEqual(model.choice_calls, 1)

  def test_min_turns_blocks_advancement(self):
    model = _BeatModel(True)
    director, gm = _make_director(
        model, beats=(dataclasses.replace(BEATS[0], min_turns=2), BEATS[1]))
    _complete_turn(gm, 'one')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b1')
    self.assertEqual(model.choice_calls, 0)
    _complete_turn(gm, 'two')
    self.assertEqual(
        director.get_last_story_turn()['beat_id_after_event'], 'b2')

  def test_max_turns_marks_timeout_without_advancing(self):
    beat = dataclasses.replace(BEATS[0], max_turns=1)
    director, gm = _make_director(_BeatModel(False), beats=(beat,))
    _complete_turn(gm, 'one')
    self.assertTrue(director.beat_timeout())
    self.assertFalse(director.is_beats_done())
    self.assertEqual(director.get_last_story_turn()['turn_id'], 1)

  def test_state_roundtrip(self):
    director, gm = _make_director(_BeatModel(False))
    _complete_turn(gm, 'hi')
    state = director.get_state()
    fresh, _ = _make_director(_BeatModel(False))
    fresh.set_state(state)
    self.assertEqual(fresh.get_state(), state)


if __name__ == '__main__':
  unittest.main()
```

- [ ] **Step 2: 运行确认失败**
- [ ] **Step 3: 实现**（关键实现约束）：
  - `pre_act`：记录 `_last_output_type = action_spec.output_type`；返回 `super().pre_act(action_spec)`（ActionSpecIgnored 产生 `f'{label}:\n{value}\n'`；`_make_pre_act_value()` 返回当前节拍描述文本，纯字符串拼接零 LLM）
  - `pre_observe(observation)`：
    - 若以 `[putative_event] ` 开头：取 active player，剥离一次 `f'{actor}: '` 前缀得 `original_speech`，存 `_turn_context = {actor, original_speech, beat_id_at_action: 当前节拍 id}`
    - 若 `_awaiting_event` 且 `observation == f'[event] {_awaited_act_result}'`：`_snapshot = dict(_turn_context)` + `resolved_event=_validated['resolved_event']` 等；`_awaiting_event = False`
    - 返回 `''`
  - `post_act(action_attempt)`：若 `_last_output_type == RESOLVE` 且 `_validated` 非空 → `_awaiting_event=True; _awaited_act_result=action_attempt`；返回 `''`
  - `post_observe()`：`_snapshot` 为空立即返回；否则在本步计算结果中增加 story_turn 与本拍步数，并将完整事件加入本地窗口。只有达到 min_turns 才调用 `yes_no_question`（附录 B-P1）；完成则最多推进一拍并重置本拍计数，否则达到 max_turns 标记超时。组装 `_pending_commit`，同时持有更新后的组件状态与 StoryTurn（Stage 1 的 tension=None）；不提前覆盖已提交状态。
  - 预算路径 B：专门捕获 `BudgetExhaustedError`，设置待提交的 budget_stopped 标志，结束全部后续模型调用。完成判定尚未返回时不推进节拍，但仍按本拍已消耗步数检查超时；保留此前已成功计算的结果。正常返回，使 update 和 step_callback 保存本步一次。
  - `update()`：有 `_pending_commit` 时同时提交计数、节拍、窗口、停止标志与 `_last_story_turn`，清理 `_turn_context/_validated/_snapshot/_pending_commit/_awaited_act_result`。没有待提交结果时，不清除 putative 上下文、已验证裁决或等待标志。**始终** `super().update()` 清父类缓存。
  - `get_state/set_state`：保存 `beats`（to_dict 列表）、`beat_index`、`turns_in_beat`、`story_turn`、`recent_events` 列表、`beat_timeout`、`budget_stopped`、`last_story_turn`（spec §12 字段清单）
  - `yes_no_question` 通过 InteractiveDocument 调用。只针对明确的模型选择无效错误（如 `language_model.InvalidResponseError`）记录“未完成”；不捕获 Exception、API 异常或 tenacity 重试耗尽错误。后者传播至 run.py，记 error。补测试：API 错误不会变成未完成；判定预算耗尽且本拍达到 max_turns 时，同时保留 budget 诊断与 beat_timeout。
- [ ] **Step 4: 测试通过；`pylint --errors-only` 该文件**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py && git commit --only -m "feat(contrib): DramaDirector lifecycle and beat advancement" -- concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py`

### Task 4: 最终双轨输出步骤（story_resolution.py）

**Files:**
- Create: `concordia/contrib/components/game_master/story_resolution.py`
- Test: `concordia/contrib/components/game_master/story_resolution_test.py`

**Interfaces:**
- Consumes: `DramaDirector.get_current_original_speech()/set_validated_resolution()`（Task 3）
- Produces:
  - `class StoryOutputError(Exception)`
  - `make_final_output_step(director, *, immutable_facts: Sequence[str] = ()) -> Callable[[InteractiveDocument, str, str], str]`

- [ ] **Step 1: 写失败测试**

```python
# concordia/contrib/components/game_master/story_resolution_test.py
"""Tests for the final dual-track output step."""
import json
import unittest
from unittest import mock

from concordia.contrib.components.game_master import drama_director
from concordia.contrib.components.game_master import story_resolution
from concordia.document import interactive_document
from concordia.testing import mock_model

class FinalOutputStepTest(unittest.TestCase):

  def _run(self, response: str, speech: str | None):
    director = mock.MagicMock()
    director.get_current_original_speech.return_value = speech
    model = mock_model.MockModel(response)
    chain = interactive_document.InteractiveDocument(model)
    step = story_resolution.make_final_output_step(director)
    result = step(
        chain, 'Alice: I hid the letter under the stairs.', 'Alice')
    return result, director

  def test_returns_string_and_stores_dual_track(self):
    payload = json.dumps({
        'resolved_event': 'Alice: "I hid the letter under the stairs." '
                          'Thunder rattles the windows.',
        'stage_directions': 'Thunder rattles the windows.'})
    result, director = self._run(
        payload, speech='I hid the letter under the stairs.')
    self.assertEqual(result,
                     'Alice: "I hid the letter under the stairs." '
                     'Thunder rattles the windows.')
    director.set_validated_resolution.assert_called_once_with(
        'Alice: "I hid the letter under the stairs." Thunder rattles the windows.',
        'Thunder rattles the windows.')

  def test_invalid_json_raises_story_output_error(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run('not json at all', speech=None)

  def test_missing_fields_raise(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(json.dumps({'resolved_event': 'x'}), speech=None)

  def test_rewritten_dialogue_raises(self):
    with self.assertRaises(story_resolution.StoryOutputError):
      self._run(
          json.dumps({
              'resolved_event': 'Alice claims she burned the letter.',
              'stage_directions': ''}),
          speech='I hid the letter under the stairs.')


if __name__ == '__main__':
  unittest.main()
```

（MockModel 恒返回同一响应，提示内容不参与该单测路径——提示文本正确性由 Task 6 prefab 测试与 Task 10 集成测试覆盖。）

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**：步骤函数将 candidate_event、active_player_name 和 director 持有的原始对白填入附录 B-P5，使用 `chain.open_question(..., max_tokens=2000, terminators=())`。`json.loads` 后要求 dict 的键集合恰为 `{'resolved_event', 'stage_directions'}`，两值均为 str；完整事件必须含当前角色与原始对白。仅捕获 JSON 解码/内容验证错误并转换为 StoryOutputError，不捕获 API 或预算异常。成功后存入 director 并仅返回 resolved_event 字符串。补错误类型、额外字段、对白改写测试；两轨的语义约束由提示和场景验收检查，不能宣称仅靠子串检查已证明角色能动性。
- [ ] **Step 4: 测试通过 + pylint**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/game_master/story_resolution.py concordia/contrib/components/game_master/story_resolution_test.py && git commit --only -m "feat(contrib): dual-track final output resolution step" -- concordia/contrib/components/game_master/story_resolution.py concordia/contrib/components/game_master/story_resolution_test.py`

### Task 5: 终止组件与原因（story_terminator.py）

**Files:**
- Create: `concordia/contrib/components/game_master/story_terminator.py`
- Test: `concordia/contrib/components/game_master/story_terminator_test.py`

**Interfaces (Produces):**
- `class SceneOrStoryTerminator(entity_component.ContextComponent, entity_component.ComponentWithLogging)`：`__init__(scene_tracker_component_key, drama_director_component_key)`；必须实现 `terminate() -> None`，将 `_terminate_now=True`；pre_act(TERMINATE) 在该标志、drama 停止、beats 完成、beat 超时或 scene_tracker.is_done() 任一为真时返回 'Yes'；其他 spec 返回 'No'。get_state/set_state 保存/恢复 terminate_now。
- `termination_reason() -> str`：组件内优先级 `beats_completed > beat_timeout > scene_limit > budget_limit`
- `resolve_final_reason(exception_reason: str | None, component_reason: str | None, committed_turn_id: int, total_turn_limit: int) -> str`：收集异常原因、组件原因以及全局剧情步达到上限的 step_limit，按 `error > beats_completed > beat_timeout > scene_limit > step_limit > budget_limit` 取最高优先级；没有候选原因时记 error。不能用恢复后重置的引擎步号判断全局上限。

- [ ] **Step 1: 写失败测试** — 覆盖单条件与逐对优先级、terminate() 状态 round-trip、其他 spec 返回 No；用真实 SceneTracker 跑完场景预算，确认其 terminate() 调用成功且原因是 scene_limit。最后一拍完成同时达到场景上限记 beats_completed。
- [ ] **Step 2: 确认失败** → **Step 3: 实现** → **Step 4: 通过 + pylint**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/game_master/story_terminator.py concordia/contrib/components/game_master/story_terminator_test.py && git commit --only -m "feat(contrib): story terminator with reason priority" -- concordia/contrib/components/game_master/story_terminator.py concordia/contrib/components/game_master/story_terminator_test.py`

### Task 6: 两个 prefab（GM + 角色实体）

**Files:**
- Create: `concordia/contrib/prefabs/game_master/story_dramaturgic.py`
- Create: `concordia/contrib/prefabs/entity/story_character.py`
- Test: `concordia/contrib/prefabs/game_master/story_prefabs_test.py`（模式仿 `concordia/prefabs/game_master/game_master_prefabs_test.py:29-140`）

**Interfaces:**
- Consumes: Task 3/4/5 组件、`basic` prefab 组件布局（basic.py 全文已核对）、`EmotionalStance`（stage 3 接线，本任务预留 params）
- Produces:
  - GM prefab story_dramaturgic.GameMaster（dataclass Prefab；supports_extra_components=True）；params 为 name、scene_dicts、beat_dicts、world_constraints、immutable_facts 与既有 extra_components/extra_components_index。build(model, memory_bank) 按以下接线组装：
    - SceneTracker（scenes 由 contrib 的 `story_scene.scene_from_dict` 解码）
    - DramaDirector（beats 由 `Beat.from_dict` 解码）——**组件键** `DEFAULT_DRAMA_DIRECTOR_COMPONENT_KEY`，并加入 `EventResolution(components=['display_events', '__drama_director__'])` 的依赖
    - `Constant` 组件 `world_constraints`（键 `'world_constraints'`，`pre_act_label='\nWorld constraints'`）同样列入 EventResolution components
    - EventResolution：Stage 1 步骤为 `(remove_prefix, make_final_output_step(director, immutable_facts=...))`；Stage 2 的 director_note 放在最终输出之前。remove_prefix 只去除 Putative event 提示前缀，首版无非言语意图；实际事实与舞台事件在最终模型步骤处理，仍受 world_constraints 和 immutable_facts 约束。
    - 终止组件换 `SceneOrStoryTerminator(scene_tracker_key, drama_key)`
    - 其余组件沿用 dialogic_and_dramaturgic 组装方式；MakeObservation 设置 external_queue=None、allow_llm_fallback=False，只返回明确入队的信息；SendEventToRelevantPlayers 保留 scene_tracker.get_participants 过滤器。普通场景 premise 仅含公开内容，GM-only 背景不进入广播队列。
  - 实体 prefab `story_character.Entity`：在 build() 内每次创建新的 Constant/情绪/关系组件，构造只在本次 build 使用的 extra_components 映射，再委托 `basic.Entity(params=basic_params).build(model, memory_bank)`；复用三问链，不复制 basic.py。story_character 自身 params 和 run_config 只保存 JSON 安全值，运行时组件映射不进入持久化配置。
    - params 增 identity/hard_constraint/secret/public_facts/style/initial_relationship_notes。CharacterBrief 由该角色资料构造，放在 Instructions 之后；secret 非空时另建 PrivateSecret，仅属于该角色。保存 basic.Entity 的原有 goal 与记忆构建约定。
    - `prefix_entity_name=False` 默认
    - Stage 3 params 预留（本任务忽略）：`emotion_options: Sequence[str] = ()`、`relationships_refresh_interval: int = 0`

- [ ] **Step 1: 写失败测试** — 使用 MockModel 和固定 embedder 的 AssociativeMemoryBank（角色 basic 不使用普通 list）。断言返回 EntityAgentWithLogging、GM 组件类型与裁决依赖、角色自动前缀关闭、支持扩展。同一 prefab 构建两次时扩展组件不是同一实例；通过真实 GM act 捕获裁决提示，断言含当前节拍与世界约束。补安装包检查：构建 wheel，在仓库之外的临时环境仅安装 wheel 与依赖后导入两个 prefab、调用 catalog discovery，确认无 examples 导入依赖。
- [ ] **Step 2-4: 红→实现→绿+pylint**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/prefabs/game_master/story_dramaturgic.py concordia/contrib/prefabs/entity/story_character.py concordia/contrib/prefabs/game_master/story_prefabs_test.py && git commit --only -m "feat(contrib): story GM and character prefabs" -- concordia/contrib/prefabs/game_master/story_dramaturgic.py concordia/contrib/prefabs/entity/story_character.py concordia/contrib/prefabs/game_master/story_prefabs_test.py`

### Task 7: StoryTurn 采集与保存边界（collector.py）

**Files:**
- Create: `examples/story_enactment/collector.py`
- Test: `examples/story_enactment/collector_test.py`

**Interfaces:**
- Consumes: `DramaDirector.get_last_story_turn()`、`BudgetGuardLanguageModel.used_attempts`、`Simulation.make_checkpoint_data()`、`StepData`（step_controller.py:29-36——**实现前先读该文件确认字段名**）
- Produces:
  - `class StoryTurnLog`：__init__(path)、append(record)、load()、trim_after(turn_id)、validate_contiguous()。新 turn_id 必须等于现有最大+1；同编号且内容相同的重复追加为 no-op，同编号内容不同报错。每次变更以同目录临时文件加 os.replace 原子重写 JSONL。
  - `make_step_callback(*, simulation, drama_director, budget_guard, turn_log, checkpoint_path, run_config, total_turn_limit, total_model_attempt_limit, emotion_enabled)`：先读取 get_last_story_turn()；None 时返回。校验 actor 与 StepData.acting_entity 一致、speech 等于 StepData.action 剥离一次确定前缀的结果。从 `step_data.entity_logs[actor][EMOTIONAL_STANCE_COMPONENT_KEY]['Value']` 读取情绪；禁用或不存在时写 None，不能搜索带空格的名称。添加 scene_id 与显示配置关联，再保存完整 StoryTurn，最后原子保存 checkpoint envelope（字段与 spec §12 相同）。重复回调只允许相同记录且已有同编号 checkpoint 的 no-op；若 turn 文件已写而 checkpoint 尚未提交，重试仍须完成 checkpoint 提交。**回调内零 LLM 调用，StepData.step 不作全局编号**。

- [ ] **Step 1: 写失败测试** — 覆盖连续性、重复相同记录 no-op、重复不同记录报错、trim_after、原子替换失败后旧文件仍可读；校验写盘顺序、actor/speech 一致和 envelope 字段。情绪测试使用实际 EmotionalStance 发布的日志，并断言当前角色标签进入 StoryTurn；补 turn 已写但 checkpoint 未提交时重试回调的测试。
- [ ] **Step 2-4: 红→实现→绿**（pytest examples）
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/collector.py examples/story_enactment/collector_test.py && git commit --only -m "feat(story): atomic story turn collection" -- examples/story_enactment/collector.py examples/story_enactment/collector_test.py`

### Task 8: 剧本渲染器（renderer.py）

**Files:**
- Create: `examples/story_enactment/renderer.py`
- Test: `examples/story_enactment/renderer_test.py`

**Interfaces (Produces):**
- `render_fdx(turns: Sequence[dict], scene_display: dict, *, show_emotion: bool = True) -> str`：直接由 StoryTurn 生成显式类型的 FDX XML，保留原始正文及空白，不经 Fountain 转换。
- `render_fountain(turns: Sequence[dict], scene_display: dict, *, show_emotion: bool = True) -> str`
- `render_plain_text(turns, scene_display, *, show_emotion=True) -> str`
- `parse_fountain(text: str) -> list[tuple[str, str]]`（测试辅助：返回 (元素类型, 内容)，类型 ∈ `{'scene_heading','character','dialogue','parenthetical','action'}`）

- [ ] **Step 1: 写失败测试** — 固定 3 条 turns 输入（含中文角色名'林雪'、emotion、stage_directions、beat 完成步 beat_id_after_event=None）；断言 Fountain 输出：`@林雪` 强制角色行、对白行、`(勉强一笑)` 括号提示、`INT. 老宅客厅 - 夜` 场景标题；`parse_fountain` 元素类型与内容精确匹配；`show_emotion=False` 无括号提示；同一输入两次渲染输出逐字节相等
- [x] **FDX 修订验证** — 独立 minidom 解析器检查场景/角色/情绪/对白/动作的类型及内容；字面括号、反斜杠、格式标记、XML 特殊字符、空段、制表符和 CR/LF 均逐字回读。覆盖空记录、关闭情绪和确定性输出。
- [ ] **Step 2-4: 红→实现→绿**
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/renderer.py examples/story_enactment/renderer_test.py && git commit --only -m "feat(story): screenplay renderers" -- examples/story_enactment/renderer.py examples/story_enactment/renderer_test.py`

### Task 9: 示例故事内容与信息隔离/对白传播测试

**Files:**
- Modify: `examples/story_enactment/story_config.py`（替换 Task 1 的占位内容）
- Test: `examples/story_enactment/story_isolation_test.py`
- Create: `examples/story_enactment/testing_stubs.py`（本任务的传播测试夹具；Task 10 扩展）

**步骤要点：**
- [ ] **Step 1: 编写真实故事配置** — `make_example_config()` 落实附录 C 场景一（老宅遗产争执）：5 角色（林雪/陈默/周律师/王婶/陈母），每人 identity/goal/hard_constraint/secret/style；`gm_only_facts`（真相：遗嘱被调包）≥2 条；`allowed_environment_events`（雷雨/电话/邮差送信等 ≥6 条）；`immutable_facts`（老宅、亲缘关系）；林雪与陈母各持一条他人不知的 private secret
- [ ] **Step 2: 写信息隔离测试** — 构建两个角色（A 有秘密 S，B 无）及真实故事 GM；GM 另持秘密 G。驱动两步观察/行动，捕获 B 的实际行动提示，断言未揭示前不含 S/G，GM 裁决提示含 G。调用 CharacterBrief/PrivateSecret 的预动作值时使用真实 act 生命周期，不能在 READY 阶段直接访问。
- [ ] **Step 3: 写对白传播测试** — 在本测试文件或 testing_stubs.py 创建最小语义 stub（Task 10 扩展它），直接用 Task 6 prefab 与 Simulation/Config 构建，不依赖尚未创建的 run.py。A 首步说出 MARKER_SECRET_42，B 第二步的观察与实际行动提示必须包含它；确认 stage_directions 不复述它。
- [ ] **Step 4: 测试通过 + pytest examples**
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/story_config.py examples/story_enactment/story_isolation_test.py examples/story_enactment/testing_stubs.py && git commit --only -m "feat(story): story content and information isolation" -- examples/story_enactment/story_config.py examples/story_enactment/story_isolation_test.py examples/story_enactment/testing_stubs.py`

### Task 10: run.py 入口、恢复与预算路径（集成）

**Files:**
- Create: `examples/story_enactment/run.py`
- Test: `examples/story_enactment/story_enactment_test.py`
- Create or extend: `examples/story_enactment/testing_stubs.py`（Task 9 已创建时扩展）

**Interfaces:**
- Consumes: 前序组件、两个 prefab、Simulation/Config/InstanceConfig/Role、共享场景编解码与 RetryLanguageModel；本任务不得改变前序任务的生命周期契约。
- Produces:
  - `make_run_config(story, *, beats=None, total_turn_limit=24, total_model_attempt_limit=400, mode='beats_only') -> dict`：记录 schema_version、完整故事资料、scene_dicts、beat_dicts、角色与 GM 的 JSON 参数、总上限、导演参数和模型配置标识。beats 显式传入匹配该故事的节拍序列（三份验收场景的节拍工厂见 acceptance.py，末拍由 `make_final_beat(story.allowed_endings)` 依据允许结局构造）；beats=None 保留老宅默认节拍，仅匹配默认示例故事。定义 `make_example_beats()`（叙事条件式大纲）；阶段开关明确为 beats_only/tension_observe/director/character_depth。Stage 1 只启用 beats_only。汇总记录包含独立的 API 重试次数与评分解析补问次数（本进程与累计两种口径），随 checkpoint envelope 持久化 `used_api_retries`。
  - `build_simulation(*, run_config, model, embedder, used_attempts=0, retryable_exceptions=()) -> tuple[Simulation, DramaDirector, BudgetGuardLanguageModel]`：guard 使用 run_config 原始总调用上限与 used_attempts；外层 RetryLanguageModel 使用传入的瞬时 API 异常白名单，所有实体与 GM 共享这一条包装链。生产 provider 工厂返回对应的异常类型，测试使用自定义瞬时异常；不把内置 ConnectionError/TimeoutError 当作所有供应商的通用白名单。Config 只有一个故事 GM，无 initializer GM；构建本身不调用模型。
  - `run(state_dir, *, resume=False, disable_language_model=False, total_turn_limit=24, total_model_attempt_limit=400, mode='beats_only', model_config=None)`：明确返回结果记录，并生成 summary.json/script.fdx/script.fountain/script.txt。FDX 也仅使用 checkpoint 确认的记录，未提交尾部不得导出。模式由 run_config 保存，恢复时不覆盖；Stage 1 仅实现 beats_only，其余模式由后续任务启用。
  - `examples/story_enactment/testing_stubs.py` 提供 PromptStubModel（继承 MockModel，完整实现确定性 text 与语义 choice 路由），用于 CI 和 `--disable_language_model` 演示；不是 NoLanguageModel 的空文本路径。此文件不被 contrib 导入。

**首次运行顺序：**
1. 构建 run_config 和 Simulation；将 world_facts/公开背景注入每角色，仅将其 own secret 注入该角色。CharacterBrief/PrivateSecret 由 prefab 自建。GM 的私有设定放入仅供裁决读取的组件，不能放进广播队列或公开 premise。
2. 在 gm READY 状态调用 `gm.observe('[event] ' + public_premise)`，public_premise 包含 conflict_start 与公开世界背景，不含 GM-only 信息；DramaDirector 没有等待标志，故不计剧情步。SceneSpec 的初始队列只提供公开背景。
3. 原子保存空 StoryTurn 文件与 committed_turn_id=0 的初始 checkpoint envelope；快照已经包含 GM premise 和角色初始状态。
4. `sim.play(premise='', max_steps=total_turn_limit, step_callback=...)`；不设置自动 checkpoint_path/get_state_callback。只由 collector 回调保存完整剧情步和 checkpoint。

**恢复顺序：**
1. 读取 envelope，校验 schema_version、run_config、预算范围和 committed_turn_id；原始总上限以 checkpoint 为准。CLI 传入不同上限时明确拒绝，不能默默增加预算。
2. 按 run_config 创建新的 Simulation，再 `load_from_checkpoint(envelope['simulation_state'])`；BudgetGuard 从 envelope 的 used_model_attempts 恢复，并使用原始总上限。不得共享旧组件实例。
3. trim_after(committed_turn_id)，随后确认记录恰为 1..committed_turn_id；缺行或内容冲突报错。确认 director.story_turn 与 committed_turn_id 一致。
4. 独立计算剩余剧情步数与剩余模型调用数。已完成、已超时、预算停止标志置位、任一剩余预算为零时不进入 play，按原因优先级输出汇总；预算耗尽运行不能用旧 checkpoint 重置额度自动续跑。
   路径 A 的最后 checkpoint 可能早于预算耗尽；恢复前还须读取该运行的终止摘要。若摘要记录 budget_limit 或预算耗尽诊断，则拒绝自动续跑；不能只根据旧 checkpoint 的剩余额度决定继续。
5. 否则 `play(premise='', max_steps=remaining_turns, step_callback=...)`。不重新注入 premise/角色资料，不以本次 StepData.step 作为全局编号。

**错误与输出：**
- 裁决前 BudgetExhaustedError 由 run.py 捕获，记 budget_limit，不追加当前 StoryTurn、不覆盖上一完整 checkpoint；仅写终止诊断与导出已提交剧本。其他 API/StoryOutputError 记 error。
- 后处理预算停止由 DramaDirector 正常完成本步提交，run.py 不期待该位置再次抛出异常；正常返回后用全局 committed_turn_id 和原始总上限汇总原因。
- 汇总保留 reason、预算耗尽诊断、已提交 turn 数、当前进程的实际调用计数、运行耗时与每步耗时；token 不可得记 None。导出仅使用最近完整 checkpoint 所确认的记录，不能渲染未提交的尾部。
- CLI 命令：`python -m examples.story_enactment.run --state_dir /tmp/story-demo --disable_language_model --mode beats_only`；恢复增加 `--resume` 并保留配置。各阶段按 mode 选择已实现模式。live model 使用仓库既有 provider 工厂；本计划不新增 provider 客户端。

**PromptStubModel 路由：**
- sample_text 根据最后一个 Question/Exercise 区块确定任务，不仅根据整份 prompt 中出现某个关键词。最终双轨 JSON 路由优先于通用推理回答；从最终提示的 Current speaker/Original speech 提取对白，原样写入 resolved_event。
- sample_choice 从提示中末尾的 `(letter) option text` 映射选择所需语义答案，再返回对应 responses 的索引与字母。完成/未完成由测试脚本明确给定；不固定 `(0, responses[0], {})`。
- 原始 sample_text 返回 Yes 不能控制 yes_no_question。任务状态与角色台词由测试配置控制，不把所有节拍默认判定为完成，也不依赖线程调用次序。

- [ ] **Step 1: 写失败集成测试**（使用 PromptStubModel 与固定 embedder）：
  1. **端到端短跑**：专用 4 步 fixture 设置 beat 的 min_turns/完成证据使第 4 步才完成，原始总上限=4；断言 4 条连续记录、committed_turn_id=4、reason=beats_completed、Fountain 角色元素合法。另测未完成 fixture 的 step_limit/scene_limit，不能只断言 reason 属于任意枚举。
  2. **恢复续写**：从开始即设置原始总上限=4；在第 2 步完整 checkpoint 提交后通过测试注入的 SimulatedInterruption(BaseException) 中断，不被 run.py 普通 Exception 处理吞掉。用新 Simulation/组件恢复剩余 2 步，断言全局编号 3/4、上限仍为 4、premise 为空。再测调用预算独立扣减，以及上限不一致/剩余调用数为零的拒绝或停止路径。
  3. **三处保存中断点**：在 turn 写入前、turn 已写但 checkpoint 未提交、checkpoint 提交后分别注入中断。恢复后核对记录范围、连续编号、旧文件可读；不能只统计 os.replace 次数。
  4. **turn 0 恢复**：仅保存初始 checkpoint 后重建，比较首次运行与恢复态的 GM 公开 premise 和角色初始记忆；不重注背景且可以正常跑出第一步。
  5. **预算路径 A**：通过请求计数定位第 2 步裁决前的超限，断言当前步不保存、checkpoint 保持 committed=1、reason=budget_limit；对角色请求与最终裁决请求分别覆盖。
  6. **预算路径 B**：判定位置耗尽时本步仍保存一次；Stage 2 再覆盖评分与指令生成位置。检查有效的既有评分是否保留、尚未取得的评分为空、没有后续模型调用、等待标志清空；达到 max_turns 时仍有 beat_timeout。原因按优先级取值，同时保留 budget 诊断。
  7. **API 错误**：完成判定或评分的 API 重试耗尽记 error，不能被转为未完成/空评分；重试次数计入 used_model_attempts。
- [ ] **Step 2-4: 红→实现→绿 + 全量验收命令**（首次跑完整 `pytest concordia && pytest examples && pylint --errors-only concordia && pyrefly check concordia`）
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/run.py examples/story_enactment/story_enactment_test.py examples/story_enactment/testing_stubs.py && git commit --only -m "feat(story): execution checkpoint recovery and budget paths" -- examples/story_enactment/run.py examples/story_enactment/story_enactment_test.py examples/story_enactment/testing_stubs.py`

---

## Stage 2：张力与导演（spec §14-2）

### Task 11: 张力评估

**Files:**
- Modify: `concordia/contrib/components/game_master/drama_director.py`
- Modify: `concordia/contrib/components/game_master/drama_director_test.py`
- Modify: `concordia/contrib/prefabs/game_master/story_dramaturgic.py`（传入评分配置）
- Modify: `examples/story_enactment/run.py`（启用 tension_observe 模式）

**Interfaces (Produces):** 构造参数增 `eval_interval: int = 2, scoring_window: int = 8, tolerance: int = 1`；StoryTurn `tension` 字段开始填充；`_tension_history` 入 checkpoint state。

- [ ] **Step 1: 写失败测试**：
  - 每 `eval_interval` 步才评分（其余步 tension=None）；评分窗口只含最近 `scoring_window` 个事件
  - stub 评分 `'7'` + 当前拍 range (1,3) + tolerance 1 → 偏离记录在案（`get_last_story_turn()` 含 `tension=7`）
  - 非数字评分重试 1 次后仍失败 → tension=None 且不生成任何后续动作（11.1）
  - 后处理位置预算耗尽（stub 在评分调用抛 BudgetExhaustedError）→ 本步提交、tension=None、`is_story_stopped()` 为真（13.1-9 评分位置）
  - 评分提示断言：捕获 InteractiveDocument prompt 含锚点文本与窗口事件、**不含** Memory 直读（stub 记录 prompt 即可断言）
- [ ] **Step 2-4: 红→实现→绿** — 流水线插入第 3 步，open_question 后解析整数并校验 1..10；仅解析无效输出时重试一次，API 错误传播、预算异常走专用路径。scoring_window 配置决定事件窗口长度，不能继续硬编码 deque(maxlen=8)；保存/恢复评估计数与窗口。评分提示不包含目标区间；用 spy 验证后处理未调用 Memory，而不是仅检查 prompt 中是否出现 Memory 字样。
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py concordia/contrib/prefabs/game_master/story_dramaturgic.py examples/story_enactment/run.py && git commit --only -m "feat(contrib): tension observation mode" -- concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py concordia/contrib/prefabs/game_master/story_dramaturgic.py examples/story_enactment/run.py`

### Task 12: 导演指令生命周期与 director_note 步骤

**Files:**
- Create: `concordia/contrib/components/game_master/director_note.py`
- Modify: `concordia/contrib/components/game_master/drama_director.py`（流水线第 4 步 + 指令状态 + `get_pending_directive()/mark_directive_integrating()/commit_directive()` + cooldown + 状态保存）
- Modify: `concordia/contrib/prefabs/game_master/story_dramaturgic.py`（步骤元组插入 director_note 于最终输出前）
- Modify: `examples/story_enactment/run.py`（启用 director 模式并传入白名单/固定事实/冷却配置）
- Test: `concordia/contrib/components/game_master/director_note_test.py`、`drama_director_test.py` 增补

**Interfaces (Produces):** `make_director_note_step(director) -> step`；DramaDirector 指令字段 `beat_id/generated_at_turn/content/status`（pending/integrating/consumed/invalidated/failed）+ `last_directive_executed_turn`。

- [ ] **Step 1: 写失败测试**：
  - 无 pending → 恒等返回候选事件且**零 LLM 调用**（stub 计数=0）
  - 生成前置条件：节拍转换步/完成步/超时步**不生成**（评分照记，13.1-4）；冷却期内不生成；评分无效不生成
  - 有 pending：步骤产出融合候选（stub 返回固定融合文本）；最终输出验证成功后 → `consumed` + 记录执行步号；再次 RESOLVE 不重复消费
  - `StoryOutputError` 发生（最终输出无效）→ 指令**不**标 consumed（error 停止语义由上层处理）
  - 指令生成处预算耗尽（13.1-9 指令位置）→ 不创建指令、停止标志
  - 恢复后（state round-trip）已 consumed 指令不再执行（13.1-4）
- [ ] **Step 2-4: 红→实现→绿** — 在 DramaDirector 构造参数与 prefab JSON 参数中接入 allowed_environment_events、immutable_facts、cooldown_turns、feedback_enabled；tension_observe 模式只记录评分，director 模式才生成指令。两段融合均包含环境白名单、固定事实和角色能动性约束（B-P3/P4）。默认用首个有效候选，避免未保存的随机选取状态；无有效候选按明确的生成失败处理。最终双轨验证成功后提交 consumed；API 错误不能变成无指令，预算异常使用路径 A 或 B，取决于所在调用阶段。
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/game_master/director_note.py concordia/contrib/components/game_master/director_note_test.py concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py concordia/contrib/prefabs/game_master/story_dramaturgic.py examples/story_enactment/run.py && git commit --only -m "feat(contrib): director feedback mode" -- concordia/contrib/components/game_master/director_note.py concordia/contrib/components/game_master/director_note_test.py concordia/contrib/components/game_master/drama_director.py concordia/contrib/components/game_master/drama_director_test.py concordia/contrib/prefabs/game_master/story_dramaturgic.py examples/story_enactment/run.py`

### Task 13: 张力曲线与人工验收资产

**Files:**
- Modify: `examples/story_enactment/renderer.py`（`render_tension_curve(turns, path)`：matplotlib，无评分步断开；`Agg` 后端）
- Create: `examples/story_enactment/acceptance.py`（三份场景配置工厂：附录 C 场景一/二/三 + 人工评审表打印）
- Test: `examples/story_enactment/renderer_test.py` 增补（曲线文件生成、空评分不崩）

- [ ] **Step 1-4: 红→实现→绿**；评审表内容按附录 C
- [ ] **Step 5: Commit** — `git add -- examples/story_enactment/renderer.py examples/story_enactment/acceptance.py examples/story_enactment/renderer_test.py && git commit --only -m "feat(story): tension curves and acceptance assets" -- examples/story_enactment/renderer.py examples/story_enactment/acceptance.py examples/story_enactment/renderer_test.py`

---

## Stage 3：角色深度（spec §14-3）

### Task 14: RelationshipsToOthers 组件

**Files:**
- Create: `concordia/contrib/components/agent/relationships.py`
- Test: `concordia/contrib/components/agent/relationships_test.py`

**Interfaces (Produces):** `RelationshipsToOthers(ActionSpecIgnored, ComponentWithLogging)`；构造参数 refresh_interval=3、pre_act_label、other_player_names（公开参与者列表）。本组件只持有该角色实际收到的已裁决事件窗口，不读取 GM 或其他角色状态。

- `pre_observe(observation)` 先用 `observation_queue.decode_observation` 还原队列编码，再只识别以 `Event: ` 开头的完整事件块；post_observe() 为这些块增加待提交事件数。发送端（故事 GM prefab）使用 `BoundaryEncodingMakeObservation` 子类对每个队列项做边界编码：三项及以上换行连排转义为可还原形式，使事件内部的换行与队列分隔符不再混淆，事件内容（含以 `Event: ` 开头的后续段落）完整进入组件窗口。公开 premise/初始资料不得使用这个保留前缀，普通背景观察不计数。使用实际 MakeObservation 输出验证该识别方式，而不是只测手写标签。
- update() 提交窗口与 `_observed_count`，达到 `_observed_count - _last_refresh >= refresh_interval` 时使 `_cache` 失效；始终调用 super().update() 清预动作缓存。
- `_make_pre_act_value()` 仅在跨步 cache 为空时用自身事件窗口调用 P6；完成后记录 `_last_refresh = _observed_count`。有 cache 时零模型调用。
- get_state/set_state 保存 cache、observed_count、last_refresh、事件窗口、刷新配置；每个新角色创建独立组件实例。

- [ ] **Step 1: 写失败测试** — 首次 act 刷新一次；任意次数管理 update 和普通背景 observe 不刷新；收到 2 个裁决事件仍复用，收到第 3 个后下一次 act 刷新。观察批次含两个 Event 块时计数增加 2。恢复 cache/count/last_refresh 后，按原阈值刷新；两角色窗口隔离，提示只含各自观察事件。通过真实实体阶段调用 pre_act，不能用未配置 phase 的 MagicMock。
- [ ] **Step 2-4: 红→实现→绿 + pylint**
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/components/agent/relationships.py concordia/contrib/components/agent/relationships_test.py && git commit --only -m "feat(contrib): cached relationship perception" -- concordia/contrib/components/agent/relationships.py concordia/contrib/components/agent/relationships_test.py`

### Task 15: EmotionalStance 接线与情绪管线

**Files:**
- Modify: `concordia/contrib/prefabs/entity/story_character.py`（在 build() 的本次 extra_components 中，emotion_options 非空时创建 EmotionalStance，键为 EMOTIONAL_STANCE_COMPONENT_KEY；relationships_refresh_interval>0 时创建 Task 14 组件，键为 RELATIONSHIPS_COMPONENT_KEY，传公开参与者名称。再委托 basic.Entity.build，不复用上次的组件对象）
- Modify: `examples/story_enactment/run.py`（启用 character_depth 模式，情绪/关系参数使用附录 C；emotion_enabled 传入 step callback。保持其他模式开关独立，不在 beats_only 中启用角色深度）
- Test: `concordia/contrib/prefabs/game_master/story_prefabs_test.py` 增补 + `examples/story_enactment/story_enactment_test.py` 增补（端到端含 emotion 字段与渲染括号提示）

- [ ] **Step 1-4: 红→实现→绿**；重跑 Task 9 的隔离/传播测试确认 stage 3 组件不破坏契约
- [ ] **Step 5: Commit** — `git add -- concordia/contrib/prefabs/entity/story_character.py examples/story_enactment/run.py concordia/contrib/prefabs/game_master/story_prefabs_test.py examples/story_enactment/story_enactment_test.py && git commit --only -m "feat(story): emotion and relationship wiring" -- concordia/contrib/prefabs/entity/story_character.py examples/story_enactment/run.py concordia/contrib/prefabs/game_master/story_prefabs_test.py examples/story_enactment/story_enactment_test.py`

### Task 16: 收尾——文档与验收核对

**Files:**
- Create: `examples/story_enactment/README.md`
- Create: `concordia/contrib/components/README.md`（当前目录没有此文件；描述新增组件及依赖，若执行时已存在则追加条目）
- Create: `concordia/contrib/prefabs/README.md`（新增故事 prefab 与共享场景编解码说明；若执行时已存在则追加条目）
- Modify: `concordia/contrib/language_models/README.md`（补 BudgetGuard 与 RetryLanguageModel 的组合、硬预算单位、异常传播说明）

- [ ] **Step 1: README** — 用法（run/resume/预算/渲染命令）、三阶段说明、指向 spec
- [ ] **Step 2: 全量验收** — `pytest concordia && pytest examples && pylint --errors-only concordia && pyrefly check concordia`；逐项核对附录 A 映射表全绿；核对 spec §15 验收标准 1/3/4/5（13.2 人工验收由用户执行）
- [ ] **Step 3: Commit** — `git add -- examples/story_enactment/README.md concordia/contrib/components/README.md concordia/contrib/prefabs/README.md concordia/contrib/language_models/README.md && git commit --only -m "docs(story): example and contrib guides" -- examples/story_enactment/README.md concordia/contrib/components/README.md concordia/contrib/prefabs/README.md concordia/contrib/language_models/README.md`

---

## 附录 A：13.1 验收项 → 测试映射

| # | 验收项 | 测试文件::用例 |
|---|---|---|
| 1 | 管理查询/premise 不计数、一次一评、并行无扰 | `drama_director_test.py::test_premise_event_not_counted`、`::test_management_queries_do_not_evaluate`、`::test_full_turn_commits_once`（重复回放段） |
| 2 | 裁决提示含节拍与世界约束 | `story_prefabs_test.py`（真实 GM act 捕获提示；同时验证共享场景编解码与安装包导入） |
| 3 | 终止原因完整 + 优先级 | `story_terminator_test.py`（逐对优先级、terminate 状态恢复、真实 SceneTracker 耗尽）+ 集成测试 |
| 4 | 指令单次/跳拍失效/恢复不重复/转换步不生成 | `director_note_test.py` 全部用例 |
| 5 | 信息隔离、双轨分离、中文 Fountain、对白传播 | `story_isolation_test.py`、`story_resolution_test.py`、`renderer_test.py`、`story_isolation_test.py::对白传播` |
| 6 | checkpoint 恢复/连续 turn_id/不重注 premise/越界清理/turn 0 | `story_enactment_test.py::恢复续写/三处中断点/turn0` |
| 7 | 生命周期签名、无事件零调用、update 不清等待标志、重复事件不重提 | `drama_director_test.py::test_full_turn_commits_once`（真实 observe/act/update）与管理查询零调用用例 |
| 8 | 最终输出适配契约（返回字符串/双轨入库/无效不广播） | `story_resolution_test.py` 全部 |
| 9 | BudgetGuard 三位置（判定/评分/指令）+ 两路径 | `budget_wrapper_test.py`、`drama_director_test.py`（判定位置）、Task 11（评分位置）、Task 12（指令位置）、`story_enactment_test.py::预算路径A/B` |
| 10 | 三处保存中断点、原子性、预算独立扣减 | `collector_test.py`（原子/校验）、`story_enactment_test.py::三处中断点`（含 StepData.step 不作全局编号断言） |

额外回归项：`story_isolation_test.py` 检查真实行动提示中的秘密隔离；
`relationships_test.py` 检查裁决事件识别与刷新游标恢复；
`story_enactment_test.py` 检查 API 错误不降级、预算零余额不续跑；
collector 的情绪测试按实际注册键读取当前角色日志。

## 附录 B：提示模板（§15.1-5）

**P1 节拍完成判定**（`yes_no_question`）：
```text
# Current story beat
{beat.description}
Completion question: {beat.done_question}

## Recent events (oldest first)
{recent_events}

## Completion criteria
Judging only by events that have actually happened (not intentions or
future plans), is the beat's completion question satisfied?
```
**P2 张力评分**（`open_question`，解析单个整数）：
```text
Rate the current dramatic tension of the story on a scale from 1 to 10.

## Scale anchors
1 = calm routine, no conflict or stakes in motion.
5 = clear conflict or stakes present, but pressure is steady.
10 = open confrontation or crisis, the outcome hangs on this moment.
## Recent events (oldest first)
{scoring_window_events}
## Output
Respond with a single integer from 1 to 10 and nothing else.
```
**P3 指令生成**（`open_question`，偏离时）：
```text
Current tension ({score}) is outside the target range {tension_range}
for the current beat. The story needs {raise_or_lower} dramatic
pressure.
## Constraints
You may ONLY use these means, never player characters' voluntary
actions: {allowed_environment_events}
## Output
Write one short director instruction (one sentence) describing an
event or NPC action that moves tension {raise_or_lower}. Never
contradict immutable facts: {immutable_facts}
```
**P4 指令融合**（两段式，仿 narrative_push）：
```text
Suggest five plausible in-story complications that fulfill this
director instruction: "{directive}". One per line, no numbering.
Only NPC or environmental events; never assume any player character
voluntarily does anything.
Allowed means: {allowed_environment_events}
Immutable facts: {immutable_facts}
Player characters whose voluntary actions you cannot decide: {player_names}
```
融合问题同时附上相同白名单、固定事实与玩家名单，然后要求将 candidate_event
与 chosen_complication 合并为一个事件，原样保留玩家对白；不得增加玩家主动行为。
**P5 最终双轨输出**（`open_question`，`terminators=()`，解析 JSON）：
```text
# Candidate event
{candidate_event}

## Current speaker
{active_player_name}

## Original speech (verbatim)
{original_speech}

## Constraints
Preserve the original speech and its speaker. Do not create voluntary
actions for any player character. Do not contradict immutable facts.
Immutable facts: {immutable_facts}

## Output
Output a JSON object with exactly two string fields:
- "resolved_event": the full canonical event, preserving the speaker's
  exact dialogue verbatim, plus what actually happened.
- "stage_directions": visible environment changes, NPC behavior, and
  outcomes only. Do NOT repeat the dialogue. Use an empty string if
  there is nothing beyond speech.
Output only the JSON object, no other text.
```
**P6 关系派生**（agent 侧）：
```text
# Observed story events of {agent_name} (oldest first)
{observed_events}

## People present
{other_player_names}

## Output
Based only on {agent_name}'s own observations, how does {agent_name}
currently view each other person present (warmth, trust, and the most
recent interaction)? One short line per person.
```

## 附录 C：导演参数默认值、人工验收场景与评审表

**默认参数**：`eval_interval=2, scoring_window=8, tolerance=1, cooldown_turns=3, total_turn_limit=24, total_model_attempt_limit=400`。Stage 1 使用 beats_only；Stage 2 先 tension_observe，人工比较后启用 director；Stage 3 使用 character_depth，`emotion_options=('平静', '焦虑', '愤怒', '悲伤', '希望', '羞愧')`、`relationships_refresh_interval=3`。

**三份验收场景**（各配 4-5 角色、至少 1 条 GM-only 秘密、6+ 条环境事件白名单）：
1. **老宅遗产**（默认示例）：遗嘱调包真相仅 GM 知；林雪持"目击陈母烧信"秘密；allowed events：雷雨断电/周律师手机响起/邮差送来旧信/花瓶碎裂/邻居敲门/收音机播报。周律师是玩家角色，环境只能让手机响起，不能替其决定接听。
2. **求职谎言**：候选人简历造假，只有其挚友知情；面试官掌握背调线索；allowed events：推荐人来电/文件掉落/前台递条/消防演习/电脑弹窗/旧同事到访。
3. **婚礼旧情**：伴娘与新郎旧情仅二人与 GM 知；新娘兄弟起疑；allowed events：香槟塔倒/投影仪放错照片/老歌响起/迟到宾客入席/花童递错戒枕/暴雨将至。

**人工评审表**（每场景跑 3 次，逐项标 turn_id；发布前由用户定通过阈值）：

| 维度 | 检查问题 | 记录 |
|---|---|---|
| 角色区分 | 台词是否能盲猜出说话者？ | turn_id + 评语 |
| 因果连续 | 事件是否被后续引用而非遗忘？ | 同上 |
| 信息泄漏 | 秘密是否在角色不知情时泄露？ | 同上 |
| 对白重复 | 相邻轮次是否复读？ | 同上 |
| 导演越权 | resolved_event 是否替玩家角色行动？ | 同上 |
| 结尾状态 | 终止原因是否与剧情吻合？ | reason + 评语 |

## 附录 D：偏离与说明（§15.1-6）

1. **新增 story_character prefab**：作为 JSON 配置到新组件实例的构建工厂；内部通过新建 extra_components 映射委托 basic.Entity.build，外部 run_config 不序列化这些对象。复用既有三问链，不修改核心。
2. **新增共享 story_scene 编解码**：放在 contrib，让安装包中的 prefab 不依赖 examples；示例只持有故事内容和显示配置。
3. **测试夹具已修订**：Task 3 通过真实实体生命周期执行；语义选择 stub 适应随机选项顺序。yes_no_question 没有 forced_response 参数，不把固定文本 Yes 当作布尔判定。
4. **关系观察识别**：继续复用既有 `Event: ` 前缀与 MakeObservation 队列分隔格式，不修改引擎/typing。三换行拆分存在歧义（事件内部的三换行与队列分隔符无法区分），故由 contrib 发送端（`observation_queue.BoundaryEncodingMakeObservation`，核心组件的子类而非修改）对队列项做边界编码，agent 侧解码后再识别完整事件块；事件内容与边界均可精确还原。公开 premise 不使用该保留前缀；Task 14 用真实队列输出验证（含事件内出现 `Event: ` 的段落）。
5. **引擎步与 story_turn 一一对应**：首版单 GM，无 initializer 或 SKIP_THIS_STEP，成功裁决对应一次剧情步。编号与预算仍以已提交全局 turn_id 为准，不能依赖恢复后从零开始的引擎局部步号。
6. **README 范围**：按仓库 AGENTS.md，新功能需要更新所属包的说明；允许修改已有 contrib README，补充新增包装器。设计中的“不修改核心代码”保持不变，源码仍仅新增于扩展区。
