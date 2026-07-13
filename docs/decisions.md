# 决策日志

记录每次偏离原始设计的决定，以及原因。

> **路径漂移说明**：本文件是历史日志。条目里提到的文件路径反映**决策当时**的代码布局；后来许多文件改名/搬迁/合并（如 `cluster/cluster_rpc.py` → `cluster/rpc.py`、`gateway/http_api_server.py` → `gateway.py`、`channels/` → `transports/`、`sessions/claude_native.py` → `sessions/browser/loaders.py`）。当前代码以 `codebase-guide.md` 为准。

---

> 📦 **2026-03 ~ 2026-05 的历史条目已归档**到 [archive/decisions-2026-03-to-05.md](archive/decisions-2026-03-to-05.md)。本文件保留 2026-06 起的决策。

## 2026-07-07 — Web UI server 从 aiohttp 迁到 Starlette + Hypercorn（拿 HTTP/2）

**病**：web UI 每个 chat 一条推送连接（SSE / WS），几个 chat 一开就占满浏览器
HTTP/1.1 的 ~6 连接槽，整页卡住。aiohttp **官方不支持 HTTP/2**（server 端 issue
closed as not planned）——同一个 6-连接上限治不了本。

**修法**：把 9292 这个 web UI server 从 aiohttp 换到 **Starlette + Hypercorn**，
Hypercorn 支持 HTTP/2（uvicorn 不支持，故不用它；MCP 那边已是 Starlette+uvicorn，
本次不动）。HTTP/2 一条连接多路复用无数 stream，连接槽问题从根上消失。

1. **框架**：`web.Application` → `Starlette(routes=[...], middleware=[...])`。27 个
   route handler 从 `(aiohttp Request)->web.Response` 逐个改成
   `(starlette Request)->JSONResponse/Response`：`request.query`→`query_params`、
   `match_info`→`path_params`、`web.json_response(d,status=n)`→`JSONResponse(d,status_code=n)`。
2. **鉴权** `_authorized`：peer 从 `request.transport.get_extra_info("peername")` 改成
   `request.client.host`；localhost / trusted-header / bearer / query-token 逻辑不变。
   WebSocket 也有 `client/headers/query_params`，multiplex WS 复用同一函数。
3. **4 处 SSE**（web_stream / events_stream 等）：`web.StreamResponse` →
   `StreamingResponse(async_gen(), media_type="text/event-stream")`。断开时 Starlette
   取消 generator → `finally` 里 `subscription.close()`，等价于旧 aiohttp 写失败清理。
4. **multiplex WS**：`web.WebSocketResponse` → Starlette `WebSocketRoute` +
   `await ws.receive_text()` / `await ws.send_json(...)`；断开抛 `WebSocketDisconnect`。
5. **static**：`add_static("/")` → `Mount("/", StaticFiles(...))`，放路由列表最后不遮 API。
6. **error 中间件**：新增 Starlette `ErrorLoggingMiddleware`（`BaseHTTPMiddleware`），
   行为与旧 aiohttp `error_logging_middleware` 对等（handler 异常 + 5xx 打进 event log）。
   旧 aiohttp 版**保留**——InternalApiServer（`/api/schedule/run`）仍是 aiohttp（独立端口，
   本次不迁）。
7. **启动**：`AppRunner`/`TCPSite` → **Hypercorn 编程式启动**。`Config.alpn_protocols =
   ["h2","http/1.1"]` + `await hypercorn.asyncio.serve(app, config, shutdown_trigger=...)`
   跑后台 task；`stop()` 设 shutdown event 优雅关闭。
8. **cluster guest WS 同迁**：`/api/guest/ws`（同端口 9292）从 aiohttp WS 换到 Starlette
   `WebSocketRoute`。`registry.handle_ws(request)` → `handle_ws(websocket)`：
   `ws.prepare` → `websocket.accept()`；`async for msg in ws` → `while True:
   await websocket.receive_text()`（断开抛 `WebSocketDisconnect`）；`ws.close(code,message=b"")`
   → `websocket.close(code,reason="")`。`ClusterBus.attach_link(mid, ws.send_json)` 照旧
   用——Starlette WS 的 `send_json` 同样收单个 dict，签名兼容。

**保留 aiohttp（客户端，不需要 HTTP/2）**：`guest_client.py`（ClientSession + ws_connect
拨号）、`request_reply.py` 的 loopback ClientSession（responder 打 127.0.0.1 跑真
handler）、`devtunnel.py`、`host_election.py` 的 probe、`fetch_host_json`——这些是
HTTP/WS **客户端**，原样留着。

**HTTP/2 验证**：起服务后 `curl --http2-prior-knowledge http://127.0.0.1:<port>/api/version`
报 `http_version: 2`（h2c 明文，无 TLS）。生产链路是浏览器 —(HTTPS,ALPN 协商 h2)→
devtunnel —(h2c)→ 本机 origin。测试见 `tests/unit/test_web_server_http2.py`（起真
Hypercorn + curl 验 h2）。

**为什么 curl 而非 httpx 验 h2**：httpx `http2=True` 在明文连接上**不发** h2
prior-knowledge 前导，只在 TLS 上靠 ALPN 升级；curl `--http2-prior-knowledge` 直接
跑 h2c，正是 devtunnel 终结 TLS 后对本机 origin 的连法。

**改的文件**：`pyproject.toml`（+`starlette` +`hypercorn[h2]`）、
`transports/web/server.py`（整体重写为 Starlette）、`web_error_middleware.py`（+Starlette
版）、`cluster/http_routes.py`（register 追加到 routes 列表）、`cluster/registry.py`
（handle_ws 改 Starlette WS）、`cluster/request_reply.py`（dispatch/handle_guest_ws 适配
Starlette，loopback ClientSession 保留）。测试：`test_web_multiplex` / `test_cluster_fast_fail`
/ `test_cluster_log_categories` 的 WS 桩改 Starlette 形；handler-direct 测试的 fake
request 改 `query_params`/`path_params`/`client.host`；新增 `test_web_server_http2.py`。

**风险点**：SSE 断开检测语义（generator 取消 vs 旧写失败）、multiplex WS 的
pump task 清理、guest WS 读循环退出条件（`WebSocketDisconnect`）——都写了测试锁定。

---

## 2026-07-07 — 跨机请求对不兼容/不可达机器「秒失败」（版本协商 + dispatch 前置门）

**病**：统一到一根 ClusterBus 后，mixed-version 场景（有机器还在老代码 / 离线 /
版本不匹配）下，跨机请求发给这种机器时**干等满超时**才失败。web UI 点开这种机器的
bot 会连发 `/api/history`、`/api/send` 等跨机请求，每个卡满超时，占满浏览器
HTTP/1.1 的 ~6 个连接槽 → UI 冻住（后台没坏，curl 秒回）。

**修法**：给跨机传输加一层「协商版本 → 前置门 → 秒失败」，不发注定超时的请求。

1. **握手带版本**：guest `hello` 帧加 `v=WIRE_VERSION`（ClusterBus 的 3），host
   `welcome` 帧同样加 `v`。缺 `v` = 老 peer = 记 `0`（不兼容）。
2. **attach_link 记真实协商版本**：host 收 hello 用 `payload.v` 建 `GuestSession.version`
   并 `attach_link(version=...)`；guest 把 `attach_link` **从「发完 hello」挪到「收到
   welcome」**（host 版本在 welcome 才拿得到）。guest 侧 `welcome`/`machines_snapshot`
   两帧移到旧 v2 版本门**之前**处理——否则 v3 的 welcome 会被 events 用的 v2 门丢掉。
