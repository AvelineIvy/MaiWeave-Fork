"""使用原生人物画像和实机模型验证关系身份。"""
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
        cfg['memory']['enabled']=True
        p.set_plugin_config(cfg);p._set_context(Context())
        self.result.update(checks=[])
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            p.config.life.enabled=True
            payload=json.loads(case)
            payload["intimacy_mode"]="auto"
            for label in ("首次建档", "重新判断"):
                self.result["stage"]=label
                row=await p.reviews.submit("profile",dict(payload))
                self.result["checks"].append({"case":label,"status":row["status"],"relationship":row["relationship"],"level":row["level"],"reason":row["reason"]})
                assert row["status"]=="已保存",row["status"]
                assert (row["relationship"],row["level"])==("恋人",3),row["reason"]
                profile=row["result"]
                profile.update(relationship="普通关系",intimacy=0,relationship_reason="旧判断：未明确画像里的角色是当前用户")
                with p.store.db:p.store.put("profile",profile)
                payload["profile_id"]=profile["id"]
            self.result.update(status="已完成",stage="已完成")
        except Exception as error:
            self.result.update(status="失败",error=repr(error));self.ctx.logger.exception("关系身份实测失败")
        finally:await p.on_unload()

def create_plugin():return ReviewProbe()
