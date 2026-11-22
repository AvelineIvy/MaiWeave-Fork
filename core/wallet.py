"""角色共用的虚拟钱包、活动收支和私聊借款。"""

from calendar import monthrange
from datetime import date, timedelta
from uuid import uuid4


def rent_date(month, day):
    return month.replace(day=min(day, monthrange(month.year, month.month)[1]))


def next_month(month):
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


class Wallet:
    def __init__(self, store):
        self.store = store
        self.currency_name = "虚拟元"

    def account(self):
        return self.store.find("runtime", "wallet") or {"id": "wallet", "opening": 100, "balance": 100}

    def set_opening(self, amount):
        if amount < 0 or self.store.count("payment"):
            raise ValueError("起始生活费可在第一笔收支前设置，金额须为非负整数")
        with self.store.db:
            return self.store.put("runtime", {"id": "wallet", "balance": amount, "opening": amount})

    def configure_rent(self, amount, day, now):
        rent = self.store.find("runtime", "rent")
        if not rent and not amount:
            return
        if rent and (rent["amount"], rent["day"]) == (amount, day):
            return
        today = now.date()
        with self.store.db:
            if rent:
                self._bill_rent(rent, today)
            else:
                rent = {"id": "rent", "pending": [], "last_month": ""}
            due = rent_date(today, day)
            if due < today or due.isoformat()[:7] == rent["last_month"]:
                due = rent_date(next_month(today), day)
            rent.update(amount=amount, day=day, next=due.isoformat() if amount else "")
            self.store.put("runtime", rent)

    def _bill_rent(self, rent, today):
        while rent["next"] and rent["next"] <= today.isoformat():
            due = date.fromisoformat(rent["next"])
            month = rent["next"][:7]
            rent["pending"].append({"month": month, "amount": rent["amount"]})
            rent["last_month"] = month
            rent["next"] = rent_date(next_month(due), rent["day"]).isoformat()

    def _settle_rent(self, now):
        rent = self.store.find("runtime", "rent")
        if not rent or not (rent["pending"] or rent["next"] and rent["next"] <= now.date().isoformat()):
            return
        previous_due = rent["next"]
        previous_count = len(rent["pending"])
        self._bill_rent(rent, now.date())
        while rent["pending"]:
            bill = rent["pending"][0]
            purpose = "房租:" + bill["month"]
            title = bill["month"] + " 房租"
            requests = [r for r in self.store.all("money_request") if r["purpose"] == purpose]
            if self.account()["balance"] < bill["amount"]:
                if not requests:
                    self._need(title, bill["amount"], "", now, purpose)
                break
            self._post(purpose, -bill["amount"], title, now)
            for row in requests:
                if row["status"] in {"待询问", "答应稍后给"}:
                    row["status"] = "已够用"
                self.store.put("money_request", row)
            rent["pending"].pop(0)
        if previous_due != rent["next"] or previous_count != len(rent["pending"]):
            self.store.put("runtime", rent)

    def rent_note(self):
        rent = self.store.find("runtime", "rent")
        if rent and rent["pending"]:
            bill = rent["pending"][0]
            gap = max(0, bill["amount"] - self.account()["balance"])
            text = f"{bill['month']} 房租待缴 · {bill['amount']} {self.currency_name}"
            if gap:
                text += f" · 还差 {gap} {self.currency_name}"
            if len(rent["pending"]) > 1:
                text += f" · 共 {len(rent['pending'])} 期待缴"
            return text
        if rent and rent["amount"]:
            return f"下次交租 {rent['next']} · {rent['amount']} {self.currency_name}"
        return ""

    def _post(self, key, amount, title, now):
        if self.store.find("payment", key):
            return False
        account = self.account()
        account["balance"] += amount
        self.store.put("runtime", account)
        self.store.put("payment", {"id": key, "amount": amount, "balance": account["balance"],
                                   "title": title, "at": now.isoformat()})
        return True

    def settle(self, plan, now):
        """随完成记录在同一个事务里记账。"""
        fare = sum(leg.get("fare", 0) for leg in (plan.get("route") or {}).get("legs", []))
        cost = plan.get("cost", 0) + fare
        if cost or plan.get("pay"):
            experience = self.store.get("experience", plan["experience_id"])
            if cost:
                experience["details"]["生活支出"] = f"{cost}{self.currency_name}"
            if plan.get("pay"):
                experience["details"]["工作报酬"] = f"{plan['pay']}{self.currency_name}"
            self.store.put("experience", experience)
        if plan.get("purchase_request"):
            request = self.store.get("money_request", plan["purchase_request"])
            request["purchased_at"] = now.isoformat()
            self.store.put("money_request", request)
        if cost:
            self._post("消费:" + plan["id"], -cost, plan["title"], now)
        amount = plan.get("pay", 0)
        if amount and self._post("工钱:" + plan["id"], amount, plan["title"], now):
            available = min(amount, self.account()["balance"])
            for request in self.store.all("money_request"):
                if request["status"] != "借入" or available <= 0:
                    continue
                paid = min(request["owed"], available)
                self._post("还款:" + plan["id"] + ":" + request["id"], -paid, "归还借款 · " + request["title"], now)
                request["owed"] -= paid
                available -= paid
                if not request["owed"]:
                    request["status"] = "已还清"
                self.store.put("money_request", request)
        if amount:
            self._settle_rent(now)

    def planning_note(self, now):
        rows = [r for r in self.store.all("money_request") if r["date"] == now.date().isoformat() and not r["source_session"] and not r.get("purchased_at") and not r["purpose"].startswith("房租:")]
        if not rows:
            return "暂无"
        row = rows[-1]
        return f"{row['title']}预计{row['price']}{self.currency_name}，{row['status']}。决定去办时沿用原活动标题，手头紧可安排免费的活动。"

    def purchase_for(self, session, title, continuation_id):
        return next((r["id"] for r in reversed(self.store.all("money_request"))
                     if r["source_session"] == session and r["purpose"] == (continuation_id or title)
                     and not r.get("purchased_at") and r["status"] in {"借入", "赠予", "已够用", "已还清"}), "")

    def need(self, title, price, session, now, continuation_id=""):
        with self.store.db:
            self._need(title, price, session, now, continuation_id)

    def _need(self, title, price, session, now, continuation_id):
        balance = self.account()["balance"]
        if price <= balance:
            return
        rows = self.store.all("money_request")
        today = now.date().isoformat()
        purpose = continuation_id or title
        if any(r["status"] == "答应稍后给" or r["date"] == today or
               (r["source_session"] == session and r["purpose"] == purpose and r["status"] in {"已过期", "已婉拒", "待询问"})
               for r in rows):
            return
        self.store.put("money_request", {"id": uuid4().hex[:12], "title": title, "price": price,
            "date": today, "purpose": purpose, "source_session": session, "session": "", "user": "", "status": "待询问",
            "amount": price - balance, "owed": 0})

    def refresh(self, now):
        with self.store.db:
            self._settle_rent(now)
            balance = self.account()["balance"]
            for row in self.store.all("money_request"):
                if row["status"] in {"待询问", "答应稍后给"}:
                    if row["price"] <= balance:
                        row["status"] = "已够用"
                    elif row["status"] == "待询问" and row["date"] < now.date().isoformat():
                        row["status"] = "已过期"
                    else:
                        continue
                    self.store.put("money_request", row)

    def pending(self, now):
        return [r for r in self.store.all("money_request") if r["date"] == now.date().isoformat()
                and r["status"] == "待询问" and r["price"] > self.account()["balance"]]

    def notice(self, request, session, user):
        if request["source_session"] not in ("", session) or request["session"] not in ("", session):
            return None
        gap = request["price"] - self.account()["balance"]
        if request["session"]:
            gap = min(gap, request["amount"])
        if gap <= 0:
            return None
        with self.store.db:
            request.update(session=session, user=user, amount=gap)
            self.store.put("money_request", request)
        return ("wallet:" + request["id"], f"{request['title']}还差{gap}{self.currency_name}，能借我一下吗？下次领了工钱就还你。", None)

    def current_notice(self, receipt, now):
        key = receipt.get("record_id", "")
        if not key.startswith("wallet:"):
            return True
        request = self.store.get("money_request", key.split(":", 1)[1])
        return (request["session"] == receipt["session"] and request["status"] == "待询问"
                and request["date"] == now.date().isoformat()
                and request["amount"] <= request["price"] - self.account()["balance"])

    def respond(self, key, session, user, action, now, amount=0):
        row = self.store.get("money_request", key)
        if (row["session"], row["user"]) != (session, user):
            raise ValueError("请选择当前私聊的借款")
        if row["status"] not in {"待询问", "答应稍后给"}:
            return row
        if row["price"] <= self.account()["balance"] or (row["date"] != now.date().isoformat() and row["status"] == "待询问"):
            raise ValueError("这笔生活费已经够用或申请已过期")
        status = {"promise": "答应稍后给", "lend": "借入", "give": "赠予", "decline": "已婉拒"}[action]
        with self.store.db:
            if action in {"lend", "give"}:
                limit = min(row["amount"], row["price"] - self.account()["balance"])
                if amount < 0 or amount > limit:
                    raise ValueError(f"这笔生活费还差{limit}{self.currency_name}，请核对金额")
                row["amount"] = amount or limit
                self._post("来款:" + key, row["amount"], status + " · " + row["title"], now)
            row.update(status=status, owed=row["amount"] if action == "lend" else 0)
            self.store.put("money_request", row)
            if action in {"lend", "give"}:
                self._settle_rent(now)
            return row