3. **版本进 `machines_snapshot`**：`collect_machines` 每台机描述符加 `version`（host
   自己 = WIRE_VERSION，各 guest 从 `GuestSession.version`；离线机器 = 0）。guest 侧
   `remote_machines` 原样缓存。
4. **dispatch 前置门（fast-fail 关键）**：`RequestReply.dispatch_machine_request` 发请求
   **前**查目标机 version（新增 `TopologyService.version_for`：host 从
   `guest_registry.sessions[*].version`，guest 从 `guest_client.remote_machines`，自己
   = WIRE_VERSION，未知 = 0）。`!= WIRE_VERSION` → **立刻回 502**（<1ms，不占槽），不发
   注定超时的请求。web/server.py 的 13 处调用点无需改。
5. **定时刷新 + 重连重握手**：机器更新重启会自动重 hello（带新版本），host 收到即
   `on_topology_change` 重推 snapshot；`HostElection._tick`（每 10s）在 host 稳态额外
   **定时重推** snapshot，保证「别人更新重启后大家很快知道它是新版本」，不把已更新机器
   一直当老版本挡。
6. **默认超时**：保持 `request()` 默认 **30s**（前置门 + `on_unreachable` 已把「已知坏」的
   机器秒毙；超时只兜底「同版本、在线、但静默」的罕见情况）。曾试过一刀切砍到 8s，**已
   revert**——它会误杀合法的慢请求，且超时语义是「结果未知」不是「没发生」，非幂等操作
   （/api/send）据此重试会双发。这块留给后续单独设计的 RPC 框架处理。

**为什么前置门而不是只靠超时**：`on_unreachable` 只在「链路当场不存在」时触发；一个
**版本不匹配但链路在**的 peer，请求会真发出去、对面（老代码）读不懂或丢弃、requester
干等超时。前置门在「发」之前就拦，把这一类从「超时级」降到「<1ms 级」。

**改的文件**：`cluster/registry.py`（GuestSession.version + hello 记版本 + welcome 加 v
+ list_machines 带 version）、`cluster/guest_client.py`（hello 加 v + attach_link 挪到
welcome + welcome/snapshot 提到 v2 门前）、`cluster/topology_service.py`（collect_machines
带 version + `version_for`）、`cluster/request_reply.py`（dispatch 前置门；timeout 保持 30s）、
`cluster/host_election.py`（host 稳态定时重推 snapshot）。测试
`tests/unit/test_cluster_fast_fail.py`（+9）。

## 2026-07-06 — chat 数据面真正上 MessageBus（ChatSyncer 从 sibling 变 bridge；net +70）

**背景 / 起因**：上一条（PR #34）统一了 API/transport/wiring/帧，但 **chat 的本地↔远端还是缝起来的**：本机 chat 走 `MessageBus`，跨机走 `PeerTransport`，中间靠 `chat_bus.py` 的 owner-pump + `on_local_publish`/`on_local_demand` 两个适配 hook + `ChatSyncer._queues` 独立队列桥接。owner 追问"跨机也走同一根 bus、同协议"这条愿景为什么没兑现 —— 确实没兑现：那时是**两根 bus（本机 `MessageBus` + 跨机 `PeerTransport`）**，chat 骑两根、用适配缝拼。RPC 反而已经 location-transparent（`rpc_over_bus`，host=guest 逐字相同），证明"请求/应答"不妨碍位置透明 —— 所以 chat 没理由不上 bus。

**这次做的**：把 `ChatSyncer` 从"sibling + 适配缝"改成**一根 `MessageBus` 上的 bridge**，chat 的数据面本地/远端**同一个 publish/subscribe**：
- 浏览器订阅（SSE）= `bus.subscribe(chat.<owner>.<bot>.<chat_id>)`，**local/remote 一条路径**（`ChatBus.subscribe` 不再分叉）。
- owner 侧 WebChannel `publish` 到该 topic → `ChatSyncer` 订 `chat.` 前缀的 `_OutboundBridge` 把它转发给下游 peer（**取代 owner-pump**）。
- 本机订了一个「远端拥有」的 topic → 新增的 `MessageBus.watch_subscriptions(prefix, on_add, on_remove)` 通知 `ChatSyncer`，refcount 边沿往上游发 `chat_subscribe`（**取代 `on_local_demand` + remote_subscribe**）。
- 入站 `chat_event` 帧 → `bus.publish` 重新注入本机 topic：本机 browser queue（bus 订阅者）+ 下游中继（`_OutboundBridge`）**一次搞定**，两跳 relay = 重注入 + 前缀转发。

**删掉的适配缝**：`chat_bus.py` 的 `_pump`/`_on_local_demand`/`aclose-cancels-pumps`；`ChatSyncer` 的 `on_local_publish`/`on_local_demand`/`_fire_demand`/`_queues`/`remote_subscribe`/`remote_unsubscribe`/`_toggle_source` 的本地分支。`ChatBus` 从 88 行缩到 62 行（纯订阅门面）。`_ChatQueueSubscriber` 与 WebChannel 的重复合并成 `bus/subscriber.py::QueueSubscriber`（两处共用）。**WebChannel 变 publish-only**：`subscribe`/`unsubscribe`/`_subscribers`/`stop` 的 `_close` 广播删掉（浏览器 SSE 订阅现在全归 ChatBus，旧"三套队列机制"的最后残留清掉）——浏览器改经 bus 直接订阅（测试同步改）。

**唯一新增的不可消除物**：`bus.publish`/`subscribe` 同步、`ws.send_json` 异步 —— 出站 peer 帧走**单条有序发送队列** `_sendq` + 一个 `_drain` task。FIFO 保证同一 chat 的 stream_delta 不乱序（**坑#1** 禁止 create_task-per-event）。这是 sync bus↔async WS 的本质边界，不是适配缝；旧的 per-`(bot,chat)` pump 就是它的等价物，现在收敛成每节点一条。

**诚实结账**：`src/` 净 **+44 行**（+246/−202）。真正的统一（`watch_subscriptions` + `QueueSubscriber` + 有序 drain + bridge 逻辑）加的比删的（pump/fork/`_queues`/WebChannel 订阅面）略多 —— **架构真统一了，代码没大缩**。跨机三种投递（broadcast+cursor / demand+relay / request-reply）是**本质不同的策略代码**，统一到一根 transport/bus 不会让策略消失，所以没有"大刀阔斧删除"藏着 —— 这和上一条的教训一致。

**边界（哪些"没上 bus"，为什么对）**：
- **RPC** 仍是 request/reply 骑 `PeerTransport`，不变 publish —— 上一条已论证（id-correlation + 并发 vs serial 保序 fan-out，正好相反；成熟 bus 如 NATS 也是 request/reply 架在 pub/sub 之上）。
- **event resync（cursor）** 仍是连上时按 cursor 拉漏事件的 query，不是 pub/sub 原语。
- 所以"一根 bus"= **数据面**（event publish + chat publish/subscribe，本地远端同协议）统一到 `MessageBus`；**控制面**（resync / RPC request-reply）仍是各自模式骑同一根 `PeerTransport`。这是"一根共享管道 + 每 topic 族一个策略"，不是"一个 publish 端到端"。

