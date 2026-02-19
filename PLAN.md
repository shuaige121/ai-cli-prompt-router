# OpenClaw v2 — 全平台多机 AI 控制中心 架构设计

## 总览

从单机 Electron GUI 重构为 **多平台、多机器、多 LLM** 的分布式 AI 开发控制中心。

```
┌──────────────────────────────────────────────────────┐
│                    Controller UI                      │
│  (iOS / Android / Windows / Mac / Linux / Web)       │
│  看到所有机器 · 发指令 · 看状态 · 切换 LLM           │
└──────────────┬───────────────────────┬───────────────┘
               │ WebSocket/P2P         │
       ┌───────▼───────┐       ┌───────▼───────┐
       │  Main Server   │◄─────►│  Main Server   │
       │  (Rendezvous)  │       │  (Relay 备用)  │
       └───────┬───────┘       └───────────────┘
               │ heartbeat + signaling
    ┌──────────┼──────────┬──────────────┐
    ▼          ▼          ▼              ▼
┌────────┐┌────────┐┌────────┐    ┌────────┐
│ Agent  ││ Agent  ││ Agent  │... │ Agent  │
│ Win PC ││Mac Mini││Linux   │    │ GPU    │
│        ││        ││Server  │    │ Server │
└────────┘└────────┘└────────┘    └────────┘
 每个 Agent:
 - 上报 CPU/GPU/RAM/VRAM/磁盘
 - 执行 LLM 指令 (Claude/GPT/Ollama/...)
 - 自动建立 SSH 互联
 - 支持 WOL 被唤醒
 - 任何 Agent 输入管理密码可升级为 Controller
```

---

## 模块划分 & Manager 分工

### Manager A: 网络层 (Network Manager)

**职责**: 所有节点的发现、连接、通信基础设施

#### Contract A1: Rendezvous Server (信令服务器)
```
输入: 无（独立服务）
输出: 可运行的信令+中继服务器

技术栈: Node.js + ws + protobuf (或 msgpack)
端口:
  - 21116/UDP: Agent 心跳注册
  - 21116/TCP: 连接协商
  - 21117/TCP: 中继转发
  - 21118/TCP: WebSocket (给 Web/移动端)

核心功能:
  1. PeerRegistry
     - 每个 Agent 每 15s 发送心跳: { id, ip, port, nat_type, timestamp }
     - 30s 无心跳标记 offline
     - 持久化到 SQLite (peer_id, public_key, last_seen, meta)

  2. ConnectionBroker
     - Controller 请求连接 Agent B
     - 查找 B 的地址和 NAT 类型
     - 决定连接策略: LAN直连 > UDP打洞 > TCP打洞 > Relay
     - 下发连接指令给双方

  3. RelayForwarder
     - 当 P2P 失败时，双方连到 Relay
     - 按 sessionUUID 配对
     - 双向转发字节流（加密后的）

  4. WOL Proxy
     - Controller 发 WOL 请求指定 MAC
     - Server 找到同子网在线的 Agent
     - 命令该 Agent 本地发送 magic packet

文件结构:
  server/
    src/
      index.ts              # 入口
      registry.ts           # PeerRegistry: 心跳、在线状态
      broker.ts             # ConnectionBroker: 连接策略决策
      relay.ts              # RelayForwarder: 中继转发
      wol.ts                # WOL proxy 逻辑
      protocol.ts           # 消息定义 (protobuf/msgpack schema)
      db.ts                 # SQLite 持久化
    package.json
    Dockerfile

QA 检查点:
  □ 100 个模拟 Agent 心跳注册，查询延迟 < 5ms
  □ Agent 掉线 30s 后正确标记 offline
  □ LAN 内两个 Agent 能直连不走 Relay
  □ NAT 后的 Agent 能通过 Relay 通信
  □ WOL magic packet 正确发送
  □ 并发 50 个 Relay session 无内存泄漏
```

