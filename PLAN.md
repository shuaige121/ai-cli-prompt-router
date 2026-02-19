# OpenClaw v2 — 多机远程终端控制中心

## 核心洞察

所有功能的底层就一个东西：**可认证的远程 PTY (伪终端)**。

```
LLM 对话    = 终端里跑 claude / ollama run / curl api
系统监控    = 终端里跑 top / nvidia-smi / df -h → parse
SSH 互联    = 终端里跑 ssh-keygen + ssh-copy-id
WOL 唤醒    = 终端里跑 wakeonlan / etherwake
项目状态    = 终端里跑 git status / git log
安装软件    = 终端里跑 apt install / brew install
任何操作    = 终端里跑任何命令
```

不搞花架子，不给每个功能写独立模块。
底层做好一个远程终端，上层全是 "跑个命令然后 parse 输出"。

---

## 架构：3 层，6 个模块

```
┌──────────────────────────────────────────────────┐
│                  UI Layer                         │
│  Controller 面板 (Web/Electron/RN)               │
│  多机列表 · 终端窗口 · 状态卡片 · LLM 对话       │
└──────────────────┬───────────────────────────────┘
                   │ WebSocket
┌──────────────────▼───────────────────────────────┐
│              Relay Layer                          │
│  Rendezvous Server (发现 + 信令 + 中继)           │
│  RustDesk 模式: LAN直连 > 打洞 > Relay           │
└──────────────────┬───────────────────────────────┘
                   │ WebSocket + heartbeat
┌──────────────────▼───────────────────────────────┐
│              Agent Layer                          │
│  每台机器跑一个 daemon                            │
│  核心能力 = 认证 + 开 PTY + 流式 I/O             │
│  附加能力 = 心跳上报 (自动跑采集命令)             │
└──────────────────────────────────────────────────┘
```

---

## Module 1: Agent Daemon — 远程 PTY 服务

**这是整个系统的基石。做好这一个，其他全是上层。**

```
agent/
  src/
    index.ts          # 入口：启动 + 连接 server
    pty.ts            # 核心：node-pty 开终端，流式 I/O
    auth.ts           # 认证：token 验证，角色检查
    heartbeat.ts      # 心跳：定期跑采集命令，上报 JSON
    platform.ts       # 平台适配：安装为系统服务
  package.json        # 依赖: node-pty, ws, msgpack
```

### pty.ts — 核心 50 行逻辑

```typescript
// 伪代码，展示核心有多简单
import { spawn } from "node-pty";

function createSession(ws, { cols, rows, cwd, cmd }) {
  const shell = cmd || process.env.SHELL || "bash";
  const pty = spawn(shell, [], { cols, rows, cwd });

  // PTY stdout → WebSocket
  pty.onData((data) => ws.send(msgpack.encode({ t: "o", d: data })));

  // WebSocket → PTY stdin
  ws.on("message", (raw) => {
    const msg = msgpack.decode(raw);
    if (msg.t === "i") pty.write(msg.d);        // input
    if (msg.t === "r") pty.resize(msg.c, msg.r); // resize
  });

  pty.onExit(() => ws.send(msgpack.encode({ t: "x" })));
  ws.on("close", () => pty.kill());
}
```

就这么多。一个真正的终端，支持 vim/tmux/htop/任何交互式程序。
不是 `exec` 跑命令然后拼字符串，是真 PTY。

### heartbeat.ts — 系统状态 = 跑命令

```typescript
// 不写采集模块，直接跑命令 parse
async function collectStats() {
  const cpu = await exec("top -bn1 | head -5");    // 或 /proc/stat
  const gpu = await exec("nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits").catch(() => null);
  const ram = { total: os.totalmem(), free: os.freemem() };
  const disk = await exec("df -h --output=target,size,used,avail,pcent | tail -n+2");
  const hostname = os.hostname();
  const platform = `${os.platform()} ${os.arch()} ${os.release()}`;

  return { cpu, gpu, ram, disk, hostname, platform, ts: Date.now() };
}

// 每 10s 发一次
setInterval(() => server.send(msgpack.encode({ t: "hb", stats: collectStats() })), 10000);
```

无 GPU？`nvidia-smi` catch 掉就完了。无需 "graceful fallback 模块"。

### QA

