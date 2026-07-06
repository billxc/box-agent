# Round 1 — TESTER 独立分析：四合一 MessageBus 统一的回归网 + 不变式 + 每阶段 gate

作者视角：QA / 回归风险。全部断言只看外部边界（store rows / queue contents / 发给 fake peer 的 frames），从不 peek 私有状态。

---

## 1. Key observations（现状盘点：今天到底测了什么）

先把四套实现的**现有测试覆盖**摊开，这决定了「哪些是已有的回归网、哪些是空白」。我逐文件读过，下面是精确账目。

### 1.1 四套实现 + 各自的 subscriber 生态

| 实现 | 文件 | 语义 | 现有测试 | 测试数 |
|------|------|------|----------|--------|
| EventBus | `events/bus.py` | LOCAL pub/sub，**同步** fan-out：`publish()` 先 `store.insert_local()`（拿到带 id 的 Event）→ 同步 for-loop 调 subscriber | `test_event_bus.py` | 11 |
| EventStore | `events/storage.py` | SQLite：`(origin_machine, origin_seq)` 自然键 + `INSERT OR IGNORE` dedup、cursor、`events_after_seq`、retention | `test_event_storage.py` | 40 |
| EventSyncer | `events/sync.py` | CROSS-MACHINE 全量复制：debounce 200ms flush、resync via cursors、gossip（host 转其他 guest）、3 天窗口 | `test_event_syncer.py` | 12 |
| sync_wiring | `events/sync_wiring.py` | registry/guest_client → EventSyncer 桥（**直接赋值** hook，非链式） | `test_event_sync_wiring.py` | 7 |
| WebChannel | `transports/web/channel.py` | LOCAL chat：per-`chat_id` `asyncio.Queue(maxsize=1024)` fan-out，事件 dict（message/stream_start/stream_delta/stream_end/tool_call/tool_result/typing） | 无直接单测（仅间接经 chat_bus） | 0 |
| ChatSyncer | `cluster/chat_sync.py` | CROSS-MACHINE chat：订阅式，key=`(owner_machine, bot, chat_id)`，两跳 host relay，refcount，`QUEUE_MAXSIZE=1024` drop | `test_chat_sync.py` | 16 |
| ChatBus | `cluster/chat_bus.py` | location-transparent 门面 + owner-side pump（`asyncio.create_task` per-(bot,chat_id)，**不是** per-event） | `test_chat_bus.py` | 6 |
| chat_sync_wiring | `cluster/chat_sync_wiring.py` | registry/guest_client → ChatSyncer 桥（**链式** hook，必须在 event hook 之后装） | `test_chat_sync_wiring.py` | 6 |
| TelegramNotifier | `events/telegram_notifier.py` | EventBus subscriber，级别/前缀过滤，`create_task(deliver)` | `test_telegram_notifier.py` | 17 |
| RetentionSweeper | `events/retention.py` | 周期删旧 | `test_event_retention.py` | 5 |
| EventStreamSubscriber | `events/web_stream.py` | /events 页 SSE，EventBus subscriber + `call_soon_threadsafe` 入队 | **无直接单测** | 0 |

合计与 bus 相关的现存测试：**126 passed**（我实测跑过，2.14s）。全套基线 886。

### 1.2 关键差异——统一后必须被同一套语义覆盖的四个「轴」

统一模型说「LOCAL subscriber → in-process queue；REMOTE → cluster WS；persistence + broadcast 是 subscriber 行为」。但四套今天在**四个正交维度**上语义不同，这正是回归风险的来源：

1. **交付方式**：EventBus 是**同步回调**（`callback(event)` 直接调用）；WebChannel/ChatSyncer 是**入队**（`queue.put_nowait`）。同步回调保证 subscriber 之间的相对顺序；入队则把顺序责任推给「谁来 drain queue」。
2. **持久化 vs 短暂**：event 全部落 SQLite；chat **绝不能**落 SQLite（HARD CONSTRAINT #1，尤其 `stream_delta` 高频）。
3. **全量 vs 订阅**：event 是全量复制（每个节点最终有全部 event）；chat 是需求驱动（只有正在看的节点订阅）。
4. **跨机去重/resync**：event 靠 `(origin_machine, origin_seq)` + cursor；chat 无 seq、无 resync backlog（重连只重发 `chat_subscribe`，丢失窗口内的 delta 是可接受的——chat 是 live 流）。