#### Contract A2: Agent Daemon (节点守护进程)
```
输入: server 地址 + agent 密钥
输出: 各平台可安装的后台服务

技术栈: Node.js (跨平台一致性，后续可选 Rust 重写热路径)

核心功能:
  1. ServerLink
     - 启动时连接 Rendezvous Server
     - 15s 心跳维持在线
     - 断线自动重连 (指数退避 1s→30s)
     - 上报本机 NAT 类型

  2. PeerConnect
     - 接受 Server 下发的连接指令
     - 执行 UDP/TCP 打洞
     - 打洞失败自动 fallback 到 Relay
     - 所有数据 NaCl 加密

  3. SystemMonitor
     - 每 5s 采集一次:
       CPU: 使用率、核心数、型号、温度
       GPU: 使用率、型号、VRAM 已用/总量、温度 (nvidia-smi / rocm-smi)
       RAM: 已用/总量
       Disk: 各挂载点 已用/总量
       Network: 上下行速率
       OS: 平台、版本、hostname
     - 通过心跳附带发送

  4. CommandExecutor
     - 接收 Controller 下发的 LLM 指令
     - 调用配置的 LLM backend (见 Manager D)
     - 流式回传结果
     - 支持中断执行

  5. SSHMesh (可选开关)
     - 首次启动生成 ed25519 keypair
     - 公钥注册到 Server
     - 收到新 peer 加入通知时:
       获取对方公钥 → 写入 ~/.ssh/authorized_keys
       写入 ~/.ssh/config: Host openclaw-{peer_id}
     - 用户可在设置中开关此功能

  6. WOLResponder
     - 监听来自 Server 的 WOL 指令
     - 构造 magic packet 广播到本地子网

  7. RoleSwitch
     - 默认角色: controlled (被控)
     - 输入管理密码后升级为 controller
     - controller 能看到所有 agent 的完整状态和控制面板

平台适配:
  Windows: 注册为 Windows Service (node-windows) 或开机启动项
  macOS: LaunchAgent plist
  Linux: systemd unit file

文件结构:
  agent/
    src/
      index.ts              # 入口，启动各模块
      link.ts               # ServerLink: 心跳、重连
      connect.ts            # PeerConnect: P2P/Relay
      monitor.ts            # SystemMonitor: 硬件采集
      executor.ts           # CommandExecutor: LLM 指令执行
      ssh-mesh.ts           # SSHMesh: 自动 SSH 互信
      wol.ts                # WOL 响应
      role.ts               # 角色管理 (controlled/controller)
      platform/
        win-service.ts      # Windows 服务注册
        mac-launchagent.ts  # macOS LaunchAgent
        linux-systemd.ts    # Linux systemd
    package.json

QA 检查点:
  □ Agent 启动后 3s 内注册到 Server
  □ 断网恢复后 30s 内自动重连
  □ CPU/RAM 采集误差 < 5%
  □ GPU 采集在无 NVIDIA 机器上 graceful fallback
  □ SSH 公钥交换后双向 ssh 可达
  □ WOL 发送后用 tcpdump 确认 magic packet
  □ 指令执行流式输出延迟 < 100ms
  □ Windows/Mac/Linux 三平台服务安装卸载正常
```

#### Contract A3: P2P 通信协议
```
输入: 无
输出: 共享协议库 @openclaw/protocol

消息类型 (protobuf 或 msgpack):

// ---- 注册层 ----
RegisterPeer     { id, pub_key, nat_type, system_info, timestamp }
Heartbeat        { id, system_stats, timestamp }
PeerOffline      { id }

// ---- 连接层 ----
ConnectRequest   { from_id, to_id, session_uuid }
ConnectResponse  { session_uuid, strategy: "lan"|"punch"|"relay", addrs }
PunchHole        { target_addr, session_uuid }
RelayJoin        { session_uuid }

// ---- 控制层 ----
LLMRequest       { session_id, provider, model, message, cwd, stream: bool }
LLMChunk         { session_id, data }
LLMDone          { session_id, result, error, usage }
LLMStop          { session_id }

// ---- 系统层 ----
SystemStats      { cpu, gpu[], ram, disk[], network, os_info }
WOLRequest       { target_mac, subnet }
SSHKeyExchange   { peer_id, public_key }

// ---- 角色层 ----
AuthRequest      { password_hash }
AuthResponse     { ok, role: "controller"|"controlled", token }
RoleChange       { peer_id, new_role }

QA 检查点:
  □ 所有消息类型有 encode/decode 测试
  □ 未知消息类型不导致 crash
  □ 消息大小合理 (心跳 < 1KB，stats < 4KB)
  □ 版本兼容性: v1 client 能连 v2 server (graceful)
```

