# Cluster：多机互联

> 依据源码：`src/boxagent/cluster/`。物理层——机器怎么发现彼此、连成 hub-and-spoke、
> 跨机联邦读写。消息**语义**（bus/packet）见 [Bus 与事件系统](bus-and-events.md)。

## 形态：hub-and-spoke

一张网里，**一个 active host** 独占管理 devtunnel，其余节点当 **guest** 拨向 host。所有跨机流量
经 host 中继（guest 只连 host → 树形拓扑，天然无环）。**host 和 guest 的 web UI 都联邦显示全网 bot**。

只有配了 `cluster.tunnel`（即 `cluster.host` 非空）的节点才装配这套（`gateway.py:271`）；否则单机模式，
共享 bus 只是普通 `MessageBus`。

## HostElection：谁当 host（`cluster/host_election.py`）

- `cluster.host` 是**有序 fallback 列表**（如 `[mbp, devbox-xl, macmini]`）。优先级最高且可达的持有 tunnel 当 host，其余当 guest。**角色运行时决定并周期重估**（`probe_interval=10s`）。
- 状态：`init` → `host` | `guest` | `standalone`。
  - guest 的 upstream 掉线且自己是下一顺位 → 升 host。
  - 更高优先级候选以 guest 身份出现 → 当前 host 自愿降级。
  - `devtunnel host` 进程意外退出 → 降级，下次 probe 重选。
- **防 split-brain**：自我升级前先 `promote_retry_count=3` 次重试 probe（间隔 `promote_retry_delay=2s`），避免单次瞬时 probe 失败就误抢位造成双 host（历史踩坑，probe 异常要用 `repr(exc)` 记类型）。
- 本对象在角色生命周期内**持有** `tunnel` / `registry` / `client`，Gateway 只读公开属性。

## devtunnel 生命周期（`cluster/tunnel.py` + `devtunnel.py`）

host 启动时（`ClusterTunnel`，默认 tunnel 名 `boxagent-cluster`）：

1. `devtunnel list -j` → 按 bare name 过滤。**同名 tunnel 可跨 region 存在**（`boxagent-cluster.asse` vs `.jpe1`）；`devtunnel show <name>` 是 region-ambiguous 的，会藏掉重复、让 guest 漂到 stale URL。>1 个匹配就 **warn + 选带 active host 连接的那个**，绝不自动 delete（删错 region 会把 host 自己关掉）。
2. 零匹配就 `devtunnel create`。解析出完整 `tunnelId`（带 region 后缀）后续都用它，避免歧义。
3. 注册端口 9292，spawn `devtunnel host <tunnel_id>` 子进程并保活。
4. 轮询 `devtunnel show -j` 拿 `portUri`，写 `{local_dir}/cluster-tunnel-url.txt` 供 guest 发现。

匿名访问 OK：membership 由 WS `hello` 帧里的 `cluster.token` 把关。

## host 侧：GuestRegistry（`cluster/registry.py`）

host 在 `/api/guest/ws`（`RequestReply.handle_guest_ws` → `registry.handle_ws`）接 guest 的 WebSocket。
`GuestSession` 持有 `machine_id` / `ws` / `bots` / 协商的 `version`。

**真实 WS 帧协议**（核对 `handle_ws`，`registry.py:178` 起 —— 注意文件顶部 docstring 里的
`rpc`/`rpc_resp`/`chat_*` 帧已过时，别信）：

```
Guest → Host（open 后）: {"type":"hello", machine_id, token, bots, v}
Host  → Guest         : {"type":"welcome", "v":3, "machine_id":<host>}   # host 在此 attach_link(machine_id)
双向 packet            : {"type":"packet", "v":3, "packet":{...}}          # → cluster_bus.on_inbound
双向心跳               : {"type":"ping"} / {"type":"pong"}
guest bot 变更         : {"type":"bots_update", "bots":[...]}
其他帧（event_batch/    : → on_unknown_frame（EventSyncer 处理，WIRE_VERSION=2）
  event_resync）
```

- token 校验通过后 host `cluster_bus.attach_link(machine_id, ws.send_json, version=guest_version)`（`registry.py:227`），断连 `detach_link`。

