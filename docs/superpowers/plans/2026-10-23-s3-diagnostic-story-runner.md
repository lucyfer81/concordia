# S3 诊断故事账本化 Runner 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付 S3 诊断故事批次的账本化 runner：包装生产 `run.py`
（字节不改）加请求账本（started 预扣、pending 纪律、报告由账本重建），
提供 `dry_run / freeze / run / summarize` 四阶段；本计划全程零真实
模型调用，真实 freeze+run 留给协议 §8 的单独执行授权。

**Architecture:** 新增 `s3_diagnostic_story.py`：`LedgerLanguageModel`
在生产 `BudgetGuard(RetryStats)` 链的最内层逐 attempt 预扣账本并在
SDK 客户端观测边界留证（原始响应、provider 返回标识、usage）；
run 阶段以模块属性替换 `run_lib.make_language_model` 注入包装器
（不改生产文件），复用 `request_ledger.RequestLedger`；每批次一个
账本（R1/R2 以 group=run_id 表达每运行 400 attempt 独立预算）；
summarize 离线交叉核对「账本计数 == 生产 used_model_attempts」。

**Tech Stack:** Python 3.12+，absl-py flags，`examples.story_enactment`
既有模块（`request_ledger`、`run`、`story_config`、`testing_stubs`、
`provenance`）；测试用 unittest + tmp 目录 + `PromptStubModel` 端到端，
CI 无 API key、无网络、不依赖 `run_results`。

**Spec:** [S3 诊断故事协议（已确认冻结）](2026-10-23-sol-s3-diagnostic-story-protocol.md)
（运行次数 2、24 turns / 400 attempts 每次、失败处理与逐轮评价标准以
该协议为准；执行者须同时读取协议原文与
[设计 §4/§9](../specs/2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md)）。

## Global Constraints

- 零真实模型调用；本计划不执行真实 `freeze`（不在 `run_results` 下建
  目录）、不读 `.env`（offline 路径除外）、不创建任何真实客户端。
- 生产代码零修改：`run.py` 及 concordia 各模块字节不动；注入只经
  模块属性替换，且必须 try/finally 恢复原属性。
- 生产行为保真：SDK `max_retries` 默认（2）不变；Concordia 层重试
  白名单与退避不变；预算守卫 `BudgetGuardLanguageModel` 不变；评审
  拒绝保持生产停止行为；观测客户端只记录、**不阻止**任何请求。
- 旧批次与 `run_results/` 全部只读；不 commit、不 stash、不 clean、
  不建 worktree、不回滚既有工作。
- 账本纪律（设计 §4.1）：started 一经持久化即占额度（含 API 错误、
  超时、无结果）；finished 不再加额；pending 不重发、不退款，须用户
  逐项授权；恢复计数一律由账本重建，不读 report 缓存、不手写数量。
- 新源文件带仓库既有 Apache 2.0 许可头；pyink（80 列、2 空格缩进）、
  isort（Google profile）、`pylint --errors-only`、`pyrefly` 通过。
- 测试 colocated；CI 不依赖 `run_results` 存在；中文字符串 UTF-8；
  全部终端命令 Bash，先 `source venv/bin/activate`。
- 诊断语义：runner 与报告**不含**任何 pass/fail 门（诊断无验收门）；
  逐轮评价与结局条件核对是运行后的人工评审交付物，不在本计划内。

## File Structure

```text
examples/story_enactment/
├── s3_diagnostic_story.py          # 新增：runner（四阶段 + 包装器）
├── s3_diagnostic_story_test.py     # 新增
└── README.md                       # 修改：新增 S3 runner 一节

run_results/story_enactment/s3_diagnostic_story/<UTC>/   # 授权轮生成
├── protocol.json / source_snapshot.json / report.json / requests.jsonl / raw/
└── R1/ R2/                        # 生产 run 状态目录（summary/checkpoint/…）
```

责任划分：`s3_diagnostic_story.py` 是唯一新增件——包装器、注入、
四阶段；预算/留证/审计全部委托 `request_ledger`；故事执行全部委托
生产 `run.run()`。批内运行目录名固定 `R1`/`R2`（协议冻结两次运行）。

---

### Task 1: LedgerLanguageModel + 观测客户端（逐 attempt 预扣与留证）

**Files:**
- Create: `examples/story_enactment/s3_diagnostic_story.py`（本任务写
  常量、异常、观测客户端、`LedgerLanguageModel`）
- Test: `examples/story_enactment/s3_diagnostic_story_test.py`

**Interfaces:**
- Consumes: `request_ledger.RequestLedger`（`begin_request`、
  `finish_request`、`record_raw_response`、`.counts`、
  `LedgerBudgetError`）；`language_model.LanguageModel`。
