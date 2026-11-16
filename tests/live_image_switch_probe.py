"""在实机核验图片开关、工具定义与已有图片发送。"""
import asyncio
import shutil
import tomllib
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from .live_reading_probe import Probe


class ImageSwitchProbe(Probe):
    async def run(self, case):
        from maiweave_probe_target.core.models import ExperienceInput
        folder = Path(self.ctx.paths.data_dir) / uuid4().hex
        folder.mkdir()
        native = self.ctx
        class Context:
            paths = SimpleNamespace(data_dir=str(folder))
            def __getattr__(self, name):
                return getattr(native, name)
        p = self.create()
        root = Path(__file__).resolve().parent.parent / "ilworkcn_maiweave"
        cfg = tomllib.loads((root / "config.toml").read_text())
        cfg["life"].update(enabled=False, autonomous=False, schedule_enabled=False)
        cfg["diary"].update(image_service_enabled=False, chat_images_enabled=True)
        p.set_plugin_config(cfg)
        p._set_context(Context())
        try:
            await p.on_load()
            p.worker.cancel()
            with suppress(asyncio.CancelledError):
                await p.worker
            p.config.life.enabled = True
            streams = await native.chat.get_all_streams(platform="internal")
            stream = next(s for s in streams if s.get("user_id") == "owner" and not s.get("group_id"))["stream_id"]
            definitions = await native.tool.get_definitions()
            tools = [{"type": "function", "function": {"name": item["name"], "description": item["definition"]["description"], "parameters": item["definition"]["parameters_schema"]}} for item in definitions if item["name"] in {"maiweave_show_life", "maiweave_query"}]
            assert any(t.get("function", {}).get("name") == "maiweave_show_life" for t in tools), "原生工具列表缺少生活图片工具"
            async def visible(session=stream):
                result = await p.planner_context(session_id=session, items=[], tool_definitions=tools)
                return next((t for t in result["modified_kwargs"]["tool_definitions"] if t.get("function", {}).get("name") == "maiweave_show_life"), None)
            assert await visible() is None
            now = p.now()
            event = p.engine.record_experience(ExperienceInput(summary="图片开关测试：手工小兔子", occurred_at=now.isoformat(), source="simulated", evidence="测试使用已有图片"), stream, now)
            image_file = "diary_images/switch-test.png"
            (folder / "diary_images").mkdir(exist_ok=True)
            shutil.copy2(Path(__file__).parent / "demo-rabbit.png", folder / image_file)
            p.life_images.images.put("experience_image", {"id":event["id"], "status":"草稿", "session_id":stream, "content":p.life_images.content(event), "image_file":image_file, "image_prompt":"手工小兔子", "image_status":"已生成"})
            tool = await visible()
            assert tool and "已有" in tool["function"]["description"]
            assert await visible("another-chat") is None
            text = await native.send.text("【图片开关测试】生成服务已关闭，这次发送保存好的手工图片。", stream, return_details=True, storage_message=True, sync_to_maisaka_history=True)
            assert text["sent"], text
            sent = await p.show_life("send", event["id"], stream_id=stream)
            assert sent["status"] == "已发送"
            p.config.diary.chat_images_enabled = False
            assert await visible() is None
            p.config.diary.chat_images_enabled = True
            p.config.diary.image_service_enabled = True
            assert await visible("another-chat") is not None
            p.config.diary.image_service_enabled = False
            trial = await p.ui_character_trial()
            assert trial["enabled"] is False
            try:
                await p.ui_test_image(scene="自拍", prompt="手工小兔子")
            except ValueError as error:
                assert "开启图片生成服务" in str(error)
            else:
                raise AssertionError("关闭服务后仍启动了试画")
            assert not p.store.all("model_usage")
            self.result = {"status":"已完成", "checks":["原生工具定义读取", "无图片时隐藏", "已有图片时只提供复用", "聊天归属", "聊天关闭后隐藏", "重新开启恢复", "关闭试画", "关闭生成服务后实际发送已有图片"], "image_message_id":sent["message_id"], "stream":stream}
        except Exception as error:
            self.result = {"status":"失败", "error":str(error)}
            native.logger.exception("图片开关实机测试失败")
        finally:
            await p.on_unload()


def create_plugin():
    return ImageSwitchProbe()