**测试**：`test_chat_sync.py`（16 例）、`test_chat_bus.py`（8 例）重写到 bus-native API（黑盒断言只看 sent 帧 / queue / 重注入），覆盖不减；出站现在异步 drain，冻结不变量 `test_INV_C2` 加 `await settle()` 等可观测帧到达（黑盒合法）。全量 **984 绿（基线 984 不降）**。`_bus_harness.py` 每 node 改用一根共享 `MessageBus`（events+chat 同实例，同生产）。

## 2026-07-06 — message-bus 统一（PR #34，含 RPC；诚实结账 +763）

**目标（owner 愿景）**：event / chat / RPC 三条跨机投递收敛到**一根 content-agnostic 的 MessageBus** —— 同协议不同链路（local→进程内 queue subscriber，remote→cluster WS），bus 不认识"事件/聊天/RPC"，只认 topic + 谁订阅。

**目标架构**：
- `bus/` 中立 leaf 包：`MessageBus.publish(topic, payload, ts)` 同步保序 fan-out（按 topic 索引：`_exact` 精确 topic + `_prefix` 前缀），`subscribe(pattern, subscriber)→Subscription`。`Message = {topic, payload, ts}`，core 从不读 payload。
- **持久化/广播是 subscriber 行为，不是 bus 特性**：`EventStore` 是订阅 `events.` 的**同步第一 slot** subscriber（`StoreSubscriber` 写库 + mint id/origin_seq，enrich 后的 Event 塞进 payload 给下游）；广播=每节点订阅 peer 的 `events.*`；chat 无 StoreSubscriber → **chat 永不进 SQLite（构造即保证，非运行时 check）**。
- **没有共享的复制算法**：`EventSyncer`（broadcast+debounce+cursor resync）和 `ChatSyncer`（demand+refcount+两跳 relay）是**两个 sibling subscriber**，共用一个 `PeerTransport`（peer 注册表 + send）。统一发生在 API / transport / wiring / 帧 层，不在复制算法层。
- **RPC 骑 transport 不进 publish/topics**：它是 request/reply（id-correlation + 并发），和 chat/event 的 serial single-pump 相反。塌成 `rpc_over_bus.py`（一份 role-agnostic call + 一份 loopback InboundRequestExecutor），host/guest 镜像消失。详见下方 Phase 1.5 条目。
- 一个 `bus_wiring.py` 取代 `sync_wiring.py` + `chat_sync_wiring.py` 的两条 install-order 链；`v` wire-version 封套 + mixed-version 优雅丢弃（三种帧族统一：peer_transport.send_to 盖 v，两个 WS 循环 dispatch 前门控）。

**分阶段执行**（P0 回归网 → P7），每步以冻结不变量为 gate，独立 review 抓修过一个假绿 blocker。**Phase 8（删 EventBus）调研后回退**：EventBus 不是"待删 shim"而是有用的事件 facade（打包"建/共享 bus + 注册 store-write + publish + subscribe"）；删了会把这套摊到 4 个消费者 + gateway + ~9 个测试，**代码不减反增**。EventBus 保留，路由经共享 MessageBus。

**诚实结账**：净 **+763 生产行**（不是最初估的 −100）。加一层 bus 抽象（`bus/` + `rpc_over_bus` + `peer_transport` + adapters）比去重删的多 —— **架构更清晰更统一，但更大不是更小**。devil's advocate 从第一轮就说中了这点；owner 在知情下坚持完整方案（C）而非 subset，最终照 C 交付。**教训**：估算净收益时把新增抽象层单列成"drift risk"是窄口径，全口径要一起算。

**Code review（xhigh recall）**：15 项 finding，无 confirmed crasher。修了 6 项实质的（版本协议缺口=RPC 原本没盖 v/没门控、`MessageBus.publish` O(全部订阅)→按 topic 索引、死代码 `bus/subscriber.py` Local/RemoteSubscriber、bus_wiring fire-and-forget create_task、`_PendingResponse` get_event_loop→get_running_loop、registry 断开 reject_all）；余 9 项低价值/latent 记录在 `docs/bus-migration-map.md`。全量 984 绿（基线 886）。

相关：`docs/bus-migration-map.md`（相位 + 测试映射 + findings）、`docs/message-bus-unification-提案.md`（设计提案 + DA + 三方讨论，**历史参考，写于 Phase 8 回退前**）。

## 2026-07-03 — message-bus 迁移 Phase 1.5：塌掉 RPC host/guest 镜像（`cluster/rpc_over_bus.py`）

**背景**：RPC 的 request/reply 半边在 tree 里存在两份，按 role 镜像：`GuestSession.call`（host 侧 registry.py）vs `GuestClient.call`（guest 侧 guest_client.py）；`GuestRegistry._serve_inbound_rpc`（host loopback 回环重发）vs `GuestClient._handle_rpc`（guest loopback，与 host 版几乎逐字相同，只差 http session / 日志身份 / host 独有的 503-not-configured guard）；`ClusterRpc._proxy_via_host` vs `_proxy_to_remote`（只差错误串）。这是 tree 里最大的一块 role-split 重复，也是整个 message-bus 迁移能 net-negative 的关键（chat+event 单独做是 +306 陷阱）。

**方案**：新增 `cluster/rpc_over_bus.py`，把每对镜像塌成一份：
- `_PendingResponse`（id→future 关联原语）从 registry.py 迁来，registry/guest_client 从此处 re-export 引用。
- `RpcChannel` —— caller 侧。owns per-link `_pending`（**保持 per-link，不做 bus-global**，否则 reject-on-disconnect 要长出 peer-scan）。`call(send_frame, method, path, ...)` 铸 rpc_id、park future、经注入的 `send_frame` 发 `{type:rpc}` 帧、await 关联回复、清理；`resolve(rpc_id, status, body)` 关联入站 `rpc_resp`；`reject_all(exc)` 断链时一把 fail 所有在飞 caller。host `GuestSession` 与 guest `GuestClient` 各 compose 一个，`call`/`_resolve`/`_pending` 全部 delegate。
- `InboundRequestExecutor` —— 唯一的 loopback 回环重发器。读 `{id,method,path,query,body}` → `http://127.0.0.1:{local_web_port}{path}` + `Bearer` 头 → `session.request(...)` → JSON-or-raw → 回 `rpc_resp` → except→502。host/guest 三处差异（http session provider / 日志身份 / host 独有 503 guard）全部参数化，**两条日志串按 role 逐字传入保持 byte-identical**。**保持真 aiohttp 回环，不塌成 in-process publish** —— 回环是隐藏的控制流环：guest→host→guest 时 host 重发的请求自己命中 `dispatch_machine_request` 再往第二个 guest 转发，两跳中继 for free；in-process 捷径会静默跳过 auth / machine-resolution / onward dispatch，破坏两跳（INV-R3 冻结此点）。
- `ClusterRpc._proxy`（一份）取代 `_proxy_via_host` + `_proxy_to_remote`，`label` 参数区分 "host"/"remote" 错误串。

**为什么 RPC 骑 transport 而非 `MessageBus.publish`/topics**：chat/event 是 fire-and-forget fan-out 走 serial single-pump（坑#1 要严格保序）；RPC 是 caller await 单条关联回复，要 id-correlation + 并发，正好相反。放 serial pump 上会把并发 RPC 串行化（一个慢 `/api/logs` 分页阻塞全部）。所以 RPC 骑 transport，`bus/` core 永不获得 "rpc" 概念。

