# Message Bus 统一方案（提案 · 历史参考）

> ⚠️ **历史文档**：方案已实现并合并（PR #34，2026-07-06）。**实际落地与本提案有出入** —— Phase 8（删 EventBus）调研后回退（EventBus 保留），净收益是 +763 而非文中估的 −100。当前架构以 `docs/codebase-guide.md` + `docs/decisions.md`（2026-07-06 条目）为准；本文仅作设计讨论留痕。
>
> 状态（写作时）：**未定，待 owner 拍板**。这是 OPC 设计讨论（architect × engineer × tester 三轮 + devil's advocate 反方）的中文整合。
> 原始英文产出在 `.harness/nodes/discuss/run_1/`（decision.md / devil-advocate.md / round-*）。

---

## ⚠️ v2 更新（RPC 纳入后重算 —— 优先读这段）

> v1 的 §3 反方 / §4 对比是在**RPC 被排除**的前提下写的，LOC 账已被 v2 覆盖。下面是 owner 纠正"RPC 也得动、代码要缩"之后，三方重跑 + devil's advocate 再挑的结论。原始英文在 `.harness/nodes/discuss/run_2/`（decision-v2.md / devil-advocate-v2.md）。

### 净收益重算（含 RPC）
- **BEFORE** in-scope ≈ 664 行 → **AFTER** ≈ 807 raw，其中 ~230 是**搬过去的复制策略**（非新增）。
- **诚实净 ≈ −100 生产行**（−90~−115，±25%）—— **负值只因为 RPC 在内**（chat+event 单独还是 v1 的 +306 坑）。
- 三大塌陷：loopback executor −48、双 wiring→单 −73、RPC call+proxy 镜像 −41~55。
- 注意：这 −100 是**生产代码**；不含永久新增的测试基建（harness + ~40 条冻结不变量 + CountingEventStore + migration-map ≈ +250~400）。**全仓总量含测试是净正的。**

### RPC 怎么折叠（关键的缝）
RPC = request/reply，**骑在 transport 层**（PeerTransport + 帧 dispatch + wiring），**不进** `MessageBus.publish`/topic —— 它要 id 关联 + 并发，和 chat/event 的串行 pump 是相反模型。落成 `cluster/rpc_over_bus.py`（第三个 sibling coordinator）：
- **一份** role-agnostic `call`（host/guest 不再各写一遍）
- **一份**共享 `InboundRequestExecutor`（host `_serve_inbound_rpc` + guest `_handle_rpc` 塌成一个）
- 复用 chat 已有的 **route-to-peer + 两跳 host 中继**原语
- `bus/` core 永不认识"rpc"；server.py 里 ~15 个 `dispatch_machine_request` 调用点门面不变。

### Phase 7 从"可选"变"必须"
现在一个 WS 上是**三种**帧词汇（rpc inline switch + event_* + chat_*）。RPC 上 topic dispatch 后不统一 = 一个 socket 三个 dispatcher，比现在更糟。**v1 devil's advocate 反对 Phase 7 的前提（RPC 不动）塌了。**

### Devil's advocate v2：认输两条 + 一条更要命的新发现
- **认输**："加的比删的多"死了（RPC 镜像 ~120 行是真的，3× 当初锚的 40）；"Phase 7 纯风险"死了（三种帧词汇要塌）。
- **新发现（code-verified，很实）**：当前一条 WS 上**已经跑着两种相反的并发契约** —— 入站 RPC 是 `create_task` 的（**并发**，`registry.py:359`/`guest_client.py:283`），chat/event owner-pump **故意不用 create_task 以保序**（坑#1，`decisions.md:16`）。今天安全只因为是物理两段代码。**Phase 6/7 合进一个 dispatcher = 头一次把坑#1 和 RPC 并发放同一处。** 不变量能兜，但那是在兜合并本身造出来的风险。

### 关键裂缝：LOC 赢面 与 "一根 bus" 愿景**可分离**
devil's advocate 据此给了个更便宜的子集，**而且 LOC 减得更多**：