**统一的核心风险**：把 chat 和 event 塞进「同一个 publish/subscribe API」时，很容易让 per-topic 的「durable vs ephemeral」策略泄漏——要么 chat delta 意外落库（违反 #1），要么 event 丢了 store 这个 subscriber 的同步落库保证（违反 #2）。**回归网的第一职责就是把这两个策略钉死成黑盒不变式。**

### 1.3 已知 footgun（必须有专门测试守住）

- **create_task-per-event → 乱序**（CLAUDE.md 踩坑 #1 + 本任务 HARD CONSTRAINT #5）。今天 EventSyncer 用**单 flush task + buffer list**（`sync.py:187-223`，`_buffer.append` 按调用顺序，flush 时 `_buffer[:MAX_BATCH]` 切片保序）；ChatBus pump 用**单 per-chat task 顺序 `await queue.get()`**（`chat_bus.py:73-76`）。两处都是「单 task 顺序消费」而非「每事件一 task」。**统一实现如果为了『解耦』给每个 event 起一个 `create_task`，顺序保证就没了。** 这必须有专门的乱序回归测试，且要能在被引入 bug 时变红。
- **hook 链式 vs 直接赋值顺序**：`sync_wiring` 直接赋值，`chat_sync_wiring` 链式并 fall-through。gateway 保证「chat hook 在 event hook 之后装」（`chat_sync_wiring.py` docstring 明写）。统一后如果两套 hook 合并，装配顺序 bug 会让 `on_unknown_frame` 只有一半 frame 类型被消费。
- **debounce 窗口内 store 已写、peer 未收**：event 的 store 写是同步的（publish 时立即落库），但跨机交付要等 debounce。任何「published on A → 立刻 query B.store」的测试若不 sleep 过 debounce 会 flaky。
- **3 天窗口双向过滤**：既在 emit 侧（`_on_local_event` ts 判断，`sync.py:190`）又在 resync 侧（`since_ts`，`sync.py:167,176`）过滤。统一后若只保留一侧，旧事件会漏/多同步。

---

## 2. Proposed approach —— 测试策略 / 回归网 / 每阶段 go-no-go gate

### 2.0 核心原则：**Characterization test set 先冻结，再动实现**

统一是重构。重构的黄金法则：**先有一层与实现无关、只描述外部可观测行为的 characterization 测试，全绿；然后每个迁移阶段结束都必须让这层全绿。** 今天的 126 个测试大部分是**实现耦合**的（断言 `sync_a.attach_peer` / `sync_a._on_local_event` / `syncer.on_local_publish` 这些**具体类的具体方法**）——它们在统一后**几乎一定要改签名**（见 §2.4）。所以我提议**新增一层 `test_message_bus_invariants.py`**，用**只依赖『发布点』和『观测点』的语言**表达不变式，这层在整个迁移中**一行都不改**。这是真正的回归网。

### 2.1 测试 HARNESS（先建，否则不变式没法黑盒表达）

现状：**没有**一个统一的 2-node in-process loopback harness。今天每个测试文件各自手搓 wiring：
- `test_event_syncer.py::_wire_pair` —— 双向 `handle_frame` 直连，只连 EventSyncer。
- `test_chat_sync.py::_make` —— fake peer 录 frame，只连 ChatSyncer。
- `test_chat_bus.py::FakeChannel` —— 手搓 WebChannel 替身。

**提议新建 `tests/unit/_bus_harness.py`（测试辅助，非产品代码）**：一个 `TwoNodeCluster`，内部起两个真 node（各自真 store + 真 bus + 真 syncer/channel），用一对 in-memory async 管道把它们的 WS 帧对接（`node_a.send(frame)` → `await node_b.receive(frame)`，反之亦然），并暴露：
- `node.publish_event(level, category, message, **meta)` —— 走真正的 `boxagent.log` facade（保证 #3 facade 不变）。
- `node.publish_chat(bot, chat_id, event)` —— 走真正的 WebChannel `_publish`。
- `node.subscribe_chat(owner_machine, bot, chat_id) -> Queue` —— 走 ChatBus.subscribe。
- `node.store_rows(**filter)` —— 直接读 EventStore.query（观测点）。
- `cluster.link()` / `cluster.drop_link()` / `cluster.relink()` —— 模拟 WS 建立/断开/重连。
- `cluster.settle()` —— `await` 到 debounce flush + 所有 pending task 跑完（内部 `asyncio.sleep(debounce*1.5)` 或轮询 buffer 空 + task idle，**不要裸 `sleep(0.05)`**，那是 flaky 之源）。

