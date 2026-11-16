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
            stream = "test-private-session"
            import base64
            p.config.diary.image_reference_protocol = "Unsloth 原生"
            p.config.diary.image_base_url = "https://images.example.com"
            p.life_images.images.upload_reference(base64.b64encode((Path(__file__).parent / "reference.jpg").read_bytes()).decode())
            p.config.diary.image_character_reference_enabled = True
            p.config.diary.image_character_enabled = True
            p.config.diary.image_character_appearance = "成年女性，棕色长发，琥珀色眼睛"
            p.config.diary.image_character_clothing = "米白色针织衫"
            checks = []
            self.result.update(checks=checks, folder=str(folder))
            summary = "刚做好一只白色钩织小兔子，给它围上粉色围巾，在窗边举着它留了一张自己的自拍"
            event = p.engine.record_experience(ExperienceInput(summary=summary, source="simulated", occurred_at=now.isoformat(), evidence="图片实机测试"), stream, now)
            self.result.update(stage="聊天追问：参考图自拍并发送 QQ")
            job = await p.show_life("send", event["id"], framing="想看看你和兔子一起的前置自拍", stream_id=stream)
            await asyncio.gather(*list(p.jobs))
            assert p.store.get("job", job["task_id"])["status"] == "已完成", p.store.get("job", job["task_id"])
            row = p.store.get("experience_image", event["id"])
            assert row["image_character_visible"] and row["image_selfie"], row
            checks.append({"case":"聊天追问", "image":row["image_file"], "delivery":p.store.all("image_delivery")[-1]})
            old_file = row["image_file"]
            await p.show_life("send", event["id"], stream_id=stream)
            assert p.store.get("experience_image", event["id"])["image_file"] == old_file
            checks.append({"case":"追问复用", "delivery":p.store.all("image_delivery")[-1]})
            for mode, detail in [("主动表达", "窗边举着刚折好的三角书签自拍，想展示纸上的小猫图案"), ("自然交流", "坐在手作桌旁举着刚做好的白色小兔子自拍，粉色围巾终于系好了")]:
                self.result.update(stage=mode+"：真实文字发送后配图")
                p.config.proactive.mode = mode
                now = p.now()
                event = p.engine.record_experience(ExperienceInput(summary=detail, source="simulated", occurred_at=now.isoformat(), evidence="图片实机测试"), stream, now)
                receipt = {"session":stream, "mode":mode, "record_id":event["id"], "experience_id":event["id"], "topic":detail, "notice":False, "image_requested":True}
                intent = "这是用户要求的 QQ 图片联调测试。请用一句自然的短话分享这件事："+detail+"。图片由插件随后发送，正文只需说这件事。"
                if mode == "主动表达":
                    await p.living.receipts.express(stream, intent, receipt, now)
                    receipt = p.store.all("share_receipt")[-1]
                else:
                    result = await native.maisaka.proactive.trigger(stream, intent, reason="用户要求的配图实机测试", metadata={"maiweave_record_id":event["id"]})
                    assert result["success"], result
                    p.living.receipts.accept(result["task_id"], receipt, now)
                    for _ in range(90):
                        await asyncio.sleep(2)
                        await p.living.receipts.refresh(p.now())
                        receipt = p.store.get("share_receipt", result["task_id"])
                        if receipt["outcome"] != "MaiBot 已受理": break
                assert receipt["outcome"] == "实际已发送", receipt
                before = len(p.living.receipts.sent_times(stream, now.tzinfo))
                await p.living.share_images(p.now())
                await p.living.share_images(p.now())
                receipt = p.store.get("share_receipt", receipt["id"])
                assert receipt["image_delivery"] == "已发送", receipt
                assert len(p.living.receipts.sent_times(stream, now.tzinfo)) == before
                checks.append({"case":mode,"receipt":receipt,"image":p.store.get("experience_image",event["id"])["image_file"]})
            self.result.update(stage="空间随笔：参考图自动配图")
            day = p.now().date().isoformat()
            with p.store.db:
                p.store.put("diary", {"id":day,"status":"已生成","material":{"经历":[{"summary":"今天做了白色兔子手作，系好粉色围巾，我在窗边拿着它拍了张前置自拍，想把这张自己的自拍放进空间随笔。", "source":"simulated","session_id":""}]}})
            p.config.diary.image_enabled = True
            row = await p.diary_publisher.prepare(day, p.now())
            assert row["image_status"] == "已生成" and row["image_character_visible"], row
            checks.append({"case":"空间自动配图", "image":row["image_file"],"selfie":row.get("image_selfie"),"status":row["status"]})
            self.result.update(status="已完成", usage=[{"feature":r["feature"],"task":r["task"],"status":r["status"]} for r in p.store.all("model_usage")], deliveries=len(p.store.all("image_delivery")))
        except Exception as error:
            self.result.update(status="失败",error=str(error))
            self.ctx.logger.exception("生活图片实机测试失败")
        finally:
            self.active=None
            await p.on_unload()


def create_plugin():
    return ImageProbe()