| | **Subset（只塌 RPC 镜像 + PeerTransport）** | **Full bus（decision-v2）** |
|---|---|---|
| 净生产行 | **−110 ~ −140**（更多！）| −100（+ ~250-400 永久测试基建）|
| 为什么更多 | 跳过 `bus/` pub-sub 核心那 **+122** | RPC 去重要先付掉那 +122 |
| 工作量 | ~2-4 个可回退 commit | 9+ phase |
| 风险 | 低（无 mixed-version、无 dispatcher 合并、无 P4/P5 hybrid）| 高（并发/串行 dispatcher 合并 + Phase 7 mixed-version + 更长 march）|
| 给不给**愿景** | ❌ 不给（chat/event/RPC 还各自 publish，只共享 link + 一份路由）| ✅ 给（content-agnostic MessageBus + envelope，三者都骑它）|

核心一句："**RPC 折叠是全部的奖赏，`bus/` 核心是全部的风险，而它俩可分离。**" —— 你要的"代码变少"**不需要** bus 愿景，分开做还减得更多、更安全。

### 修订后的 phase（Full bus 版）
P0 harness+不变量（+RPC 维度 INV-R1..R6/DR1..DR4）→ P1 抽 PeerTransport → **P1.5 新增：RPC 上 transport**（loopback executor 塌 + role-agnostic call）→ P2 bus core → P3 StoreSubscriber → P4 EventBus adapter → P5 chat 上 bus → P6 合并 syncer（+吸收 RPC 帧臂）→ **P7 load-bearing 线上帧统一**（rpc+chat+event 一个 topic dispatch）→ P8 拆 shim。

### 头号风险（Full bus）
loopback 重放是个隐藏控制流环（重进本机 web 口 → host handler 白嫖两跳中继）。"都一根 bus 了，直接进程内 publish"的简化会**悄悄弄坏两跳**（跳过 auth / 机器解析 / 向下转发）。必须保留真 HTTP loopback executor + 冻结两跳不变量 INV-R3。

### 我的诚实判断
你表达过**两个诉求**，现在摊牌：
- 若"**代码要缩**"（你那句抱怨）→ **Subset 赢**：减更多、风险小、贴 CLAUDE.md（不加抽象层）。
- 若"**一根 content-agnostic bus**"（愿景）→ Full bus，但接受净 ~−100 生产行 + 测试基建 + dispatcher 合并的并发风险。

Subset 不给愿景；Full 给愿景但为它付风险和基建。**看完这段选：Subset / Full。**

---

## 0. 要解决什么（v1 · RPC 排除时写的背景，仍有效）

现在 BoxAgent 有**四块**消息投递实现，其实是个 **2×2 网格**（传输轴 × 内容轴）：

| | local | remote（跨机） |
|---|---|---|
| **事件** | `EventBus`（63行，`publish(level,category,msg,**meta)`）| `EventSyncer`（248行，全量复制+debounce+cursor resync+gossip）|
| **聊天** | `WebChannel._publish`（per-chat queue fan-out）| `ChatSyncer`（209行，订阅+refcount+两跳中继）|

痛点：两个跨机 syncer **互相抄骨架**（`decisions.md` 白纸黑字："ChatSyncer 抄了已在生产验证的 EventSyncer 骨架"），两个 wiring 模块**抢同三个 registry callback**、靠脆弱的 install-order 链（`chat_sync_wiring` 必须在 `sync_wiring` 之后装、fall-through）。

owner 的诉求（原话）："应该有一个 event bus 承载所有消息，不区分内容……本地协议也一样，只是链路不一样。"

---

## 1. 三方收敛的目标架构（这部分没争议）

一根 **content-agnostic 的 `MessageBus`**：
- **envelope 只有 `{topic, payload, ts}`**。`origin_seq`/`origin_machine`/`level`/`bot` 全在 `payload` 里，bus core 从不读。
- **`publish` 同步、保序** fan-out（本地 for 循环）；所有 async 只存在于 `RemoteSubscriber` 的**单个 pump task**里 —— 这一条同时守住坑#1（乱序）、`/events` 分页（fan-out 时要有 id）、跨机 dedup（要 origin_seq）。
- **持久化/广播是 subscriber 行为，不是 bus 特性**：
  - `EventStore` = 订阅 `events.*` 的 **privileged 同步 subscriber**（第一个跑、同步、mint id+origin_seq，enrich 后的 envelope 才给后面的 subscriber 看）。
  - "每台机器看到全部事件" = 每个节点订阅所有 peer 的 `events.*`。
  - 聊天 = 只有正在看的节点订阅 `chat.<machine>.<bot>.<chat>`。
- **durable = "该 topic 的订阅者列表里有没有 StoreSubscriber"这个事实**，不是 envelope 字段、不是 topic 名推断、不是运行时判断。`chat.*` 的列表里没有 StoreSubscriber → stream_delta **物理上到不了 SQLite**（构造即保证，不是运行时 check）。