这个 harness 是**三跳拓扑**可配的：`TwoNodeCluster`（owner + subscriber）和 `ThreeNodeCluster`（gA — host — gB，host relay），因为 chat 的两跳 relay 和 event 的 gossip 都需要中间节点。

harness 的存在本身要**先有自测**：`test_bus_harness.py::test_link_delivers_frame_both_directions`、`test_settle_waits_for_debounce`——否则 harness 有 bug 会让所有依赖它的不变式假绿。

### 2.2 THE INVARIANTS —— 黑盒回归网（每条 = 一个测试，整个迁移不改）

下面每条我给 **input / expected / why it matters**。这些是 go/no-go 的硬指标。

#### A. 持久化边界（HARD CONSTRAINT #1 + #2）—— 最重要，因为是**负向断言**

- **INV-A1「event 落库」**
  input：node A `publish_event("info","scheduler.run","fired", bot="b1", task_id="t1")`。
  expected：`node_a.store_rows()` 恰好 1 行，`level=info / category=scheduler.run / message=fired / bot=b1 / meta={"task_id":"t1"} / origin_machine=A / origin_seq=1`。
  why：event 的同步落库是 store-subscriber 的核心契约，统一后不能因为「store 变成一个普通 subscriber」而变异步导致 publish 返回时行还没写。

- **INV-A2「chat stream_delta 绝不落库」（负向 / 反事件断言）** ★ 最关键
  input：node M 上 `publish_chat("b","c",{"type":"stream_delta","delta":"x","message_id":"m1"})` 重复 200 次；同时有一个 browser 订阅 `(M,"b","c")`。
  expected：`node_m.store_rows()` == `[]`（**0 行**，`store_rows(category_prefix="chat")` 也 0，任何 category 都 0）；且订阅 queue 收到全部 200 个 delta。
  **如何断言一个「非事件」**：不是「grep 没有」——而是**先记录 publish_chat 之前的 `store_rows()` 全量快照（应为空或已知集合），publish 之后再取快照，断言 `after == before`（集合完全相等，count 相等）**。再加一条：把 EventStore 换成一个 `CountingEventStore` spy（子类，`insert_local`/`insert_remote` 计数），断言 chat publish 期间 **insert 计数增量为 0**。两条一起——一条防「写了别的 category」，一条防「写了但 query filter 没查到」。
  why：这是整个统一最容易破的约束。高频 delta 一旦泄漏进 SQLite，磁盘和写锁瞬间爆炸。**必须有一个会因为『chat 意外落库』而变红的测试**，且断言要强到 spy 层。

- **INV-A3「per-topic 策略是声明式的、可枚举」**
  input：对每个已知 topic 类别（event 类 vs chat 类）跑一张参数化表：`[("event/scheduler.run", durable=True), ("chat/stream_delta", durable=False), ("chat/message", durable=False), ("chat/tool_call", durable=False), ...]`。
  expected：`durable=True` 的 publish 后 store 增 1 行；`durable=False` 的 publish 后 store 增 0 行。
  why：统一后「durable vs ephemeral」应是**一处声明**（per-topic policy）。参数化表把「新加一个 topic 忘了标 ephemeral」这类未来 bug 挡在门口。这条也直接回应任务里「per-topic ephemeral-vs-durable policy」的诉求。

#### B. 跨机 event 复制（回归 EventSyncer 全部语义）

- **INV-B1「A 发 → 窗口内到 B.store」**
  input：`cluster.link()`；node A `publish_event("info","c","task fired")`；`cluster.settle()`。
  expected：`node_b.store_rows()` 1 行，`origin_machine=A / message="task fired"`。
  （= 现 `test_local_publish_propagates_to_peer`，但走 harness + facade。）

- **INV-B2「双向」**：A 发一条、B 发一条、settle → 两边 store 都恰好含两条，各自 origin_machine 正确。（= `test_bidirectional_sync`。）

- **INV-B3「dedup」**：同一 batch 重发两次 → B.store 仍 1 行。（= `test_duplicate_batch_is_ignored`；断言点必须是 `store_rows()` count，不是内部 seen-set。）

