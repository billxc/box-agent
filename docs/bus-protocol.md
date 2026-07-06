# Bus 协议（现行设计 · WIP）

> 本文取代 `message-bus-unification-提案.md` 里的「具体接口」一节。那份是历史参考
> （A1「保留 request/reply 独立门面」的老思路，已被 owner 否掉）。
>
> 当前状态：**只有 Packet 定稿**。收发接口、local/cluster 两套实现、路由、掉线
> fast-fail、版本门落点——尚未设计（见文末 Open）。

## 设计原则（owner 拍板）

1. **只有一个原语：pub/sub 的 bus。** request/reply 不是它的平级兄弟，是**架在它之上**的
   一层业务模式（publish 一个请求 + await 一个 reply）。原来独立的 `rpc` 帧 / `RpcChannel`
   传输**溶解进 bus**，不再单独存在。
2. **bus 只负责收发。** 底下 **local 一套实现、cluster 一套实现**。调用方只跟 bus 打交道，
   local 还是 remote 由实现决定 —— location transparency 的天然归宿就是 pub/sub。
3. **哑管：语义属于业务层。** 数据 vs 控制、subscribe/unsubscribe/request/reply、
   correlation —— 全是业务层的事，编码在 `payload` / `topic` 里。bus 不拆 `payload` 一个字节。
4. **寻址是 transport 的事。** `sender`/`receiver` 是地址，`topic` 是频道/主题，`payload`
   是业务。三者职责分明。

## Packet（定稿）

Packet 是 local 与 cluster 两套实现**共用的货币**。local 实现进程内直接投递（不序列化）；
cluster 实现把它 JSON 化过 WS。

```
Packet {
    message_id: str      # 每包唯一 UUID，发送端生成。transport 身份：去重 / bridge 防环 / trace
    sender:     str      # machine id，永远是"造这个包的机器"
    receiver:   str      # machine id；空 = 广播，有值 = 点对点
    topic:      str      # 频道/主题，选"机内哪些订阅者"
    payload:    dict     # 不透明业务数据（correlation_id / reply_to / kind … 全在这）
    ts:         float    # 调用方给的时间戳（core 无时钟，为确定性 + 可测）
}
```

**cluster 实现的外层 frame**：`{ v, packet: {…} }` —— `v`（wire 版本）是 cluster 独有关切
（local 进程内没有版本问题），**不进共享 packet**。

### 字段说明

- **`message_id`** vs payload 里的 `correlation_id` 是两回事：
  - `message_id`（packet，transport 层）：**每个包都有**（含广播），UUID，发送端盖。去重 /
    防环 / 端到端 trace。
  - `correlation_id`（payload，业务层）：只有 request/reply 包才有，把 reply 对上 pending 请求。
- **`sender`** 永远是发起机器 → reply 靠它知道回哪；bridge 用它防环（自己发出去的广播不往回转）。
- **`receiver` 空/非空**是广播与点对点的唯一分流开关。

## 投递语义（由 receiver × topic 推出）

| `receiver` | `topic` | 怎么投 |
|---|---|---|
| 空 | 有 | **广播**：所有有匹配订阅的机器都收，机内按 topic 扇出（chat 流 / events）|
| 有值 | 有 | **点对点**：先路由到 receiver 那台机，机内再按 topic 扇给匹配订阅者（request/reply）|

- `request`  = 点对点包，`receiver` = 目标机。
- `reply`    = 点对点包，`receiver` = 原请求的 `sender`，`payload.correlation_id` 对号入座。
- 两种模式**共用一个 packet**，无 `kind` 字段。

## 接口（定稿）

`Bus` 纯收发，就两个方法。`LocalBus`（进程内）和 `ClusterBus`（= LocalBus + 一个 packet
bridge）都满足它。

```python
# bus/subscriber.py —— deliver 收整个 Packet（不是只 payload）
class Subscriber(Protocol):
    def deliver(self, packet: Packet) -> None: ...

# bus/core.py
class Bus(Protocol):
    def send(self, *, receiver: str, topic: str, payload: dict, ts: float) -> str: ...
    #   内部造完整 Packet（补 message_id + sender），返回 message_id
    def subscribe(self, topic_pattern: str, subscriber: Subscriber) -> Subscription: ...
    #   topic_pattern: exact 或 "prefix." —— 沿用现有匹配
```

- **`send` 同步**：本机扇出同步有序（守坑#1，无 per-message task）；跨机那份 = 投进 pump
  队列 fire-and-forget，不 await。要 await 的「回复」在业务层壳里，不在 `send`。
- **`message_id`（UUID）+ `sender`（本机 id）在 send 缝盖** —— id 生成集中一处（工厂可注入
  测试）、调用方无法伪造 sender。`ts` 暂仍由调用方传（沿用现状），将来可同样挪到 send 缝。