- Produces（后续任务依赖的精确签名）:
  - `HarnessError(RuntimeError)`、`BatchStop(HarnessError)`
  - 常量：`_BATCH_ID='s3_diagnostic_story'`、`_RUN_IDS=('R1','R2')`、
    `_MODEL_TAG='m4'`、`_MODEL_NAME='gpt-6.1-sol'`、
    `_ENV_KEY='AI_MODEL_4'`、`_MODE='director'`、
    `_ACTION_MODE='speech_and_action'`、`_DEFAULT_TURNS=24`、
    `_DEFAULT_ATTEMPTS=400`、`_TERMINAL_REASONS`（story_terminator
    六个 reason 的 frozenset）
  - `ledger_caps(attempt_limit: int = _DEFAULT_ATTEMPTS) -> dict`，
    键 `global_cap/model_caps/group_caps/sample_caps`：
    global 与 model 为 `attempt_limit * len(_RUN_IDS)`，
    group/sample 为 `{run_id: attempt_limit}`（R1/R2 独立预算）
  - `LedgerLanguageModel(model_name, *, ledger, run_id,
    attempt_limit=_DEFAULT_ATTEMPTS, stage='real_request', inner=None,
    api_key='', api_base='')`：`model_name` 与 `attempts` 属性；
    `sample_text`/`sample_choice` 每次逻辑调用占一个 slot，
    `slot_id = f'{run_id}__a{seq:06d}'`，seq 起点为
    `ledger.counts['by_group'].get(run_id, 0)`（重建后续号）
  - `_ObservationState`（`begin(slot_id)` / `finish()` /
    `boundary` dict）/ `_ObservingCompletions` / `_ObservingChat` /
    `_ObservingClient`：转发每个 `create()` 并观测
    （boundary_kwargs 去 messages、`provider_model`、`usage`、
    `first_content`），**从不阻止**；active slot 为空时 create
    抛 `BatchStop`（绕过包装器的调用 = 布线错误）

- [ ] **Step 1: 写失败测试**

```python
"""Tests for the S3 diagnostic ledger runner (offline, no network)."""

import json
import pathlib
import unittest

from concordia.language_model import language_model

from examples.story_enactment import request_ledger as rl
from examples.story_enactment import s3_diagnostic_story as s3


class _FakeInner(language_model.LanguageModel):
  """Scripted inner model; optional hook runs before each call."""

  def __init__(self, fail_on_call: int | None = None,
               hook: Callable[[], None] | None = None):
    self.calls = 0
    self._fail_on_call = fail_on_call
    self._hook = hook
    self.seen: list[str] = []

  def sample_text(self, prompt, **kwargs):
    self.calls += 1
    self.seen.append(prompt)
    if self._hook is not None:
      self._hook()
    if self.calls == self._fail_on_call:
      raise RuntimeError('transient provider error')
    return f'answer-{self.calls}'

  def sample_choice(self, prompt, responses, *, seed=None):
    self.calls += 1
    self.seen.append(prompt)
    if self._hook is not None:
      self._hook()
    return 0, responses[0], {}


class _FakeUsage:
  total_tokens = 11


class _FakeMessage:
  content = '{"valid": true}'


class _FakeResponse:
  model = 'gpt-6.1-solx'  # 故意与请求名不同，断言留证
  usage = _FakeUsage()
  choices = [type('Choice', (), {'message': _FakeMessage()})()]


class _FakeCompletions:
  def __init__(self):
    self.calls = 0

  def create(self, **kwargs):
    self.calls += 1
    return _FakeResponse()


class _FakeChat:
  def __init__(self, completions):
    self.completions = completions


class _FakeClientModel(_FakeInner):
  """Inner exposing a _client so the observation boundary is exercised."""

  def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.completions = _FakeCompletions()

  @property
  def _client(self):
    return self


class WrapperTest(unittest.TestCase):

  def _ledger(self, path, attempt_limit=400):
    return rl.RequestLedger(
        path, batch_id='t', **s3.ledger_caps(attempt_limit)
    )

  def test_caps_mirror_per_run_budgets(self):
    caps = s3.ledger_caps(400)
    self.assertEqual(caps['global_cap'], 800)
    self.assertEqual(caps['group_caps'], {'R1': 400, 'R2': 400})
    self.assertEqual(caps['sample_caps'], {'R1': 400, 'R2': 400})

  def test_prompt_and_started_persist_before_inner_call(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    raw_dir = path / 'raw'

    def hook():
      # inner 调用发生时，prompt 与 started 必须已在磁盘上。
      self.assertTrue((raw_dir / 'R1__a000001.prompt.txt').exists())
      self.assertTrue((raw_dir / 'R1__a000001.started.json').exists())

    inner = _FakeInner(hook=hook)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1', inner=inner)
    self.assertEqual(model.sample_text('a'), 'answer-1')
```

（`from collections.abc import Callable` 加入测试文件导入。）