- **INV-B4「重连 resync via cursor」**（reconnect + 顺序两件事）
  input：`link()`；A 发 old1、old2、settle（B 收到）；`drop_link()`；A 发 mid1、mid2（B 断连收不到，但 A.store 有）；`relink()`；`settle()`。
  expected：B.store 最终 = {old1,old2,mid1,mid2}，且 **origin_seq 连续无洞**（1,2,3,4 for machine A）。
  why：resync 是 event 的可靠性核心。重连必须 backfill 断连期间的漏发，且靠 cursor（不重发已有的 old1/old2——用 spy 断言 relink 后 B 只 insert 了 mid1/mid2 两条，old 的没重插）。

- **INV-B5「gossip：g1 → host → g2」**：三节点，g1 发，g2.store 收到，且不回环到 g1。（= `test_host_gossips_...`；用 ThreeNodeCluster。）

- **INV-B6「3 天窗口」**：注入一条 fresh + 一条 ancient（ts < now - 3d），link+settle → B 只收 fresh。（= `test_old_events_excluded_from_resync` + `test_old_event_not_pushed_on_publish` 两条都要保留：一条测 resync 侧过滤，一条测 emit 侧过滤。）

- **INV-B7「detach 停投递」**：drop_link 后 A 发 → B 收不到。（= `test_detach_peer_stops_sync`。）

- **INV-B8「send 失败不崩、本地仍落库」**：peer send 抛异常 → A.store 仍有该 event。（= `test_send_failure_is_swallowed`。）

#### C. 跨机 chat 订阅（回归 ChatSyncer 全部语义）

- **INV-C1「订阅者只收自己订的 chat」**（正 + 负一对）
  input：subscriber 订阅 `(M,"b","c")`；owner M 上 `publish_chat("b","c",{...})` 和 `publish_chat("b","OTHER",{...})`。
  expected：subscriber queue 只收到 c 的事件，**收不到 OTHER**。
  why：chat 是订阅式（区别于 event 全量）。统一后如果 chat 误用了 event 的「广播给所有 peer」，隐私和带宽都崩。（= `test_owner_forwards...` + `test_owner_publish_to_unwatched...`。）

- **INV-C2「refcount：两个本地 watcher 只产生一次 upstream subscribe / 最后一个走才 unsubscribe」**（= `test_subscriber_refcount_single_upstream_sub`）。观测点：发给 host peer 的 `chat_subscribe`/`chat_unsubscribe` frame 计数。

- **INV-C3「两跳 host relay」**：gA 订阅 gB 的 bot，host 转发 subscribe 给 gB，gB 发布，host 中继 chat_event 回 gA。（= `test_host_relays_subscribe_and_events`；ThreeNodeCluster。）

- **INV-C4「host relay refcount 跨两个下游 guest」**（= `test_host_relay_refcount_across_two_downstream_guests`）。

- **INV-C5「host 检测到 gA 的 WS 整体断 → 释放对 gB 的 upstream sub」**（= `test_host_relay_detach_releases_upstream`）。这是 reconnect 的另一半。

- **INV-C6「subscriber 重连重发 subscribe」**（= `test_subscriber_reconnect_resends_subscribe`）。

- **INV-C7「owner-side demand 边沿」**：第一个远端订阅 → demand active；最后一个走 → inactive；WS 断 → inactive。（= `test_local_demand_fires_on_first_and_last`、`test_local_demand_deactivates_on_peer_detach`。）demand 驱动 pump，是 chat「订阅式」的引擎，绝不能丢。

#### D. 交叉不变式（这是**统一才会出现的新风险**，今天没有测试）★ 空白区

- **INV-D1「重连时 event resync AND chat re-subscribe——两者都要，不是只有一个」** ★
  input：ThreeNodeCluster，node B 既订阅了 remote chat `(A,"b","c")`，又在做 event 全量复制；`drop_link(A,B)`；断连期间 A 发 2 个 event + 3 个 chat delta；`relink(A,B)`；`settle()`。
  expected：**(a)** B.store 补齐那 2 个 event（cursor resync）；**(b)** B 的 chat queue 重新开始收到 A 的**新** chat delta（re-subscribe 生效）。断连期间的 3 个 delta **可以**丢（chat 是 live，无 backlog），但**重连后**的新 delta 必须到。
  why：这是任务点名的不变式——「on WS reconnect, events resync via cursor AND chat re-subscribes — BOTH, not one」。统一成一根 bus 后，重连逻辑合并，极容易只恢复一半（event 复用了 resync 但 chat 订阅没重发，或反之）。今天 event 和 chat 的重连是**两套独立代码**各自测的，**没有一个测试同时验证两者在同一次重连里都恢复**。这是最重要的新增测试。

