# Round 2 — TESTER：把 RPC 折叠进 MessageBus 给回归网加了什么，还安全吗？

作者视角：QA / 回归风险。承接 run_1 决策（内容无关 MessageBus + `PeerTransport` + 冻结不变式层 + 2/3-node harness + migration-map）。所有断言只看外部边界（store rows / queue contents / 发给 fake peer 的 frames / RPC 返回的 body），从不 peek 私有状态（`_pending` / `_subscribers` / `_pumps`）。

结论先行：**RPC-on-bus 是三块折叠里回归风险最高的一块，但风险是可控的、且大部分是「新增负向不变式」而非「改期望值」**。原因：RPC 是唯一一条 **request→reply 双向关联（correlated round-trip）** 语义，chat/event 都是单向 fan-out。run_1 的 harness 只建模了单向消息流（publish → 远端 subscriber 收到），**完全没有 round-trip + 关联 + 超时 + pending-future 生命周期** 这一维。折叠进 bus 意味着 `_PendingResponse` future 关联机制要么保留、要么用 bus 的 reply-topic 重建——这是全新的、今天几乎没被测试网罩住的一块（现有 RPC 测试极其单薄，见 §2）。

---

## 0. 现状盘点：今天 RPC 到底是什么、测了什么

### 0.1 RPC 的真实形态（读代码后的精确账目）

RPC **不是** 走 `on_unknown_frame` 链的（那条链是 `event_batch`/`chat_subscribe` 用的）。RPC 是 registry/guest_client 的 WS 消息循环里的**一等 frame 类型**，硬编码在 dispatch 里：

- **发起端 `call()`**（`registry.py:81-105` GuestSession / `guest_client.py:87-115` GuestClient）：
  - 生成 `rpc_id = uuid4().hex`，建 `_PendingResponse`（内含 `asyncio.Future`），存进 `self._pending[rpc_id]`。
  - `ws.send_json({type:rpc, id, method, path, query, body})`。
  - `await asyncio.wait_for(pending.result, timeout=30.0)`。
  - `finally: self._pending.pop(rpc_id, None)`（无论成功/超时/异常都清）。
- **接收端 loopback re-issue**（`registry.py:211-269` `_serve_inbound_rpc` / `guest_client.py:315-355` `_handle_rpc`）：
  - 收到 `type:rpc` → `asyncio.create_task(self._serve_inbound_rpc(...))`（registry.py:359）/ `create_task(self._handle_rpc(...))`（guest_client.py:283）。
  - 在**自己的本地 web 端口**上用 `ClientSession.request(method, url, params=query, json=body)` 重放这个 HTTP 请求（`http://127.0.0.1:{local_web_port}{path}` + `Authorization: Bearer {local_web_token}`）。
  - 复用了全部 `_handle_web_*` handler；host 端重放时，如果 target 是**另一个** guest，`dispatch_machine_request` 会再 proxy 一跳（**两跳**）。
  - 把 `{type:rpc_resp, id, status, body}` 发回。
- **回复端 `_resolve` / rpc_resp 分支**（`registry.py:107-110` + `registry.py:360-365` / `guest_client.py:288-294`）：按 `id` 找 `_pending`，`future.set_result({status, body})`。

### 0.2 两跳拓扑（关键，harness 必须覆盖）

`guest A → host → guest B`：
1. A 的 web handler 调 `dispatch_machine_request("B", ...)` → A 是 guest → `_proxy_via_host` → `guest_client.call()` 把 rpc 发给 host。
2. host 收到 `type:rpc` → `_serve_inbound_rpc` → 在 host 本地 web 端口重放 → host 的 `_handle_web_*` 又调 `dispatch_machine_request("B", ...)` → host 是 host → `_proxy_to_remote` → `session.call()` 把 rpc 发给 B。
3. B 收到 → `_handle_rpc` → 在 B 本地端口重放 → 真正的 `_handle_web_history` 跑出真 history rows → `rpc_resp` 回 host → host 的 loopback HTTP response → `rpc_resp` 回 A → A 的 `dispatch_machine_request` 返回 `web.json_response(body, status)`。

**同一次逻辑 RPC 在链路上有两对独立的 `(rpc_id, _pending)`**（A↔host 一对，host↔B 一对），id 各自独立生成、互不相干。这一点在 bus 化后必须保持——否则两跳会 id 撞车。

### 0.3 现有 RPC 测试 —— 极其单薄（这是最大的隐忧）