## guest 侧：GuestClient（`cluster/guest_client.py`）

- 拨向 host WS，`reconnect_delay=3s` 自动重连（指数退避）。发 `hello` 后**等 `welcome`** 才 `attach_link("host", ...)`（host 的 wire 版本要 welcome 到达才知道）。
- `host_version`（活值，重连刷新、断连清 0）—— `version_for` 直接读它判兼容，不走会 stale 的 snapshot。
- `remote_machines`：host 推来的 `machines_snapshot` 帧更新，guest 的 `_handle_web_machines` / `_handle_web_bots` 用它渲染全网视图。
- 每个入站帧：`packet` → `cluster_bus.on_inbound("host", ...)`；`machines_snapshot` → 更新缓存。

## TopologyService（`cluster/topology_service.py`）

Gateway 的 `self._topology`，机器级拓扑的只读门面：`local_machine_id` / `local_role`（host|guest|single）/
`local_bot_descriptors`（读 web_channels）/ `collect_machines` / `push_machines_snapshot_to_sats`
（host 把 snapshot 推给各 guest，排掉该 guest 自己那行）/ `remote_session_for`（host 查持有某 bot 的
guest session）。`guest_registry` / `guest_client` 是对 HostElection 持有对象的只读转暴露。

> **workgroup 删除后**，TopologyService **只描述机器级拓扑**，不再有 peer/workgroup 描述符。
> "peer" 现在只指一台 peer 机器。

## 跨机 RPC：RequestReply（`cluster/request_reply.py`）

web 端要读别机的 sessions/history/events/schedules 或 send/stream 中继时用它。**架在 ClusterBus 之上**
的薄壳（"共用管道，不共用模式"），不是独立 transport。旧 `ClusterRpc` 的 drop-in
（Gateway 里字段名还叫 `_cluster_rpc`，docstring 也还写 `ClusterRpc`，但真实类型是 `RequestReply`）。

wire 形状（都是 bus 上普通 packet）：

```
request : receiver=<target>, topic="request.<target>",
          payload={method, path, query, body, correlation_id, reply_machine}
reply   : receiver=<reply_machine>, topic="reply.<reply_machine>.<correlation_id>",
          payload={status, body, correlation_id}
```

- `dispatch_machine_request(machine, ...)`：target 是本机返回 `None`（调用方本地处理）。
  **fast-fail 版本门**：发前查 `version_for(machine)`，**确知不同版本**（正数且 != 3）<1ms 回 502，不挂满 timeout（避免卡死 web 的浏览器连接槽）；**版本 0（未知）放行**——宁可走一遭也不误杀。
- **responder 走 127.0.0.1 loopback HTTP**：把请求重新打到本节点自己的 web 端口，让真实的 `_handle_web_*` handler 带鉴权跑一遍，再发 reply 包。两跳中继（guest→host→guest）交给 bus 按 `receiver` 路由，loopback 从不中继。
- **掉线 fast-fail**：ClusterBus 报某机不可达（`on_unreachable`）→ `RequestReply.fail_unreachable` 立刻 fail 掉指向它的 pending 请求（`gateway.py:315` 接线）。

## 信任模型的软点

- WS `hello` 的 sender/token 是**纯字符串、无强验证**，host 收到中继请求后直接信任 sender（`IncomingMessage.trusted=True` 绕过 `allowed_users`）。是 cluster 信任模型的已知软点，不是 bug，但跨机 auth 时记着。

## 两个 wire 版本并存（别搞混）

- **ClusterBus packet = `WIRE_VERSION 3`**（`cluster_bus.py`）：chat 广播 + RPC request/reply。
- **EventSyncer 帧 = `WIRE_VERSION 2`**（`peer_transport.py`）：`event_batch` / `event_resync`，走 `on_unknown_frame`。events 故意没并进 ClusterBus（可靠复制关切，见 [Bus 页](bus-and-events.md#eventsyncer-为什么还没并进-clusterbus)）。

返回：[快速上手](quickstart.md) · [架构总览](architecture.md)