- **INV-D2「同一根 WS 上 event_batch 和 chat_event 帧不互相吞」**
  input：一条 WS 同时承载 event 复制和 chat 中继；交替发 event_batch 和 chat_event；
  expected：event 进 store，chat 进对应 queue，互不串台、互不吞（`handle_frame` 对未知类型返回 False 让下一个 handler 接手——这正是 chat_sync_wiring 链式的意义）。
  why：统一后如果用**单一 dispatch**，帧类型路由错误会让一类消息静默消失。今天靠 `test_registry_unknown_frame_chains_event_and_chat` 覆盖了「链式」，但统一后 dispatch 换了实现，这条不变式要以「行为」而非「链式实现」重写。

- **INV-D3「chat 走跨机时也绝不落 event store」**：INV-A2 的跨机版——subscriber 在 node B 看 node A 的 chat，A 上 publish_chat delta，**A 和 B 两边 store 都 0 行增量**。why：跨机路径是另一条可能泄漏落库的通道。

#### E. 顺序不变式（HARD CONSTRAINT #5，create_task footgun）★

- **INV-E1「N 条 event 按序发 → 远端 subscriber 按序收」**
  input：node A 连发 100 条 `publish_event("info","c",f"n{i}")`（i=0..99，同一个 debounce 窗口内）；`settle()`。
  expected：`node_b.store_rows(machines=["A"])` 按 origin_seq 排序后 message = `["n0","n1",...,"n99"]`，**严格递增无乱序无重复无洞**。
  why：直接守 create_task-per-event footgun。今天 `test_owner_pump_forwards_local_events_in_order` 只测了 2 条 chat，**event 侧完全没有顺序测试**，chat 侧只有 2 条（不够触发竞态）。100 条 + 跨 debounce 边界才够。

- **INV-E2「chat delta 按序到达远端 subscriber」**
  input：owner 连发 100 个 stream_delta（delta="0".."99"）；远端 subscriber drain queue。
  expected：queue 里 delta 顺序 = `["0","1",...,"99"]`。
  why：stream_delta 乱序会让 UI 拼出的文本乱码。今天只测 2 条。

- **INV-E3「跨 flush 边界仍保序」**
  input：发 `MAX_BATCH + 50`（=550）条 event，跨两个 flush batch；settle。
  expected：B.store 收全 550 条，origin_seq 连续 1..550 无洞、无重排。
  why：`sync.py:214-223` 的 `_buffer[:MAX_BATCH]` 切片 + 递归 `_schedule_flush` 是保序关键路径，统一实现若改成并发 flush 会破。

#### F. 背压 / 慢订阅者（bounded queue，隔离性）

- **INV-F1「慢订阅者 queue 满 → 丢自己的，不阻塞别人」**
  input：两个 subscriber 订同一 chat，A 快速 drain、B 从不 drain；owner 发 `QUEUE_MAXSIZE + 100` 条。
  expected：B 的 queue 停在 `QUEUE_MAXSIZE`（1024）不再涨、**不抛异常**；A 收到全部（A 不受 B 拖累）。
  why：`chat_sync.py:131-135` 和 `channel.py:64-70` 都是 `put_nowait` + `QueueFull` 吞掉。统一后 fan-out 若改成 `await queue.put()`（阻塞版）会让一个卡死的 browser 拖垮整条 bus。今天 `test_subscriber_queue_full_drops_without_crashing` 只测了单订阅者满，**没测「满的那个不拖累不满的那个」的隔离性**——这是关键补充。

- **INV-F2「event SSE subscriber（/events 页）queue 满也 drop 不崩」**：`web_stream.py` 的 `EventStreamSubscriber` 今天**零单测**。补：queue 满 → `logger.warning` + drop，其他 subscriber 和 store 不受影响。why：/events 页是 #3 明列的「必须继续工作」项。

#### G. facade + 现有出口不回归（HARD CONSTRAINT #3）