| 文件 | 覆盖了什么 | 数量 | 缺口 |
|------|----------|------|------|
| `test_cluster_rpc.py` | 只测 `ClusterRpc.dispatch_machine_request` 的**路由分支**：local→None、unknown machine→404、无路由→503。**全用 MagicMock，从不真跑一次 round-trip。** | 3 | 没有任何真实 request→reply；没有两跳；没有 loopback 重放；没有超时；没有并发 |
| `test_cluster_registry.py::TestRpcRoundtrip` | `session.call` 发 rpc frame → 手动 `_resolve` → 断言 `{status, body}`；`call` 超时 raise `TimeoutError`。**单跳、单条、手工注入 resp。** | 2 | 没有 loopback re-issue（`_serve_inbound_rpc` **零测试**）；没有并发关联；没有 reply-after-timeout；host 侧 disconnect-cleanup 缺失 |
| `test_admin_cluster_restart.py` | guest 模式 `_handle_admin_cluster_restart` 走 `fetch_host_json`（**不是** WS RPC，是一次性 HTTPS）→ 200/503/502。 | 3 | 这条走的是 `fetch_host_json` 旁路，**不经 WS RPC bus**，折叠时要小心别把它也吸进去（它有独立的 devtunnel HTTPS 语义） |

**合计 RPC 相关现存测试：8 个，其中真正跑 WS round-trip 的只有 2 个（`test_cluster_registry.py::TestRpcRoundtrip`），且都是单跳、单条、手工注入。**

**`_serve_inbound_rpc`（host 侧 loopback 重放）和 `_handle_rpc`（guest 侧 loopback 重放）——这两个 RPC 的执行核心——今天完全零单测。** 两跳完全零测试。并发多 in-flight 关联完全零测试。这意味着：**折叠 RPC 进 bus 时，我们几乎是在没有回归网的情况下重写它。** 这本身就是 run_1 plan 之上的**新增风险**——不是因为 bus 化难，而是因为被折叠的这块**原本就没被罩住**。

### 0.4 已发现的现状 footgun（bus 化必须保留/修复的行为）

1. **host 侧无 in-flight 清理**（不对称 bug/设计缺陷）：`GuestClient._run_forever` 在 disconnect 时（`guest_client.py:255-260`）会把所有 in-flight `_pending` future `set_exception(RuntimeError("guest: ws disconnected"))`，让 caller 立刻拿到干净错误。**但 host 侧 `GuestSession` 没有对应逻辑**——guest 断线时，host 上正在 `await session.call()` 的 RPC 只能干等到自己的 30s 超时。bus 化时这个不对称要么保留（有意）要么统一修掉，但**必须由测试钉死行为，不能悄悄改**。
2. **timeout 双层**：`call` 内部 `wait_for(timeout=30.0)`；`dispatch_machine_request` 外层 `except asyncio.TimeoutError → 504`。折叠后 reply-topic 的等待仍需一个超时，且超时后**必须清 pending，不能泄漏 future**。
3. **loopback 重放依赖 local_web_port/token**：`_serve_inbound_rpc` 若 `local_web_port==0` → 直接回 503（`registry.py:226-234`）。这是 gateway 装配顺序依赖（web app 起来后才注入）。bus 化后 executor 收敛成一份，这个「未配置→503」的降级不能丢。
4. **`create_task` per-inbound-rpc**：`registry.py:359` / `guest_client.py:283` 对每个 inbound rpc 起一个 task。这是**对的**（RPC 是独立 request，本就该并发，不像 stream_delta 要保序）——**这是 RPC 与 chat/event 的根本差异**：坑 #1「create_task 乱序」对 chat/event 是禁令，对 RPC 反而是正确模型。折叠时**绝不能**把 RPC 也塞进 `RemoteSubscriber` 的单 pump 顺序队列——那会把并发 RPC 串行化，一个慢 RPC 阻塞后面所有 RPC。**这是折叠 RPC 最容易踩的架构错误**（详见 §5.4）。

---

## 1. RPC-on-bus 需要哪些新冻结不变式（frozen 层新增）

沿用 run_1 的 INV 命名法，RPC 专属不变式用 **INV-R\*** 前缀（R = RPC），交叉的用 **INV-DR\***（D=cross，R=rpc）。每条给 input / expected / why。这些进 `test_message_bus_invariants.py` 冻结层，**整个迁移一行不改**。

### R. RPC round-trip 核心（今天几乎没测，最重要）

