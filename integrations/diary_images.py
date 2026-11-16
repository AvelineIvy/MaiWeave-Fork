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


class CharacterDraft(BaseModel):
    appearance: str = Field(min_length=1, max_length=800)
    clothing: str = Field(min_length=1, max_length=400)


class IllustratedText(BaseModel):
    content: str = Field(min_length=1)
    image_prompt: str = Field(min_length=1, max_length=1200)
    character_visible: bool = False


IMAGE_INSTRUCTION = "输出 JSON：content（正文）、image_prompt（1200字以内的中文画面描述）。配图选正文中的一个生活瞬间，着重场景、物件、光线与氛围，具体细节取自素材。"


class DiaryImages:
    request_timeout = 180
    download_timeout = 60

    def __init__(self, engine, context, config):
        self.store, self.ctx, self.config = engine.store, context, config
        with self.store.db:
            for category in ("diary", "diary_publication", "experience_image", "image_test"):
                for row in self.store.all(category):
                    if row.get("image_status") == "生成中":
                        row.update(image_status="生成失败", image_error="配图已中断，可以重新生成")
                        self.store.put(category, row)

    def instruction(self):
        text = IMAGE_INSTRUCTION
        if self.config.diary.image_character_enabled:
            text += ("同时输出 character_visible 布尔值：画面包含麦麦本人时为 true，物件或风景特写为 false。"
                     "根据本次展示要求和生活素材选择构图：看书签、饭菜等物件时聚焦物件；"
                     "看麦麦本人、做某事的样子或合照时让麦麦入镜。人物只表现素材中的活动，外貌由角色卡统一提供。")
        return text

    def character_prompt(self, visible):
        config = self.config.diary
        if not config.image_character_enabled or not visible:
            return ""
        parts = ["画面中的麦麦使用以下角色设定："]
        if config.image_character_appearance.strip():
            parts.append("稳定外貌：" + config.image_character_appearance.strip())
        if config.image_character_clothing.strip():
            parts.append("常用穿着参考，按当前活动调整：" + config.image_character_clothing.strip())
        return "\n".join(parts) if len(parts) > 1 else ""

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

    async def character_draft(self):
        personality = await self.ctx.config.get("personality")
        prompt = ("根据下面的 MaiBot 人设，为图片生成整理一张角色外观卡，输出 JSON："
                  "appearance（稳定外貌，800字以内）和 clothing（常用穿着，400字以内）。"
                  "沿用人设明确的外貌与身份；缺少的外貌、服装细节可以按人设补成一份供用户修改的建议。"
                  "只描述可见特征，保持简洁，人物性格用于选择衣着气质。\n人设："
                  + json.dumps(personality, ensure_ascii=False)[:6000])
        return await generate(self.ctx, self.store, self.config, "配图提示词", prompt, CharacterDraft)

    def request(self, prompt, reference=""):
        config = self.config.diary
        payload = {"model": config.image_model, "prompt": prompt, "n": 1, "size": "1024x768"}
        endpoint = "/images/generations"
        content_type = "application/json"
        data = json.dumps(payload).encode()
        if reference:
            if not reference.startswith(("http://", "https://")):
                raise ValueError("参考图片地址请填写 HTTP(S) 图片直链")
            with urlopen(reference, timeout=self.download_timeout) as response:
                original = response.read()
            with Image.open(io.BytesIO(original)) as picture:
                output = io.BytesIO()
                picture.convert("RGB").save(output, format="PNG")
            boundary = "maiweave-" + uuid4().hex
            parts = []
            for key, value in payload.items():
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="character.png"\r\nContent-Type: image/png\r\n\r\n').encode() + output.getvalue() + b"\r\n")
            data = b"".join(parts) + f"--{boundary}--\r\n".encode()
            endpoint = "/images/edits"
            content_type = "multipart/form-data; boundary=" + boundary
        request = Request(config.image_base_url.rstrip('/') + endpoint, data=data,
                          headers={"Authorization": "Bearer " + config.image_api_key, "Content-Type": content_type})
        try:
            with urlopen(request, timeout=self.request_timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            if reference and error.code in {404, 405}:
                raise RuntimeError("当前图片生成服务未提供参考图接口 /images/edits，请更换支持此接口的服务或关闭角色参考图") from error
            raise RuntimeError(f"图片生成服务返回 HTTP {error.code}：{error.read().decode('utf-8', errors='replace')[:1000]}") from error
        item = result["data"][0]
        if item.get("b64_json"):
            data = base64.b64decode(item["b64_json"], validate=True)
        else:
            with urlopen(item["url"], timeout=self.download_timeout) as response:
                data = response.read()
        return data, result.get("usage", {})

    async def illustrate(self, category, day, now, prompt=""):
        if not self.config.diary.image_service_enabled:
            raise ValueError("图片生成服务已关闭，请在模型设置中开启")
        row = self.editable(category, day)
        config = self.config.diary
        if not all(value.strip() for value in (config.image_base_url, config.image_api_key, config.image_model)):
            raise ValueError("请在模型设置的图片生成服务中填写生图接口地址、API Key 和模型")
        supplied_prompt = prompt.strip()
        prompt = supplied_prompt or row.get("image_prompt", "")
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
            decide_character = (config.image_character_enabled and category != "image_test"
                                and (bool(supplied_prompt) or "image_character_visible" not in row))
            if not prompt or decide_character:
                instructions = self.instruction() + "正文沿用下面的内容，只补充画面描述：" + row["content"][:4000]
                if prompt:
                    instructions += "\n本次画面描述：" + prompt
                if row.get("image_request"):
                    instructions += "\n本次展示要求：" + row["image_request"][:300]
                idea = await generate(self.ctx, self.store, self.config, "配图提示词", instructions, IllustratedText)
                prompt = prompt or idea.image_prompt
                if config.image_character_enabled:
                    row["image_character_visible"] = idea.character_visible
                else:
                    row.pop("image_character_visible", None)
            row["image_prompt"] = prompt
            style = config.chat_image_style if category == "experience_image" else config.image_style
            character = self.character_prompt(row.get("image_character_visible", False)) if category != "image_test" else ""
            full_prompt = prompt + ("\n" + character if character else "") + "\n画面风格：" + style
            if config.image_negative_prompt.strip():
                full_prompt += "\n负面提示词（画面中避免）：" + config.image_negative_prompt.strip()
            reference = ""
            if config.image_character_enabled and config.image_character_reference_enabled and row.get("image_character_visible") and category != "image_test":
                reference = config.image_character_reference_url.strip()
                if not reference:
                    raise ValueError("请在麦麦角色卡中填写参考图片地址")
                full_prompt += "\n参考图用于保持麦麦的外貌，动作、场景和构图按本次画面描述，穿着结合当前活动。"
                usage["task"] = "images/edits"
            data, tokens = await asyncio.to_thread(self.request, full_prompt, reference) if reference else await asyncio.to_thread(self.request, full_prompt)
            if len(data) > self.config.retention.image_max_mb * 1024 * 1024:
                raise ValueError("生成的图片超过配图容量上限，请调整容量设置")
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime, extension = "image/png", "png"
            elif data.startswith(b"\xff\xd8"):
                mime, extension = "image/jpeg", "jpg"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime, extension = "image/webp", "webp"
            else:
                raise ValueError("图片生成服务返回的内容不是 PNG、JPEG 或 WebP 图片")
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
