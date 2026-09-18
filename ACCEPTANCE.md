# MVP 验收记录

整理日期：2026-09-17。范围：本地 Git 仓库、单 Agent、Java Maven/Gradle、CLI、本地 PR 文案。

## 结论

核心开发闭环已在本机两个小型真实 Java 项目中跑通：读取任务 → 修改测试 →
测试失败 → 修复业务代码 → 测试通过 → 确定性检查与模型审查 → 本地交付。
这不是对任意仓库成功率的保证，也不是操作系统级隔离认证。
下面区分真实模型运行、预设模型自动化测试和仍未验证的范围。

## 原始 15 项验收标准

| 项目 | 当前证据 | 判定与边界 |
| --- | --- | --- |
| 1. 接收 Repository + Issue | main.py 支持 --repo、--issue、--issue-file；两次真实任务 | 通过，本地仓库 |
| 2. 分析代码 | 真实任务读取源码、测试和构建文件 | 通过，小型 Java 示例 |
| 3. 制定 Plan | Planner 创建计划并写入 State/Trace | 基础实现：固定阶段计划，不是模型推导的细粒度方案 |
| 4. 读取/搜索代码 | 文件工具、search_code；基础测试及真实读文件记录 | 已实现；本次验收未单独度量搜索质量 |
| 5. 修改代码 | 两次 task.diff 都包含源码和测试变化 | 通过 |
| 6. 执行 Maven/Gradle 测试 | 两种构建工具真实执行并解析 XML | 通过，当前示例环境 |
| 7. 失败反馈给 Agent | 首轮测试失败进入 tool_result | 通过 |
| 8. 继续修改 | 两次任务均先失败后修复 | 通过 |
| 9. 阻止假完成 | 非空变更、最新测试、模型审查；拒绝后继续的自动化测试 | 机制通过；模型是否识别所有遗漏尚无系统性评测 |
| 10. 生成 diff | task.diff、git.diff | 通过；审阅本次任务优先看 task.diff |
| 11. PR Title/Description | 本地标题、描述；Gradle 运行包含审查摘要 | 通过；标题取 Issue 首行，未创建 GitHub PR |
| 12. 保存 Trace | events.jsonl、完整工具输出、测试日志、state.json | 通过；包含模型输入和仓库内容，不应随意公开 |
| 13. 基础 Metrics | 调用次数、token、测试、失败、耗时、文件数 | 通过；尚无缓存命中统计与精确费用核算 |
| 14. workspace 限制 | 路径解析、元数据隔离、链接检查及自动化测试 | 文件工具层已限制；构建子进程不具备 OS 沙箱，不能声称所有进程都无法访问仓库外 |
| 15. 最大 step | test_step_limit | 通过；验证调用及 SDK 重试不等同于执行步骤数 |

## 真实运行证据

证据保存在相邻测试仓库，以下链接依赖当前目录布局；分享产品仓库时需另行提供脱敏证据。

| 项目 | Maven 用户名任务 | Gradle 运费任务 |
| --- | --- | --- |
| 运行 ID | fd1b318420ac443a9b9f3814cfd11dfb | bb5a296a12954376888fc003b3d7b5bc |
| 最终状态 | completed | completed |
| 第一轮 | 30 个测试，6 个失败 | 8 个测试，5 个失败 |
| 最后一轮 | 30 个通过，0 跳过 | 8 个通过，0 跳过 |
| 最终修改文件 | 2 | 2 |
| 模型 / 工具调用 | 12 / 13 | 10 / 11 |
| 验证失败 / 内部重试 | 0 / 0 | 0 / 0 |
| 耗时 | 约 33 秒 | 约 63 秒 |

- Maven：[结果](../registration-test-repo/.task_outputs/fd1b318420ac443a9b9f3814cfd11dfb/result.json)、[差异](../registration-test-repo/.task_outputs/fd1b318420ac443a9b9f3814cfd11dfb/task.diff)。
- Gradle：[结果](../gradle-test-repo/.task_outputs/bb5a296a12954376888fc003b3d7b5bc/result.json)、[差异](../gradle-test-repo/.task_outputs/bb5a296a12954376888fc003b3d7b5bc/task.diff)、[PR 描述](../gradle-test-repo/.task_outputs/bb5a296a12954376888fc003b3d7b5bc/pr-description.md)。

Gradle 曾错误调用 create_file 创建已有测试文件，被拒绝后改用 edit_file。
所以其 tool_failures=2，分别是创建文件失败和首轮测试失败，并非两轮测试失败。

## 自动化测试与复现

最近一次完整测试记录：37 项，35 项通过、2 项跳过。它与业务仓库的 30/8 项测试不同。

```powershell
Set-Location 'C:\Users\崔佑齐\PycharmProjects\my new agent\issue-agent'
& 'C:\Users\崔佑齐\PycharmProjects\learn-claude-code\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

主要检查：

- 验证拒绝后继续：test_real_verifier_feedback_reenters_agent（使用预设模型结论和模拟测试报告）。
- 验证截断后提高额度重试：test_truncated_review_retries_internally_with_larger_budget。
- 重试耗尽后停止：test_review_retry_exhaustion_stops_without_more_edits。
- 最大步数：test_step_limit。
- 旧报告、零测试、全部跳过不能判成功；修改后不能复用旧测试证据。
- Windows 中文与空格路径：模拟 Wrapper 分别运行在 936/65001 代码页。
- PR 摘要取成功审查结果；失败不输出成功 PR 文案。

常见跳过项：无权限创建符号链接；未设置 RUN_JAVA_INTEGRATION=1 的真实 Maven 集成测试。
跳过不算验证通过。详细原因以 unittest 的 verbose 输出为准。
以上预设模型测试验证程序分支，不衡量真实模型的判断能力。

## 已知限制

1. 仅在可信仓库中执行构建。Maven/Gradle 本身可以执行仓库脚本，cwd 限制不等于沙箱。
2. 真实示例是普通 Java/JUnit 项目；尚未验收大型 Spring Boot、多模块、外部数据库或服务依赖。
3. 本机 Gradle 示例使用 GBK 构建 JVM 设置解决中文路径参数文件问题，源代码仍是 UTF-8。
   这是本机 Windows 兼容配置，不应原样推广到所有系统。
4. 上下文按近期消息和字符数裁剪，不是严格 token 上限；缓存优化尚未进行。
5. 模型审查可能漏判；测试通过不能证明没有缺陷。尚无独立隐藏测试、成功率基准或多次重复实验。
6. 只读任务、无代码变化任务和没有可执行测试的项目不符合当前严格成功条件。
7. 超出可审查 diff 大小的任务会被拒绝；不支持自动拆分大型任务。
8. 不支持 repo URL 克隆、恢复中断任务、实际 GitHub PR；不引入多 Agent、RAG 等扩展。

## 建议的下一步

先固化这份证据，再开展小规模 Benchmark：选择 3–5 个独立任务，固定初始提交和需求，
每次在独立副本运行，额外用不暴露给 Agent 的验收测试判断结果。
记录真实成功率、Verifier 误放行、工具误用、调用量和耗时。
有了可比较的数据后，再选择 Context、Tool Selection 或 Verifier 的优化方向。
这些属于后续工作，本记录不表示已经实现 Benchmark。
