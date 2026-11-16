"""按生活日期整理角色的一天，保存日记与写作依据。"""

import asyncio
import json
from datetime import date, datetime, time, timedelta

from pydantic import BaseModel, Field
from ..integrations.chat_context import brief, dump
from ..core.models import PlanInput
from ..integrations.usage import generate
from .living import in_hours, next_bedtime


class DiaryMoment(BaseModel):
    source_ids: list[str]
    summary: str = Field(max_length=500)
    public_summary: str = Field(max_length=300)


class DiaryMoments(BaseModel):
    moments: list[DiaryMoment] = Field(default_factory=list, max_length=8)


def writing_material(material):
    """各类素材分别留出篇幅，整条选入模型上下文。"""
    result = {key: material[key] for key in ("日期", "记录截止") if key in material}
    budgets = {"活动": 4000, "经历": 5000, "状态变化": 1000, "梦境": 1500, "阅读笔记": 2000, "聊天片刻": 5000}
    for key, budget in budgets.items():
        result[key] = []
        for row in material.get(key, []):
            item = brief({k: v for k, v in row.items() if k != "sources"}, 500, 6)
            if len(dump([*result[key], item])) <= budget:
                result[key].append(item)
    return result


class DiarySlot(BaseModel):
    start: str
    end: str
    reason: str