- **INV-G1「`boxagent.log` facade 签名/行为不变」**：`log.info("scheduler.run","x",bot="b",task_id="t")` → store 1 行且字段正确。（= `test_bus_can_be_bound_to_log_facade` + `test_publish_implements_logsink_protocol`，两条都保留，后者静态查 `publish(self,level,category,message,**meta)` 签名。）
- **INV-G2「/api/events 查询/分页/过滤/mark_read 全绿」**：整个 `test_event_web_api.py`（12 条）作为「/events 页 API 契约」冻结不动。
- **INV-G3「TelegramNotifier 仍收到匹配 event 并 POST」**：`test_telegram_notifier.py` 全套冻结——尤其 `test_no_rate_limit_every_event_delivers`（每条都发，无节流是显式设计）。
- **INV-G4「retention sweeper 仍删旧」**：`test_event_retention.py` 冻结。
- **INV-G5「subscriber 异常不影响 store 写和其他 subscriber」**（= `test_subscriber_exception_does_not_break_store_write` + `test_subscriber_exception_does_not_block_others`）。统一后 subscriber 变多（store 也成了 subscriber！），这条要升级为：**任一 subscriber 抛异常，store-subscriber 的写和其他 subscriber 的投递都不受影响**，且顺序不变。

### 2.3 每迁移阶段的 go/no-go gate

统一是大功能，必然分阶段。我不知道 architect/engineer 会怎么切，但从测试角度，**每个阶段的 gate 都是「§2.2 全部不变式 + `uv run pytest -x -q` ≥ 886」**。下面按我推测的阶段给**增量 gate**：

| 阶段（推测） | 该阶段特有的 go/no-go 测试门 |
|------|------|
| **P0 建 characterization 层** | §2.1 harness 自测绿 + §2.2 全部不变式**基于旧实现**先跑绿（证明不变式表达的是真实现有行为，不是我脑补）。**这一步不改任何产品代码。** 若某条不变式在旧实现下红，说明我理解错了行为，回去改测试不改代码。Gate：新增不变式全绿 + 886 不降。 |
| **P1 引入统一 envelope + MessageBus 骨架（旧四套仍在，新 bus 空跑）** | 新 bus 的 publish/subscribe 单测（envelope roundtrip、local queue、topic 路由）绿；§2.2 全绿（旧路径没动）；886 只增不减。 |
| **P2 store 变成 subscriber（event 落库路径切到新 bus）** | INV-A1/A3、INV-B*（跨机 event）、INV-E1/E3（顺序）、INV-G1/G2/G3/G4/G5 全绿。**特别盯 INV-A1：store 写必须仍是「publish 返回前完成」**——若新 bus 把 store 也异步化，这条会红，是 no-go。 |
| **P3 broadcast 变成 subscriber（跨机 event sync 切新 bus）** | INV-B4（cursor resync）、INV-B6（3 天窗口）、INV-D2、INV-E3 全绿。dedup（INV-B3）盯死。 |
| **P4 chat 切到新 bus（ephemeral topic）** | INV-A2/D3（chat 不落库，spy 计数 0）、INV-C*（订阅/relay/refcount）、INV-E2（chat 顺序）、INV-F1（背压隔离）全绿。**INV-A2 是这阶段的头号 no-go**。 |
| **P5 拆除旧四套 + 收敛 wiring** | INV-D1（重连 event+chat 双恢复）、INV-D2、全部不变式 + 全套 886+ 一次过。删旧实现导致的 import 断裂靠全量测试兜。**这阶段最容易掉测试数**——删 `chat_sync.py` 时它的 16 个测试若被顺手删掉，就违反「测试只增不减」。正确做法：把那 16 条的**行为**迁进 `test_message_bus_invariants.py`（换成 harness 表达），而不是删掉。 |

**贯穿所有阶段的红线**：任一阶段结束 `uv run pytest -x -q` 的 passed 数 **< 886 即 no-go**（HARD CONSTRAINT #4）。

### 2.4 哪些现有测试**会**改——是红旗还是预期？

我逐条判断（这很重要，避免「改测试掩盖回归」）：

