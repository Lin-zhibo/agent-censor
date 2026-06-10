# 对话记录

## 对话 1

**用户**: 阅读代码，告诉我subagent调用是如何实现的？workflow是什么样的？

**Claude**: (读取代码后给出以下分析)

---

### 一、整体架构概览

```
Claude API (主对话)
    │
    ├── query.ts (查询循环)
    │       │
    │       ├── AgentTool.call() ─── 启动 subagent
    │       │       ├── spawnTeammate() ─── 多 agent team (tmux/进程内)
    │       │       ├── FORK_AGENT ─── fork 路径 (继承父对话)
    │       │       └── 命名 agent ─── 选择特定 agent 定义
    │       │               │
    │       │               └── runAgent() ─── agent 执行引擎
    │       │                       └── query() ─── agent 自己的 API 循环
    │       │
    │       └── WorkflowTool.call() ─── 执行 workflow 脚本
    │               └── agent() / parallel() / pipeline()
    │                       └── AgentTool.call() ─── 每个 agent() 调用
    │
    └── Task System (AppState.tasks)
            ├── local_agent ─── 后台 agent
            ├── local_workflow ─── workflow 执行
            ├── local_bash ─── 后台 bash
            ├── in_process_teammate ─── 进程内 teammate
            └── Task Notification ─── 完成/进度通知
```

---

### 二、AgentTool — Subagent 调用的核心

**入口**: `src/tools/AgentTool/AgentTool.tsx` 的 `call()` 方法 (~第 250 行)

#### 2.1 参数

```typescript
// input schema
{
  description: string,        // 3-5个词的任务描述
  prompt: string,             // 给 agent 的任务指令
  subagent_type?: string,     // agent 类型 (Explore, Plan, general-purpose...)
  model?: 'sonnet'|'opus'|'haiku',  // 模型覆盖
  run_in_background?: boolean,      // 是否后台运行
  name?: string,              // 多 agent 模式下 agent 的名字
  team_name?: string,         // 多 agent 模式下的 team 名称
  mode?: PermissionMode,      // 权限模式
  isolation?: 'worktree'|'remote',  // 隔离模式
  cwd?: string                // 工作目录覆盖
}
```

#### 2.2 调用路由 (三条路径)

```
AgentTool.call()
    │
    ├── 路径1: name + team_name → spawnTeammate()
    │       创建多 agent team 中的 teammate
    │       返回: { status: 'teammate_spawned', teammate_id, ... }
    │
    ├── 路径2: FORK_SUBAGENT 开启 && 无 subagent_type → FORK_AGENT
    │       子 agent 继承父 agent 的全部对话上下文和 system prompt
    │       所有 spawn 强制异步，通过 <task-notification> 交互
    │
    └── 路径3: 正常路径 → 按 subagent_type 选择 agent 定义
            可以是 built-in / custom / plugin agent
```

#### 2.3 Agent 类型来源 (`src/tools/AgentTool/loadAgentsDir.ts`)

| 来源 | 示例 | 加载方式 |
|------|------|----------|
| **Built-in** | `Explore`, `Plan`, `general-purpose`, `claude-code-guide`, `verification` | `getBuiltInAgents()` 硬编码 |
| **Custom (Markdown)** | 用户放入 `.claude/agents/*.md` 的 agent | `loadMarkdownFilesForSubdir('agents')` |
| **Plugin** | 插件提供的 agent | `loadPluginAgents()` 动态加载 |
| **JSON (Settings)** | `settings.json` 中的 agent 定义 | `parseAgentsFromJson()` |

优先级覆盖链: `built-in < plugin < userSettings < projectSettings < flagSettings < policySettings`

#### 2.4 Agent 执行引擎 — `runAgent()` (`src/tools/AgentTool/runAgent.ts`)

这是每个 subagent 的核心执行器：

```
runAgent({
  agentDefinition,     // agent 的定义 (system prompt, tools, model...)
  promptMessages,      // 初始用户消息
  toolUseContext,      // 工具上下文 (父 agent 环境的克隆)
  isAsync,             // 是否异步
  availableTools,      // agent 可用的工具池
  ...
})
    │
    ├── 1. 解析 Agent Model (按 agent 定义 > 参数 > 父 model > 默认)
    ├── 2. 创建 agent ID
    ├── 3. 设置 transcript 子目录 (workflow 的路由到 workflows/<runId>/)
    ├── 4. 注册 Perfetto 追踪
    ├── 5. 过滤上下文消息 (去除孤儿 tool_use)
    ├── 6. 可选去除 CLAUDE.md (Explore/Plan 不需要)
    ├── 7. 覆盖权限模式 (agent 定义的可能不同于父 agent)
    ├── 8. 解析工具池 (filterToolsForAgent)
    ├── 9. 初始化 agent 特定的 MCP servers
    ├── 10. 构建 system prompt
    ├── 11. 执行 SubagentStart hooks
    ├── 12. 预加载 skills (agent 定义里声明的)
    ├── 13. 创建 subagent 工具上下文 (createSubagentContext)
    │        ├── 同步 agent: 共享 setAppState, abortController
    │        └── 异步 agent: 完全隔离
    │
    ├── 14. query() ─── Claude API 循环
    │        ├── 发送 (messages + systemPrompt + userContext + systemContext)
    │        ├── 流式接收回复
    │        ├── 处理 tool_use → 执行工具 → 发送 tool_result
    │        ├── 记录 sidechain transcript (agent 的对话持久化)
    │        └── 检查 maxTurns 限制
    │
    └── 15. finally: 清理
             ├── MCP cleanup
             ├── Session hooks cleanup
             ├── Prompt cache tracking cleanup
             ├── ReadFileState 内存释放
             ├── Perfetto 取消注册
             ├── TodoWrite 清理
             └── Shell tasks kill
```

#### 2.5 同步 vs 异步执行

**同步 (sync)**:
- `runAgent()` 直接在 for-await 循环中迭代消息
- 父 agent 的 turn 被阻塞，直到 subagent 完成
- 支持自动后台化: 2 秒后显示 "Running in background" 提示
- 通过 `Promise.race([nextMessage, backgroundPromise])` 实现自动切换

**异步 (async)**:
- `runAsyncAgentLifecycle()` 在后台运行
- 立即返回 `{ status: 'async_launched', agentId, outputFile }`
- 通过 **task notification** 系统通知父 agent 完成
- 通知格式: `<task-notification><task-id>...</task-id><status>completed</status>...</task-notification>`

#### 2.6 工具过滤 (`src/tools/AgentTool/agentToolUtils.ts`)

Subagent 可用的工具被严格限制:
- `ALL_AGENT_DISALLOWED_TOOLS`: 所有 subagent 都不能用的工具 (AgentTool, TaskOutput, ExitPlanMode, AskUserQuestion, TaskStop, WorkflowTool)
- `CUSTOM_AGENT_DISALLOWED_TOOLS`: 自定义 agent 额外不能用的
- `ASYNC_AGENT_ALLOWED_TOOLS`: 异步 agent 只能用的工具 (白名单模式)
- Agent 可以通过 `tools` / `disallowedTools` 字段进一步定制
- MCP 工具始终对所有 agent 开放

---

### 三、Workflow 实现

**位置**: `src/tools/WorkflowTool/` (被 feature flag `WORKFLOW_SCRIPTS` 控制，源码未在 sourcemap 中完整恢复)

Workflow 工具让你可以写 JavaScript 脚本来编排多个 subagent。核心概念：

#### 3.1 Workflow 脚本结构

```javascript
export const meta = {
  name: 'my-workflow',
  description: '描述',
  phases: [{ title: 'Phase1' }, { title: 'Phase2' }]
}

// 脚本体中使用以下 API:
phase('Phase1')
const result = await agent('do something', { schema: MY_SCHEMA })
```

#### 3.2 API 原语