**net LOC（code-only，docstring/注释/空行剔除）**：3 个既有文件删 162 行、加 84 行（delegation shim + lazy-executor build）；新文件 `rpc_over_bus.py` 加 128 行（含 `_PendingResponse` 7 行是**搬迁**非新增）。净 `(84+128)−162 = +50` raw，扣掉搬迁的 `_PendingResponse`(7)+`_resolve`(4) 后按"policy held constant"口径约 **+39**。注：Phase 1.5 是**纯 delegation**，镜像塌成一份共享体的收益在**行数上被 `rpc_over_bus.py` 的两大 docstring 抵消**（模块+类 docstring ~54 行）；纯 executable-body 层面 host+guest 两份 loopback（94 行）+ 两份 call（53 行）+ 两份 proxy（38 行）= 185 行镜像塌成 executor(~55)+RpcChannel-call/resolve/reject(~30)+`_proxy`(~13) ≈ 98 行，body 净减 ~87。行数账的"负"在 Phase 6/7 帧统一落地时才完全兑现（见 decision-v2.md §3）。

**测试**：`test_message_bus_invariants.py` 的 INV-R1..R6 全绿（单跳真 body / loopback 命中真 handler / **两跳 gA→host→gB** / 50 并发乱序不串 / 超时无泄漏 / 非串行）；既有 `test_cluster_rpc.py` / `test_cluster_registry.py` / `test_admin_cluster_restart.py` **零改动**全绿（delegation 保持了 `session._pending` / `session.call` / `session._resolve` 的公开面）。全量 968 passed。

## 2026-07-02 — 跨机 chat 流统一到 ChatBus/ChatSyncer（干掉 SSE re-framing）

**背景**：同机器的 chat stream 走 `WebChannel` per-chat `asyncio.Queue` fan-out，浏览器 SSE 订阅 `/api/stream`。跨机器则完全另一套：`_handle_web_stream` 里 `if machine != local: dispatch_machine_stream` → guest/host 把 SSE 逐帧 `data:` 行拆开、经 WS `rpc_stream`/`rpc_end` 帧重发、对端再拼回 `data:`。事件因此被 **序列化→拆行→重发→拼回→再序列化** 每一跳一次，脆且和同机器完全不同架构。Owner 要求同机器/跨机器同架构。

**方案（直接 C）**：新增 message-bus 层，location-transparent：
- `cluster/chat_sync.py` `ChatSyncer` —— location-transparent 跨机 pub/sub，仿 `events/sync.py`。三种帧 `chat_subscribe` / `chat_unsubscribe` / `chat_event`（event 是**原始 dict**，不再序列化成 SSE），走既有 cluster WS。订阅式（非全量复制）：只订阅浏览器正在看的 `(bot, chat_id)`。**一张订阅表 keyed by `(owner_machine, bot, chat_id)`**：`_downstream` 记要转发给谁（`machine==self` 即"我拥有该 bot"，否则是 host 两跳中继），`_queues` 记本机浏览器，`_sources` 每 key 一个源（owner→pump 本地 WebChannel / remote→上游 chat_subscribe）refcount。owner-publish 和 relay-event 走同一个 `_deliver`；pump-local 和 subscribe-upstream 走同一个 `_toggle_source` —— owner/subscriber/relay 是同一张表的三种 machine 取值，不是三套代码。
- `cluster/chat_bus.py` `ChatBus` —— `subscribe(bot, chat_id, machine)`：local 返回 WebChannel 队列，remote 返回 ChatSyncer 队列，**同一 queue 形状**。owner 侧用 `on_local_demand` 回调驱动 per-`(bot,chat_id)` **pump**：订阅本地 WebChannel、单任务顺序 `await on_local_publish` 转发给远端订阅者 —— 复用同一份 in-process fan-out、天然保序，**不用 create_task-per-event**（避开踩过的乱序坑）。
- `cluster/chat_sync_wiring.py` —— **链式**接上 registry/guest_client 的 `on_unknown_frame`/`on_guest_attached`/`on_guest_detached`（EventSyncer 已占用且是直接赋值，chat 必须在其后安装并捕获旧值 fallthrough）。同步 attach/detach callback 用 `create_task` 桥接到 async 方法。
- `server.py::_handle_web_stream` —— 删掉 `dispatch_machine_stream` 分叉，改 `queue = await chat_bus.subscribe(...)`，SSE 循环对 local/remote **完全一致**。`/api/send`（POST）仍走 `dispatch_machine_request` 代理，不动。

**删除的死代码**（ChatBus 接线后第二套架构整体死掉）：`rpc.py` 的 `dispatch_machine_stream`/`_proxy_stream_to_remote`/`_proxy_via_host_stream`；`registry.py` 的 `GuestSession.call_stream`/`_push_stream`/`_end_stream` + `_serve_inbound_rpc` 的 `is_sse` 分支 + `rpc_stream`/`rpc_end` 入站；`guest_client.py` 同类。`_PendingResponse` 砍到只剩 `result`。（这也让 decisions.md 里 2026-06 那条"回复走 `_proxy_via_host_stream` 无超时"的机制过时 —— `/api/send` 立即返回不阻塞的结论仍成立，只是回复流现在由 ChatBus 承载。）

**测试**：`test_chat_sync.py`（15 例：owner/subscriber/host-relay/refcount/detach/reconnect/demand）、`test_chat_bus.py`（11 例：local/remote 分派 + pump 保序 + aclose）、`test_chat_sync_wiring.py`（6 例：链式不覆盖 event sync）。删 `test_cluster_registry.py::test_call_stream_yields_then_ends`（测已删的 call_stream）。全量 918 passed。

**为什么这样**：ChatSyncer 抄了已在生产验证的 `EventSyncer` 骨架（attach_peer/detach_peer/handle_frame/refcount）去风险；owner pump 复用 WebChannel 队列而非改 `WebChannel._publish`，把 transport 改动降到零。

## 2026-06-28 — 物理删除 ClaudeProcess（claude CLI subprocess backend）

接 2026 早期"`claude-cli` 静默重定向到 `AgentSDKClaude`"的迁移，本次把 CLI 子进程实现 `claude_process.py` 物理删除（之前只是占位不实例化）。

**调研发现**：删之前 `ClaudeProcess` 还有一处生产实例化 —— `agent_manager.py` 的 raw passthrough bot 用它当 Router 的占位 backend（Router.backend 不能为 None，`env_builder` 无条件读 `.yolo`/`.model`，但真正干活走 pool）。

**改动**：
- `agent_manager.py`：占位 stub 改用 `self._raw_backend_factory(backend="claude-cli", ...)` —— 和 pool 用同一个 factory，产出真正的 `AgentSDKClaude`，删掉 `from ...claude_process import ClaudeProcess`
- 删 `src/boxagent/agent/claude_process.py`（240 行）
- `_normalize_usage` 此前 claude_process / sdk_claude_process 各有一份（生产只用 SDK 那份），删 claude 那份，`test_session_info` 改用 `AgentSDKClaude._normalize_usage`（@staticmethod）
- 删 `test_claude_process.py`（21 例，测 CLI stream-json 解析/`_build_args`）+ `tests/integration/test_cli_real.py`（4 例，真 CLI）+ `test_system_prompt.py::TestClaudeSystemPrompt`（5 例，测 `--append-system-prompt` 拼接）—— 都是 CLI 专属死代码
- `test_workgroup`/`test_agent_backend_protocol` 的 ClaudeProcess 引用改指 `AgentSDKClaude`

