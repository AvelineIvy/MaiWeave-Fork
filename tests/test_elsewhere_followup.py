"""主动私聊等待、群里发言、关系判断与发送额度。"""
import json
from datetime import timedelta
import pytest
from test_plugin import plugin
from test_proactive_modes import prepare


def setup(p, monkeypatch):
    now = prepare(p, monkeypatch)
    p.config.life.schedule_enabled = False
    p.config.memory.enabled = False
    p.config.proactive.natural_greetings = False
    p.config.proactive.cooldown_minutes = 30
    p.ctx.chat.get_all_streams.return_value = [
        {"stream_id":"chat-a", "user_id":"u", "platform":"qq", "user_nickname":"朋友"},
        {"stream_id":"group", "group_id":"g", "platform":"qq"}]
    with p.store.db:
        for event in p.store.all("experience"):
            p.store.delete("experience",event["id"])
        p.store.put("share_receipt", {"id":"first", "session":"chat-a", "created":(now-timedelta(hours=1)).isoformat(),
            "sent_at":(now-timedelta(hours=1)).isoformat(), "outcome":"实际已发送", "topic":"小猫书签", "sent_text":"给你看我折的书签"})
    p.ctx.llm.generate.side_effect = None
    p.ctx.llm.generate.return_value = {"success":True,"response":json.dumps({"send":True,"reason":"相处亲近，可以轻轻打趣"})}
    return now


def message(text="大家在聊什么", user="u", platform="qq"):
    return {"session_id":"group", "platform":platform,"processed_plain_text":text,
            "message_info":{"group_info":{"group_id":"g"},"user_info":{"user_id":user}}}


@pytest.mark.parametrize("mode", ["主动表达", "自然交流"])
async def test_group_activity_reaches_original_private_once(plugin, monkeypatch, mode):
    p=plugin; now=setup(p,monkeypatch); p.config.proactive.mode=mode
    await p.presence.observe_group(message(),now)
    await p.living.share(now)
    row=p.store.all("share_receipt")[-1]
    assert row["record_id"]=="elsewhere:first" and row["session"]=="chat-a"
    assert row["topic"].startswith("回到当前私聊") and not row.get("image_requested")
    assert p.ctx.llm.generate.await_count==1
    await p.presence.observe_group(message("又说了一句"),now+timedelta(minutes=1))
    await p.living.share(now+timedelta(minutes=1))
    assert p.ctx.llm.generate.await_count==1
    if mode=="主动表达":
        p.ctx.send.text.assert_awaited_once()
        assert len(p.living.receipts.sent_times("chat-a",now.tzinfo))==2
    else:
        p.ctx.maisaka.proactive.trigger.assert_awaited_once()
        assert len(p.living.receipts.sent_times("chat-a",now.tzinfo))==1


@pytest.mark.parametrize("change", ["other_user","other_platform","command","private","replied","expired","unsent","closed"])
async def test_only_pending_same_person_private_triggers(plugin,monkeypatch,change):
    p=plugin;now=setup(p,monkeypatch);msg=message()
    row=p.store.get("share_receipt","first")
    if change=="other_user":msg["message_info"]["user_info"]["user_id"]="v"
    if change=="other_platform":msg["platform"]="other"
    if change=="command":msg["processed_plain_text"]="/help"
    if change=="private":msg["message_info"].pop("group_info")
    if change=="replied":row["observed_at"]=now.isoformat()
    if change=="expired":row["sent_at"]=(now-timedelta(days=3)).isoformat()
    if change=="unsent":row["outcome"]="Planner 选择沉默"
    if change=="closed":row["followup"]="closed"
    with p.store.db:p.store.put("share_receipt",row)
    await p.presence.observe_group(msg,now)
    assert not p.store.get("share_receipt","first").get("elsewhere")


@pytest.mark.parametrize("gate",["replied","expired","limit","cooldown","sleep","disabled"])
async def test_waiting_keeps_existing_gates(plugin,monkeypatch,gate):
    p=plugin;now=setup(p,monkeypatch)
    await p.presence.observe_group(message(),now)
    if gate=="replied":p.living.receipts.receive("chat-a","u","刚忙完",now)
    if gate=="expired":now+=timedelta(minutes=31)
    if gate=="limit":p.config.proactive.daily_limit=1
    if gate=="cooldown":p.config.proactive.cooldown_minutes=120
    if gate=="sleep":monkeypatch.setattr(p.living,"proactive_pause",lambda _: {"message":"午睡中"})
    if gate=="disabled":p.config.proactive.enabled=False
    await p.living.share(now)
    p.ctx.llm.generate.assert_not_awaited()
    p.ctx.send.text.assert_not_awaited()


async def test_model_waits_once_and_prompt_budget(plugin,monkeypatch):
    p=plugin;now=setup(p,monkeypatch)
    p.config.context.private_chars=1200
    p.ctx.llm.generate.return_value={"success":True,"response":json.dumps({"send":False,"reason":"对方在处理正事，先等着"})}
    await p.presence.observe_group(message("工作求助"*200),now)
    await p.living.share(now)
    await p.living.share(now)
    assert p.ctx.llm.generate.await_count==1
    assert len(p.ctx.llm.generate.await_args.args[0])<=1200
    assert p.store.get("share_receipt","first")["elsewhere"]["status"]=="waiting"
    p.ctx.send.text.assert_not_awaited()


async def test_private_reply_during_judgment_ends_wait(plugin,monkeypatch):
    p=plugin;now=setup(p,monkeypatch)
    await p.presence.observe_group(message(),now)
    async def answer(*args,**kwargs):
        p.living.receipts.receive("chat-a","u","书签好可爱",now)
        return {"success":True,"response":json.dumps({"send":True,"reason":"亲近"})}
    p.ctx.llm.generate.side_effect=answer
    await p.living.share(now)
    p.ctx.send.text.assert_not_awaited()


async def test_receive_hook_and_new_private_reply(plugin,monkeypatch):
    p=plugin;now=setup(p,monkeypatch);p.now=lambda:now
    await p.management_message(message())
    assert p.store.get("share_receipt","first")["elsewhere"]["session_id"]=="group"
    await p.management_message({"session_id":"chat-a","platform":"qq","processed_plain_text":"刚看到了",
                                "message_info":{"user_info":{"user_id":"u"}}})
    await p.living.share(now)
    p.ctx.send.text.assert_not_awaited()


async def test_relation_is_in_judgment_and_no_group_context_leak(plugin,monkeypatch):
    p=plugin;now=setup(p,monkeypatch)
    with p.store.db:
        p.store.put("profile",{"id":"friend","session_id":"chat-a","user_id":"u","intimacy_mode":"manual","intimacy":3,"relationship":"亲密"})
    await p.presence.observe_group(message(),now)
    await p.living.share(now)
    assert '"有效亲密度": 3' in p.ctx.llm.generate.await_args.args[0]
    assert p.living.receipts.context("group",now)==[]
    assert p.ctx.send.text.await_args.args[1]=="chat-a"
