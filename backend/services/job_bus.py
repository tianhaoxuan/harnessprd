"""同一进程内的事件广播：后台 runner 产出事件，推给**当前正在订阅**的 SSE 连接。

## 为什么需要它

Generation Job 的执行者是一个后台 `asyncio.Task`，而接收者是一条（或零条、
或多条）SSE 连接。两者生命周期**刻意解耦**：

- 没有订阅者时，`publish()` 什么也不做（任务照跑，草稿照落库）；
- 订阅者断开时只 `unsubscribe()`，**不停任务** —— 这正是需求 §"客户端断开 SSE 后，
  后台 runner 仍继续执行"的落点；
- 中途接进来的订阅者不会漏掉历史：它先由 `GET /jobs/{id}` 或 SSE 首帧的 `snapshot`
  拿到"到目前为止的全文"，再从当前时刻往后听增量。

## 为什么不是 asyncio.Event / 直接回调

用**每订阅者一条队列**而不是共享一个事件列表：慢订阅者（浏览器在后台被节流）
只会塞满自己的队列，不会拖住 runner，也不会把别人挤掉。
代价是每个订阅者要有独立的"我读到哪了"——那就是队列本身。

队列有上限：真出现"订阅者永远不读"（SSE 连接半死但不关闭）时，
队列满了会**丢弃最旧的事件**而不是无限涨内存。丢的后果是那段增量缺失，
但 `snapshot` / 最终 `done` 里的全文仍然是对的（前端以 done 的全文为准）。

## 单进程的边界

`_subscribers` 是**进程内**字典，所以：多实例部署时，创建任务的实例与订阅的实例
若不是同一个，订阅者收不到事件（只会看到 snapshot 与最终的 done）。
按需求"本次不做 Celery / Redis / 独立 Worker"，这里就是答案；
将来换 Redis pub/sub 时，改动点只在 `publish` / `subscribe` 两个函数里。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_QUEUE_MAXSIZE = 512
"""单个订阅者的队列上限。按事件数（不是字节）算：

一次 PRD 生成实测几百到一两千个 chunk，512 足够让"偶尔卡一下"的客户端不掉队，
又不至于让一个半死的连接把内存撑起来。
"""

_subscribers: dict[str, list[asyncio.Queue]] = {}
"""job_id → 该任务的订阅者队列列表。**只在事件循环线程里访问**，不加锁。"""


class JobSubscription:
    """一个订阅的句柄。用 `async with` 拿到队列，退出时自动退订。

    为什么要有这个类：`subscribe` / `unsubscribe` 必须**成对**，而 SSE 生成器可能
    在任意位置被关闭（客户端断开、异常、正常收尾）。写成 `try/finally` 容易漏一处，
    句柄 + 上下文管理器让"漏退订"变成写不出来。
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._subscribed = False

    def __enter__(self) -> JobSubscription:
        subscribe(self.job_id, self.queue)
        self._subscribed = True
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._subscribed:
            unsubscribe(self.job_id, self.queue)
            self._subscribed = False


def subscription(job_id: str) -> JobSubscription:
    """建一个订阅句柄（用 `with subscription(job_id) as sub:` 使用）。"""
    return JobSubscription(job_id)


def subscribe(job_id: str, queue: asyncio.Queue) -> None:
    """登记一条队列。同一个队列重复登记会被忽略（幂等，便于重连逻辑写简单些）。"""
    queues = _subscribers.setdefault(job_id, [])
    if queue not in queues:
        queues.append(queue)


def unsubscribe(job_id: str, queue: asyncio.Queue) -> None:
    """退订。**只影响这一条连接，绝不动后台任务**（这是需求里最重要的一条）。

    列表空了就把 job 的键删掉：字典不能随"历史上生成过多少个任务"一直长大。
    """
    queues = _subscribers.get(job_id)
    if not queues:
        return
    if queue in queues:
        queues.remove(queue)
    if not queues:
        _subscribers.pop(job_id, None)


def subscriber_count(job_id: str) -> int:
    """当前订阅者数量（自检与排查用：验证"断开后任务仍在跑、订阅者归零"）。"""
    return len(_subscribers.get(job_id, ()))


async def publish(job_id: str, event: dict[str, Any]) -> None:
    """把一个事件推给该任务的所有订阅者（`async` 只为调用点统一，内部是同步投递）。

    投递本身是纯内存操作，不需要 await。所以真正的实现放在 `publish_nowait()` 里 ——
    有些场景**不能 await**：取消路径上的收尾、以及自检脚本要模拟
    "读状态的那一瞬间任务刚好跑完"（见 `scripts/job_check.py` 的竞态用例）。
    两处各写一遍投递逻辑必然分叉（队列满时的丢弃策略就是最容易被抄漏的那一段）。

    - **没有订阅者时是空操作**（无人观看也要继续跑，事件只是不必留）；
    - 队列满时丢**最旧**的一条再放新的：宁可丢一段中间增量，也不要让 runner 阻塞。
      丢的事件会记一条 warning，否则"少了一段文字"会被当成模型的问题。
    """
    publish_nowait(job_id, event)


def publish_nowait(job_id: str, event: dict[str, Any]) -> None:
    """`publish` 的同步版本（**唯一实现**）。"""
    queues = _subscribers.get(job_id)
    if not queues:
        return
    for queue in list(queues):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            dropped = None
            try:
                dropped = queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 与上一步之间被读走了
                pass
            logger.warning(
                "订阅者队列已满（job=%s），丢弃一条旧事件以放下新事件（丢弃的类型：%s）",
                job_id,
                (dropped or {}).get("type", "未知"),
            )
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - 极小窗口，放弃这一条
                logger.warning("订阅者队列仍然满（job=%s），这一条事件也丢了", job_id)


def reset_for_tests() -> None:
    """清空所有订阅（自检脚本用）。**生产代码不该调它** —— 那会无声地掐断在跑的订阅。"""
    _subscribers.clear()


STREAM_END_TYPE = "__end__"
"""流结束哨兵的事件类型（**只在进程内的队列里流转，不会变成 SSE 帧**）。

为什么要有哨兵，而不是让 SSE 那侧"看到库里的状态是终态就收尾"：终态是在事件**之前**
写库的（先落库、再广播），订阅者若按状态判断，会在还没读到 `done` / `run_summary`
时就结束 ---- 于是最常见的现象是"页面停在没有 done 的流上"。
哨兵由 runner 在**所有事件都推完之后**才放，顺序因此是确定的。
"""


def publish_stream_end(job_id: str) -> None:
    """广播"这个任务的流到此为止"。**同步函数**（`publish` 是 async，而取消路径里不能 await）。

    没有订阅者时是空操作；有订阅者时每条队列各放一个哨兵。
    """
    queues = _subscribers.get(job_id)
    if not queues:
        return
    for queue in list(queues):
        try:
            queue.put_nowait({"type": STREAM_END_TYPE})
        except asyncio.QueueFull:  # pragma: no cover - 队列满：丢最旧的再放哨兵
            try:
                queue.get_nowait()
                queue.put_nowait({"type": STREAM_END_TYPE})
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover
                logger.warning("订阅者队列满，结束哨兵没送出去（job=%s）", job_id)
