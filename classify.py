#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit Hook - 本地意图路由器
1. 正则快速匹配 / Ollama fallback 分类用户意图
2. 按需注入上下文片段（代码规范、ML规则等）
3. 查询 tool-gating-mcp 语义搜索相关工具
"""

import json
import os
import re
import sys
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"
OLLAMA_TIMEOUT = 5

TOOL_GATING_URL = "http://localhost:8000/api/tools/discover"
TOOL_GATING_TIMEOUT = 5
MAX_TOOLS = 3

CONTEXTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "contexts")

# ============================================================
# 规则表: (正则, tool-gating搜索词, 要加载的上下文片段列表)
# ============================================================

# 注意: 中文字符前后 \b 不生效，含中文的模式不用 \b
RULES: list[tuple[re.Pattern, str, list[str]]] = [
    # Deck / PPT — 优先匹配，避免被其他规则截走
    (re.compile(r"(deck|ppt|幻灯片|演示文稿|presentation|slides?|keynote)", re.I),
     "", ["work", "deck"]),

    # 历史记录
    (re.compile(r"(历史记录|聊天记录|之前的对话|上次聊|history|previous.?session|past.?chat)", re.I),
     "", ["history"]),

    (re.compile(r"\b(git\s|commit|push|pull|merge|rebase|branch|PR|pull\s*request|issue|github|gh\s)", re.I),
     "git version control", ["work"]),

    (re.compile(r"(浏览器|browser|playwright|selenium|爬虫|scrape|crawl|网页|webpage|截图|screenshot)", re.I),
     "browser automation web scraping screenshot", ["work"]),

    (re.compile(r"(sql|database|数据库|postgres|mysql|sqlite|supabase|mongodb|redis)", re.I),
     "database query", ["work", "code"]),

    (re.compile(r"(文件|file|目录|directory|folder|读取|写入|创建文件|删除文件)", re.I),
     "file read write filesystem", ["work"]),

    (re.compile(r"\b(api|http|rest|graphql|endpoint|curl|fetch|webhook)\b", re.I),
     "api http request", ["work", "code"]),

    (re.compile(r"(docker|container|容器|k8s|kubernetes|deploy|部署|nginx|systemd)", re.I),
     "devops deployment container", ["work"]),

    (re.compile(r"(搜索|search|查找|find|grep|文档|documentation|library|框架|framework)", re.I),
     "search documentation code library", ["work"]),

    (re.compile(r"(test|测试|pytest|jest|unittest|spec|coverage)", re.I),
     "testing", ["work", "code"]),

    (re.compile(r"(模型|model|train|训练|inference|推理|torch|tensorflow|cuda|gpu|深度学习|deep.?learn)", re.I),
     "machine learning model training", ["work", "ml", "code"]),

    # 通用编程 — 兜底，不需要 MCP
    (re.compile(r"(函数|function|class|类|变量|variable|代码|code|编程|program|写一个|实现|implement|重构|refactor)", re.I),
     "", ["work", "code"]),
]


def regex_match(prompt: str) -> tuple[str, list[str]]:
    """正则匹配，返回 (tool-gating搜索词, 上下文片段列表)"""
    for pattern, query, contexts in RULES:
        if pattern.search(prompt):
            return query, contexts
    return "", []


# ============================================================
# 上下文片段加载
# ============================================================

HISTORY_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history", "index.json")


def load_contexts(names: list[str]) -> str:
    """加载上下文片段：静态 txt 或动态生成"""
    parts = []
    for name in names:
        if name == "history":
            parts.append(_load_history_context())
            continue
        path = os.path.join(CONTEXTS_DIR, f"{name}.txt")
        try:
            with open(path) as f:
                parts.append(f.read().strip())
        except FileNotFoundError:
            pass
    return "\n".join(p for p in parts if p)


def _load_history_context() -> str:
    """动态生成历史记录上下文"""
    try:
        with open(HISTORY_INDEX) as f:
            index = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return "[历史记录] 暂无备份记录。"

    if not index:
        return "[历史记录] 暂无备份记录。"

    entries = sorted(index.values(), key=lambda x: x.get("updated", ""), reverse=True)
    lines = [f"[历史记录] 共 {len(entries)} 条会话备份，存储在 ~/router/history/："]
    for e in entries[:10]:
        title = e.get("title", "untitled")
        created = e.get("created", "?")[:16]
        preview = " / ".join(e.get("first_messages", [])[:2])[:60]
        path = e.get("path", "")
        lines.append(f"- [{created}] {title} — {preview}  ({os.path.basename(path)})")

    lines.append("用 Read 工具读取对应 .jsonl 文件可查看完整对话。")
    return "\n".join(lines)


# ============================================================
# Ollama 意图提取（fallback）
# ============================================================

OLLAMA_SYSTEM = """分析用户输入，输出 JSON:
{"query": "英文搜索关键词(用于查找MCP工具，如无需工具则为空)", "contexts": ["需要的上下文，可选: work, code, ml"]}

规则:
- 任何需要执行任务的 → contexts 加 "work"
- 写代码/编程相关 → contexts 加 "work" 和 "code"
- AI/ML/深度学习 → contexts 加 "work", "ml", "code"
- 闲聊/问答 → query 为空, contexts 为空

例如:
- "帮我看下这个网页" → {"query": "browser navigate webpage", "contexts": ["work"]}
- "写个排序函数" → {"query": "", "contexts": ["work", "code"]}
- "训练一个分类模型" → {"query": "machine learning training", "contexts": ["work", "ml", "code"]}
- "你好" → {"query": "", "contexts": []}"""


def ollama_classify(prompt: str) -> tuple[str, list[str]]:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"用户输入：{prompt}",
        "system": OLLAMA_SYSTEM,
        "format": "json",
        "stream": False,
        "options": {"temperature": 0.1, "num_predict": 80},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read())
            parsed = json.loads(result.get("response", ""))
            return parsed.get("query", ""), parsed.get("contexts", [])
    except Exception:
        return "", []


# ============================================================
# tool-gating 查询
# ============================================================

def discover_tools(query: str) -> list[dict] | None:
    if not query:
        return None
    payload = json.dumps({"query": query, "limit": MAX_TOOLS}).encode()
    req = urllib.request.Request(
        TOOL_GATING_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TOOL_GATING_TIMEOUT) as resp:
            result = json.loads(resp.read())
            return result.get("tools", [])
    except Exception:
        return None


def format_tools_context(tools: list[dict]) -> str:
    if not tools:
        return ""
    lines = []
    for t in tools:
        if t.get("score", 0) < 0.15:
            continue
        lines.append(f"- {t['name']} (server: {t.get('server', '?')}): {t.get('description', '')}")
    if not lines:
        return ""
    return "[可用 MCP 工具]\n" + "\n".join(lines) + "\n通过 tool-gating 的 execute_tool 调用。"


# ============================================================
# Main
# ============================================================

def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt = data.get("prompt", "")
    if not prompt:
        sys.exit(0)

    # Step 1: 分类
    query, ctx_names = regex_match(prompt)
    if not query and not ctx_names:
        query, ctx_names = ollama_classify(prompt)

    # Step 2: 收集上下文
    parts = []

    # 上下文片段
    if ctx_names:
        ctx_text = load_contexts(ctx_names)
        if ctx_text:
            parts.append(ctx_text)

    # MCP 工具
    if query:
        tools = discover_tools(query)
        tools_text = format_tools_context(tools) if tools else ""
        if tools_text:
            parts.append(tools_text)

    # Step 3: 输出
    if not parts:
        sys.exit(0)

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(parts),
        }
    }
    json.dump(output, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
