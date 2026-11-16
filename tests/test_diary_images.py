"""配图保存、失败重画、公开素材和用量记录。"""
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from test_plugin import plugin
from test_qzone import prepare

import io
from PIL import Image
buffer = io.BytesIO()
Image.new("RGB", (16, 16), "white").save(buffer, format="PNG")
PNG = buffer.getvalue()


def service(p, monkeypatch):
    p.config.diary.image_service_enabled = True
    p.config.diary.image_api_key = 'test-key'
    monkeypatch.setattr(type(p.diary_publisher.images), 'request', lambda self, prompt: (PNG, {'input_tokens': 20, 'output_tokens': 40, 'total_tokens': 60}))


def legacy_image(p, day):
    """准备升级前保存的日记图片。"""
    path = Path(p.ctx.paths.data_dir) / "diary_images" / "legacy.png"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(PNG)
    row = p.store.get('diary', day)
    row.update(image_file='diary_images/legacy.png', image_prompt='旧日记配图', image_status='已生成')
    with p.store.db: p.store.put('diary', row)
    return row


async def test_public_image_and_publish_keep_legacy_diary_file(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    private = legacy_image(p, day)
    await p.diary_publisher.prepare(day, now)
    public = await p.diary_publisher.illustrate(day, now, '空间随笔中的茶杯')
    assert private['image_file'] != public['image_file']
    assert p.diary_publisher.images.preview(public).startswith('data:image/jpeg;base64,')
    assert p.store.all('model_usage')[-1]['feature'] == '空间配图'
    await p.diary_publisher.publish(day, now)
    assert p.ctx.call_capability.call_args.kwargs['args']['params']['images'][0].startswith('base64://')
    assert p.store.get('diary', day)['image_prompt'] == '旧日记配图'
    assert (Path(p.ctx.paths.data_dir) / private['image_file']).exists()


async def test_failed_redraw_keeps_image_and_text(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    await p.diary_publisher.prepare(day, now)
    row = await p.diary_publisher.images.illustrate('diary_publication', day, now, '台灯')
    original = row['image_file']
    def fail(self, prompt):
        raise TimeoutError('生图超时')
    monkeypatch.setattr(type(p.diary_publisher.images), 'request', fail)
    with pytest.raises(TimeoutError):
        await p.diary_publisher.images.illustrate('diary_publication', day, now, '书桌')
    row = p.store.get('diary_publication', day)
    assert row['image_file'] == original and row['status'] == '草稿'
    assert row['image_status'] == '生成失败'
    assert (Path(p.ctx.paths.data_dir) / original).exists()
    p.diary_publisher.images.remove('diary_publication', day)
    assert not (Path(p.ctx.paths.data_dir) / original).exists()


async def test_auto_image_reuses_writing_request_and_public_summary(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.diary.image_enabled = True
    diary = p.store.get('diary', day)
    diary['material']['聊天片刻'] = [{'summary': 'PRIVATE_SECRET', 'session_id': 'private', 'public_summary': '收到朋友鼓励，很开心'}]
    p.store.put('diary', diary)
    p.ctx.llm.generate.return_value = {'success': True, 'response': json.dumps({'content': '今天收到了一点鼓励。', 'image_prompt': '暖色灯光下的小桌'})}
    result = await p.diary_publisher.prepare(day, now)
    assert p.ctx.llm.generate.await_count == 1
    prompt = p.ctx.llm.generate.call_args.args[0]
    assert 'PRIVATE_SECRET' not in prompt and '收到朋友鼓励' in prompt
    assert result['status'] == '草稿' and result['image_status'] == '已生成'


async def test_chat_moments_keep_stream_ownership_and_budget(plugin):
    p = plugin
    now = p.now()
    p.ctx.chat.get_all_streams.return_value = [{'stream_id': 'a'}, {'stream_id': 'b'}]
    p.ctx.message.get_by_time_in_chat.return_value = [
        {'message_id': str(n), 'processed_plain_text': '当天聊天' * 150, 'account_id': 'bot', 'message_info': {'user_info': {'user_id': 'user', 'user_nickname': '朋友'}}} for n in range(40)]
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {'success': True, 'response': json.dumps({'moments': [{'source_ids': ['chat-1'], 'summary': '聊了近况', 'public_summary': ''}]})}
    rows = await p.diary.chat_moments(now.date(), now, {'经历': []})
    assert rows[0]['session_id'] == 'a'
    assert len(p.ctx.llm.generate.call_args.args[0]) < 12000
    p.ctx.llm.generate.return_value = {'success': True, 'response': json.dumps({'moments': [{'source_ids': ['chat-1', 'chat-2'], 'summary': '混合', 'public_summary': ''}]})}
    with pytest.raises(ValueError, match='同一聊天流'):
        await p.diary.chat_moments(now.date(), now, {'经历': []})


async def test_generation_applies_image_limit_immediately(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.retention.image_count = 1
    await p.diary_publisher.prepare(day, now)
    first = legacy_image(p, day)
    original = Path(p.ctx.paths.data_dir) / first['image_file']
    await p.diary_publisher.illustrate(day, now, '茶杯')
    assert not original.exists()
    assert 'image_file' not in p.store.get('diary', day)
    assert p.store.get('diary', day)['status'] == '已生成'
    assert p.store.get('diary_publication', day)['image_file']


async def test_webui_space_image_actions_leave_diary_untouched(plugin, monkeypatch):
    import asyncio
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    private = legacy_image(p, day)
    await p.diary_publisher.prepare(day, now)
    job = await p.ui_diary_publish_action('image', day, image_prompt='桌上的茶杯')
    await asyncio.gather(*list(p.jobs))
    assert p.store.get('job', job['task_id'])['status'] == '已完成'
    assert (await p.ui_diary_publication(day))['image']
    await p.ui_diary_publish_action('remove_image', day)
    assert not (await p.ui_diary_publication(day))['image']
    assert p.store.get('diary', day) == private
    assert (Path(p.ctx.paths.data_dir) / private['image_file']).exists()


async def test_image_config_test_replaces_preview_and_reports_failure(plugin, monkeypatch):
    import asyncio
    p = plugin
    service(p, monkeypatch)
    for _ in range(2):
        job = await p.ui_test_image()
        await asyncio.gather(*list(p.jobs))
        assert p.store.get('job', job['task_id'])['status'] == '已完成'
    result = await p.ui_image_test()
    assert result['status'] == '已生成'
    assert result['seconds'] is not None
    assert len(list((Path(p.ctx.paths.data_dir) / 'diary_images').iterdir())) == 1
    assert not p.store.all('diary_publication') and not p.store.all('experience_image')
    p.ctx.send.image.assert_not_awaited()
    def fail(self, prompt):
        raise RuntimeError('生图服务返回 HTTP 401')
    monkeypatch.setattr(type(p.diary_publisher.images), 'request', fail)
    await p.ui_test_image()
    await asyncio.gather(*list(p.jobs))
    result = await p.ui_image_test()
    assert result['status'] == '生成失败' and '401' in result['error']
    assert all(row['feature'] == '生图测试' for row in p.store.all('model_usage'))
    p.retention.images.clean(p.now() + timedelta(days=91))
    assert not p.store.get('image_test', 'latest').get('image_file')


async def test_config_widget_uses_image_test_job(plugin, monkeypatch):
    import asyncio
    from maibot_sdk.config_widgets import ConfigWidgetSnapshot
    p = plugin
    states = await p.get_config_widget_state()
    assert states['test_image'].disabled
    assert not states['image_test_result'].visible
    service(p, monkeypatch)
    await p.on_config_widget_action('test_image')
    p._image_test_started -= 12
    running = await p.get_config_widget_state()
    assert running['test_image'].disabled
    assert p.now().strftime('%Y-%m-%d %H:%M:%S') in running['image_test_result'].text
    assert '已用时 12 秒' in running['image_test_result'].text
    assert '请求超时 180 秒' in running['image_test_result'].text
    assert '链接下载超时 60 秒' in running['image_test_result'].text
    await asyncio.gather(*list(p.jobs))
    states = await p.get_config_widget_state()
    ConfigWidgetSnapshot(widgets=p.config_widgets, states=states)
    assert not states['test_image'].disabled
    assert states['image_test_result'].tone == 'success'
    assert '已生成' in states['image_test_result'].text
    assert '总用时' in states['image_test_result'].text
    assert states['image_test_result'].visible
    p._image_test_feedback_until = 0
    assert not (await p.get_config_widget_state())['image_test_result'].visible


@pytest.mark.parametrize("category,expected", [("diary_publication", "空间风格"), ("image_test", "空间风格"), ("experience_image", "聊天风格")])
async def test_image_style_follows_use(plugin, monkeypatch, category, expected):
    p = plugin
    service(p, monkeypatch)
    p.config.diary.image_style = "空间风格"
    p.config.diary.chat_image_style = "聊天风格"
    prompts = []
    def request(self, prompt):
        prompts.append(prompt)
        return PNG, {}
    images = p.diary_publisher.images
    monkeypatch.setattr(type(images), 'request', request)
    images.put(category, {"id": "style-test", "status": "草稿", "content": "窗边茶杯"})
    await images.illustrate(category, "style-test", p.now(), "窗边茶杯")
    assert prompts == ["窗边茶杯\n画面风格：" + expected + "\n负面提示词（画面中避免）：" + p.config.diary.image_negative_prompt]


@pytest.mark.parametrize("category", ["experience_image", "diary_publication"])
async def test_missing_image_prompt_uses_selected_text_model(plugin, monkeypatch, category):
    p = plugin
    service(p, monkeypatch)
    p.config.models.image_prompt = "prompt-task"
    p.config.models.diary = "diary-task"
    p.config.models.qzone = "qzone-task"
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "茶杯", "image_prompt": "窗边的茶杯"})}
    images = p.diary_publisher.images
    images.put(category, {"id": "prompt-test", "status": "草稿", "content": "窗边的茶杯"})
    await images.illustrate(category, "prompt-test", p.now())
    assert p.ctx.llm.generate.call_args.kwargs["task_name"] == "prompt-task"
    assert any(row["feature"] == "配图提示词" for row in p.store.all("model_usage"))
    p.ctx.llm.generate.reset_mock()
    await images.illustrate(category, "prompt-test", p.now())
    p.ctx.llm.generate.assert_not_awaited()
    await images.illustrate(category, "prompt-test", p.now(), "用户填写的提示词")
    p.ctx.llm.generate.assert_not_awaited()
    await p.ui_test_image()
    import asyncio
    await asyncio.gather(*list(p.jobs))
    p.ctx.llm.generate.assert_not_awaited()


async def test_auto_space_prompt_stays_with_space_writer(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.config.diary.image_enabled = True
    p.config.models.qzone = "space-task"
    p.config.models.image_prompt = "prompt-task"
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({"content": "午后读书", "image_prompt": "窗边的书"}), "total_tokens": 37}
    await p.diary_publisher.prepare(day, now)
    assert p.ctx.llm.generate.await_count == 1
    assert p.ctx.llm.generate.call_args.kwargs["task_name"] == "space-task"
    rows = p.store.all("model_usage")
    assert [r["feature"] for r in rows] == ["空间文案", "空间配图"]
    usage = await p.ui_usage(feature="空间文案")
    assert usage["totals"]["calls"] == 1 and usage["totals"]["total"] == 37
