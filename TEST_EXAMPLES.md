# Phase 2 历史练习样例

> 代码已升级到 Phase 3，请优先阅读 [PHASE3.md](PHASE3.md)。
> 下文 verification_unavailable 是旧版行为；新版会要求真实测试与需求验证，无测试的读取/README任务不会标记 completed。

这些样例测试当前 Phase 2 的文件操作和循环，不代表 Maven/Gradle 测试或完整 Issue-to-PR 已实现。
建议按顺序执行，一次观察一个结果。真实模型的工具调用顺序可能不同。

## 先准备 PowerShell 环境

打开 PowerShell，复制下面四行。变量只在当前终端生效。

```powershell
Set-Location 'C:\Users\崔佑齐\PycharmProjects\my new agent\issue-agent'
$agentPython = 'C:\Users\崔佑齐\PycharmProjects\learn-claude-code\.venv\Scripts\python.exe'
$agentRepo = 'C:\Users\崔佑齐\PycharmProjects\my new agent\历史测试归档\agent-test-repo'
$agentEntry = Join-Path (Get-Location) 'main.py'
```

借用原项目的 Python 环境，实际执行的是当前目录的新 Agent。

## 样例 0：检查仓库，不调用模型

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '检查仓库' --inspect
```

预期：输出仓库路径，build_system 为 unknown，退出码为 0。
unknown 正常，因为这个练习仓库尚未配置 Maven/Gradle。不会修改文件或产生任务日志。

## 样例 1：运行自动化基础测试，不调用模型

```powershell
& $agentPython -m unittest discover -s tests -v
```

预期：看到测试列表和 OK；Windows 无权限创建符号链接时，对应测试会 skipped。
这些测试自动使用临时目录和模拟模型，不修改练习仓库。
其中包括“编辑失败 → 错误反馈 → 重新读取 → 正确修改”的确定性测试。

## 以下样例需要真实模型

先安装依赖并设置你实际可用的模型参数。不要将密钥写进仓库文件。
如果当前终端已经设置过环境变量，不必重复设置。

```powershell
& $agentPython -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = '替换为你的密钥'
$env:MODEL_ID = '替换为你实际可用的模型名称'
```

若使用兼容的代理服务，再按服务配置设置 ANTHROPIC_BASE_URL。
程序不会自动读取原项目的 .env。

## 样例 2：只读取和搜索

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '列出仓库文件，搜索 greet 方法并读取 Greeting.java，解释这个方法目前如何处理 null。不要修改或新建任何文件。' --max-steps 10
```

检查：

- files_modified 应为空。
- 日志里应有读取/搜索等工具记录；模型应该识别 null 会被字符串拼接为 Hello null。
- CLI 目前不直接打印模型的解释，查看输出目录 state.json 的 messages 或 events.jsonl。
- 模型停止后应为 verification_unavailable，退出码 2；这不是通过验证。

## 样例 3：修改 README 中的一行

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '先读取 README.md，把开头独立一行的 Hello world 改成 Hello Issue Agent。不要修改下面任务列表里的示例文字，也不要改其他文件。' --max-steps 10
```

然后查看真正的文件差异：

```powershell
git -C $agentRepo diff -- README.md
```

预期差异：

```diff
-Hello world
+Hello Issue Agent
```

README 任务列表里也出现了相同文字，所以模型应使用带上下文的唯一匹配。
如果 edit_file 报“must match exactly once”，模型应根据反馈重新组织修改。
重复运行时内容已经是目标值，不要求再次产生差异。

## 样例 4：修改 Java 方法

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '读取 src/main/java/demo/Greeting.java，修改 greet 方法：name 为 null、空字符串或全部为空白字符时返回 Hello guest；普通姓名仍返回 Hello 加一个空格加原始姓名。不要改变普通姓名的空格，不要修改其他文件。' --max-steps 15
```

```powershell
git -C $agentRepo diff -- src/main/java/demo/Greeting.java
```

人工检查时使用这些输入/输出样例：

| 输入 name | 预期返回值 |
| --- | --- |
| null | Hello guest |
| 空字符串 | Hello guest |
| 三个空格 | Hello guest |
| Alice | Hello Alice |
| Bob | Hello Bob |
| 前后带空格的 ` Alice ` | `Hello  Alice `，保留传入姓名的空格 |

这里列的是预期行为，当前 Agent 不会实际编译或执行这些断言。

## 样例 5：新建文件

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '在 docs/agent-note.txt 新建一个 UTF-8 文本文件，内容只有一行：这是 Agent 新建的测试文件。不要修改其他文件；如果目标文件已存在，先读取并说明情况，不要覆盖。' --max-steps 10
```

检查：

```powershell
Get-Content -Encoding UTF8 (Join-Path $agentRepo 'docs/agent-note.txt')
git -C $agentRepo status --short
```

预期显示新文件。注意：普通 git diff 不显示未跟踪新文件的内容，因此这里用 status 和直接读取检查。

## 样例 6：验证父目录访问限制

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '用 read_file 读取 ../boundary-probe.txt，start_line 为 1，end_line 为 5。不要改用其他路径，也不要新建或修改任何文件。如果被拒绝就说明原因并停止。' --max-steps 5
```

不必创建 boundary-probe.txt，路径校验应该在读取之前拒绝 ../。
如果模型调用工具，应在日志看到 Parent traversal 一类错误，且没有文件修改。
如果模型直接拒绝尝试，只证明模型没有执行；真正的路径拦截仍应以样例 1 的自动测试为依据。

## 样例 7：最大步数限制

```powershell
& $agentPython $agentEntry --repo $agentRepo --issue '先列出文件，再搜索 greet，再读取对应文件。不要修改文件。' --max-steps 1
```

如果模型第一轮调用工具，工具执行后就应停止为 failed，原因为 Maximum agent steps reached。
max_steps 限制的是主循环轮数，不是单次回复中的工具数量。
若模型第一轮就不调用工具，会提前进入 verification_unavailable；这是另一条预期停止路径。
自动测试中的 test_step_limit 则用模拟响应确定性验证步数上限。

## 每次运行后看什么

真实运行的 CLI 会输出 outputs，指向目标仓库下 .task_outputs/任务ID。
将实际输出路径复制到下面变量中：

```powershell
$agentOutput = '替换为本次 outputs 的完整路径'
Get-Content -Encoding UTF8 (Join-Path $agentOutput 'state.json')
Get-Content -Encoding UTF8 (Join-Path $agentOutput 'metrics.json')
Get-Content -Encoding UTF8 (Join-Path $agentOutput 'events.jsonl')
```

- state.json：状态、模型消息、读写文件列表、工具历史。
- metrics.json：模型/工具调用次数、token、耗时等。
- events.jsonl：按时间记录的调用和反馈。
- tool-*.log：完整工具输出。

成功完成一次文件修改练习，不等于整个开发任务已通过验证。
不要为了反复测试而直接清空工作目录或重置 Git；先检查差异，必要时手动恢复你刚改的那一行即可。
