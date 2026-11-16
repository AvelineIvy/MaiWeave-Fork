"""角色卡入镜、展示要求、图片复用与模型调用。"""
import asyncio
import json
import pytest
from test_plugin import plugin
from test_diary_images import service, PNG
from test_qzone import prepare
from test_life_images import setup


@pytest.mark.parametrize("enabled,visible", [(True, True), (True, False), (False, True)])
async def test_character_only_in_person_shots(plugin, monkeypatch, enabled, visible):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.diary.image_character_enabled = enabled
    p.config.diary.image_character_appearance = "黑色齐肩发，棕色眼睛"
    p.config.diary.image_character_clothing = "米白色针织衫"
    prompts = []
    monkeypatch.setattr(type(p.diary_publisher.images), "request", lambda self, prompt: (prompts.append(prompt) or PNG, {}))
    p.diary_publisher.images.put("diary_publication", {"id": day, "status": "草稿", "content": "麦麦在窗边折书签"})
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "折书签", "image_prompt": "窗边折书签", "character_visible": visible})}
    await p.diary_publisher.illustrate(day, now)
    assert ("黑色齐肩发" in prompts[-1]) == (enabled and visible)
    assert ("米白色针织衫" in prompts[-1]) == (enabled and visible)
    assert p.ctx.llm.generate.await_count == 1
    assert "黑色齐肩发" not in p.ctx.llm.generate.call_args.args[0]


async def test_shared_card_auto_space_uses_one_model_call(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.diary.image_character_enabled = True
    p.config.diary.image_enabled = True
    p.config.diary.image_character_appearance = "黑色齐肩发"
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "窗边折纸", "image_prompt": "麦麦在窗边折纸", "character_visible": True})}
    row = await p.diary_publisher.prepare(day, now)
    assert row["image_character_visible"] is True
    assert p.ctx.llm.generate.await_count == 1


async def test_framing_card_changes_reuse_and_owner(plugin, monkeypatch):
    p = plugin
    now, event = setup(p, monkeypatch)
    p.config.diary.image_character_enabled = True
    p.config.diary.image_character_appearance = "黑色齐肩发"
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "折纸", "image_prompt": "麦麦折书签", "character_visible": True})}
    job = await p.show_life("send", event["id"], framing="想看看你折纸的样子", stream_id="chat-a")
    await asyncio.gather(*list(p.jobs))
    assert p.store.get("job", job["task_id"])["status"] == "已完成"
    row = p.store.get("experience_image", event["id"])
    assert row["image_character_visible"] is True
    assert "想看看你折纸的样子" in p.ctx.llm.generate.call_args.args[0]
    assert len(p.ctx.llm.generate.call_args.args[0]) < 6000
    count = p.ctx.llm.generate.await_count
    p.config.diary.image_character_appearance = "新发型"
    await p.show_life("send", event["id"], framing="另一构图", stream_id="chat-a")
    assert p.store.get("experience_image", event["id"])["image_file"] == row["image_file"]
    assert p.ctx.llm.generate.await_count == count
    with pytest.raises(ValueError, match="不属于"):
        await p.show_life("send", event["id"], stream_id="other")
    assert "黑色齐肩发" not in str(p.life_images.context("chat-a"))


async def test_manual_prompt_and_fixed_test(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.diary.image_character_enabled = True
    p.config.diary.image_character_appearance = "黑色齐肩发"
    p.diary_publisher.images.put("diary_publication", {"id": day, "status": "草稿", "content": "麦麦折纸"})
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "折纸", "image_prompt": "模型改写描述", "character_visible": True})}
    row = await p.diary_publisher.illustrate(day, now, "手写的人物构图")
    assert row["image_prompt"] == "手写的人物构图"
    p.ctx.llm.generate.reset_mock()
    prompts = []
    monkeypatch.setattr(type(p.diary_publisher.images), "request", lambda self, prompt: (prompts.append(prompt) or PNG, {}))
    await p.ui_test_image()
    await asyncio.gather(*list(p.jobs))
    p.ctx.llm.generate.assert_not_awaited()
    assert "黑色齐肩发" not in prompts[-1]