**测试数下降说明**：本次测试从 1073 → 1047（-26）。这是删除死代码连带删其测试的**合理例外**，非隐藏回归。Claude backend 的存活实现由 `test_sdk_claude_process.py`（9 例）覆盖，未丢核心覆盖。

**为什么现在删**：`backend_factory` 早已不经 CLI 路径；保留只增加"4 个 backend"的认知负担和误导（测试里有人会 patch 错对象）。

## 2026-06-28 — workgroup 隔离为可插拔模块（路线 B）Phase 1：数据类归并（yait #98）

**背景**：`IncomingMessage` / `AgentEnv` / `Router` 三个核心数据类各自背着一批 workgroup/peer 专属字段，让单机单 agent 主链路读起来背着用不到的概念。Owner 决策不删 workgroup（日常在用），走**路线 B 务实隔离**：把 workgroup 知识尽量收回 workgroup 包，core 只留少量守卫分支，最终目标是删 workgroup = 删包 + 拔 ~5 个守卫（而非外科手术）。详见 yait #98 伞形 issue。

**Phase 1 改动**（本 commit）：
1. **删死字段 `via_workgroup`**：实测它在 4 处被写、0 处被读驱动行为（`is_specialist`/`is_workgroup_admin` 用的是 `workgroup_role`）。从 `IncomingMessage` + `AgentEnv` + env_builder + core.py(×2) + manager.py 整条删除。
2. **AgentEnv 归并**：`has_peer_channel` + `workgroup_role` + `workgroup_agents` + `running_tasks` + `peers` 五个裸字段 → 单一 `workgroup: WorkgroupContext | None`。`is_workgroup_admin`/`is_specialist`/`has_peer_channel` 改为 property 委托到 `workgroup`。

**为什么保留 property 作为读 API**：`agent/mcp_endpoints.py`、`tools/registry.py` 都通过 property 读 → 0 改动。真正要改的只有写入侧（env_builder、heartbeat）和读裸字段的 context.py。

**为什么 env_builder 的 workgroup 守卫用 `role or has_peer_channel or agents`（不含 running_tasks/peers）**：role/peer/agents 是身份字段，running_tasks/peers 是动态状态。普通 bot 的 `get_running_tasks` 是 None，不会误建 WorkgroupContext。

**Router 不动**：Phase 1 只归并 AgentEnv（被传遍 dispatch 链的痛点）。Router 是装配期对象，污染影响面小，留待后续。

**测试**：新增 `tests/unit/test_agent_env_workgroup.py`（6 例：property 委托 + 字段已删）。存量 6 个测试文件改构造点为嵌套形式。全量 1079 passed（基线 1073 + 6）。

## 2026-06-28 — workgroup 隔离 Phase 2 + 3（yait #98）

接 Phase 1（数据类归并），继续把 workgroup 知识从 core 收回 workgroup 包。

**Phase 2 — system-prompt 片段外移**：`router/context.py` 不再硬编码 `[Workgroup]`/`[Peer Messaging]` 段，也不再 `import workgroup.formatting`。渲染逻辑（含 `_format_peer`）搬进 `workgroup/prompt_fragment.py: build_workgroup_block()`；context.py 缩成一个 `if workgroup_agents or has_peer_channel:` 守卫委托。输出逐字节不变。

**Phase 3a — gateway 装配外移**：`WorkgroupManager` 的构造 + `set_workgroup_manager`(topology/peer/web) + start 搬进 `workgroup/wiring.py: install_workgroup(gateway, storage)`。gateway 18 行装配块 → 3 行守卫调用。`WorkgroupManager` import 降级到 TYPE_CHECKING + 字段注解字符串化（gateway 无 `from __future__ annotations`）。

**Phase 3b — peer 条件化构造（方案 A）**：`cluster/peer_service.py` → `workgroup/peer_service.py`。PeerService 改为**仅 `config.workgroups` 时构造**（`gateway._peer` 否则为 None）。配套：
- `ClusterHttpRoutes.register()` 拆分：`/api/guest/ws` 永远挂（核心 cluster），`/api/peer/*` 仅 `peer is not None` 时挂
- `InternalApiServer.peer` 改 Optional，`/api/peer/send` 条件注册
- `send_to_peer` 工具加 `_peer is None` 防御守卫

**为什么 peer 构造守卫留在 gateway cluster 阶段（而非 install_workgroup 内）**：peer 路由必须在 `web_server.start()` 之前注册（aiohttp 启动后 router 冻结），而 install_workgroup 在其后。所以构造点必须前置，用 `if config.workgroups` 守卫保证条件化。

**达成**：删 `workgroup/` 包后，core/cluster 运行时无 import 崩溃（gateway 的 workgroup 运行时 import 全在守卫块内；cluster 对 workgroup 只剩 TYPE_CHECKING）。peer 消息能力随 workgroup 一起出现/消失。

**测试**：新增 test_workgroup_prompt_fragment.py（5）+ test_peer_conditional_wiring.py（2）；peer 3 个测试改 import 路径。全量 1086 passed（基线 1073 + 13）。

## 2026-06-28 — topology→workgroup 依赖反转（yait #98 Phase 5）

接 Phase 1-4，处理最后一处**真实的 cluster→workgroup 运行时耦合**：`TopologyService` 此前通过 `set_workgroup_manager` 持有 `WorkgroupManager`，在 `build_peer_descriptors` / `push_peers_snapshot_to_sats` 里读 `workgroup_manager.routers` 拿"本机活跃 workgroup admin 名字"。

**反转**：
- 删 `TopologyService.set_workgroup_manager` + `self.workgroup_manager` + TYPE_CHECKING 的 `WorkgroupManager` import
- 改为 `set_local_workgroup_provider(callable)` —— 一个返回本机活跃 workgroup admin 名字列表的回调
- `workgroup/wiring.py` 在 install_workgroup 里注册 `lambda: list(manager.routers.keys())`
- `config.workgroups`（core 的 AppConfig 字段）读取保留 —— 那是 config 字段访问，非 workgroup 包依赖

**效果**：cluster 层不再 import / 持有 `WorkgroupManager`。依赖方向掰正：不再是"cluster 设施反向依赖 workgroup"，而是"workgroup 启动时把自己的 bot 名字注册进 cluster"。`cluster/` 对 workgroup 现在只剩 `http_routes.py` 一处 TYPE_CHECKING 的 `PeerService` 类型注解（无运行时依赖）。

**为什么不连 config.workgroups 一起反转**：`config.workgroups` 是 AppConfig（core）的字段，topology 读它是读 config，不是依赖 workgroup 包。强行也反转会把 `local_bot_descriptors`（按 web_channels 枚举本机 bot）搅复杂，收益不抵成本。

**测试**：`test_topology_service` 的 set/assert 改 provider；`test_cluster_peer_e2e` 的 build_peer_descriptors 注入改 provider。全量 1064 passed（行为逐字节不变）。

## 2026-06-28 — workgroup 测试拆分进 tests/unit/workgroup/（yait #98 收尾）

代码隔离进 workgroup 包后，把测试也对称拆开。

**新建 `tests/unit/workgroup/`**（含 `__init__.py`，与 `tests/unit` 包结构一致）。

