"""按 MaiBot 人物身份保存近期陈述与后续活动。"""

import re
import json

from ..core.models import ShareDecision
from ..integrations.usage import generate
from datetime import timedelta


class Presence:
    def __init__(self, living):
        self.living = living
        self.engine = living.engine
        self.store = self.engine.store

    def declare(self, session, user, status, now, until=""):
        person = self.living.person_key(session, user)
        hours = {"准备睡觉": 12, "忙碌": 4, "外出": 6, "空闲": 2}
        if status not in hours:
            raise ValueError("请选择准备睡觉、忙碌、外出或空闲")
        if not person:
            raise ValueError("请先关联当前发言人的 MaiBot 人物身份")
        expires = self.engine.time(until) if until else now + timedelta(hours=hours[status])
        if expires <= now:
            raise ValueError("近况结束时间应晚于当前时间")
        record = {"id": person, "status": status, "source": "reported", "session_id": session,
                  "user_id": user, "said_at": now.isoformat(),
                  "expires_at": expires.isoformat(),
                  "activity": {session: {"user_id": user, "at": now.isoformat()}}}
        with self.store.db:
            self.store.put("person_state", record)
        return record

    def receive(self, session, user, text, now):
        if not text.strip() or text.lstrip().startswith("/"):
            return
        # 明确的当前陈述即时记录，其余表达由 Planner 结合上下文调用工具。
        statements = {
            "准备睡觉": r"(?:我)?(?:先|准备|要)?(?:去睡了|睡觉了|去睡觉了)(?:啊|呀|啦|哦|哈)?",
            "忙碌": r"(?:我)?(?:先|要)?(?:去忙了|忙去了|去工作了|去开会了)",
            "外出": r"(?:我)?(?:先|要)?(?:出门了|出去了)",
            "空闲": r"(?:我)?(?:刚|已经)?(?:忙完了|回来了|起床了|睡醒了|下班了)",
        }
        status = next((status for status, pattern in statements.items()
                       if re.fullmatch(pattern, text.strip().rstrip("。！!～~"))), None)
        if status:
            self.declare(session, user, status, now)
        person = self.living.person_key(session, user)
        record = self.store.find("person_state", person) if person else None
        if not record:
            return
        if self.engine.time(record["expires_at"]) <= now:
            with self.store.db:
                self.store.delete("person_state", person)
            return
        record["activity"][session] = {"user_id": user, "at": now.isoformat()}
        record["activity"] = {key: value for key, value in record["activity"].items()
                              if now - self.engine.time(value["at"]) < timedelta(minutes=30)}
        with self.store.db:
            self.store.put("person_state", record)

    def context(self, session, now):
        result = []
        for record in self.store.all("person_state"):
            activity = record["activity"].get(session)
            if (self.engine.time(record["expires_at"]) <= now or not activity
                    or now - self.engine.time(activity["at"]) >= timedelta(minutes=30)):
                continue
            later = self.engine.time(activity["at"]) > self.engine.time(record["said_at"])
            result.append({"user_id": activity["user_id"], "person_id": record["id"],
                           "statement": record["status"], "said_at": record["said_at"], "source": "reported",
                           "expires_at": record["expires_at"],
                           "activity": "此后仍在发言" if later else "刚刚作出陈述",
                           "observed_at": activity["at"], "activity_source": "observed",
                           "same_chat": record["session_id"] == session})
        return result

    def targets(self, session, now):
        """接收资料关联的人物近况，供主动交流选择时机。"""
        people = {p.get("person_id") for p in self.store.all("profile") if p["session_id"] == session}
        return [{"person_id": r["id"], "status": r["status"], "said_at": r["said_at"], "expires_at": r["expires_at"],
                 "active_here": bool(r["activity"].get(session) and
                    self.engine.time(r["activity"][session]["at"]) > self.engine.time(r["said_at"]) and
                    now - self.engine.time(r["activity"][session]["at"]) < timedelta(minutes=30))}
                for r in self.store.all("person_state") if r["id"] in people and self.engine.time(r["expires_at"]) > now]

    async def observe_group(self, message, now):
        """将同一人在群里的发言关联到尚未回复的主动私聊。"""
        info = message.get("message_info", {})
        text = (message.get("processed_plain_text") or "").strip()
        if not info.get("group_info") or not text or text.startswith("/"):
            return
        if not self.living.config.proactive.enabled:
            return
        user = str(info.get("user_info", {}).get("user_id", ""))
        streams = await self.living.ctx.chat.get_all_streams(platform="all_platforms")
        for stream in streams:
            if (stream.get("group_id") or stream.get("is_group_session")
                    or str(stream.get("user_id", "")) != user or stream.get("platform") != message.get("platform")
                    or (stream.get("account_id") and message.get("account_id") and stream["account_id"] != message["account_id"])):
                continue
            recent = self.living.receipts.context(stream["stream_id"], now)
            if not recent:
                continue
            row = self.store.get("share_receipt", recent[0]["id"])
            if (row.get("outcome") != "实际已发送" or row.get("followup", "waiting") != "waiting" or row.get("observed_at")
                    or row.get("elsewhere") or row.get("record_id", "").startswith("elsewhere:")):
                continue
            row["elsewhere"] = {"session_id": message["session_id"], "user_id": user,
                                "at": now.isoformat(), "text": text[:300], "status": "pending"}
            with self.store.db:
                self.store.put("share_receipt", row)

    async def followup(self, session, now):
        """沿用主动交流时机，在原私聊决定是否轻轻打趣一次。"""
        recent = self.living.receipts.context(session, now)
        if not recent:
            return None
        row = self.store.get("share_receipt", recent[0]["id"])
        activity = row.get("elsewhere", {})
        if (activity.get("status") != "pending" or row.get("observed_at")
                or row.get("followup", "waiting") != "waiting"
                or not timedelta() <= now - self.engine.time(activity["at"]) < timedelta(minutes=30)):
            return None
        recipient = await self.living.share_recipient(session, "")
        history = await self.living.receipts.recent_chat(session)
        group_history = await self.living.receipts.recent_chat(activity["session_id"])
        guide = ("你主动私聊后，对方还没有在私聊回复，随后在共同群聊里说了话。判断现在是否适合回原私聊轻轻打趣一次。"
                 "输出 JSON：send（是否开口）、reason（原因）。普通关系更倾向等待，亲近关系可以表达一点想念和轻松的小情绪。"
                 "群里发言表示对方正在参与群聊；结合群消息内容，处理正事、求助或情绪低落时体谅对方。"
                 "开口时围绕看到对方冒泡这一件事，语气轻松，给对方自由回复的空间。"
                 f"当前时间：{now.isoformat()}。")
        budget = self.living.config.context.private_chars
        sections = [row.get("sent_text", row.get("topic", ""))[:600],
                    json.dumps({"这次群里发言": activity["text"], "最近群聊": list(reversed(group_history))}, ensure_ascii=False),
                    json.dumps(recipient, ensure_ascii=False),
                    json.dumps(list(reversed(history)), ensure_ascii=False)]
        labels = ["上次主动私聊：", "群里观察：", "彼此关系：", "最近私聊："]
        size = max(0, (budget - len(guide) - sum(map(len, labels)) - 4) // 4)
        prompt = guide + "\n".join(label + value[:size] for label, value in zip(labels, sections))
        decision = await generate(self.living.ctx, self.store, self.living.config, "分享判断", prompt, ShareDecision)
        row = self.store.get("share_receipt", row["id"])
        if row.get("observed_at") or row.get("followup", "waiting") != "waiting":
            return None
        row["elsewhere"].update(status="noticed" if decision.send else "waiting", reason=decision.reason,
                                decided_at=now.isoformat())
        with self.store.db:
            self.store.put("share_receipt", row)
        if not decision.send:
            return None
        intent = ("回到当前私聊轻轻打趣：刚看到对方在共同群聊冒泡，之前主动私聊尚未收到回复。"
                  "说一句自然、亲近程度合适的话，围绕看到对方出现这一件事。"
                  f"本次判断：{decision.reason}。关系参考：{json.dumps(recipient, ensure_ascii=False)[:600]}。")
        return ("elsewhere:" + row["id"], intent, None)