**关键让步（architect 认输）**：**没有共享的复制算法**。`EventReplicator`（broadcast+cursor+gossip）和 `ChatReplicator`（demand+refcount+relay）共享**零**语句。所以它俩是**两个 sibling subscriber**（各自持有一个共享的 `PeerTransport`），不是一个 strategy 类、不是父子类。统一只发生在 **API / transport / local-link / wiring / wire-frame / dispatch** 这几层（都是单数），复制策略保留两个 coordinator。

### 具体接口
```python
# bus/message.py
@dataclass(frozen=True)
class Message:
    topic: str      # "events.<category>" | "chat.<machine>.<bot>.<chat>"
    payload: dict   # bus 从不读
    ts: float

# bus/core.py — MessageBus
def subscribe(topic_pattern, subscriber) -> Subscription   # 支持精确 / 前缀（events. / events.scheduler.）
def publish(topic, payload, ts=None) -> None               # 同步保序 fan-out，无 create_task-per-message
def attach_link(peer_key, transport) / detach_link(peer_key)

# cluster/peer_transport.py — PeerTransport（抽出的共享 link）
attach_peer / detach_peer / async send_to(peer, frame) / async handle_frame(peer, frame) -> bool  # 按 topic 路由

# bus/subscriber.py
class LocalSubscriber:   # 包 asyncio.Queue，put_nowait，满了丢
class RemoteSubscriber:  # 有界 queue + 一个 pump task → await transport.send_to；保序 + 每 peer 背压
```

### 模块布局（关键的依赖方向决策）
```
bus/                  新建 · 中立 leaf，不 import 任何项目内模块
  message.py / core.py / subscriber.py
events/               变成"bus 上的 durable-broadcast 策略"
  storage.py          EventStore 内部不变（唯一 SQLite writer）
  store_subscriber.py 新建（原 EventBus.publish 的写库 + insert_remote）
  telegram_notifier / web_stream / retention  都变成 subscriber，基本不动
cluster/
  peer_transport.py   新建（抽出的共享 link）
  event_replicator.py 原 EventSyncer（broadcast 策略）
  chat_replicator.py  原 ChatSyncer（demand 策略）
  bus_wiring.py       新建 · 一个 wiring 取代 sync_wiring + chat_sync_wiring 的链
log/                  facade 不变；log.bind 绑一个 10 行 LogToBusAdapter
```
`bus/` 是 leaf；`events/` 和 `cluster/` 都依赖 `bus/`，**彼此不依赖**（干净的 fan-in）。`boxagent.log` 签名字节级不变，业务代码零改动。

---

## 2. 9-phase 迁移计划（每步可回退、每步绿）

> 每个 phase 的 gate = 冻结不变量集全绿 + 该 phase 专属 no-go 绿 + `uv run pytest -x -q ≥ 886`。当前 bus 相关测试 126 passed，全量底线 886，只能涨。

