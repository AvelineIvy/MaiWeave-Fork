"""使用原生模型、图片服务和内部聊天验证生活图片。"""
import asyncio
import tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from PIL import Image
from .live_reading_probe import Probe


class ImageProbe(Probe):
    task = None
    async def run(self, case):
        from maiweave_probe_target.core.models import ExperienceInput
        native = self.ctx
        folder = Path(native.paths.data_dir) / uuid4().hex
        folder.mkdir()
        class Context:
            paths = SimpleNamespace(data_dir=str(folder))
            def __getattr__(self, name):
                return getattr(native, name)
        p = self.create()
        root = Path(__file__).resolve().parent.parent / "ilworkcn_maiweave"
        cfg = tomllib.loads((root / "config.toml").read_text())
        cfg["life"].update(enabled=False, autonomous=False, schedule_enabled=False)
        cfg["diary"].update(image_service_enabled=True, chat_images_enabled=True)
        p.set_plugin_config(cfg)
        p._set_context(Context())
        try:
            await p.on_load()
            p.worker.cancel()
            with suppress(asyncio.CancelledError): await p.worker
            p.config.life.enabled = True
            self.active = p
            now = p.now()
            stream = "test-local-session"
            event = p.engine.record_experience(ExperienceInput(summary="折好一枚三角书角书签，夹在摊开的书页上", source="simulated", occurred_at=now.isoformat(), evidence="生活图片实机测试", details={"形状":"三角书角，平整折痕", "地点":"窗边木桌"}), stream, now)
            self.result.update(stage="验证服务不可用后的聊天提示")
            service_url = p.config.diary.image_base_url
            p.config.diary.image_base_url = "http://127.0.0.1:1/v1"
            failed = await p.show_life("send", event["id"], stream_id=stream)
            await asyncio.gather(*list(p.jobs))
            assert p.store.get("job", failed["task_id"])["status"] == "失败"
            assert p.store.get("experience_image", event["id"])["image_status"] == "生成失败"
            p.config.diary.image_base_url = service_url
            self.result.update(stage="生成并发送追问图片")
            await p.show_life("send", event["id"], stream_id=stream)
            await asyncio.gather(*list(p.jobs))
            row = p.store.get("experience_image", event["id"])
            assert row.get("image_file"), row.get("image_error")
            with Image.open(folder / row["image_file"]) as im: assert im.size == (1024,768), im.size
            original = row["image_file"]
            await p.show_life("send", event["id"], stream_id=stream)
            assert p.store.get("experience_image", event["id"])["image_file"] == original
            try: await p.show_life("send", event["id"], stream_id="other")
            except ValueError: pass
            else: raise AssertionError("聊天归属未校验")
            modes = []
            for mode in ("主动表达", "自然交流"):
                self.result.update(stage=mode+"图文发送")
                data={"session":stream,"mode":mode,"record_id":event["id"],"experience_id":event["id"],"topic":"书角书签实机验证","image_requested":True}
                if mode == "主动表达":
                    await p.living.receipts.express(stream,"这是一轮生活图片实机验证，请简短说刚折好的三角书角书签，随后会展示图片。", data, p.now())
                else:
                    response=await native.maisaka.proactive.trigger(stream,"生活图片实机验证：请用reply工具发一句‘给你看看刚折好的三角书角书签’，本次发送后插件会补上图片。",reason="生活图片实机测试")
                    assert response["success"], response
                    p.living.receipts.accept(response["task_id"],data,p.now())
                    for _ in range(90):
                        await asyncio.sleep(2)
                        await p.living.receipts.refresh(p.now())
                        receipt=p.store.get("share_receipt",response["task_id"])
                        if receipt["outcome"] != "MaiBot 已受理": break
                    assert receipt["outcome"] == "实际已发送", receipt
                await p.living.share_images(p.now())
                receipt=p.store.all("share_receipt")[-1]
                assert receipt["image_delivery"] == "已发送", receipt
                modes.append({"mode":mode,"text":receipt["sent_text"],"image_message_id":receipt["image_message_id"]})
            assert len(p.living.receipts.sent_times(stream,now.tzinfo)) == 2
            assert len(p.store.all("image_delivery")) == 4
            self.result.update(status="已完成",result={"size":[1024,768],"modes":modes,"image_deliveries":4,"quota":2,"reuse":True,"ownership":True,"folder":str(folder)})
        except Exception as error:
            self.result.update(status="失败",error=str(error))
            self.ctx.logger.exception("生活图片实机测试失败")
        finally:
            self.active=None
            await p.on_unload()


def create_plugin():
    return ImageProbe()
