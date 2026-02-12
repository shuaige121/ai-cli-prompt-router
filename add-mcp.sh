#!/bin/bash
# 添加新 MCP server 到 tool-gating 并重启
# 用法: add-mcp.sh <name> <command> [args...] -- [description]
#
# 示例:
#   add-mcp.sh github npx -y @modelcontextprotocol/server-github -- "GitHub API integration"
#   add-mcp.sh filesystem npx -y @modelcontextprotocol/server-filesystem /home -- "File system access"

CONFIG="$HOME/tool-gating-mcp/mcp-servers.json"

if [ $# -lt 2 ]; then
    echo "用法: $0 <name> <command> [args...] -- [description]"
    echo "示例: $0 github npx -y @modelcontextprotocol/server-github -- \"GitHub API\""
    exit 1
fi

NAME="$1"
shift

# 分离 command/args 和 description
CMD="$1"
shift
ARGS=()
DESC=""
while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then
        shift
        DESC="$*"
        break
    fi
    ARGS+=("$1")
    shift
done

# 构建 JSON args 数组
ARGS_JSON=$(printf '%s\n' "${ARGS[@]}" | python3 -c "import sys,json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")

# 用 python3 更新 JSON
python3 -c "
import json, sys
with open('$CONFIG') as f:
    config = json.load(f)
config['$NAME'] = {
    'command': '$CMD',
    'args': $ARGS_JSON,
    'env': {},
    'description': '''$DESC'''
}
with open('$CONFIG', 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
print(f'✓ 已添加 $NAME 到 {\"$CONFIG\"} ')
"

# 重启服务
sudo systemctl restart tool-gating-mcp
echo "✓ tool-gating-mcp 已重启"

# 等待启动
sleep 5
if curl -s http://localhost:8000/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'✓ 服务状态: {d[\"status\"]}')" 2>/dev/null; then
    echo "✓ 新 MCP server '$NAME' 已就绪"
else
    echo "✗ 服务启动异常，检查: sudo journalctl -u tool-gating-mcp -n 20"
fi
