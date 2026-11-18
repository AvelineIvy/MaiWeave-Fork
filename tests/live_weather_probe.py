"""在实机隔离数据目录核验两种天气来源与生活联动。"""
import asyncio
import importlib.util
import sys
import tomllib
from pathlib import Path
from datetime import timedelta
from contextlib import suppress
from types import SimpleNamespace
from uuid import uuid4
from maibot_sdk import API, MaiBotPlugin


class WeatherProbe(MaiBotPlugin):
    async def on_load(self):
        self.result = {"status": "等待测试"}
        self.task = None

    async def on_unload(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def on_config_update(self, scope, config_data, version):
        pass

    @API("start", version="1")
    async def start(self, case: str):
        self.result = {"status": "运行中", "checks": []}
        self.task = asyncio.create_task(self.run())
        return self.result

    @API("status", version="1")
    async def status(self):
        return self.result

    async def run(self):
        root = Path(__file__).resolve().parent.parent / "ilworkcn_maiweave"
        spec = importlib.util.spec_from_file_location("weather_probe_target", root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        from weather_probe_target.plugin import create_plugin
        from weather_probe_target.core.models import PlanInput, ExperienceInput
        folder = Path(self.ctx.paths.data_dir) / uuid4().hex
        folder.mkdir()
        native = self.ctx
        class Context:
            paths = SimpleNamespace(data_dir=str(folder))
            def __getattr__(self, name):
                return getattr(native, name)
        p = create_plugin()
        config = tomllib.loads((root / "config.toml").read_text())
        config['life'].update(enabled=False, autonomous=False, schedule_enabled=False)
        p.set_plugin_config(config)
        p._set_context(Context())
        try:
            await create_plugin().on_unload()
            await p.on_load()
            p.worker.cancel()
            with suppress(asyncio.CancelledError):
                await p.worker
            p.config.life.enabled = True
            p.config.world.weather_enabled = True
            for source in ["心知天气", "Open-Meteo"]:
                self.result['stage'] = source
                p.config.world.weather_provider = source
                now = p.now()
                weather = await p.world_tool('weather')
                view = await p.ui_world()
                assert weather['source'] == view['source'] == source
                event = p.engine.record_experience(ExperienceInput(summary='在公园散步拍照', source='simulated',
                    occurred_at=now.isoformat(), evidence='实机隔离测试', details={'地点':'公园'}), '', now)
                environment = p.life_images.images.environment('experience_image', {'id':event['id']}, now)
                assert source == p.world.context(now)['weather']['source']
                assert str(weather['current']['temperature_2m']) in environment
                self.result['checks'].append({'source':source,'temperature':weather['current']['temperature_2m'],
                    'label':weather['label'],'forecast_days':len(weather['forecast']), 'image_environment':environment})
                if not weather['outdoor_suitable']:
                    a = p.spatial.schedule(PlanInput(title='散步', start=(now+timedelta(hours=1)).isoformat(),
                        end=(now+timedelta(hours=2)).isoformat(),location='公园'),'',now,autonomous=True)
                    b = p.spatial.schedule(PlanInput(title='约好见面', start=(now+timedelta(hours=3)).isoformat(),
                        end=(now+timedelta(hours=4)).isoformat(),location='公园',participants=['测试朋友']),'test-session',now)
                    p.living.adjust(now)
                    assert p.store.get('plan',a['id'])['location'] == '住处'
                    assert p.store.get('plan',b['id'])['location'] == '公园'
                    assert p.store.get('schedule_notice',b['id'])
                    self.result['checks'][-1]['schedule'] = '自主外出转室内，共同安排保留并提醒'
                    for plan in p.store.all('plan'):
                        if p.store.get('plan',plan['id'])['status'] == 'scheduled':
                            p.engine.transition(plan['id'],'cancel',plan['session_id'],now)
            assert await p.ui_record_support('images') == await p.ui_life_images()
            assert await p.ui_record_support('retention') == await p.ui_retention()
            self.result['status'] = '通过'
        except Exception as error:
            self.result.update(status='失败',error=str(error),trace=__import__('traceback').format_exc())
        finally:
            await p.on_unload()


def create_plugin():
    return WeatherProbe()
