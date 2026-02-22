#!/usr/bin/env python3
"""
Claude Code UserPromptSubmit Hook - 语义检索路由器
1. 先做语义检索，选出最相关 context 文件
2. 去掉检索内容中的命令和代码，写入临时 markdown
3. 用 LoRA 小模型做 prompt 去噪
4. 把 markdown 路径发给 LLM，用于替换原话中的占位符
"""

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

DEBUG = os.environ.get("ROUTER_DEBUG", "").lower() in ("1", "true", "yes")

ROUTER_DIR = Path(__file__).resolve().parent
CONTEXTS_DIR = ROUTER_DIR / "contexts"
TEMP_ROOT = Path(os.environ.get("ROUTER_TEMP_DIR", os.path.join(tempfile.gettempdir(), "ai-cli-prompt-router")))

OLLAMA_GENERATE_URL = os.environ.get("ROUTER_OLLAMA_GENERATE_URL", "http://localhost:11434/api/generate")
OLLAMA_EMBED_URL = os.environ.get("ROUTER_OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
OLLAMA_TIMEOUT = float(os.environ.get("ROUTER_OLLAMA_TIMEOUT", "8"))

EMBED_MODEL = os.environ.get("ROUTER_EMBED_MODEL", "nomic-embed-text")
DENOISE_MODEL = os.environ.get("ROUTER_DENOISE_MODEL", "qwen2.5:1.5b")

TOP_K = int(os.environ.get("ROUTER_TOP_K", "3"))
MIN_SCORE = float(os.environ.get("ROUTER_MIN_SCORE", "0.12"))
PLACEHOLDER = os.environ.get("ROUTER_CONTEXT_PLACEHOLDER", "{{CONTEXT_MD_PATH}}")

SUPPORTED_EXTENSIONS = {".md", ".txt"}
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.M)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
COMMAND_LINE_RE = re.compile(
    r"^(?:(?:\$|#!|>>>)\s*)?(?:sudo\s+)?(?:python3?|pip3?|npm|pnpm|yarn|npx|git|curl|wget|bash|sh|node|docker|kubectl|make|cmake|go|cargo)\b",
    re.I,
)

DENOISE_SYSTEM_PROMPT = (
    "你是一个用户需求去噪器。"
    "请删除客套话、重复描述、情绪化表达，只保留可执行目标、关键约束、实体名和占位符。"
    "保留原语言，输出单段纯文本，不要解释。"
)

NOISE_PATTERNS = [
    r"(?:^|[\s，。！!？?])请(?:问|帮忙|你)?",
    r"(?:^|[\s，。！!？?])麻烦(?:你)?",
    r"(?:^|[\s，。！!？?])帮我(?:一下|处理一下|看一下)?",
    r"(?:^|[\s，。！!？?])谢谢(?:你)?",
    r"(?:^|[\s，。！!？?])辛苦了",
    r"\b(?:please|pls|kindly|thank you|thanks)\b",
]


def debug(msg: str) -> None:
    if DEBUG:
        print(f"[router] {msg}", file=sys.stderr)


def post_json(url: str, payload: dict, timeout: float) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        debug(f"http error ({url}): {e}")
        return None


def embed_text(text: str) -> list[float] | None:
    payload = {"model": EMBED_MODEL, "prompt": text[:4000]}
    result = post_json(OLLAMA_EMBED_URL, payload, timeout=OLLAMA_TIMEOUT)
    if not result:
        return None
    vec = result.get("embedding")
    if isinstance(vec, list) and vec:
        return [float(x) for x in vec]
    return None


def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    return dot / (n1 * n2)


def tokenize(text: str) -> list[str]:
    return re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9_]+", text.lower())


def lexical_score(query: str, doc: str) -> float:
    q = set(tokenize(query))
    d = set(tokenize(doc))
    if not q or not d:
        return 0.0
    return len(q & d) / len(q)


