# Issue Agent

独立的 Issue-to-PR Software Engineering Agent。当前完成 Phase 1 / Phase 2，
**尚不是完成全部验收条件的 MVP**。所有新增代码位于此目录，不依赖教学章节的运行时。

## 快速运行

Python 3.11+，本机安装 Git。在本目录执行：

```powershell
python -m pip install -r requirements.txt
$env:ANTHROPIC_API_KEY = "your-api-key"
$env:MODEL_ID = "your-model-id"
python main.py --repo C:\path\to\repository --issue "给注册接口增加邮箱格式校验并补充测试"
```

支持 `ANTHROPIC_BASE_URL`、`--model`、`--max-steps`（默认 20，上限 200）。
只从进程环境读取凭据，不自动加载目标仓库 `.env`。
真实运行会直接编辑指定仓库，现有修改也可能被本次任务编辑；请使用专门的任务 checkout。
不会自动提交、推送或创建 PR。

不使用模型即可检查本地 Git 仓库：

```powershell
python main.py --repo C:\path\to\repository --issue "inspect" --inspect
python -m unittest discover -s tests -v
```

Phase 2 暴露 `list_files`、`read_file`、`search_code`、`edit_file`、`create_file`。
read_file 使用 1-based 的 `start_line` / `end_line`，一次最多 400 行。
edit_file 必须恰好匹配一次；create_file 不覆盖已有文件。
工具错误通过 tool_result 返回，Agent 可以继续修复。

模型停止调用工具时进入 Verifier。当前生产 Verifier 明确返回
`verification_unavailable`，CLI 退出码为 2；绝不声称已经完成测试或整个任务。
已支持注入 Verifier，测试覆盖拒绝→反馈→继续执行路径。
真实 Maven/Gradle 测试、LLM 验证、Planner、git diff 和 PR 文案仍待后续阶段。
错误退出码为 1，循环失败/未通过验证为 2；未来真正验证成功为 0。

## 模块

```text
issue-agent/
  main.py
  issue_agent/
    core/          # AgentState、ModelClient、Agent Loop
    workspace/     # 路径边界、仓库识别
    tools/         # Schema、权限、注册表、文件与搜索工具
    context/       # 有界近期消息，保留完整工具调用/结果配对
    verification/  # 完成判定接口，Phase 2 默认拒绝完成
    trace/         # JSONL 事件、完整工具输出、状态与指标
    prompts/       # 固定系统提示
  tests/
```

使用 `issue_agent` 包避免顶层 `trace` 与 Python 标准库重名。
暂不创建没有实际实现的 Planner/TestRunner/Git 空模块。

## 数据与边界

每次运行写入目标仓库 `.task_outputs/<随机任务ID>/`：
`events.jsonl`、`tool-*.log`、`state.json`、`metrics.json`。
日志保留模型输入/响应和工具输出，可能包含仓库内容，应按源代码权限管理。
这些产物不允许模型工具访问，不自动更改目标仓库的 `.gitignore`。
后续 Git diff 必须排除该目录。

Context 保留原始 Issue、Plan、修改文件列表、最近测试摘要及近期完整交互组。
长工具输出保留头尾摘要，完整输出落盘；不使用额外 LLM 做摘要。
当前指标含模型/工具调用数、输入输出 token、工具失败、验证失败、耗时、修改文件数。
files_modified 是工具修改记录，后续 Verifier 必须用实际 diff 确认最终改动。

文件工具拒绝绝对路径、父目录遍历、Windows 数据流、符号链接、junction、硬链接、
Git 元数据访问；读写、列举和搜索都经过 Workspace。
这是单进程文件工具的路径约束，不是 OS 沙箱，不防御并发恶意替换路径。
Phase 2 不执行仓库代码。后续固定的 Maven/Gradle 命令仍会执行构建脚本，
仅设置 cwd 无法保证它们不读写外部文件；这一点必须与文件工具的边界区分。

## 后续阶段

1. Phase 3：结构化 Planner、固定测试命令、超时与测试摘要、确定性及 LLM Verifier。
2. Phase 4：任务差异与未跟踪新文件处理、PR Title/Description、完善 metrics。
3. Phase 5：Java 示例仓库端到端失败→修复验收、完善 README 与回归测试。

测试中的 ScriptedModelClient 和模拟 Verifier 只验证 Harness 行为，
不能作为真实模型与 Java 测试闭环已通过的证据。

来源与许可见 [MIGRATION.md](MIGRATION.md) 和 [LICENSE](LICENSE)。