- **INV-R1「单跳 RPC：发给机器 B 的请求返回 B 的真实 HTTP response body，按 id 关联」** ★
  input：2-node harness（A owner + B）；A 上触发 `dispatch_machine_request("B", "GET", "/api/history", query={bot:"b1", chat_id:"c1"})`；B 上有真 storage 含已知 history rows。
  expected：返回 `web.Response`，`status==200`，`body` == B 的**真** history rows（逐行相等），**不是** stub、不是 echo。
  why：这是 RPC 存在的理由——「在 B 上真跑一次 `_handle_web_history`」。bus 化后 request 变成 publish 到 B 的 target-topic、reply 变成关联消息，最容易退化成「关联对了但 body 是空壳」。必须断言 body 是真实 handler 输出，不是关联机制自身的产物。

- **INV-R2「loopback executor 仍复用真 `_handle_web_*` handler」** ★（INV-R1 的机制断言）
  input：同上，但 B 的 `_handle_web_history` 被一个 spy 包裹（记录被调用 + 参数）。
  expected：B 上**真的**调到了 `_handle_web_history`，且收到的 `query` == `{bot:"b1", chat_id:"c1"}`（query 透传不丢）、method/path 正确。
  why：run_1 的核心卖点是「loopback-reissue executor 从 host+guest 双份收敛成一份共享件」。收敛后必须证明**那一份**仍然打到真 handler、参数透传完整——否则「收敛」变成「换了个空壳」。这条守住 method/path/query/body 四要素透传。

- **INV-R3「两跳 RPC：guest A → host → guest B 返回正确 body」** ★★（run_1 harness 没有的拓扑维）
  input：3-node harness（gA — host — gB）；gA 触发对 gB 的 `/api/session_info`（一个 GET）。
  expected：gA 拿到 gB 的真 session_info body；**中间 host 不篡改**；两对 `(rpc_id,_pending)` 各自独立关联，reply 不串。
  why：两跳是今天**零测试**的拓扑，也是 bus 化最易破的地方（host 的 loopback 重放又触发一次 dispatch → 又一次 publish/reply）。必须证明「一次逻辑 RPC = 两段独立关联 round-trip，正确嵌套」。

- **INV-R4「并发 in-flight RPC 不串 reply（id 关联隔离）」** ★★
  input：A 对 B **并发**发起 N=50 个不同 RPC（不同 path，返回可区分的 body，如 `/api/x?n=0..49`）；handler 端故意乱序返回（快的先回、慢的后回）。
  expected：每个 `dispatch_machine_request` 调用拿到的 body **恰好对应它自己的请求**（`n=k` 的请求拿到 `n=k` 的 body），**零串台**。
  why：这是 RPC 与 chat/event 最本质的区别——chat/event 是「广播/订阅，谁都能收」，RPC 是「一对一关联，绝不能收错」。bus 化后如果 reply 用 topic 而非精确 id 匹配，两个并发 RPC 走同一 target-topic 就可能收到对方的 reply。**必须用「乱序返回 + 每个请求 body 可区分」构造，能因串台变红。** 今天完全没有这条。

- **INV-R5「不可达机器的 RPC 干净超时，不 hang，不泄漏 pending future」** ★
  input：A 对一个「已注册但 WS 已死/永不回复」的机器发 RPC，超时设短（如 0.1s）。
  expected：(a) `dispatch_machine_request` 在 ~超时后返回 `504`（不是永久 hang）；(b) **超时后 `_pending`（或 bus 的 reply-waiter 表）里没有该 id 的残留**——用 spy 或 `len(pending)` 观测点断言归零。
  why：pending-future 泄漏是慢性内存/关联表膨胀。今天 `call` 的 `finally: pop` 保证了这点；bus 化后 reply-waiter 换实现，**必须重新证明超时清理**。这是负向 + 资源不变式，最易在重写中丢。

### DR. 交叉不变式（Phase 7 帧统一后才出现的新风险）★ 空白区