| Phase | 改什么 | 可回退性 | 专属 no-go gate |
|---|---|---|---|
| **P0** | 建 2/3 节点 in-process harness `_bus_harness.py` + 冻结不变量 `test_message_bus_invariants.py`（append-only，只断言 store 行/queue 内容/抓到的帧）+ `docs/bus-migration-map.md`。**不动产品代码** | 删文件 | 不变量对**旧代码**全绿（证明描述的是真行为）；`reorder_tasks` 注入对故意写坏的 per-event-create_task stub **证明能变红**（没见过失败的守卫不算守卫） |
| **P1** | 抽 `PeerTransport`，两个 syncer delegate 过去。行为不变。**这一步落地最大的真去重（~40 逻辑 + ~120 wiring 行）** | inline 回去 | 跨机 event/chat 不变量不变绿；126 旧测试全绿 |
| **P2** | 落 `bus/` core（Message/MessageBus/Subscriber/Local/Remote），**不接线** | 删 bus/ 包 | bus core 单测绿（envelope roundtrip / 本地投递 / 前缀路由 / 满丢） |
| **P3** | `StoreSubscriber` 抽出，仍由 `EventBus` **同步、第一个**调。行输出字节级不变 | inline 回 EventBus.publish | **INV-A1：`publish` 返回前行已落库**（store 变异步就红）|
| **P4** ⚠️ | `EventBus.publish` 改成建 Message 调 `bus.publish`；store/notifier/stream/syncer 都改成订阅 bus。**但 `log.bind` 仍绑 EventBus（内部 delegate）**，裸 swap 推迟到 P8 | 回退一个 adapter 文件 | 同样 SQLite 行 + 同样 SSE + 同样 notifier 调用 + INV-E（保序）。**这是 load-bearing 内部相；pipeline 全程不红** |
| **P5** ⚠️ | `WebChannel._publish → bus.publish`；`ChatBus.subscribe → bus.subscribe`。demand/pump/两跳中继全保留（坑#1 在这） | 回退一个 channel 文件 | **INV-A2：200 条 stream_delta → CountingEventStore 插入增量 == 0**（聊天永不进库）+ INV-背压隔离 |
| **P6** | `EventSyncer→event_replicator.py`、`ChatSyncer→chat_replicator.py`，都组合 PeerTransport。删两个 wiring 的链，一个 `bus_wiring.py`，**install-order 约束消失** | 两个旧 wiring 还在 git，回退一个 commit | 两种 reconnect 都保留（event cursor-resync **且** chat re-subscribe，同一个测试） |
| **P7** ⚠️ | **统一线上帧** → `{v:2, topic, payload, ts}` + 少量 typed 控制帧，一个 topic 路由 dispatch。version-gated（`v:1` 收到不认的 `v:2` 优雅丢弃不崩） | version gate 就是缝，回退帧翻转，`v:1` 仍有效 | **INV-D1（一次 reconnect 同时验 event+chat）+ mixed-version 测试**（A 说 v2、B 说 v1，同一 link 不崩） |
| **P8** | 拆 `EventBus` shim；`log.bind(LogToBusAdapter)` 直绑。白盒测试改成黑盒，旧 syncer 测试**迁移不删**（按 migration-map） | 回退一行 log.bind | migration-map 每行都指向绿不变量；`pytest ≥ 886` 且净涨 |

⚠️ = 高风险相。

### 验收标准
1. **一根 bus，五个单数**：一个 publish/subscribe API、一个 PeerTransport、一个 bus_wiring、一个 topic 路由 dispatch、一个线上帧。grep 证明 `sync_wiring.py`/`chat_sync_wiring.py`/旧帧词汇/复制的 `_send_to` 全没了。
2. **stream_delta 永不进 SQLite**（INV-A2，local + 跨机）。
3. **事件 durability + 保序不变**（INV-A1 / B* / E*）。
4. **一次 reconnect 同时恢复两半**（INV-D1）。
5. **facade + 所有现有出口字节级不变**（`boxagent.log` 签名、`/api/events`、Telegram、retention、subscriber 异常隔离）。业务代码 import `boxagent.events` 的 diff == 0。
6. **依赖 fan-in 干净**：`bus/` 不 import 项目内；`events/`↔`cluster/` 互相 import == 0。
7. **测试底线只涨**（每个 phase 结束 ≥ 886，最终净涨）。

### 残留风险（即便分阶段）
- **R1 Phase 7 mixed-version 窗口** → `v` 版本字节 + 优雅丢弃 + 全节点一起重启（几分钟、自己可控）。
- **R2 live log.bind swap（P4+P8）** → 绝不裸 swap；P4 让 EventBus 保持绑定+内部 delegate，裸 swap 推迟 P8 且 gate 在字节级行相等。
- **R3 保序坑#1** → 铁律"本地 fan-out 永远同步 for 循环，async 只在 RemoteSubscriber 单 pump"；INV-E + P0 的 reorder 注入先证明能抓 bug。
- **R4 背压隔离** → 每订阅自己的有界 queue，绝不共享；durable 不丢、ephemeral 满丢。

---

## 3. 反方（Devil's Advocate）—— 而且站你这边

> 三方收敛太顺是"该挑战"的信号，不是"该往前"。以下数字都对着真代码核过。

### 一句话反对
**为了删掉 ~40 行真正共享的 transport + 把两种线上帧合成一种，方案要新建一整套永久机构**（`bus/` 包、Subscriber 协议、两个 RemoteSubscriber pump、PeerTransport、StoreSubscriber、冻结不变量层、2/3 节点 harness、migration-map、version-gated 帧翻转 + mixed-version 隐患）—— 在一个 2 个月大、单人、~17k 行的 hobby 系统上，而它自己的 CLAUDE.md 黑体字写着"不要重构没坏的 / 不要加抽象层 / 能 30 行别写 class"，代码上周刚 ship 且在正常用。**这正是你自己在本项目里已经点过一次名的"加的比删的多"陷阱**（`decisions.md:25` 记着：ChatSyncer 是**故意** copy-paste EventSyncer 去 de-risk 的 —— 那个重复是有意识、被批准的决定，不是要赎罪的错误）。

