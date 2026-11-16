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
            p.config.diary.image_character_enabled = True
            p.config.diary.image_character_appearance = "成年女性，黑色齐肩发，棕色眼睛"
            p.config.diary.image_character_clothing = "米白色针织衫"
            cls = type(p.diary_publisher.images)
            original_request = cls.request
            prompts = []
            def capture(images, prompt):
                prompts.append(prompt)
                return original_request(images, prompt)
            cls.request = capture
            checks = []
            for summary, framing, visible in [
                ("折好一枚三角书角书签，放在窗边的木桌上", "只看书签特写", False),
                ("在窗边坐着折一枚三角书签，双手正在压平折痕", "想看看麦麦本人折纸的样子，人物入镜", True),
            ]:
                self.result.update(stage=framing)
                event = p.engine.record_experience(ExperienceInput(summary=summary, source="simulated", occurred_at=now.isoformat(), evidence="角色卡实机验证"), stream, now)
                job = await p.show_life("send", event["id"], framing=framing, stream_id=stream)
                await asyncio.gather(*list(p.jobs))
                assert p.store.get("job", job["task_id"])["status"] == "已完成", p.store.get("job", job["task_id"])
                row = p.store.get("experience_image", event["id"])
                assert row["image_character_visible"] == visible, row
                assert ("黑色齐肩发" in prompts[-1]) == visible, prompts[-1]
                checks.append({"scene":framing,"visible":row["image_character_visible"],"prompt":prompts[-1],"file":row["image_file"]})
            original_file = row["image_file"]
            count = len(p.store.all("model_usage"))
            p.config.diary.image_character_appearance = "成年女性，棕色长发，绿色眼睛"
            await p.show_life("send", event["id"], stream_id=stream)
            assert p.store.get("experience_image", event["id"])["image_file"] == original_file
            assert len(p.store.all("model_usage")) == count
            try: await p.show_life("send", event["id"], stream_id="other")
            except ValueError: pass
            else: raise AssertionError("聊天归属校验失败")
            self.result.update(stage="空间自动配图共用角色卡")
            day = now.date().isoformat()
            with p.store.db:
                p.store.put("diary", {"id":day,"status":"已生成","material":{"经历":[{"summary":"今天在窗边折纸，给自己留了一张双手捧着折好书签的人物画面。", "source":"simulated","session_id":""}]}})
            p.config.diary.image_enabled = True
            row = await p.diary_publisher.prepare(day, now)
            assert row["image_status"] == "已生成", row
            usage = p.store.all("model_usage")[count:]
            assert len([r for r in usage if r["task"] != "images/generations"]) == 1, usage
            if row["image_character_visible"]:
                assert "棕色长发" in prompts[-1], prompts[-1]
            checks.append({"scene":"空间自动配图", "visible":row["image_character_visible"],"prompt":prompts[-1],"file":row["image_file"]})
            assert len(p.store.all("image_delivery")) == 3
            self.result.update(status="已完成", result={"checks":checks,"reuse":True,"ownership":True,"deliveries":3,"folder":str(folder)})
        except Exception as error:
            self.result.update(status="失败",error=str(error))
            self.ctx.logger.exception("生活图片实机测试失败")
        finally:
            if "original_request" in locals(): cls.request = original_request
            self.active=None
            await p.on_unload()


def create_plugin():
    return ImageProbe()
