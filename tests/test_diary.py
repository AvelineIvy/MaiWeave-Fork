"""日记日程窗口、写作和失败重试。"""
import json
from datetime import timedelta
import pytest
from maiweave.core.models import PlanInput
from maiweave.life.diary import writing_material
from test_plugin import plugin


def history(plugin, at):
    with plugin.store.db:
        plugin.store.put("state_history", {"id": at.isoformat(), "at": at.isoformat(), "energy": 60, "mood": 10})


def test_busy_day_includes_evening_and_hides_unfinished_shift_result():
    plans = [{"title": f"当天活动{i}", "status": "completed", "motivation": "背景" * 250}
             for i in range(20)]
    plans.append({"title": "晚班理货", "status": "active", "shift_scene": {
        "task": "清点新书", "detail": "刚打开箱子", "resolution": "稍后才清点完毕"}})
    material = writing_material({"活动": plans})
    assert len(material["活动"]) == 21
    assert material["活动"][-1] == {"title": "晚班理货", "status": "active", "手头的事": "清点新书", "进展": "刚打开箱子"}


async def test_model_arranges_diary_and_only_writes_in_window(plugin):
    now = plugin.now().replace(hour=14, minute=0, second=0, microsecond=0) + timedelta(days=1)
    history(plugin, now - timedelta(minutes=1))
    start = now + timedelta(hours=1)
    plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"start": start.isoformat(), "end": (start+timedelta(minutes=15)).isoformat(), "reason": "空闲时回顾"})}
    await plugin.diary.arrange(now, plugin.spatial)
    await plugin.diary.arrange(now, plugin.spatial)
    plan = plugin.store.all("plan")[0]
    assert plan["kind"] == "diary"
    await plugin.diary.process(now)
    assert plugin.store.all("diary") == []
    plugin.engine.advance(start)
    plugin.ctx.llm.generate.return_value = {"success": True, "response": "今天过得很平静。"}
    await plugin.diary.process(start)
    await plugin.diary.process(start + timedelta(minutes=1))
    assert plugin.store.all("diary")[0]["plan_id"] == plan["id"]
    assert plugin.ctx.llm.generate.await_count == 2
    assert plugin.engine.current(start)["kind"] == "diary"


async def test_manual_diary_occupies_real_schedule(plugin):
    now=plugin.now();history(plugin,now-timedelta(minutes=1))
    plan=await plugin.ui_write_diary()
    assert plan["kind"]=="diary"
    assert plugin.engine.time(plan["end"])-plugin.engine.time(plan["start"])==timedelta(minutes=15)


async def test_diary_arrangement_only_shows_relevant_busy_windows(plugin):
    now = plugin.now().replace(hour=12, minute=0, second=0, microsecond=0)
    for days, title in [(1, "明天下午的排班"), (5, "下周的排班")]:
        start = now + timedelta(days=days, hours=3)
        plugin.engine.create_plan(PlanInput(title=title, kind="work", start=start.isoformat(),
            end=(start + timedelta(hours=1)).isoformat(), motivation="班次内部细节"), "", now)
    start = now + timedelta(hours=2)
    plugin.ctx.llm.generate.return_value = {"success": True, "response": json.dumps(
        {"start": start.isoformat(), "end": (start + timedelta(minutes=15)).isoformat(), "reason": "午后空闲"})}
    await plugin.diary.arrange(now, plugin.spatial)
    prompt = plugin.ctx.llm.generate.call_args.args[0]
    assert "明天下午的排班" in prompt
    assert "下周的排班" not in prompt and "班次内部细节" not in prompt


async def test_diary_model_retries_empty_output(plugin):
    now=plugin.now();history(plugin,now-timedelta(minutes=1))
    plugin.ctx.llm.generate.side_effect=[{"success":True,"response":""},{"success":True,"response":"写下今天的心情。"}]
    result=await plugin.diary.write("",now)
    assert result["status"]=="已生成"
    assert len(plugin.store.all("model_usage"))==2


