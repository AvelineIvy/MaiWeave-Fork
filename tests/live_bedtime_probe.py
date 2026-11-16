"""使用实机模型核验凌晨作息与日记安排。"""
import asyncio,json,tomllib
from pathlib import Path
from types import SimpleNamespace
from datetime import timedelta
from contextlib import suppress
from uuid import uuid4
from .live_reading_probe import Probe

class BedtimeProbe(Probe):
    async def run(self,case):
        self.result.update(checks=[])
        native=self.ctx
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=True,schedule_enabled=True,wake_hour=9)
        cfg['world'].update(weather_enabled=False,places_enabled=False,explore_enabled=False)
        cfg['proactive'].update(enabled=False,natural_greetings=True)
        try:
            for hour in (1,2):
                folder=Path(native.paths.data_dir)/uuid4().hex;folder.mkdir()
                class Context:
                    paths=SimpleNamespace(data_dir=str(folder))
                    def __getattr__(self,name):return getattr(native,name)
                cfg['life']['sleep_hour']=hour
                p=self.create();p.set_plugin_config(cfg);p._set_context(Context())
                try:
                    await p.on_load();p.worker.cancel()
                    with suppress(asyncio.CancelledError):await p.worker
                    p.config.life.enabled=True
                    bedtime=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)+timedelta(days=1)
                    for remaining in (120,40,20):
                        now=bedtime-timedelta(minutes=remaining);p.now=lambda:now
                        self.result.update(stage=f'{hour}点入睡，提前{remaining}分钟规划')
                        with p.store.db:
                            for row in p.store.all('plan'):p.store.delete('plan',row['id'])
                            p.store.delete('runtime','planning')
                            state=p.engine.state();state.update(energy=65,hunger=25,updated_at=now.isoformat());p.store.put('state',state)
                        await p.living.choose_activity(now)
                        current=p.engine.current(now)
                        assert current and current['kind']!='sleep',current
                        assert p.engine.time(current['end'])<=bedtime
                        context=json.loads(p.context_text('bedtime-probe').split('\n')[-1])
                        assert not context['sleep_policy']['quiet']
                        assert context['sleep_policy']['minutes_until_bed']==remaining
                        greeting=p.living.greetings.window(now)
                        assert bool(greeting)==(remaining<=20)
                        self.result['checks'].append({'hour':hour,'remaining':remaining,'plan':{k:current[k] for k in ('title','kind','start','end','motivation')},'sleep_policy':context['sleep_policy'],'greeting':str(greeting)})
                    with p.store.db:
                        for row in p.store.all('plan'):p.store.delete('plan',row['id'])
                    now=bedtime-timedelta(hours=2);p.now=lambda:now
                    self.result['stage']=f'{hour}点入睡，真实日记安排'
                    diary=await p.diary.arrange(now,p.spatial)
                    assert diary
                    self.result['checks'].append({'hour':hour,'diary':{k:diary[k] for k in ('title','start','end','motivation')}})
                    for minutes in (30,10,0,-30):
                        at=bedtime-timedelta(minutes=minutes)
                        assert await p.diary.arrange(at,p.spatial) is None
                    with p.store.db:
                        for row in p.store.all('plan'):p.store.delete('plan',row['id'])
                        p.store.delete('runtime','planning')
                        state=p.engine.state();state.update(energy=20,hunger=25,updated_at=now.isoformat());p.store.put('state',state)
                    await p.living.choose_activity(now)
                    assert p.engine.current(now)['kind']=='rest' and not p.living.quiet_sleep(now)
                    with p.store.db:
                        for row in p.store.all('plan'):p.store.delete('plan',row['id'])
                        p.store.delete('runtime','planning')
                    now=bedtime;p.now=lambda:now
                    await p.living.choose_activity(now)
                    assert p.engine.current(now)['kind']=='sleep' and p.living.quiet_sleep(now)
                    self.result['checks'].append({'hour':hour,'rest':'低精力休息可聊','sleep':'到点进入睡眠并静默'})
                finally:await p.on_unload()
            self.result.update(status='已完成',stage='全部完成')
        except Exception as error:
            self.result.update(status='失败',error=repr(error));self.ctx.logger.exception('凌晨作息实测失败')

def create_plugin():return BedtimeProbe()