**拆 `test_workgroup.py`（598 行大杂烩，13 类混了 4+ 源模块）** 按源模块切分：
- `test_formatting.py` ← format_running_tasks / extract_specialist_response
- `test_heartbeat.py` ← is_silent_reply / _extract_action / _build_heartbeat_prompt / HeartbeatManager read-md / log facade / fork-skip（6 类 + 模块级 log 测试）
- `test_workspace_templates.py` ← seed_admin/specialist_workspace + 模板格式
- `test_manager.py` ← WorkgroupManager 纯方法
- `TestBackendForkCapability`（测 backend.supports_fork，**非 workgroup**）移到 `test_agent_backend_protocol.py`

**移入子目录**（去冗余 `workgroup_` 前缀）：integration / web_e2e / channel_adapter / prompt_fragment / config / template_loader / peer_service / agent_env。

**刻意留在 `tests/unit/`**（cluster/peer-routing 集成，非纯 workgroup 包测试）：`test_cluster_peer.py` / `test_cluster_peer_e2e.py` / `test_peer_conditional_wiring.py` / `test_topology_service.py`。

**不变量**：全量仍 1064 passed —— 纯重定位，零测试增减。用脚本按 class 边界切分 + 自动检测每文件实际用到的 import（避免 F401）。

## 2026-06-29 — 修 WebUI 跨机整条丢消息：/api/send fire-and-forget（yait #100）

症状：guest 浏览器正常聊天整条回复偶发消失 + 504 host timeout。非 buffer/queue（queue full 0 次）、非重连。

根因：`WebChannel.inject()` `await on_message(msg)` 阻塞整轮；guest 的 `/api/send` 经 `guest_client.call` 中继到 host，硬超时 30s（rpc.py:92）。任何 >30s 回复 → 504 → turn 丢。回复本应走独立 SSE `/api/stream`（`_proxy_via_host_stream`，无超时），send 不该等整轮。

修复：删掉 `inject` 里阻塞的 `await self.on_message(msg)`，换成一行 `asyncio.create_task(...)`。回复/错误走独立 SSE，`/api/send` 立即返回不撞 30s。活跃 turn task 一直在 await I/O、被 loop 引用，不会被 GC，无需额外跟踪。测试新增"慢 handler 下 inject 不阻塞"。

## 2026-06-30 — 前端 Web Component 试点：<tool-card>（无框架/无 build）

目标是**可维护性**（不是减行数）。把 tool-call 卡片抽成原生 custom element，放进独立文件 `static/components/tool-card.js`，作为前端组件化 + 文件拆分的第一刀。

- 新建 `<tool-card>`：自包含 DOM + `setCall()`/`setResult()` 生命周期；connectedCallback 延迟构建，兼容 history 的 detached fragment（setCall/setResult 先缓冲，连接时渲染）。
- app.js 删掉 `_buildToolCard`/`_applyToolResult`/`_argSummary` + `state.toolCards` 注册表，改为 `document.createElement("tool-card")` + 按 `data-tool-id` DOM 查找。
- index.html 加一行 `<script src="components/tool-card.js">`（classic script，全局注册，无 ES module/无 build）。

**净行数 +56**（组件 91 − app.js −35）—— 印证"WC 换结构不减行数"。收益是 tool-card 逻辑内聚、app.js 不再 juggle DOM 引用注册表。

**风险/限制**：前端 0 测试，本改动靠浏览器手验（live 工具卡 / 历史卡 / ✓✗ 结果 / 折叠 / subagent 嵌套）。若继续 WC 化，需先加 jsdom smoke-test 作安全网。

## 2026-06-30 — 前端组件测试：node --test + 自写 DOM stub（无 jsdom/npm）

WC 化后每个组件都要肉眼手验，迟早漏。加自动测试，但**不引入 jsdom/npm 工具链**（项目刻意 vanilla 无 build）。

方案：`static/test/dom-stub.js` 自写最小 DOM（createElement / classList / append / querySelector 支持 tag+[data-attr]+.class / dataset↔attribute / custom-element upgrade + connectedCallback），`load.js` 用 `vm.runInThisContext` 把 util.js + 组件源码 eval 进 stub 全局，`*.test.js` 用 Node 内置 `node:test` 断言。

接进主套件：`tests/unit/test_web_frontend.py` shell 出 `node --test static/test/*.test.js`，`uv run pytest` 一并跑（node 缺失则 skip）。

