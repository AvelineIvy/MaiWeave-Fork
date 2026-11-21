"""通过排班、生活推进和私聊工具核对角色收支。"""
import json
from datetime import timedelta

import pytest

from maiweave.core.engine import LifeEngine
from maiweave.core.models import ExperienceInput
from test_plugin import plugin
from test_proactive_modes import prepare


async def test_shift_move_leave_and_settlement_after_restart(plugin, monkeypatch):
    p = plugin
    now = p.now().replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=1)
    monkeypatch.setattr(p, "now", lambda: now)
    p.engine.wallet.set_opening(20)
    await p.ui_save_place("花店")
    await p.ui_save_route("住处", "花店", 10, "公交", fare=2)
    rule = await p.ui_save_routine("花店早班", now.date().isoformat(), "10:00", 60,
                                  frequency="daily", kind="work", location="花店", pay=50)
    shifts = sorted((r for r in p.store.all("plan") if r.get("routine_id") == rule["id"]), key=lambda r: r["start"])
    shift = shifts[0]
    await p.ui_update_plan(shift["id"], new_start=now.replace(hour=14).isoformat(), new_end=now.replace(hour=15).isoformat())
    assert p.store.get("plan", shift["travel_id"])["status"] == "cancelled"
    await p.ui_routine_action(rule["id"], "skip", shifts[1]["routine_date"])
    assert p.store.get("plan", shifts[1]["id"])["status"] == "cancelled"
    page = await p.ui_space()
    assert page["shifts"][0]["at"] == now.replace(hour=14).isoformat()
    assert page["wallet"]["balance"] == 20
    after = now.replace(hour=15, minute=1)
    p.engine.advance(after)
    assert sorted(r["amount"] for r in p.store.all("payment")) == [-2, 50]
    assert p.engine.wallet.account()["balance"] == 68
    resumed = LifeEngine(p.store, p.engine.timezone, after)
    resumed.advance(after + timedelta(days=1))
    assert resumed.wallet.account()["balance"] == 68
    monkeypatch.setattr(p, "now", lambda: after)
    await p.ui_save_routine("花店晚班", now.date().isoformat(), "17:00", 30, frequency="daily",
                            kind="work", location="花店", pay=30, rule_id=rule["id"])
    assert len([r for r in p.store.all("plan") if r.get("routine_date") == shift["routine_date"] and r["status"] != "cancelled"]) == 1


@pytest.mark.parametrize("mode", ["自然交流", "主动表达"])
async def test_private_loan_to_wages_and_repayment(plugin, monkeypatch, mode):
    p = plugin
    now = prepare(p, monkeypatch)
    monkeypatch.setattr(p, "now", lambda: now)
    p.config.proactive.mode = mode
    p.config.proactive.session_ids = ["group", "chat-a", "chat-b"]
    peers = [{"stream_id": s, "user_id": s, "platform": "qq", "group_id": "group" if s=="group" else ""}
             for s in p.config.proactive.session_ids]
    p.ctx.chat.get_all_streams.return_value = peers
    for peer in peers:
        with p.store.db:
            p.store.put("profile", {"id": peer["stream_id"], "name": "小林", "session_id": peer["stream_id"],
                "user_id": peer["user_id"], "intimacy_mode": "manual", "intimacy": 2})
    p.engine.wallet.set_opening(2)
    wallet = p.engine.wallet
    wallet.need("买毛线", 10, "", now)
    await p.living.share(now)
    row, = p.store.all("money_request")
    assert row["session"] == "chat-a" and row["amount"] == 8
    assert [r["session"] for r in p.store.all("share_receipt") if r.get("record_id") == "wallet:"+row["id"]] == ["chat-a"]
    key = row["id"]
    async def respond(action, session="chat-a"):
        return await p.wallet_action(action, key, stream_id=session, user_id=session)
    assert not (await respond("lend", "group"))["success"]
    assert not (await respond("lend", "chat-b"))["success"]
    assert (await respond("promise"))["content"]["status"] == "答应稍后给"
    assert wallet.account()["balance"] == 2
    assert (await respond("lend"))["content"]["owed"] == 8
    await respond("lend")
    assert wallet.account()["balance"] == 10
    for i, pay in enumerate([5, 10]):
        rule = await p.ui_save_routine("整理花束", now.date().isoformat(), f"{14+i}:00", 30, frequency="daily",
                                      kind="work", pay=pay, location="住处", simulation=False)
        plan = next(r for r in p.store.all("plan") if r.get("routine_id")==rule["id"])
        end = p.engine.time(plan["end"])
        p.engine.record_experience(ExperienceInput(summary="花束已经包好", occurred_at=end.isoformat(), source="reported",
                                  evidence="已下班", plan_id=plan["id"]), "", end)
        assert p.store.get("money_request",key)["owed"] == max(0, 8 - sum([5,10][:i+1]))
    assert wallet.account()["balance"] == 17
    assert sorted(r["amount"] for r in p.store.all("payment")) == [-5, -3, 5, 8, 10]
    p.ctx.maisaka.proactive.trigger.return_value = {"success": True, "task_id": "proactive:ilworkcn.maiweave:2"}
    await p.living.share(now + timedelta(hours=4))
    returned, = [r for r in p.store.all("share_receipt") if r.get("record_id") == "repayment:" + key]
    assert returned["session"] == "chat-a" and "还清" in returned["topic"]


