"""按 MaiBot 人物身份保存近期陈述与后续活动。"""

import re
from uuid import uuid4
from datetime import timedelta


class Presence:
    def __init__(self, living):
        self.living = living
        self.engine = living.engine
        self.store = self.engine.store
        self.offered = {}

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
            "准备睡觉": r"(?:@\S+\s+)?(?:我)?(?:先|准备|要)?(?:去睡了|睡觉了|去睡觉了)(?:啊|呀|啦|哦|哈)?(?:[，,。！!\s]+晚安)?",
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
            if record["status"] == "准备睡觉" and record.get("elsewhere"):
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
        """把群里遇见同一人的机会留给当前群聊。"""
        receipts = self.living.receipts
        if any(r.get("mode") == "群聊接话" and r["outcome"] == "MaiBot 已受理" for r in self.store.all("share_receipt")):
            await receipts.refresh(now)
        info = message.get("message_info", {})
        text = (message.get("processed_plain_text") or "").strip()
        if not info.get("group_info") or not text or text.startswith("/") or not self.living.config.proactive.enabled or not self.living.config.proactive.group_enabled:
            return
        user = str(info.get("user_info", {}).get("user_id", ""))
        person = self.living.person_key(message["session_id"], user)
        activity = {"session_id": message["session_id"], "user_id": user, "person_id": person,
                    "at": now.isoformat(), "status": "pending"}
        state = self.store.find("person_state", person) if person else None
        if (state and state["status"] == "准备睡觉" and not state.get("elsewhere")
                and self.engine.time(state["said_at"]) < now < self.engine.time(state["expires_at"])):
            state["elsewhere"] = {**activity, "kind": "sleep", "public": state["session_id"] == message["session_id"]}
            with self.store.db:
                self.store.put("person_state", state)
        streams = await self.living.ctx.chat.get_all_streams(platform="all_platforms")
        for stream in streams:
            if (stream.get("group_id") or stream.get("is_group_session")
                    or str(stream.get("user_id", "")) != user or stream.get("platform") != message.get("platform")
                    or (stream.get("account_id") and message.get("account_id") and stream["account_id"] != message["account_id"])):
                continue
            recent = receipts.context(stream["stream_id"], now)
            if not recent:
                continue
            row = self.store.get("share_receipt", recent[0]["id"])
            if (row.get("outcome") != "实际已发送" or row.get("followup", "waiting") != "waiting"
                    or row.get("observed_at") or row.get("elsewhere") or row.get("record_id", "").startswith("elsewhere:")):
                continue
            row["elsewhere"] = {**activity, "kind": "unanswered", "public": False}
            with self.store.db:
                self.store.put("share_receipt", row)

    def opportunities(self, session, user, now):
        if not self.living.config.proactive.group_enabled:
            return []
        result = []
        for category in ("person_state", "share_receipt"):
            for row in self.store.all(category):
                activity = row.get("elsewhere", {})
                if (activity.get("status") != "pending" or activity["session_id"] != session
                        or activity["user_id"] != user
                        or not timedelta() <= now - self.engine.time(activity["at"]) < timedelta(minutes=30)):
                    continue
                if category == "share_receipt" and (row.get("observed_at") or row.get("followup", "waiting") != "waiting"):
                    continue
                if category == "person_state" and self.engine.time(row["expires_at"]) <= now:
                    continue
                result.append((category, row))
        return result

    def encounter_context(self, session, user, now, group, *, offer=True):
        if not self.living.config.proactive.enabled or self.living.sleeping(now) or self.living.quiet_sleep(now, session):
            return []
        sources = self.opportunities(session, user, now) if group and offer else []
        if sources:
            public_sleep = any(row["elsewhere"].get("kind") == "sleep" and row["elsewhere"].get("public") for _, row in sources)
            return [{"stage": "群里遇见", "user_id": user,
                     "observation": "这个人先前在本群说去睡了，现在又在群里发言" if public_sleep else "刚在群里遇见这个人，可以顺势打个招呼",
                     "intimacy": self.living.intimacy(session, user),
                     "guide": "结合当前话题决定是否轻轻接一句，也可以沉默；公开表达围绕眼前的群聊。"}]
        person = self.living.person_key(session, user)
        incoming = self.living.receipts.incoming.get((session, user))
        for row in reversed(self.store.all("share_receipt")):
            if row.get("mode") != "群聊接话" or not person or row.get("encounter_person") != person:
                continue
            if group and row["session"] != session:
                continue
            if not timedelta() <= now - self.engine.time(row["created"]) < timedelta(hours=2):
                continue
            if (not offer and row["outcome"] == "MaiBot 已受理" and row["session"] == session
                    and incoming and self.engine.time(incoming["at"]) <= self.engine.time(row["created"])):
                return [{"stage":"本轮群聊", "user_id":user, "guide":row["topic"]}]
            if (row.get("sent_at") and row.get("sent_text") and incoming
                    and self.engine.time(incoming["at"]) > self.living.receipts.time(row["sent_at"], now.tzinfo)):
                return [{"stage":"接着聊", "user_id":user, "said":row["sent_text"][:300],
                         "guide":"这是之前在共同群里实际说过的话。判断当前发言是否在回应它；相关就在当前聊天接着说，换话题就聊新话题。"}]
        return []

    def offered_context(self, session, user, now, included):
        self.offered[session] = [(category, row["id"]) for category, row in self.opportunities(session, user, now)] if included else []

    def observe_planner(self, session, items, now):
        sources = self.offered.get(session, [])
        if not sources:
            return
        calls = {item["tool_call"]["call_id"]: "reply" for item in items
                 if item.get("item_type") == "FunctionCallItem" and item.get("tool_call", {}).get("func_name") == "reply"}
        if not calls and any(item.get("item_type") == "FunctionCallItem" and item.get("tool_call", {}).get("func_name") != "stop" for item in items):
            return
        self.offered.pop(session, None)
        activity = None
        with self.store.db:
            for category, key in sources:
                row = self.store.get(category, key)
                activity = row["elsewhere"]
                activity.update(status="handled" if calls else "silent", decided_at=now.isoformat())
                self.store.put(category, row)
            if calls:
                self.store.put("share_receipt", {"id":"encounter:"+uuid4().hex, "session":session, "created":now.isoformat(),
                    "mode":"群聊接话", "outcome":"MaiBot 已受理", "delivery":"等待本轮群聊回复回执", "calls":calls,
                    "encounter_person":self.living.person_key(session, activity["user_id"]), "encounter_user":activity["user_id"],
                    "topic":"本轮在群里遇见对方，结合当前话题自然接话。公开表达围绕眼前群聊，亲密程度沿用当前人物关系。"})
