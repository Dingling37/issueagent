# Phase 1：迁移评估

本实现参考 learn-claude-code（shareAI Lab，MIT）的 Harness 思想，
保留原项目许可，不修改或搬走教学章节。当前仓库同时存在 `agents/` 示例和
`s01`–`s20` 章节目录；部分章节包含用户已有改动，均保持原样。

| 来源 | 决策 | 产品实现 |
| --- | --- | --- |
| `agents/s01_agent_loop.py`、`s01_agent_loop/code.py` 的 agent_loop | 重构循环思想 | 有限步数、显式 State、无工具调用后进入 Verifier |
| `agents/s02_tool_use.py` 的 safe_path | 强化复用思想 | Workspace 实例，拒绝越界、链接、元数据 |
| 同文件 run_read/run_edit、TOOL_HANDLERS | 重写接口 | UTF-8、唯一匹配、严格参数类型、集中错误反馈 |
| `s03_permission/code.py` 的 check_permission/check_rules | 简化 | READ/WRITE 白名单；无任意 shell，不使用危险命令子串黑名单 |
| `s05_todo_write/code.py` 的 _normalize_todos/run_todo_write | 后续参考 | Plan 存入 AgentState；Phase 3 接入结构化规划 |
| `s08_context_compact/code.py` 的 snip_compact、micro_compact、persist_large_output | 重构思想 | 保留工具配对、近期窗口、完整输出日志 |
| `s11_error_recovery/code.py` 的 RecoveryState、with_retry | 参考边界，暂不复制 | SDK 有界重试、总步数限制、失败状态落盘；不引入模型切换 |

舍弃全局 WORKDIR/client/messages、交互式教学 REPL、自由 shell、
LLM 历史总结、Hooks 框架、Memory、Skills 加载、SubAgent、Teams、MCP、后台任务。
`dependency-upgrade-agent` 是已有独立项目，本轮不耦合其工作流或服务层。

按 Phase 2 先完成可测试基础设施，后续再逐阶段接入 Planner、测试和真实完成判定。
当前完成判定默认失败，避免临时实现误导用户以为整个 MVP 已经可交付。

## Phase 3 已落地

新增 planning/planner.py、tools/test.py、workspace/changes.py、tools/git.py 和真实 IssueVerifier。
Planner 采用简单固定阶段，测试失败及验证失败均回到同一个 Agent 循环。
Git 只读和任务差异因验证需要提前实现；PR 文案仍属于 Phase 4。
保留 PendingVerifier 仅用于显式注入的基础测试，生产默认使用 IssueVerifier。