- **INV-DR1「RPC 帧不投递给 chat/event subscriber，反之亦然（topic 隔离）」** ★★（Phase 7 头号 no-go）
  input：Phase 7 统一 `v:2` 帧后，同一根 WS 上交替流过：RPC request（topic=`rpc.<machine>.<id>` 或等价）、chat delta（`chat.*`）、event batch（`events.*`）。
  expected：RPC request **只**触发 loopback executor（不进任何 chat queue、不落 event store）；chat delta **只**进 chat queue（不触发 RPC executor、不产生 rpc_resp）；event **只**进 store。三类互不误投。
  why：run_1 把 chat/event 帧统一进 `v:2` 后靠「topic 前缀路由」隔离；RPC 加入后**多了第三种 topic 语义（request/reply）**。如果 RPC 的 target-topic 前缀设计不当（比如 RPC 也用 `chat.` 前缀寻址目标机器），ChatReplicator 会误吞 RPC 帧。这条是 RPC 折叠进统一帧后的**头号回归风险**——必须在 Phase 7 gate 前变红可检测。

- **INV-DR2「RPC 的 reply 帧不被当成 chat/event 广播（reply 定向回发起者，不 fan-out）」** ★
  input：3-node 全连通；gA 对 host 发 RPC；host 回 rpc_resp。
  expected：rpc_resp **只**回到 gA（关联 gA 的 pending），**不**被 gossip 给 gB、不进任何 store/queue。
  why：event 是 gossip 广播、chat 是定向 relay，RPC reply 是**点对点关联回发**。统一帧后若 reply 误入广播路径，会既泄漏（B 看到 A 的 RPC 结果）又可能触发 B 上的 executor 重放。run_1 §3 风险 7（回环/广播风暴）的 RPC 版。

- **INV-DR3「RPC 请求/回复绝不落 event store」**（INV-A2 的 RPC 版）
  input：A 对 B 发 100 个 RPC；两边 `CountingEventStore` spy。
  expected：两边 store insert 增量 == 0（RPC 是 ephemeral request/reply，比 chat 更不该落库）。
  why：RPC 走同一根 bus 后，若 executor 或 reply 路径不小心复用了 event 的 durable-topic 订阅，高频 RPC（logs 分页、session 列表刷新）会泄漏进 SQLite。RPC topic **必须**是 ephemeral（无 StoreSubscriber）——run_1「durability = subscriber-list fact」模型天然支持，但要有测试钉死。

### 小结（frozen 层新增 8 条）

R1（单跳真 body）、R2（复用真 handler）、R3（两跳）、R4（并发关联隔离）、R5（超时不泄漏）+ DR1（帧 topic 隔离）、DR2（reply 不广播）、DR3（不落库）。**其中 R3/R4/DR1 是全新维度（round-trip + 关联 + 三类帧隔离），今天连近似测试都没有。**

---

## 2. 哪些现有 RPC 测试会改——红旗还是预期迁移？（886-baseline-trap guard）

逐条判断，映射到新不变式。红旗判据：**任何原本断言外部行为（返回 body / status / 发出的 frame）的测试，若迁移后期望值变了（不只是调用方式变），停下来问 owner——那是行为回归。**

| 现有测试 | 断言的是 | 会改吗 | 判定 | 迁到哪条 INV |
|---|---|---|---|---|
| `test_cluster_rpc.py::test_local_returns_none` | dispatch 对 local machine 返回 None（调用方继续本地处理） | **不该改** | 保留 | 这是 `dispatch_machine_request` 的外层路由契约，与 bus 无关，冻结 |
| `test_cluster_rpc.py::test_remote_unknown_machine_404` | 未知机器→404 | **不该改** | 保留 | 冻结（路由契约） |
| `test_cluster_rpc.py::test_no_routing_returns_503` | 无 registry/client→503 | **不该改** | 保留 | 冻结（路由契约） |
| `test_cluster_registry.py::TestRpcRoundtrip::test_call_resolves_on_rpc_resp` | `session.call` 发 `type:rpc` frame + `_resolve` 后拿到 `{status,body}` | **预期要改** | 迁移（非红旗） | 断言的是 `GuestSession.call` 具体 API + `rpc` frame 字面；bus 化后 `call` 被 bus 的 request/reply pattern 吸收。**行为**（发请求→关联→拿 body）迁进 **INV-R1**（用 harness 表达，不 peek `.sent[0]["type"]=="rpc"`） |
| `test_cluster_registry.py::TestRpcRoundtrip::test_call_timeout` | `call` 超时 raise `TimeoutError` | **预期要改** | 迁移（非红旗） | 迁进 **INV-R5**，但**升级**：不只测「超时 raise」，还要测「超时后 pending 清理无泄漏」。今天只测了 raise，没测清理——这是**加强**不是削弱 |
| `test_cluster_registry.py::TestGuestRegistry::*`（get_bot/list_bots） | registry 的 bot 索引 | **不该改** | 保留 | 与 RPC bus 无关，冻结 |
| `test_cluster_registry.py::TestHelloHandshake::*` | hello token 校验 / reconnect 替换 session | **不该改** | 保留 | hello 握手是 WS 连接层，不是 RPC 层，冻结 |
| `test_admin_cluster_restart.py::*`（3 条） | guest 模式 restart 走 `fetch_host_json`（HTTPS 旁路） | **大概率不改** | 保留（但需确认） | `fetch_host_json` 是**独立于 WS RPC 的一次性 HTTPS 路径**。折叠 WS RPC 进 bus **不应**碰它。**红旗预警**：如果有人「顺手」把 `fetch_host_json` 也统一进 bus，这 3 条会改——那要停下来，因为 `fetch_host_json` 有独立的 devtunnel 认证语义（`X-Tunnel-Authorization`），不是 WS 帧 |

