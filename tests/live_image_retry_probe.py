"""在独立数据目录验证重试后调用真实图片服务。"""
import asyncio,tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from .live_reading_probe import Probe


class RetryProbe(Probe):
    async def run(self,case):
        from maiweave_probe_target.integrations.diary_images import RetryableImageError
        native=self.ctx
        folder=Path(native.paths.data_dir)/uuid4().hex
        folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=False)
        cfg['world'].update(weather_enabled=False,places_enabled=False,explore_enabled=False)
        p.set_plugin_config(cfg);p._set_context(Context())
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            assert Path(p.store.db.execute('pragma database_list').fetchone()[2]).resolve().parent==folder.resolve()
            images=p.diary_publisher.images
            original=images.request
            calls=[]
            def request(*args):
                calls.append(args)
                if len(calls)==1:raise RetryableImageError('隔离测试：首次请求模拟 HTTP 504')
                return original(*args)
            images.request=request
            await p.ui_test_image(scene='物件近景',prompt='木桌上的三角书角书签，柔和自然光')
            await asyncio.sleep(1)
            progress=await p.ui_image_test()
            self.result.update(stage='真实服务重试中',progress=progress['progress'])
            await asyncio.gather(*list(p.jobs))
            result=await p.ui_image_test()
            assert result['status']=='已生成',result
            assert result['attempts']==2 and calls[0]==calls[1]
            assert not p.store.all('image_delivery')
            self.result.update(status='已完成',attempts=result['attempts'],seconds=result['seconds'],same_request=True,sent_images=0)
        except Exception as error:
            self.result.update(status='失败',error=str(error))
        finally:await p.on_unload()


def create_plugin():return RetryProbe()