**预期要改（实现耦合，不是红旗）——但改法有严格约束**：
- `test_event_syncer.py`（12 条）：断言 `sync_a.attach_peer(...)` / `sync_a._on_local_event(...)` / `_wire_pair` 这些 EventSyncer 具体 API。统一后 EventSyncer 大概率被吸收进 MessageBus，这些 API 消失。**改法**：不是删，是把每条的**外部行为**（A 发 → B.store 有）迁进 harness 表达（已在 INV-B* 覆盖）。迁移必须**一对一保留每条的语义**，我会做一张「旧测试 → 新不变式」映射表 code review，确认没有一条行为悄悄消失。
- `test_chat_sync.py`（16 条）、`test_chat_bus.py`（6 条）：同理，断言 `syncer.remote_subscribe` / `bus._pumps` 等。行为迁进 INV-C*/INV-E2/INV-F1。**`test_aclose_cancels_pumps` 断言了 `not bus._pumps`（peek 私有）**——这条本就违反黑盒风格，迁移时改成「aclose 后再 publish，subscriber 收不到」的行为断言，是改进。
- `test_event_sync_wiring.py` / `test_chat_sync_wiring.py`：wiring 合并后 hook 装配方式变。**但 `test_real_registry_wiring_uses_session_ws` / `test_real_guest_client_wiring_uses_underscore_ws` 这两条钉的是真实 attribute path（`session.ws.send_json` / `client._ws.send_json`）——统一后如果还走这些 path，这两条应尽量保留；它们是防「重命名 attribute 静默断链」的最后一道，很值钱。**

**不该改（改了就是红旗）**：
- `test_event_bus.py` 里 `test_publish_implements_logsink_protocol` / `test_bus_can_be_bound_to_log_facade`：facade 契约（#3），改它 = facade 破了。
- `test_event_storage.py`（40 条）：store 是保留组件（statement:「EventStore = a subscriber that writes SQLite」），schema/dedup/cursor/resync 语义**不该动**。若这层要改，是 store 语义被破坏的强信号，必须停下来问 architect。
- `test_event_web_api.py`（12 条）、`test_telegram_notifier.py`、`test_event_retention.py`：都是 #3 明列的「必须继续工作」出口。改这些 = 出口回归。

**红旗判据**：任何一条「原本断言外部行为（store 行/queue 内容/发出的 frame）」的测试，如果迁移后**期望值变了**（不只是调用方式变），停下来——那是行为回归，不是测试重构。

---

## 3. Risks & concerns

1. **INV-A2 的「反事件」断言强度**是整个回归网成败的关键。仅靠 `store.query()==[]` 不够——如果实现写进了一个我没 query 到的角落（比如新表、或 category 前缀我没覆盖），会假绿。**必须上 `CountingEventStore` spy 断言 insert 调用次数增量为 0**。请 engineer 确认统一后 store 只有一个写入点（`insert_local`/`insert_remote`），否则 spy 挡不全。

2. **顺序测试的规模阈值**：现有 chat 顺序测试只有 2 条事件，**根本触发不了 create_task 竞态**（2 条即使乱序也 50% 概率碰巧对）。必须 100+ 条且跨 debounce/flush 边界。但纯 in-process 单线程 asyncio 下，乱序 bug 也可能**不稳定复现**（取决于 task 调度）。缓解：在 harness 里提供一个「故意打乱 create_task 完成顺序」的 hook（注入一个把 pending task 随机 reorder 的 loop policy），让 INV-E1 在**有 bug 的实现下必然红**——否则这个守 footgun 的测试可能自己就是假绿的。这点要和 engineer 对齐：**测试能不能真的抓到乱序 bug，得先用一个故意乱序的 mock 实现验证测试会变红。**

3. **`settle()` 的确定性**：现有测试全靠 `asyncio.sleep(0.05)` 等 debounce。这在 CI 慢机器上 flaky。harness 的 `settle()` 应基于**可观测条件**（buffer 空 + 无 pending flush task + 目标 queue 到达预期 count）轮询，而非固定 sleep。否则回归网自己会 flaky，被当成「偶发」忽略，失去守门作用。

4. **测试数「只增不减」与「删旧实现」的张力**：P5 删 `chat_sync.py`/`sync.py` 时，它们的 28 条专属测试若跟文件一起删，`886` 基线会掉。CLAUDE.md 有先例（workgroup 删除时基线从 1039 降到 886，是「模块整体删除、专属测试随之删」的**已批准**特例）。但这次不同：**chat/event 的行为不是被删除，是被迁移**——所以对应测试也必须**迁移而非删除**。我担心迁移中「懒省事直接删」。缓解：P0 就把不变式建全，删旧测试前先确认每条旧行为在新不变式里有对应，做映射表 review。

