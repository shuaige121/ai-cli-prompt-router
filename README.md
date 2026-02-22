# AI CLI Prompt Router

AI CLI 本地路由器，当前架构改为：
- 先语义检索本地 `contexts/` 语料
- 把检索结果里的命令/代码清洗掉
- 产出临时 markdown 文件（放在系统临时目录）
- 把 markdown 路径发给 LLM，用于替换用户原话里的占位符
- 使用 LoRA 小模型对用户自然语言做去噪

## 架构

```
用户消息 → [Hook] classify.py
              ├─ 语义检索 contexts/*.txt|*.md
              ├─ 清洗命令与代码片段
              ├─ 写入临时 md（/tmp/ai-cli-prompt-router/*.md）
              ├─ LoRA 小模型去噪用户输入
              └─ 输出：md 路径 + 去噪后的需求
           → 大模型（按路径读取上下文）
           → [Hook] backup.py → 静默备份会话
```

## 组件

| 文件 | 功能 |
|------|------|
| `classify.py` | UserPromptSubmit hook - 语义检索 + 去噪 + 临时 md 路径注入 |
| `backup.py` | Stop hook - 异步备份会话，Ollama 自动起标题 |
| `web.py` | Web UI - 查看会话历史 (port 8877) |
| `contexts/` | 语义检索知识库（支持 `.txt`/`.md`） |
| `history/` | 会话备份（gitignored） |

## 占位符约定

- 默认占位符：`{{CONTEXT_MD_PATH}}`
- 如果用户原话包含这个占位符，`classify.py` 会把它替换成临时 md 文件路径并发给 LLM

## 依赖

- Ollama
- 嵌入模型（默认 `nomic-embed-text`）
- 去噪模型（可用 LoRA 合并/适配模型，通过环境变量指定）

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ROUTER_EMBED_MODEL` | `nomic-embed-text` | 语义检索 embedding 模型 |
| `ROUTER_DENOISE_MODEL` | `qwen2.5:1.5b` | 去噪模型（建议换成你的 LoRA 模型） |
| `ROUTER_TEMP_DIR` | `/tmp/ai-cli-prompt-router` | 临时 markdown 输出目录 |
| `ROUTER_CONTEXT_PLACEHOLDER` | `{{CONTEXT_MD_PATH}}` | 用户原话中的替换占位符 |
| `ROUTER_TOP_K` | `3` | 检索 top-k |
| `ROUTER_MIN_SCORE` | `0.12` | 检索最低分阈值 |

## 快速开始

```bash
# 1. 安装 Ollama 并准备模型
curl -fsSL https://ollama.com/install.sh | sh
ollama pull nomic-embed-text
ollama pull qwen2.5:1.5b

# 2. 克隆项目
git clone git@github.com:shuaige121/ai-cli-prompt-router.git ~/router

# 3. 设置（可选）去噪模型为你的 LoRA 模型
export ROUTER_DENOISE_MODEL='your-lora-denoise-model'

# 4. 配置 Claude Code hooks (~/.claude/settings.json)
# 见下方示例

# 5. 启动历史查看页（可选）
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
        "timeout": 15
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