**结论**：只有 **2 条**真正会改（`TestRpcRoundtrip` 的 2 条），且都是**实现耦合→行为迁移**（断言 `call` API + `rpc` frame 字面 → 迁进 INV-R1/R5 的黑盒表达），**非红旗**。其余 6 条要么是路由契约、要么是 HTTPS 旁路，**不该改，改了就是红旗**。

**886-baseline-trap 具体防法**：迁移 `TestRpcRoundtrip` 2 条时，**不删**——把行为迁进 `test_message_bus_invariants.py` 的 INV-R1/R5，在 `docs/bus-migration-map.md` 加两行（`test_call_resolves_on_rpc_resp → INV-R1`、`test_call_timeout → INV-R5`），确认新 INV 绿了再删旧。净测试数**必涨**（新增 R1/R2/R3/R4/R5/DR1/DR2/DR3 八条 + `_serve_inbound_rpc`/`_handle_rpc` 今天零测试的 loopback executor 补测）。**这块是 baseline 只涨不跌里涨得最多的——因为它原本欠债最多。**

---

## 3. 2/3-node harness 需要为 RPC 扩展吗？（需要，是新增维度）

**需要，且是 run_1 harness 最大的一处结构性扩展。** run_1 harness 建模的是**单向 fan-out**：`publish_event`/`publish_chat` → 远端 subscriber queue / store 收到。RPC 是 **request→reply round-trip + 关联 + 两跳嵌套**，run_1 的原语（`publish_*` / `subscribe_chat` / `store_rows` / `settle`）**表达不了 RPC**。

### 3.1 harness 新增原语

在 `tests/unit/_bus_harness.py` 的 `TwoNodeCluster` / `ThreeNodeCluster` 上加：

- **`node.serve_web(path, handler)`** —— 注册一个 fake 本地 web handler，供 loopback executor 重放时命中。默认注册几个真形状的 handler（`/api/history` 返回可控 rows、`/api/session_info` 返回可控 dict、`/api/x?n=k` 回显 n）。这是 INV-R1/R2/R3 的「真 handler」来源——**harness 必须能证明 executor 打到了真 handler，而不是 harness 自己伪造 reply**。所以 handler 用 spy 包裹（记录 called + 收到的 method/path/query/body）。
- **`node.rpc(target_machine, method, path, query, body, timeout) -> (status, body)`** —— 走真正的 `dispatch_machine_request` → bus request/reply → 远端 loopback executor → 真 handler → reply 关联回来。这是 RPC 的**发布点**（对应 chat 的 `publish_chat`、event 的 `publish_event`）。
- **`node.pending_rpc_count()`** —— 观测点，读 reply-waiter 表的长度（不 peek 内部结构，暴露一个只读计数）。INV-R5 用它断言超时后归零。
- **`cluster.hold_replies(node, predicate)` / `release_replies()`** —— 让某节点**暂缓/乱序**回 reply 的注入 hook。INV-R4（并发关联隔离）靠它构造「乱序返回」；INV-R5 靠它构造「永不回复→超时」。这是 RPC 版的「reorder 注入」，对应 run_1 §Phase-0 的 `reorder_tasks`。

### 3.2 拓扑：两跳必须能真跑

