"""通过真实 QQ 验证图片重试、主动分享与失败提示。"""
import asyncio,json,re,tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from maibot_sdk import HookHandler
from .live_reading_probe import Probe


class DeliveryProbe(Probe):
    @HookHandler('maisaka.planner.before_request')
    async def planner_context(self, **kwargs):
        if self.active and kwargs.get('session_id')==self.stream:
            for item in reversed(kwargs.get('items',[])):
                text=''.join(part.get('text','') for part in item.get('parts',[]))
                match=re.search(r'<plugin_proactive_task id="([^"]+)" plugin_id="ilworkcn.maiweave-reading-probe">',text)
                if match:
                    self.active.living.receipts.turn[self.stream]=match[1]
                    break
        return {'action':'continue'}

    @HookHandler('maisaka.planner.after_response')
    async def planner_receipt(self, **kwargs):
        if self.active and kwargs.get('session_id')==self.stream:
            self.active.living.receipts.observe(self.stream,kwargs['output_items'])
        return {'action':'continue'}

    async def run(self,case):
        from maiweave_probe_target.integrations.diary_images import RetryableImageError
        from maiweave_probe_target.core.models import ExperienceInput
        native=self.ctx
        payload=json.loads(case)
        self.stream=payload['session_id']
        if payload.get('verify_message_ids'):
            rows=await native.db.get('Messages',filters={'session_id':self.stream},order_by='-timestamp',limit=60)
            counts=[sum(str(r['message_id'])==str(key) and bool(r['is_picture']) for r in rows) for key in payload['verify_message_ids']]
            assert counts==[1]*len(counts),counts
            self.result.update(status='已完成',picture_counts=counts,latest_timestamp=rows[0]['timestamp'])
            return
        folder=Path(native.paths.data_dir)/uuid4().hex
        folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=False)
        cfg['world'].update(weather_enabled=False,places_enabled=False,explore_enabled=False)
        cfg['diary'].update(image_service_enabled=True,chat_images_enabled=True)
        p.set_plugin_config(cfg);p._set_context(Context())
        self.result.update(checks=[])
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            assert Path(p.store.db.execute('pragma database_list').fetchone()[2]).resolve().parent==folder.resolve()
            p.config.life.enabled=True
            self.active=p
            images=p.life_images.images
            assert p.config.diary.image_retry_count==3
            original=images.request
            progress_seen={}
            async def watch():
                while True:
                    for category in ('experience_image','diary_publication','image_test'):
                        for row in p.store.all(category):
                            progress=row.get('image_progress','')
                            if '正在重试' in progress:
                                progress_seen.setdefault(row['id'],set()).add(progress)
                    if p._image_test_feedback_until==float('inf'):
                        widgets=await p.get_config_widget_state()
                        text=widgets['image_test_result'].text
                        if '正在重试' in text:
                            self.result['widget_progress']=text
                    await asyncio.sleep(0.2)
            watcher=asyncio.create_task(watch())
            async def verify_delivery(event,mode,calls):
                records=[r for r in p.store.all('image_delivery') if r['experience_id']==event['id']]
                assert len(records)==1 and records[0]['status']=='已发送',records
                message=await native.db.get('Messages',filters={'session_id':self.stream,'message_id':records[0]['message_id']},single_result=True)
                assert message and message['is_picture'],records
                assert len(calls)==4 and all(call==calls[0] for call in calls)
                assert len(progress_seen[event['id']])==3,progress_seen
                self.result['checks'].append({'progress':sorted(progress_seen[event['id']]),'mode':mode,'attempts':len(calls),'delivery_count':len(records),'message_id':records[0]['message_id'],'host_picture':True})
            for mode,summary in ([] if payload.get('remaining') else [('聊天追问','桌上刚折好的三角书角书签'),('主动表达','桌上刚做好的白色毛线小兔子'),('自然交流','窗边刚摆好的小花束')]):
                self.result['stage']=mode
                calls=[]
                def request(*args):
                    calls.append(args)
                    if len(calls)<=3:raise RetryableImageError('隔离测试：前三次请求模拟 HTTP 504')
                    return original(*args)
                images.request=request
                now=p.now()
                event=p.engine.record_experience(ExperienceInput(summary=summary,source='simulated',occurred_at=now.isoformat(),evidence='用户授权的 QQ 发图测试'),self.stream,now)
                if mode=='聊天追问':
                    job=await p.show_life('send',event['id'],framing='第一人称物件近景，物件为主体',stream_id=self.stream)
                    await asyncio.gather(*list(p.jobs))
                    assert p.store.get('job',job['task_id'])['status']=='已完成',p.store.get('job',job['task_id'])
                else:
                    p.config.proactive.mode=mode
                    receipt={'session':self.stream,'mode':mode,'record_id':event['id'],'experience_id':event['id'],'topic':summary,'notice':False,'image_requested':True}
                    intent='用户正在进行 QQ 配图联调测试，请用一句话分享这个测试场景：'+summary+'。请实际发送文字，图片由插件随后发送。'
                    if mode=='主动表达':
                        await p.living.receipts.express(self.stream,intent,receipt,now)
                        receipt=next(r for r in p.store.all('share_receipt') if r['experience_id']==event['id'])
                    else:
                        result=await native.maisaka.proactive.trigger(self.stream,intent,reason='用户要求的图片重试实测',metadata={'maiweave_record_id':event['id']})
                        assert result['success'],result
                        p.living.receipts.accept(result['task_id'],receipt,now)
                        for _ in range(90):
                            await asyncio.sleep(2)
                            await p.living.receipts.refresh(p.now())
                            receipt=p.store.get('share_receipt',result['task_id'])
                            if receipt['outcome']!='MaiBot 已受理':break
                    assert receipt['outcome']=='实际已发送',receipt
                    before=len(p.living.receipts.sent_times(self.stream,now.tzinfo))
                    await p.living.share_images(p.now())
                    await p.living.share_images(p.now())
                    assert len(p.living.receipts.sent_times(self.stream,now.tzinfo))==before
                    assert p.store.get('share_receipt',receipt['id'])['image_delivery']=='已发送'
                await verify_delivery(event,mode,calls)
            for mode in (('配置测试',) if payload.get('remaining') else ('空间配图','配置测试')):
                self.result['stage']=mode
                calls=[]
                def request(*args):
                    calls.append(args)
                    if len(calls)<=3:raise RetryableImageError('隔离测试：前三次请求模拟 HTTP 504')
                    return original(*args)
                images.request=request
                p.diary_publisher.images.request=request
                deliveries=len(p.store.all('image_delivery'))
                if mode=='空间配图':
                    key='space-retry'
                    with p.store.db:p.store.put('diary_publication',{'id':key,'status':'草稿','content':'窗边木桌上的小花束'})
                    await images.illustrate('diary_publication',key,p.now(),'窗边木桌上的小花束')
                    row=p.store.get('diary_publication',key)
                    assert row['image_status']=='已生成',row['image_status']
                else:
                    await p.ui_test_image(scene='物件近景',prompt='木桌上的三角书角书签')
                    await asyncio.gather(*list(p.jobs))
                    result=await p.ui_image_test()
                    assert result['status']=='已生成',result['error']
                    key='latest'
                    assert '每次请求超时' in self.result['widget_progress']
                assert len(calls)==4 and all(call==calls[0] for call in calls)
                assert len(progress_seen[key])==3,progress_seen
                assert len(p.store.all('image_delivery'))==deliveries
                self.result['checks'].append({'mode':mode,'attempts':len(calls),'progress':sorted(progress_seen[key]),'extra_deliveries':0})
            self.result['stage']='连续失败提示'
            calls=[]
            def failed(*args):
                calls.append(args)
                raise RetryableImageError('隔离测试：连续 HTTP 504')
            images.request=failed
            now=p.now()
            event=p.engine.record_experience(ExperienceInput(summary='测试用纸花',source='simulated',occurred_at=now.isoformat(),evidence='用户授权的失败提示测试'),self.stream,now)
            job=await p.show_life('send',event['id'],stream_id=self.stream)
            await asyncio.gather(*list(p.jobs))
            assert p.store.get('job',job['task_id'])['status']=='失败'
            assert len(calls)==p.config.diary.image_retry_count+1
            rows=await native.db.get('Messages',filters={'session_id':self.stream},order_by='-timestamp',limit=12)
            replies=[r for r in rows if r.get('processed_plain_text')=='这次图片没发出来，可以稍后再让我试试。' and p.living.receipts.time(r['timestamp'],now.tzinfo)>=now]
            assert len(replies)==1,replies
            assert not [r for r in p.store.all('image_delivery') if r['experience_id']==event['id']]
            self.result['checks'].append({'mode':'连续失败','attempts':len(calls),'failure_messages':len(replies),'images':0})
            for retries in (0,1,2):
                p.config.diary.image_retry_count=retries
                calls.clear()
                key=f'limit-{retries}'
                with p.store.db:p.store.put('diary_publication',{'id':key,'status':'草稿','content':'测试用纸花'})
                try:await images.illustrate('diary_publication',key,p.now(),'测试用纸花')
                except RuntimeError:pass
                assert len(calls)==retries+1,len(calls)
                assert p.store.get('diary_publication',key)['image_status']=='生成失败'
                self.result['checks'].append({'mode':'自定义次数','retries':retries,'attempts':len(calls)})
            self.result.update(status='已完成',stage='完成')
        except Exception as error:
            self.result.update(status='失败',error=str(error))
            self.ctx.logger.exception('图片真实投递测试失败')
        finally:
            if 'watcher' in locals():
                watcher.cancel()
                with suppress(asyncio.CancelledError):await watcher
            self.active=None
            await p.on_unload()


def create_plugin():return DeliveryProbe()
