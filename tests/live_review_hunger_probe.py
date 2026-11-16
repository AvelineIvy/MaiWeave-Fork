"""用实机模型验证资料核对和高饥饿活动。"""
import asyncio,json,tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from .live_reading_probe import Probe

class ReviewProbe(Probe):
    async def run(self,case):
        native=self.ctx;folder=Path(native.paths.data_dir)/uuid4().hex;folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=True,schedule_enabled=False)
        cfg['world'].update(weather_enabled=False,places_enabled=False,explore_enabled=False)
        cfg['memory']['enabled']=False
        p.set_plugin_config(cfg);p._set_context(Context())
        self.result.update(checks=[])
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            p.config.life.enabled=True
            from maiweave_probe_target.interfaces.review import Assessment
            parsed=Assessment.model_validate_json(json.dumps({'conflicts':[{'type':'生日','text':'日期不同'}],'relationship':'朋友','level':3}))
            assert parsed.conflicts==['生日：日期不同']
            self.result['checks'].append({'case':'冲突对象格式','result':parsed.model_dump()})
            self.result['stage']='真实资料模型核对'
            row=await p.reviews.submit('profile',{'name':'实机核对对象','session_id':'review-probe','notes':'这是角色的好朋友，彼此很亲密，经常相互关心。','intimacy_mode':'auto'})
            assert row['status']=='已保存',row
            self.result['checks'].append({'case':'真实自动关系核对','relationship':row['relationship'],'level':row['level'],'status':row['status']})
            for hunger,energy in ((65,80),(100,80),(100,20)):
                self.result['stage']=f'饥饿{hunger}精力{energy}真实规划'
                now=p.now()
                with p.store.db:
                    for plan in p.store.all('plan'):p.store.delete('plan',plan['id'])
                    p.store.delete('runtime','planning')
                    p.store.put('state',{**p.engine.state(),'hunger':hunger,'energy':energy,'updated_at':now.isoformat()})
                await p.living.choose_activity(now)
                plans=[r for r in p.store.all('plan') if r['kind']!='travel']
                assert plans and plans[-1]['kind'] in {'meal','cook'},plans
                self.result['checks'].append({'hunger':hunger,'energy':energy,'activity':{k:plans[-1][k] for k in ('title','kind','motivation','start','end')}})
            self.result.update(status='已完成',stage='全部完成')
        except Exception as error:
            self.result.update(status='失败',error=repr(error));self.ctx.logger.exception('资料与饥饿实测失败')
        finally:await p.on_unload()

def create_plugin():return ReviewProbe()