### 成本账（方案自己从不摆的账）
**真正删掉的**：`_send_to` 两份逐字相同（除 log 前缀）≈ **7 行**；`_peers` + attach/detach 骨架 ≈ 10 行（还不完全一样，chat 的 detach 是 async 带 refcount 清理）；handle_frame dispatch 骨架 ≈ 10 行（**函数体零共享**）；wiring 链 ≈ 15 行。**诚实合计 ~40 行**。另外 ~380 行 divergent，方案**逐字保留**（自己承认 "Verbatim EventSyncer/ChatSyncer policy"）。

**新增的**：一个新顶层包 + Subscriber/Local/Remote 协议 + PeerTransport + StoreSubscriber + LogToBusAdapter + bus_wiring + 改名的两个 replicator + 永久的 `_bus_harness.py` + ~30 条冻结不变量 + reorder 注入 + CountingEventStore + migration-map + **一个只为later删掉而写的 EventBus shim（P4–P8 活着）**。

**加的机构 5–10 倍于删的重复。** 方案自己在 §2.2 说破了：两个 replicator 是"trenchcoat 里的两个类"、零共享代码。

### Phase 7 收益最小、风险最大 —— 不该存在
停在 **Phase 6** 你**已经**拿到：一个 bus API、一个 PeerTransport、一个 wiring、一个本地 dispatch、复制的 `_send_to` 没了、脆弱的链没了 —— **全部正确性和去重的赢面都在这了**。install-order 约束在 P6 就化解了。

Phase 7 只多干一件事：把线上两种帧词汇变一种。代价是全文最吓人的一项（它自己的风险登记 R1）：**mixed-version cluster 窗口**。用一个"跨机 wire 契约隐患"（CLAUDE.md 踩坑清单里最密集的一类：坑#8 devtunnel region 漂移、坑#9 split-brain、坑#4 Codex session 跨重启）去换"线上只有一种帧"这个审美属性 —— 而你这网络**本来就是手动重启**，两种帧共存**运行上零成本**。`handle_frame` 返回 False fall-through 是 15 年的无聊安全 dispatch。两种不冲突的帧类型不是缺陷，就是"一条通道上有两种消息"，地球上每个协议都这样。

把"最险、最没用"的一步定成"NOT optional"，是这方案的**自伤核心**。

### 它甚至没兑现你的心智模型
你要的是"一根 bus 承载所有消息，不区分内容"。方案实际 ship 的是：`event_replicator.py` 和 `chat_replicator.py` 并排、各 ~180 行、除了 import 同一个 PeerTransport **什么都不共享**，外加一个打破统一 subscriber 模型的"privileged 同步 store 特例"。你打开 `cluster/` 看到两个明显不同的复制大脑，**大概率还是会说那句**："这还是两个 bus，戴了个共享 transport 的帽子。" 因为 —— 三方都同意 —— 那个"单一算法"在问题里**不存在**（事件要 backlog resync，聊天是 demand 驱动的 live，没有一个算法覆盖两者）。**方案花 9 个 phase 到达一个仍然明显违背你 why 的终态。**

### 便宜版（~1 天，2 个可回退 commit，捕获 80% 的"像一根 bus"）
1. **抽 `PeerTransport`**：把 ~40 行真共享的 transport 抽出，两个 syncer delegate。方案里全部真去重都在这。
2. **收掉 wiring 链**：一个按 frame type/topic 路由的 dispatch，**干掉 install-order 这个唯一真脆弱点**。
3. **（可选）改名** `EventSyncer→EventReplicator`、`ChatSyncer→ChatReplicator` + 一段文档："这是一个共享 PeerTransport 上的两个复制策略，故意没有共享算法。" **这就把你要的概念清晰给到了 —— 一个有名有据的'一个 transport、两个策略'模型 —— 不写一个新抽象。**

**不做**：新 `bus/` 包、Subscriber 协议拆分、StoreSubscriber 重构事件写路径（P3-4 那个"live log.bind 灾难且不可见"的中间相）、帧翻转（P7 mixed-version 隐患）、EventBus shim 写了又拆的 churn（P4→P8）、~30 条冻结不变量 harness。

