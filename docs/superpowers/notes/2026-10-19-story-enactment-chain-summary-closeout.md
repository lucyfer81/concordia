# Story enactment 链路汇总工具修复与离线重汇总收尾记录

日期：2026-10-19。方案（实施计划）：
`../plans/2026-10-19-story-enactment-chain-summary-closeout.md`，设计：
`../specs/2026-10-19-story-enactment-chain-summary-closeout-design.md`。
按用户授权执行 Task 1–8（含 Task 6 真实批次离线重汇总与 Task 8 全量验证）；
全程未执行任何 git commit/stash/clean/checkout（开工基线快照：
`/tmp/opencode/chain-summary-closeout-baseline/`，收尾 `git status --porcelain`
与开工基线逐行一致）；未回滚同事未提交的进行中修改；离线重汇总为纯
本地代码路径，新增真实模型调用 0。

## 1. 改动文件清单

- 工具源码：`examples/story_enactment/adjudication_chain_probe.py`
  （E 阶段 gate 接入人工污染标注计数：`human_canonical_nonstory` /
  `human_label_incomplete`；新增 `resummarize_offline` 离线重汇总入口，
  含输入清单双向核对、原报告哈希、工具源码快照与差异白名单）。
- 工具测试：`examples/story_enactment/adjudication_chain_probe_test.py`
  （构建辅助函数重构为模块级 + 新增 gate/离线工具测试，见第 2 节）。
- 文档：`examples/story_enactment/README.md`（Adjudication-Chain Probe 节
  增补 `resummarize_offline` 用法说明与命令）；批次
  `run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z/README.md`
  （两处计数勘误 + 新增 §8 收尾说明，见第 7 节）。
- 清单（新）：
  `docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json`
  （443 个文件，sha256
  `d5da985c97a6d81a96933af7a2697dd5454d6a20e99f5b59fbf5238563621f6a`）。
- 新报告目录（新）：
  `run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_20261019T023636Z/`
  （仅含 `report.json` 一个文件，33,052 字节）。
- 本交付记录自身。

明细证据见工作区 SDD 台账
`.superpowers/sdd/2026-10-19-story-enactment-chain-summary-closeout/task-1..8-report.md`。

## 2. 测试结果（RED/GREEN 与全量检查）

关键反例（E 阶段人工污染标注 gate）TDD 证据（Task 3）：

- **RED**（实现前）：
  `pytest examples/story_enactment/adjudication_chain_probe_test.py::SummarizeAccountingTest::test_e_gate_fails_on_one_human_canonical_nonstory_label -v`
  → FAILED，`adjudication_chain_probe_test.py:700`：
  `KeyError: 'human_canonical_nonstory'`（原实现 `e_metrics` 无该键，
  第一条新断言即抛 KeyError，与计划预期一致）。
- **GREEN**（实现后）：同命令 → `1 passed in 2.83s`；全文件当前
  `40 passed`（开工基线 27 → 全部任务完成后 40）。

全量检查（AGENTS.md 适用项 + 改动文件格式检查，每条独立运行、单独捕获
退出码，2026-10-19 收尾复核）：

| 检查命令 | 退出码 | 结果 |
|---|---|---|
| `pytest examples/story_enactment/adjudication_chain_probe_test.py` | 0 | `40 passed in 3.11s` |
| `pytest examples` | 0 | `488 passed in 30.65s`（较基线 475 增加 13） |
| `pytest concordia` | 0 | `1472 passed, 33 skipped in 17.37s` |
| `pylint --errors-only concordia` | 0 | 无错误（无输出） |
| `pyrefly check concordia` | 1 | 仅既有 4 项依赖类型错误（见下分类），875 suppressed |
| `pyink --check`（上述两个工具文件） | 0 | `2 files would be left unchanged.` |
| `isort --check-only`（同两文件） | 0 | 通过（无输出） |
| `pylint --errors-only`（同两文件） | 0 | 无错误（无输出） |
| `pyrefly check`（同两文件） | 1 | 5 项错误，均为实验版既有模式（见下分类） |

pyrefly 基线区分说明（两类错误互不重叠，均不属本次改动引入）：

- `pyrefly check concordia` 的 4 项为**已知依赖未装入 venv** 类，与
  2026-10-16/2026-10-17 记录的既有基线完全一致，非源码缺陷：
  `concordia/contrib/language_models/vllm/vllm_model.py:53–55`
  vllm missing-import ×3；
  `concordia/contrib/tools/mcp/servers/file_reader_server.py:28`
  FastMCP missing-attribute ×1。故**不宣称"全部静态检查通过"**。
- `pyrefly check` 两个工具文件的 5 项为**实验版既有代码模式**的类型
  推断限制（Task 3 报告已确认先于本次编辑存在）：
  `adjudication_chain_probe.py:551` `_ObservingClient` 赋值
  （`AzureOpenAI | OpenAI` 类型不兼容）×1；`adjudication_chain_probe.py:2545/2548/2551/2554`
  `summary[...] = bool(...)` 异构字典键推断 ×4。均不在本次
  `e_metrics` 编辑区内。

## 3. 实际离线重汇总命令与新报告路径

Task 6 实际执行（`STAMP` 运行时经 `date -u +%Y%m%dT%H%M%SZ` 解析为
`20261019T023636Z`，两步退出码均 0）：