```python
  def test_each_attempt_is_one_ledger_slot(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeInner()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1', inner=inner)
    model.sample_text('a')
    model.sample_text('b')
    self.assertEqual(model.attempts, 2)
    self.assertEqual(ledger.counts['global'], 2)
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'started'],
        ['R1__a000001', 'R1__a000002'],
    )
    self.assertEqual(
        [e['status'] for e in events if e['event'] == 'finished'],
        ['completed', 'completed'],
    )

  def test_client_observation_archives_raw_response_and_identity(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeClientModel()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1', inner=inner)
    answer = model.sample_text('p')
    self.assertEqual(answer, 'answer-1')
    raw = (path / 'raw' / 'R1__a000001.response.txt').read_text()
    self.assertEqual(raw, '{"valid": true}')
    events = rl.load_events(path / 'requests.jsonl')
    finished = [e for e in events if e['event'] == 'finished'][0]
    self.assertEqual(finished['boundary']['provider_model'], 'gpt-6.1-solx')
    self.assertEqual(finished['boundary']['usage']['total_tokens'], 11)

  def test_model_level_archive_when_no_client(self):
    # inner 无 _client（stub 形态）：response.txt 记返回文本，
    # boundary 记 observation_level='model_return'。
    ...

  def test_error_occupies_budget_and_records_status(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path)
    inner = _FakeInner(fail_on_call=1)
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1', inner=inner)
    with self.assertRaises(RuntimeError):
      model.sample_text('p')
    self.assertEqual(ledger.counts['global'], 1)  # started 已预扣
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['status'] for e in events if e['event'] == 'finished'],
        ['RuntimeError'],
    )

  def test_slot_sequence_continues_after_ledger_rebuild(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger1 = self._ledger(path)
    s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger1, run_id='R1', inner=_FakeInner()
    ).sample_text('a')
    ledger2 = self._ledger(path)  # 重建
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger2, run_id='R1', inner=_FakeInner())
    model.sample_text('b')
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(
        [e['slot_id'] for e in events if e['event'] == 'started'][-1],
        'R1__a000002',
    )

  def test_per_run_cap_blocks_before_request(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path, attempt_limit=1)
    inner = _FakeInner()
    model = s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1',
        attempt_limit=1, inner=inner)
    model.sample_text('a')
    with self.assertRaises(rl.LedgerBudgetError):
      model.sample_text('b')
    self.assertEqual(inner.calls, 1)  # 第二次从未到达 inner
    events = rl.load_events(path / 'requests.jsonl')
    self.assertEqual(events[-1]['event'], 'budget_blocked')
    self.assertEqual(events[-1]['level'], 'group')

  def test_second_run_uses_its_own_budget(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    ledger = self._ledger(path, attempt_limit=1)
    s3.LedgerLanguageModel(
        'gpt-6.1-sol', ledger=ledger, run_id='R1',
        attempt_limit=1, inner=_FakeInner()).sample_text('a')
    s3.LedgerLanguageModel(   # R2 预算独立，不受 R1 影响
        'gpt-6.1-sol', ledger=ledger, run_id='R2',
        attempt_limit=1, inner=_FakeInner()).sample_text('a')
    self.assertEqual(ledger.counts['by_group'], {'R1': 1, 'R2': 1})

  def test_choice_params_are_archived(self):
    # sample_choice 的 passed_params 含 responses 与 seed；请求前落盘。
    ...

  def test_create_outside_active_slot_is_rejected(self):
    # 观测客户端在无 active slot 时 create → BatchStop（布线错误）。
    ...
```

（`_tmp_dir` 用 `tempfile.TemporaryDirectory` 上下文 helper。）

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v`
Expected: FAIL（`ModuleNotFoundError: s3_diagnostic_story`）

- [ ] **Step 3: 实现常量、异常、观测客户端与包装器**

模块 docstring：S3 诊断故事 runner（协议
`2026-10-23-sol-s3-diagnostic-story-protocol.md` 的执行器）；生产
`run.py` 字节不改，注入经模块属性替换；逐 attempt 账本预扣（设计
§4.1），观测客户端在解析之前留证原始响应与 provider 返回标识（协议
身份披露要求），只观测不阻止（生产重试保真）。

要点：

```python
class _ObservationState:
  """Active-slot holder shared with the observing client."""

  def __init__(self):
    self.slot_id = None
    self.boundary = {}
    self.observed_response = False

  def begin(self, slot_id): ...
  def finish(self): ...  # 清空三者


class _ObservingCompletions:
  """Forwards every create() and observes it; never blocks."""

  def create(self, **kwargs):
    if self._state.slot_id is None:
      raise BatchStop('provider call outside any active slot.')
    response = self._real.create(**kwargs)
    self._state.observed_response = True
    self._state.boundary = {
        'observation_level': 'sdk_client',
        'requested_model': kwargs.get('model'),
        'messages_count': len(kwargs['messages']) if 'messages' in kwargs else None,
        'provider_model': getattr(response, 'model', None),
        'usage': _jsonable(getattr(response, 'usage', None)),
        'first_content': _first_content(response),
    }
    self._ledger.record_raw_response(
        self._state.slot_id, self._state.boundary['first_content'])
    return response
```

（`_ObservingClient.with_options` 记录调用并返回共享同一 state 的新
观测客户端；观测异常不得掩盖真实响应——client 端观测放在 try/except
里，异常时退回 model 级留证。）

`LedgerLanguageModel`：

```python
class LedgerLanguageModel(language_model.LanguageModel):
  """Wraps one run's provider model with per-attempt ledger discipline."""

  def __init__(self, model_name, *, ledger, run_id,
               attempt_limit=_DEFAULT_ATTEMPTS, stage='real_request',
               inner=None, api_key='', api_base=''):
    self._model_name = model_name
    self._ledger = ledger
    self._run_id = run_id
    self._stage = stage
    self._state = _ObservationState()
    if inner is None:
      inner = gpt_model.GptLanguageModel(
          model_name=model_name, api_key=api_key, api_base=api_base)
    client = getattr(inner, '_client', None)
    if client is not None and hasattr(client, 'chat'):
      inner._client = _ObservingClient(client, self._state, ledger)  # pylint: disable=protected-access
    self._inner = inner
    self._seq = ledger.counts['by_group'].get(run_id, 0)
    self._attempts = 0

  def _next_slot(self):
    self._seq += 1
    self._attempts += 1
    return f'{self._run_id}__a{self._seq:06d}'

  def _run_call(self, prompt, call, passed_params):
    slot_id = self._next_slot()
    self._ledger.begin_request(
        slot_id=slot_id, sample_id=self._run_id, group=self._run_id,
        model_tag=_MODEL_TAG, model_name=self._model_name,
        attempt_id=f'{self._run_id}__{_MODEL_TAG}', stage=self._stage,
        prompt=prompt, passed_params=passed_params)
    self._state.begin(slot_id)
    try:
      result = call()
    except BaseException as error:  # pylint: disable=broad-except
      self._finish(slot_id, error)
      raise
    self._finish(slot_id, None)
    return result

  def _finish(self, slot_id, error):
    boundary = dict(self._state.boundary)
    if not self._state.observed_response:
      # 无 SDK 客户端可观测（stub 或异常先于响应）：
      # model 级留证由调用点以返回文本补充。
      boundary['observation_level'] = 'model_return'
    status = 'completed' if error is None else type(error).__name__
    try:
      self._ledger.finish_request(
          slot_id=slot_id, status=status,
          error=None if error is None else f'{type(error).__name__}: {error}',
          boundary=boundary)
    finally:
      self._state.finish()