```
□ Agent 启动，连上 Server，心跳正常
□ Controller 连上 Agent，打开终端，能跑 vim
□ 终端 resize 正确
□ 断网重连后终端 session 还在（可选：tmux attach）
□ nvidia-smi 不存在不 crash
□ Windows / Mac / Linux 三平台 node-pty 编译通过
```

---

## Module 2: Relay Server — 发现 + 信令 + 中继

```
server/
  src/
    index.ts          # 入口
    registry.ts       # 在线 Agent 注册表 (内存 Map + SQLite 持久化)
    broker.ts         # 连接协商 (LAN > punch > relay)
    relay.ts          # 中继转发 (两个 WS 对接，pipe bytes)
    wol.ts            # WOL: 找同子网 Agent 代发 magic packet
  package.json
  Dockerfile
```

### registry.ts — 就一个 Map

```typescript
const peers = new Map<string, {
  id: string,
  ws: WebSocket,
  ip: string,
  nat: string,
  stats: SystemStats,  // 最近一次心跳的数据
  lastSeen: number,
  subnet: string,      // 用于 WOL 和 LAN 判断
}>();

// Agent 心跳进来 → 更新 Map
// Controller 查询 → 返回 peers 列表
// 30s 没心跳 → 标记 offline
```

### relay.ts — 最简中继

```typescript
// 当 P2P 打洞失败，两端都连到 Relay
// 按 sessionUUID 配对，然后 pipe
const waiting = new Map<string, WebSocket>();

function handleRelay(ws, sessionId) {
  const other = waiting.get(sessionId);
  if (other) {
    waiting.delete(sessionId);
    // 双向 pipe
    ws.on("message", (d) => other.send(d));
    other.on("message", (d) => ws.send(d));
  } else {
    waiting.set(sessionId, ws);
  }
}
```

### wol.ts — 代理唤醒

```typescript
// Controller 要唤醒某台机器
// Server 找到同子网在线的 Agent → 命令它跑:
//   echo -e '\xff\xff\xff\xff\xff\xff' + MAC*16 | socat - UDP-DATAGRAM:255.255.255.255:9
// 或者 Agent 上有 wakeonlan 命令直接用
```

### QA

```
□ 100 Agent 注册，查询延迟 < 5ms
□ Agent 掉线 30s 后正确标记
□ Relay 配对正确，双向 pipe 无丢数据
□ WOL 指令能到达同子网 Agent
□ Server docker 跑起来占用 < 50MB RAM
```

---

## Module 3: Protocol — 共享消息格式

```
packages/protocol/
  src/
    messages.ts       # 所有消息类型
    codec.ts          # msgpack encode/decode
```

**不用 protobuf，用 msgpack。** 原因：
- 无需编译 .proto 文件
- JS 生态 msgpack-lite 零依赖
- 消息简单，不需要 schema 验证

```typescript
// 全部消息类型，就这些:

// Agent → Server
{ t: "reg", id, pub_key, nat }           // 注册
{ t: "hb", stats }                        // 心跳 + 系统状态

// Controller → Server
{ t: "list" }                             // 查询所有 Agent
{ t: "connect", target_id }               // 请求连接某 Agent
{ t: "wol", target_mac, subnet }          // 唤醒

// Server → Controller
{ t: "peers", list: [...] }               // Agent 列表
{ t: "route", strategy, addr }            // 连接路由

// PTY 通道 (Controller ↔ Agent，直连或经 Relay)
{ t: "pty.open", cols, rows, cwd }        // 开终端
{ t: "pty.i", d: "ls\n" }                // stdin
{ t: "pty.o", d: "file1 file2\n" }       // stdout
{ t: "pty.r", c: 120, r: 40 }            // resize
{ t: "pty.x" }                            // 终端退出

// 快捷指令 (Controller → Agent，走 PTY 之外的通道)
{ t: "exec", cmd, parse: "json"|"lines" } // 跑命令拿结构化输出
{ t: "exec.result", data }                 // 结果

// 认证
{ t: "auth", password_hash }              // 认证
{ t: "auth.ok", token, role }             // 成功
{ t: "auth.fail", error }                 // 失败
```

`exec` 是 `pty` 的简化版：不开交互式终端，跑完返回结果。
用于系统监控、git status 这类 "跑个命令拿数据" 的场景。

### QA

```
□ 所有消息 encode → decode 往返一致
□ 二进制数据 (PTY 输出) 不被破坏
□ 未知 t 值不 crash
□ 心跳消息 < 500 bytes
```

---

## Module 4: UI — Controller 面板