---

### Manager B: UI 层 (UI Manager)

**职责**: 全平台统一 UI，Controller 控制面板

#### Contract B1: 共享 UI 框架选型 & 基础组件
```
技术选型:
  React Native + Expo  → iOS / Android
  Electron + React     → Windows / Mac / Linux
  Web (React)          → 浏览器直接访问

  共享代码比例目标: 90%+ (除平台 native 模块)

  替代方案:
    Capacitor + React → 也能覆盖全平台，但 native 体验略差
    选择 RN 因为移动端体验更好

项目结构:
  ui/
    packages/
      shared/              # 共享组件和逻辑 (90% 代码在这)
        src/
          components/       # UI 组件
          hooks/            # React hooks
          stores/           # 状态管理 (zustand)
          api/              # 与 Agent/Server 通信
          theme/            # 主题系统 (dark/light)
          i18n/             # 中英文
      mobile/              # React Native (Expo)
        app/
        ios/
        android/
      desktop/             # Electron + React
        main/
        renderer/
      web/                 # Vite React 纯 Web 版

QA 检查点:
  □ shared 组件在 3 个平台渲染一致
  □ 主题切换无闪烁
  □ 中英文切换覆盖所有文案
```

#### Contract B2: Controller 面板 — 机器列表 & 状态总览
```
页面: /dashboard (主页)

布局:
  ┌─────────────────────────────────────────────┐
  │ OpenClaw    [搜索]    [+添加机器]    [设置] │
  ├─────────────────────────────────────────────┤
  │                                             │
  │  ┌─────────┐  ┌─────────┐  ┌─────────┐    │
  │  │ Win PC  │  │Mac Mini │  │ GPU Box │    │
  │  │ ● online│  │ ● online│  │ ○ sleep │    │
  │  │         │  │         │  │         │    │
  │  │CPU 23%  │  │CPU 45%  │  │ [唤醒]  │    │
  │  │RAM 8/16 │  │RAM 12/32│  │         │    │
  │  │GPU 0%   │  │GPU --   │  │RTX 4090 │    │
  │  │VRAM --  │  │         │  │VRAM 0/24│    │
  │  │SSD 45%  │  │SSD 67%  │  │SSD 12%  │    │
  │  │         │  │         │  │         │    │
  │  │ 2 tasks │  │ idle    │  │         │    │
  │  └─────────┘  └─────────┘  └─────────┘    │
  │                                             │
  │  总资源: CPU 24核 · RAM 72GB · GPU 3张      │
  │          VRAM 48GB · 磁盘 2.4TB             │
  └─────────────────────────────────────────────┘

卡片组件 MachineCard:
  - 实时更新 (WebSocket push，5s 刷新)
  - 点击进入该机器详情
  - 长按/右键: 唤醒、SSH、重启 Agent
  - 离线机器灰色显示，有 WOL 按钮
  - 新加入机器有呼吸灯动画

顶部聚合状态栏:
  - 在线机器数 / 总机器数
  - 总 CPU 核心、总 RAM、总 GPU、总 VRAM
  - 活跃任务数

QA 检查点:
  □ 10 台机器卡片列表渲染 < 16ms (60fps)
  □ 机器上下线状态 3s 内刷新
  □ WOL 按钮点击后显示 "唤醒中..." 反馈
  □ 离线机器排到末尾
  □ 移动端横竖屏适配
```