async def test_enable_card_after_old_image_cleaned(plugin, monkeypatch):
    from maiweave.core.image_retention import ImageRetention
    from datetime import timedelta
    p = plugin
    now, event = setup(p, monkeypatch)
    await p.life_images.send(event["id"], "chat-a", now)
    row = p.store.get("experience_image", event["id"])
    assert "image_character_visible" not in row
    ImageRetention(p.store, p.config, p.ctx.paths.data_dir).clean(now + timedelta(days=100))
    p.config.diary.image_character_enabled = True
    p.config.diary.image_character_appearance = "黑色齐肩发"
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "折纸", "image_prompt": "麦麦折纸", "character_visible": True})}
    before = p.ctx.llm.generate.await_count
    await p.life_images.send(event["id"], "chat-a", now)
    assert p.ctx.llm.generate.await_count == before + 1
    assert p.store.get("experience_image", event["id"])["image_character_visible"] is True


async def test_generate_card_uses_native_personality_and_selected_model(plugin):
    p = plugin
    p.config.models.image_prompt = "replyer"
    p.ctx.config.get.return_value = {"personality": "黑发，喜欢散步"}
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"appearance":"黑色齐肩发", "clothing":"米白色针织衫"})}
    await p.on_config_widget_action("generate_character")
    await asyncio.gather(*list(p.jobs))
    p.ctx.config.get.assert_awaited_with("personality")
    assert "喜欢散步" in p.ctx.llm.generate.call_args.args[0]
    assert p.ctx.llm.generate.call_args.kwargs["task_name"] == "replyer"
    states = await p.get_config_widget_state()
    assert "黑色齐肩发" in states["character_draft"].text
    assert states["character_draft"].tone == "success"
    assert not states["generate_character"].disabled
    assert p.config.diary.image_character_appearance == ""
    assert p.store.all("model_usage")[-1]["feature"] == "配图提示词"


@pytest.mark.parametrize("visible", [True, False])
async def test_reference_only_for_character(plugin, monkeypatch, visible):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    cfg = p.config.diary
    cfg.image_character_enabled = True
    cfg.image_character_reference_enabled = True
    cfg.image_character_reference_url = "https://example.com/character.png"
    captured = []
    monkeypatch.setattr(type(p.diary_publisher.images), "request", lambda self, prompt, reference="": (captured.append((prompt, reference)) or PNG, {}))
    p.diary_publisher.images.put("diary_publication", {"id":day, "status":"草稿", "content":"窗边折纸"})
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success":True, "response":json.dumps({"content":"折纸", "image_prompt":"窗边折纸", "character_visible":visible})}
    await p.diary_publisher.illustrate(day, now)
    assert captured[-1][1] == (cfg.image_character_reference_url if visible else "")
    assert p.store.all("model_usage")[-1]["task"] == ("images/edits" if visible else "images/generations")


async def test_reference_request_multipart_and_unsupported(plugin, monkeypatch):
    import io
    from urllib.error import HTTPError
    import maiweave.integrations.diary_images as module
    p = plugin
    requests = []
    def open_url(request, timeout):
        if isinstance(request, str):
            return io.BytesIO(PNG)
        requests.append(request)
        return io.BytesIO(json.dumps({"data":[{"b64_json": __import__("base64").b64encode(PNG).decode()}]}).encode())
    monkeypatch.setattr(module, "urlopen", open_url)
    images = p.diary_publisher.images
    data, _ = images.request("人物在折纸", "https://example.com/character.png")
    assert data == PNG
    req = requests[0]
    assert req.full_url.endswith("/images/edits")
    assert b'name="image"; filename="character.png"' in req.data
    assert PNG[:8] in req.data
    assert "人物在折纸".encode() in req.data
    def unsupported(request, timeout):
        if isinstance(request, str): return io.BytesIO(PNG)
        raise HTTPError(request.full_url, 404, "Not found", {}, io.BytesIO(b""))
    monkeypatch.setattr(module, "urlopen", unsupported)
    with pytest.raises(RuntimeError, match="未提供参考图接口"):
        images.request("人物", "https://example.com/character.png")