| API | 功能 | 行为 |
|-----|------|------|
| `agent(prompt, opts)` | 启动一个 subagent | 调用 AgentTool，可选 `schema` 参数注入 `StructuredOutput` 工具 |
| `parallel(thunks)` | 并行执行多个任务 | **BARRIER** — 等待所有完成才返回 |
| `pipeline(items, stage1, stage2, ...)` | 流水线处理 | **无 BARRIER** — item A 在 stage3 时 item B 可以还在 stage1 |
| `phase(title)` | 分组标记 | 后续 agent() 在进度显示中归组 |
| `log(message)` | 日志输出 | 显示给用户 |
| `workflow(name, args)` | 嵌套另一个 workflow | 只允许一层嵌套 |
| `budget` | token 预算 | `budget.total`, `budget.spent()`, `budget.remaining()` |

#### 3.3 StructuredOutput 机制

当 `agent()` 带了 `schema` 参数时:

1. 创建一个配置了 JSON Schema 的 **SyntheticOutputTool** (`src/tools/SyntheticOutputTool/SyntheticOutputTool.ts`)
2. 这个工具被注入到 subagent 的工具列表中
3. Subagent 必须调用 `StructuredOutput` 工具返回符合 schema 的 JSON
4. `agent()` 返回的是验证过的 JSON 对象，无需手动解析

```typescript
// Workflow 中:
const bugs = await agent('Find bugs', {
  schema: { type: 'array', items: { type: 'object', properties: { ... } } }
})
// bugs 就是验证过的数组，不是字符串
```

SyntheticOutputTool 还有 WeakMap 缓存: identity-based 缓存避免了重复的 Ajv 编译，80 次调用的 workflow 从 ~110ms 降到 ~4ms。

#### 3.4 Workflow 在工具链中的位置

```
WorkflowTool
  │  (执行 JS 脚本)
  │
  ├── agent() 调用 → AgentTool.call() → runAgent()
  │       │
  │       ├── 同步调用 (pipeline 模式下)
  │       └── 后台调用 (parallel 可以真正并行)
  │
  └── 进度回到 → 父 Claude 的 Task System
          ├── local_workflow task 状态
          └── 每个 agent() → local_agent task 状态
```

#### 3.5 防递归

Workflow 不能在 subagent 中使用:
```
// constants/tools.ts
ALL_AGENT_DISALLOWED_TOOLS = new Set([
  ...
  ...(feature('WORKFLOW_SCRIPTS') ? [WORKFLOW_TOOL_NAME] : []),
])
```

---

### 四、Task System — 统一的后台任务管理

所有长期运行的操作都通过 Task 系统追踪:

```
AppState.tasks = {
  [taskId]: TaskState
}

Task 类型:
├── local_bash (b*)      — 后台 bash 命令
├── local_agent (a*)     — 后台 agent
├── remote_agent (r*)    — 远程 CCR agent
├── in_process_teammate (t*) — 进程内 teammate
├── local_workflow (w*)  — workflow 执行
├── monitor_mcp (m*)     — MCP 监控
└── dream (d*)           — dream 任务
```

**生命周期**: `pending` → `running` → `completed` / `failed` / `killed`

**通知机制** (`src/utils/task/framework.ts`):
1. 任务完成时调用 `enqueueAgentNotification()`
2. 生成 XML 格式的 `<task-notification>` 消息
3. 通过 `enqueuePendingNotification()` 注入到父 agent 的对话流中
4. 包含 `taskId`, `outputFile`, `status`, `summary`

---

### 五、多 Agent Team (Spawn via AgentTool)

当 AgentTool 收到 `name` + `team_name`:

```
AgentTool.call({ name: 'worker', team_name: 'my-team', prompt: '...' })
    │
    └── spawnTeammate() (src/tools/shared/spawnMultiAgent.ts)
            │
            ├── in-process (推荐): 同一 Node.js 进程，AsyncLocalStorage 隔离
            │       └── spawnInProcessTeammate() → startInProcessTeammate()
            │
            ├── tmux split-pane: 在 tmux 面板中启动新 claude 进程
            │       └── createTeammatePaneInSwarmView() → sendCommandToPane()
            │
            └── iTerm2 native: 原生 iTerm2 split pane
```

---

### 六、关键架构决策总结

1. **Agent 即 Tool**: Subagent 调用被建模为工具调用（AgentTool），而非独立 API。这使其无缝融入 Claude 的 tool-use 循环。

2. **双轨执行**: 同步（父阻塞等待）和异步（后台 + task notification）。系统提示词中的强制异步策略（fork、coordinator、assistant 模式）防止 UI 线程被阻塞。

3. **Transcript 持久化**: 每个 agent 的对话通过 `recordSidechainTranscript()` 独立记录，支持 resume 和调试。

4. **权限级联**: Agent 继承父的权限上下文，但可以覆盖（如 agent 定义 `permissionMode: 'acceptEdits'`）。`bubble` 模式将权限提示转发给父终端。

5. **Workflow 的本质**: Workflow 是一个确定性脚本执行器，它使用 `agent()` 调用来编排 subagent。每个 `agent()` 调用内部还是通过 AgentTool → runAgent() → query() 的同一套管道。Workflow 的价值在于组合模式（`parallel`, `pipeline`）和结构化输出（`SyntheticOutputTool` + JSON Schema 验证）。

---

## 对话 2

**用户**: 总结出agent的调用链

**Claude**: (给出 Agent 调用链全景图)

### 核心调用链 (自上而下)

```
用户输入
│
└── query()  [src/query.ts:219]
    └── queryLoop()  [src/query.ts:241]
        │  (Claude API stream → 解析 tool_use blocks)
        │
        ├── runTools(toolUseBlocks, ...)  [src/query.ts:1382]
        │   └── 匹配 tool.name === 'Agent' → AgentTool.call()
        │
        └── AgentTool.call()  [src/tools/AgentTool/AgentTool.tsx:239]
            │
            ├─ 路由判断 ──────────────────────────────────────────
            │
            ├── [name + team_name] → spawnTeammate()
            │   └── src/tools/shared/spawnMultiAgent.ts
            │       ├── in-process → spawnInProcessTeammate()
            │       │   └── startInProcessTeammate() → runAgent() → query()
            │       ├── tmux split → handleSpawnSplitPane()
            │       │   └── sendCommandToPane() → 新 claude 进程
            │       └── iTerm2 → 同上
            │
            ├── [FORK_SUBAGENT + 无 subagent_type] → FORK_AGENT
            │   └── runAgent({ useExactTools: true })
            │       (继承父 systemPrompt + 父工具池 + 父对话上下文)
            │
            └── [正常路径] → 按 subagent_type 查找 AgentDefinition
                │
                ├── 同步 (shouldRunAsync = false) ─────────────────
                │   ├── runAgent({...})  [src/tools/AgentTool/runAgent.ts:248]
                │   │   ├── 过滤工具 → filterToolsForAgent()
                │   │   ├── 解析 MCP → initializeAgentMcpServers()
                │   │   ├── 构建 systemPrompt
                │   │   ├── 注册 hooks → SubagentStart
                │   │   ├── createSubagentContext() → 隔离上下文
                │   │   └── query() → Claude API 循环
                │   │       ├── stream API → 解析消息
                │   │       ├── tool_use → runTools() → ... (递归)
                │   │       └── 记录 sidechain transcript
                │   │
                │   ├── Promise.race([agentIterator, backgroundPromise])
                │   │   └── 超时可自动切换为后台
                │   │
                │   └── finalizeAgentTool() → 提取结果文本
                │       → 返回给父 agent
                │
                └── 异步 (shouldRunAsync = true) ────────────────
                    ├── registerAsyncAgent() → 注册到 AppState.tasks
                    ├── 立即返回 { status: 'async_launched', agentId, outputFile }
                    │
                    └── (后台) runWithAgentContext() →
                        runAsyncAgentLifecycle()  [agentToolUtils.ts:508]
                        ├── runAgent({ isAsync: true, ... })
                        │   └── query() → Claude API 循环
                        ├── 进度追踪 → updateAsyncAgentProgress()
                        ├── completeAsyncAgent() → task 状态 → 'completed'
                        └── enqueueAgentNotification()
                            └── 注入 <task-notification> 到父对话
```