class Diary:
    def __init__(self, engine, context, config):
        self.engine, self.ctx, self.config = engine, context, config
        with engine.store.db:
            for record in engine.store.all("diary"):
                if record["status"] == "生成中":
                    record.update(status="生成失败", error="上次写作随插件停止而中断")
                    engine.store.put("diary", record)

    def material(self, day, now):
        start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
        end = start + timedelta(days=1)
        store = self.engine.store
        plans = [p for p in store.all("plan") if self.engine.time(p["start"]) < min(end, now) and self.engine.time(p["end"]) > start]
        experiences = [e for e in store.all("experience") if start <= self.engine.time(e["occurred_at"]) < min(end, now)]
        states = [s for s in store.all("state_history") if start <= self.engine.time(s["at"]) < min(end, now)]
        dreams = [d for d in store.all("dream") if d["status"] == "已生成" and start <= self.engine.time(d["sleep_end"]) < min(end, now)]
        return {"阅读笔记": [{k: n[k][:500] for k in ("book_title", "chapter_title", "summary", "thoughts")} for n in store.all("book_note") if n["status"] == "已完成" and start <= self.engine.time(n["finished_at"]) < min(end, now)][-4:], "日期": day.isoformat(), "记录截止": min(end, now).isoformat(), "活动": plans, "经历": experiences,
                "状态变化": [{k: s[k] for k in ("at", "energy", "hunger", "mood", "stress") if k in s} for s in states], "梦境": dreams}

    async def chat_moments(self, day, now, material):
        start = datetime.combine(day, time.min, tzinfo=now.tzinfo)
        end = min(start + timedelta(days=1), now)
        sources = {}
        streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        remaining = 10000
        for stream in sorted(streams, key=lambda row: row.get("last_active_time", 0) or 0, reverse=True):
            if remaining < 500:
                break
            response = await self.ctx.message.get_by_time_in_chat(
                stream["stream_id"], str(start.timestamp()), str(end.timestamp()),
                limit=40, limit_mode="latest", filter_command=True, include_binary_data=False)
            rows = []
            for message in response:
                text = (message.get("processed_plain_text") or "").strip()
                if not text or text.startswith("/"):
                    continue
                info = message.get("message_info", {}).get("user_info", {})
                rows.append({"message_id": message["message_id"], "user_id": str(info.get("user_id", "")),
                             "speaker": "角色" if message.get("account_id") and str(info.get("user_id", "")) == str(message["account_id"]) else info.get("user_nickname", "对方"),
                             "text": text[:500], "timestamp": message.get("timestamp")})
            selected = []
            for row in reversed(rows):
                if len(dump([*selected, row])) <= min(2500, remaining):
                    selected.append(row)
            if selected:
                key = "chat-" + str(len(sources) + 1)
                sources[key] = {"session_id": stream["stream_id"], "messages": list(reversed(selected))}
                remaining -= len(dump(selected))
        for event in material["经历"]:
            if event.get("session_id") and remaining >= 600:
                item = brief({k: event[k] for k in ("id", "session_id", "source", "summary", "details") if k in event}, 400, 4)
                size = len(dump(item))
                if size <= remaining:
                    sources["event-" + event["id"]] = item
                    remaining -= size
        if not sources:
            return []
        prompt = (
            "整理角色今天与人的相处，输出 JSON：moments 数组，每项为 source_ids、summary、public_summary。"
            "最多选八件值得记下的小事，重复提到的同一件事合并。source_ids 使用给出的素材编号。"
            "summary 写发生了什么、是谁说的、角色有什么回应；承诺和愿望按原来的语气记录。"
            "public_summary 是适合角色发空间的个人感受或日常，比如收到礼物后的开心、一次聊天带来的安慰。"
            "称呼用有人、朋友等泛称，个人身份、联系方式、私密倾诉留在 summary；适合公开的内容为空时填空字符串。"
            "不同聊天流中的人物分别处理，具体物品、动作和细节取自素材，模型自己的提议按提议记录。"
            f"日期：{day.isoformat()}；素材：{dump(sources)}"
        )
        def validate(value):
            for moment in value.moments:
                if not moment.source_ids or any(key not in sources for key in moment.source_ids):
                    raise ValueError("每个片刻请填写对应素材编号")
                sessions = {sources[key]["session_id"] for key in moment.source_ids}
                if len(sessions) != 1:
                    raise ValueError("每个片刻请使用同一聊天流的素材")
        result = await generate(self.ctx, self.engine.store, self.config, "生活日记", prompt, DiaryMoments, validate=validate)
        return [{**moment.model_dump(), "session_id": sources[moment.source_ids[0]]["session_id"],
                 "sources": [sources[key] for key in moment.source_ids]} for moment in result.moments]

    async def arrange(self, now, spatial):
        if not self.config.diary.enabled:
            return
        life = self.config.life
        bedtime = next_bedtime(life, now)
        if life.schedule_enabled and (in_hours(now.hour, life.sleep_hour, life.wake_hour)
                or bedtime and bedtime - now <= timedelta(minutes=30)):
            return
        store = self.engine.store
        day = now.date().isoformat()
        if any(p["kind"] == "diary" and (p.get("diary_day") or p["start"][:10]) == day
               and p["status"] != "cancelled" for p in store.all("plan")):
            return
        prompt = (
            "为角色安排今天写生活日记的时间窗口，结合当前时间、作息和已有日程，选择未来空闲的10到20分钟。"
            "临近午夜时可安排到次日。输出 JSON：start、end（带时区的ISO时间）、reason（选择这个时间的理由）。"
            "作息开启时，日记放在睡前收尾之前完成，给入睡前留30分钟洗漱和放松；今晚空档不足就选醒来后的空闲。"
            f"下一次入睡：{bedtime.isoformat() if bedtime else '未设置待进入的睡眠'}；"
            f"现在：{now.isoformat()}；睡眠时间：{self.config.life.sleep_hour}至{self.config.life.wake_hour}；"
            f"遵循作息：{self.config.life.schedule_enabled}；已有日程：{json.dumps([p for p in store.all('plan') if p['status'] in {'scheduled', 'active'}], ensure_ascii=False)}"
        )
        def validate(slot):
            start, end = self.engine.time(slot.start), self.engine.time(slot.end)
            if start < now or not 10 <= (end - start).total_seconds() / 60 <= 20:
                raise ValueError("日记时间应为未来的10到20分钟")
            if start.date() > now.date() + timedelta(days=1):
                raise ValueError("请安排在今天或次日")
            if life.schedule_enabled:
                for offset in range(-1, 3):
                    sleep = (now + timedelta(days=offset)).replace(hour=life.sleep_hour, minute=0, second=0, microsecond=0)
                    wake = sleep.replace(hour=life.wake_hour)
                    if wake <= sleep:
                        wake += timedelta(days=1)
                    if start < wake and end > sleep - timedelta(minutes=30):
                        raise ValueError("日记请安排在入睡前30分钟之前完成，或醒来后的空闲")
            conflicts = [p for p in store.all("plan") if p["status"] in {"scheduled", "active"}
                         and self.engine.time(p["start"]) < end and self.engine.time(p["end"]) > start]
            if conflicts:
                raise ValueError("请避开这些已有安排：" + json.dumps(
                    [{k: p[k] for k in ("title", "start", "end")} for p in conflicts], ensure_ascii=False))

        slot = await generate(self.ctx, store, self.config, "日记安排", prompt, DiarySlot, validate=validate)
        value = PlanInput(title="写生活日记", kind="diary", start=slot.start, end=slot.end,
                          diary_day=now.date().isoformat(), motivation=slot.reason, flexible=True)
        return spatial.schedule(value, "", now, autonomous=True)

    async def process(self, now):
        for plan in self.engine.store.all("plan"):
            if plan["kind"] != "diary" or plan["status"] not in {"active", "completed"}:
                continue
            day = plan.get("diary_day") or self.engine.time(plan["start"]).date().isoformat()
            old = self.engine.store.find("diary", day)
            if plan["status"] == "completed" and old and old.get("plan_id") != plan["id"]:
                continue
            if old and old.get("plan_id") == plan["id"]:
                if old["status"] in {"已生成", "生成中"}:
                    continue
                if old.get("retry_at") and self.engine.time(old["retry_at"]) > now:
                    continue
            await self.write(day, now, plan["id"])

    async def write(self, day_text, now, plan_id=""):
        day = date.fromisoformat(day_text) if day_text else now.date()
        if day > now.date():
            raise ValueError("请选择今天或过去的日期")
        store = self.engine.store
        old = store.find("diary", day.isoformat())
        if old and (old["status"] == "生成中"):
            return old
        material = self.material(day, now)
        record = {**(old or {}), "id": day.isoformat(), "day": day.isoformat(), "title": day.isoformat() + " 的一天",
                  "content": old.get("content", "") if old else "", "status": "生成中", "error": "",
                  "plan_id": plan_id, "created_at": now.isoformat(), "complete": day < now.date(), "material": material}
        with store.db:
            store.put("diary", record)
        try:
            material["聊天片刻"] = await self.chat_moments(day, now, material)
            if not any(material[key] for key in ("活动", "经历", "状态变化", "梦境", "聊天片刻", "阅读笔记")):
                raise ValueError("这一天还没有生活记录，积累活动或经历后即可写日记")
            personality = await self.ctx.config.get("personality")
            prompt = (
                "请以角色自己的第一人称写今天的生活日记，直接返回中文正文，约300到600字。"
                "挑今天真正有感触的两三件事来写：与人的相处、活动的小进展、心情转折和还惦记的事。"
                "根据当天素材决定重点，阅读只是生活的一部分。同一件事在聊天、经历和笔记中重复出现时合并叙述。平淡的一天可以简短些。"
                "叙述以提供的活动和经历为依据：completed 是完成的活动，active 是进行中的活动，"
                "scheduled 是计划，cancelled 是取消，awaiting_confirmation 是等待确认完成情况。"
                "simulated 用角色世界的视角讲述，reported 是用户讲述，observed 是实际互动；梦境以梦见来表达。"
                "涉及对方的行动和约定，沿用记录里的信息。感受可以自然表达，具体过程和进度沿用已有细节。"
                "日记写到记录截止时间为止，尚在进行的事情按当前进展叙述。"
                f"角色人格：{dump(brief(personality, 1500, 6))}；生活背景：{self.config.life.background[:1500]}；"
                f"生活记录：{dump(writing_material(material))}"
            )
            result = await generate(self.ctx, store, self.config, "生活日记", prompt)
            content = result["response"].strip()
            if not content:
                raise ValueError("模型返回了空日记，请重新生成")
            record.update(content=content, status="已生成")
        except asyncio.CancelledError:
            record.update(status="生成失败", error="写作任务已中断", retry_at=(now + timedelta(minutes=5)).isoformat())
            raise
        except Exception as error:
            record.update(status="生成失败", error=str(error), retry_at=(now + timedelta(minutes=5)).isoformat())
            raise
        finally:
            with store.db:
                store.put("diary", record)
        return record