run_1 已有 `ThreeNodeCluster`（gA — host — gB）用于 chat 两跳 relay + event gossip。RPC 复用同一拓扑，但**路径不同**：RPC 是 gA→host→gB 的**嵌套 round-trip**（host 收到后在自己端口重放，重放又触发对 gB 的 dispatch）。harness 必须让 host 节点的 loopback executor 能真的再发一次 RPC 给 gB。这要求 `ThreeNodeCluster` 的 host 节点同时挂 **guest_registry**（对 gB）+ **guest_client 反向**（收 gA），且 host 的 `serve_web` 里 `/api/history` handler 内部会调 `dispatch_machine_request("gB", ...)`——**即 host 的 web handler 本身有「继续 proxy」逻辑**。这是 harness 里最需要还原真实的一环。

### 3.3 harness 自测（否则假绿）

新增 `test_bus_harness.py::test_rpc_roundtrip_returns_handler_body`（单跳）、`test_rpc_two_hop_nested_correlation`（两跳）、`test_hold_replies_forces_reorder`（证明乱序 hook 真能乱序）、`test_pending_count_drops_after_timeout`（证明超时清理观测点有效）。**尤其 `hold_replies` 的乱序 hook 要先证明能让 INV-R4 在一个「用 topic 而非 id 匹配 reply」的故意错误实现下变红**——否则 INV-R4 是假绿的守门员（对应 run_1 对 `reorder_tasks` 的同款要求）。

---

## 4. RPC-on-bus 专属的负向 / 安全测试

这些是 §1 里 R5/DR\* 的展开，单列强调因为它们是「重写关联机制」最易破的点：

- **NEG-R1「永不回复 → 超时不泄漏 pending future」**：`hold_replies(node_b, forever)`；A 发 RPC，timeout=0.1s → 504；`settle()` 后 `node_a.pending_rpc_count() == 0`。**双断言**：(a) caller 拿到 504（不 hang）；(b) 关联表归零（不泄漏）。今天 `call` 的 `finally:pop` 保证了 (b)，bus 化后必须重证。
- **NEG-R2「迟到的 reply（超时后才回）不崩、被安全丢弃」**：A 发 RPC，timeout=0.05s（先超时）；0.2s 后 B 才回 rpc_resp。expected：reply 到达时 `_resolve`/reply-waiter 发现该 id 的 future 已不在表里（已被 finally 清）→ **静默丢弃，不抛异常、不崩 WS 循环**。今天 `_resolve` 有 `if p and not p.result.done()` 守卫（`registry.py:108-110`），guest 侧同理（`guest_client.py:290`）——bus 化后这个「id 不存在则丢」的守卫**必须保留**。这条今天**零测试**，是明确空白。
- **NEG-R3「reply id 从未见过（伪造/错乱 id）→ 丢弃不崩」**：注入一个 `rpc_resp` 带 A 从没发过的 id。expected：静默丢弃。防御性，防未来两跳 id 命名空间设计错误时崩溃。
- **NEG-R4「topic 隔离：RPC 帧不进 chat/event，chat/event 不触发 executor」**（= INV-DR1 的负向压力版）：Phase 7 后，向一个只订阅 `chat.*` 的 subscriber 的 WS 灌一个 RPC request 帧。expected：subscriber queue 收到 0 个、RPC executor 也没被 chat delta 触发。这是**帧统一后**才可测——run 在 Phase 7 gate。
- **NEG-R5「in-flight RPC 期间 WS 断开 → caller 拿干净错误，不 hang 到超时」**：A 发 RPC；reply 未回时 `drop_link(A,host)`。expected：A 的 `dispatch_machine_request` **立刻**（不等 30s）拿到 502/连接错误。**这条暴露了 §0.4 footgun #1 的不对称**——guest 侧今天有 disconnect-cleanup（`guest_client.py:255-260`），host 侧 `GuestSession` 没有。bus 化时必须**决定并测试**：是两侧都做 disconnect-reject（推荐，对称），还是保留不对称。**无论选哪个，都要有测试钉死，不能悄悄改。**

---

## 5. Per-phase gate delta：新增「RPC onto bus」阶段 + reconnect 不变式扩展

### 5.1 新增 Phase 6.5「RPC 折叠进 bus」（在 Phase 6 合并 syncer 之后、Phase 7 帧统一之前）

放这个位置的理由：Phase 6 才刚把两份 syncer 收敛成 `EventReplicator`+`ChatReplicator` over `PeerTransport`；RPC 的 loopback executor 双份收敛（host `_serve_inbound_rpc` + guest `_handle_rpc` → 一份共享 executor）**依赖 PeerTransport 已就位**，所以必须在 Phase 6 之后。但必须在 Phase 7（帧统一）**之前**——因为 Phase 7 的 DR1（三类帧 topic 隔离）需要 RPC 已经是 bus 上的一等公民才谈得上「隔离」。