---

### Agent 类型解析链

```
AgentTool.call()
│
├── 1. 代码判定:
│   ├── name + team_name → 走 teammate 路径
│   ├── 无 subagent_type + fork gate 开 → FORK_AGENT
│   └── 否则 → 按 subagent_type 查找
│
├── 2. Agent 定义来源:
│   └── getAgentDefinitionsWithOverrides()  [loadAgentsDir.ts:296]
│       ├── builtInAgents.ts → 硬编码的内置 agent
│       ├── loadMarkdownFilesForSubdir('agents') → 自定义 .md agent
│       └── loadPluginAgents() → 插件 agent
│
└── 3. 优先级覆盖 (后加载覆盖先加载):
    built-in < plugin < userSettings < projectSettings < flagSettings < policySettings
```

---

### runAgent() 内部调用链 (agent 执行引擎)

```
runAgent({ agentDefinition, promptMessages, toolUseContext, isAsync, ... })
│                                                          [runAgent.ts:248]
├── getAgentModel() → 解析最终使用的模型
├── createAgentId() / registerPerfettoAgent()
├── setAgentTranscriptSubdir() → 路由到子目录 (workflow 用)
├── filterIncompleteToolCalls() → 清理孤儿 tool_use
├── cloneFileStateCache() → 克隆文件状态
├── getUserContext() / getSystemContext()
├── 可选: 去掉 CLAUDE.md (Explore/Plan)
├── 可选: 去掉 gitStatus (Explore/Plan)
├── agentGetAppState() → 覆盖权限模式
├── resolveAgentTools() / filterToolsForAgent() → 工具过滤
│   ├── ALL_AGENT_DISALLOWED_TOOLS (Agent, TaskOutput, ExitPlanMode, ...)
│   ├── CUSTOM_AGENT_DISALLOWED_TOOLS (自定义 agent 额外限制)
│   ├── ASYNC_AGENT_ALLOWED_TOOLS (异步 agent 白名单)
│   └── agent 自身的 tools / disallowedTools
├── getAgentSystemPrompt() → 构建 system prompt
├── executeSubagentStartHooks() → 生命周期钩子
├── registerFrontmatterHooks() → agent 的 frontmatter hooks
├── 预加载 skills (agent.skills)
├── initializeAgentMcpServers() → agent 专用 MCP
├── createSubagentContext() → 创建隔离的工具上下文
│   ├── 同步: shareSetAppState = true (共享)
│   └── 异步: shareSetAppState = false (隔离)
│
└── query() → [src/query.ts:219] → queryLoop()
    │                               [src/query.ts:241]
    ├── buildQueryConfig() → 查询配置
    ├── (循环) sendMessages() → Claude API
    ├── (循环) 流式接收 assistant message
    ├── (循环) 遇到 tool_use → runTools()
    │   └── 可能的递归: AgentTool.call() → runAgent() → query() ...
    ├── autoCompact → 上下文过长时压缩
    ├── maxTurns 检查 → 达到限制则 break
    └── recordSidechainTranscript() → 持久化对话
```

---

### 异步 Agent 生命周期链

```
AgentTool.call() 判断 shouldRunAsync = true
│
├── registerAsyncAgent() → task 状态: pending → running
│
├── return { status: 'async_launched', agentId, outputFile }
│   └── 父 agent 拿到 agentId 和 outputFile，可以后续查询
│
└── (后台 detached promise)
    runWithAgentContext() →
    runAsyncAgentLifecycle()                    [agentToolUtils.ts:508]
    │
    ├── 循环: runAgent() → query() → 收集 messages
    ├── updateProgressFromMessage() → 进度追踪
    ├── updateAsyncAgentProgress() → 更新 AppState
    │
    ├── 完成路径:
    │   ├── finalizeAgentTool() → 提取结果
    │   ├── completeAsyncAgent() → task 状态 → 'completed'
    │   ├── classifyHandoffIfNeeded() → 安全检查
    │   ├── getWorktreeResult() → worktree 清理
    │   └── enqueueAgentNotification()
    │       └── <task-notification>
    │           <task-id>aXXXXXXXXX</task-id>
    │           <status>completed</status>
    │           <summary>Task "..." completed successfully</summary>
    │           <output-file>/path/to/output</output-file>
    │           </task-notification>
    │       └── 注入到父 agent 的消息队列
    │
    ├── 终止路径 (AbortError):
    │   ├── killAsyncAgent() → status → 'killed'
    │   ├── extractPartialResult() → 保留部分结果
    │   └── enqueueAgentNotification({ status: 'killed', ... })
    │
    ├── 失败路径:
    │   ├── failAsyncAgent() → status → 'failed'
    │   └── enqueueAgentNotification({ status: 'failed', error, ... })
    │
    └── finally:
        ├── mcpCleanup()
        ├── clearSessionHooks()
        ├── cleanupAgentTracking()
        ├── killShellTasksForAgent()
        ├── clearAgentTranscriptSubdir()
        └── clearInvokedSkillsForAgent()
```

---

### Workflow agent() 调用链

```
WorkflowTool.call()
│  (执行用户编写的 JS 脚本)
│
├── agent(prompt, { schema })
│   │
│   ├── 如果有 schema:
│   │   └── createSyntheticOutputTool(jsonSchema)
│   │       → 注入 StructuredOutput 工具到 subagent
│   │
│   └── AgentTool.call({ prompt, subagent_type: ... })
│       └── (同上方的 AgentTool 调用链)
│
├── parallel(thunks)
│   └── Promise.all(thunks.map(fn => fn()))
│       └── 每个 thunk 内部的 agent() 并发执行
│
├── pipeline(items, stage1, stage2, ...)
│   └── 每个 item 独立流经各 stage
│       └── stage 内部调用 agent()
│
└── 结果 → workflow 脚本 return
    → WorkflowTool 返回给父 Claude
```

---

### 一图总结

```
                 User Message
                      │
                      ▼
              ┌───────────────┐
              │   query()     │  ← Claude API 主循环
              │   queryLoop() │
              └───────┬───────┘
                      │ tool_use: Agent
                      ▼
              ┌───────────────┐
              │  AgentTool    │  ← 路由: teammate | fork | 正常
              │  .call()      │
              └───────┬───────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
   spawnTeammate   FORK_AGENT   命名 Agent
   (多 agent)      (继承父)     (Explore/Plan/...)
        │             │             │
        ▼             └──────┬──────┘
   tmux/进程内               │
                      ▼
              ┌───────────────┐
              │  runAgent()   │  ← Agent 执行引擎
              │               │
              │  工具过滤      │
              │  MCP 初始化    │
              │  Hooks 注册    │
              │  Context 隔离  │
              └───────┬───────┘
                      │
                      ▼
              ┌───────────────┐
              │   query()     │  ← Subagent 的 API 循环
              │   queryLoop() │     (可递归: Agent → Agent → ...)
              └───────┬───────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
      同步返回    异步返回       Workflow
      (阻塞父)   (后台+通知)    (脚本编排)
        │             │             │
        ▼             ▼             ▼
   finalizeAgent  Task System   agent() × N
   Tool()         ┌──────────┐  ──→ 每个都是
   返回文本结果   │ pending  │      AgentTool调用
                  │ running  │
                  │completed │
                  └────┬─────┘
                       │
                       ▼
              <task-notification>
              (注入到父对话)
```

---

## 对话 3

**用户**: 我现在需要你详细地告诉我：当父agent认为需要调用子agent后，发生了什么？