覆盖：util escapeHtml/renderMarkdown（含 XSS fallback）、tool-card upsert/幂等/result/synth/subagent/**detached-fragment 连接时渲染时序**、chat-message markdown/user 转义/setText 流式/data-id/时间戳。15 个子测试。

**为什么不 jsdom**：一个个人工具，自写 stub ~140 行覆盖组件实际用到的 DOM 子集，比拉进 npm + node_modules + package.json 这套异质工具链更轻、更符合无 build 的取向。stub 不是通用 DOM，够测这些组件即可。

## 2026-06-30 — 从 web 层移除 workgroup（前端 UI + server + set_main）

延续 route-B（workgroup 可插拔），把 workgroup 从 **web 层**整体拿掉。

**前端**：删 specialist 抽屉 UI（loadSpecialists/renderSpecialistsInto/selectSpecialist/isSpecialistChat + renderMachines specialist 块 + workgroup: platform 分支）；删 **"set as main"**（badge/链接/setMainSession/is_main）—— 它本就是 workgroup 专属（tooltip：heartbeat/peer 消息路由进该会话）。

**server.py**：删 `_handle_web_bots` 的 workgroup 分支、`/api/claude/resume` 的 workgroup backend/model/workspace/pool fallback、`set_workgroup_manager` + `workgroup_manager` 字段、`/api/sessions/set_main` 路由+handler、session 列表的 `is_main`。

**topology**：删 `local_bot_descriptors` 的 workgroup 分支（这才是 web 侧边栏 bot 列表的真实来源）。`build_peer_descriptors`（peer 消息路由）保留——属 workgroup 模块内部，非 web bot 列表。

**wiring**：删 `gateway._web_server.set_workgroup_manager`。

**保留**：`storage.{get,set,get_or_create}_main_chat_id`（workgroup peer/heartbeat/manager 仍用）。

**结果**：web 层零 workgroup（admin bot 不再在 UI 列出/可聊）。**workgroup 模块仍加载运行**（manager/heartbeat/peer），admin web channel 仍创建但不被列出 → 半死状态。clean finish = 删整个 workgroup 模块（另排）。app.js 1357→约 1230。全量 1066 passed。

## 2026-06-30 — 物理删除整个 workgroup 模块（route-B endgame）

接上一条（web 层移除 workgroup）的"另排 clean finish"，本次把 workgroup **整体物理删除**，结束 route-B（"workgroup 作为可插拔扩展隔离"）。

**删了什么**：
- 整个 `src/boxagent/workgroup/` 包（manager / heartbeat / peer_service / channel_adapter / config / formatting / http_routes / persistence / prompt_fragment / specialist_skills / task_queue / template_loader / templates / wiring / workspace_templates）。
- `tools/builtin/admin.py` + `tools/builtin/peer.py`（send_to_agent / send_to_peer / list_specialists 工具）。
- `AgentEnv.workgroup`（WorkgroupContext）+ `is_workgroup_admin` / `is_specialist` / `has_peer_channel` 属性 + `heartbeat_display_mode`。AgentEnv 现在零 workgroup 字段。
- `Router` 的 5 个 workgroup 字段（workgroup_agents / get_running_tasks / get_peers / has_peer_channel / workgroup_role）+ `dispatch_sync` 方法。Router 回归纯 auth/command/dispatch。
- gateway：PeerService 构造、`_peer` / `_workgroup_manager` 字段、`install_workgroup`、`/api/workgroup/*` + `/api/peer/send` 路由、InternalApiServer 的 `peer` / `workgroup_routes` 参数。
- config：`SpecialistConfig` / `WorkgroupConfig` 数据类、`AppConfig.workgroups` 字段、parse 循环。
- cluster/topology_service：`build_peer_descriptors` / `push_peers_snapshot_to_sats` / `set_local_workgroup_provider` / `_local_workgroup_names`（TopologyService 回归 machine-level only）。
- cluster/guest_client：`remote_peers` 字段 + `peers_snapshot` 帧处理。
- cluster/http_routes：`peer` 参数 + `/api/peer/send` + `/api/wg/peer/recv` 路由（只剩 `/api/guest/ws`）。
- sessions/storage：`get/set/get_or_create_main_chat_id` + `main_sessions.yaml` + 旧 `wg:`→`workgroup:` 迁移。
- log/categories：`HEARTBEAT_TICK/DRIVE/PAUSE`。
- transports/mcp/server：`/mcp/admin` + `/mcp/peer` 端点（只剩 base + telegram）。

**刻意保留**：
- backend `supports_fork` / `fork_and_send` 能力留在 Protocol（曾被 HeartbeatManager 唯一调用，现无调用者）—— 它是 backend 层通用能力，不属 workgroup 模块；删它要动三个 backend 实现 + SDK fork 调用链，属另一条重构，本次不碰，只把注释里对已删 HeartbeatManager 的引用去掉。
- `IncomingMessage.trusted`（cluster 绕 auth）、`ChannelCallback.webhook_name`（注释改为"bus replies"）。

**"peer" 语义收敛**：cluster 层的 "peer" 现在只指 **peer 机器**（拓扑里的 machine / `cluster.peer.up` 事件），不再有 "workgroup admin peer"。

**为什么物理删而不是继续留着**：route-B 隔离做完后，workgroup 模块虽可插拔但仍**加载运行**（半死状态：admin channel 创建但 UI 不列、heartbeat/peer 仍跑）。owner 决定一步到位删掉，避免长期养一个无 UI、无文档、无人用的子系统。隔离工作（PRs #15/#17）的价值正是让这次删除变成"删目录 + 摘调用点"而非大手术。

**测试**：workgroup 专属测试（`tests/unit/workgroup/`、`test_cluster_peer*`、`test_peer_conditional_wiring`、`test_prompt_tool_names`）随模块一并删除；共享测试里的 workgroup case 摘掉。基线 1066 → **886 passed**（降幅即被删的 workgroup/peer 测试）。`uv run boxagent --help` + 全量 import OK。

## 2026-07-06 — 跨机传输统一到一根 bus：chat + rpc 溶进 ClusterBus（分支 unify-message-bus）

**背景**：跨机 WS 上原有 **3 套并行机制**（chat sync / event sync / rpc）+ 9 种帧 + 3 份重复版本门。owner 要「一根 location-transparent 的抽象 bus：调用者只管把数据发给目标，local/remote 由 bus 内部决定」。设计详见 `docs/bus-protocol.md`。

**核心决策**：
- **一个原语 = pub/sub bus。request/reply 是架在其上的一层薄壳**，不是平级传输——「共用管道，不共用模式」。否掉了「把 RPC 塞进 MessageBus.publish topic」（会在有序 fan-out 上重建 correlation/超时/并发，退化 bus 不变量）。
- **哑管**：bus 只按 `topic` 投递 `(sender, receiver, payload, ts, message_id)`，payload 不拆。correlation_id/reply_to 等语义在 payload（业务层）。
- **Packet**：`message_id`(UUID，发送端 send 缝盖) + `sender` + `receiver`("" = 广播 / 有值 = 点对点) + `topic` + `payload` + `ts`。cluster 实现外层包 `{v, packet}`，`v` 不进 packet。
- **location-unified ≠ transparent**：调用方给目标地址、不写 `if local/else remote`，但失败仍可见（request 返回真 web.Response 502/504/409）。
- **硬切版本门**：ClusterBus WIRE_VERSION 2→3，缺失/异版本一律 drop（不再默认放行）+ 发端 fast-fail（on_unreachable → 失败 pending，不干等 30s）。
- **两跳中继**：host 按 `receiver` 转发 packet，删掉旧「loopback-reissue-for-two-hop」hack；127.0.0.1 真 HTTP loopback 只保留在 responder 跑本机真 handler（auth）。

**删了什么**（净删 ~642 源码行 + 大量测试）：
- `cluster/rpc.py`(ClusterRpc)、`cluster/rpc_over_bus.py`(RpcChannel/InboundRequestExecutor)、`cluster/chat_sync.py`(ChatSyncer)、`cluster/chat_bus.py`(ChatBus)。
- registry/guest_client 的 `GuestSession.call`/`_serve_inbound_rpc`/`_handle_rpc`/`RpcChannel`/rpc·rpc_resp 帧。
- 测试：`test_cluster_rpc`、`_rpc_bus_harness`、`test_rpc_bus_harness`、R1–R6 invariants、`test_chat_sync`、`test_chat_bus` + invariants 的 chat 段(A2/A3/C/D1/D3/E2/F1)。

**新增**：`cluster/cluster_bus.py`(ClusterBus，一个 `_forward` 3 规则)、`cluster/request_reply.py`(RequestReply，旧 ClusterRpc 的 drop-in：dispatch_machine_request + handle_guest_ws)。测试 `test_cluster_bus`、`test_request_reply`；`bus/core.py` 加 `send()`。

**刻意保留 events 走 EventSyncer**：events 跨机复制需要 `(origin_machine, origin_seq)` 去重 + resync-on-reconnect，naive broadcast 给不了；这是 pub/sub 之外 legitimately 不同的**可靠复制**关切，不是冗余。`events/sync.py` + `bus_wiring.py`(收成 events-only) + `peer_transport.py`(events 帧 WIRE_VERSION=2) 保留。若将来迁 events，见 `docs/bus-protocol.md` 的 Open。

**部署**：big-bang——代码在分支上按可测小步走（每步跑测试），最后全 fleet 一起重启（新旧 bus 不互通，无灰度窗口，3-4 台个人机可接受）。基线 984 → 迁移后 952 passed（降幅=删的 rpc/chat 测试 − 新增 bus 测试）。

## 2026-07-07 — Web UI 推送多路复用：每 chat 一条 SSE → 整页一条 WS（分支 sse-multiplex）

**病因**：Web UI 每打开一个 chat 就开一条 SSE 长连接 `GET /api/stream?bot&machine&chat_id`（`_handle_web_stream` 订一个 bus topic `chat.<owner>.<bot>.<chat_id>` 逐条 SSE 推给浏览器）。一个 chat = 占一个浏览器 HTTP/1.1 连接槽；单 host 上限 ~6 个并发连接，开几个 chat + 其它请求就把槽占满，整个 UI 卡住排队。aiohttp 不支持 HTTP/2（官方 not planned），扩连接数这条路堵死。

**方案（选 WS，非 SSE+POST）**：
- 动态订/退需要 client→server 指令通道。SSE 单向，要么配一个 POST 控制端点（需 server 用 connection-id 把无状态 POST 和某条在飞 SSE 关联起来，多一层握手），要么直接上 WebSocket（双向，订/退天然干净）。选 **WS**：一根 socket = 永远只占 1 个槽，订/退 + 未来 client→server 需求都骑同一条；唯一代价是重连，用 backoff + 重连后重报订阅集兜住。
- 旧 `/api/stream` **保留不动**（向后兼容 / iOS 等其它消费者），只新增 `/api/multiplex`；前端 chat 路径切到新端点。

**后端**（`transports/web/server.py`）：新增 `_handle_web_multiplex`——一条 WS 持有**多个** bus 订阅（`dict[topic, Subscription]`），收 `{type:subscribe|unsubscribe, machine, bot, chat_id}` 控制帧动态加/减订阅；每条事件用新的 `TaggedQueueSubscriber`（`bus/subscriber.py`）打上 `{machine,bot,chat_id, event:{...}}` 标签汇入一根共享 queue，pump task 逐条 `send_json` 推下去；WS 关闭时 finally 里 `subscription.close()` 全部退订。topic 解析抽成 `_resolve_chat_topic()` 与旧 SSE handler 共用。`bus/core.py` 加只读 `has_subscribers(topic)` 供测试断言订阅生命周期（不 peek 私有 `_exact`）。

**前端**（`transports/web/static/`）：新增 `multiplex.js`（`MultiplexClient`，一根页面级 WS + backoff 重连 + 订阅集 + 按 `machine|bot|chat_id` demux）。`chat-controller.js` 的 `switchChat` 不再 `EventSource.close()` + 开新流，改成对上一个 chat `app.multiplex.unsubscribe` + 对新 chat `subscribe(handler=handleEvent)`；`openStream()` 保留名字但改成订阅一条 tag。连接数从 N 降到 1。`events.js`（Events 页那条独立 SSE）不在此列，未动。

**测试**：后端 `tests/unit/test_web_multiplex.py`（真 aiohttp server + 真 ws client + 真 MessageBus：订阅→publish→tagged 帧→退订→断连清理，5 条）。前端 `test/multiplex.test.js`（fake WebSocket 注入 via `app._makeSocket`：单 socket 多 chat、demux、退订、幂等、缓冲、drop 重连、close 不重连、token 上 URL，9 条）；`chat-controller.test.js` 的 `es`/EventSource 断言改为 `app.multiplex` spy。基线后端 886 → 958 passed；前端 76 → 85 passed。

## 2026-07-07 — 修 fast-fail 版本验证：取活连接握手值 + 未知放行（分支 fix-version-refresh）

**病**：PR #37 的跨机 fast-fail 上线后，mbp 一直报 `devbox-xl is incompatible (wire version 0)`，即使 devbox-xl 已升级重启。**重启 mbp 才好**。两个错：

1. **判据用了会 stale 的异步快照**：`version_for` 对「guest 看 host」这条路读的是 host 异步推来、缓存在 `guest_client.remote_machines` 的 machines_snapshot。快照晚到/漏推/重连时序不对，缓存就 stale 在 0，dispatch 一查还是 0 → 一直挡。只有重启清缓存才恢复。
2. **把「0（未知）」当「不兼容」**：0 有三义混一起——真老机器 / 同协议但旧构建没在握手报 v（如 5705858）/ **还没学到版本**。后两种被误杀。

**修**（都取活连接握手值，重连即刷新，绝不读 stale 缓存）：
- welcome 帧带上 host 的 `machine_id`（`GuestRegistry.local_machine_id`，host_election 注入）；guest 收 welcome 存 `host_machine_id` + `host_version` 活值，断连清 0。
- `version_for`：guest 看 host 直接读 `guest_client.host_version`（活值），不读快照；host 看 guest 仍读 `GuestSession.version`（本就是活的）；看其他 guest（无直连）才兜底读快照。
- `dispatch_machine_request`：**只对确知不同版本**（正数且 != 本机）回 502；**0/未知放行**（宁可走一遭，兼容就成、真不通才走原 timeout），不误杀。

**改的文件**：`cluster/registry.py`（local_machine_id 字段 + welcome 带 machine_id）、`cluster/host_election.py`（注入 local_machine_id）、`cluster/guest_client.py`（host_machine_id/host_version 活值 + welcome 存 + 断连清）、`cluster/topology_service.py`（version_for 走活值）、`cluster/request_reply.py`（dispatch 未知放行）。测试 `test_cluster_fast_fail.py` 加 5 个（活值盖 stale 快照、未知放行、welcome 带 machine_id 等）。972 passed。

## 2026-07-13 — 修 guest 重连竞态：旧协程 finally 误删新连接的 ClusterBus link

**病**：mbp（guest）到全网所有远端 bot 的 RPC 全部 30s 超时，但 web UI 里 mbp 及各机器都显示在线、心跳新鲜。从 host（devbox-xl）侧反向验证：`host→mbp` 秒回 `mbp unreachable`，`host→macmini` 正常。即 host 认为 mbp 在线，却路由不到它。

**根因**：`GuestRegistry.handle_ws` 的 `finally` 块里 `detach_link` **无条件执行**，而紧邻的 `sessions.pop` 有 `not session._closed` 守卫——两者不对称。guest 重连时新旧两个 `handle_ws` 协程短暂并存：新协程处理 hello 时逐出旧 session（`old._closed=True` + `close(old.ws)`）并 `attach_link` 自己的 link；随后被关的旧 ws 让旧协程从 `receive_text` 抛 `WebSocketDisconnect` → 进旧协程 finally → **无条件 `detach_link` 把新连接刚 attach 的 link 删掉**。结果：`registry.sessions[mbp]` 是新 session（拓扑在线、心跳走**广播**路径不查 link），但 `cluster_bus._links` 没有 mbp（点对点走 `_usable` 门被判 unreachable）→ 一切发给 mbp 的点对点 packet（**含 RPC 应答**）被丢弃 → mbp 发的请求到得了 host、host 也处理了，但应答回不来 → 全部超时。

**触发条件**：guest 的旧连接**没被及时回收**就发生重连（网络抖动后 host 靠 ~30s 心跳才发现旧 ws 死、或进程重启与旧连接回收重叠）。所以"重启 mbp"这种 naive 补救**会再次触发同一竞态**，不可靠——现象上就是"越重启越坏"。这与 [07-07 fix-version-refresh] 是同一家族（guest 重连留下 host 侧 stale 状态），但那次是 stale 版本缓存，这次是 link 生命周期。

**修**：把 `detach_link` 挪进 `not session._closed` 守卫，与 `sessions.pop` 同条件。被顶掉的旧协程（`_closed=True`）在 finally 里**什么都不碰**——link 和 sessions 槽位都已被新连接接管；只有真·断连（未被顶掉）才 detach + pop。1 处 ~6 行改动。

**改的文件**：`cluster/registry.py`（finally 守卫）。测试 `tests/unit/test_cluster_reconnect_race.py` 加 2 个（重叠协程后新 link 仍在；正常断连仍 detach + 清 session），用能被 `close()` 确定性触发断连的 fake WebSocket 驱动真 `handle_ws`。977 → 979 passed。

**清现存幽灵态**：代码修好后仍需让当前"卡住"的 guest 干净重连一次（停掉 → 等 host 标 offline → 再启动），或重启 host 清空 link 表。