5. **WebChannel 和 EventStreamSubscriber 今天零单测**（channel.py 的 stream_delta 累积逻辑、web_stream.py 的过滤+call_soon_threadsafe）。它们是统一的两端 subscriber，却是**黑盒盲区**。统一前应先给它们补 characterization 测试（stream_delta 累积成 full text、web_stream 过滤 by level/machine/bot/category_prefix），否则改动时无网。这是**先决债务**。

6. **`call_soon_threadsafe`（web_stream.py:53）暗示 EventBus 可能被非 event-loop 线程调用**（EventStore 有 `threading.Lock`，storage.py:52）。统一后如果 chat 和 event 共用一个 bus，而 event 可能来自其他线程、chat 来自 event loop——**线程安全边界**会变复杂。需要一条并发测试：多线程并发 publish_event + event-loop 内 publish_chat，断言 store 无损坏、无 seq 冲突、无死锁。这是今天完全没有的维度。

7. **两跳 relay + gossip 在同一根 bus 上的回环**：event 是 gossip（host 转所有其他 guest），chat 是定向 relay（host 只转给订阅方）。统一到「每节点订阅 peer 的 events topic」后，要防**广播风暴/回环**——A→host→B→host→A。event 靠 `(origin_machine,origin_seq)` dedup 断环，chat 靠订阅定向天然不环。统一后若 chat 误进了 gossip 广播路径，会既泄漏又可能环。需要一条「三节点全连通，发一条 chat delta，只有订阅者收到一次、其他节点 store+queue 都不动」的测试。

---

## 4. Questions for architect and engineer

**给 architect：**

1. **统一 envelope 里有没有一个显式的 `durable: bool`（或 `topic policy`）字段**，还是 durable-vs-ephemeral 靠 topic 名字前缀推断？这决定 INV-A3 参数化表怎么写，也决定「新加 topic 忘标 ephemeral」这类 bug 能不能被静态挡住。我强烈倾向显式声明。

2. **store 仍是「publish 返回前同步写完」，还是变成异步 subscriber？** 现在 EventBus 是同步落库（INV-A1 依赖）。如果统一后 store 成了「一个普通 subscriber」并异步化，那 `publish_event` 返回后 `store_rows()` 可能还没这行——大量现有测试（和 mark_read/查询链路）会翻车。这是 P2 的头号 no-go 判据，需要你先定死语义。

3. **重连恢复：event resync（cursor）和 chat re-subscribe 是共用一个「reconnect topic 恢复」机制，还是各自的？** INV-D1 要验证「两者都恢复」。如果是同一机制，我怎么在测试里区分「event backlog 补齐（有 backlog）」vs「chat 只恢复订阅无 backlog」这两种本质不同的语义？

4. **背压策略统一成什么？** 现在全是 `put_nowait` + 满则 drop（chat 可丢，event SSE 也可丢，但 event **store** 不能丢）。统一后 bounded queue 的 drop 策略是 per-subscriber 可配（durable subscriber 不许 drop、ephemeral 可 drop）吗？INV-F1/F2 的期望值取决于此。

**给 engineer：**

5. **统一后 EventStore 是否仍是唯一 SQLite 写入点（`insert_local`/`insert_remote` 两个方法）？** 我要用 `CountingEventStore` spy 断言「chat publish 期间 insert 计数增量 = 0」（INV-A2）。如果有别的写库路径，spy 挡不全，得换断言方式。

6. **fan-out 到 local subscriber 用同步回调还是入队？** 今天 EventBus 同步回调（保 subscriber 间顺序）、WebChannel 入队。统一后如果全改入队，我需要一条测试证明「多 subscriber 收到的相对顺序仍确定」。你倾向哪种，以便我把 INV-E/G5 的顺序期望写对？

7. **能否在 harness 里提供一个「故意乱序 task 完成」的注入点**（见 §3 风险 2）？我要用它先证明 INV-E1（顺序不变式）在**有 create_task-per-event bug 的实现下会变红**——否则这个守 footgun 的测试可能是假绿的。你实现时如果所有 fan-out 都走单 task 顺序消费，请告诉我那个「单 task」在哪，我好在测试里针对它构造竞态。

8. **跨线程 publish 还存在吗？**（web_stream.py 用了 `call_soon_threadsafe`，storage 有 `threading.Lock`。）如果统一后所有 publish 都在同一 event loop 线程，我可以省掉并发线程安全测试（§3 风险 6）；如果还有跨线程入口，我需要补一整类并发测试。请明确入口线程模型。
