"""真实群聊接话与私聊接续的实机测试。"""
import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from datetime import timedelta
from uuid import uuid4
from maibot_sdk import HookHandler
from .live_reading_probe import Probe

PRIVATE = "test-private-session"
GROUP = ""
GROUP_ID = "test-group-id"
USER = "test-user-id"


class GroupProbe(Probe):
    task = None

    @HookHandler("chat.receive.after_process")
    async def farewell_receive(self, message: dict, **kwargs):
        global GROUP
        group_info = message.get("message_info", {}).get("group_info")
        if message.get("platform") == "qq" and group_info and str(group_info.get("group_id")) == GROUP_ID:
            GROUP = message["session_id"]
        if self.active and message.get("session_id") in (PRIVATE,GROUP):
            await self.active.management_message(message,**kwargs)
            info=message.get("message_info",{})
            if str(info.get("user_info",{}).get("user_id"))==USER:
                self.messages.append({"session":message["session_id"],"text":message.get("processed_plain_text",""),"at":self.active.now().isoformat()})
                if message["session_id"]==GROUP and self.result.get("stage")=="等待群消息":
                    self.group_message=message
                    self.group_ready.set()
                if message["session_id"]==PRIVATE and self.result.get("stage")=="等待私聊接话":
                    self.private_ready.set()
        return {"action":"continue"}

    @HookHandler("maisaka.planner.before_request")
    async def planner_context(self, **kwargs):
        if self.active and kwargs.get("session_id") in (PRIVATE,GROUP):
            result=await self.active.planner_context(**kwargs)
            text=result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
            snapshot=json.loads(text.split("\n")[-1])
            self.contexts.append({"session":kwargs["session_id"],"stage":"Planner","chars":len(text),"encounters":snapshot.get("encounters",[])})
            return result
        return {"action":"continue"}

    @HookHandler("maisaka.planner.after_response")
    async def planner_receipt(self, **kwargs):
        if self.active and kwargs.get("session_id") in (PRIVATE,GROUP):
            return await self.active.planner_receipt(**kwargs)
        return {"action":"continue"}

    @HookHandler("maisaka.replyer.before_request")
    async def reply_context(self, **kwargs):
        if self.active and kwargs.get("session_id") in (PRIVATE,GROUP):
            result=await self.active.reply_context(**kwargs)
            text=result["modified_kwargs"]["extra_prompt"]
            snapshot=json.loads(text.split("\n")[-1])
            self.contexts.append({"session":kwargs["session_id"],"stage":"回复","encounters":snapshot.get("encounters",[])})
            return result
        return {"action":"continue"}

    @HookHandler("send_service.before_send")
    async def farewell_before_send(self,message:dict,storage_message:bool=True,**kwargs):
        if self.active and message.get("session_id") in (PRIVATE,GROUP):
            return await self.active.sleep_before_send(message,storage_message=storage_message)
        return {"action":"continue"}

    @HookHandler("send_service.after_send")
    async def farewell_after_send(self,message:dict,sent:bool,**kwargs):
        if self.active and message.get("session_id") in (PRIVATE,GROUP):
            self.deliveries.append({"session":message["session_id"],"sent":sent,"at":self.active.now().isoformat()})
            if sent and message["session_id"]==PRIVATE and self.result.get("stage")=="等待私聊回复":
                self.private_sent.set()
        return {"action":"continue"}

    async def run(self,case):
        try:
            await self.run_checks(case)
        except Exception as error:
            self.result.update(status="失败", error=repr(error))
            self.ctx.logger.exception("实机测试准备失败")

    async def run_checks(self,case):
        from maiweave_probe_target.core.models import PlanInput
        native=self.ctx
        folder=Path(native.paths.data_dir)/uuid4().hex
        folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=False,schedule_enabled=False)
        cfg['proactive'].update(enabled=True,session_ids=[],natural_greetings=False)
        p.set_plugin_config(cfg);p._set_context(Context())
        self.messages=[];self.contexts=[];self.deliveries=[]
        self.result.update(checks=[],contexts=self.contexts,messages=self.messages,deliveries=self.deliveries,folder=str(folder))
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            p.config.life.enabled=True;self.active=p
            await p.identify_person(PRIVATE,USER,'qq')
            person=p.living.person_key(PRIVATE,USER)
            with p.store.db:p.store.put('profile',{'id':'friend','session_id':PRIVATE,'user_id':USER,'person_id':person,'intimacy_mode':'manual','intimacy':3,'relationship':'亲密'})
            self.result['stage']='睡眠静默实测'
            now=p.now();p.config.life.schedule_enabled=True;p.config.life.nap_quiet=True
            plan=p.engine.create_plan(PlanInput(title='实机睡眠静默测试',kind='sleep',start=(now-timedelta(minutes=1)).isoformat(),end=(now+timedelta(minutes=5)).isoformat()),'',now)
            p.engine.advance(now)
            result=await native.send.text('【睡眠静默测试】这条应被睡眠钩子拦截。',PRIVATE,return_details=True,storage_message=True,sync_to_maisaka_history=True)
            assert not result['sent'],result
            self.result['checks'].append({'case':'睡眠静默','result':result})
            with p.store.db:p.store.delete('plan',plan['id'])
            p.config.life.schedule_enabled=False
            for kind in ('主动私聊未回','说去睡了'):
                self.result.update(stage='准备场景',kind=kind)
                with p.store.db:
                    for category in ('share_receipt','person_state'):
                        for row in p.store.all(category):p.store.delete(category,row['id'])
                p.presence.offered.clear()
                now=p.now()
                if kind=='主动私聊未回':
                    text='【群聊接话实测】我先来找你啦，等会儿群里见。暂时不用回复这条。'
                    sent=await native.send.text(text,PRIVATE,return_details=True,storage_message=True,sync_to_maisaka_history=True)
                    assert sent['sent'],sent
                    with p.store.db:p.store.put('share_receipt',{'id':'initial','session':PRIVATE,'created':now.isoformat(),'sent_at':now.isoformat(),'outcome':'实际已发送','sent_text':text,'message_id':sent['message_id']})
                else:
                    self.result.update(stage='等待睡觉声明')
                    while True:
                        state=p.store.find('person_state',person)
                        if state and state['status']=='准备睡觉':break
                        await asyncio.sleep(1)
                self.group_ready=asyncio.Event();self.private_ready=asyncio.Event();self.private_sent=asyncio.Event()
                self.result.update(stage='等待群消息')
                await self.group_ready.wait()
                self.result.update(stage='等待群聊实际回复')
                row=None
                for _ in range(180):
                    await asyncio.sleep(2);await p.living.receipts.refresh(p.now())
                    rows=[r for r in p.store.all('share_receipt') if r.get('mode')=='群聊接话']
                    if rows and rows[-1]['outcome']!='MaiBot 已受理':row=rows[-1];break
                assert row and row.get('sent_at'),{'group_receipts':rows,'contexts':self.contexts[-4:]}
                self.result['checks'].append({'case':kind+'群聊投递','receipt':row})
                assert p.living.receipts.sent_times(GROUP,p.now().tzinfo)==[]
                before=len([r for r in p.store.all('share_receipt') if r.get('mode')=='群聊接话'])
                await p.management_message(self.group_message)
                assert p.presence.opportunities(GROUP,USER,p.now())==[]
                assert before==len([r for r in p.store.all('share_receipt') if r.get('mode')=='群聊接话'])
                self.result['checks'].append({'case':kind+'重复触发','result':'原机会已消费，不再产生新机会'})
                saved=p.config.context.group_chars
                for budget in (1200,2400,4800):
                    p.config.context.group_chars=budget
                    text=p.context_text(GROUP,stage='预算验证')
                    assert len(text)<=budget
                p.config.context.group_chars=saved
                self.result['checks'].append({'case':kind+'上下文预算','result':'1200/2400/4800 均在预算内'})
                self.result.update(stage='等待私聊接话')
                await self.private_ready.wait()
                self.result.update(stage='等待私聊回复')
                await asyncio.wait_for(self.private_sent.wait(),360)
                matched=[r for r in self.contexts if r['session']==PRIVATE and any(e.get('stage')=='接着聊' for e in r['encounters'])]
                assert matched,self.contexts[-4:]
                self.result['checks'].append({'case':kind+'转私聊','result':'实际发送成功，模型收到原群已发送内容','context':matched[-1]})
            self.result.update(status='已完成',stage='全部完成')
        except Exception as error:
            self.result.update(status='失败',error=str(error))
            self.ctx.logger.exception('群聊接续实机测试失败')
        finally:
            self.active=None
            await p.on_unload()


def create_plugin():return GroupProbe()
