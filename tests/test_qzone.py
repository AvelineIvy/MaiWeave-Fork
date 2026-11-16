"""日记、空间文案、适配器回执与页面任务的联动。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_plugin import plugin
from maiweave.integrations.qzone import DiaryPublisher
from maiweave.core.models import PlanInput


def prepare(p):
    now = p.now()
    day = now.date().isoformat()
    p.config.diary.publish_mode = "手动发布"
    p.ctx.call_capability = AsyncMock(side_effect=adapter)
    p.ctx.llm.generate.return_value = {"success": True, "response": "午后读了几页书，忽然想起昨天留下的问题。"}
    with p.store.db:
        p.store.put("diary", {"id": day, "day": day, "status": "已生成", "complete": False,
                             "content": "私人日记内容", "plan_id": "writing", "material": {
            "活动": [{"title": "读书", "kind": "leisure", "status": "completed", "session_id": "", "participants": []},
                     {"title": "私人约定SECRET", "kind": "social", "status": "completed", "session_id": "private"}],
            "经历": [{"title": "散步", "summary": "闻到桂花香", "session_id": "", "source": "simulated"},
                     {"title": "私聊SECRET", "session_id": "private"}]}})
    return now, day


async def adapter(capability, *, api_name, version, timeout_ms, args):
    assert capability == "api.call" and version == "1"
    name = api_name
    if name.endswith("get_version_info"):
        return {"status": "ok", "retcode": 0, "data": {"app_name": "SnowLuma"}}
    if name.endswith("get_login_info"):
        return {"user_id": 123456}
    if name.endswith("send_qzone_msg"):
        return {"status": "ok", "retcode": 0, "data": {"tid": "post1"}}
    raise AssertionError(name)


async def test_diary_draft_edit_publish_receipt_and_regeneration(plugin):
    p=plugin;now,day=prepare(p)
    row=await p.diary_publisher.prepare(day,now)
    assert row['status']=='草稿'
    prompt=p.ctx.llm.generate.call_args.args[0]
    assert 'SECRET' not in prompt and '私人日记内容' not in prompt and '读书' in prompt
    assert p.store.all('model_usage')[-1]['feature']=='空间文案'
    await p.ui_diary_publish_action('save',day,'编辑后的生活随笔')
    await p.diary_publisher.publish(day,now)
    result=await p.ui_diary_publication(day)
    assert result['record']['tid']=='post1' and result['record']['status']=='已发布'
    assert result['record']['published_at'] and result['record']['account']=='123456'
    assert p.ctx.call_capability.call_args.kwargs['args']['params']=={'content':'编辑后的生活随笔','ugc_right':4}
    assert (await p.ui_diaries())['entries'][0]['publication_status']=='已发布'
    await p.diary_publisher.publish(day,now)
    assert p.ctx.call_capability.await_count==3
    diary=p.store.get('diary',day);diary['content']='重新生成的日记'
    with p.store.db:p.store.put('diary',diary)
    assert (await p.ui_diary_publication(day))['record']['content']=='编辑后的生活随笔'


async def test_auto_waits_diary_schedule_completion_once(plugin):
    p=plugin;now,day=prepare(p);p.config.diary.publish_mode='日记完成后自动发布'
    plan=p.engine.create_plan(PlanInput(title='写日记',kind='diary',start=now.isoformat(),end=(now+timedelta(minutes=1)).isoformat()),'',now)
    diary=p.store.get('diary',day);diary['plan_id']=plan['id']
    with p.store.db:p.store.put('diary',diary)
    p.engine.advance(now)
    await p.diary_publisher.process(now)
    p.ctx.call_capability.assert_not_awaited()
    p.engine.advance(now+timedelta(minutes=2))
    await p.diary_publisher.process(now+timedelta(minutes=2))
    await p.diary_publisher.process(now+timedelta(minutes=3))
    assert p.store.get('diary_publication',day)['status']=='已发布'
    assert p.ctx.llm.generate.await_count==1 and p.ctx.call_capability.await_count==3


async def test_off_mode_and_manual_mode_skip_automatic_publish(plugin):
    p=plugin;now,day=prepare(p)
    for mode in ['关闭','手动发布']:
        p.config.diary.publish_mode=mode
        await p.diary_publisher.process(now)
    p.ctx.call_capability.assert_not_awaited()
    p.config.diary.publish_mode='关闭'
    await p.diary_publisher.prepare(day,now)
    with pytest.raises(ValueError,match='配置'):
        await p.diary_publisher.publish(day,now)


async def test_wrong_client_is_shown_before_publishing(plugin):
    p=plugin;now,day=prepare(p)
    p.ctx.call_capability.return_value={'status':'ok','retcode':0,'data':{'app_name':'OtherClient'}}
    p.ctx.call_capability.side_effect=None
    await p.diary_publisher.prepare(day,now)
    with pytest.raises(ValueError,match='SnowLuma'):
        await p.diary_publisher.publish(day,now)
    assert p.ctx.call_capability.await_count==1
    assert p.store.get('diary_publication',day)['status']=='发布失败'
    assert p.diary_publisher.connection['status']=='连接失败'


@pytest.mark.parametrize('result,expected',[(TimeoutError('发送超时'),'待核对'),({'status':'failed','retcode':1,'message':'空间服务拒绝'},'发布失败'),({'success':False,'error':'对端未连接'},'发布失败')])
async def test_failure_receipt_and_explicit_recovery(plugin,result,expected):
    p=plugin;now,day=prepare(p)
    async def call(name,**kwargs):
        if kwargs['api_name'].endswith('send_qzone_msg'):
            if isinstance(result,Exception):raise result
            return result
        return await adapter(name,**kwargs)
    p.ctx.call_capability.side_effect=call
    await p.diary_publisher.prepare(day,now)
    with pytest.raises((TimeoutError,RuntimeError)):
        await p.diary_publisher.publish(day,now)
    assert p.store.get('diary_publication',day)['status']==expected
    assert p.ctx.call_capability.await_count==3
    if expected=='待核对':
        with pytest.raises(ValueError,match='待核对'):p.diary_publisher.save(day,'新文案',now)
        await p.ui_diary_publish_action('confirm_unpublished',day)
    else:p.diary_publisher.save(day,'重试文案',now)
    p.ctx.call_capability.side_effect=adapter
    await p.diary_publisher.publish(day,now)
    assert p.store.get('diary_publication',day)['status']=='已发布'


async def test_two_publish_requests_share_state(plugin):
    p=plugin;now,day=prepare(p);await p.diary_publisher.prepare(day,now)
    entered=asyncio.Event();release=asyncio.Event()
    async def call(name,**kwargs):
        if kwargs['api_name'].endswith('send_qzone_msg'):entered.set();await release.wait()
        return await adapter(name,**kwargs)
    p.ctx.call_capability.side_effect=call
    task=asyncio.create_task(p.diary_publisher.publish(day,now));await entered.wait()
    with pytest.raises(ValueError,match='发布中'):await p.diary_publisher.publish(day,now)
    release.set();await task
    assert p.ctx.call_capability.await_count==3


async def test_background_job_progress_and_model_failure(plugin):
    p=plugin;now,day=prepare(p)
    entered=asyncio.Event();release=asyncio.Event()
    async def model(*args,**kwargs):entered.set();await release.wait();return {'success':True,'response':'今天的随笔'}
    p.ctx.llm.generate.side_effect=model
    job=await p.ui_diary_publish_action('generate',day)
    await entered.wait()
    assert not (await p.ui_operation_status(task_id=job['task_id']))['done']
    assert (await p.ui_diary_publication(day))['record']['status']=='生成中'
    release.set();await asyncio.gather(*list(p.jobs))
    assert (await p.ui_operation_status(task_id=job['task_id']))['done']
    p.ctx.llm.generate.side_effect=None;p.ctx.llm.generate.return_value={'success':False,'error':'模型暂不可用'}
    with pytest.raises(RuntimeError):await p.diary_publisher.prepare(day,now)
    assert p.store.get('diary_publication',day)['status']=='生成失败'
    assert p.ctx.llm.generate.await_count==4


async def test_reload_publication_requires_result_check(plugin):
    p=plugin;now,day=prepare(p)
    p.diary_publisher.save(day,'一篇随笔',now)
    row=p.store.get('diary_publication',day);row['status']='发布中'
    with p.store.db:p.store.put('diary_publication',row)
    p.diary_publisher=DiaryPublisher(p.engine,p.ctx,p.config)
    assert p.store.get('diary_publication',day)['status']=='待核对'
    await p.ui_diary_publish_action('confirm_published',day)
    assert p.store.get('diary_publication',day)['receipt']=='用户在 QQ 空间确认已发布'


async def test_no_public_material_and_visibility(plugin):
    p=plugin;now,day=prepare(p)
    diary=p.store.get('diary',day);original=diary['material'];diary['material']={}
    with p.store.db:p.store.put('diary',diary)
    with pytest.raises(ValueError,match='素材'):await p.diary_publisher.prepare(day,now)
    p.ctx.llm.generate.assert_not_awaited()
    diary['material']=original
    with p.store.db:p.store.put('diary',diary)
    p.config.diary.publish_visibility='仅自己';p.diary_publisher.save(day,'自己看',now)
    await p.diary_publisher.publish(day,now)
    assert p.ctx.call_capability.call_args.kwargs['args']['params']['ugc_right']==64


def test_publication_capability_declared():
    import json
    from pathlib import Path
    manifest=json.loads((Path(__file__).parents[1]/'_manifest.json').read_text(encoding='utf-8'))
    assert 'api.call' in manifest['capabilities']


async def test_connection_shows_native_api_error(plugin):
    plugin.ctx.call_capability = AsyncMock(return_value={"success": False, "error": "对端动作执行失败: action=get_version_info error=对端未连接"})
    with pytest.raises(RuntimeError, match="对端未连接"):
        await plugin.diary_publisher.check_connection()
    assert plugin.diary_publisher.connection["status"] == "连接失败"
    assert "对端未连接" in plugin.diary_publisher.connection["error"]


async def test_diary_finished_after_midnight_is_published_once(plugin):
    p = plugin
    now = p.now().replace(hour=23, minute=59, second=0, microsecond=0)
    p.now = lambda: now
    with p.store.db:
        p.store.put("state", {**p.engine.state(), "updated_at": now.isoformat()})
    now, day = prepare(p)
    p.config.diary.publish_mode = "日记完成后自动发布"
    plan = p.engine.create_plan(PlanInput(title="睡前日记", kind="diary", start=now.isoformat(),
                               end=(now + timedelta(minutes=15)).isoformat()), "", now)
    with p.store.db:
        p.store.put("diary", {**p.store.get("diary", day), "plan_id": plan["id"]})
    finished = now + timedelta(minutes=16)
    p.engine.advance(finished)
    await p.diary_publisher.process(finished)
    await p.diary_publisher.process(finished + timedelta(minutes=1))
    assert p.store.get("diary_publication", day)["status"] == "已发布"
    assert p.ctx.call_capability.await_count == 3


@pytest.mark.parametrize("image_status", ["生成失败", "配图已按保留设置清理"])
async def test_changed_publication_clears_old_scene_without_image(plugin, image_status):
    p = plugin
    now, day = prepare(p)
    await p.diary_publisher.prepare(day, now)
    row = p.store.get("diary_publication", day)
    row.update(image_prompt="旧正文中的书桌", image_status=image_status, image_error="之前的错误")
    p.diary_publisher.put(row)
    p.diary_publisher.save(day, row["content"], now)
    assert p.store.get("diary_publication", day)["image_prompt"] == "旧正文中的书桌"
    p.diary_publisher.save(day, "今天去了海边散步。", now)
    saved = p.store.get("diary_publication", day)
    assert not any(key.startswith("image_") for key in saved)
    assert saved["content"] == "今天去了海边散步。"


@pytest.mark.parametrize("client", ["SnowLuma", "NapCat"])
@pytest.mark.parametrize("visibility,right", [("所有人", 1), ("好友", 4), ("仅自己", 64)])
@pytest.mark.parametrize("with_image", [False, True])
async def test_clients_publish_with_shared_records(plugin, monkeypatch, client, visibility, right, with_image):
    from test_diary_images import service
    p = plugin
    now, day = prepare(p)
    calls = []
    from maiweave.integrations.qzone_web import QzoneWeb, UPLOAD_URL
    http = []
    def request(self, url, fields):
        http.append((url, fields))
        if url == UPLOAD_URL:
            return {"ret": 0, "data": {"url": "https://example.com/image?bo=abc", "albumid": "a", "lloc": "l", "sloc": "s", "type": 1, "height": 576, "width": 768}}
        return {"code": 0, "tid": "napcat-post"}
    monkeypatch.setattr(QzoneWeb, "request", request)
    async def call(capability, **kwargs):
        calls.append(kwargs)
        if kwargs["api_name"].endswith("get_version_info"):
            return {"status": "ok", "retcode": 0, "data": {"app_name": client}}
        if kwargs["api_name"] == "adapter.napcat.account.get_cookies":
            assert kwargs["args"] == {"params": {"domain": "qzone.qq.com"}}
            return {"status": "ok", "retcode": 0, "data": {"cookies": "p_skey=test; uin=o123456;"}}
        return await adapter(capability, **kwargs)
    p.ctx.call_capability.side_effect = call
    p.config.diary.publish_visibility = visibility
    p.diary_publisher.save(day, "今天的生活随笔", now)
    if with_image:
        service(p, monkeypatch)
        await p.diary_publisher.illustrate(day, now, "窗边的茶杯")
    result = await p.diary_publisher.publish(day, now)
    assert result["client"] == client and result["account"] == "123456"
    assert result["status"] == "已发布"
    assert result["tid"] == ("napcat-post" if client == "NapCat" else "post1")
    if client == "NapCat":
        assert calls[-1]["api_name"] == "adapter.napcat.account.get_cookies"
        fields = http[-1][1]
        assert fields["ugc_right"] == right and fields["con"] == "今天的生活随笔"
        assert ("richval" in fields) == with_image
        if with_image:
            assert fields["pic_bo"] == "abc"
            assert fields["richval"] == ",a,l,s,1,576,768,,576,768"
            assert http[0][1]["picfile"] and http[0][1]["base64"] == 1
    else:
        assert not http
        assert calls[-1]["api_name"] == "adapter.snowluma.qzone.send_qzone_msg"
        params = calls[-1]["args"]["params"]
        assert params["ugc_right"] == right and params["content"] == "今天的生活随笔"
        assert ("images" in params) == with_image
        if with_image:
            assert params["images"][0].startswith("base64://")
    await p.diary_publisher.publish(day, now)
    assert len(calls) == 3


@pytest.mark.parametrize("outcome", ["failure", "timeout", "upload_failure", "missing_tid", "cookie_failure", "invalid_response"])
async def test_napcat_publish_failure_keeps_draft_image(plugin, monkeypatch, outcome):
    from test_diary_images import service
    p = plugin
    now, day = prepare(p)
    service(p, monkeypatch)
    p.diary_publisher.save(day, "今天的随笔", now)
    await p.diary_publisher.illustrate(day, now, "茶杯")
    original = p.store.get("diary_publication", day)["image_file"]
    async def call(capability, **kwargs):
        if kwargs["api_name"].endswith("get_version_info"):
            return {"status": "ok", "retcode": 0, "data": {"app_name": "NapCat"}}
        if kwargs["api_name"] == "adapter.napcat.account.get_cookies":
            if outcome == "cookie_failure":
                return {"status": "failed", "retcode": 1}
            return {"status": "ok", "retcode": 0, "data": {"cookies": "p_skey=test; uin=o123456;"}}
        return await adapter(capability, **kwargs)
    from maiweave.integrations.qzone_web import QzoneWeb, UPLOAD_URL
    http = []
    def request(self, url, fields):
        http.append(url)
        if url == UPLOAD_URL:
            if outcome == "upload_failure":
                return {"ret": 1, "msg": "上传失败"}
            return {"ret": 0, "data": {"url": "https://example.com/image?bo=abc", "albumid": "a", "lloc": "l", "sloc": "s", "type": 1, "height": 576, "width": 768}}
        if outcome == "timeout":
            raise TimeoutError("请求超时")
        if outcome == "invalid_response":
            raise ValueError("空间响应不是 JSON")
        return {"code": 0} if outcome == "missing_tid" else {"code": 1, "message": "发布被拒绝"}
    monkeypatch.setattr(QzoneWeb, "request", request)
    p.ctx.call_capability.side_effect = call
    with pytest.raises((RuntimeError, TimeoutError, ValueError)):
        await p.diary_publisher.publish(day, now)
    row = p.store.get("diary_publication", day)
    assert row["status"] == ("待核对" if outcome in {"timeout", "missing_tid", "invalid_response"} else "发布失败")
    assert row["image_file"] == original and row["content"] == "今天的随笔"

    if outcome == "cookie_failure":
        assert not http
    if outcome == "upload_failure":
        assert http == [UPLOAD_URL]


def test_qzone_web_form_and_cookie(monkeypatch):
    import io
    from urllib.parse import parse_qs, urlsplit
    from maiweave.integrations import qzone_web
    requests = []
    def open_request(request, timeout):
        requests.append(request)
        assert timeout == 60
        return io.BytesIO(b'callback({"code":0,"tid":"test"});')
    monkeypatch.setattr(qzone_web, "urlopen", open_request)
    web = qzone_web.QzoneWeb("123456", "uin=o123456; p_skey=abc==; skey=test;")
    fields = web.prepare("窗边 & 茶杯 + 书", 64)
    assert web.publish(fields)["tid"] == "test"
    request = requests[0]
    assert parse_qs(request.data.decode())["con"] == ["窗边 & 茶杯 + 书"]
    assert parse_qs(request.data.decode())["ugc_right"] == ["64"]
    assert "p_skey=abc==" in request.get_header("Cookie")
    assert parse_qs(urlsplit(request.full_url).query)["uin"] == ["123456"]
    with pytest.raises(ValueError, match="账号"):
        qzone_web.QzoneWeb("123456", "uin=o654321; p_skey=test")
    with pytest.raises(ValueError, match="p_skey"):
        qzone_web.QzoneWeb("123456", "uin=o123456")

async def test_diary_completed_before_midnight_publishes_on_next_tick(plugin):
    p=plugin;_,day=prepare(p);p.config.diary.publish_mode='日记完成后自动发布'
    now=p.now().replace(hour=23,minute=58,second=30,microsecond=0)
    plan=p.engine.create_plan(PlanInput(title='写日记',kind='diary',start=now.isoformat(),end=(now+timedelta(minutes=1)).isoformat()),'',now)
    with p.store.db:
        diary=p.store.get('diary',day);diary['plan_id']=plan['id'];p.store.put('diary',diary)
    p.engine.advance(now)
    p.engine.advance(now+timedelta(minutes=2))
    await p.diary_publisher.process(now+timedelta(minutes=2))
    assert p.store.get('diary_publication',day)['status']=='已发布'
