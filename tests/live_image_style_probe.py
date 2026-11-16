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
            p.config.diary.image_style = "自然生活照片，空间测试用柔和窗光，横向4:3"
            p.config.diary.chat_image_style = "自然生活照片，聊天用随手拍构图，横向4:3"
            cls = type(p.diary_publisher.images)
            original_request = cls.request
            prompts = []
            def capture(images, prompt):
                prompts.append(prompt)
                return original_request(images, prompt)
            cls.request = capture
            self.result.update(stage="配置测试真实生成")
            await p.ui_test_image()
            await asyncio.gather(*list(p.jobs))
            assert p.store.get("image_test", "latest")["image_status"] == "已生成"
            assert prompts[-1].endswith(p.config.diary.image_style)
            self.result.update(stage="空间随笔真实配图")
            day = now.date().isoformat()
            p.diary_publisher.images.put("diary_publication", {"id":day,"status":"草稿","content":"窗边木桌上有一杯热茶，旁边摊开一本书。"})
            await p.diary_publisher.illustrate(day, now, "窗边木桌的热茶与书")
            assert prompts[-1].endswith(p.config.diary.image_style)

            event = p.engine.record_experience(ExperienceInput(summary="折好一枚三角书角书签，夹在摊开的书页上", source="simulated", occurred_at=now.isoformat(), evidence="生活图片实机测试", details={"形状":"三角书角，平整折痕", "地点":"窗边木桌"}), stream, now)
            self.result.update(stage="生成并发送追问图片")
            await p.show_life("send", event["id"], stream_id=stream)
            await asyncio.gather(*list(p.jobs))
            row = p.store.get("experience_image", event["id"])
            assert row.get("image_file"), row.get("image_error")
            with Image.open(folder / row["image_file"]) as im: assert im.size == (1024,768), im.size
            assert prompts[-1].endswith(p.config.diary.chat_image_style)
            original = row["image_file"]
            count = len(prompts)
            p.config.diary.chat_image_style = "自然生活照片，聊天新风格，明亮自然光，横向4:3"
            await p.show_life("send", event["id"], stream_id=stream)
            assert p.store.get("experience_image", event["id"])["image_file"] == original
            assert len(prompts) == count
            try: await p.show_life("send", event["id"], stream_id="other")
            except ValueError: pass
            else: raise AssertionError("聊天归属未校验")
            if case == "revision":
                self.result.update(stage="更正经历后重新生成图片")
                p.engine.revise_experience(event["id"], {"形状": "心形折纸书签"}, "折好一枚心形书签，放在窗边木桌上", "实机更正形状验证", stream, p.now())
                job = await p.show_life("send", event["id"], stream_id=stream)
                assert "task_id" in job, job
                await asyncio.gather(*list(p.jobs))
                assert p.store.get("job", job["task_id"])["status"] == "已完成"
                revised = p.store.get("experience_image", event["id"])
                assert revised["image_file"] != original
                assert "心形" in revised["content"] and "心形" in revised["image_prompt"]
                self.result.update(status="已完成", result={"updated_prompt": revised["image_prompt"], "regenerated": True, "deliveries": len(p.store.all("image_delivery")), "folder": str(folder)})
                return
            modes = []
            for mode in ("主动表达", "自然交流"):
                self.result.update(stage=mode+"图文发送")
                event = p.engine.record_experience(ExperienceInput(summary="折好一枚三角书角书签，放在木桌上", source="simulated", occurred_at=now.isoformat(), evidence="图片风格实机测试"), stream, now)
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
                assert prompts[-1].endswith(p.config.diary.chat_image_style)
                modes.append({"mode":mode,"text":receipt["sent_text"],"image_message_id":receipt["image_message_id"]})
            assert len(p.living.receipts.sent_times(stream,now.tzinfo)) == 2
            assert len(p.store.all("image_delivery")) == 4
            self.result.update(status="已完成",result={"size":[1024,768],"modes":modes,"image_deliveries":4,"quota":2,"reuse":True,"ownership":True,"request_styles":[x.split("画面风格：")[-1] for x in prompts],"folder":str(folder)})
        except Exception as error:
            self.result.update(status="失败",error=str(error))
            self.ctx.logger.exception("生活图片实机测试失败")
        finally:
            if "original_request" in locals(): cls.request = original_request
            self.active=None
            await p.on_unload()


def create_plugin():
    return ImageProbe()
