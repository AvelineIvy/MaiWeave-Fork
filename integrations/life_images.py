"""把具体生活经历展示给当前聊天对象，复用图片与发送回执。"""
import asyncio
import json
import re
from datetime import datetime
from uuid import uuid4
from weakref import WeakValueDictionary

from .diary_images import DiaryImages
from .chat_context import brief


FINISH_IMAGE = "图片会自动发到当前聊天。还有文字要说时用 reply 并设 after_reply=stop，否则用无时间参数的 wait 结束本轮。"
SENT_IMAGE = "图片已发到当前聊天。还有文字要说时用 reply 并设 after_reply=stop，否则用无时间参数的 wait 结束本轮。"


def image_tool_relevant(items):
    """按本次来信提供图片工具，也接受 Planner 在本轮主动找图。"""
    for item in reversed(items):
        call = item.get("tool_call") or {}
        if call.get("func_name") == "tool_search":
            query = str(call.get("args", {}).get("query", ""))
            if re.search(r"maiweave_show_life|图片|照片|自拍|发图", query, re.I):
                return True
        if item.get("item_type") != "UserMessageItem":
            continue
        text = "".join(p.get("text", "") for p in item.get("parts", []) if p.get("type") == "text")
        if text.startswith(("<plugin_proactive_task", "<internal_", "<subagent_result")):
            return False
        header, _, body = text.partition(">")
        if header.startswith("<message ") and 'is_self_message="true"' not in header:
            return bool(re.search(r"想看|看看|瞅瞅|瞧瞧|给我看|给你看|什么样|啥样|长啥样|照片|图片|拍张|拍一|自拍|发.{0,4}(张|图|来|给我)|photo|selfie|picture", body, re.I))
    return True


def distinct_image_calls(items, allowed=None):
    """执行本轮提供的图片操作，同一批次中每张图片只发送一次。"""
    seen = set()
    kept = []
    rejected = None
    for item in items:
        call = item.get("tool_call") or {}
        if item.get("item_type") == "FunctionCallItem" and call.get("func_name") == "maiweave_show_life":
            args = call.get("args") or {}
            action = args.get("action")
            if allowed is not None and (action not in allowed or
                                        (action == "send" and not args.get("experience_id"))):
                rejected = item
                continue
            if action == "list" or (action == "send" and isinstance(args.get("experience_id"), str)):
                key = (action, args.get("experience_id") if action == "send" else "")
                if key in seen:
                    continue
                seen.add(key)
        kept.append(item)
    if rejected and not any(i.get("item_type") == "FunctionCallItem" for i in kept):
        # 只有失效图片操作时，用宿主的静默结束动作收尾。
        kept.append({**rejected, "tool_call": {**rejected["tool_call"], "func_name": "wait", "args": {}}})
    return kept


def current_image_candidates(items):
    """给 Planner 保留本轮的选图过程，上一轮的候选随消息翻篇。"""
    old = set()
    current = set()
    for item in items:
        if item.get("item_type") == "UserMessageItem":
            text = next((p.get("text", "") for p in item.get("parts", []) if p.get("type") == "text"), "")
            if (text.startswith("<message ") and 'is_self_message="true"' not in text.split(">", 1)[0]
                    or text.startswith(("<plugin_proactive_task", "<internal_", "<subagent_result"))):
                old.update(current)
                current.clear()
        elif item.get("item_type") == "FunctionCallItem":
            call = item["tool_call"]
            if call.get("func_name") == "maiweave_show_life" and call.get("args", {}).get("action") == "list":
                current.add(call["call_id"])
    if not old:
        return items
    return [i for i in items if (i.get("tool_call") or {}).get("call_id") not in old and i.get("call_id") not in old]


def image_request_stage(items, is_sent_image=None):
    """从本轮回执识别已列出的候选和已受理的发图。"""
    calls = {}
    stage = ""
    for item in items:
        kind = item.get("item_type")
        if kind == "UserMessageItem":
            text = next((p.get("text", "") for p in item.get("parts", []) if p.get("type") == "text"), "")
            header = text.split(">", 1)[0]
            incoming = text.startswith("<message ") and 'is_self_message="true"' not in header
            if incoming and stage == "sent" and is_sent_image:
                # 宿主同步 QQ 图片时，消息头可能只带回执编号。
                message = re.search(r'\bmsg_id="([^"]+)"', header)
                if message and is_sent_image(message[1]):
                    continue
            if incoming or text.startswith(("<plugin_proactive_task", "<internal_", "<subagent_result")):
                calls.clear()
                stage = ""
        elif kind == "FunctionCallItem":
            call = item.get("tool_call", {})
            if call.get("func_name") == "maiweave_show_life":
                calls[call.get("call_id")] = call.get("args", {}).get("action")
        elif kind == "FunctionCallOutputItem" and calls and item.get("call_id") in calls:
            output = item.get("output")
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except ValueError:
                    continue
            if isinstance(output, dict):
                action = calls[item["call_id"]]
                if action == "send" and output.get("status") in {"进行中", "正在生成，请稍等", "已发送"}:
                    stage = "sent"
                elif stage != "sent" and action == "list" and isinstance(output.get("items"), list):
                    stage = "listed" if output["items"] else "empty"
    return stage


