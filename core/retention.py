"""按用途清理旧记录，延续当前生活所需的关联。"""

from datetime import timedelta
from pathlib import Path

from .models import SHARE_CONTEXT_WINDOW, SHARE_REPLY_PAUSE, local_time
from .image_retention import ImageRetention
from .wallet import MONTHLY_BILLS


HISTORY_FIELDS = (("relationship", "history", "关系变化历史"), ("goal", "outline_history", "目标分解历史"),
                  ("plan", "changes", "日程修改历史"))


class Retention:
    def __init__(self, store, config, data_dir):
        self.store, self.config = store, config
        self.book_dir = Path(data_dir) / "books"
        self.images = ImageRetention(store, config, data_dir)

    def time(self, value):
        return local_time(value, self.config.life.timezone)

    def collect(self, now):
        store, cfg = self.store, self.config.retention
        plans = store.all("plan")
        events = store.all("experience")
        protected_plans, protected_events = set(), set()
        goals = {g["id"] for g in store.all("goal") if g["status"] == "active"}
        recent = now - timedelta(days=max(1, self.config.rhythm.learning_days))
        rules = {r["id"] for r in store.all("routine") if r.get("status") == "active"
                 and (not r.get("until") or r["until"] >= now.date().isoformat())}
        latest_shifts = {}
        for plan in plans:
            if plan["status"] not in {"completed", "cancelled"} or self.time(plan["end"]) >= recent or plan.get("goal_id") in goals:
                protected_plans.add(plan["id"])
            if plan["status"] == "completed":
                if (plan.get("kind") == "meal" or plan.get("groceries")) and 0 <= (now.date() - self.time(plan["end"]).date()).days <= 7:
                    protected_plans.add(plan["id"])
                rule = plan.get("routine_id")
                if rule in rules and (plan.get("shift_scene") or {}).get("done"):
                    previous = latest_shifts.get(rule)
                    if not previous or self.time(plan["start"]) > self.time(previous["start"]):
                        latest_shifts[rule] = plan
            if plan.get("book_id") and plan["status"] == "completed":
                note = store.find("book_note", plan["id"])
                if not note or note["status"] not in {"已完成", "本书已读完", "书籍已删除"}:
                    protected_plans.add(plan["id"])
        protected_plans.update(p["id"] for p in latest_shifts.values())
        for category in ("reflection", "dream"):
            for row in store.all(category):
                if row["status"] in {"生成中", "阅读中"}:
                    protected_plans.add(row["id"])
        for row in store.all("diary"):
            if row["status"] != "已生成":
                protected_plans.add(row.get("plan_id", ""))
        for row in store.all("thread"):
            if row["status"] == "open" and row.get("plan_id"):
                protected_plans.add(row["plan_id"])
        for row in store.all("review"):
            if row["status"] not in {"已保存", "已取消", "失败"}:
                protected_events.add(row.get("payload", {}).get("experience_id", ""))
        for row in store.all("share_receipt"):
            if row["outcome"] in {"MaiBot 已受理", "生成中", "待核对"}:
                protected_events.add(row.get("experience_id", ""))
        # 正在使用的出行链保留整个关联链。
        while True:
            linked = {p.get(key, "") for p in plans if p["id"] in protected_plans for key in ("travel_id", "activity_id")}
            if linked <= protected_plans:
                break
            protected_plans.update(linked)

        # 数量上限统计本类记录；被生活关联占用的条目在页面单列。
        def select(rows, days, count, field, protected):
            ordered = sorted(rows, key=lambda r: self.time(r[field]))
            deleted = {r["id"] for r in ordered if days and self.time(r[field]) < now - timedelta(days=days) and r["id"] not in protected}
            excess = max(0, len(rows) - len(deleted) - count) if count else 0
            for row in ordered:
                if not excess:
                    break
                if row["id"] not in protected and row["id"] not in deleted:
                    deleted.add(row["id"])
                    excess -= 1
            return deleted

        terminal = [p for p in plans if p["status"] in {"completed", "cancelled"}]
        delete = {"plan": select(terminal, cfg.plan_days, cfg.plan_count, "end", protected_plans)}
        retained_plans = [p for p in plans if p["id"] not in delete["plan"]]
        linked_memories = {p.get("continuation_id") for p in retained_plans}
        memories = store.all("activity_memory")
        consumed = {(p["continuation_id"], p["continuation_experience_id"]) for p in plans
                    if p["id"] in delete["plan"] and p["status"] == "completed"
                    and p.get("continuation_id") and p.get("continuation_experience_id")}
        used = {r["id"] for r in memories if (r["id"], r.get("experience_id")) in consumed}
        dated_memories = [r for r in memories if r.get("updated_at")]
        linked_memories.update(r["id"] for r in dated_memories if r.get("next_action") and
                               (r.get("next_date") or "") >= now.date().isoformat() and r["id"] not in used)
        delete["activity_memory"] = select(dated_memories, cfg.experience_days, cfg.experience_count,
                                           "updated_at", linked_memories)
        delete["activity_memory"].update(used - linked_memories)
        protected_events.update(r.get("experience_id") for r in memories if r["id"] not in delete["activity_memory"])
        for plan in plans:
            if plan["id"] not in delete["plan"]:
                protected_events.add(plan.get("experience_id", ""))
        protected_events.update(e["id"] for e in events if e.get("keep") or e["importance"] >= 7 or self.time(e["occurred_at"]) >= now - timedelta(days=1))
        ordinary = [e for e in events if not e.get("keep") and e["importance"] < 7]
        delete["experience"] = select(ordinary, cfg.experience_days, cfg.experience_count, "occurred_at", protected_events)
        summary = []
        for category, label, rows, days, count, protected in (
            ("plan", "已结束日程", terminal, cfg.plan_days, cfg.plan_count, protected_plans),
            ("experience", "普通生活经历", ordinary, cfg.experience_days, cfg.experience_count, protected_events),
            ("activity_memory", "活动念头", dated_memories, cfg.experience_days, cfg.experience_count, linked_memories),
        ):
            summary.append(dict(category=category, label=label, total=len(rows), days=days, limit=count, protected=sum(r["id"] in protected for r in rows), remove=len(delete[category])))
        for category, label, field in (("job", "后台任务", "created_at"), ("review", "资料核对", "created_at"), ("share_receipt", "主动交流回执", "created")):
            rows = store.all(category)
            protected = set()
            for row in rows:
                if category == "share_receipt":
                    window = timedelta(minutes=max(1440, self.config.proactive.cooldown_minutes, self.config.proactive.retry_minutes))
                    if row.get("sent_at"):
                        window = max(window, SHARE_CONTEXT_WINDOW)
                    active = (row["outcome"] in {"MaiBot 已受理", "生成中", "待核对"} or
                              self.time(row.get("sent_at") or row[field]) >= now - window or
                              row.get("followup") == "answered" and row.get("responded_at") and
                              self.time(row["responded_at"]) > now - SHARE_REPLY_PAUSE)
                elif category == "job":
                    active = row["status"] == "进行中"
                else:
                    active = row["status"] not in {"已保存", "已取消", "失败"}
                if active:
                    protected.add(row["id"])
            delete[category] = select(rows, cfg.log_days, cfg.log_count, field, protected)
            summary.append(dict(category=category, label=label, total=len(rows), days=cfg.log_days, limit=cfg.log_count, protected=len(protected), remove=len(delete[category])))
        for category, label, field, days, count in (
            ("image_delivery", "生活图片发送记录", "at", cfg.log_days, cfg.log_count),
            ("model_usage", "模型调用明细", "at", cfg.usage_days, cfg.usage_count),
            ("emotion", "情绪变化", "at", cfg.log_days, cfg.log_count),
            ("chat_arrangement", "聊天约定", "expires_at", cfg.log_days, cfg.log_count),
            ("effect", "情绪影响", "expires_at", cfg.log_days, cfg.log_count),
            ("thread", "已结束跟进", "resolved_at", cfg.log_days, cfg.log_count),
            ("place_search", "地点搜索缓存", "searched_at", cfg.world_days, cfg.world_count),
            ("habit", "淡出的习惯", "last_seen", cfg.experience_days, cfg.experience_count),
        ):
            rows = store.all(category)
            if category == "thread":
                rows = [r for r in rows if r["status"] in {"resolved", "expired"}]
            if category == "habit":
                rows = [r for r in rows if not r["active"] and r["enabled"]]
            protected = {r["id"] for r in rows if (category == "effect" and r["status"] == "active") or (category == "chat_arrangement" and r["status"] == "active" and self.time(r["expires_at"]) > now)}
            delete[category] = select(rows, days, count, field, protected)
            summary.append(dict(category=category, label=label, total=len(rows), days=days,
                                limit=count, protected=len(protected), remove=len(delete[category])))
        chat_checks = [r for r in store.all("runtime") if r["id"].startswith("chat_schedule:")]
        # 聊天安排读取最近一天的消息，这段时间内沿用同一核对进度。
        protected_checks = {r["id"] for r in chat_checks if r["pending"] or self.time(r["updated_at"]) >= now - timedelta(days=1)}
        delete["runtime"] = select(chat_checks, cfg.log_days, cfg.log_count, "updated_at", protected_checks)
        summary.append(dict(category="chat_schedule_cursor", label="聊天安排核对进度", total=len(chat_checks), days=cfg.log_days,
                            limit=cfg.log_count, protected=len(protected_checks), remove=len(delete["runtime"])))
        states = store.all("person_state")
        delete["person_state"] = {r["id"] for r in states if self.time(r["expires_at"]) <= now}
        summary.append(dict(category="person_state", label="人物近况", total=len(states), days=0,
                            limit=0, protected=len(states)-len(delete["person_state"]),
                            remove=len(delete["person_state"]), policy="到期清理"))
        # 分享索引只保存仍可能成为候选的内容。
        valid_notices = set()
        loans = store.all("money_request")
        payments = store.all("payment")
        shares = store.all("sharing")
        receipts = store.all("share_receipt")
        delivered = {(r["id"], key) for r in shares for key in r.get("notice_ids", [])}
        delivered.update((r.get("session"), r.get("record_id")) for r in receipts if r.get("sent_at"))
        keep_loans = {p.get("purchase_request") for p in retained_plans}
        pending_bills = {label + ":" + bill["month"] for key, label in MONTHLY_BILLS.items()
                         for bill in (store.find("runtime", key) or {}).get("pending", [])}
        for loan in loans:
            key, status = loan["id"], loan["status"]
            if status == "待询问":
                valid_notices.add("wallet:" + key)
            if status == "借入" and 0 < loan["owed"] < loan["amount"]:
                valid_notices.add(f"installment:{loan['owed']}:{key}")
            if status == "已还清":
                notice = "repayment:" + key
                valid_notices.add(notice)
                if (loan["session"], notice) not in delivered:
                    keep_loans.add(key)
            if status in {"待询问", "答应稍后给", "借入"} or loan["date"] == now.date().isoformat() or loan["purpose"] in pending_bills:
                keep_loans.add(key)
        for r in receipts:
            key = r.get("record_id", "")
            if key.startswith(("wallet:", "repayment:", "installment:")) and r["id"] not in delete["share_receipt"]:
                keep_loans.add(key.split(":", 2)[2] if key.startswith("installment:") else key.partition(":")[2])
        # 最新一笔流水标记钱包已经启用；保留在用日程的入账凭据和未还借款的到账顺序。
        keep_payments = {max(payments, key=lambda r: r["at"])["id"]} if payments else set()
        kept_plan_ids = {p["id"] for p in retained_plans}
        last_transfer = {}
        for row in payments:
            kind, _, source = row["id"].partition(":")
            if kind in {"来款", "还款"}:
                loan_id = source if kind == "来款" else source.partition(":")[2]
                last_transfer[loan_id] = max(row["at"], last_transfer.get(loan_id, ""))
            if kind in {"工钱", "消费", "车费", "还款"} and source.partition(":")[0] in kept_plan_ids:
                keep_payments.add(row["id"])
            if kind == "来款" and source in keep_loans:
                keep_payments.add(row["id"])
        for category, label, rows, field, protected in (
            ("payment", "钱包流水", payments, "at", keep_payments),
            ("money_request", "借还与生活费申请", [{**r, "last_at": max(r["date"], r.get("updated_at", ""),
                r.get("purchased_at", ""), last_transfer.get(r["id"], ""))} for r in loans], "last_at", keep_loans),
        ):
            delete[category] = select(rows, cfg.log_days, cfg.log_count, field, protected)
            summary.append(dict(category=category, label=label, total=len(rows), days=cfg.log_days,
                                limit=cfg.log_count, protected=sum(r["id"] in protected for r in rows), remove=len(delete[category])))
        valid_notices.difference_update("repayment:" + key for key in delete["money_request"])
        for row in store.all("thread"):
            if row["status"] == "open":
                valid_notices.update(("thread:" + row["id"], f"thread:{row['id']}:{row.get('updated_at', row['due_at'])}"))
        for row in plans:
            if row["status"] in {"scheduled", "active"}:
                valid_notices.add(f"reminder:{row['id']}:{row['start']}")
            if row["status"] == "awaiting_confirmation":
                valid_notices.add(f"confirmation:{row['id']}")
        valid_notices.update(f"issue:{r['id']}:{r['reason']}" for r in store.all("schedule_issue") if r["id"] in kept_plan_ids)
        sharing_updates = {}
        index_removed = 0
        events_in_window = {e["id"] for e in events if self.time(e["occurred_at"]) >= now - timedelta(hours=6)}
        for row in shares:
            notices = [key for key in row.get("notice_ids", []) if key in valid_notices or
                       key.startswith("anniversary:") and key.rsplit(":", 1)[-1] >= now.date().isoformat() or
                       key.startswith("greeting:") and key.rsplit(":", 2)[-2] >= now.date().isoformat()]
            experiences = [key for key in row.get("experience_ids", []) if key in events_in_window]
            if notices != row.get("notice_ids", []) or experiences != row.get("experience_ids", []):
                sharing_updates[row["id"]] = {**row, "notice_ids": notices, "experience_ids": experiences}
                index_removed += len(row.get("notice_ids", [])) - len(notices) + len(row.get("experience_ids", [])) - len(experiences)
        # 旧版本可能已删掉报信索引，趁成功回执还在时补回。
        for receipt in receipts:
            key = receipt.get("record_id", "")
            if key in valid_notices and key.startswith(("wallet:", "repayment:", "installment:")) and receipt.get("sent_at"):
                session = receipt["session"]
                row = sharing_updates.get(session) or store.find("sharing", session) or {"id": session, "experience_ids": []}
                if key not in row.setdefault("notice_ids", []):
                    row["notice_ids"].append(key)
                    sharing_updates[session] = row
        self.sharing_updates = sharing_updates
        summary.append(dict(category="sharing_index", label="旧分享索引", total=sum(len(r.get("notice_ids", [])) + len(r.get("experience_ids", [])) for r in store.all("sharing")), days=0, limit=0, protected=0,
                            remove=index_removed, policy="话题到期清理"))
        for category in ("news", "research", "reading"):
            rows = store.all(category, omit=("content", "results"))
            delete[category] = select(rows, cfg.world_days, cfg.world_count, "retrieved_at", set())
            label = {"news": "订阅资讯", "research": "检索缓存", "reading": "网页缓存"}[category]
            summary.append(dict(category=category, label=label, total=len(rows), days=cfg.world_days,
                                limit=cfg.world_count, protected=0, remove=len(delete[category])))
        for category in ("research_task", "reflection", "schedule_issue", "schedule_notice"):
            delete[category] = {r["id"] for r in store.all(category) if r["id"] not in kept_plan_ids}
        adjustments = store.all("adjustment")
        delete["adjustment"] = {r["id"] for r in adjustments if r["plan_id"] not in kept_plan_ids}
        delete["adjustment"].update(select([r for r in adjustments if r["plan_id"] in kept_plan_ids],
                                           cfg.log_days, cfg.log_count, "at", set()))
        summary.append(dict(category="adjustment", label="日程动态调整", total=len(adjustments), days=cfg.log_days,
                            limit=cfg.log_count, protected=0, remove=len(delete["adjustment"])))
        for category in ("occurrence", "routine_issue"):
            delete[category] = {r["id"] for r in store.all(category) if r["id"].rsplit(":", 1)[-1] < now.date().isoformat()}
        auxiliary = ("research_task", "reflection", "schedule_issue", "schedule_notice", "occurrence", "routine_issue")
        summary.append(dict(category="related", label="旧日程附属记录", policy="随日程清理", total=sum(len(store.all(c)) for c in auxiliary), days=0, limit=0, protected=0, remove=sum(len(delete[c]) for c in auxiliary)))
        histories = {e["id"]: e["history"][-cfg.history_count:] for e in events if e["id"] not in delete["experience"] and cfg.history_count and len(e.get("history", [])) > cfg.history_count}
        summary.append(dict(category="history", label="经历旧修订", total=sum(len(e.get("history", [])) for e in events), days=0, limit=cfg.history_count, protected=0, remove=sum(len(store.get("experience", key)["history"]) - len(value) for key, value in histories.items())))
        for category, field, label in HISTORY_FIELDS:
            rows = retained_plans if category == "plan" else store.all(category)
            summary.append(dict(category=category+"_history", label=label, total=sum(len(r.get(field, [])) for r in rows),
                                days=0, limit=cfg.history_count, protected=0,
                                remove=sum(max(0, len(r.get(field, []))-cfg.history_count) for r in rows) if cfg.history_count else 0))
        exceptions = [day for r in store.all("routine") for day in r.get("exceptions", [])]
        summary.append(dict(category="routine_exceptions", label="重复日程跳过日期", total=len(exceptions), days=0, limit=0,
                            protected=0, remove=sum(day < now.date().isoformat() for day in exceptions), policy="过期清理"))
        for category, label in (("diary", "生活日记"), ("diary_publication", "空间发布记录"),
                                ("book_note", "阅读笔记"), ("dream", "梦境")):
            summary.append(dict(category=category, label=label, total=store.count(category), days=0,
                                limit=0, protected=0, remove=0, policy="长期保留"))
        removed_images, _, image_summary = self.images.collect(now)
        summary.append(image_summary)
        kept_event_ids = {e["id"] for e in events} - delete["experience"]
        preferences = store.all("preference")
        self.preference_updates = {}
        removed_evidence = 0
        for row in preferences:
            evidence = row.get("evidence_ids", [])
            kept = [key for key in evidence if key in kept_event_ids]
            if cfg.history_count:
                kept = kept[-cfg.history_count:]
            if kept != evidence:
                self.preference_updates[row["id"]] = {**row, "evidence_ids": kept}
                removed_evidence += len(evidence) - len(kept)
        summary.append(dict(category="preference_history", label="偏好参考经历", total=sum(len(r.get("evidence_ids", [])) for r in preferences),
                            days=0, limit=cfg.history_count, protected=0, remove=removed_evidence))
        removed_names = {path.name for path in removed_images}
        image_rows = store.all("experience_image")
        delete["experience_image"] = {r["id"] for r in image_rows if r["id"] not in kept_event_ids
                                      and r.get("image_status") != "生成中"
                                      and (not r.get("image_file") or Path(r["image_file"]).name in removed_names)}
        summary.append(dict(category="experience_image", label="失去关联的经历配图", total=len(image_rows), days=0, limit=0,
                            protected=0, remove=len(delete["experience_image"]), policy="随经历和图片清理"))
        uploads = store.all("book_upload")
        delete["book_upload"] = set()
        for row in uploads:
            path = self.book_dir / (row["id"] + row["suffix"])
            if not path.exists() or cfg.log_days and path.stat().st_mtime < (now - timedelta(days=cfg.log_days)).timestamp():
                delete["book_upload"].add(row["id"])
        summary.append(dict(category="book_upload", label="未完成的书籍上传", total=len(uploads), days=cfg.log_days,
                            limit=0, protected=0, remove=len(delete["book_upload"]), policy="从最后一次上传起算"))
        return delete, histories, summary

    def preview(self, now):
        _, _, rows = self.collect(now)
        return {"rows": rows, "total": sum(r["remove"] for r in rows), "last": self.store.find("runtime", "retention") or {}, "enabled": self.config.retention.enabled}

    def clean(self, now):
        delete, histories, summary = self.collect(now)
        store = self.store
        self.images.clean(now)
        with store.db:
            for key in delete["book_upload"]:
                for suffix in (store.get("book_upload", key)["suffix"], ".json"):
                    (self.book_dir / (key + suffix)).unlink(missing_ok=True)
            for row in self.sharing_updates.values():
                if row["notice_ids"] or row["experience_ids"]:
                    store.put("sharing", row)
                else:
                    store.delete("sharing", row["id"])
            for plan_id in delete["plan"]:
                note = store.find("book_note", plan_id)
                if note:
                    note["plan_status"] = store.get("plan", plan_id)["status"]
                    store.put("book_note", note)
            for category, ids in delete.items():
                for record_id in ids:
                    store.delete(category, record_id)
            for record_id, history in histories.items():
                store.put("experience", {**store.get("experience", record_id), "history": history})
            if self.config.retention.history_count:
                for category, field, _ in HISTORY_FIELDS:
                    for row in store.all(category):
                        if len(row.get(field, [])) > self.config.retention.history_count:
                            store.put(category, {**row, field: row[field][-self.config.retention.history_count:]})
            for row in store.all("routine"):
                kept = [day for day in row.get("exceptions", []) if day >= now.date().isoformat()]
                if kept != row.get("exceptions", []):
                    store.put("routine", {**row, "exceptions": kept})
            for row in self.preference_updates.values():
                store.put("preference", row)
            result = {"id": "retention", "at": now.isoformat(), "removed": sum(r["remove"] for r in summary), "rows": summary}
            store.put("runtime", result)
        return result

    def daily(self, now):
        last = self.store.find("runtime", "retention")
        if self.config.retention.enabled and (not last or self.time(last["at"]).date() < now.date()):
            return self.clean(now)