```
ui/
  packages/
    shared/            # 90% 共享代码
      components/
        Dashboard.tsx     # 机器列表 + 状态总览
        MachineCard.tsx   # 单机卡片 (CPU/RAM/GPU/Disk)
        Terminal.tsx      # xterm.js 终端组件
        ChatPanel.tsx     # LLM 对话 (= 终端的美化版)
        LLMSelector.tsx   # Provider/Model 选择器
        Settings.tsx      # 设置面板
        Login.tsx         # 认证
      hooks/
        useAgent.ts       # WebSocket 连接管理
        useTerminal.ts    # 终端 session 管理
        usePeers.ts       # Agent 列表 + 实时状态
      stores/
        auth.ts           # token + role
        peers.ts          # 机器列表状态 (zustand)
        settings.ts       # 本地设置
      api/
        client.ts         # 与 Server/Agent 通信封装
    mobile/             # React Native (Expo)
    desktop/            # Electron
    web/                # Vite (纯 Web)
```

### Dashboard — 就是一个卡片列表

```
┌──────────────────────────────────────────────────┐
│ OpenClaw                [搜索]   [+ 添加]  [⚙]  │
├──────────────────────────────────────────────────┤
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │● Win PC  │  │● Mac Mini│  │○ GPU Box │      │
│  │          │  │          │  │  (sleep)  │      │
│  │CPU  23%  │  │CPU  45%  │  │          │      │
│  │RAM  8/16 │  │RAM 12/32 │  │ [唤醒]   │      │
│  │GPU  --   │  │GPU  --   │  │RTX 4090  │      │
│  │SSD  120G │  │SSD  89G  │  │VRAM 0/24 │      │
│  │          │  │          │  │          │      │
│  │ [终端]   │  │ [终端]   │  │          │      │
│  │ [AI对话] │  │ [AI对话] │  │          │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                   │
│  在线 2/3  ·  CPU 12核  ·  RAM 48GB  ·  GPU 1张 │
└──────────────────────────────────────────────────┘
```

卡片数据全来自 heartbeat 里的 stats，不需要额外请求。

### Terminal — xterm.js 直连 PTY

```typescript
// 用 xterm.js + WebSocket 直接对接 Agent PTY
const term = new Terminal({ cols: 120, rows: 40 });
const ws = connectToAgent(agentId);

ws.send(msgpack.encode({ t: "pty.open", cols: 120, rows: 40, cwd: "/home/user" }));

term.onData((data) => ws.send(msgpack.encode({ t: "pty.i", d: data })));
ws.on("message", (raw) => {
  const msg = msgpack.decode(raw);
  if (msg.t === "pty.o") term.write(msg.d);
});
```

真终端。能跑 vim、htop、tmux、ssh，什么都行。

### ChatPanel — LLM 对话 = 终端的美化版

LLM 对话不是独立功能，是终端的语法糖：

```typescript
// "AI 对话" 本质上就是:
function sendToLLM(message, provider, model) {
  if (provider === "claude-cli") {
    // 在远程终端跑:
    exec(`claude --print "${escape(message)}"`);
  } else if (provider === "ollama") {
    exec(`ollama run ${model} "${escape(message)}"`);
  } else if (provider === "openai") {
    // 用 curl 调 API:
    exec(`curl -s https://api.openai.com/v1/chat/completions -H "Authorization: Bearer $OPENAI_API_KEY" -d '...'`);
  }
}
// 解析输出，渲染成好看的气泡
```

或者，对于 API 类 provider，直接在 UI 端调（不走终端）：

```typescript
// API 类可以直接 fetch，不需要走远程终端
async function* chatViaAPI(provider, model, messages) {
  const res = await fetch(provider.baseUrl + "/chat/completions", {
    method: "POST",
    headers: { Authorization: `Bearer ${provider.apiKey}` },
    body: JSON.stringify({ model, messages, stream: true }),
  });
  // 解析 SSE stream
  for await (const chunk of parseSSE(res.body)) {
    yield chunk.choices[0].delta.content;
  }
}
```

两种模式都支持：
1. **CLI 模式**: 通过远程终端跑 `claude` / `ollama` — 适合有 CLI 工具的
2. **API 模式**: UI 端直接 fetch — 适合 OpenAI/Anthropic/DeepSeek API

### QA

```
□ Dashboard 卡片实时更新 (5s)
□ 终端能跑 vim，resize 正确
□ CLI 模式 LLM 对话流式输出
□ API 模式 LLM 对话流式输出
□ 移动端终端能用 (触摸键盘)
□ 离线机器灰色 + WOL 按钮
```

---

## Module 5: Auth — 认证 + 角色

```
packages/auth/
  src/
    password.ts       # bcrypt hash/verify
    token.ts          # JWT 签发/验证
    role.ts           # controller / controlled 角色管理
    keystore.ts       # API key 加密存储