```

`sample_text`：params 记
`max_tokens/terminators/temperature/top_p/top_k/timeout/seed`；
model 级留证（无 client 观测时）在成功返回后
`record_raw_response(slot_id, text)`；注意该调用需在 `_finish` 之前
——实现为 `_run_call` 接受 `record_result: Callable[[Any], None]`，
或 `sample_text` 内联 begin/finish（允许复制此五步，不强行抽象）。
`sample_choice`：params 记 `responses/seed`。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v`
Expected: PASS（Task 1 全部）

- [ ] **Step 5: 静态检查**

```bash
pylint --errors-only examples/story_enactment/s3_diagnostic_story.py && \
pyrefly check examples/story_enactment/s3_diagnostic_story.py
```

---

### Task 2: freeze 阶段（协议、源快照、初始报告）

**Files:**
- Modify: `examples/story_enactment/s3_diagnostic_story.py`
- Test: `examples/story_enactment/s3_diagnostic_story_test.py`

**Interfaces:**
- Consumes: Task 1 常量；`request_ledger`（无）。
- Produces:
  - `_SNAPSHOT_FILES: tuple[str, ...]`（repo 相对 posix 路径，见下）
  - `protocol_payload(created_utc: str | None = None) -> dict`
  - `source_snapshot_payload() -> dict`（逐文件 sha256+bytes，调用时
    计算以捕获工作树）
  - `freeze_stage(batch_dir: pathlib.Path | str) -> None`
  - `_REPO_ROOT`、`_utc_now()`、`_file_sha256()`、`_write_json_file()`
    、`_save_report()`、`_load_report()`、`_verify_frozen()`

快照清单（冻结真实路径触达的全部文件；freeze 时任一缺失即
`BatchStop`，不做静默收缩）：

```python
_SNAPSHOT_FILES = (
    'examples/story_enactment/s3_diagnostic_story.py',
    'examples/story_enactment/s3_diagnostic_story_test.py',
    'examples/story_enactment/request_ledger.py',
    'examples/story_enactment/request_ledger_test.py',
    'examples/story_enactment/run.py',
    'examples/story_enactment/story_config.py',
    'examples/story_enactment/testing_stubs.py',
    'examples/story_enactment/collector.py',
    'examples/story_enactment/provenance.py',
    'examples/story_enactment/renderer.py',
    'concordia/components/game_master/terminate.py',
    'concordia/contrib/components/game_master/drama_director.py',
    'concordia/contrib/components/game_master/story_action.py',
    'concordia/contrib/components/game_master/story_action_adjudication.py',
    'concordia/contrib/components/game_master/story_resolution.py',
    'concordia/contrib/components/game_master/story_terminator.py',
    'concordia/contrib/language_models/budget_wrapper.py',
    'concordia/contrib/language_models/retry_stats_wrapper.py',
    'concordia/contrib/prefabs/entity/story_character.py',
    'concordia/contrib/prefabs/game_master/story_dramaturgic.py',
    'concordia/contrib/prefabs/game_master/story_scene.py',
    'concordia/contrib/language_models/openai/gpt_model.py',
    'concordia/contrib/language_models/openai/base_gpt_model.py',
    'concordia/language_model/language_model.py',
    'concordia/prefabs/simulation/generic.py',
    'concordia/agents/entity_agent_with_logging.py',
    'docs/superpowers/plans/2026-10-23-sol-s3-diagnostic-story-protocol.md',
    'docs/superpowers/specs/'
    '2026-10-21-story-enactment-probe-accounting-and-sol-validation-design.md',
)
```

（实施第一步先运行存在性核对脚本，路径不符时以实际仓库路径修正
元组并在交付记录注明；修正后不得再改。）

`protocol_payload` 必含字段（协议数字逐字来自 S3 协议 §3/§4/§5）：
`batch`、`protocol_document`（路径 + 'user-confirmed 2026-10-23'）、
`model_scope='Sol-only'`、`models`（`m4`: env 键、期望名、api 说明）、
`identity_disclosure`（provider 返回标识逐请求留证、请求与返回名差异
如实披露）、`runs`（`['R1','R2']` 顺序、fresh、不重跑）、
`budgets`（turns 24 / attempts 400 每 run、独立不结转、账本镜像）、
`retry_disclosure`（生产 SDK max_retries 默认 2 与 tenacity 白名单
原文描述、观测不阻止）、`failure_handling`（§5 五行规则原文）、
`evaluation_criteria`（§6 五问 + 结局条件核对 + 误拒嫌疑单列 +
beat_timeout 不是完成结局）、`conclusion_semantics`（诊断无验收门）、
`invocation`、`stop_boundary`（本协议不发起调用）、`created_utc`。