**Claude**: (逐步骤详细追踪代码)

### 第一步：API 返回 `tool_use: Agent`

父 agent 的主循环在 `queryLoop()` (`src/query.ts:241`) 中运行。当 Claude API 的 streaming 响应里出现一个 `tool_use` 块，且 `name === 'Agent'` 时：

```
queryLoop()  [src/query.ts:241]
│
├── sendMessages() → Claude API (streaming)
│
├── for await (const message of apiStream) {
│     if message.type === 'assistant' {
│       assistantMessages.push(message)
│       // 提取所有 tool_use blocks:
│       const msgToolUseBlocks = message.content.filter(
│         c => c.type === 'tool_use'
│       )                // ← 这里捕获到 { name: 'Agent', input: {...} }
│       toolUseBlocks.push(...msgToolUseBlocks)
│       needsFollowUp = true      // ← 标记需要继续循环
│     }
│   }
│
├── if (needsFollowUp && toolUseBlocks.length > 0) {
│     // 执行工具调用:
│     const toolUpdates = streamingToolExecutor
│       ? streamingToolExecutor.getRemainingResults()
│       : runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)
│          // ← Agent tool 在这里被执行                [src/query.ts:1382]
│
│     for await (const update of toolUpdates) {
│       yield update.message    // ← 返回给父 agent 的上层调用者
│       toolResults.push(...)   // ← 收集 tool_result
│     }
│   }
│
├── // 将 tool_results 追加到 messages，下一轮 API 调用
│   messagesForQuery = [...messagesForQuery, ...assistantMessages, ...toolResults]
│
└── continue  // ← 回到循环顶部，继续下一轮 API 调用
```

关键点：`runTools()` 是一个 `AsyncGenerator`，父 agent 在 `for await` 中迭代它的输出。而对于 Agent 工具，这就是**阻塞点**——如果 Agent 是同步执行的，父的 `for await` 会一直等到子 agent 完成。

---

### 第二步：`runTools()` → 工具调度

```
runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)
    [src/services/tools/toolOrchestration.ts:19]
│
├── partitionToolCalls(toolUseBlocks, toolUseContext)
│   │  // 按并发安全性分区: 只读工具可以并行，非只读必须串行
│   │  // Agent tool 属于非并发安全的 (isConcurrencySafe = false)
│   └── 返回: [{ isConcurrencySafe: false, blocks: [Agent的tool_use] }]
│
└── 因为 isConcurrencySafe = false，走串行路径:
    runToolsSerially(blocks, assistantMessages, canUseTool, currentContext)
        [src/services/tools/toolOrchestration.ts:118]
    │
    └── for (const toolUse of toolUseMessages) {
          toolUseContext.setInProgressToolUseIDs(...)  // 标记"正在执行"
          
          for await (const update of runToolUse(
            toolUse,               // { name: 'Agent', input: {...} }
            assistantMessage,      // 包含这个 tool_use 的 assistant 消息
            canUseTool,
            currentContext,
          )) { ... }
          
          markToolUseAsComplete(...)  // 标记"执行完毕"
        }
```

---

### 第三步：`runToolUse()` → 权限检查 & 执行

```
runToolUse()  [src/services/tools/toolExecution.ts:337]
│
├── findToolByName(tools, 'Agent') → AgentTool 对象
│
├── tool.inputSchema.safeParse(input)
│   └── 用 Zod 验证 Agent 的输入参数
│       { description, prompt, subagent_type?, model?, run_in_background?, ... }
│
├── tool.validateInput?.(parsedInput.data, toolUseContext)
│   └── Agent 特定的业务校验
│
├── streamedCheckPermissionsAndCallTool(...)
│   └── checkPermissionsAndCallTool(...)  [src/services/tools/toolExecution.ts:599]
│       │
│       ├── 1. canUseTool() → 权限检查
│       │   ├── deny rules 检查 (Agent(Explore) 之类的禁止规则)
│       │   ├── classifier 自动判断 (auto mode)
│       │   └── 用户交互式询问 (ask mode)
│       │
│       ├── 2. executePreToolUse hooks
│       │   └── 可能修改输入参数
│       │
│       └── 3. tool.call(
│               callInput,        // 最终解析后的输入
│               { ...toolUseContext, toolUseId },
│               canUseTool,
│               assistantMessage,
│               onProgress,       // 进度回调
│             )
│             // ← 这就是进入 AgentTool.call() 的入口  [src/services/tools/toolExecution.ts:1207]
```

---

### 第四步：`AgentTool.call()` — Agent 路由

```
AgentTool.call()  [src/tools/AgentTool/AgentTool.tsx:239]
│
├── 1. 参数解构:
│   { description, prompt, subagent_type, model, run_in_background,
│     name, team_name, mode, isolation, cwd }
│
├── 2. 路由判断 ─── 三条路径选一:
│
│   ┌─ 路径A: 有 name + team_name?
│   │   └── spawnTeammate({ name, prompt, team_name, ... }, toolUseContext)
│   │       │   [src/tools/shared/spawnMultiAgent.ts]
│   │       ├── in-process → spawnInProcessTeammate()
│   │       │   └── startInProcessTeammate() → runAgent() → query()
│   │       │       (同一 Node.js 进程，AsyncLocalStorage 隔离)
│   │       ├── tmux split-pane → sendCommandToPane()
│   │       │   (新 claude 进程在独立终端面板)
│   │       └── iTerm2 native → 同上
│   │       返回: { status: 'teammate_spawned', teammate_id, ... }
│   │
│   ├─ 路径B: FORK_SUBAGENT 功能开启 且 无 subagent_type?
│   │   └── selectedAgent = FORK_AGENT
│   │       (不查 agent 注册表，直接使用 fork agent 定义)
│   │       isForkPath = true
│   │       forceAsync = true   // fork 路径强制所有 spawn 异步
│   │
│   └─ 路径C: 正常路径
│       └── 按 subagent_type 查找 AgentDefinition:
│           toolUseContext.options.agentDefinitions.activeAgents
│             .find(a => a.agentType === effectiveType)
│           │
│           ├── built-in: 'Explore', 'Plan', 'general-purpose', ...
│           ├── custom: .claude/agents/*.md
│           └── plugin: 插件注册的 agent
│           如果找不到 → throw Error
│
├── 3. MCP 检查:
│   if (selectedAgent.requiredMcpServers?.length) {
│     // 等待所需的 MCP servers 连接就绪 (最多 30s)
│     // 如果 server 没有 tools 则抛错
│   }
│
├── 4. 确定隔离模式:
│   effectiveIsolation = isolation ?? selectedAgent.isolation
│   if (effectiveIsolation === 'worktree') {
│     worktreeInfo = await createAgentWorktree(slug)
│     // 创建一个临时 git worktree，子 agent 在其中操作
│   }
│
├── 5. 组装子 agent 的工具池:
│   workerTools = assembleToolPool({
│     mode: selectedAgent.permissionMode ?? 'acceptEdits'
│   }, appState.mcp.tools)
│   // 使用子 agent 自己的权限模式重新组装工具
│   // 这确保子 agent 不受父 agent 工具限制的影响
│
├── 6. 构建 system prompt:
│   正常路径: selectedAgent.getSystemPrompt({ toolUseContext })
│             → 加上 env details 增强
│   Fork 路径: 使用父 agent 的 renderedSystemPrompt
│             (保持 byte-identical 以便 prompt cache 命中)
│
├── 7. 构建 prompt messages:
│   正常路径: [createUserMessage({ content: prompt })]
│   Fork 路径: buildForkedMessages(prompt, assistantMessage)
│             (克隆父的完整对话历史 + 子 agent 的指令)
│
├── 8. 决定 sync vs async:
│   shouldRunAsync =
│     run_in_background === true
│     || selectedAgent.background === true
│     || isCoordinator
│     || forceAsync (fork 路径下)
│     || assistantForceAsync
│     || proactiveModule.isProactiveActive()
│   && !isBackgroundTasksDisabled
│
├── 9. 分支: 异步执行 或 同步执行
│
│   ┌── 异步 ──────────────────────────────────────
│   │
│   ├── registerAsyncAgent() → 在 AppState.tasks 中注册
│   │   状态: pending → running
│   │
│   ├── rootSetAppState 写入 agentNameRegistry (供 SendMessage 路由)
│   │
│   ├── void runWithAgentContext(asyncCtx, () =>
│   │     wrapWithCwd(() =>
│   │       runAsyncAgentLifecycle({          // [agentToolUtils.ts:508]
│   │         taskId,
│   │         abortController,
│   │         makeStream: (onCacheSafeParams) =>
│   │           runAgent({...}),              // ← 子 agent 实际执行
│   │         ...
│   │       })
│   │     )
│   │   )
│   │   // 注意: void —— 不 await，fire-and-forget
│   │
│   └── 立即返回 (父 agent 继续):
│       return {
│         data: {
│           isAsync: true,
│           status: 'async_launched',
│           agentId,
│           description,
│           prompt,
│           outputFile,    // ← 父 agent 可以后续用 Read/TaskOutput 查看
│           canReadOutputFile
│         }
│       }
│
│   ┌── 同步 ──────────────────────────────────────
│   │
│   ├── registerAgentForeground() → 注册前台任务
│   │    (允许用户通过 UI 切换到后台)
│   │
│   ├── runAgent({...}) → AsyncGenerator<Message>
│   │   (这个 generator 在下方被迭代)
│   │
│   └── while (true) {
│         // Promise.race: agent 的下一条消息 vs 后台化信号
│         const raceResult = await Promise.race([
│           agentIterator.next(),      // 等待子 agent 的下一个消息
│           backgroundPromise          // 用户点击了 "后台运行"
│         ])
│         
│         if (raceResult.type === 'background') {
│           // 用户要求后台化 → 切换为异步模式继续
│           wasBackgrounded = true
│           void runAgent({ isAsync: true, ... })
│           return async_launched 结果
│         }
│         
│         // 正常情况: 收到子 agent 的消息
│         yield message  // (可选，同步 agent 也可以有进度)
│       }
│       
│       // 子 agent 完成:
│       const agentResult = finalizeAgentTool(agentMessages, agentId, metadata)
│       return { data: agentResult }
│     }
```

