# Phase 3 怎么运行、怎么看代码

## 本轮增加了什么

1. Planner：根据 Issue 和 Maven/Gradle 类型生成四步计划，保存在 state.plan。
2. run_test：模型只需调用这个无参数工具，命令由程序固定选择。
3. TestRunner：运行真实测试、读取 JUnit XML、保存完整日志和失败摘要。
4. IssueVerifier：先检查本次差异和最新测试，再让模型审查需求是否完成。
5. 反馈循环：测试失败或验证失败会进入下一轮消息，Agent 继续修复，最多运行 max_steps 轮。

代码已升级 Phase 4：验证通过后会生成本地 PR 文案，但不自动提交或推送。Phase 2 的练习仓库 agent-test-repo 保持原样。

## 第一次：不花 API 费用，跑确定性的真实 Java 演示

以下路径根据这台电脑已经存在的环境填写，只影响当前 PowerShell 窗口。

```powershell
Set-Location 'C:\Users\崔佑齐\PycharmProjects\my new agent\issue-agent'
$agentPython = 'C:\Users\崔佑齐\PycharmProjects\learn-claude-code\.venv\Scripts\python.exe'
$env:JAVA_HOME = 'C:\Users\崔佑齐\.jdks\ms-17.0.17'
$mavenBin = 'C:\Users\崔佑齐\.m2\wrapper\dists\apache-maven-3.9.10-bin\53h08a94dg6djh6umvruv7q564\apache-maven-3.9.10\bin'
$env:PATH = "$mavenBin;$env:JAVA_HOME\bin;$env:PATH"
& $agentPython demo_phase3.py --repo '..\my-phase3-demo'
```

my-phase3-demo 必须是尚不存在的目录。演示脚本会复制 Java 示例、初始化 Git 并建立初始提交，
随后让 Agent 增加边界测试、运行失败、修复实现、再次测试。
初始提交是演示准备，不是 Agent 开放了 commit 工具。

预期输出：

```json
{
  "status": "completed",
  "failure_repair_confirmed": true,
  "tests": [
    {"passed": false, "tests": 4, "failures": 3, "returncode": 1},
    {"passed": true, "tests": 4, "failures": 0, "returncode": 0}
  ]
}
```

此演示使用固定模型响应；Maven 编译、JUnit 和 Git 是真实执行。
开发时成功的演示仓库位于同级 phase3-java-ready，其源码已经修好，不适合重复执行原 Issue。
phase3-java-demo 和 phase3-java-verified 保留了排查 Windows 命令引号/JDK 配置时的失败日志，
它们不是通过验收的演示结果。

## 第二次：让 DeepSeek 自己完成任务

沿用上面的 Python/JDK/Maven 设置，然后准备一个全新的未修复仓库：

```powershell
$agentRepo = 'C:\Users\崔佑齐\PycharmProjects\my new agent\phase3-deepseek-repo'
& $agentPython -c 'from pathlib import Path; from demo_phase3 import create_repository; import sys; create_repository(Path(sys.argv[1]))' $agentRepo
$env:ANTHROPIC_BASE_URL = 'https://api.deepseek.com/anthropic'
$env:ANTHROPIC_API_KEY = '填你新生成的密钥'
$env:MODEL_ID = 'deepseek-flash'
& $agentPython main.py --repo $agentRepo --issue '修改 Greeting.greet：null、空字符串、纯空格输入返回 Hello guest，普通姓名行为不变。先增加覆盖这三类边界的 JUnit 测试并运行，观察失败后再修复实现并重跑测试。不要删除或弱化测试。' --max-steps 20 --test-timeout 300
```

真实模型可能采用不同工具顺序。只有命令成功、存在本轮执行的测试、源码指纹一致，
且需求审查通过时，状态才会是 completed。若审查认为缺测试，它会反馈继续修复。
如果达到步数限制，状态为 failed，具体原因和过程看日志。不要把最后一段模型文字当作验证结论。

## 调试断点

按这个顺序看代码：

1. main.py：参数和启动环境；PyCharm 环境变量要包含上面的 JAVA_HOME、PATH 和模型配置。
2. planning/planner.py 的 create：如何生成计划。
3. core/agent.py 的 run：模型调用、工具分发和验证反馈。
4. tools/test.py 的 run：运行测试、超时终止、解析报告。
5. verification/verifier.py 的 verify：为什么通过、为什么拒绝。

Verifier 中的 review 会额外调用一次模型；它的上下文只包含原始 Issue、任务 diff 和测试摘要，
不沿用执行模型的聊天历史。仍然是单 Agent，不是子 Agent 系统。

## 日志

outputs 指向目标仓库 .task_outputs/任务ID。

- test-001.log：第一次失败的完整 Maven 输出。
- test-002.log：修复后成功的完整输出。
- task.diff：本次任务实际修改，包含新增文件。
- git.diff：Git 已跟踪文件的差异。
- state.json：Plan、工具历史、两次测试、验证反馈和最后的回复。
- events.jsonl：PLAN_CREATED、TEST_FAILED、TEST_PASSED、VERIFICATION_*、TASK_COMPLETED 等。
- metrics.json：模型/工具调用、测试失败、验证失败、Token 和耗时。

时间仍按 UTC 记录（+00:00），北京时间加 8 小时。

## 自动测试

```powershell
& $agentPython -m unittest discover -s tests -v
# 同时运行较慢的真实 Maven 集成测试：
$env:RUN_JAVA_INTEGRATION = '1'
& $agentPython -m unittest discover -s tests -v
```

Windows 没有符号链接权限时对应测试跳过。没有设置 RUN_JAVA_INTEGRATION 时真实 Maven 测试跳过。
Gradle 命令选择、报告路径和 Windows wrapper 有回归测试；本机尚未用真实 Gradle 引擎跑端到端。
当前还没有对真实 Spring Boot 项目或真实模型做完整评测。

只在可信本地仓库执行构建：cwd 限制不等于 OS 沙箱，构建插件和测试代码仍有本机用户权限。
详细限制和报告格式见 README。

## 验证器恢复补充

输出截断后验证器内部重试一次（4096 → 8192），不让执行 Agent 无谓地改代码。
JSON 格式错误也只重试一次；耗尽后返回 verification_error，先看 error 再处理模型配置。
只有测试失败或有效的需求不通过结论才进入修复循环。
新增日志事件 VERIFICATION_RETRY 会告诉你重试原因与下一次额度。

## Phase 4：完成后看哪里

启动命令不变。新任务成功后打开 outputs 目录：

1. pr-title.txt：复制为 PR 标题。
2. pr-description.md：复制为 PR 描述，含真实测试结果。
3. task.diff：检查本次任务修改。
4. result.json：查看完整状态、产物位置和 metrics。

旧的 registration-test-repo 邮箱任务已经完成，不必重复执行相同 Issue 来测试文案。
可以运行 demo_phase3.py --repo 指向新的目录；脚本名虽保留 phase3，调用的是最新 Agent，
现在也会生成 Phase 4 产物。示例使用固定模型响应、真实 Maven/JUnit，不需要 API 密钥。
