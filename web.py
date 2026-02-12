#!/usr/bin/env python3
"""
OpenClaw Router - Web UI
查看会话历史记录的简单 web 界面。
启动: python3 ~/router/web.py
访问: http://localhost:8899
"""

import json
import os
import html
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HISTORY_DIR = Path(__file__).parent / "history"
INDEX_FILE = HISTORY_DIR / "index.json"
PORT = 8877


def load_index() -> dict:
    if INDEX_FILE.exists():
        with open(INDEX_FILE) as f:
            return json.load(f)
    return {}


def parse_transcript(path: str) -> list[dict]:
    """解析 transcript JSONL 为消息列表"""
    messages = []
    try:
        with open(path) as f:
            for line in f:
                d = json.loads(line)
                msg_type = d.get("type", "")
                if msg_type not in ("user", "assistant"):
                    continue
                msg = d.get("message", {})
                role = msg.get("role", msg_type)
                content = msg.get("content", "")

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                parts.append(f"[Tool: {block.get('name', '?')}]")
                            elif block.get("type") == "tool_result":
                                parts.append("[Tool Result]")
                    text = "\n".join(parts)

                if text.strip():
                    messages.append({"role": role, "text": text.strip()})
    except Exception:
        pass
    return messages


def render_index_page() -> str:
    index = load_index()
    entries = sorted(index.values(), key=lambda x: x.get("updated", ""), reverse=True)

    rows = ""
    for e in entries:
        sid = ""
        for k, v in index.items():
            if v is e:
                sid = k
                break
        title = html.escape(e.get("title", "untitled"))
        created = e.get("created", "?")[:16].replace("T", " ")
        updated = e.get("updated", "?")[:16].replace("T", " ")
        size_kb = e.get("size", 0) // 1024
        previews = [html.escape(m[:50]) for m in e.get("first_messages", [])[:2]]
        preview = " → ".join(previews) if previews else ""
        rows += f"""
        <tr onclick="location.href='/session?id={sid}'" style="cursor:pointer">
            <td><strong>{title}</strong></td>
            <td class="dim">{preview}</td>
            <td class="mono">{created}</td>
            <td class="mono">{updated}</td>
            <td class="mono">{size_kb} KB</td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenClaw Router - History</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #c9d1d9; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
h1 {{ color: #58a6ff; margin-bottom: 8px; font-size: 24px; }}
.subtitle {{ color: #8b949e; margin-bottom: 24px; }}
table {{ width: 100%; border-collapse: collapse; }}
th {{ text-align: left; padding: 12px; color: #8b949e; border-bottom: 1px solid #21262d;
     font-size: 13px; text-transform: uppercase; letter-spacing: 0.5px; }}
td {{ padding: 12px; border-bottom: 1px solid #21262d; }}
tr:hover {{ background: #161b22; }}
.dim {{ color: #8b949e; font-size: 14px; }}
.mono {{ font-family: 'SF Mono', monospace; font-size: 13px; color: #8b949e; white-space: nowrap; }}
.empty {{ text-align: center; padding: 60px; color: #484f58; }}
</style>
</head>
<body>
<div class="container">
<h1>OpenClaw Router</h1>
<p class="subtitle">{len(entries)} sessions backed up</p>
{"<table><tr><th>Title</th><th>Preview</th><th>Created</th><th>Updated</th><th>Size</th></tr>" + rows + "</table>" if entries else '<div class="empty">No sessions yet.</div>'}
</div>
</body>
</html>"""


def render_session_page(session_id: str) -> str:
    index = load_index()
    entry = index.get(session_id)
    if not entry:
        return "<h1>Session not found</h1>"

    title = html.escape(entry.get("title", "untitled"))
    path = entry.get("path", "")
    messages = parse_transcript(path)

    msgs_html = ""
    for m in messages:
        role = m["role"]
        text = html.escape(m["text"])
        # 简单 markdown: 代码块
        text = text.replace("```", "</code></pre>" if "```" in text else "<pre><code>")
        cls = "user" if role == "user" else "assistant"
        label = "You" if role == "user" else "Claude"
        msgs_html += f"""
        <div class="msg {cls}">
            <div class="label">{label}</div>
            <div class="content">{text}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} - OpenClaw</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0d1117; color: #c9d1d9; }}
.container {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
.back {{ color: #58a6ff; text-decoration: none; font-size: 14px; }}
.back:hover {{ text-decoration: underline; }}
h1 {{ color: #e6edf3; margin: 16px 0 24px; font-size: 22px; }}
.msg {{ margin-bottom: 20px; padding: 16px; border-radius: 8px; }}
.msg.user {{ background: #161b22; border-left: 3px solid #58a6ff; }}
.msg.assistant {{ background: #0d1117; border-left: 3px solid #3fb950; }}
.label {{ font-size: 12px; font-weight: 600; margin-bottom: 8px;
         text-transform: uppercase; letter-spacing: 0.5px; }}
.msg.user .label {{ color: #58a6ff; }}
.msg.assistant .label {{ color: #3fb950; }}
.content {{ white-space: pre-wrap; word-break: break-word; line-height: 1.6; font-size: 15px; }}
pre {{ background: #161b22; padding: 12px; border-radius: 6px; overflow-x: auto;
      margin: 8px 0; }}
code {{ font-family: 'SF Mono', monospace; font-size: 13px; }}
</style>
</head>
<body>
<div class="container">
<a class="back" href="/">&larr; All Sessions</a>
<h1>{title}</h1>
{msgs_html if msgs_html else '<p style="color:#484f58">No messages found.</p>'}
</div>
</body>
</html>"""


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index":
            content = render_index_page()
        elif parsed.path == "/session":
            params = parse_qs(parsed.query)
            sid = params.get("id", [""])[0]
            content = render_session_page(sid)
        else:
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode())

    def log_message(self, format, *args):
        pass  # 静默日志


def main():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"OpenClaw Router Web UI: http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