---

### 第五步：`runAgent()` — 子 Agent 执行引擎

```
runAgent({                                  [src/tools/AgentTool/runAgent.ts:248]
  agentDefinition,    // 子 agent 的定义
  promptMessages,     // [{ type: 'user', content: prompt }]
  toolUseContext,     // 父的上下文
  isAsync,            // 是否异步
  availableTools,     // 预组装的工作工具池
  ...
})
│
├── 1. getAgentModel() → 解析最终模型
│   优先级: agent定义.model > 参数model > 父model
│
├── 2. createAgentId() → 生成唯一 agent ID
│
├── 3. registerPerfettoAgent() → 追踪层次关系
│
├── 4. setAgentTranscriptSubdir()
│   └── 如果是 workflow 子 agent → 'workflows/<runId>/'
│
├── 5. getUserContext() / getSystemContext()
│   ├── Explore/Plan: 去掉 CLAUDE.md (省 ~5-15 Gtok/周)
│   ├── Explore/Plan: 去掉 gitStatus (省 ~1-3 Gtok/周)
│   └── 其他: 完整继承
│
├── 6. agentGetAppState() → 权限上下文覆盖
│   ├── 如果 agent 定义了 permissionMode: 覆盖
│   ├── 异步 agent: shouldAvoidPermissionPrompts = true
│   ├── allowedTools 提供时: 替换 session 级别 allow rules
│   └── 继承 effort 级别
│
├── 7. resolveAgentTools() → 工具过滤
│   ├── filterToolsForAgent() 过滤掉 ALL_AGENT_DISALLOWED_TOOLS
│   ├── 应用 agent 的 disallowedTools
│   ├── 应用 agent 的 tools (白名单)
│   └── dedup by name
│
├── 8. getAgentSystemPrompt() → 构建 system prompt
│   正常: agentDefinition.getSystemPrompt() + envDetails
│   Fork: 覆盖为父的 systemPrompt (cache-identical)
│
├── 9. executeSubagentStartHooks() → 生命周期钩子
│   └── 结果作为 additionalContext 追加到 initialMessages
│
├── 10. registerFrontmatterHooks() → agent 的 frontmatter hooks
│    └── Stop hooks 转换为 SubagentStop hooks
│
├── 11. 预加载 skills → agent.skills
│    └── 每个 skill 的内容作为 user message 注入
│
├── 12. initializeAgentMcpServers() → agent 专属 MCP
│    ├── 字符串引用 → 共享已有连接
│    └── 内联定义 → 创建新连接 (agent 结束时会清理)
│
├── 13. createSubagentContext() → 创建隔离的工具上下文
│    ├── 同步: shareSetAppState = true (共享状态)
│    ├── 异步: shareSetAppState = false (完全隔离)
│    ├── thinkingConfig: fork → 继承, 正常 → disabled
│    ├── isNonInteractiveSession: 异步 → true
│    └── 克隆 readFileState
│
├── 14. recordSidechainTranscript() → 持久化初始消息
│    writeAgentMetadata() → 持久化 agentType, worktreePath
│
├── 15. query() → 子 agent 的 API 循环
│    for await (const message of query({
│      messages: initialMessages,
│      systemPrompt: agentSystemPrompt,
│      userContext: resolvedUserContext,
│      systemContext: resolvedSystemContext,
│      canUseTool,
│      toolUseContext: agentToolUseContext,   // ← 用隔离后的上下文
│      querySource,
│      maxTurns: agentDefinition.maxTurns,
│    })) {
│      // 处理流式消息
│      // 如果消息中有 tool_use → 递归: runTools() → ... → AgentTool.call()
│      // 记录 sidechain transcript
│      yield message
│    }
│
├── 16. finally: 清理
│    ├── mcpCleanup()            // 断开 agent 专属 MCP
│    ├── clearSessionHooks()     // 清除 agent 的 hooks
│    ├── cleanupAgentTracking()  // 清除 prompt cache 追踪
│    ├── readFileState.clear()   // 释放文件缓存
│    ├── unregisterPerfettoAgent()
│    ├── clearAgentTranscriptSubdir()
│    ├── TodoWrite 清理
│    └── killShellTasksForAgent() // 杀 agent 留下的后台 shell
```

---

### 第六步：异步 Agent 完成后的通知

```
(在后台)
runAsyncAgentLifecycle()  [src/tools/AgentTool/agentToolUtils.ts:508]
│
├── for await (const message of makeStream(onCacheSafeParams)) {
│     agentMessages.push(message)
│     updateProgressFromMessage()   // 更新 token / tool use 计数
│     updateAsyncAgentProgress()    // 更新 AppState 中的进度
│     emitTaskProgress()            // 发 SDK progress 事件
│   }
│
├── 完成:
│   ├── finalizeAgentTool() → 提取文本内容、统计信息
│   ├── completeAsyncAgent() → 设置 task 状态为 'completed'
│   ├── classifyHandoffIfNeeded() → (auto mode) 安全检查
│   └── enqueueAgentNotification({
│         taskId, description, status: 'completed',
│         finalMessage: agent的文本输出,
│         usage: { totalTokens, toolUses, durationMs }
│       })
│       └── 生成 XML 消息:
│           <task-notification>
│             <task-id>aXXXXXXXX</task-id>
│             <status>completed</status>
│             <summary>Task "..." completed successfully</summary>
│             <output-file>/path/to/output</output-file>
│           </task-notification>
│       └── enqueuePendingNotification()
│           └── 注入到父 agent 的消息队列
│               → 下次父 agent 调用 API 时，这个消息会出现在 messages 中
│
├── 被 kill (AbortError):
│   ├── killAsyncAgent() → status = 'killed'
│   ├── extractPartialResult() → 保留已生成的部分输出
│   └── enqueueAgentNotification({ status: 'killed', partialResult })
│
└── 失败:
    ├── failAsyncAgent() → status = 'failed'
    └── enqueueAgentNotification({ status: 'failed', error })
```