- **`deliver` 收整个 Packet** —— 业务层要 `sender` 做 reply 路由。`QueueSubscriber` 仍只把
  `payload` 塞队列（SSE 消费方不变）。

### 两个实现的 `send` 路由（唯一区别）

| | `receiver==""`（广播） | `receiver==self` | `receiver==远端` |
|---|---|---|---|
| **LocalBus** | 本机按 topic 扇出 | 本机扇出 | 不可达（单机不该出现）|
| **ClusterBus** | 本机扇出 + ship 给有 demand 的 peer | 只本机扇出 | 只 ship 给那台机 |

- ClusterBus 收到远端 packet → **只注回本机**扇出（不再 ship，防环靠 `sender`/`message_id`）。
- `subscribe` 两边一致。ClusterBus 订「远端拥有的广播 topic」时内部往上游传 demand ——
  **实现细节，不进接口**。
- **点对点（request/reply）不需要 demand**：requester `send(receiver=目标机)` 直投；
  responder 在目标机 `subscribe(请求topic)` 接住。demand 只为广播流（chat/event）存在。

### 什么在接口 / 什么不在
- **在**：`send` + `subscribe`，纯收发。
- **不在**（业务层架在其上）：`request()`/`reply` 壳（correlation_id + timeout + 掉线
  fast-fail）、chat 订阅、event publish —— 全用 `send`+`subscribe` 拼。

## 删除账（KILL LIST）

本次不是"加适配层复用旧件"，是**整段删**。今天 WS 上跑着 **3 套跨机机制（chat sync /
event sync / rpc）+ 9 种帧 + 3 份重复版本门**，packet bus 把它们塌成 1 套。

### 整删（实测行数）= 1083 行

| 文件 | 行 | 谁吃掉它 |
|---|---|---|
| `cluster/chat_sync.py` `ChatSyncer` | 262 | 泛化成一个 packet bridge |
| `events/sync.py` `EventSyncer` | 245 | 同上，跟 chat 同构，合成一个 bridge |
| `cluster/rpc_over_bus.py` `RpcChannel`/`InboundRequestExecutor` | 221 | correlation 降成业务层 `request()` 壳；loopback 候选全删 |
| `cluster/bus_wiring.py` | 111 | 只剩一个 bridge，wiring 塌成一处 |
| `cluster/rpc.py` `ClusterRpc` | 97 | 调用方走 bus，寻址靠 `receiver` |
| `cluster/peer_transport.py` `PeerTransport` | 85 | 吸收进 `ClusterBus` |
| `cluster/chat_bus.py` `ChatBus` | 62 | 调用方直接 `bus.subscribe(chat_topic)` |
| **小计** | **1083** | |

### 瘦身里再删的 rpc-相关（估算）≈ 175 行
`guest_client.py`(~70) + `registry.py`(~90) + `events/bus.py`(~15) —— 删掉 `GuestSession.call` /
`_serve_inbound_rpc` / `_handle_rpc` / `RpcChannel` 字段等，留纯 WS 链路。

### 新增（估算）≈ 330 行
Packet +3 字段(~6) · `LocalBus.send()`(~20) · `cluster/cluster_bus.py`(~230) ·
request/reply 壳(~60) · 统一版本门(~15)。

### 净账
- **源码：删 ~1258 − 加 ~330 ≈ 净删 900 行。** `cluster/` 2619 → ~1850，传输层砍 1/3。
- **测试：整删 ~1018 行**（`test_chat_sync`330 + `test_event_syncer`245 + `test_bus_wiring`185
  + `test_chat_bus`116 + `test_peer_transport`87 + `test_cluster_rpc`55），三套机制的测试塌成一套；
  `test_message_bus_invariants.py`(1023) 的 R1–R6 + `_rpc_bus_harness.py`(447) **保留复用**当回归网。

### 概念数塌缩（比行数更重要）
| | 现在 | 之后 |
|---|---|---|
| 跨机机制 | 3 套 | 1 套 |
| WS 帧类型 | 9 种 | ~3 种 |
| 版本门 | 3 份重复 | 1 个 |
| 调用方跨机分叉 | 13 处 `dispatch_machine_request` + local/remote if | 0 |

## ClusterBus 路由（定稿）

ClusterBus 持有：`local_machine`、内层 `local_bus`、`links`（我的 WS 链路）、
`link_to(machine)`（去某机走哪条链路：guest 永远是 host，host 是那个 guest 的链路）。

出站入站共用**一个** `_forward`，出站 = `from_link=None`：

