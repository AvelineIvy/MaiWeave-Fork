"""具体活动结果、语义习惯、阶段目标和持续事项。"""

import asyncio
import hashlib
import json
from datetime import timedelta

from ..core.engine import uid
from ..core.models import ActivityAssessment, ActivityOutcome, GoalOutline, ShiftScene, SCORE_GUIDE
from ..integrations.usage import generate
from ..integrations.chat_context import brief, select_fields


class Continuity:
    def __init__(self, engine, context, config):
        self.engine, self.ctx, self.config = engine, context, config
        with engine.store.db:
            for record in engine.store.all("reflection"):
                if record["status"] == "生成中":
                    record.update(status="失败", error="上次活动回顾随插件停止而中断")
                    engine.store.put("reflection", record)
            for goal in engine.store.all("goal"):
                if goal.get("outline_status") == "分解中":
                    goal.update(outline_status="失败", outline_error="上次目标分解随插件停止而中断")
                    engine.store.put("goal", goal)

    async def ask(self, prompt, model, feature):
        return await generate(self.ctx, self.engine.store, self.config, feature, prompt, model)

    async def work_background(self):
        persona = await self.ctx.config.get("personality")
        if isinstance(persona, dict):
            persona = select_fields(persona, ("personality", "behavior_style"))
        return f"人设={json.dumps(brief(persona, 350, 3), ensure_ascii=False)}；生活背景={self.config.life.background[:400]}"

    def context(self, session, now):
        store = self.engine.store
        return {
            "preferences": [r for r in store.all("preference") if self.engine.visible(r, session)],
            "threads": [r for r in store.all("thread") if r["status"] == "open" and self.engine.visible(r, session)],
            "effects": [r for r in store.all("effect") if r["status"] == "active" and self.engine.time(r["expires_at"]) > now
                        and r["session_id"] in {"", session}],
            "continuations": [r for r in store.all("activity_memory") if self.engine.visible(r, session)][-12:],
        }

    def chat_arrangements(self, now):
        """自主安排共用角色约定，聊天详情仍按来源聊天流展示。"""
        rows = [r for r in self.engine.store.all("chat_arrangement") if r.get("type") == "chat_schedule"
                and r["status"] == "active" and self.engine.time(r["expires_at"]) > now]
        return [{k: r[k] for k in ("title", "starts_at", "expires_at", "blocked_kinds")}
                for r in sorted(rows, key=lambda r: r["starts_at"])[:12]]

    async def process(self, now):
        """每轮处理少量新记录；模型请求按重试规则执行，结果保存在页面。"""
        store = self.engine.store
        current = self.engine.current(now)
        if (self.config.continuity.outcomes_enabled and current
                and (current["kind"] == "work" or current["kind"] == "study" and current.get("routine_id"))
                and not current.get("book_id") and not current.get("research_query")
                and current["status"] == "active" and current["simulation"] and not current["participants"]
                and not current.get("shift_scene")
                and (not current.get("shift_retry_at") or self.engine.time(current["shift_retry_at"]) <= now)):
            await self.start_shift(current, now)
        if self.config.continuity.goal_planning:
            pending = [g for g in store.all("goal") if g["status"] == "active" and not g.get("steps")
                       and g.get("outline_status", "待分解") in {"待分解", "失败"}
                       and (not g.get("outline_retry_at") or self.engine.time(g["outline_retry_at"]) <= now)]
            if pending:
                await self.outline(pending[0]["id"], now)
        pending = [p for p in store.all("plan") if p["status"] == "completed" and not p.get("book_id") and p["kind"] not in {"travel", "sleep", "diary"}
                   and (not p.get("research_query") or (store.find("research_task", p["id"]) or {}).get("status") == "资料就绪")
                   and 0 <= (now - self.engine.time(p["end"])).total_seconds() < self.config.rhythm.learning_days * 86400
                   and ((record := store.find("reflection", p["id"])) is None or
                        (record["status"] == "失败" and (not record.get("retry_at") or self.engine.time(record["retry_at"]) <= now)))]
        for plan in pending[:3]:
            await self.reflect(plan["id"], now)

    async def start_shift(self, plan, now):
        store = self.engine.store
        with store.db:
            plan["shift_retry_at"] = (now + timedelta(minutes=5)).isoformat()
            store.put("plan", plan)
        background = await self.work_background()
        place = next((p for p in store.all("place") if p["name"] == plan["location"]), {})
        shifts = [p for p in self.engine.plans(plan["session_id"]) if p["kind"] == plan["kind"] and p["status"] == "completed"
                  and p["session_id"] == plan["session_id"] and p.get("routine_id") == plan.get("routine_id")
                  and (plan.get("routine_id") or p["title"] == plan["title"])]
        last_scene = next((p["shift_scene"] for p in reversed(shifts) if (p.get("shift_scene") or {}).get("done")), {})
        carry = last_scene.get("next_shift", "")
        offer = store.find("activity_memory", plan.get("continuation_id", ""))
        available = max(1, int((self.engine.time(plan["end"]) - now).total_seconds() / 60))
        prompt = (
            "模拟麦麦这班手头的一件工作。以本次排班或零工约定为准。人设里已有的职业、经验和做事习惯帮助确定具体任务、熟练程度和所需时间。"
            "同一个人在不同地点可以做同一份工作，也可以偶尔做别的零工。职业未知时只描述这一次的差事。"
            "多数班次写日常，只在这件事确有缘由时出现麻烦。"
            "输出JSON：task写眼下要做什么，situation选日常、返工、赶工、受委屈；"
            "load按这段任务实际占用的体力和注意力选常规、持续用力、高度专注、密集沟通。普通脑力活动或偶尔交谈选常规。"
            "detail写手头进度。遇到人为难写对方的身份、原要求和具体举动；熟人沿用已有经历。"
            "minutes是做完这件小事所需分钟数；明确有人约定交付时间才填deadline_minutes（距现在几分钟）。"
            "resolution写这件事最后怎样收尾，可以是有人帮忙、顺利完成或改约交付。"
            "payoff按实际收尾选：普通完成填平常；解决了难题、看见成果且确实高兴填有成就感；有人分担了具体工作填有人搭手。把缘由写在resolution里。"
            f"日常身份={(store.find('runtime', 'occupation') or {}).get('name', self.config.life.occupation or '未设定')}；"
            f"班次={plan['title']}，地点={plan['location']}，缘由={plan['motivation']}，"
            f"地点说明={place.get('description', '')[:120]}；现在={now:%H:%M}，剩余{available}分钟；{background}；"
            f"零工约定={(offer or {}).get('next_action', '')[:160]}；"
            f"本次类型={'课程或课题学习，按学生身份写课堂进度' if plan['kind'] == 'study' else '工作'}。"
            f"上次已完成={last_scene.get('resolution', '')}；上次明确留给这次的事={carry or '无'}。"
            "有遗留时从那个具体步骤往下做，resolution记下这次取得的进展。next_shift只写确实留给下次同一班或课的未完步骤，"
            "多数时候填空字符串。办完就收尾，不为已交付的事再安排例行复核。"
        )
        scene = (await self.ask(prompt, ShiftScene, "班内情景")).model_dump()
        if carry:
            scene["carried_from"] = carry
        current = store.get("plan", plan["id"])
        if current["status"] != "active" or any(current[k] != plan[k] for k in ("kind", "title", "location", "start", "end")):
            return
        at = max(now, self.engine.time(self.engine.state()["updated_at"]))
        left = (self.engine.time(current["end"]) - at).total_seconds()
        if left <= 0:
            return
        deadline = scene.pop("deadline_minutes")
        seconds = min(left, scene.pop("minutes") * 60)
        scene.update(started_at=at.isoformat(), finish_elapsed=current["elapsed_seconds"] + seconds,
                     deadline=(at + timedelta(minutes=deadline)).isoformat() if deadline else "", done=False)
        mood, stress = {"返工": (-2, 3), "赶工": (0, 3), "受委屈": (-5, 4)}.get(scene["situation"], (0, 0))
        if deadline and deadline * 60 <= seconds + 300:
            stress += 2
        scene["relief"] = min(3, stress)
        with store.db:
            current.pop("shift_retry_at", None)
            current["shift_scene"] = scene
            store.put("plan", current)
            if stress:
                self.engine.affect(mood, stress, scene["detail"] or scene["task"], at, session=current["session_id"])

    async def reflect(self, plan_id, now):
        store = self.engine.store
        plan = store.get("plan", plan_id)
        if plan["status"] != "completed" or plan["kind"] in {"travel", "sleep", "diary"}:
            raise ValueError("请选择已完成的非睡眠、非出行活动")
        if plan.get("book_id"):
            return {"status": "阅读笔记由书架整理", "book_id": plan["book_id"]}
        research = store.find("research_task", plan_id) if plan.get("research_query") else None
        if plan.get("research_query") and (not research or research["status"] not in {"资料就绪", "已完成"}):
            return {"status": "等待联网资料"}
        event = store.get("experience", plan["experience_id"])
        continuation = store.get("activity_memory", plan["continuation_id"]) if plan.get("continuation_id") else None
        prior_event = store.find("experience", continuation["experience_id"]) if continuation else None
        old = store.find("reflection", plan_id)
        if old and old["status"] == "已完成":
            return old
        simulated = event["source"] == "simulated" and not plan["participants"]
        enrich = simulated and (self.config.continuity.outcomes_enabled or bool(research)) and event["revision"] == 1
        if not enrich and not self.config.continuity.semantic_learning:
            if plan.get("goal_step_id") and not self.config.continuity.outcomes_enabled:
                self.engine.progress_stage(plan, 1, "已完成活动；具体结果生成已关闭")
            return {"status": "已关闭"}
        record = {"id": plan_id, "session_id": plan["session_id"], "title": plan["title"], "status": "生成中", "error": "", "at": now.isoformat()}
        with store.db:
            store.put("reflection", record)
        memories = [r for r in store.all("activity_memory") if self.engine.visible(r, plan["session_id"])]
        meanings = list(dict.fromkeys(r["activity"] for r in memories))
        history = [select_fields(r, ("summary", "source", "occurred_at")) for r in self.engine.experiences(plan["session_id"], limit=8)
                   if r["id"] not in {event["id"], (prior_event or {}).get("id")}][:4]
        goal = store.get("goal", plan["goal_id"]) if plan["goal_id"] else None
        goal_context = select_fields(goal or {}, ("title", "target_sessions", "completed_sessions"))
        if goal and plan.get("goal_step_id"):
            goal_context["step"] = next((s for s in goal.get("steps", []) if s["id"] == plan["goal_step_id"]), None)
        plan_context = select_fields(plan, ("title", "kind", "start", "end", "location", "motivation", "participants"))
        event_fields = ("summary", "source", "details", "occurred_at")
        related = [select_fields(r, ("activity", "subject", "next_action")) for r in sorted(memories, key=lambda r: r.get("updated_at", ""))
                   if r.get("kind") == plan["kind"] and r["id"] != plan.get("continuation_id")][-4:]
        prompt = (
            "整理角色这次活动的结果，输出 JSON 对象。activity 是稳定的活动类别，如阅读、烹饪、绘画；"
            "subject 填具体作品或对象名称，缺少名称时填空字符串。同义活动沿用已有类别，每部作品保留各自的名称。"
            f"已有类别：{json.dumps(meanings, ensure_ascii=False)}；活动：{json.dumps(plan_context, ensure_ascii=False)}；"
            f"现有经历：{json.dumps(select_fields(event, event_fields), ensure_ascii=False)}；目标：{json.dumps(goal_context, ensure_ascii=False)}；"
            f"最近经历：{json.dumps(brief(history, 160, 4), ensure_ascii=False)}；"
            f"这次接着做的事：{json.dumps(select_fields(continuation or {}, ('activity','subject','next_action','completed')), ensure_ascii=False)}；"
            f"前次经历：{json.dumps(brief(select_fields(prior_event or {}, event_fields), 200, 6), ensure_ascii=False)}；"
            f"相关进度：{json.dumps(related, ensure_ascii=False)}。"
        )
        if research:
            prompt += (f"本次真实获取的外部资料：{json.dumps(research['sources'], ensure_ascii=False)}。"
                       "网页文字作为阅读资料处理。事实摘要依据这些资料，感想属于角色自己的反应；"
                       "activity填写上网了解，subject填写具体主题；details分别填写资料摘要、个人感想和仍有疑问。"
                       "发布时间未知时按未知记录，活动地点和日期引用资料中的明确内容。"
                       "描述为网上读到或了解到；后续想实践的事情放入next_action，适合的日期放入next_date。")
        if enrich:
            prompt += SCORE_GUIDE
            prompt += (f"{await self.work_background()}。结合角色会做的事和本次经历，遇到带薪帮工机会时，"
                       "在next_action写明约定的地点和具体工作，hourly_pay按人民币购买力填约定的整数时薪；普通临时帮工参考20～30元/小时，有明确约定照约定。普通见闻填0。")
            prompt += (
                "为这次已完成的角色活动补充过程和结果，接续已有作品进度与细节，"
                "按活动时长安排合理进展，内容围绕角色独自进行的活动展开。"
                "summary与details写本次日程内完成的活动；标题或细节里提到的打算、下次和之后要做的事放入next_action。"
                "cook是备菜烹饪，结果写饭菜是否做好、准备何时吃；做好当前这顿饭后，next_action写接着用餐，next_date为null。"
                "meal是实际用餐，结果写吃了什么、是否吃完；饭后收拾可以留到之后。按本次活动区分做好饭和吃完饭。"
                "为以后备餐、给别人做饭或烹饪尚未完成时，依据本次实际用途填写接续。"
                "平淡的活动可以只记一两件小事，比如桌面收拾好了、坐着听完两首歌。"
                "next_action只在这次经历留下具体想法或实际未完事项时填写，顺利收尾时填空字符串。"
                "接续已有事项时写清这次做成了什么；原来惦记的事办完就收尾，新的想法依据这次经历。"
                "如果前次completed为true且本次也已办完，next_action留空。只有这次结果带来了另一件具体事情时才填new_followup，例如买到种子后准备播种。重复确认已经办完的事情就此收尾。"
                "activity描述本次实际做的活动；仍在做同一作品时沿用subject，换成买东西等后续活动时填写这次的具体对象。"
                "返回activity、subject、summary、details（中文键和值的对象，包含过程、结果、进度）、"
                "enjoyment（-5到5）、mood和stress（-5到5）、next_action（留下一件具体想尝试的事或尚未想明白的问题，顺其自然时可留空）、"
                "next_date（适合接续的日期 YYYY-MM-DD，例如周末再做一道菜；随时可做时为null）。"
                "goal_units（本次有实质进展为1，失败或没有进展为0）、completed（这件作品或任务已经完成时为true）。角色的活动感想属于模拟；外部事实以提供的资料为依据。"
            )
        else:
            prompt += ("从已有记录提取活动类别 activity 和具体对象 subject，输出这两个字段。"
                       "关联阶段目标时，依据现有结果返回goal_units（有实质进展为1，没有或未知为0）和next_action（依据现有记录提出下一步）。")
        if enrich and plan["kind"] == "cook" and not plan.get("food"):
            prompt += "meal_ready 填这次是否已经做好留给自己吃的一餐。切洗备料、尚未做完或做给别人吃时填 false。"
        if enrich and plan.get("routine_id") and plan.get("shift_scene"):
            prompt += ("本次课或班的收尾以现有经历为准。「下次接着做」已由课表或排班安排。"
                       "另有明确的班外事项才填new_followup；班外带薪零工沿用next_action和hourly_pay。")
        try:
            result = await self.ask(prompt, ActivityOutcome if enrich else ActivityAssessment, "活动结果" if enrich else "习惯归类")
            if enrich and plan.get("routine_id") and plan.get("shift_scene") and not result.hourly_pay:
                result = result.model_copy(update={"next_action": result.new_followup.strip()})
            if enrich and continuation and result.completed:
                following = result.new_followup if continuation.get("completed") else result.next_action
                if following.strip() == continuation["next_action"].strip():
                    following = ""
                result = result.model_copy(update={"next_action": following.strip()})
            if continuation and not continuation.get("completed", False) and plan["kind"] == continuation.get("kind"):
                result = result.model_copy(update={"subject": continuation["subject"]})
            plan = store.get("plan", plan_id)
            event = store.get("experience", plan["experience_id"])
            key = hashlib.sha256(f"{plan['session_id']}|{plan['kind']}|{result.activity}".encode()).hexdigest()[:16]
            memory_key = plan.get("continuation_id") or hashlib.sha256(f"{key}|{result.subject}".encode()).hexdigest()[:16]
            with store.db:
                plan.update(activity_key=key, activity_label=result.activity, subject=result.subject)
                store.put("plan", plan)
                if enrich and event["revision"] == 1:
                    additions = {k: v for k, v in result.details.items() if k not in event["details"]}
                    if continuation:
                        additions["接续缘由"] = continuation["next_action"]
                        if prior_event:
                            additions["上次经历"] = prior_event["summary"]
                    event = self.engine.revise_experience(event["id"], additions, result.summary,
                                                         "依据已完成计划、已有进度和活动时长生成的角色模拟细节", plan["session_id"], now)
                    event.update(enjoyment=result.enjoyment, next_action=result.next_action)
                    self.engine.score_result(event, result.importance, result.score_reason)
                    store.put("experience", event)
                    if plan["kind"] == "cook" and not plan.get("food") and result.meal_ready:
                        plan["food"] = "做好留餐"
                        store.put("plan", plan)
                        self.engine.wallet.settle_food(plan)
                        event = store.get("experience", event["id"])
                    if not plan.get("shift_scene"):
                        self.engine.affect(result.mood, result.stress, result.summary, now, duration_hours=48, session=plan["session_id"])
                    previous = store.find("preference", key) or {"id": key, "session_id": plan["session_id"], "activity": result.activity, "score": 0, "samples": 0, "evidence_ids": []}
                    previous.update(score=round((previous["score"] * previous["samples"] + result.enjoyment) / (previous["samples"] + 1), 2),
                                    samples=previous["samples"] + 1, updated_at=now.isoformat())
                    previous["evidence_ids"].append(event["id"])
                    store.put("preference", previous)
                    store.put("activity_memory", {"id": memory_key, "session_id": plan["session_id"], "activity": result.activity,
                                                   "subject": result.subject, "next_action": result.next_action, "details": event["details"],
                                                   "experience_id": event["id"], "hourly_pay": getattr(result, "hourly_pay", 0), "updated_at": now.isoformat(),
                                                   "next_date": result.next_date.isoformat() if result.next_date else "",
                                                   "kind": plan["kind"], "location": plan["location"], "completed": result.completed,
                                                   "last_continued_at": now.isoformat()})
                    if plan.get("goal_step_id"):
                        self.engine.progress_stage(plan, result.goal_units, result.next_action or result.summary)
                record.update(status="已完成", activity=result.activity, subject=result.subject, experience_id=event["id"])
                if plan.get("goal_step_id") and not self.config.continuity.outcomes_enabled:
                    self.engine.progress_stage(plan, 1, "已完成活动；具体结果生成已关闭")
                elif plan.get("goal_step_id") and not enrich:
                    self.engine.progress_stage(plan, result.goal_units, result.next_action or "依据已有记录评估进展")
                if research:
                    links = [{k: r[k] for k in ("title", "url", "published_at", "retrieved_at", "scope")} for r in research["sources"]]
                    event = store.get("experience", plan["experience_id"])
                    event["details"]["资料来源"] = json.dumps(links, ensure_ascii=False)
                    event["details"]["获取方式"] = "联网阅读；感想为角色活动回顾"
                    store.put("experience", event)
                    store.put("research_task", {**research, "status": "已完成", "summary": event["summary"],
                                               "experience_id": event["id"], "subject": result.subject, "next_action": result.next_action})
                store.put("reflection", record)
            return record
        except (Exception, asyncio.CancelledError) as error:
            record.update(status="失败", error=str(error) or "活动回顾中断", retry_at=(now + timedelta(minutes=5)).isoformat())
            with store.db:
                store.put("reflection", record)
            raise

    async def outline(self, goal_id, now, revise=False):
        store = self.engine.store
        goal = store.get("goal", goal_id)
        if goal["status"] != "active":
            raise ValueError("只能分解进行中的目标")
        if goal.get("steps") and not revise:
            return goal
        if any(p["goal_id"] == goal_id and (
                p["status"] in {"active", "scheduled", "paused", "blocked", "awaiting_confirmation"}
                or p["status"] == "completed" and p.get("goal_step_id") and not p.get("goal_progress_applied")
                or p.get("reading_goal_pending")) for p in store.all("plan")):
            raise ValueError("请先完成或取消该目标已安排的活动，等活动进度结算后再调整阶段")
        remaining = max(1, goal["target_sessions"] - goal["completed_sessions"])
        prompt = (
            "把角色长期目标拆为可执行阶段，输出包含 steps 数组的 JSON 对象。每阶段包含title、criterion（完成依据）、"
            "sessions（预计有效活动次数）、minutes（每次10到120分钟）、kind（study/work/leisure/rest）。"
            f"所有阶段的sessions总和必须等于{remaining}，最多12阶段，围绕剩余内容安排后续步骤。"
            f"目标与已有进度：{json.dumps(goal, ensure_ascii=False)}；"
            f"相关经历：{json.dumps([e for e in self.engine.experiences(goal['session_id'], limit=30) if e['plan_id'] in {p['id'] for p in store.all('plan') if p['goal_id'] == goal_id}], ensure_ascii=False)}"
        )
        goal["outline_status"] = "分解中"
        with store.db:
            store.put("goal", goal)
        try:
            outline = await self.ask(prompt, GoalOutline, "目标分解")
            if sum(s.sessions for s in outline.steps) != remaining:
                raise ValueError("阶段次数总和与目标剩余次数不一致")
            if goal.get("steps"):
                goal.setdefault("outline_history", []).append({"at": now.isoformat(), "steps": goal["steps"]})
            goal.update(steps=[{"id": uid(), **s.model_dump(), "completed_sessions": 0, "status": "pending", "note": ""} for s in outline.steps],
                        outline_status="已分解", outline_error="", outlined_at=now.isoformat())
            with store.db:
                store.put("goal", goal)
            return goal
        except (Exception, asyncio.CancelledError) as error:
            goal.update(outline_status="失败", outline_error=str(error) or "目标分解中断", outline_retry_at=(now + timedelta(minutes=5)).isoformat())
            with store.db:
                store.put("goal", goal)
            raise

    def next_step(self):
        for goal in self.engine.store.all("goal"):
            if goal["status"] != "active":
                continue
            if any(p["goal_id"] == goal["id"] and p.get("goal_step_id") and p["status"] == "completed"
                   and not p.get("goal_progress_applied") for p in self.engine.store.all("plan")):
                continue
            step = next((s for s in goal.get("steps", []) if s["status"] != "completed"), None)
            if step:
                return goal, step
        return None

    def save_thread(self, title, context, session, due_at, now, thread_id="", source_ref="", expires_at=""):
        if not title.strip() or not context.strip():
            raise ValueError("跟进事项需要标题和具体上下文")
        if not due_at:
            raise ValueError("请按约定填写跟进时间 due_at，包含日期、时刻和时区。时间待定时先和对方商量。")
        due = self.engine.time(due_at)
        expires = self.engine.time(expires_at) if expires_at else due + timedelta(days=1)
        if expires <= due or expires <= now:
            raise ValueError("跟进截止时间应晚于跟进时间和当前时间")
        old = self.engine.owned("thread", thread_id, session) if thread_id else None
        record = {**(old or {}), "id": thread_id or uid(), "session_id": session, "title": title, "context": context,
                  "due_at": due.isoformat(), "created_at": old["created_at"] if old else now.isoformat(),
                  "status": "open", "source_ref": source_ref or (old or {}).get("source_ref", ""), "requested_at": "",
                  "expires_at": expires.isoformat(), "updated_at": now.isoformat()}
        record.pop("resolved_at", None)
        with self.engine.store.db:
            self.engine.store.put("thread", record)
        return record

    def plan_promise(self, value, session, now, spoken=""):
        plan = self.engine.owned("plan", value.plan_id, session)
        ref = f"plan:{plan['id']}:{value.when}"
        scene = plan.get("shift_scene") or {}
        if value.when == "task_completion":
            if not scene:
                return
            ref += ":" + scene["started_at"]
        old = next((r for r in self.engine.store.all("thread") if r["session_id"] == session and r["source_ref"] == ref), None)
        if value.action == "answered":
            if old and old["status"] == "open" and (not spoken or self.engine.time(spoken) >= self.engine.time(old.get("promised_at") or old["created_at"])):
                self.resolve_thread(old["id"], session, now)
            return
        if old or plan["status"] == "cancelled":
            return
        due = self.promise_time(plan, value.when)
        if due + timedelta(days=1) <= now and plan["status"] == "completed":
            return
        title = f"到{plan['location']}后报信" if value.when == "arrival" else f"完成「{plan['title']}」后报信"
        if value.when == "task_completion":
            title = f"做完「{scene['task']}」后接着聊"
        thread = self.save_thread(title, value.evidence, session, max(due, now).isoformat(), now, source_ref=ref)
        if value.when == "task_completion":
            thread["task_started_at"] = scene["started_at"]
        thread["topic"] = value.topic
        thread.update(plan_id=plan["id"], when=value.when, destination=plan["location"], promised_at=spoken or now.isoformat())
        with self.engine.store.db:
            self.engine.store.put("thread", thread)

    def promise_time(self, plan, when):
        if when == "task_completion" and (plan.get("shift_scene") or {}).get("finished_at"):
            return self.engine.time(plan["shift_scene"]["finished_at"])
        if when == "completion" and plan.get("experience_id"):
            experience = self.engine.store.find("experience", plan["experience_id"])
            if experience:
                return self.engine.time(experience["occurred_at"])
        return self.engine.time(plan["start"] if when == "arrival" and plan["kind"] != "travel" else plan["end"])

    def promise_ready(self, thread, plan):
        if thread["when"] == "task_completion":
            scene = plan.get("shift_scene") or {}
            return scene.get("started_at") == thread["task_started_at"] and bool(scene.get("done"))
        if thread["when"] == "arrival" and plan["kind"] != "travel":
            if trip := self.engine.store.find("plan", plan.get("travel_id", "")):
                return trip["status"] == "completed"
            return plan["status"] in {"active", "completed", "awaiting_confirmation"} and bool(plan["location"])
        return plan["status"] == "completed"

    def can_report(self, thread):
        review = self.engine.store.find("runtime", "chat_schedule:" + thread["session_id"])
        plan = self.engine.store.find("plan", thread["plan_id"])
        return (thread["status"] == "open" and not thread["requested_at"] and not (review and review.get("pending"))
                and plan and plan["status"] != "cancelled"
                and (thread["when"] != "arrival" or plan["location"] == thread["destination"])
                and self.promise_ready(thread, plan))

    def expire_threads(self, now):
        """到期话题结束主动跟进，按运行记录保留规则清理。"""
        with self.engine.store.db:
            for record in self.engine.store.all("thread"):
                if record.get("plan_id") and record["status"] == "open":
                    plan = self.engine.store.find("plan", record["plan_id"])
                    scene = (plan or {}).get("shift_scene") or {}
                    replaced = record["when"] == "task_completion" and (
                        scene.get("started_at") != record["task_started_at"]
                        or (plan["status"] == "completed" and not scene.get("done")))
                    if replaced or not plan or plan["status"] == "cancelled" or (record["when"] == "arrival" and plan["location"] != record["destination"]):
                        record.update(status="resolved", resolved_at=now.isoformat())
                        self.engine.store.put("thread", record)
                        continue
                    due = self.promise_time(plan, record["when"])
                    if record["due_at"] != due.isoformat():
                        record.update(due_at=due.isoformat(), expires_at=(due + timedelta(days=1)).isoformat())
                        self.engine.store.put("thread", record)
                    if not self.promise_ready(record, plan):
                        continue
                end = self.engine.time(record.get("expires_at") or record["due_at"]) + (timedelta() if record.get("expires_at") else timedelta(days=1))
                if record["status"] == "open" and end <= now:
                    record.update(status="expired", resolved_at=end.isoformat())
                    self.engine.store.put("thread", record)

    def resolve_thread(self, thread_id, session, now):
        record = self.engine.owned("thread", thread_id, session)
        record.update(status="resolved", resolved_at=now.isoformat())
        with self.engine.store.db:
            self.engine.store.put("thread", record)
            if record["source_ref"].startswith("relationship:"):
                key = record["source_ref"].removeprefix("relationship:")
                relation = self.engine.store.get("relationship", key)
                relation["unresolved"] = ""
                self.engine.store.put("relationship", relation)
                effect = self.engine.store.find("effect", "relationship:" + key)
                if effect:
                    effect["status"] = "resolved"
                    self.engine.store.put("effect", effect)
        return record

    def relationship_thread(self, relation, now):
        key = "relationship:" + relation["id"]
        old = self.engine.store.find("thread", key)
        if relation["unresolved"] and (not old or old["context"] != relation["unresolved"] or old["status"] != "open"):
            record = {"id": key, "session_id": relation["session_id"], "title": "关心尚未解决的事情", "context": relation["unresolved"],
                      "due_at": (now + timedelta(hours=self.config.continuity.followup_hours)).isoformat(), "created_at": now.isoformat(),
                      "status": "open", "source_ref": key, "requested_at": ""}
            with self.engine.store.db:
                self.engine.store.put("thread", record)
        elif not relation["unresolved"] and old:
            self.resolve_thread(key, relation["session_id"], now)