class LifeImages:
    def __init__(self, engine, ctx, config):
        self.engine, self.ctx, self.config = engine, ctx, config
        self.store = engine.store
        self.images = DiaryImages(engine, ctx, config)
        self.pending_requests = set()
        self.planner_actions = {}
        self.send_locks = WeakValueDictionary()

    @staticmethod
    def content(event):
        return json.dumps(brief({key: event[key] for key in ("summary", "details", "source")}, 600, 6), ensure_ascii=False)

    def reusable(self, event, image):
        return bool(image.get("image_file") and image.get("content") == self.content(event))

    def candidates(self, session):
        if not self.config.diary.chat_images_enabled:
            return []
        rows = sorted((row for row in self.store.all("experience") if self.engine.visible(row, session)),
                      key=lambda row: self.engine.time(row["occurred_at"]), reverse=True)
        candidates = [{"id": row["id"], "summary": row["summary"][:200], "occurred_at": row["occurred_at"],
                       "has_image": self.reusable(row, self.store.find("experience_image", row["id"]) or {})} for row in rows]
        if not self.config.diary.image_service_enabled:
            candidates = [row for row in candidates if row["has_image"]]
        return candidates[:10]

    def tool_definitions(self, definitions, session, items=()):
        """按当前聊天可用的图片能力提供工具。"""
        enabled = self.config.life.enabled and self.config.diary.chat_images_enabled
        enabled = enabled and (self.config.diary.image_service_enabled or bool(self.candidates(session)))
        result = []
        for tool in definitions:
            function = tool.get("function", {})
            if function.get("name") == "maiweave_show_life":
                if not enabled or not image_tool_relevant(items):
                    continue
                stage = image_request_stage(items, lambda mid: self.sent_image(session, mid))
                if stage in {"empty", "sent"}:
                    continue
                if not self.config.diary.image_service_enabled:
                    tool = {**tool, "function": {**function, "description": "对方明确想看图片时展示已有生活图片。本轮已有候选就按 experience_id 调 send 一次，需要选图时才调 list。普通接话和文字分享使用 reply 或 wait。"}}
                if stage == "listed":
                    params = function["parameters"]
                    properties = {**params["properties"], "action": {"type": "string", "enum": ["send"]}}
                    tool = {**tool, "function": {**function, "description": "仅用于发送对方想看的照片。使用本轮候选中的 ID；讲述经历时直接用 reply。", "parameters": {**params, "properties": properties, "required": ["action", "experience_id"]}}}
            result.append(tool)
        return result

    def sent_image(self, session, message_id):
        return self.store.db.execute(
            """SELECT 1 FROM records WHERE category='image_delivery'
               AND json_extract(data,'$.session_id')=? AND json_extract(data,'$.message_id')=?
               AND json_extract(data,'$.status')='已发送' LIMIT 1""", (session, message_id)
        ).fetchone() is not None

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
        finally:
            self.pending_requests.discard((session, experience_id))

    async def send(self, experience_id, session, now, *, share_id="", framing="", since=None):
        if not self.config.diary.chat_images_enabled:
            raise ValueError("聊天图片已关闭，可在「插件配置 → 模型设置 → 图片用途」中开启")
        event = self.engine.owned("experience", experience_id, session)
        lock = self.send_locks.setdefault(experience_id, asyncio.Lock())
        if lock.locked() and since is None:
            since = now
        async with lock:
            if since is not None:
                for row in reversed(self.store.all("image_delivery")):
                    if (row["experience_id"] == experience_id and row["session_id"] == session
                            and (not share_id or row.get("share_id", "") in {"", share_id})
                            and row["status"] == "已发送" and self.engine.time(row["at"]) >= since):
                        return {"status": "已发送", "message_id": row["message_id"], "experience_id": experience_id,
                                "description": row["description"]}
            return await self._deliver(event, session, now, share_id, framing)

    async def _deliver(self, event, session, now, share_id, framing):
        experience_id = event["id"]
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
                image.pop("image_selfie", None)
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