```

规则：
- Server 首次启动设管理密码
- Agent 默认 = controlled (只能被操作)
- 任何客户端输入管理密码 → 升级为 controller (看到所有机器)
- 多个 controller 可以共存
- token 7 天过期
- API key 存本地，AES 加密，密钥 = 管理密码 hash

### QA

```
□ 错误密码被拒
□ 正确密码拿到 controller token
□ token 过期后自动降级
□ API key 不明文出现在日志
```

---

## Module 6: 平台打包

```
desktop:  Electron + electron-builder (现有)
          → Windows NSIS / Mac DMG / Linux AppImage
mobile:   React Native + Expo EAS
          → iOS IPA / Android APK
web:      Vite build → 静态文件，Server 直接 serve
agent:    pkg 或 直接 node 启动
          → Windows Service / macOS LaunchAgent / Linux systemd
server:   Docker image
```

---

## 执行计划：3 个 Phase

### Phase 1: 基石 (并行)

| Contract | 做什么 | 核心产物 |
|----------|--------|----------|
| M3 Protocol | 消息格式 + codec | `@openclaw/protocol` |
| M1 Agent PTY | node-pty 开终端 + 心跳 | `agent/` 能跑 |
| M2 Server Registry | Agent 注册 + 在线列表 | `server/` 能跑 |

Phase 1 完成标志：**一个 Agent 注册到 Server，Controller 通过 Server 连上 Agent 的终端，能跑 `ls`。**

### Phase 2: 可用 (依赖 Phase 1)

| Contract | 做什么 |
|----------|--------|
| M2 Relay | P2P 打洞 + Relay 中继 |
| M4 Dashboard | 机器列表 + 状态卡片 |
| M4 Terminal | xterm.js 终端组件 |
| M4 ChatPanel | LLM 对话 (CLI + API 双模式) |
| M5 Auth | 密码 + token + 角色切换 |

Phase 2 完成标志：**多台机器在 Dashboard 显示状态，能开终端、能 AI 对话、能切 controller。**

### Phase 3: 完善

| Contract | 做什么 |
|----------|--------|
| M2 WOL | 代理唤醒 |
| M1 SSH Mesh | 自动 SSH 互信 (通过终端跑命令实现) |
| M4 Settings | 完整设置面板 |
| M6 Packaging | 全平台打包 |

Phase 3 完成标志：**全平台可安装，WOL 可用，SSH mesh 可用。**

---

## 与现有代码的关系

现有 `desktop/` 里的代码不废弃，**迁移复用**:

| 现有代码 | 迁移到 |
|----------|--------|
| `main.js` 的 `buildSpawn()` WSL/SSH 逻辑 | `agent/src/pty.ts` |
| `main.js` 的认证 + token 逻辑 | `packages/auth/` |
| `main.js` 的 WebSocket server | `server/src/` |
| `index.html` 的聊天 UI | `ui/packages/shared/ChatPanel.tsx` |
| `index.html` 的设置面板 | `ui/packages/shared/Settings.tsx` |
| `classify.py` / `backup.py` | 保留在 `router/`，Agent 启动时挂载 |

---

## 为什么这个架构更好

**之前的 plan**: 16 个 Contract，每个功能一个独立模块
- 硬件采集模块、LLM adapter 模块、SSH mesh 模块、WOL 模块...
- 每个模块都要定义接口、写适配器、处理边界情况
- 代码量大，维护成本高

**现在的 plan**: 6 个模块，核心是一个远程 PTY
- 硬件采集 = 跑 `nvidia-smi` parse 输出 (10 行代码)
- LLM 对话 = 跑 `claude --print` 或 `curl API` (20 行代码)
- SSH mesh = 跑 `ssh-keygen` + `ssh-copy-id` (5 行代码)
- WOL = 跑 `wakeonlan` (3 行代码)
- 项目状态 = 跑 `git status` (3 行代码)

**复杂度从 O(n个功能) 变成 O(1个终端)。**
新功能 = 新命令，不需要改架构。