---

### 完整调用链一览图

```
(1) Claude API 返回 tool_use: Agent
        │
        ▼
(2) queryLoop() 捕获 tool_use block
        │
        ▼
(3) runTools() → partitionToolCalls() → runToolsSerially()
        │
        ▼
(4) runToolUse() → findToolByName('Agent') → 权限检查
        │
        ▼
(5) tool.call() → AgentTool.call()
        │
        ├── spawnTeammate() ────→ tmux/进程内新 claude 实例
        │
        ├── FORK_AGENT ────→ forceAsync + 继承父上下文
        │
        └── 命名 Agent ──────┬── 同步: runAgent() → query()
                             │       │
                             │       ├── for await 迭代子 agent 输出
                             │       ├── Promise.race 可切换后台
                             │       └── finalizeAgentTool() 返回结果
                             │
                             └── 异步: registerAsyncAgent()
                                     │
                                     ├── 立即返回 async_launched
                                     │
                                     └── (后台) runAsyncAgentLifecycle()
                                         └── runAgent() → query()
                                             └── 完成/失败/kill
                                                 └── enqueueAgentNotification()
                                                     └── <task-notification>
                                                         注入父 agent 消息流
```

**核心要点**：
- **同步 agent** 会阻塞父 agent 的 `for await` 循环，直到子 agent 完成所有 turn
- **异步 agent** 是 fire-and-forget，父 agent 立即拿到 `agentId` 和 `outputFile`，后续通过 `<task-notification>` 获知完成
- 子 agent 的 `query()` 循环和父 agent 用的是**同一套代码**（`query.ts`），只是用了不同的 `toolUseContext`（隔离的上下文）和过滤后的工具集
- 每个子 agent 的对话都会作为 **sidechain transcript** 独立持久化，支持 resume

---

## 对话 4

**用户**: 我需要你展示调用链上的API交互全程：
1. 父agent向API提交了？
2. API返回了？
3. 子agent向API提交了？
4. 子API返回了？
5. 父agent是如何调起子agent的？
6. 子agent工作时，父agent在？
7. 如何将子agent的结果传回给父agent？

**Claude**: (给出完整 API 交互分析)

### 阶段一：父 Agent 发起 API 请求（决定调用子 Agent 之前）

```
┌─────────────────────────────────────────────────────────────────┐
│  父 Agent 的 queryLoop() — 第 N 轮迭代                           │
│                                                                  │
│  messagesForQuery = [                                            │
│    { role: 'user', content: '帮我重构 src/auth 模块' },           │
│    { role: 'assistant', content: [...之前的思考和工具调用...] },    │
│    { role: 'user', content: [...之前的 tool_results ...] },       │
│    ...                                                            │
│  ]                                                                │
│                                                                  │
│  最终发送给 API 的参数 (claude.ts:1822-1831):                       │
│                                                                  │
│  anthropic.beta.messages.create({                                │
│    model: 'claude-sonnet-4-6',                                   │
│    stream: true,                                                 │
│    max_tokens: 16384,                                            │
│    messages: addCacheBreakpoints(messagesForQuery),              │
│    system: [                                                     │
│      getCLISyspromptPrefix(),           // CLI 身份头              │
│      getAttributionHeader(),            // 指纹归属                 │
│      ...systemPrompt,                   // agent 的 system prompt  │
│    ],                                                            │
│    tools: [                                                      │
│      { name: 'Agent', description: 'Launch a new agent...',      │
│        input_schema: {...} },                                     │
│      { name: 'Bash', ... },                                      │
│      { name: 'Read', ... },                                      │
│      ...所有可用工具...                                             │
│    ],                                                            │
│  })                                                               │
└─────────────────────────────────────────────────────────────────┘
```

---

### 阶段二：API 返回（父 Agent 收到 tool_use: Agent）

```
┌──────────────────────────────────────────────────────────────────┐
│  Claude API Streaming Response                                   │
│                                                                  │
│  SSE stream:                                                     │
│                                                                  │
│  event: message_start                                            │
│  data: {"message": {"id": "msg_xxx", ...}}                       │
│                                                                  │
│  event: content_block_start                                      │
│  data: {"content_block": {"type": "text", ...}}                  │
│  ...模型的思考文字...                                              │
│                                                                  │
│  event: content_block_start                                      │
│  data: {"content_block": {                                       │
│    "type": "tool_use",                                           │
│    "name": "Agent",                    ← 决定调用子 agent!        │
│    "id": "toolu_01ABCDEF...",                                    │
│  }}                                                              │
│                                                                  │
│  event: content_block_delta                                      │
│  data: {"delta": {"type": "input_json_delta",                    │
│    "partial_json": "{\"description\":\"重构auth模块\",            │
│     \"prompt\":\"请分析 src/auth 目录...\",                        │
│     \"subagent_type\":\"Explore\"}}"                              │
│  }}                                                              │
│                                                                  │
│  event: content_block_stop                                       │
│  event: message_delta { stop_reason: "tool_use", usage: {...} }  │
│  event: message_stop                                             │
└──────────────────────────────────────────────────────────────────┘

queryLoop() 中处理:
  assistantMessages.push(message)   // 保存整个 assistant 消息
  toolUseBlocks = [                  // 提取 tool_use 块
    { name: 'Agent', id: 'toolu_01ABCDEF...',
      input: { description: '重构auth模块',
               prompt: '请分析 src/auth 目录的结构和现有实现...',
               subagent_type: 'Explore' }}
  ]
  needsFollowUp = true              // 标记: 需要执行工具后继续循环
```

---

### 阶段三：父 Agent 执行 Agent Tool → 调起子 Agent

这是整个流程中最关键的部分。在 `queryLoop()` 中，`needsFollowUp=true`，所以进入工具执行阶段：

```
┌──────────────────────────────────────────────────────────────────┐
│  queryLoop() — 工具执行阶段                                       │
│  [src/query.ts:1360-1408]                                        │
│                                                                  │
│  for await (const update of runTools(toolUseBlocks, ...)) {      │
│    // toolUseBlocks = [{ name: 'Agent', ... }]                   │
│    //                                                             │
│    // runTools() 内部:                                            │
│    //   → partitionToolCalls() → Agent 是非并发安全工具           │
│    //   → runToolsSerially()                                     │
│    //     → runToolUse(toolUse)                                  │
│    //       → findToolByName('Agent') → AgentTool 对象            │
│    //       → checkPermissionsAndCallTool()                      │
│    //         → canUseTool() 权限检查                              │
│    //         → tool.call(input, toolUseContext, ...)            │
│    //           ↓                                                │
│                                                                  │
│    // ===== AgentTool.call() 内部 [AgentTool.tsx:239] =====       │
│                                                                  │
│    // 1. 解析找到 Explore agent 定义                               │
│    selectedAgent = agentDefinitions.find(                        │
│      a => a.agentType === 'Explore'                              │
│    )                                                             │
│                                                                  │
│    // 2. 组装子 agent 的工具池 (独立于父 agent)                    │
│    workerTools = assembleToolPool({                              │
│      mode: 'acceptEdits'  // Explore 的默认权限模式               │
│    }, mcpTools)                                                  │
│    // Explore agent 被限制为只读工具:                               │
│    //   Read, Grep/Glob, Bash(只读), TodoWrite                    │
│    //   (没有 Write, Edit, Agent)                                 │
│                                                                  │
│    // 3. 同步执行 (shouldRunAsync = false)                        │
│    // 调用 runAgent()                                             │
│                                                                  │
│    agentIterator = runAgent({                                    │
│      agentDefinition: EXPLORE_AGENT,                             │
│      promptMessages: [createUserMessage({                        │
│        content: '请分析 src/auth 目录的结构和现有实现...'          │
│      })],                                                        │
│      toolUseContext: parentToolUseContext,                       │
│      availableTools: workerTools,                                │
│      isAsync: false,                                             │
│    })                                                            │
│                                                                  │
│    // ===== 然后 while(true) 迭代子 agent 的输出 =====            │
│    while (true) {                                                │
│      const { result } = await Promise.race([                     │
│        agentIterator.next(),       // ← 父 agent 在这里阻塞!     │
│        backgroundPromise           // 用户可切换后台              │
│      ])                                                          │
│      if (result.done) break                                      │
│      agentMessages.push(result.value)                            │
│    }                                                             │
│    // 子 agent 完成 ← 父 agent 解除阻塞                           │
│                                                                  │
│    const agentResult = finalizeAgentTool(messages, id, meta)     │
│    return { data: agentResult }  // ← 包含子 agent 的文本输出     │
│  }                                                               │
└──────────────────────────────────────────────────────────────────┘
```

