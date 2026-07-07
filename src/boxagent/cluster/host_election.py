"""运行时 host/guest 角色选举与故障切换。

`cluster.host` 是有序 fallback 列表（如 `[mbp, devbox-xl, macmini]`）。
优先级最高且可达的节点持有 cluster tunnel、当 active host，其余当 guest。
角色运行时决定并周期性重估：primary 掉线由下一顺位接管，primary 恢复则
当前 host 降级。

升/降级共用同一个 `cluster.tunnel_name`（`boxagent-cluster`），同一时刻只有
一个节点 host 它——依赖 `devtunnel host` 的互斥性。

状态转换：

  init ──probe──►  guest（别人在 host）
       └───────►  host （无人 host；我是候选）

  guest ──upstream 掉线且我是下一顺位──► host
  host  ──更高优先级候选以 guest 身份出现──► guest
  host  ──devtunnel host 进程意外退出──► guest（下次 probe 重选）

"高优先级顶替低优先级"无需新协议：恢复的 primary 启动后首次 probe 见到
低优先级 host 持有 tunnel，就以 guest 身份加入；低优先级 host 下个 tick 在
registry 里发现更高优先级 session，自愿降级；primary 下个 tick 找不到 host
遂自我升级。

所有权：本对象在角色生命周期内持有 ``tunnel`` / ``registry`` / ``client``，
调用方（Gateway）只读公开属性不回写。topology 变更和 bot 描述符回调在构造时
注入，本模块不触碰 ``gateway._xxx`` 私有状态。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TYPE_CHECKING

import aiohttp

from . import devtunnel
from .guest_client import GuestClient
from .registry import GuestRegistry
from .tunnel import ClusterTunnel
from boxagent.log import Category, log

if TYPE_CHECKING:
    from ..config import AppConfig

logger = logging.getLogger(__name__)


TopologyChangeCb = Callable[[str | None], Awaitable[None]]
BotProviderCb = Callable[[], list[dict]]
RegistryReadyCb = Callable[[GuestRegistry], None]
GuestClientReadyCb = Callable[[GuestClient], None]


@dataclass
class HostElection:
    """决定本节点 host/guest 角色，并让它与现实保持一致。"""

    config: "AppConfig"
    on_topology_change: TopologyChangeCb | None = None
    bot_provider: BotProviderCb | None = None
    on_registry_ready: RegistryReadyCb | None = None
    on_guest_client_ready: GuestClientReadyCb | None = None
    probe_interval: float = 10.0
    # 空 probe 就自我升级前，先重试这么多次（间隔 promote_retry_delay）。
    # 防止单次瞬时 probe 失败（timeout、devtunnel 抽风）时另一节点其实在正常
    # host 而导致 split-brain。
    promote_retry_count: int = 3
    promote_retry_delay: float = 2.0

    state: str = "init"  # "init" | "host" | "guest" | "standalone"
    current_upstream: str = ""

    # 持有的 cluster 组件——由状态转换填充，供 Gateway 读取。
    tunnel: ClusterTunnel | None = field(default=None, repr=False)
    registry: GuestRegistry | None = field(default=None, repr=False)
    client: GuestClient | None = field(default=None, repr=False)

    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: bool = False
    _http: aiohttp.ClientSession | None = field(default=None, repr=False)
    _transition_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def is_host(self) -> bool:
        return self.state == "host"

    @property
    def is_guest(self) -> bool:
        return self.state == "guest"

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = False
        # 启动周期循环前先跑一次 tick，让角色在任何 web 请求到来前就定下来。
        try:
            await self._tick()
        except Exception as e:
            logger.warning("host election: initial tick failed: %s", e)
        self._task = asyncio.create_task(self._run(), name="cluster-host-election")

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._teardown_all()
        if self._http is not None:
            try:
                await self._http.close()
            except Exception:
                pass
            self._http = None

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.sleep(self.probe_interval)
                if self._stop:
                    break
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("host election tick failed: %s", e)

    async def _fire_topology_change(self, changed: str | None) -> None:
        if self.on_topology_change is None:
            return
        try:
            await self.on_topology_change(changed)
        except Exception:
            pass

    # ── 核心决策逻辑 ──

    async def _tick(self) -> None:
        if not self.config.cluster_tunnel:
            self.state = "standalone"
            return

        priority = self.config.host_priority
        my_index = self.config.my_host_index
        my_machine_id = self.config.machine_id

        # 永远先 probe 真实状态，不信自己的自我认知。捕捉 promote 后
        # `devtunnel host` 子进程死掉的情况（tunnel 被 peer 抢走、devtunnel 抽风
        # 等），否则会永远停留在虚假的 "host" 幻觉里。
        upstream = await self._probe_active_host()

        if self.state == "host":
            # 自检：tunnel 是不是真的在服务*我们*？
            tunnel = self.tunnel
            tunnel_dead = tunnel is None or not tunnel.is_alive()
            stolen = upstream and upstream != my_machine_id
            if tunnel_dead or stolen:
                logger.warning(
                    "host election: lost host status (tunnel_dead=%s, "
                    "probe_says='%s', expected='%s') — demoting",
                    tunnel_dead, upstream, my_machine_id,
                )
                log.warning(
                    Category.CLUSTER_HOST_DEMOTED,
                    f"lost host status (tunnel_dead={tunnel_dead}, probe='{upstream}')",
                    machine_id=my_machine_id, tunnel_dead=tunnel_dead, probe=upstream,
                )
                await self._become_guest(upstream or "")
                # 立即重跑一次 tick：要么重新升级（若无其他 host），要么落入新
                # upstream。
                await self._tick()
                return

            # probe 失败（看不到 upstream）但子进程还活着，就接受——可能是
            # devtunnel show 的瞬时抽风。然后检查是否有更高优先级候选以 guest
            # 身份加入、该让位。
            registry = self.registry
            if registry is not None and my_index > 0:
                for sess_machine_id in list(registry.sessions.keys()):
                    if sess_machine_id in priority and priority.index(sess_machine_id) < my_index:
                        logger.info(
                            "host election: demoting; higher-priority candidate '%s' is here",
                            sess_machine_id,
                        )
                        log.info(
                            Category.CLUSTER_HOST_DEMOTED,
                            f"demoting; higher-priority '{sess_machine_id}' joined",
                            machine_id=my_machine_id, displaced_by=sess_machine_id,
                        )
                        await self._become_guest(sess_machine_id)
                        return
            # 本 tick host 稳定。周期性重推 machines 快照，让 guest 保持对全网
            # cluster-bus wire 版本的新鲜视图——尤其是刚升级并重连的 peer，不再被
            # 别人当成旧版本。
            if registry is not None and registry.on_topology_change is not None:
                try:
                    await registry.on_topology_change(None)
                except Exception as exception:
                    logger.warning("host election: periodic snapshot re-push failed: %r", exception)
            return

        # 还不是 host——落入 guest 或升级。
        if upstream and upstream != my_machine_id:
            await self._ensure_guest(upstream)
            return

        if my_index >= 0:
            # 空 probe 可能是"真没 host"，也可能是"peer 在 host 时的瞬时抽风"。
            # 抢 tunnel 前多 probe 几次，避免 split-brain。
            for attempt in range(1, self.promote_retry_count + 1):
                await asyncio.sleep(self.promote_retry_delay)
                if self._stop:
                    return
                upstream = await self._probe_active_host()
                if upstream and upstream != my_machine_id:
                    logger.info(
                        "host election: probe recovered on retry %d (upstream=%s) — staying guest",
                        attempt, upstream,
                    )
                    await self._ensure_guest(upstream)
                    return
            await self._try_promote()
        else:
            # 非候选，且看不到 host。保持安静；一旦有 host 出现，guest_client（若有）
            # 会自己重试。
            await self._ensure_guest("")

    # ── probe ──

    async def _probe_active_host(self) -> str:
        """解析 cluster tunnel URL，向 host 它的节点问 /api/version 拿 machine_id。
        无 host 可达时返回 ""。
        """
        tunnel = self.config.cluster_tunnel
        if not tunnel or not shutil.which("devtunnel"):
            return ""
        try:
            url = await devtunnel.resolve_url(tunnel, port=self.config.web_port or 9292)
        except Exception as e:
            logger.debug("host election: tunnel resolve failed: %s", e)
            log.debug(
                Category.CLUSTER_HOST_PROBE_FAIL,
                "probe: tunnel URL resolve failed",
                tunnel=tunnel, error=repr(e),
            )
            return ""
        try:
            token = await devtunnel.connect_token(tunnel)
        except Exception as e:
            logger.debug("host election: devtunnel token mint failed: %s", e)
            log.debug(
                Category.CLUSTER_HOST_PROBE_FAIL,
                "probe: devtunnel token mint failed",
                tunnel=tunnel, error=repr(e),
            )
            return ""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        headers = {"X-Tunnel-Authorization": f"tunnel {token}"}
        if self.config.host_token:
            headers["Authorization"] = f"Bearer {self.config.host_token}"
        try:
            async with self._http.get(
                f"{url.rstrip('/')}/api/version",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5.0),
            ) as response:
                if response.status != 200:
                    return ""
                data = await response.json(content_type=None)
                return str(data.get("machine_id") or "")
        except Exception as e:
            logger.debug("host election: probe /api/version failed: %r", e)
            log.debug(
                Category.CLUSTER_HOST_PROBE_FAIL,
                "probe: /api/version failed",
                tunnel=tunnel, error=repr(e),
            )
            return ""

    # ── 状态转换 ──

    async def _try_promote(self) -> None:
        async with self._transition_lock:
            if self.state == "host":
                return
            logger.info("host election: attempting promote to active host")
            await self._teardown_guest()

            tunnel = ClusterTunnel(
                name=self.config.cluster_tunnel,
                port=self.config.web_port or 9292,
            )
            try:
                url = await tunnel.start()
            except Exception as e:
                logger.warning("host election: promote failed (tunnel host busy?): %s", e)
                log.warning(
                    Category.CLUSTER_TUNNEL_ERROR,
                    "promote failed (tunnel host busy?)",
                    machine_id=self.config.machine_id,
                    tunnel=self.config.cluster_tunnel,
                    error=repr(e),
                )
                # 降级为 guest——tunnel 被别人持有。
                await self._ensure_guest_locked("")
                return
            self.tunnel = tunnel

            self.registry = GuestRegistry(
                expected_token=self.config.guest_token or self.config.host_token,
                on_topology_change=self.on_topology_change,
            )
            if self.on_registry_ready is not None:
                try:
                    self.on_registry_ready(self.registry)
                except Exception:
                    logger.exception("on_registry_ready hook failed")

            self.state = "host"
            self.current_upstream = ""
            logger.info("host election: promoted to active host (tunnel %s)", url)
            log.info(
                Category.CLUSTER_HOST_ELECTED,
                f"promoted to active host (tunnel {self.config.cluster_tunnel})",
                machine_id=self.config.machine_id, tunnel=self.config.cluster_tunnel, url=url,
            )
            # 给正在看的人刷新 sidebar。
            await self._fire_topology_change(None)

    async def _ensure_guest(self, upstream: str) -> None:
        async with self._transition_lock:
            await self._ensure_guest_locked(upstream)

    async def _ensure_guest_locked(self, upstream: str) -> None:
        if self.state == "host":
            await self._teardown_host()

        if self.client is None:
            machine_id = self.config.machine_id or self.config.node_id or "guest"
            self.client = GuestClient(
                host_url="",
                host_token=self.config.host_token,
                machine_id=machine_id,
                local_web_port=self.config.web_port or 9292,
                local_web_token=self.config.web_token or "",
                tunnel_name=self.config.cluster_tunnel,
                bot_provider=self.bot_provider or (lambda: []),
            )
            self.client.start()
            if self.on_guest_client_ready is not None:
                try:
                    self.on_guest_client_ready(self.client)
                except Exception:
                    logger.exception("on_guest_client_ready hook failed")
            logger.info(
                "host election: guest mode — dialing tunnel '%s' (upstream=%s)",
                self.config.cluster_tunnel, upstream or "?",
            )
        self.state = "guest"
        self.current_upstream = upstream

    async def _become_guest(self, upstream: str) -> None:
        async with self._transition_lock:
            await self._teardown_host()
            await self._ensure_guest_locked(upstream)

    # ── teardown 辅助 ──

    async def _teardown_host(self) -> None:
        if self.registry is not None:
            try:
                await self.registry.close_all_sessions()
            except Exception as e:
                logger.warning("host election: close registry sessions failed: %s", e)
            self.registry = None

        if self.tunnel is not None:
            try:
                await self.tunnel.stop()
            except Exception as e:
                logger.warning("host election: stop cluster tunnel failed: %s", e)
            self.tunnel = None

    async def _teardown_guest(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop()
            except Exception as e:
                logger.warning("host election: stop guest client failed: %s", e)
            self.client = None

    async def _teardown_all(self) -> None:
        await self._teardown_host()
        await self._teardown_guest()