async def test_paid_opportunity_uses_saved_place_and_experience(plugin, monkeypatch):
    p = plugin
    now = p.now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    p.config.life.autonomous = True
    with p.store.db:
        p.store.put("activity_memory", {"id":"零工", "experience_id":"花店见闻", "next_action":"店主邀我帮忙包花",
            "activity":"做手工", "subject":"包花", "session_id":"", "kind":"leisure", "location":"图书馆",
            "updated_at":now.isoformat(), "hourly_pay":30})
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success":True,"response":json.dumps(dict(title="帮忙包花", kind="work",
        location="图书馆", continuation_id="零工", minutes=60, motivation="接上上次的约定"))}
    await p.living.choose_activity(now)
    job, = [r for r in p.store.all("plan") if r["kind"]=="work"]
    assert job["pay"] == 30 and job["continuation_experience_id"] == "花店见闻"
    p.engine.advance(p.engine.time(job["end"]))
    assert p.engine.wallet.account()["balance"] == 130

@pytest.mark.parametrize("action, credited", [("give", 8), ("decline", 0)])
async def test_shortfall_purchase_and_gift(plugin, monkeypatch, action, credited):
    p = plugin
    now = p.now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=1)
    monkeypatch.setattr(p, "now", lambda: now)
    p.config.life.autonomous = True
    with p.store.db:
        p.store.put("state", {**p.engine.state(), "updated_at": now.isoformat()})
    p.engine.wallet.set_opening(2)
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps(dict(title="买毛线", location="住处",
        kind="leisure", minutes=15, cost=10, motivation="给兔子织围巾"))}
    assert "暂缓" in await p.living.choose_activity(now)
    assert not p.store.all("plan")
    request, = p.engine.wallet.pending(now)
    notice = p.engine.wallet.notice(request, "chat-a", "同伴")
    receipt = {"record_id": notice[0], "session": "chat-a"}
    assert p.engine.wallet.current_notice(receipt, now)
    result = await p.wallet_action(action, request["id"], stream_id="chat-a", user_id="同伴")
    assert result["success"] and result["content"]["owed"] == 0
    assert not p.engine.wallet.current_notice(receipt, now)
    if credited:
        await p.living.choose_activity(now)
        plan, = p.store.all("plan")
        end = p.engine.time(plan["end"])
        assert p.engine.wallet.account()["balance"] == 10
        p.engine.advance(end)
        p.engine.advance(end + timedelta(seconds=10))
        assert p.engine.wallet.account()["balance"] == 0
        assert p.store.all("experience")[-1]["details"]["生活支出"] == "10虚拟元"
        assert p.engine.wallet.planning_note(end) == "暂无"
    else:
        p.engine.wallet.need("买毛线", 10, "", now + timedelta(days=1))
        assert len(p.store.all("money_request")) == 1
        assert not p.store.all("payment")


async def test_opening_balance_config_reload(plugin):
    p = plugin
    cfg = p.get_plugin_config_data()
    cfg["life"]["opening_balance"] = 0
    p.set_plugin_config(cfg)
    await p.on_config_update("self", cfg, "0.12.6")
    assert (await p.ui_dashboard())["wallet"]["balance"] == 0
    with p.store.db:
        p.engine.wallet._post("测试工钱", 35, "工钱", p.now())
    cfg["life"]["opening_balance"] = 200
    cfg["life"]["currency_name"] = "麦币"
    p.set_plugin_config(cfg)
    await p.on_config_update("self", cfg, "0.12.6")
    dashboard = await p.ui_dashboard()
    assert dashboard["wallet"]["balance"] == 35
    assert dashboard["currency_name"] == "麦币"
    wallet = p.engine.wallet
    wallet.need("花束包装纸", 40, "", p.now())
    request, = wallet.pending(p.now())
    assert "5麦币" in wallet.notice(request, "chat-a", "伙伴")[1]
