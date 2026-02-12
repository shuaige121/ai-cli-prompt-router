# OpenClaw Router

AI CLI 工具的本地智能路由器。用 Ollama 小模型分类用户意图，按需注入上下文和 MCP 工具，保持大模型上下文干净。

## 架构

```
用户消息 → [Hook] classify.py
              ├─ 正则快速匹配 (~1ms)
              ├─ Ollama fallback (~500ms)
              ├─ 按需注入 contexts/*.txt
              └─ 查询 tool-gating → 注入相关 MCP 工具
           → 大模型（只看到相关上下文）
           → [Hook] backup.py → 静默备份会话
```

## 组件

| 文件 | 功能 |
|------|------|
| `classify.py` | UserPromptSubmit hook - 意图分类 + 上下文/工具注入 |
| `backup.py` | Stop hook - 异步备份会话，Ollama 自动起标题 |
| `web.py` | Web UI - 查看会话历史 (port 8877) |
| `add-mcp.sh` | 快捷添加 MCP server |
| `mcp-servers.json` | tool-gating 后端 MCP 配置 |
| `contexts/` | 按需加载的上下文片段 |
| `history/` | 会话备份（gitignored） |

## 上下文片段

| 文件 | 触发条件 |
|------|---------|
| `work.txt` | 任何执行任务 |
| `code.txt` | 写代码/编程 |
| `ml.txt` | AI/ML/深度学习 |
| `deck.txt` | PPT/演示文稿 |

## 依赖

- **Ollama** + qwen2.5:1.5b（意图分类 + 会话命名）
- **tool-gating-mcp**（MCP 工具语义搜索，可选）

## 适配

核心逻辑与 AI CLI 工具无关，通过 adapter 适配不同工具的 hook 机制：
- Claude Code: `UserPromptSubmit` / `Stop` hooks
- Codex CLI / Gemini CLI: 待适配

## 快速开始

```bash
# 1. 安装 Ollama + 拉取小模型
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:1.5b

# 2. 克隆本项目
git clone git@github.com:shuaige121/openclaw-router.git ~/router

# 3. 配置 Claude Code hooks (~/.claude/settings.json)
# 见下方配置示例

# 4. 启动 Web UI
python3 ~/router/web.py
```

### Claude Code 配置

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "python3 ~/router/classify.py",
        "timeout": 10
      }]
    }],
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "python3 ~/router/backup.py",
        "timeout": 15,
        "async": true
      }]
    }]
  }
}
```