def list_context_files() -> list[Path]:
    if not CONTEXTS_DIR.exists():
        return []
    files = []
    for p in sorted(CONTEXTS_DIR.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        files.append(p)
    return files


def semantic_retrieve(query: str) -> list[dict]:
    files = list_context_files()
    if not files:
        return []

    query_vec = embed_text(query)
    rows: list[dict] = []

    for path in files:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not text:
            continue

        score_lex = lexical_score(query, text)
        score = score_lex

        if query_vec:
            doc_vec = embed_text(text)
            if doc_vec:
                score_sem = cosine_similarity(query_vec, doc_vec)
                score = 0.8 * score_sem + 0.2 * score_lex

        rows.append({"path": path, "text": text, "score": score})

    if not rows:
        return []

    rows.sort(key=lambda x: x["score"], reverse=True)
    picked = [r for r in rows if r["score"] >= MIN_SCORE][:TOP_K]
    if not picked:
        picked = rows[:1]
    return picked


def strip_commands_and_code(text: str) -> str:
    no_fences = CODE_FENCE_RE.sub("", text)
    no_inline_code = INLINE_CODE_RE.sub("", no_fences)

    cleaned: list[str] = []
    for line in no_inline_code.splitlines():
        s = line.strip()
        if not s:
            cleaned.append("")
            continue
        if COMMAND_LINE_RE.match(s):
            continue
        cleaned.append(line)

    merged = "\n".join(cleaned)
    merged = re.sub(r"\n{3,}", "\n\n", merged).strip()
    return merged


def heuristic_denoise(text: str) -> str:
    out = text
    for pattern in NOISE_PATTERNS:
        out = re.sub(pattern, " ", out, flags=re.I)
    out = re.sub(r"\s+", " ", out).strip()
    out = out.strip("，。！？,.!?;；")
    return out


def denoise_prompt(raw_prompt: str) -> str:
    payload = {
        "model": DENOISE_MODEL,
        "prompt": raw_prompt,
        "system": DENOISE_SYSTEM_PROMPT,
        "stream": False,
        "options": {"temperature": 0, "num_predict": 180},
    }
    result = post_json(OLLAMA_GENERATE_URL, payload, timeout=OLLAMA_TIMEOUT)
    if not result:
        fallback = heuristic_denoise(raw_prompt)
        return fallback if fallback else raw_prompt.strip()

    denoised = str(result.get("response", "")).strip()
    if not denoised:
        fallback = heuristic_denoise(raw_prompt)
        return fallback if fallback else raw_prompt.strip()

    cleaned = heuristic_denoise(denoised)
    return cleaned if cleaned else denoised


def write_temp_markdown(query: str, rows: list[dict]) -> Path:
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    digest = hashlib.sha1(f"{query}-{ts}-{os.getpid()}".encode("utf-8")).hexdigest()[:8]
    out_path = TEMP_ROOT / f"ctx_{ts}_{digest}.md"

    lines = [
        "# Retrieved Context",
        "",
        f"- generated_at: {datetime.now().isoformat()}",
        f"- source_dir: {CONTEXTS_DIR}",
        "",
    ]

    if not rows:
        lines.extend(["## Result", "", "No related context found.", ""])
    else:
        for idx, row in enumerate(rows, start=1):
            lines.extend(
                [
                    f"## Source {idx}: {row['path'].name}",
                    f"- score: {row['score']:.4f}",
                    "",
                    row["cleaned_text"] or "(empty after cleanup)",
                    "",
                ]
            )

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def apply_placeholder(text: str, context_path: str) -> tuple[str, bool]:
    if PLACEHOLDER and PLACEHOLDER in text:
        return text.replace(PLACEHOLDER, context_path), True
    return text, False


def main() -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        sys.exit(0)

    debug(f"received prompt, len={len(prompt)}")
    retrieved = semantic_retrieve(prompt)
    for row in retrieved:
        row["cleaned_text"] = strip_commands_and_code(row["text"])

    temp_md = write_temp_markdown(prompt, retrieved)
    denoised = denoise_prompt(prompt)
    denoised_with_path, replaced = apply_placeholder(denoised, str(temp_md))

    source_names = ", ".join(r["path"].name for r in retrieved) if retrieved else "none"
    additional_context = "\n".join(
        [
            "[Router Output]",
            f"context_markdown_path: {temp_md}",
            f"retrieved_sources: {source_names}",
            "commands_and_code_removed: true",
            f"placeholder: {PLACEHOLDER}",
            f"placeholder_replaced: {'true' if replaced else 'false'}",
            "",
            "[Denoised Prompt]",
            denoised_with_path,
            "",
            "如果用户原话里出现占位符，请替换为 context_markdown_path 后再继续推理。",
            "如果需要上下文内容，先用 Read 工具读取该 markdown 文件。",
        ]
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    json.dump(output, sys.stdout, ensure_ascii=False)
    sys.exit(0)


if __name__ == "__main__":
    main()
