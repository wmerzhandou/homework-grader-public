"""小工具：让"进度回调"这种可选参数既接受同步函数也接受协程函数。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

Notify = Callable[[str], object] | None


async def notify(callback: Notify, message: str) -> None:
    """调用进度回调；同步/异步都支持，回调里的异常不影响主流程。"""
    if callback is None:
        return
    try:
        result = callback(message)
        if inspect.isawaitable(result):
            await result  # type: ignore[misc]
    except Exception:  # noqa: BLE001 - 进度提示失败不该影响生成/渲染
        pass


__all__ = ["Notify", "notify", "Awaitable"]