---

### 阶段四：子 Agent 的独立 API 交互

**关键点：子 agent 有自己独立的 `query()` 调用和独立的 API 请求！**

```
┌──────────────────────────────────────────────────────────────────┐
│  子 Agent (Explore) 的 runAgent() → query()                      │
│  [src/tools/AgentTool/runAgent.ts:748]                           │
│                                                                  │
│  query({                                                         │
│    messages: [                                                   │
│      // 来自父 agent 的 forkContextMessages (如果有) +            │
│      // promptMessages:                                          │
│      { role: 'user', content: '请分析 src/auth 目录的结构...' },  │
│    ],                                                            │
│    systemPrompt: [                                               │
│      // Explore agent 自己的 system prompt:                       │
│      'You are a file search specialist for Claude Code...'       │
│      '=== CRITICAL: READ-ONLY MODE ==='                          │
│      'NEVER use Bash for mkdir, touch, rm, cp, mv...'            │
│    ],                                                            │
│    userContext: { claudeMd, envDetails, ... },                   │
│    systemContext: { cwd, platform, date, gitStatus, ... },       │
│    toolUseContext: agentToolUseContext,  // 隔离的上下文           │
│    maxTurns: agentDefinition.maxTurns,                           │
│  })                                                              │
│                                                                  │
│  ===== 子 Agent 的 queryLoop() — 第 1 轮 =====                    │
│                                                                  │
│  queryModelWithStreaming() →                                     │
│  anthropic.beta.messages.create({                                │
│    model: 'claude-haiku-4-5',         // Explore 默认用 haiku    │
│    stream: true,                                                 │
│    messages: [                                                   │
│      { role: 'user',                                             │
│        content: '请分析 src/auth 目录的结构和现有实现...' },       │
│    ],                                                            │
│    system: [                                                     │
│      getCLISyspromptPrefix(),                                    │
│      'You are a file search specialist...',                      │
│      '=== CRITICAL: READ-ONLY MODE ===',                         │
│      'Available tools: Read, Grep, Glob, Bash(只读)',            │
│    ],                                                            │
│    tools: [                                                      │
│      { name: 'Read', ... },                                      │
│      { name: 'Grep', ... },                                      │
│      { name: 'Glob', ... },                                      │
│      { name: 'Bash', ... },       // 只读 bash                   │
│      // 注意: 没有 Agent, Write, Edit!                            │
│    ],                                                            │
│  })                                                              │
└──────────────────────────────────────────────────────────────────┘
```

子 Agent 的 API 交互可能是多轮的：

```
┌──────────────────────────────────────────────────────────────────┐
│  子 Agent — 第 1 轮 API 调用                                       │
│                                                                  │
│  → API Request:                                                  │
│    model: haiku,                                                 │
│    messages: [user: "请分析 src/auth 目录..."],                   │
│    tools: [Read, Grep, Glob, Bash]                               │
│                                                                  │
│  ← API Response:                                                 │
│    assistant: tool_use { name: 'Glob',                           │
│      input: { pattern: 'src/auth/**/*.ts' } }                    │
│                                                                  │
│  执行 Glob 工具 → 返回文件列表                                     │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  子 Agent — 第 2 轮 API 调用                                       │
│                                                                  │
│  → API Request:                                                  │
│    messages: [                                                   │
│      user: "请分析 src/auth 目录...",                             │
│      assistant: tool_use(Glob...),                               │
│      user: tool_result(Glob → ["auth.ts", "middleware.ts"...]),  │
│    ]                                                             │
│                                                                  │
│  ← API Response:                                                 │
│    assistant: tool_use { name: 'Read',                           │
│      input: { file_path: 'src/auth/auth.ts' } }                  │
│    assistant: tool_use { name: 'Read',                           │
│      input: { file_path: 'src/auth/middleware.ts' } }            │
│    (并行调用!)                                                    │
│                                                                  │
│  执行 Read × 2 → 返回文件内容                                     │
│                                                                  │
├──────────────────────────────────────────────────────────────────┤
│  子 Agent — 第 3 轮 API 调用                                       │
│                                                                  │
│  → API Request:                                                  │
│    messages: [                                                   │
│      user: "请分析 src/auth 目录...",                             │
│      assistant: tool_use(Glob...),                               │
│      user: tool_result(Glob → [...]),                            │
│      assistant: tool_use(Read auth.ts), tool_use(Read mw.ts),    │
│      user: tool_result(Read → "export class AuthService..."),     │
│      user: tool_result(Read → "export function authMiddleware...")│
│    ]                                                             │
│                                                                  │
│  ← API Response:                                                 │
│    assistant: text "src/auth 目录结构如下:                        │
│      - auth.ts: AuthService 类 (JWT 认证 + session 管理)          │
│      - middleware.ts: authMiddleware (Express 中间件)             │
│      - types.ts: AuthRequest, TokenPayload 类型定义              │
│      ..."                                                        │
│    stop_reason: "end_turn"                                       │
│                                                                  │
│  ← 子 agent 完成! needsFollowUp = false                          │
└──────────────────────────────────────────────────────────────────┘
```

---

### 阶段五：子 Agent 完成 → 结果传回父 Agent

```
┌──────────────────────────────────────────────────────────────────┐
│  子 Agent 返回结果流                                              │
│                                                                  │
│  ===== 在 runAgent() 内部 =====                                   │
│                                                                  │
│  query() 循环结束                                                 │
│  → 所有子 agent 消息已通过 recordSidechainTranscript() 持久化     │
│  → 子 agent 清理: mcpCleanup, hooks, todos, shell tasks...        │
│                                                                  │
│  ===== 回到 AgentTool.call() =====                                │
│                                                                  │
│  finalizeAgentTool(agentMessages, agentId, metadata)             │
│  → 从最后一个 assistant message 提取文本内容:                      │
│    content: [                                                    │
│      { type: 'text',                                             │
│        text: "src/auth 目录结构如下:\n..." }                       │
│    ]                                                             │
│  → 计算统计: totalTokens, totalToolUseCount, totalDurationMs      │
│                                                                  │
│  return {                                                        │
│    data: {                                                       │
│      agentId: "a1b2c3d4...",                                     │
│      content: [{ type: 'text', text: "src/auth 目录..." }],       │
│      totalToolUseCount: 8,                                       │
│      totalDurationMs: 3500,                                      │
│      totalTokens: 12000,                                         │
│      usage: { input_tokens: 8000, output_tokens: 4000, ... }     │
│    }                                                             │
│  }                                                               │
│                                                                  │
│  ===== 这个返回值被转换为 tool_result =====                        │
│                                                                  │
│  runToolUse() 拿到 result 后:                                     │
│  processToolResultBlock() → 格式化为 tool_result block:           │
│                                                                  │
│  {                                                               │
│    type: 'tool_result',                                          │
│    tool_use_id: 'toolu_01ABCDEF...',  // 匹配父 agent 的 tool_use│
│    content: "src/auth 目录结构如下:\n..."                          │
│  }                                                               │
│                                                                  │
│  → 包装为 user message:                                          │
│  createUserMessage({                                             │
│    content: [tool_result_block],                                 │
│    toolUseResult: "src/auth 目录...",                             │
│  })                                                              │
│                                                                  │
│  → yield 给父 agent 的 queryLoop():                               │
│  toolResults.push(userMessage)                                   │
└──────────────────────────────────────────────────────────────────┘
```

