# Issue Agent

独立的单 Agent Issue-to-PR 框架，当前完成 Phase 4：规划、文件修改、Java 测试、失败反馈、双重验证和本地 PR 产物。
自动生成 PR Title/Description，但不自动 commit、push 或创建 GitHub PR。
已有 Maven、Gradle 真实模型闭环记录。能力证据、复现命令与剩余限制见 [MVP 验收记录](ACCEPTANCE.md)。
现版本已作为 Git Baseline 固定；隔离 Hidden Tests 的单任务评测见 [Benchmark 说明](BENCHMARK.md)。

## 快速使用

Python 3.11+、Git；目标项目需要 JDK 和 Maven/Gradle（或仓库自带 wrapper）。
详细的本机 PowerShell 操作见 [PHASE3.md](PHASE3.md)。

```powershell
python -m pip install -r requirements.txt
$env:ANTHROPIC_BASE_URL = 'https://api.deepseek.com/anthropic'
$env:ANTHROPIC_API_KEY = '你的新 DeepSeek 密钥'
$env:MODEL_ID = 'deepseek-flash'
python main.py --repo C:\path\to\repository --issue "给注册接口增加邮箱校验并补充测试"
```

仅检查仓库用 `--inspect`，不调用模型、不执行构建。
`--max-steps` 默认 20，上限 200；每轮最多 8 次工具调用，每次验证额外至多 2 次验证模型调用。
`--test-timeout` 默认 300 秒，允许 1–1800 秒。SDK 单次请求 120 秒，最多重试 2 次。
`--verification-model` 可指定审查模型，默认复用执行模型，但使用独立上下文、无工具。
凭据仅从进程环境读取，不自动加载任何 .env。

退出码：0=验证完成/inspect成功；1=启动异常；2=任务失败、验证不可用或达到上限。
CLI 现在同时输出模型最后的文字、最新测试结果和日志目录，不必只去日志找解释。
生产环境不再使用 Phase 2 的 verification_unavailable 占位状态。
纯阅读、README 修改等没有测试的任务可以执行工具，但不能通过当前面向开发任务的严格完成判定。

## 流程与职责

```text
main.py → Workspace → AgentState + Planner
                          ↓
                ContextManager → ModelClient
                          ↓
                  ToolRegistry 执行工具
                          ↓
           run_test 失败 → 摘要反馈 → 修复 → 重测
                          ↓
         模型停止调用工具 → IssueVerifier
                          ↓
       本次实际差异 + 最新测试通过 + LLM需求审查
                   ↓失败           ↓通过
                返回主循环        completed
```

- core/agent.py：循环、验证反馈和统一的模型调用统计。
- core/state.py：计划、消息、文件、测试、验证与 token 状态。
- planning/planner.py：以 Issue 和构建类型生成四阶段结构化计划，无额外模型调用。
- workspace/workspace.py：统一路径检查。
- workspace/changes.py：任务起点源码快照、当前指纹和包含新文件的任务 diff。
- tools/file.py、search.py：读取、搜索、精确修改、新建。
- tools/test.py：无参数 run_test、固定命令、超时终止、XML 报告解析。
- tools/git.py：只读 Git status/diff，禁用 external diff/textconv。
- verification/verifier.py：确定性门槛后调用模型审查，严格验证 JSON。
- trace/tracer.py：JSONL 事件、完整输出、状态与指标。

Plan 是可解释的阶段指南，阶段状态不替代验证证据；没有单独的 Planner Agent。
因为 Verifier 需要查看真实改动，Git 只读工具和任务 diff 提前到 Phase 3 实现，不增加框架。

## 测试执行与边界

Maven 检测根目录 pom.xml，优先 mvnw/mvnw.cmd，否则 PATH 上的 mvn。
执行固定 `-B -DskipTests=false -Dmaven.test.skip=false test`。
Gradle 检测 build.gradle/build.gradle.kts，优先 gradlew/gradlew.bat，否则 PATH 上的 gradle。
执行固定 `test --no-daemon --rerun-tasks --no-build-cache`。

解析 Maven `target/surefire-reports/TEST-*.xml` 和 Gradle `build/test-results/test/TEST-*.xml`，支持标准布局的子模块。
仅接受本轮新写入/更新的报告；零测试、全跳过、超时、命令失败、测试期间源码变化均不通过。
自定义报告目录、自定义测试 task、Failsafe 集成测试尚未支持。
默认不执行 clean，不删除仓库构建目录。超时尝试终止整个测试进程树。

文件工具禁止越界、Git 元数据、符号链接/junction/硬链接访问。
**构建进程不是 OS 沙箱**：Maven/Gradle 插件、测试和 wrapper 是仓库代码，有当前用户权限；
即使 cwd 位于仓库，也可以访问其他目录和网络。当前只适用于可信本地仓库，不宣称隔离恶意构建。
构建子进程会移除名称含 API_KEY/AUTH_TOKEN 的环境变量。
不保证防御并发恶意换路径或篡改测试报告。LLM 审查也不能证明业务逻辑绝对正确。

