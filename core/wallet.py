"""角色共用的虚拟钱包、活动收支和私聊借款。"""

from uuid import uuid4


class Wallet:
    def __init__(self, store):
        self.store = store

    def account(self):
        return self.store.find("runtime", "wallet") or {"id": "wallet", "opening": 100, "balance": 100}

    def set_opening(self, amount):
        if amount < 0 or self.store.count("payment"):
            raise ValueError("起始生活费可在第一笔收支前设置，金额须为非负整数")
        with self.store.db:
            return self.store.put("runtime", {"id": "wallet", "balance": amount, "opening": amount})

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
                experience["details"]["生活支出"] = f"{cost}虚拟元"
            if plan.get("pay"):
                experience["details"]["工作报酬"] = f"{plan['pay']}虚拟元"
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

    def planning_note(self, now):
        rows = [r for r in self.store.all("money_request") if r["date"] == now.date().isoformat() and not r["source_session"] and not r.get("purchased_at")]
        if not rows:
            return "暂无"
        row = rows[-1]
        return f"{row['title']}预计{row['price']}元，{row['status']}。决定去办时沿用原活动标题，手头紧可安排免费的活动。"

    def purchase_for(self, session, title, continuation_id):
        return next((r["id"] for r in reversed(self.store.all("money_request"))
                     if r["source_session"] == session and r["purpose"] == (continuation_id or title)
                     and not r.get("purchased_at") and r["status"] in {"借入", "赠予", "已够用", "已还清"}), "")

    def need(self, title, price, session, now, continuation_id=""):
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
        with self.store.db:
            self.store.put("money_request", {"id": uuid4().hex[:12], "title": title, "price": price,
                "date": today, "purpose": purpose, "source_session": session, "session": "", "user": "", "status": "待询问",
                "amount": price - balance, "owed": 0})

    def refresh(self, now):
        balance = self.account()["balance"]
        with self.store.db:
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
        return ("wallet:" + request["id"], f"为「{request['title']}」还差{gap}元虚拟生活费，可以私下问对方借，领工钱后还。", None)

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
                    raise ValueError(f"这笔生活费还差{limit}元，请核对金额")
                row["amount"] = amount or limit
                self._post("来款:" + key, row["amount"], status + " · " + row["title"], now)
            row.update(status=status, owed=row["amount"] if action == "lend" else 0)
            return self.store.put("money_request", row)