#### Contract B3: 单机详情 & AI 对话界面
```
页面: /machine/:id

布局:
  ┌─────────────────────────────────────────┐
  │ ← Win PC                    [SSH] [设置]│
  ├────────┬────────────────────────────────┤
  │ 状态   │ AI 对话                        │
  │        │                                │
  │ CPU    │ ┌─ user ──────────────────┐   │
  │ ██░░   │ │ 帮我优化这个函数        │   │
  │ 34%    │ └─────────────────────────┘   │
  │        │                                │
  │ RAM    │ ┌─ claude ────────────────┐   │
  │ ████░  │ │ 好的，我来看看...       │   │
  │ 8/16GB │ │ ...                     │   │
  │        │ └─────────────────────────┘   │
  │ GPU    │                                │
  │ ░░░░   │                                │
  │ 0%     │                                │
  │ VRAM   │                                │
  │ ██░░   │ ┌──────────────────────┐      │
  │ 4/8GB  │ │ Ask AI...        [▶] │      │
  │        │ └──────────────────────┘      │
  │ Disk   │                                │
  │ ████░  │ Provider: [Claude ▾]          │
  │ 450GB  │ Model:    [Opus 4 ▾]         │
  │ free   │ CWD:      [/home/user ▾]     │
  │        │ Mode:     [Act ▾]             │
  ├────────┤                                │
  │ 项目   │                                │
  │ repo-a │                                │
  │ repo-b │                                │
  └────────┴────────────────────────────────┘

移动端: 状态面板收起为可展开的顶部条

功能:
  - 左侧: 实时硬件状态图表 (mini charts)
  - 左下: 该机器上的 git 项目列表 (自动扫描)
  - 右侧: AI 对话，完全复用现有聊天 UI
  - 底部: LLM Provider/Model 选择器
  - SSH 按钮: 打开终端连接到该机器

QA 检查点:
  □ 硬件状态图表 5s 更新无卡顿
  □ 对话流式输出延迟 < 200ms
  □ 切换 LLM provider 后下一条消息用新 provider
  □ 移动端左侧面板折叠展开流畅
```

#### Contract B4: 设置 & 角色切换
```
页面: /settings

功能:
  1. 管理密码设置/修改
  2. 角色切换
     - "升级为 Controller" 按钮 → 输入管理密码 → 解锁完整控制面板
     - 多个 Controller 可同时存在
  3. 网络设置
     - Main Server 地址
     - 端口配置
     - NAT 穿透开关
  4. SSH Mesh 开关
     - 全局开关
     - 每台机器单独开关
  5. 通知设置
     - 机器离线通知
     - 任务完成通知
     - 磁盘满警告
  6. LLM 配置 (详见 Manager D)

QA 检查点:
  □ 管理密码错误时明确提示
  □ 升级 Controller 后立即看到所有机器
  □ 降级回 controlled 后面板隐藏
  □ SSH Mesh 开关生效 < 10s
```

---

### Manager C: 系统监控层 (Monitor Manager)

**职责**: 硬件信息采集、展示、告警