- [ ] **Step 1: 写失败测试**

```python
class FreezeStageTest(unittest.TestCase):

  def test_freeze_refuses_existing_dir(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.freeze_stage(path)
    with self.assertRaises(s3.BatchStop):
      s3.freeze_stage(path)

  def test_freeze_writes_hashed_payloads_and_initial_report(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.freeze_stage(path)
    report = json.loads((path / 'report.json').read_text())
    for name, key in (
        ('protocol.json', 'protocol_sha256'),
        ('source_snapshot.json', 'source_snapshot_sha256'),
    ):
      digest = hashlib.sha256((path / name).read_bytes()).hexdigest()
      self.assertEqual(report[key], digest)
    self.assertEqual(report['counts'], {
        'planned_runs': 2, 'finished_runs': 0,
        'ledger_attempts': 0, 'by_run': {},
    })
    self.assertEqual(report['runs'], {})
    self.assertIsNone(report['stop'])
    self.assertEqual(report['deviations'], [])

  def test_protocol_declares_confirmed_numbers(self):
    protocol = s3.protocol_payload()
    self.assertEqual(protocol['model_scope'], 'Sol-only')
    self.assertEqual(protocol['runs'], ['R1', 'R2'])
    self.assertEqual(protocol['budgets']['turns_per_run'], 24)
    self.assertEqual(protocol['budgets']['attempts_per_run'], 400)
    self.assertIn('beat_timeout 不是完成结局',
                  protocol['evaluation_criteria'])
    self.assertIn('诊断无验收门', protocol['conclusion_semantics'])

  def test_verify_frozen_refuses_drift(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.freeze_stage(path)
    target = path / 'protocol.json'
    target.write_text(target.read_text() + ' ')
    with self.assertRaises(s3.BatchStop):
      s3._verify_frozen(path)  # pylint: disable=protected-access

  def test_verify_frozen_refuses_source_drift(self):
    # 改动快照清单内任一仓库文件（本 runner 之外的 case：用 tmp 内
    # 不可能；改为把一个清单路径指向 tmp 相对文件——实现快照时支持
    # 注入 root 便于测试？——否则跳过此测，以 S2 同型测试为准）。
    ...
```

（实现注：`source_snapshot_payload(root: pathlib.Path = _REPO_ROOT)`
加可选 root 参数，测试以 tmp root 放置假文件后改动验证漂移拒绝；
生产路径不变。）

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k freeze`
Expected: FAIL

- [ ] **Step 3: 实现 freeze 与验证 helper**

`freeze_stage`：拒绝已存在目录 → mkdir → 写 `protocol.json`、
`source_snapshot.json` → 初始 `report.json`（含三个 sha256 字段：
protocol/source_snapshot/以及 `protocol_document_sha256` 对仓库协议
文档本体，供运行前核对文档未变）。`_verify_frozen` 复制 S2 probe
语义：四文件存在 + 快照逐文件哈希 + report 记录哈希一致。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k freeze`
Expected: PASS

- [ ] **Step 5: 静态检查**

```bash
pylint --errors-only examples/story_enactment/s3_diagnostic_story.py && \
pyrefly check examples/story_enactment/s3_diagnostic_story.py
```

---

### Task 3: run 阶段（注入、恢复决策、失败映射、pending 授权）

**Files:**
- Modify: `examples/story_enactment/s3_diagnostic_story.py`
- Test: `examples/story_enactment/s3_diagnostic_story_test.py`

**Interfaces:**
- Consumes: Task 1/2 全部；`run_lib.run`、`run_lib.make_run_config`；
  `story_terminator` 终态常量。
- Produces:
  - `_load_settings() -> tuple[str, str, list[str]]`：`.env` 读
    `AI_MODEL_4`（≠`gpt-6.1-sol` 即 `BatchStop`）、`AI_API_KEY`、
    `AI_BASE_URL`；`os.environ.setdefault('OPENAI_API_KEY', key)`、
    `setdefault('OPENAI_BASE_URL', base)`；返回 `(api_key, api_base,
    secrets)`。offline 路径不调用。
  - `_run_decision(state_dir: pathlib.Path) -> str`：
    `summary.json` 存在且 reason ∈ `_TERMINAL_REASONS` →
    `'finished'`；summary 存在但 reason 未知 → `BatchStop`；
    `checkpoint.json` 存在 → `'resume'`；否则 `'fresh'`
  - `_execute_run(run_id, state_dir, *, ledger, offline, settings,
    resume) -> tuple[dict, Any]`：替换 `run_lib.make_language_model`
    为包装器工厂（try/finally 还原），调
    `run_lib.run(state_dir, resume=resume,
    disable_language_model=offline, total_turn_limit=24,
    total_model_attempt_limit=400, mode='director',
    action_mode='speech_and_action', model_config=live_or_None)`；
    返回 `(report_entry, result)`
  - `run_stage(batch_dir, *, authorize_skip_pending: Sequence[str] = (),
    offline: bool = False, runs: Sequence[str] = _RUN_IDS) -> None`

