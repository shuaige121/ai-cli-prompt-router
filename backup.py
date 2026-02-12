#!/usr/bin/env python3
"""
Claude Code Stop Hook - 静默会话备份
每次 Claude 回复后在后台备份 transcript，首次用 Ollama 生成会话标题。
"""

import json
import os
import shutil
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HISTORY_DIR = Path(__file__).parent / "history"
INDEX_FILE = HISTORY_DIR / "index.json"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5:1.5b"
OLLAMA_TIMEOUT = 5


def load_index() -> dict:
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            return json.load(f)
    return {}


def save_index(index: dict):
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2, ensure_ascii=False)


def extract_user_messages(transcript_path: str, limit: int = 5) -> list[str]:
    """提取前几条用户消息用于生成标题"""
    messages = []
    try:
        with open(transcript_path) as f:
            for line in f:
                d = json.loads(line)
                if d.get("type") != "user":
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    messages.append(content.strip()[:200])
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            messages.append(block["text"][:200])
                            break
                if len(messages) >= limit:
                    break
    except Exception:
        pass
    return messages


def generate_title(messages: list[str]) -> str:
    """用 Ollama 生成会话标题"""
    if not messages:
        return "untitled"

    prompt = "根据以下对话开头，生成一个简短的中文标题（10字以内），只输出标题文字：\n"
    prompt += "\n".join(f"- {m}" for m in messages[:3])

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 30},
    }).encode()

    req = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            result = json.loads(resp.read())
            title = result.get("response", "").strip().strip('"').strip("《》")
            # 清理：去掉换行，截断
            title = title.split("\n")[0][:20]
            return title if title else "untitled"
    except Exception:
        return "untitled"


def sanitize_filename(s: str) -> str:
    """清理文件名"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).strip("_")


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = data.get("session_id", "")
    transcript_path = data.get("transcript_path", "")

    if not session_id or not transcript_path or not os.path.exists(transcript_path):
        sys.exit(0)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index()

    if session_id in index:
        # 已有记录，只更新备份文件
        entry = index[session_id]
        backup_path = entry["path"]
        shutil.copy2(transcript_path, backup_path)
        entry["updated"] = datetime.now().isoformat()
        entry["size"] = os.path.getsize(transcript_path)
        save_index(index)
    else:
        # 新会话：生成标题，创建备份
        messages = extract_user_messages(transcript_path)
        title = generate_title(messages)
        date_str = datetime.now().strftime("%Y%m%d_%H%M")
        safe_title = sanitize_filename(title)
        filename = f"{date_str}_{safe_title}_{session_id[:8]}.jsonl"
        backup_path = str(HISTORY_DIR / filename)

        shutil.copy2(transcript_path, backup_path)

        index[session_id] = {
            "title": title,
            "path": backup_path,
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "size": os.path.getsize(transcript_path),
            "first_messages": messages[:3],
        }
        save_index(index)

    sys.exit(0)


if __name__ == "__main__":
    main()