#### Contract C1: 跨平台硬件采集库
```
输出: @openclaw/sysinfo 包

采集项:
  CPU:
    - Windows: wmic + powershell (Get-Counter)
    - macOS: sysctl + powermetrics
    - Linux: /proc/stat + /proc/cpuinfo + lm-sensors
    → { model, cores, threads, usage_percent, temp_celsius }

  GPU:
    - NVIDIA: nvidia-smi --query-gpu=... --format=csv,noheader
    - AMD: rocm-smi (Linux), 无 (Win/Mac fallback)
    - Intel: 暂不支持
    - Apple Silicon: powermetrics (GPU 部分)
    - 无 GPU: 返回 null
    → { model, vram_used_mb, vram_total_mb, usage_percent, temp_celsius }[]

  RAM:
    - 全平台: os.totalmem() + os.freemem() (Node.js 内置)
    → { used_mb, total_mb }

  Disk:
    - Windows: wmic logicaldisk
    - Unix: df -h + statvfs
    → { mount, fs_type, used_gb, total_gb }[]

  Network:
    - 全平台: /proc/net/dev 或 netstat 或 性能计数器
    → { rx_bytes_sec, tx_bytes_sec }

  OS Info:
    - Node.js 内置: os.platform(), os.release(), os.hostname(), os.arch()
    → { platform, version, hostname, arch }

实现注意:
  - nvidia-smi 不存在时 catch 并返回 null，不 crash
  - 采集频率可配: 默认 5s
  - 首次采集 < 500ms，后续 < 100ms (缓存路径)

QA 检查点:
  □ Windows 10/11: CPU + RAM + GPU(NVIDIA) 正确
  □ macOS (Intel + Apple Silicon): CPU + RAM 正确，GPU usage 有值
  □ Linux: CPU + RAM + Disk 正确
  □ 无 GPU 机器: gpu 字段为 null，不报错
  □ 连续运行 24h 无内存泄漏
  □ 采集过程 CPU 占用 < 1%
```

#### Contract C2: 监控数据存储 & 历史
```
输出: 时序数据存储模块

方案: SQLite + 降采样

  - 最近 1h: 5s 精度 (原始)
  - 最近 24h: 1min 精度 (12:1 降采样)
  - 最近 7d: 15min 精度 (180:1)
  - 更早: 丢弃

存储位置:
  - Agent 本地: ~/.openclaw/metrics.db
  - Server: 聚合所有 Agent 的摘要数据

QA 检查点:
  □ 7 天数据量 < 50MB per agent
  □ 查询最近 1h 数据 < 50ms
  □ 降采样无数据丢失边界问题
```

---

### Manager D: LLM 接入层 (LLM Manager)

**职责**: 统一多 LLM provider 接口，本地 + 远程 + CLI

#### Contract D1: 统一 LLM Adapter 接口
```
输出: @openclaw/llm-adapters 包

统一接口:
  interface LLMAdapter {
    id: string                      // "claude-cli" | "openai" | "ollama" | ...
    name: string                    // 显示名
    models(): Promise<Model[]>      // 可用模型列表
    chat(req: ChatRequest): AsyncIterable<ChatChunk>  // 流式对话
    abort(): void                   // 中断
  }

  interface ChatRequest {
    model: string
    messages: Message[]
    cwd?: string                    // 工作目录 (CLI 类)
    permissionMode?: string         // CLI 类专用
    temperature?: number
    max_tokens?: number
  }

  interface ChatChunk {
    type: "text" | "tool_use" | "error" | "done"
    content: string
    usage?: { input_tokens, output_tokens }
  }
```

#### Contract D2: Claude Code CLI Adapter
```
现有逻辑迁移:
  - spawn claude --print --output-format json
  - 支持 native / WSL / SSH 三种启动方式
  - 流式解析 JSON 输出
  - 支持 permission mode

新增:
  - 自动检测 claude 是否安装
  - 版本检测和兼容性提示

QA 检查点:
  □ Native 模式: 发送消息、收到流式响应、中断
  □ WSL 模式: Windows 路径正确转换
  □ SSH 模式: 远程执行正常
  □ 未安装 claude 时友好提示
```

#### Contract D3: OpenAI / Anthropic API Adapter
```
支持:
  - OpenAI API (GPT-4o, o1, o3, ...)
  - Anthropic API (Claude via API，非 CLI)
  - DeepSeek API
  - 任何 OpenAI 兼容 API (自定义 base URL)

实现:
  - 使用 fetch，不引入 SDK (减少依赖)
  - API key 加密存储在本地
  - 流式 SSE 解析
  - 支持 tool use / function calling

配置:
  {
    "providers": [
      { "id": "openai", "name": "OpenAI", "baseUrl": "https://api.openai.com/v1", "apiKey": "sk-..." },
      { "id": "anthropic", "name": "Anthropic", "baseUrl": "https://api.anthropic.com", "apiKey": "sk-ant-..." },
      { "id": "deepseek", "name": "DeepSeek", "baseUrl": "https://api.deepseek.com", "apiKey": "..." },
      { "id": "custom", "name": "My Proxy", "baseUrl": "http://my-proxy:8080/v1", "apiKey": "..." }
    ]
  }

QA 检查点:
  □ OpenAI GPT-4o 流式对话正常
  □ Anthropic Claude API 流式正常
  □ 自定义 base URL 正常
  □ API key 错误时明确提示
  □ 网络超时 30s 后提示
  □ 流式中断正确清理连接
```