**你相比完整版损失**：线上一种帧 vs 两种（运行零成本）；EventStore 形式上成为 subscriber（纯洁性，无行为变化）；`bus/` 作为假想第三种内容的家（YAGNI —— roadmap 上没有第三种，CLAUDE.md 明禁"以后可能用到"的抽象）。**你保留**：所有 load-bearing 行为字节级不变 + 90% 的"现在像一根 bus"的故事，1 天而不是 9 个 phase。

**顺带**：WebChannel / EventStreamSubscriber 现在**零单测** —— 这个特征化测试是 Phase 0 里真正的持久价值，**可以脱离重构单独收割**（值得做的是**测试**，不是重构）。

### 会在哪半途而废（regret 风险）
方案致命结构性：**明令禁止停**（"all phases land / Phase 7 NOT optional / no pause"）。单人 hobby 项目一定会因生活中断，而方案把暂停点设计成最危险的。**不归点是 Phase 4**（EventBus 变 shim 包 MessageBus）：停在这 → 事件写路径经 shim 走新 bus、聊天还没迁、两种帧还在、半成品 `bus/` —— **比任一端点都更难理解的 hybrid**。**Phase 5 是第二颗雷**（坑#1 在这；若 P4 ship 而 P5 卡住，事件和聊天在**不同的 bus 上** —— 目标的反面，还多套机构）。便宜版没这个陷阱：每个 commit 都是完整、可 ship、可回退的改进。

### 反方结论
**成本收益：按现在的 scope 净负。** 推荐：**做便宜版**（~1 天）+ 单独补 WebChannel/EventStreamSubscriber 特征化测试。若要更多：**做但硬砍 Phase 7、去掉"不许停"的强制**（停在 Phase 6 保留全部正确性赢面、零 wire 风险、可在任一绿相自由停）。**不要按原样推进** —— "9 phase 必须全落、P7 不可选、不许暂停"是决策文档里最危险的一句，且危险是结构性的。

---

## 4. 三条路对比

| | **A · 便宜版** | **B · 统一 API，停在 wire-frame 前** | **C · 完整 9-phase** |
|---|---|---|---|
| 范围 | 抽 PeerTransport + 收 wiring 链 + rename + doc；顺带补缺失测试 | A + 建 content-agnostic MessageBus + envelope + Local/RemoteSubscriber，events+chat 都走它，store 同步 subscriber。**停在 P6，不做 P7 帧翻转** | 全部含 P7 线上帧统一 + P8 拆 shim |
| 交付"一根 bus"心智 | 弱（"一个 transport 两个策略"，可能还像两个 bus） | **强**（一个 publish/subscribe API + envelope，local=queue/remote=WS 同协议只 link 不同） | 最强（连线上帧也一种） |
| 工作量 | ~1 天，2 commit | 中（~P0–P6，去掉最险的 P7/P8 churn） | 大（9 phase + 新包 + shim churn） |
| 风险 | 近零 | 低（无 mixed-version、无裸 log.bind swap 到 P8） | 高（P7 mixed-version + P4/P5 半成品 hybrid + 不许停） |
| 净代码 | 略减 | 增（新 bus 层，但换来统一 API 心智） | 增最多（devil's advocate：加的比删的多 5–10×）|
| 半途风险 | 无（每 commit 完整） | 低（每 phase 可停在绿） | 高（P4/P5 是雷） |

---

## 5. 我的推荐

**B**。理由：A 太少 —— 它去重去脆弱，但**没建你真正要的那个统一 bus API/envelope**，你打开还是两个 syncer，"像一根 bus"这个 owner 诉求没兑现。C 的最后一步（P7 帧翻转）是**用真实的跨机 wire 契约风险换一个审美属性**，在你手动重启的网络上性价比最差，且 devil's advocate 关于"加的比删的多、半途 hybrid 风险、不许停"的批评全部成立。

**B = 建统一 MessageBus + envelope + Local/RemoteSubscriber，events 和 chat 都 publish/subscribe 走它（这就兑现"同协议不同链路"），两个 replicator 作为 subscriber，store 作为同步 subscriber —— 但保留两种线上帧、不做 mixed-version 的 P7。** 每个 phase 可停在绿，去掉"不许停"的强制。

如果连 B 都嫌重，就 A + 补测试，完全正当。

你定 A / B / C。