run_stage 规则（协议 §4/§5 逐条落地）：

1. offline=False 时先 `_load_settings()`；offline=True 用 stub、
   账本 stage=`'stub_request'`、不读任何凭据。
2. `_verify_frozen`；`RequestLedger(batch_dir, batch_id=_BATCH_ID,
   **ledger_caps())` 从账本重建；`report['stop']` 已记录 →
   `SystemExit`（只允许离线汇总）。
3. `ledger.pending_slots()` 非空且未全部列入
   `authorize_skip_pending` → `SystemExit` 列出槽位（不重发不退款）；
   授权后：`report['deviations']` 追加
   `{slots, note: 'user-authorized deviation: pending slots not
   resent; scope limited to these slots', utc}`，然后继续（不整 run
   作废——全故事中断槽不代表该 run 失败，由恢复继续；槽位永远留在
   `started_no_result` 口径中）。
4. 每个 run：`_run_decision` → `finished`：从 `summary.json` 回填
   report 条目（幂等跳过，不重跑）；`fresh`/`resume`：执行
   `_execute_run`；`result.used_model_attempts` 与
   `ledger.counts['by_group'][run_id]` 不等 → `BatchStop`（审计异常，
   停批留场）。
5. report 条目字段：`run_id, status('fresh'|'resumed'|'finished'),
   reason, committed_turn_id, used_model_attempts, ledger_attempts,
   used_api_retries, scoring_parse_retries, wall_time_seconds,
   budget_exhausted, error_message`。
6. 每例之后 `_refresh_counts(report, ledger)`（与 S2 同型：计数一律
   `ledger.counts` 派生）并原子保存。
7. `run_lib.run` 抛出异常（认证/路由/校验）→ `report['stop']` 记录
   后 `SystemExit`（批次停止，现场保留）；`run()` 内部转 `reason` 的
   故障（含评审拒绝 `error`）是正常终态，批次继续。
8. `LedgerBudgetError`/`BatchStop`/意外异常 → 同规则 7（停批）。

- [ ] **Step 1: 写失败测试**

```python
class RunStageTest(unittest.TestCase):

  def _frozen_batch(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.freeze_stage(path)
    return path

  def test_run_stage_offline_executes_both_runs_with_stub(self):
    batch = self._frozen_batch()
    s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(sorted(report['runs']), ['R1', 'R2'])
    for run_id, entry in report['runs'].items():
      self.assertIn(entry['reason'], s3._TERMINAL_REASONS)  # pylint: disable=protected-access
      self.assertEqual(
          entry['used_model_attempts'], entry['ledger_attempts'])
    self.assertEqual(
        report['counts']['finished_runs'], 2)
    self.assertLessEqual(report['counts']['ledger_attempts'], 800)

  def test_run_stage_is_idempotent_after_terminal(self):
    batch = self._frozen_batch()
    s3.run_stage(batch, offline=True)
    events_before = (batch / 'requests.jsonl').read_text()
    s3.run_stage(batch, offline=True)  # 重入：全部 finished，零新请求
    self.assertEqual((batch / 'requests.jsonl').read_text(),
                     events_before)

  def test_summary_without_checkpoint_reports_finished_entry(self):
    # 幂等回填：删 report 中 runs 后重入，仍从 summary.json 回填。
    ...

  def test_resume_after_interrupted_run_completes(self):
    batch = self._frozen_batch()
    # 模拟中断：先跑一次 stub 运行，删除 summary.json 使
    # _run_decision 返回 'resume'，checkpoint 仍在。
    state = batch / 'R1'
    s3.run_stage(batch, offline=True, runs=('R1',))
    (state / 'summary.json').unlink()
    # 清 report 里 R1 条目，模拟「该 run 未完成」的重入。
    report = json.loads((batch / 'report.json').read_text())
    report['runs'].pop('R1')
    (batch / 'report.json').write_text(json.dumps(report, ensure_ascii=False))
    s3.run_stage(batch, offline=True)
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(report['runs']['R1']['status'], 'resumed')

  def test_pending_slot_refuses_until_authorized(self):
    batch = self._frozen_batch()
    ledger = rl.RequestLedger(batch, batch_id=s3._BATCH_ID,  # pylint: disable=protected-access
                              **s3.ledger_caps())
    ledger.begin_request(
        slot_id='R1__a000001', sample_id='R1', group='R1',
        model_tag='m4', model_name='gpt-6.1-sol',
        attempt_id='R1__m4', stage='real_request', prompt='p',
        passed_params={})  # started 无 finished → pending
    with self.assertRaises(SystemExit):
      s3.run_stage(batch, offline=True)
    s3.run_stage(batch, offline=True, authorize_skip_pending=('R1__a000001',))
    report = json.loads((batch / 'report.json').read_text())
    self.assertEqual(len(report['deviations']), 1)
    self.assertIn('R1__a000001', report['deviations'][0]['slots'])

  def test_run_exception_stops_batch(self):
    batch = self._frozen_batch()
    original = run_lib.run
    def exploding(*args, **kwargs):
      raise ConnectionError('auth failed')
    run_lib.run = exploding
    try:
      with self.assertRaises(SystemExit):
        s3.run_stage(batch, offline=True, runs=('R1',))
    finally:
      run_lib.run = original
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNotNone(report['stop'])

  def test_ledger_production_mismatch_stops_batch(self):
    # 构造：账本少记一次（直接改 report 不可行——实现为
    # _execute_run 返回后核对；测试用 fake run_lib.run 返回
    # 伪造 RunResult(used_model_attempts=+1) 触发不一致）。
    ...

  def test_offline_stage_never_reads_env(self):
    # 删除 tmp 外 .env 不可行；以 offline 分支不调用 _load_settings
    # 的单测覆盖（monkeypatch _load_settings 为抛错，offline=True
    # 仍成功）。
    ...
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k run_stage`
Expected: FAIL