#### Contract D4: Ollama Adapter
```
支持:
  - 本地 Ollama (http://localhost:11434)
  - 远程 Ollama (任意 IP:port)
  - 自动发现同网络的 Ollama 实例

实现:
  - POST /api/chat 流式
  - GET /api/tags 获取已安装模型
  - 支持 pull model 下载新模型
  - 支持 GPU 加速状态显示

QA 检查点:
  □ 本地 Ollama 对话正常 (qwen2.5, llama3, ...)
  □ 远程 Ollama 对话正常
  □ 模型列表正确显示
  □ Ollama 未运行时友好提示
```

---

### Manager E: 安全层 (Security Manager)

**职责**: 认证、加密、权限控制

#### Contract E1: 认证系统
```
层级:
  1. Agent → Server: ed25519 keypair (首次注册生成)
  2. Controller → Server: 管理密码 + JWT token
  3. Agent ↔ Agent: NaCl 加密通道
  4. SSH Mesh: ed25519 key 自动交换

管理密码:
  - 首次启动 Server 时设置
  - bcrypt hash 存储
  - 任何 Agent 输入正确密码 → 升级为 Controller
  - Controller token: JWT, 7 天过期，可手动撤销

API Key 存储:
  - 各 LLM 的 API key
  - 使用 OS keychain (keytar):
    Windows: Credential Manager
    macOS: Keychain
    Linux: libsecret
  - fallback: AES-256 加密文件，密码 = 管理密码 hash

QA 检查点:
  □ 错误密码无法获得 Controller 权限
  □ Token 过期后自动降级
  □ API key 不以明文出现在任何日志
  □ 中间人无法解密 Agent 间通信
```

---

## 执行顺序 (4 个 Phase)

### Phase 1: 基础设施 (可并行)
```
并行:
  [A1] Rendezvous Server     ← Worker 1
  [A3] Protocol 定义         ← Worker 2
  [C1] 硬件采集库            ← Worker 3
  [D1] LLM Adapter 接口      ← Worker 4

依赖: 无，各自独立
预期: 这些是所有后续模块的基础
```

### Phase 2: 核心功能 (依赖 Phase 1)
```
并行:
  [A2] Agent Daemon           ← Worker 5 (依赖 A1, A3, C1)
  [D2] Claude CLI Adapter     ← Worker 6 (依赖 D1，迁移现有代码)
  [D3] API Adapters           ← Worker 7 (依赖 D1)
  [D4] Ollama Adapter         ← Worker 8 (依赖 D1)
  [E1] 认证系统               ← Worker 9 (依赖 A3)
  [B1] UI 框架搭建            ← Worker 10

依赖: Phase 1 全部完成
```

### Phase 3: 集成 (依赖 Phase 2)
```
并行:
  [B2] Dashboard 机器列表     ← Worker 11 (依赖 A2, B1, C1)
  [B3] 单机详情 + AI 对话     ← Worker 12 (依赖 B1, D2-D4)
  [C2] 监控数据存储            ← Worker 13 (依赖 C1)

依赖: Phase 2 核心模块完成
```

### Phase 4: 收尾
```
顺序:
  [B4] 设置 + 角色切换        ← Worker 14
  多平台打包测试               ← Worker 15
  集成测试 + 压力测试          ← Worker 16
```

---

## 目录结构总览

