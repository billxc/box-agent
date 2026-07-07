"""内容无关的消息总线核心。

`MessageBus` 只按 `topic` 路由，`payload` 对核心透明——从不检查。`publish` 是同步的：
按注册顺序 for-loop 遍历匹配 topic 的订阅者。每条消息不 create_task（坑 #1）；
异步全在 `RemoteSubscriber` 的单个 pump task 里。先注册先投递（有序槽位——后续阶段
依赖 store-subscriber 最先且同步执行）。

Topic 匹配（刻意保持简单——线性扫描，无 trie；集群只有 3-4 个节点）：
  - exact：pattern "chat.m.b.c" 只匹配 topic "chat.m.b.c"
  - prefix：以 "." 结尾的 pattern 匹配任何以它开头的 topic，故
            "events." 匹配 "events.scheduler.run"，
            "events.scheduler." 匹配 "events.scheduler.run" 但不匹配
            "events.cluster.x"。

本模块是中立叶子：不 import 任何项目内部代码。
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Callable

from boxagent.bus.message import Packet

if TYPE_CHECKING:
    from boxagent.bus.subscriber import Subscriber

logger = logging.getLogger(__name__)

# 订阅观察者：一个 (prefix, on_add, on_remove) 三元组。当有人 subscribe / 退订一个
# 前缀匹配的 EXACT topic 时被通知，用来把"本机有人在看某远端 chat"这种 demand
# 沿 WS 往上游传播。只报告 exact-topic 订阅——bridge 自己的前缀订阅（如 "chat."）
# 不算 demand，故不上报。
SubscriptionWatcher = tuple[str, "Callable[[str], None]", "Callable[[str], None]"]


class Subscription:
    """`MessageBus.subscribe` 返回的句柄。`close()` 退订。

    close 幂等，总线销毁后调用也安全。`order` 是进程内单调递增序号，让 fan-out
    能恢复全局订阅顺序（尽管订阅按 topic 索引）。
    """

    def __init__(
        self,
        bus: "MessageBus",
        topic_pattern: str,
        subscriber: "Subscriber",
        order: int,
    ) -> None:
        self._bus = bus
        self.topic_pattern = topic_pattern
        self.subscriber = subscriber
        self.order = order
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._remove(self)


class MessageBus:
    """同步、有序、内容无关的 publish/subscribe。"""

    def __init__(
        self,
        *,
        machine_id: str = "",
        id_factory: "Callable[[], str] | None" = None,
    ) -> None:
        # machine_id 盖在每个 packet 的 `sender` 上；id_factory 在 send() 处生成
        # `message_id`（可注入，让测试拿到确定性 id——绝不在 fan-out 深处 uuid4()，
        # 那会破坏无时钟/可测试契约，同 caller 提供 ts 的理由）。
        self._machine_id = machine_id
        self._id_factory: "Callable[[], str]" = (
            id_factory if id_factory is not None else (lambda: uuid.uuid4().hex)
        )
        # 按 topic 索引，让一次 publish 只碰相关订阅，而非进程内全部订阅（整个节点
        # 共用一根总线，故 chat stream_delta 不能扫无关的 event/chat 订阅）：
        #   _exact：exact-topic pattern → subs，O(1) 查找（chat 热路径）
        #   _prefix：prefix pattern（以 "." 结尾）→ 用 startswith 扫描
        #            （只有 events.* 家族 + 少量 /events SSE 订阅）
        # `order` 恢复两者间的全局先订阅先执行顺序。
        self._exact: dict[str, list[Subscription]] = {}
        self._prefix: list[Subscription] = []
        self._next_order = 0
        # 订阅观察者（chat bridge 用来传播 demand）。见 SubscriptionWatcher。
        self._watchers: list[SubscriptionWatcher] = []

    def watch_subscriptions(
        self,
        topic_prefix: str,
        on_add: "Callable[[str], None]",
        on_remove: "Callable[[str], None]",
    ) -> None:
        """注册一个订阅观察者：当有人 subscribe / 退订一个以 ``topic_prefix`` 开头
        的 EXACT topic 时，分别调用 ``on_add(topic)`` / ``on_remove(topic)``。

        每次 add/remove 都触发（不去重）；调用方自行 refcount。只报告 exact-topic
        订阅——bridge 自己在同一 prefix 上的前缀订阅不算 demand。"""
        self._watchers.append((topic_prefix, on_add, on_remove))

    def subscribe(
        self,
        topic_pattern: str,
        subscriber: "Subscriber",
    ) -> Subscription:
        """为一个 topic pattern 注册订阅者。

        `topic_pattern` 要么是 exact topic（"chat.m.b.c"），要么是以 "." 结尾的
        prefix（"events." / "events.scheduler."）。返回 `Subscription`，调 `.close()`
        退订。
        """
        subscription = Subscription(self, topic_pattern, subscriber, self._next_order)
        self._next_order += 1
        if topic_pattern.endswith("."):
            self._prefix.append(subscription)
        else:
            self._exact.setdefault(topic_pattern, []).append(subscription)
            self._notify_watchers(topic_pattern, added=True)
        return subscription

    def has_subscribers(self, topic: str) -> bool:
        """publish 到 ``topic`` 是否有任何存活订阅会收到（exact 桶或匹配的
        prefix）。只读探查。"""
        if self._exact.get(topic):
            return True
        return any(topic.startswith(sub.topic_pattern) for sub in self._prefix)

    def send(self, *, receiver: str, topic: str, payload: dict, ts: float) -> str:
        """位置统一的 send。盖上 `message_id`（UUID）+ `sender`（本机），当 packet
        寻址到本机时投递给本地订阅者——`receiver == ""`（广播）或 `receiver ==` 本机。
        返回盖好的 `message_id`。

        LocalBus 只能到达本机：寻址到*别的*机器的 packet 会被盖章并返回 id，但在这里
        无处可去——ClusterBus 才负责经链路发出去。
        """
        packet = Packet(
            message_id=self._id_factory(),
            sender=self._machine_id,
            receiver=receiver,
            topic=topic,
            payload=payload,
            ts=ts,
        )
        if receiver == "" or receiver == self._machine_id:
            self._deliver_local(packet)
        return packet.message_id

    def publish(self, topic: str, payload: dict, ts: float) -> None:
        """`send()` 之上的广播垫片——保留到调用方全部迁移到 send 为止。"""
        self.send(receiver="", topic=topic, payload=payload, ts=ts)

    def _deliver_local(self, packet: Packet) -> None:
        """按顺序把 packet fan-out 给每个匹配的本地订阅者。

        同步且有序：按 `order` 排序匹配到的订阅，保持 store-subscriber 优先 /
        先订阅先执行。某订阅者 `deliver` 抛异常会被捕获并记录，绝不能中断其他
        订阅者（订阅者异常隔离）。
        """
        topic = packet.topic
        # 收集匹配：O(1) exact 桶 + 扫描（很小的）prefix 列表。
        matched = list(self._exact.get(topic, ()))
        for subscription in self._prefix:
            if topic.startswith(subscription.topic_pattern):
                matched.append(subscription)
        # 恢复全局注册顺序；列表是快照，故某订阅者在 fan-out 途中关闭自己（或别人）
        # 的订阅是安全的。
        if len(matched) > 1:
            matched.sort(key=lambda subscription: subscription.order)
        for subscription in matched:
            try:
                subscription.subscriber.deliver(packet)
            except Exception:
                logger.warning(
                    "subscriber for pattern %s raised on topic %s; continuing",
                    subscription.topic_pattern,
                    topic,
                    exc_info=True,
                )

    def _remove(self, subscription: Subscription) -> None:
        if subscription.topic_pattern.endswith("."):
            try:
                self._prefix.remove(subscription)
            except ValueError:
                pass
        else:
            bucket = self._exact.get(subscription.topic_pattern)
            if bucket is not None:
                try:
                    bucket.remove(subscription)
                except ValueError:
                    pass
                if not bucket:
                    del self._exact[subscription.topic_pattern]
            self._notify_watchers(subscription.topic_pattern, added=False)

    def _notify_watchers(self, topic: str, *, added: bool) -> None:
        for prefix, on_add, on_remove in self._watchers:
            if topic.startswith(prefix):
                callback = on_add if added else on_remove
                try:
                    callback(topic)
                except Exception:
                    logger.warning(
                        "subscription watcher for prefix %s raised on topic %s",
                        prefix, topic, exc_info=True,
                    )