```python
def send(*, receiver, topic, payload, ts) -> str:
    message_id = new_uuid()                         # send 缝盖 message_id + sender
    packet = Packet(message_id, local_machine, receiver, topic, payload, ts)
    _forward(packet, from_link=None)
    return message_id

def on_inbound(link, frame):                         # WS 读循环喂进来
    if not version_ok(frame):                        # ⑤ 版本门（见下）
        _reject(link, frame); return
    _forward(frame["packet"], from_link=link)

def _forward(packet, from_link=None):
    # 1. 是给我的吗 → 本机投递
    if packet.receiver in ("", local_machine):
        local_bus.deliver_local(packet)
    # 2. 要往外发吗
    if packet.receiver == "":                        # 广播 → 发给所有别的链路
        for link in links:
            if link is not from_link:
                link.send(packet)
    elif packet.receiver != local_machine:            # 点对点去别的机
        link = link_to(packet.receiver)
        if link and link is not from_link:
            link.send(packet)
```

- **两跳（guest→host→guest）不是新概念**：host 收到 `receiver=某guest` 的包，走第 2 条分支
  转出去，自然发生。
- **广播不追踪「谁想要」**：直接发给所有别的链路；没订阅的机器收到直接丢。3-4 台 + chat/event
  小流量，可忽略。将来带宽疼再加过滤（YAGNI）。
- **防环靠结构**：树形拓扑（guest 只连 host）无环，`from_link` 排除即可。**不建 seen-set**。
  `message_id` 留作 trace + 未来上 mesh 的保险。
- 实际 `link.send` 是 async，`_forward` 同步 → 中间一条小发送队列 + drain（sync→async 缝，守坑#1）。

## 已定
- 设计原则、Packet、投递语义、**收发接口（`send`/`subscribe`）**、**ClusterBus 路由**、删除账。
- `message_id` 生成缝：UUID 由发送端在 `send()` seam 盖，工厂可注入（测试塞确定性 id），
  不埋进 core `uuid4()`（会破坏 core 确定性/可测）。

## 落地状态（已实现）

分支 `user/xiaocw/unify-message-bus`，big-bang（代码可测小步，最后 fleet 一起重启）。

| 件 | 落地 |
|---|---|
| Packet（6 字段，message_id UUID 发送端盖） | ✅ `bus/message.py` |
| LocalBus.send + deliver(Packet) | ✅ `bus/core.py`（MessageBus） |
| ClusterBus（`_forward` 3 规则 + 版本门 + on_unreachable + 发送队列） | ✅ `cluster/cluster_bus.py`，WIRE_VERSION=3 |
| WS 读循环路由 packet 帧 + attach/detach 链路 | ✅ `guest_client.py` / `registry.py` |
| **chat → ClusterBus 广播** | ✅ WebChannel.publish→send(receiver="")；SSE 直接 `bus.subscribe`。删 `chat_sync.py`/`chat_bus.py` |
| **rpc → request/reply 薄壳** | ✅ `cluster/request_reply.py`（ClusterRpc drop-in）。删 `rpc.py`/`rpc_over_bus.py` |
| 127.0.0.1 loopback | ✅ **保留**在 request_reply responder（跑真 handler+auth）；两跳交给 bus receiver 路由，删掉旧 reissue-for-two-hop hack |
| 掉线 fast-fail | ✅ ClusterBus.on_unreachable → RequestReply.fail_unreachable |
| **events** | ⏸ **保留 EventSyncer**（可靠复制需 dedup by (origin_machine,origin_seq) + resync-on-reconnect，非 naive broadcast 能给；这是 pub/sub 之外 legitimately 不同的可靠性关切，不是冗余）。仍走 `events/sync.py` + `bus_wiring.py`(events-only) + `peer_transport.py`(WIRE_VERSION=2 帧) |

净删（源码）：`rpc.py`(97) + `rpc_over_bus.py`(221) + `chat_sync.py`(262) + `chat_bus.py`(62)
= 642 行整删，加 registry/guest_client 的 rpc 瘦身；新增 `cluster_bus.py` + `request_reply.py`。
测试：删 rpc harness + R1–R6 + chat 不变量 + test_chat_*，加 test_cluster_bus/test_request_reply。

## Open（未排期）

- **events 上 ClusterBus**：若将来想让 events 也走一根 bus，需在广播 packet 里带
  `(origin_machine, origin_seq)` + 收端 `insert_remote` 去重，并决定是否放弃 resync-on-reconnect
  （变 best-effort）。当前判断：EventSyncer 的可靠复制值这个复杂度，不迁。
- **peer_transport / bus_wiring 消亡**：events 迁走后这俩（events 专用）才能删，届时 WIRE_VERSION
  只剩 ClusterBus 一份。