快照排除 target/build、.gradle、.task_outputs、.idea 等生成目录；总源码上限 50 MB/20000 文件。
验证阶段拒绝无法审查的二进制改动以及超过 60000 字符的任务 diff，不用截断 diff 冒充完整审查。

## 日志

目标仓库 `.task_outputs/<任务ID>/`：

- events.jsonl：UTC 时间、事件、轮数、耗时、模型输入/输出、工具和验证记录。
- state.json：当前 Plan、全部消息、文件列表、测试摘要、验证结果、最后回复。
- test-001.log 等：完整测试输出（单次上限约 50 MB，超过时终止）；摘要保留末尾 4000 字节和最多 10 项失败。
- tool-*.log：工具完整返回内容。
- git.diff：当前已跟踪文件的 staged + unstaged 差异，可能含运行前已有改动。
- task.diff：相对任务启动时的源码差异，含本次新增文件，用于需求验证。
- metrics.json：模型/工具调用数、token、测试次数、测试失败、验证失败、耗时与修改文件数量。

测试结果关联源码指纹；测试后又编辑，Verifier 必须重跑。用户原有未提交改动不计作本次成果。
files_modified 是历史写入记录，最终存活改动以 task.diff 为准。
日志会包含仓库内容，请按源码权限管理。Agent 不自动改目标仓库 .gitignore。

## 验证

```powershell
python -m unittest discover -s tests -v
# 可选：真实 Maven/JUnit 集成测试，需要本机 JDK 和 Maven
$env:RUN_JAVA_INTEGRATION = '1'
python -m unittest discover -s tests -v
# 创建全新演示仓库，固定模型响应 + 真实 Git/Maven/JUnit
python demo_phase3.py --repo C:\path\to\new-demo
```

基础测试包含模拟子进程报告和模拟模型；Windows wrapper 回归测试真实执行 bat，但不启动 Gradle 引擎。
演示和可选 Maven 集成测试真实执行编译和 JUnit，验证 3 项失败 → 修复 → 4 项通过。
其中模型与审查响应固定，不代表真实模型自主完成能力已经评测通过。

来源与许可见 [MIGRATION.md](MIGRATION.md)、[LICENSE](LICENSE)。后续重点为更多真实仓库验收和失败场景评测。

## 验证截断恢复（新增）

验证首次预算为 4096；若 stop_reason=max_tokens，验证器内部重试一次，预算提高到 8192。
JSON 格式或字段错误也最多重试一次，保持原预算。合法的需求拒绝不在验证器内重试，而是返回 Agent 修复。
API 异常由 SDK 原有有限重试处理，验证层不再套额外网络重试。
验证重试耗尽或 API 仍失败时，状态为 verification_error，退出码 2，保存原因且不继续改代码。
不会使用不完整回复判定成功。执行阶段的默认输出额度仍为 4096，未涉及缓存优化。

events.jsonl 新增 VERIFICATION_ATTEMPT_FAILED、VERIFICATION_RETRY、VERIFICATION_ERROR；
LLM_CALL 包含 max_tokens。metrics.json 的 verification_retries 记录内部重试次数，
verification_failures 只统计返回修复循环的验证拒绝。每次重试的调用和 token 仍计入总数。
验证上下文增加首轮和近期测试摘要（最多 10 项），用于核对失败→通过的过程。

## Phase 4 本地交付

只有生产 Verifier 通过后，ResultBuilder 才生成以下文件：

- pr-title.txt：Issue 第一条有效行生成的标题，最多 100 个字符。
- pr-description.md：任务、成功审查的改动说明、实际变更文件、测试命令、真实测试数量、历次测试结果。
- task.diff：相对于任务开始时的变化，包含新增文件。
- git.diff：只读 Git 差异，可能包括任务开始前已有的改动；审阅本次任务以 task.diff 为准。

成功或失败都会保存 result.json，CLI 输出与它完全一致，含状态、错误、产物路径和 metrics。
changed_files 是最终实际变化列表，files_modified 是工具曾写入的历史列表，两者可能不同。
metrics.final_changed_files 统计交付时实际变化数量。失败任务不会生成 PR 文案。

文案用确定性模板，不额外调用 LLM；摘要复用最后一次成功验证的 reason，标注为
Model-reviewed change summary。它是模型对代码差异的审查说明，仍需要人工审阅，
不是把执行模型的“我完成了”直接写进交付物。旧记录缺少结构化说明时回退到基础模板。
Verification 和 Test history 中的数量始终来自实际测试记录，不从模型文字提取。
标题仍取 Issue 首行，提交前可以人工润色。产物路径相对于目标仓库根目录。
交付前再次核对源码指纹，防止测试后源码变化还生成成功产物。
现有日志不会被覆盖，也不会对旧任务自动补发 PR 文案。