---

### 阶段六：父 Agent 继续 — 将子 Agent 结果传给 API

```
┌──────────────────────────────────────────────────────────────────┐
│  父 Agent 的 queryLoop() — 组装下一轮 API 请求                     │
│                                                                  │
│  // 子 agent 的结果现在被追加到父 agent 的对话历史中:              │
│  messages = [                                                    │
│    ...messagesForQuery,          // 之前的对话                    │
│    ...assistantMessages,         // 包含 tool_use(Agent) 的消息   │
│    ...toolResults,               // 包含子 agent 的 tool_result   │
│  ]                                                               │
│                                                                  │
│  // 现在父 agent 的对话看起来像:                                   │
│  [                                                               │
│    user: "帮我重构 src/auth 模块",                                │
│    assistant: "我来先分析一下代码结构"                              │
│      tool_use { id: 'toolu_01...', name: 'Agent',               │
│        input: { subagent_type: 'Explore',                        │
│          prompt: '请分析 src/auth 目录...' } },                   │
│    user:                                                         │
│      tool_result {                                               │
│        tool_use_id: 'toolu_01...',                               │
│        content: "src/auth 目录结构如下:\n                         │
│          - auth.ts: AuthService 类 (JWT + session)\n             │
│          - middleware.ts: authMiddleware\n                       │
│          - types.ts: AuthRequest, TokenPayload\n                 │
│          ..."                                                    │
│      }                                                           │
│  ]                                                               │
│                                                                  │
│  ===== 继续循环 → 下一轮 API 调用 =====                            │
│                                                                  │
│  anthropic.beta.messages.create({                                │
│    model: 'claude-sonnet-4-6',  // 父 agent 的模型               │
│    messages: [上述完整对话],                                      │
│    system: [...父 agent 的 system prompt...],                     │
│    tools: [...父 agent 的完整工具集 (包含 Agent!) ...],           │
│  })                                                              │
│                                                                  │
│  ← API Response:                                                 │
│    assistant: "很好，我已经了解了 src/auth 的结构。                │
│      现在让我来实现重构..."                                         │
│      tool_use { name: 'Read',  // 父 agent 也可以读文件了          │
│        input: { file_path: 'src/auth/auth.ts' } }                │
│      ...继续工作...                                                │
└──────────────────────────────────────────────────────────────────┘
```

---

### 异步 Agent 的结果传回路径（差异对比）

```
┌──────────────────────────────────────────────────────────────────┐
│  如果是异步 Agent，结果传回路径不同                                │
│                                                                  │
│  ===== AgentTool.call() 立即返回 =====                            │
│                                                                  │
│  return {                                                        │
│    data: {                                                       │
│      status: 'async_launched',                                   │
│      agentId: 'a1b2c3d4...',                                     │
│      description: '重构auth模块',                                 │
│      outputFile: '/path/to/task-output/a1b2c3d4...'              │
│    }                                                             │
│  }                                                               │
│                                                                  │
│  → 转换为 tool_result:                                            │
│  "Launched agent a1b2c3d4 in background.                         │
│   Output will be written to /path/to/task-output/a1b2c3d4...     │
│   Use TaskOutput to check progress."                             │
│                                                                  │
│  → 父 agent 继续下一轮 API 调用 (不会被阻塞!)                      │
│                                                                  │
│  ===== 后台: 子 agent 执行 =====                                  │
│                                                                  │
│  runAsyncAgentLifecycle()                                        │
│  → runAgent() → query() → ...子 agent 的 API 调用...             │
│  → 完成! completeAsyncAgent()                                    │
│  → enqueueAgentNotification()                                    │
│                                                                  │
│  ===== 通知注入 =====                                             │
│                                                                  │
│  生成 XML 消息:                                                   │
│  <task-notification>                                             │
│    <task-id>a1b2c3d4...</task-id>                                │
│    <status>completed</status>                                    │
│    <summary>Task "重构auth模块" completed successfully</summary>  │
│  </task-notification>                                            │
│                                                                  │
│  → enqueuePendingNotification()                                  │
│  → 父 agent 下一轮 queryLoop() 中通过 getAttachmentMessages()     │
│    或 command queue 获取                                          │
│  → 作为 user message 注入:                                       │
│    { role: 'user', content: [                                    │
│      { type: 'text', text: '<task-notification>...' }            │
│    ]}                                                            │
│                                                                  │
│  → 父 agent 看到通知，可以用 Read 去读 outputFile 获取结果         │
└──────────────────────────────────────────────────────────────────┘
```

---

### 六个问题的直接回答

| 问题 | 答案 |
|------|------|
| **1. 父 agent 向 API 提交了什么？** | `messages` (完整对话历史)、`system` (父 agent 的 system prompt)、`tools` (父 agent 的完整工具集，包含 Agent 工具)。格式为标准 Anthropic Messages API (`anthropic.beta.messages.create({ stream: true })`) |
| **2. API 返回了什么？** | Streaming SSE 响应，包含 `assistant` role 的消息。这个消息中有 `tool_use` content block，`name='Agent'`，`input` 包含 `description`, `prompt`, `subagent_type` 等参数 |
| **3. 子 agent 向 API 提交了什么？** | 子 agent 发起**独立的 API 调用**。提交**子 agent 自己的 system prompt**（不是父的）、**过滤后的工具集**（只读工具）、以及 `messages`（初始只有用户的 prompt message，后续会累积自己的工具调用历史） |
| **4. 子 API 返回了什么？** | 和父 agent 一样的 streaming 响应格式。子 agent 可能有自己的多轮 tool_use → tool_result 循环，直到 `stop_reason = 'end_turn'` |
| **5. 父是如何调起子的？** | 父 agent 的 `queryLoop()` 中，API 返回 `tool_use(Agent)` → 进入 `runTools()` → `runToolUse()` → `AgentTool.call()` → `runAgent()`。子 agent 本质是在父 agent 的 **同步 for-await 循环**中被执行——父 agent 阻塞等待子 agent 的 `query()` generator 完成 |
| **6. 子工作时父在做什么？** | **同步模式**：父 agent 完全阻塞在 `for await (const msg of agentIterator)` 循环中，不做任何 API 调用。子 agent 的所有消息通过这个 generator 流回父 agent，最终被父 agent 的 `queryLoop()` yield 出去。**异步模式**：父 agent 立即拿到 `async_launched` 结果继续自己的 API 循环，子和父**并发运行** |
| **7. 子结果如何传回父？** | **同步**：`finalizeAgentTool()` 提取子 agent 最后一条 assistant 消息的文本 → 封装为 `tool_result` user message → 附加到父 agent 的对话历史 → 下一轮 API 调用时发送给 Claude。**异步**：完成后生成 `<task-notification>` XML 消息 → 注入父 agent 的消息队列 → 父 agent 收到通知后可读取 outputFile |

---

## 对话 5

**用户**: 请你将我们的对话记录**完完整整**地写入./NOTE.md中，**不要修改任何东西**
