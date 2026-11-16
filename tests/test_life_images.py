"""聊天配图的来源、复用、开关与主动发送结算。"""
import json
from unittest.mock import AsyncMock
import pytest
from test_plugin import plugin
from test_diary_images import service
from maiweave.core.models import ExperienceInput


def setup(p, monkeypatch, session="chat-a"):
    service(p, monkeypatch)
    p.config.diary.chat_images_enabled = True
    now = p.now()
    event = p.engine.record_experience(ExperienceInput(summary="折好一个三角书角书签", source="simulated",
        occurred_at=now.isoformat(), evidence="完成折纸", details={"外形": "三角形"}), session, now)
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "书签", "image_prompt": "木桌上的三角书角书签"})}
    return now, event


async def test_send_reuses_image_and_checks_owner(plugin, monkeypatch):
    p = plugin
    now, event = setup(p, monkeypatch)
    await p.show_life("send", event["id"], stream_id="chat-a")
    import asyncio
    await asyncio.gather(*list(p.jobs))
    first = p.store.get("experience_image", event["id"])["image_file"]
    p.config.diary.image_service_enabled = False
    await p.show_life("send", event["id"], stream_id="chat-a")
    assert p.store.get("experience_image", event["id"])["image_file"] == first
    assert len([r for r in p.store.all("model_usage") if r["feature"] == "生活图片"]) == 1
    assert p.ctx.send.image.await_count == 2
    assert len(p.life_images.context("chat-a")) == 2
    assert not p.life_images.context("other")
    with pytest.raises(ValueError, match="不属于"):
        await p.show_life("send", event["id"], stream_id="other")
    p.config.diary.chat_images_enabled = False
    with pytest.raises(ValueError, match="关闭"):
        await p.show_life("send", event["id"], stream_id="chat-a")


@pytest.mark.parametrize("mode", ["自然交流", "主动表达"])
async def test_share_image_waits_for_text_and_counts_once(plugin, monkeypatch, mode):
    p = plugin
    now, event = setup(p, monkeypatch)
    row = {"id": "share", "session": "chat-a", "created": now.isoformat(), "mode": mode,
           "outcome": "MaiBot 已受理", "experience_id": event["id"], "image_requested": True}
    with p.store.db: p.store.put("share_receipt", row)
    await p.living.share_images(now)
    p.ctx.send.image.assert_not_awaited()
    row.update(outcome="实际已发送", sent_at=now.isoformat())
    with p.store.db: p.store.put("share_receipt", row)
    await p.living.share_images(now)
    await p.living.share_images(now)
    assert p.ctx.send.image.await_count == 1
    assert len(p.living.receipts.sent_times("chat-a", now.tzinfo)) == 1
    assert p.store.get("share_receipt", "share")["image_delivery"] == "已发送"


async def test_image_send_failure_preserves_reusable_picture(plugin, monkeypatch):
    p = plugin
    now, event = setup(p, monkeypatch)
    p.ctx.send.image.return_value = {"sent": False, "error": "连接断开"}
    with pytest.raises(RuntimeError, match="连接断开"):
        await p.life_images.send(event["id"], "chat-a", now)
    assert p.store.get("experience_image", event["id"])["image_file"]
    assert p.store.all("image_delivery")[-1]["status"] == "发送失败"
    assert p.life_images.context("chat-a") == []
