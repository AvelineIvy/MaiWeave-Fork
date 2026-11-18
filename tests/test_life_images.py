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


async def test_requested_failure_notifies_original_chat_and_can_retry(plugin, monkeypatch):
    import asyncio
    p = plugin
    now, event = setup(p, monkeypatch)
    request = p.life_images.images.request
    def fail(prompt):
        raise TimeoutError("生图超时")
    monkeypatch.setattr(p.life_images.images, "request", fail)
    p.ctx.send.text.return_value = {"sent": True}
    job = await p.show_life("send", event["id"], stream_id="chat-a")
    await asyncio.gather(*list(p.jobs))
    assert p.store.get("job", job["task_id"])["status"] == "失败"
    assert p.store.get("experience_image", event["id"])["image_status"] == "生成失败"
    assert p.ctx.send.text.await_args.args[1] == "chat-a"
    assert p.ctx.send.text.await_args.kwargs["storage_message"] is True
    p.ctx.send.image.assert_not_awaited()
    monkeypatch.setattr(p.life_images.images, "request", request)
    await p.show_life("send", event["id"], stream_id="chat-a")
    await asyncio.gather(*list(p.jobs))
    assert p.ctx.send.image.await_count == 1
    assert p.ctx.send.text.await_count == 1
    assert p.life_images.context("chat-a")


async def test_corrected_experience_regenerates_image(plugin, monkeypatch):
    import asyncio
    p = plugin
    now, event = setup(p, monkeypatch)
    await p.show_life("send", event["id"], stream_id="chat-a")
    await asyncio.gather(*list(p.jobs))
    original = p.store.get("experience_image", event["id"])["image_file"]
    p.engine.revise_experience(event["id"], {"外形": "心形"}, "折好一个心形书签", "更正折纸形状", "chat-a", now)
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "心形书签", "image_prompt": "木桌上的心形书签"})}
    result = await p.show_life("send", event["id"], stream_id="chat-a")
    await asyncio.gather(*list(p.jobs))
    assert "task_id" in result
    row = p.store.get("experience_image", event["id"])
    assert row["image_file"] != original
    assert "心形" in row["content"] and "心形" in row["image_prompt"]


async def test_image_tool_follows_switches_and_chat_ownership(plugin, monkeypatch):
    import copy
    p = plugin
    registered = next(c for c in p.get_components() if c["name"] == "maiweave_show_life")
    assert registered["metadata"]["visibility"] == "visible"
    now, event = setup(p, monkeypatch)
    tools = [{"type": "function", "function": {"name": "maiweave_show_life", "description": "生成或复用图片"}},
             {"type": "function", "function": {"name": "maiweave_query", "description": "查询生活"}}]
    original = copy.deepcopy(tools)

    async def definitions(session="chat-a"):
        result = await p.planner_context(session_id=session, items=[], tool_definitions=tools)
        return result["modified_kwargs"]["tool_definitions"]

    assert len(await definitions()) == 2
    p.config.diary.image_service_enabled = False
    assert len(await definitions()) == 1
    assert (await p.show_life("list", stream_id="chat-a"))["items"] == []
    p.config.diary.image_service_enabled = True
    await p.life_images.send(event["id"], "chat-a", now)
    p.config.diary.image_service_enabled = False
    p.ctx.llm.generate.reset_mock()
    available = await definitions()
    assert "已有" in available[0]["function"]["description"]
    assert "生成" not in available[0]["function"]["description"]
    assert len(await definitions("chat-b")) == 1
    result = await p.show_life("send", event["id"], stream_id="chat-a")
    assert result["status"] == "已发送"
    p.ctx.llm.generate.assert_not_awaited()
    p.config.diary.chat_images_enabled = False
    assert len(await definitions()) == 1
    assert (await p.show_life("list", stream_id="chat-a"))["enabled"] is False
    with pytest.raises(ValueError, match="聊天图片已关闭"):
        await p.show_life("send", event["id"], stream_id="chat-a")
    p.config.diary.chat_images_enabled = True
    p.config.life.enabled = False
    assert len(await definitions()) == 1
    assert tools == original


async def test_service_off_lists_existing_images_before_recent_limit(plugin, monkeypatch):
    from datetime import timedelta
    p = plugin
    now, event = setup(p, monkeypatch)
    await p.life_images.send(event["id"], "chat-a", now)
    for index in range(11):
        p.engine.record_experience(ExperienceInput(summary=f"新的生活片刻{index}", source="simulated",
            occurred_at=(now + timedelta(minutes=index + 1)).isoformat(), evidence="生活记录"), "chat-a", now + timedelta(minutes=index + 1))
    p.config.diary.image_service_enabled = False
    assert [row["id"] for row in p.life_images.candidates("chat-a")] == [event["id"]]
    event["summary"] = "修改后的书签"
    with p.store.db:
        p.store.put("experience", event)
    assert p.life_images.candidates("chat-a") == []
