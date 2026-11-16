"""把具体生活经历展示给当前聊天对象，复用图片与发送回执。"""
import asyncio
import json
from datetime import datetime
from uuid import uuid4

from .diary_images import DiaryImages
from .chat_context import brief


class LifeImages:
    def __init__(self, engine, ctx, config):
        self.engine, self.ctx, self.config = engine, ctx, config
        self.store = engine.store
        self.images = DiaryImages(engine, ctx, config)

    @staticmethod
    def content(event):
        return json.dumps(brief({key: event[key] for key in ("summary", "details", "source")}, 600, 6), ensure_ascii=False)

    def reusable(self, event, image):
        return bool(image.get("image_file") and image.get("content") == self.content(event))

    def candidates(self, session):
        rows = sorted(self.engine.experiences(session), key=lambda row: row["occurred_at"], reverse=True)[:10]
        return [{"id": row["id"], "summary": row["summary"][:200], "occurred_at": row["occurred_at"],
                 "has_image": self.reusable(row, self.store.find("experience_image", row["id"]) or {})} for row in rows]

    async def requested(self, experience_id, session, now, *, framing=""):
        """追问图片的后台任务失败时，在原聊天告知结果。"""
        self.engine.owned("experience", experience_id, session)
        try:
            return await self.send(experience_id, session, now, framing=framing)
        except Exception:
            result = await self.ctx.send.text("这次图片没发出来，可以稍后再让我试试。", session,
                return_details=True, storage_message=True, sync_to_maisaka_history=True)
            if not result["sent"]:
                self.ctx.logger.error("生活图片失败提示发送失败：%s", result.get("error"))
            raise

    async def send(self, experience_id, session, now, *, share_id="", framing=""):
        if not self.config.diary.chat_images_enabled:
            raise ValueError("聊天图片已关闭，可在「角色与图片 → 图片用在哪里」中开启")
        event = self.engine.owned("experience", experience_id, session)
        image = self.store.find("experience_image", experience_id)
        content = self.content(event)
        if image and image["content"] != content:
            image = self.images.remove("experience_image", experience_id)
            image["content"] = content
            self.images.put("experience_image", image)
        if not image:
            image = {"id": experience_id, "status": "草稿", "session_id": event["session_id"],
                     "content": content}
            self.images.put("experience_image", image)
        if not image.get("image_file"):
            if framing:
                image["image_request"] = framing[:300]
                image.pop("image_prompt", None)
                image.pop("image_character_visible", None)
                self.images.put("experience_image", image)
            image = await self.images.illustrate("experience_image", experience_id, now)
        delivery = {"id": uuid4().hex, "at": now.isoformat(), "session_id": session,
                    "experience_id": experience_id, "share_id": share_id, "status": "发送中",
                    "description": image["image_prompt"][:600], "error": ""}
        self.images.put("image_delivery", delivery)
        try:
            encoded = await asyncio.to_thread(self.images.encoded, image, 768)
            result = await self.ctx.send.image(encoded, session, return_details=True,
                                               storage_message=True, sync_to_maisaka_history=True)
            if not result["sent"]:
                raise RuntimeError(result.get("error") or "图片发送失败")
            delivery.update(status="已发送", message_id=result["message_id"], at=datetime.now(now.tzinfo).isoformat())
        except (Exception, asyncio.CancelledError) as error:
            delivery.update(status="发送失败", error=str(error) or "图片发送已中断")
            raise
        finally:
            self.images.put("image_delivery", delivery)
        return {"status": "已发送", "message_id": delivery["message_id"], "experience_id": experience_id,
                "description": delivery["description"]}

    def context(self, session):
        rows = [row for row in self.store.all("image_delivery") if row["session_id"] == session and row["status"] == "已发送"]
        return [{key: row[key] for key in ("experience_id", "description", "at")} for row in rows[-2:]]
