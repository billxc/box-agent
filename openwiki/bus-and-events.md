# Bus 与事件系统

> 依据源码：`src/boxagent/bus/`、`events/`、`log/`。权威协议定稿见 `docs/bus-protocol.md`
> （但本页以代码为准）。

## 一根总线的设计

**一个进程一根共享 MessageBus**，events（`events.*`）和 chat（`chat.*`）**骑同一个实例**
（`gateway.py:163-176` 装配）。单机是 `MessageBus`，配了 cluster 的节点是 `ClusterBus`
（继承 MessageBus，多一层跨机转发）。持久化/广播是**订阅者行为**，不是 bus 的事。

## Packet：唯一的信封（`bus/message.py`）

`@dataclass(frozen=True)`，6 个字段，local 与 cluster 两套实现**逐字共用**（local 进程内直投，
cluster JSON 化过 WS）：

```
Packet { message_id, sender, receiver, topic, payload, ts }
```

- `message_id`：每包唯一 UUID，**发送端在 `send()` 缝盖**（transport 身份：去重/防环/trace）。区别于 payload 里的业务级 `correlation_id`。
- `sender`：造包机器的 machine_id。`receiver`：目标 machine_id，`""` = 广播，有值 = 点对点。
- `topic`：路由键（如 `chat.<machine>.<bot>.<chat>` / `events.<category>`）。
- `payload`：**对 bus core 完全不透明**，core 从不读一个字节。`ts`：调用方给（core 无时钟，为确定性可测）。

## MessageBus / LocalBus（`bus/core.py`）

同步、有序、内容无关的 pub/sub。

- `send(*, receiver, topic, payload, ts) → message_id`：盖 `message_id`+`sender`，`receiver` 为空或本机时 `_deliver_local`（`core.py:132`）。`publish(topic, payload, ts)` = 广播垫片。
- `subscribe(topic_pattern, subscriber) → Subscription`：pattern 要么 **exact**（`chat.m.b.c`），要么以 `.` 结尾的 **prefix**（`events.`）。`.close()` 退订。
- `_deliver_local`：exact 桶 O(1) + prefix 线性扫描，按注册 `order` 排序后**同步**投递，**订阅者异常隔离**（一个抛异常不影响其他）。
- **先注册先投递 + 同步** 是硬契约：EventBus 的 StoreSubscriber 必须最先且同步跑（保证 publish 返回前库里已有行）。
- `watch_subscriptions(prefix, on_add, on_remove)`：订阅观察者，chat bridge 用来把"本机有人在看某远端 chat"的 demand 沿链路上传。
- **坑#1**：每条消息**不** `create_task`；所有 async 挪到单个 pump task（ClusterBus 的发送队列）。

## ClusterBus：跨机转发（`cluster/cluster_bus.py`）

`ClusterBus(MessageBus)` —— 本机 fan-out + 把 packet 经 WS 链路转发到别的机器，`send`/`subscribe`
接口跟 local 一模一样，**调用方从不分本地/远端**，`receiver` 决定一切。

- `WIRE_VERSION = 3`（**硬切**：`v` 缺失或 != 3 的入站帧直接 drop，不误解析）。wire 帧 `{"type":"packet","v":3,"packet":{...}}`。
- 一个 `_forward`（`cluster_bus.py:121`）出入站共用（出站 `from_link=None`）：
  1. `receiver ∈ ("", self)` → `_deliver_local`。
  2. 广播（`receiver==""`）→ 发给**所有别的链路**（排除 `from_link`）。
  3. 点对点 → `route(receiver)` 拿链路 key，`_usable`（存在且版本==3）就发，否则 `on_unreachable`。
- **Hub-and-spoke 无环**：guest 只连 host → 树形拓扑，`from_link` 排除即防环，不建 seen-set。host 是唯一中继 + 广播扇出点，guest→host→guest 两跳自然发生。
- sync→async：`_enqueue` 进单条有序队列，`_drain` task 逐帧 `await ws.send`（守坑#1 + 同链路不乱序）。

RPC（request/reply）也**骑这根 bus**，见 [Cluster](cluster.md#跨机-rpcrequestreply)。

## 事件日志：log facade → EventBus → EventStore

### log facade（`log/facade.py`）——业务代码唯一入口

```python
from boxagent.log import Category, log
log.info(Category.XXX, "message", key=value)   # levels: debug/info/warning/error/notify
```

- `log.<level>(category, message, **meta)` **永不抛异常**（sink 抛了打 stderr 吞掉）。
- `Gateway.start` 调 `log.bind(EventBus)` 之前，所有调用是 no-op（`NullLogger`）。
- **业务代码禁止直接 import `boxagent.events`** —— `events/` 是实现细节，要换 backend 不破坏调用方。Category 常量在 `log/categories.py`。

### EventBus（`events/bus.py`）——log sink + 事件 pub/sub

- 实现 `LogSink.publish(level, category, message, **meta)`。内部拥有那根共享 MessageBus，`publish` 把 payload 发到 `events.<category>` topic。
- **第一个 bus 订阅者是 `StoreSubscriber`**（`events.` 前缀，同步）：写 SQLite、mint `id` + `origin_seq`，把 enrich 后的 `Event` 塞回 `payload["event"]`。后续订阅者（web SSE / notifier / syncer）读同一个 `Event` 对象。
- `EventBus.subscribe(callback)` 是兼容垫片（callback 收 `payload["event"]`）。

### EventStore（`events/storage.py`）——唯一 SQLite writer

- `events` 表：`(id, origin_machine, origin_seq, ts, bot, level, category, message, meta_json, read_at)`，`(origin_machine, origin_seq)` 唯一（跨机 gossip 去重的自然键）。WAL 模式。
- `sync_cursor` 表：`(peer_machine, last_seen_seq)`，跨机复制游标。
- `/api/events` 读路径直接 `store.query`。

### events.* 的其他订阅者

| 订阅者 | 文件 | 干什么 |
|---|---|---|
| `StoreSubscriber` | `events/store_subscriber.py` | 本地写库（第一 slot，同步） |
| `EventStreamSubscriber` | `events/web_stream.py` | 推 web UI `/api/events/stream` SSE |
| `TelegramNotifier` | `events/telegram_notifier.py` | 独立 Telegram 推送（按 `notify_telegram_levels/categories` 过滤） |
| `EventSyncer` | `events/sync.py` | 跨机全量复制（见下） |
| `RetentionSweeper` | `events/retention.py` | 周期清理过期事件 |

### EventSyncer 为什么还没并进 ClusterBus

事件跨机复制**仍走 `events/sync.py` + `peer_transport.py`（WIRE_VERSION=2 帧 `event_batch`/`event_resync`）**，
没塌进 ClusterBus 的 packet 路径。原因（`docs/bus-protocol.md` "落地状态"）：可靠复制需要
`(origin_machine, origin_seq)` 去重 + 重连 resync，这是 naive broadcast 给不了的可靠性关切，**值这个
复杂度，故意不迁**。所以现在 cluster WS 上同时有两个 wire 版本：**ClusterBus packet = v3**，
**EventSyncer 帧 = v2**。

下一步：[Cluster](cluster.md) 看这些帧在物理上怎么连起来。