- [ ] **Step 3: 实现 run 阶段**

按上述规则 1–8 实现；`_execute_run` 的包装器工厂：

```python
def _execute_run(run_id, state_dir, *, ledger, offline, settings, resume):
  original = run_lib.make_language_model
  stage = 'stub_request' if offline else 'real_request'

  def factory(model_config, *, disable_language_model=False):
    provider, retryable = original(
        model_config, disable_language_model=disable_language_model)
    wrapped = LedgerLanguageModel(
        _MODEL_NAME, ledger=ledger, run_id=run_id, stage=stage,
        inner=provider)
    return wrapped, retryable

  run_lib.make_language_model = factory
  try:
    started = time.time()
    result = run_lib.run(
        str(state_dir), resume=resume, disable_language_model=offline,
        total_turn_limit=_DEFAULT_TURNS,
        total_model_attempt_limit=_DEFAULT_ATTEMPTS,
        mode=_MODE, action_mode=_ACTION_MODE,
        model_config=None if offline else _live_model_config(settings))
  finally:
    run_lib.make_language_model = original
  ...
```

（注意 `make_language_model` 的真实签名是
`(model_config, *, disable_language_model=False)`，工厂必须兼容位置
与关键字两种调用形态——`run()` 只用关键字，但工厂签名照抄原函数。）

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k run_stage`
Expected: PASS

---

### Task 4: summarize 阶段（离线交叉核对与披露）

**Files:**
- Modify: `examples/story_enactment/s3_diagnostic_story.py`
- Test: `examples/story_enactment/s3_diagnostic_story_test.py`

**Interfaces:**
- Consumes: Task 1–3；`request_ledger.load_events/audit_events/
  rebuild_counts`；各 run 的 `summary.json`。
- Produces:
  - `summarize_stage(batch_dir, *, reference_snapshot:
    pathlib.Path | None = None) -> dict`

规则：

1. `_verify_frozen`；账本 `load_events` + `audit_events(raw_dir=)`；
   任一失败 → 清除 `report['summary']`、写 `summary_error` 后抛
   `LedgerAuditError`（不得保留旧 summary）。
2. 逐 run 交叉核对：`ledger.counts['by_group']` vs
   `summary.json['used_model_attempts']`（缺失 summary 的 run 记
   `no_summary`）→ 不等即 `LedgerAuditError`。
3. `summary` 块：`integrity`（总 attempt、按 run、pending 列表、
   deviations、stop）+ `runs`（每 run：reason、committed turns、
   两口径 attempt、api retries、scoring parse retries、wall）+
   `dependency_drift_vs_reference`（与 S2 同型交集哈希差异清单，
   未提供记 `'not compared'`）+ `disclosures`（口径声明：账本计
   Concordia attempts，SDK 层重试不在账本内，HTTP 请求可更多；
   pending 即已预扣未观测）。
4. **无 pass/fail 字段**；打印 summary。

- [ ] **Step 1: 写失败测试**

```python
class SummarizeStageTest(unittest.TestCase):

  def test_summary_cross_checks_ledger_against_production(self):
    batch = self._prepared_batch()  # freeze + offline run 两例
    summary = s3.summarize_stage(batch)
    for run_id, entry in summary['runs'].items():
      self.assertEqual(
          entry['ledger_attempts'], entry['production_attempts'])

  def test_tampered_summary_fails_audit(self):
    batch = self._prepared_batch()
    summary_path = batch / 'R1' / 'summary.json'
    data = json.loads(summary_path.read_text())
    data['used_model_attempts'] += 1
    summary_path.write_text(json.dumps(data, ensure_ascii=False))
    with self.assertRaises(rl.LedgerAuditError):
      s3.summarize_stage(batch)
    report = json.loads((batch / 'report.json').read_text())
    self.assertIsNone(report['summary'])
    self.assertIsNotNone(report.get('summary_error'))

  def test_dependency_drift_lists_intersection_differences(self):
    # S2 同型：合成 reference snapshot，交集文件改一字节 → 清单含之。
    ...

  def test_summary_has_no_pass_gate(self):
    batch = self._prepared_batch()
    summary = s3.summarize_stage(batch)
    self.assertNotIn('rev_gate', summary)
    self.assertNotIn('overall_pass',
                     json.loads((batch / 'report.json').read_text()))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k summarize`
Expected: FAIL

- [ ] **Step 3: 实现 summarize**

按规则 1–4；`_dependency_drift` 与 S2 probe 同型（交集比对）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k summarize`
Expected: PASS

---

### Task 5: dry_run 阶段、README 与全量验证

**Files:**
- Modify: `examples/story_enactment/s3_diagnostic_story.py`
- Modify: `examples/story_enactment/README.md`
- Test: `examples/story_enactment/s3_diagnostic_story_test.py`

