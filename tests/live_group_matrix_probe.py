"""实机验证两种交流模式的次数、冷却与群聊接话。"""
import asyncio
import json
import tomllib
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from .live_group_encounter_probe import GroupProbe, PRIVATE, USER

GROUP = "test-group-session"

class MatrixProbe(GroupProbe):
    async def run_checks(self, case):
        native=self.ctx
        folder=Path(native.paths.data_dir)/uuid4().hex
        folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=False,schedule_enabled=False)
        cfg['proactive'].update(enabled=True,session_ids=[PRIVATE],natural_greetings=False,start_hour=0,end_hour=0)
        p.set_plugin_config(cfg);p._set_context(Context())
        self.messages=[];self.contexts=[];self.deliveries=[]
        self.result.update(checks=[],contexts=self.contexts,messages=self.messages,deliveries=self.deliveries,folder=str(folder))
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            p.config.life.enabled=True;self.active=p
            await p.identify_person(PRIVATE,USER,'qq')
            await p.identify_person(GROUP,USER,'qq')
            person=p.living.person_key(PRIVATE,USER)
            def clear():
                with p.store.db:
                    for category in ('share_receipt','person_state'):
                        for row in p.store.all(category):p.store.delete(category,row['id'])
                p.presence.offered.clear()
            group_message={'session_id':GROUP,'platform':'qq','processed_plain_text':'实机组合验证群消息',
                'message_info':{'user_info':{'user_id':USER},'group_info':{'group_id':'test-group-id'}}}
            private_message={'session_id':PRIVATE,'platform':'qq','processed_plain_text':'我看到你的消息了',
                'message_info':{'user_info':{'user_id':USER},'group_info':None}}
            for mode in ('自然交流','主动表达'):
                p.config.proactive.mode=mode
                self.result.update(mode=mode,stage='组合检查')
                for cooldown in (False,True):
                    for full in (False,True):
                        for replied in (False,True):
                            clear();now=p.now()
                            p.config.proactive.cooldown_minutes=60 if cooldown else 1
                            p.config.proactive.daily_limit=1 if full else 4
                            initial={'id':'initial','session':PRIVATE,'created':(now-timedelta(minutes=1)).isoformat(),
                                'sent_at':(now-timedelta(minutes=1)).isoformat(),'outcome':'实际已发送','mode':mode,'sent_text':'实机组合验证记录'}
                            with p.store.db:p.store.put('share_receipt',initial)
                            if replied:await p.management_message(private_message)
                            if full or cooldown:
                                await p.living.share(now)
                                decision=p.living.share_decisions[0]
                                assert ('今日实际已发送' if full else '下次可请求时间') in decision['reason'],decision
                            await p.management_message(group_message)
                            opportunities=p.presence.encounter_context(GROUP,USER,p.now(),True)
                            assert bool(opportunities)==(not replied),(mode,cooldown,full,replied,opportunities)
                            self.result['checks'].append({'mode':mode,'cooldown':cooldown,'daily_full':full,'private_replied':replied,
                                'group_opportunity':bool(opportunities),'source':'实机隔离记录与接收流程重放'})
                clear()
                p.config.proactive.cooldown_minutes=60;p.config.proactive.daily_limit=1
                now=p.now()
                sent=await native.send.text(f'【{mode}组合实测】我来找你啦，先别回复，等会儿群里见。',PRIVATE,
                    return_details=True,storage_message=True,sync_to_maisaka_history=True)
                assert sent['sent'],sent
                with p.store.db:p.store.put('share_receipt',{'id':'initial','session':PRIVATE,'created':now.isoformat(),
                    'sent_at':now.isoformat(),'outcome':'实际已发送','mode':mode,'sent_text':'我来找你啦，等会儿群里见。','message_id':sent['message_id']})
                await p.living.share(p.now())
                assert '今日实际已发送' in p.living.share_decisions[0]['reason']
                self.group_ready=asyncio.Event();self.private_ready=asyncio.Event();self.private_sent=asyncio.Event()
                self.result.update(stage='等待群消息',kind=mode+'：冷却中且已达每日上限')
                await self.group_ready.wait()
                self.result['stage']='等待群聊实际回复'
                row=None
                for _ in range(180):
                    await asyncio.sleep(2);await p.living.receipts.refresh(p.now())
                    rows=[r for r in p.store.all('share_receipt') if r.get('mode')=='群聊接话']
                    if rows and rows[-1].get('sent_at'):row=rows[-1];break
                assert row,{'contexts':self.contexts[-4:]}
                assert len(p.living.receipts.sent_times(PRIVATE,p.now().tzinfo))==1
                assert p.living.receipts.sent_times(GROUP,p.now().tzinfo)==[]
                await p.management_message(self.group_message)
                assert p.presence.opportunities(GROUP,USER,p.now())==[]
                self.result['checks'].append({'mode':mode,'case':'冷却与满额下真实群聊','receipt':row,'private_count':1,'group_count':0})
                self.result['stage']='等待私聊接话'
                await self.private_ready.wait()
                self.result['stage']='等待私聊回复'
                await asyncio.wait_for(self.private_sent.wait(),360)
                assert p.store.get('share_receipt','initial').get('observed_at')
                await p.management_message(self.group_message)
                assert p.presence.opportunities(GROUP,USER,p.now())==[]
                self.result['checks'].append({'mode':mode,'case':'真实私聊已回复后重放群消息','result':'没有新抓包机会'})
                await asyncio.sleep(8)
            self.result.update(status='已完成',stage='全部完成')
        except Exception as error:
            self.result.update(status='失败',error=repr(error));self.ctx.logger.exception('组合实机测试失败')
        finally:
            self.active=None;await p.on_unload()

def create_plugin():return MatrixProbe()