- **Changes**：把 `GuestSession.call` + `GuestClient.call`（发起端关联）收敛成 bus 的 request/reply pattern（publish 到 target-topic + await reply-correlated-by-id）；把 `_serve_inbound_rpc` + `_handle_rpc`（loopback 重放 executor）收敛成**一份共享 executor**（host/guest 只差「重放后可能再 proxy 一跳」，那是 handler 层的事，executor 本身同一份）。RPC 仍走**独立的并发模型**（每 inbound RPC 一个 task，**不**进 RemoteSubscriber 单 pump——见 §5.4）。`rpc`/`rpc_resp` frame 暂时保留字面（Phase 7 才并入 `v:2`）。
- **Why reversible**：两份旧 call/executor 留在 git，revert 一个 commit。
- **GATE（named no-go）**：**INV-R1 + INV-R2 + INV-R3 + INV-R4 + INV-R5 + INV-DR3 全绿**；头号 no-go = **INV-R4（并发关联隔离）**——如果 bus 的 request/reply 用 topic 而非精确 id 匹配 reply，R4 会红，立即停。次号 no-go = **INV-R2（loopback 仍打真 handler）**——收敛 executor 后 `/api/history` RPC 必须返回真 history rows，不是空壳。`pytest ≥ 886` 且净增（R\* + DR3 + loopback executor 补测全是新增）。

### 5.2 Phase 7 gate 扩展（帧统一，RPC 并入 `v:2`）

run_1 的 Phase 7 gate 是 INV-D1（reconnect 双恢复）+ INV-D2（event/chat 帧不互吞）+ mixed-version。**RPC 折叠后新增**：

- **INV-DR1（RPC 帧不误投 chat/event，反之亦然）** 成为 Phase 7 的**并列头号 no-go**——因为 Phase 7 才把 `rpc`/`rpc_resp` 并入 `v:2` topic-addressed 帧，三类帧首次共用一个 `handle_frame` dispatch。这是「三类帧 topic 路由」首次全放一起，最易串。
- **INV-DR2（reply 不广播）** 也在 Phase 7 gate（reply 帧统一后若误入 gossip 路径会广播）。
- **mixed-version 扩展**：run_1 的 mixed-version 测试（v:1 节点收 v:2 帧优雅丢弃）现在**多一类帧**——v:1 节点收到一个 v:2 的 **RPC** 帧（它只懂旧 `rpc`/`rpc_resp` 字面）必须优雅忽略不崩。即 mixed-version gate 要覆盖 rpc/chat/event **三类**帧的降级，不只两类。

### 5.3 reconnect 不变式必须覆盖「RPC-in-flight-during-reconnect」——**是的，必须扩**

run_1 的 INV-D1 只覆盖两半：event cursor-resync + chat re-subscribe。**RPC 加入后 reconnect 有第三种语义，且与前两者本质不同**：

- event：断连期间的漏发，重连后靠 cursor **backfill**（有 backlog，必须补齐）。
- chat：断连期间的 delta **可丢**（live 流，无 backlog），重连只需 re-subscribe 恢复**新** delta。
- **RPC：in-flight 的 request 在断连时既不能 backfill（它是一次性请求，重发语义危险——可能重复执行有副作用的 POST 如 `/api/send`、`/api/admin/restart`），也不能静默丢（caller 在 await）。正确行为 = 立即 reject caller（拿干净错误），caller 层决定是否重试。**

所以 reconnect 不变式**不能**把 RPC 归进 event（backfill）或 chat（静默丢）任何一半，要**新增 INV-DR-RECONNECT**：

- **INV-DR4「reconnect 时三种恢复语义并存且正确」** ★★（Phase 7 gate 的 D1 扩展版）
  input：3-node；B 既订 remote chat `(A,b,c)`、又做 event 复制、**且 A→B 有一个 in-flight RPC 正在 await**；`drop_link(A,B)`；断连期间 A 发 2 event + 3 chat delta；`relink`；`settle`。
  expected：**(a)** event：B.store backfill 那 2 个 event（cursor resync，run_1 原 D1 的一半）；**(b)** chat：B 的 queue 重新收到 A 的**新** delta（re-subscribe，3 个断连 delta 允许丢）；**(c) RPC：那个 in-flight RPC 的 caller 在 drop_link 时立即拿到干净错误（502/连接断），不 hang、不在 relink 后被神秘重放。**
  why：这是 run_1 INV-D1 的严格超集。统一成一根 bus + 一套 reconnect 逻辑后，**极易只处理 backfill 和 re-subscribe 两半，忘了 RPC 的「立即 reject」这第三半**——或更糟，把 RPC 也塞进 backfill 导致有副作用的 POST 被重放（`/api/send` 重发消息、`/api/admin/restart` 重启两次）。**三种语义在同一次 reconnect 里必须都对，缺一即 no-go。** 这直接暴露 §0.4 footgun #1（host 侧无 in-flight cleanup）——bus 化必须补上对称的 disconnect-reject（对应 NEG-R5）。