**Interfaces:**
- Consumes: Task 3 的 offline 执行路径。
- Produces:
  - `dry_run_stage(dry_run_dir: pathlib.Path | str) -> None`：
    拒绝已存在目录 → 冻结等价物（tmp 内 protocol+snapshot+report）
    → `run_stage(offline=True)` → `summarize_stage` → 断言：
    两 run 终态、逐 run `ledger==production`、账本 prompt 文件数
    == 账本 attempt 数、零真实客户端（包装器 inner 均为
    `PromptStubModel`）、cap 未超 → 写 `dry_run_report.json`
    （`ok`、逐 run reason/attempts、断言结果）；任一断言失败
    `SystemExit`。
  - FLAGS：`--stage`（必填，四选一）、`--batch_dir`、
    `--dry_run_dir`（默认
    `/tmp/opencode/s3_diagnostic_story_dry_run`）、
    `--authorize_skip_pending`、`--reference_snapshot`、
    `--runs`（逗号分隔子集，默认 `R1,R2`）。

- [ ] **Step 1: 写失败测试**

```python
class DryRunStageTest(unittest.TestCase):

  def test_dry_run_end_to_end_with_zero_network(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.dry_run_stage(path)
    report = json.loads((path / 'dry_run_report.json').read_text())
    self.assertTrue(report['ok'])
    self.assertEqual(sorted(report['runs']), ['R1', 'R2'])
    for entry in report['runs'].values():
      self.assertEqual(entry['ledger_attempts'],
                       entry['production_attempts'])
      self.assertLessEqual(entry['ledger_attempts'], 400)
    # 每个账本 attempt 都有留证 prompt。
    ledger_lines = (path / 'requests.jsonl').read_text().splitlines()
    started = [json.loads(l) for l in ledger_lines
               if json.loads(l)['event'] == 'started']
    for event in started:
      self.assertTrue(
          (path / 'raw' / f"{event['slot_id']}.prompt.txt").exists())

  def test_dry_run_refuses_existing_dir(self):
    path = pathlib.Path(self.enterContext(_tmp_dir()))
    s3.dry_run_stage(path)
    with self.assertRaises(s3.BatchStop):
      s3.dry_run_stage(path)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v -k dry_run`
Expected: FAIL

- [ ] **Step 3: 实现 dry_run + main 分发**

`main` 按 `--stage` 分发（`run` 支持 `--runs` 子集与
`--authorize_skip_pending`；`summarize` 支持 `--reference_snapshot`，
指向控制轮
`run_results/story_enactment/full_story_control/20261019T032409Z/
source_snapshot.json` 以披露依赖漂移——该参数仅授权轮使用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest examples/story_enactment/s3_diagnostic_story_test.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 无网络 dry-run 证据**

```bash
source venv/bin/activate && rm -rf /tmp/opencode/s3_dry_run_evidence && \
python -m examples.story_enactment.s3_diagnostic_story \
  --stage=dry_run --dry_run_dir=/tmp/opencode/s3_dry_run_evidence
```

Expected: exit 0；`dry_run_report.json` 的 `ok=true`、两 run 终态、
逐 run 账本与生产计数相等。

- [ ] **Step 6: README 新增一节**

`examples/story_enactment/README.md` 在 Sol REV Re-Validation 一节后
新增 `## S3 Diagnostic Story Runner`：协议引用（已确认冻结的 S3 协议
文档）、Sol-only 2×(24 turns/400 attempts)、账本纪律与 pending 规则、
四阶段命令块（标注：真实 `freeze`/`run` 需按协议 §8 另获授权）、
诊断无验收门声明。

- [ ] **Step 7: 全量验证**

```bash
source venv/bin/activate && pytest examples -x -q && \
pylint --errors-only examples/story_enactment && \
pyrefly check examples/story_enactment
```

Expected: 全部 PASS / 无 error（既有无关失败如实分列记录，不修复
不掩盖）。

---

## 执行边界（再次强调）

- 本计划所有步骤零真实模型调用、零 `.env` 读取（offline 路径）、
  零 `run_results` 写入；真实 freeze/run 须按协议 §8 在本计划交付并
  审阅后单独授权。
- 生产文件字节不动；唯一被临时替换的是 `run_lib.make_language_model`
  模块属性（try/finally 还原）。
- 逐轮评价（协议 §6 五问）、结局条件核对表、误拒嫌疑记录是运行后的
  人工评审交付物，不在本计划内。

## Self-Review 记录

- 协议覆盖：§3 运行配置（Task 2 protocol_payload + Task 3 run 参数）、
  §4 预算与口径（Task 1 caps/镜像 + Task 4 disclosures）、§5 失败
  处理五行（Task 3 规则 2/3/7/8 + Task 4 stop/pendings）、§6 评价
  标准冻结进 protocol_payload（Task 2）、§7 产出（Task 4 summary
  结构 + 生产产物由 run() 原生写）、§8 停止边界（Global Constraints
  + 执行边界）。
- 类型一致性：`ledger_caps()` 键名与 `RequestLedger` 构造参数一致；
  `LedgerLanguageModel.__init__` 签名在 Task 1/3 一致；
  `_run_decision` 返回值三态在 Task 3 规则 4 一致；report 条目字段
  在 Task 3 规则 5 与 Task 4 runs 提取一致。
- 占位符扫描：全部测试给出可写出的断言或明确指认 S2 同型实现
  （`_dependency_drift`、`_verify_frozen`、`_refresh_counts`，
  均给出源文件与语义）；无 TBD/TODO。
