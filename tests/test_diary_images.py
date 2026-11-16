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
    p.config.diary.image_api_key = 'test-key'
    monkeypatch.setattr(type(p.diary.images), 'request', lambda self, prompt: (PNG, {'input_tokens': 20, 'output_tokens': 40, 'total_tokens': 60}))


async def test_private_and_public_images_keep_separate_files_and_publish(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    await p.diary_publisher.prepare(day, now)
    private = await p.diary.images.illustrate('diary', day, now, '私人日记中的窗边')
    public = await p.diary_publisher.illustrate(day, now, '空间随笔中的茶杯')
    assert private['image_file'] != public['image_file']
    assert p.diary.images.preview(private).startswith('data:image/jpeg;base64,')
    assert p.store.all('model_usage')[-1]['total_tokens'] == 60
    await p.diary_publisher.publish(day, now)
    assert p.ctx.call_capability.call_args.kwargs['args']['params']['images'][0].startswith('base64://')
    assert p.store.get('diary', day)['image_prompt'] == '私人日记中的窗边'


async def test_failed_redraw_keeps_image_and_text(plugin, monkeypatch):
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    row = await p.diary.images.illustrate('diary', day, now, '台灯')
    original = row['image_file']
    def fail(self, prompt):
        raise TimeoutError('生图超时')
    monkeypatch.setattr(type(p.diary.images), 'request', fail)
    with pytest.raises(TimeoutError):
        await p.diary.images.illustrate('diary', day, now, '书桌')
    row = p.store.get('diary', day)
    assert row['image_file'] == original and row['status'] == '已生成'
    assert row['image_status'] == '生成失败'
    assert (Path(p.ctx.paths.data_dir) / original).exists()
    p.diary.images.remove('diary', day)
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
    first = await p.diary.images.illustrate('diary', day, now, '窗边')
    original = Path(p.ctx.paths.data_dir) / first['image_file']
    await p.diary_publisher.illustrate(day, now, '茶杯')
    assert not original.exists()
    assert 'image_file' not in p.store.get('diary', day)
    assert p.store.get('diary', day)['status'] == '已生成'
    assert p.store.get('diary_publication', day)['image_file']