### 5.4 贯穿红线：RPC 绝不进 RemoteSubscriber 单 pump（架构级 gate）

run_1 的 R3 风险（create_task-per-message 破坏顺序）对 chat/event 是禁令：**必须**单 pump 顺序消费。**RPC 恰好相反**：RPC 是独立 request，**必须**并发（今天 `create_task` per-inbound-rpc 是对的）。如果折叠时把 RPC 也套进 `RemoteSubscriber` 的单 pump 顺序队列，会把并发 RPC 串行化——一个慢 RPC（比如卡住的 `/api/logs` 大分页）阻塞后面所有 RPC，**这是把 chat 的正确模型错误地套到 RPC 上**。

因此新增一条**架构不变式**（也是 Phase 6.5 的 gate）：
- **INV-R6「并发 RPC 不被彼此阻塞（RPC 不共享单 pump）」**：A 对 B 发一个慢 RPC（handler sleep）+ 一个快 RPC；快的**不等**慢的，先返回。expected：快 RPC 的 round-trip 时间 ≪ 慢 RPC。why：证明 RPC 走独立并发路径，没被 chat/event 的保序单 pump 串行化。这条把「RPC ≠ chat/event 的交付模型」钉死成不变式。

---

## 6. 总评：RPC-on-bus 相对 run_1 plan 的净回归风险

**是的，明显抬高，但抬高的主因是 RPC 原本欠债，不是 bus 化本身危险。** 三点：

1. **被折叠的东西回归网最薄**：chat/event 折叠时有 126 个现存测试兜底（run_1 已盘点）；RPC 折叠时只有 8 个测试、其中真跑 round-trip 的仅 2 个、loopback executor（`_serve_inbound_rpc`/`_handle_rpc`）和两跳**完全零测试**。**我们几乎是在没网的情况下重写 RPC 的关联机制。** → 缓解：Phase 6.5 前必须先把 R1-R6 + DR1-DR4 建全并**基于旧实现跑绿**（run_1 Phase 0 同款纪律，只是补给 RPC），把今天零测试的 executor/两跳/并发关联先罩住，再动手折叠。这把「重写无网」变成「重写有网」。

2. **RPC 引入了 chat/event 都没有的三个新维度**：request→reply **关联**（R4 并发不串）、**round-trip 超时+资源清理**（R5/NEG-R1/R2 不泄漏 future）、**两跳嵌套关联**（R3）。这三维 run_1 harness 完全没有，需要实打实扩 harness（§3）。这是新增复杂度，不是想象出来的。

3. **RPC 的交付模型与 chat/event 正相反**（并发 vs 保序），最危险的单点错误 = 把 RPC 塞进 RemoteSubscriber 单 pump（INV-R6 守）；帧统一后最危险 = 三类帧 topic 串投（INV-DR1 守）；reconnect 最危险 = 把有副作用的 in-flight POST 当 backlog 重放（INV-DR4 守）。

**但风险是可控且值得的**：run_1 的模型（内容无关 bus + topic 路由 + PeerTransport + subscriber-list durability）**天然容纳** RPC——RPC 就是「ephemeral 的、点对点关联的、并发交付的」一类 topic，不需要新机制，只需要新不变式钉死它的三个特殊维度。净代码减少（executor 双份→一份、call 双份→一份、`rpc`/`rpc_resp` 帧并入 `v:2`）是真实的。**结论：可以折叠，但 Phase 6.5 的进入门槛 = R1-R6 + DR1-DR4 十条不变式先基于旧实现建绿（补上今天的零测试欠债），否则就是在无网状态下重写关联机制，那才是真正不可接受的风险。** 折叠不改变 run_1 的架构判断，只是给回归网加了「round-trip / 关联 / 两跳 / 并发 / 帧隔离 / reconnect-三语义」这一整块 RPC 专属维度。