async def test_diary_empty_day(plugin):
    with pytest.raises(ValueError,match="生活记录"):
        await plugin.diary.write((plugin.now().date()-timedelta(days=3)).isoformat(),plugin.now())


async def test_bad_source_ids_stop_until_new_writing_slot(plugin):
    p = plugin
    now = p.now()
    day = now.date().isoformat()
    history(p, now - timedelta(minutes=1))
    p.ctx.chat.get_all_streams.return_value = [{"stream_id": "example"}]
    p.ctx.message.get_by_time_in_chat.return_value = [
        {"message_id": "sample-message", "processed_plain_text": "今天做了书签"}]
    p.ctx.llm.generate.side_effect = None
    def response(key):
        return {"success": True, "response": json.dumps({"moments": [
            {"source_ids": [key], "summary": "聊起书签", "public_summary": ""}]})}
    p.ctx.llm.generate.return_value = response("sample-message")
    p.engine.create_plan(PlanInput(title="写日记", kind="diary", start=now.isoformat(),
        end=(now + timedelta(minutes=15)).isoformat(), diary_day=day), "", now)
    p.engine.advance(now)
    with pytest.raises(ValueError, match="chat-1"):
        await p.diary.process(now)
    await p.diary.process(now + timedelta(minutes=6))
    assert p.ctx.llm.generate.await_count == 3
    assert "请修正" in p.ctx.llm.generate.call_args.args[0]
    later = now + timedelta(minutes=20)
    p.engine.create_plan(PlanInput(title="再写日记", kind="diary", start=later.isoformat(),
        end=(later + timedelta(minutes=15)).isoformat(), diary_day=day), "", later)
    p.engine.advance(later)
    p.ctx.llm.generate.side_effect = [response("chat-1"), {"success": True, "response": "今天聊起了书签。"}]
    await p.diary.process(later)
    assert p.store.get("diary", day)["content"] == "今天聊起了书签。"


async def test_missed_diary_window_is_written_after_restart(plugin):
    now = plugin.now()
    history(plugin, now - timedelta(minutes=1))
    plan = plugin.engine.create_plan(PlanInput(title="写日记", kind="diary",
        start=(now + timedelta(minutes=5)).isoformat(), end=(now + timedelta(minutes=20)).isoformat(),
        diary_day=now.date().isoformat()), "", now)
    await plugin.on_unload()
    await plugin.on_load()
    later = now + timedelta(minutes=30)
    plugin.engine.advance(later)
    assert plugin.store.get("plan", plan["id"])["status"] == "completed"
    plugin.ctx.llm.generate.return_value = {"success": True, "response": "今天整理了书桌，留下一点安静的时间。"}
    await plugin.diary.process(later)
    diary = plugin.store.find("diary", now.date().isoformat())
    assert diary and diary["status"] == "已生成"
    assert diary["plan_id"] == plan["id"]
    await plugin.diary.process(later + timedelta(minutes=1))
    assert plugin.ctx.llm.generate.await_count == 1


async def test_diary_stays_text_when_space_images_enabled(plugin, tmp_path):
    from unittest.mock import AsyncMock
    now = plugin.now()
    history(plugin, now - timedelta(minutes=1))
    plugin.config.diary.image_service_enabled = True
    plugin.config.diary.image_enabled = True
    old_file = tmp_path / "old-diary.png"
    old_file.write_bytes(b"old")
    with plugin.store.db:
        plugin.store.put("diary", {"id": now.date().isoformat(), "status": "已生成", "content": "旧日记", "image_file": str(old_file)})
    plugin.diary_publisher.images.illustrate = AsyncMock()
    plugin.ctx.llm.generate.side_effect = None
    plugin.ctx.llm.generate.return_value = {"success": True, "response": "今天过得很平静。"}
    row = await plugin.diary.write("", now)
    assert row["content"] == "今天过得很平静。"
    plugin.diary_publisher.images.illustrate.assert_not_awaited()
    assert old_file.exists()
    assert row["image_file"] == str(old_file)
