#!/usr/bin/env python3
"""白名单：只有 ALLOWED_USER_IDS 里的用户能用 bot。

- 检查直接用 event.sender_id，绝不用 get_sender()：后者要查 session 数据库解析实体，
  数据库一旦被锁就会失败/返回空，导致把自己的消息也当成陌生人拒绝。
- 拒绝时把 sender_id 记下来，不然下次再刷屏又查不到原因。
"""
import config


def is_allowed(uid):
    return uid in config.ALLOWED


async def cb_uid(event, log_tag=None):
    """回调按钮的白名单检查：有权限返回 sender_id，没权限弹提示并返回 None。"""
    uid = event.sender_id or 0
    if uid not in config.ALLOWED:
        if log_tag:
            config.log(f"白名单拒绝（{log_tag}）：sender_id={uid}")
        await event.answer("你没有权限用这个 bot。", alert=True)
        return None
    return uid
