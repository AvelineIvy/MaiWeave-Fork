"""空间随笔与聊天共用的图片生成、保存和用量记录。"""

import asyncio
import base64
import json
import io
from PIL import Image
from datetime import datetime
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from .usage import generate
from ..core.image_retention import ImageRetention


class IllustratedText(BaseModel):
    content: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1, max_length=1200)


IMAGE_INSTRUCTION = "输出 JSON：content（正文）、image_prompt（1200字以内的中文画面描述）。配图选正文中的一个生活瞬间，着重场景、物件、光线与氛围，具体细节取自素材。"


class DiaryImages:
    def __init__(self, engine, context, config):
        self.store, self.ctx, self.config = engine.store, context, config
        with self.store.db:
            for category in ("diary", "diary_publication", "experience_image", "image_test"):
                for row in self.store.all(category):
                    if row.get("image_status") == "生成中":
                        row.update(image_status="生成失败", image_error="配图已中断，可以重新生成")
                        self.store.put(category, row)

    def put(self, category, row):
        with self.store.db:
            self.store.put(category, row)

    def encoded(self, row, size=512):
        with Image.open(Path(self.ctx.paths.data_dir) / row["image_file"]) as image:
            image.thumbnail((size, size))
            output = io.BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=80)
        return base64.b64encode(output.getvalue()).decode()

    def preview(self, row, size=512):
        return "data:image/jpeg;base64," + self.encoded(row, size) if row.get("image_file") else ""

    def editable(self, category, day):
        row = self.store.get(category, day)
        if row.get("image_status") == "生成中" or row["status"] in {"生成中", "发布中", "已发布", "待核对"}:
            raise ValueError("请等待当前操作完成，已发布的文案可在发布记录中查看")
        if not row.get("content"):
            raise ValueError("请先完成正文")
        return row

    def request(self, prompt):
        config = self.config.diary
        payload = {"model": config.image_model, "prompt": prompt, "n": 1, "size": "1024x768"}
        request = Request(config.image_base_url.rstrip('/') + '/images/generations',
                          data=json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + config.image_api_key, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=180) as response:
                result = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"生图服务返回 HTTP {error.code}：{error.read().decode('utf-8', errors='replace')[:1000]}") from error
        item = result["data"][0]
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"], validate=True)
        else:
            with urlopen(item["url"], timeout=60) as response:
                data = response.read()
        return data, result.get("usage", {})

    async def illustrate(self, category, day, now, prompt=""):
        if not self.config.diary.image_service_enabled:
            raise ValueError("生图服务已关闭，请在模型设置中开启")
        row = self.editable(category, day)
        config = self.config.diary
        if not all(value.strip() for value in (config.image_base_url, config.image_api_key, config.image_model)):
            raise ValueError("请在模型设置的生图服务中填写生图接口地址、API Key 和模型")
        prompt = prompt.strip() or row.get("image_prompt", "")
        if len(prompt) > 1200:
            raise ValueError("画面描述请控制在1200字以内")
        row.update(image_status="生成中", image_error="")
        self.put(category, row)
        started = perf_counter()
        usage = {"id": uuid4().hex, "at": datetime.now(ZoneInfo(self.config.life.timezone)).isoformat(),
                 "feature": {"experience_image": "生活图片", "image_test": "生图测试"}.get(category, "空间配图"), "task": "images/generations", "model": config.image_model,
                 "status": "失败", "error": "", "prompt_tokens": None, "completion_tokens": None,
                 "total_tokens": None, "prompt_cache_hit_tokens": None}
        try:
            if not prompt:
                idea = await generate(self.ctx, self.store, self.config, "空间文案" if category == "diary_publication" else "生活日记",
                    IMAGE_INSTRUCTION + "正文沿用下面的内容，只补充画面描述：" + row["content"][:4000], IllustratedText)
                prompt = idea.image_prompt
            row["image_prompt"] = prompt
            style = "自然生活照片，随手拍的构图，自然光，具体物件清楚，横向4:3" if category == "experience_image" else config.image_style
            data, tokens = await asyncio.to_thread(self.request, prompt + "\n画面风格：" + style)
            if len(data) > self.config.retention.image_max_mb * 1024 * 1024:
                raise ValueError("生成的图片超过配图容量上限，请调整容量设置")
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime, extension = "image/png", "png"
            elif data.startswith(b"\xff\xd8"):
                mime, extension = "image/jpeg", "jpg"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime, extension = "image/webp", "webp"
            else:
                raise ValueError("生图服务返回的内容不是 PNG、JPEG 或 WebP 图片")
            relative = f"diary_images/{category}-{uuid4().hex}.{extension}"
            path = Path(self.ctx.paths.data_dir) / relative
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_bytes, data)
            previous = row.get("image_file")
            row.update(image_status="已生成", image_file=relative, image_mime=mime, image_model=config.image_model,
                       image_content=row["content"], image_at=now.isoformat())
            self.put(category, row)
            if previous:
                await asyncio.to_thread((Path(self.ctx.paths.data_dir) / previous).unlink)
            usage.update(status="成功", prompt_tokens=tokens.get("input_tokens", tokens.get("prompt_tokens")),
                         completion_tokens=tokens.get("output_tokens", tokens.get("completion_tokens")), total_tokens=tokens.get("total_tokens"))
        except (Exception, asyncio.CancelledError) as error:
            row.update(image_status="生成失败", image_error=str(error) or "配图已中断")
            usage["error"] = row["image_error"]
            raise
        finally:
            self.put(category, row)
            usage["seconds"] = round(perf_counter() - started, 3)
            self.put("model_usage", usage)
        ImageRetention(self.store, self.config, self.ctx.paths.data_dir).clean(now)
        return self.store.get(category, day)

    def remove(self, category, day):
        row = self.editable(category, day)
        if row.get("image_file"):
            (Path(self.ctx.paths.data_dir) / row["image_file"]).unlink()
        for key in list(row):
            if key.startswith("image_"):
                row.pop(key)
        self.put(category, row)
        return row