```bash
source venv/bin/activate
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_${STAMP}"
python -m examples.story_enactment.adjudication_chain_probe \
    --stage=resummarize_offline \
    --batch_dir=run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z \
    --output_dir="$OUT" \
    --input_manifest=docs/superpowers/notes/2026-10-19-chain-probe-offline-input-manifest.json
```

stdout（逐字）：

```
offline resummary written: run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_20261019T023636Z/report.json overall_pass=True new_real_api_calls=0
```

新报告路径：
`run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_20261019T023636Z/report.json`。

## 4. 关键反例：仅改一条人工标签即可翻转批次结论

测试 `SummarizeAccountingTest::test_e_gate_fails_on_one_human_canonical_nonstory_label`
（Task 3，RED→GREEN）：在临时数据中**仅**将 `E03__m4__r1` 的人工标签
`canonical_nonstory_text` 由 false 改为 true（机器扫描
`canonical_known_nonstory` 保持 false），批次由 PASS 变 FAIL——
`stage_e.m4.human_canonical_nonstory=1` → E gate `pass=False` →
`overall_pass=False`；恢复 false 后重新运行即恢复 PASS。该反例证明：
旧汇总完全忽略人工污染标注（机器扫描独扛），修复后任何一条人工
non-story 判定都足以否决整批通过，满足设计 §4.1 的五级失败链要求。

## 5. 原始输入零改动证明与新工具源码差异清单

Task 6 Step 2 清单复核输出（逐字，断言全部通过、退出码 0）：

```
offline resummary verified: run_results/story_enactment/action_authorization_chain_probe/20261018T012101Z_resummarized_20261019T023636Z
source diff files: ['examples/story_enactment/adjudication_chain_probe.py', 'examples/story_enactment/adjudication_chain_probe_test.py']
```

- 原始批次 `20261018T012101Z` 全部 443 个文件与 Task 1 清单逐文件
  sha256 比对一致（字节级零改动）；清单文件自身 sha256 复核仍为
  `d5da985c97a6d81a96933af7a2697dd5454d6a20e99f5b59fbf5238563621f6a`；
  `original_report_sha256` 与清单记录的原 `report.json` 哈希相等。
- 新工具源码差异清单（新报告 `offline_resummarize.source_diff_vs_experiment`，
  实验 sha256 → 当前 sha256）：

| 文件 | experiment_sha256 | current_sha256 |
|---|---|---|
| `examples/story_enactment/adjudication_chain_probe.py` | `d03eb3ecc63912c7a05c8107204b748634bcae1c65a1cbca6929438f2ee45ba6` | `0de217c45f70657ed79e644d8670fdc687095afbd4a93f872422d8b62ef640f7` |
| `examples/story_enactment/adjudication_chain_probe_test.py` | `51e7f1e86e5c9b3ca45132bfaac43caf8a0011ece11b5f3f943665466239d318` | `8a81d7b53dcc35e73da637820d1aab389766c338ad5b66beee063150fda6b797` |

## 6. 新报告来源记录摘要

新 `report.json` 的 `offline_resummarize` 来源块要点：`overall_pass=true`
（`report` 与 `summary` 两处一致）；m1、m4 两模型
`human_canonical_nonstory=0` 且 `human_label_incomplete=0`、E gate
`pass=true`；调用计数与原批次一致——计划尝试 24/24/24，真实调用总数
120（stage_a 24 + stage_final 48 + stage_e 48）；`new_real_api_calls=0`。
重汇总时间 `resummarized_at_utc: 2026-10-19T02:36:37Z`，输入清单路径与
哈希、原报告哈希、20 个工具源码文件快照均已嵌入来源块（run 阶段会在
任何请求前复验这些哈希）。本次修复不改变任何裁决/生成/标注数据，
仅以修复后的固定汇总逻辑离线重算既有响应的汇总与 gate 结论。

## 7. 批次 README 勘误与收尾链接

批次 `20261018T012101Z/README.md`（Task 7）：

- §3 路由观察：`（本批 72/72 一致）` →
  `（m1 的 A 12 + FINAL 24 + E 24 共 60 个真实响应，60/60 一致）`，
  并附口径说明（仅勘误计数口径，不表示路由身份已查明；路由身份局限
  披露原文保留）。
- §2 m1 生成器观察项：`被复核按规则 7（no-repair）12/12 拦截` →
  `6/6 拦截（E02/E04 各 3 次；两模型负例合计 12/12）`；§1 表格中
  每模型 12/12 条目未动。
- 新增 §8「汇总工具修复与离线重汇总收尾（2026-10-19）」，含新报告
  相对链接
  `../20261018T012101Z_resummarized_20261019T023636Z/report.json`（已
  `ls` 确认目标存在）与上述离线重汇总命令记录。

## 8. 收尾声明（停在工程审阅点）

- 本轮全部工作（含离线重汇总）**新增真实模型调用 0**
  （`new_real_api_calls=0`，离线入口不访问网络与凭据）。
- **未重新裁决、未重新生成、未重新复核、未重新标注**任何实验数据；
  原始批次字节级未动（第 5 节证明）。
- **未启动** Gemini/Sol 四组完整故事运行。
- 全程**无任何 git commit/stash/clean/checkout**；收尾
  `git status --porcelain`（30 行）与开工基线逐行一致（快照存
  `/tmp/opencode/chain-summary-closeout-baseline/git_status_final.txt`），
  生产快照完整性校验输出 `production snapshot integrity OK`。
- 本轮到此**停在工程审阅点**：上述离线汇总修复不构成新增真实模型
  验证，是否开展后续四组完整故事运行由用户审阅后决定。