```
openclaw/
├── server/                    # Manager A: Rendezvous Server
│   ├── src/
│   │   ├── index.ts
│   │   ├── registry.ts
│   │   ├── broker.ts
│   │   ├── relay.ts
│   │   ├── wol.ts
│   │   └── db.ts
│   ├── package.json
│   └── Dockerfile
│
├── agent/                     # Manager A: Agent Daemon
│   ├── src/
│   │   ├── index.ts
│   │   ├── link.ts
│   │   ├── connect.ts
│   │   ├── monitor.ts
│   │   ├── executor.ts
│   │   ├── ssh-mesh.ts
│   │   ├── wol.ts
│   │   ├── role.ts
│   │   └── platform/
│   │       ├── win-service.ts
│   │       ├── mac-launchagent.ts
│   │       └── linux-systemd.ts
│   └── package.json
│
├── packages/
│   ├── protocol/              # Manager A: 共享协议
│   │   ├── src/
│   │   │   ├── messages.ts
│   │   │   ├── encode.ts
│   │   │   └── decode.ts
│   │   └── package.json
│   │
│   ├── sysinfo/               # Manager C: 硬件采集
│   │   ├── src/
│   │   │   ├── cpu.ts
│   │   │   ├── gpu.ts
│   │   │   ├── ram.ts
│   │   │   ├── disk.ts
│   │   │   ├── network.ts
│   │   │   └── index.ts
│   │   └── package.json
│   │
│   ├── llm-adapters/          # Manager D: LLM 统一接口
│   │   ├── src/
│   │   │   ├── types.ts
│   │   │   ├── claude-cli.ts
│   │   │   ├── openai.ts
│   │   │   ├── anthropic.ts
│   │   │   ├── ollama.ts
│   │   │   └── index.ts
│   │   └── package.json
│   │
│   └── security/              # Manager E: 认证加密
│       ├── src/
│       │   ├── auth.ts
│       │   ├── crypto.ts
│       │   ├── keystore.ts
│       │   └── jwt.ts
│       └── package.json
│
├── ui/                        # Manager B: 全平台 UI
│   ├── packages/
│   │   ├── shared/            # 共享 React 组件
│   │   │   ├── src/
│   │   │   │   ├── components/
│   │   │   │   │   ├── MachineCard.tsx
│   │   │   │   │   ├── MachineDetail.tsx
│   │   │   │   │   ├── ChatPanel.tsx
│   │   │   │   │   ├── StatusBar.tsx
│   │   │   │   │   ├── LLMSelector.tsx
│   │   │   │   │   ├── LoginScreen.tsx
│   │   │   │   │   └── Settings/
│   │   │   │   ├── hooks/
│   │   │   │   ├── stores/
│   │   │   │   └── api/
│   │   │   └── package.json
│   │   │
│   │   ├── mobile/            # React Native (Expo)
│   │   ├── desktop/           # Electron
│   │   └── web/               # Vite
│   └── package.json
│
├── router/                    # 保留现有 context router
│   ├── classify.py
│   ├── backup.py
│   ├── web.py
│   └── contexts/
│
├── package.json               # monorepo root (pnpm workspaces)
├── pnpm-workspace.yaml
├── tsconfig.base.json
└── PLAN.md
```

---

## QA 流水线规则

每个 Contract 完成后自动触发 QA:

```
Worker 完成 Contract
       ↓
  自动生成 QA Checklist (基于 Contract 中定义的检查点)
       ↓
  QA 运行所有检查
       ↓
  ┌─ PASS → 标记 Contract 完成，通知 Manager
  │
  └─ FAIL → 生成修复 Contract → 新 Worker/原 Worker 修复
                ↓
            再次 QA → 循环直到 PASS
```

QA 工具:
  - 单元测试: vitest
  - 集成测试: 模拟多 Agent + Server 场景
  - 平台测试: GitHub Actions matrix (win/mac/linux)
  - 性能测试: 内存泄漏检测 (--max-old-space-size + heap snapshot)
  - 安全测试: API key 明文扫描、注入测试
