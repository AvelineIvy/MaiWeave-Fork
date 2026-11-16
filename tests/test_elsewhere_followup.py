"""群里接话、实际回执、跨聊天接续与睡眠近况。"""
import json
from datetime import timedelta
import pytest
from test_plugin import plugin


def setup(p):
    now=p.now()
    p.now=lambda:now
    p.config.proactive.enabled=True
    p.config.life.schedule_enabled=False
    p.config.memory.enabled=False
    p.ctx.chat.get_all_streams.return_value=[
        {"stream_id":"private","user_id":"u","platform":"qq"},
        {"stream_id":"group","group_id":"g","platform":"qq"}]
    with p.store.db:
        p.store.put("share_receipt",{"id":"first","session":"private","created":(now-timedelta(hours=1)).isoformat(),
            "sent_at":(now-timedelta(hours=1)).isoformat(),"outcome":"实际已发送","sent_text":"PRIVATE_SECRET"})
        for session in ("private","group","other-group"):
            p.store.put("identity",{"id":session+":u","session_id":session,"user_id":"u","person_id":"person-u"})
    return now


def message(session="group",text="大家聊什么",user="u"):
    return {"session_id":session,"platform":"qq","processed_plain_text":text,
            "message_info":{"user_info":{"user_id":user},**({"group_info":{"group_id":"g"}} if session!="private" else {})}}


def reply():
    return [{"item_type":"FunctionCallItem","tool_call":{"func_name":"reply","call_id":"reply-one"}}]


async def offer(p):
    await p.management_message(message())
    result=await p.planner_context(session_id="group",items=[])
    text=result["modified_kwargs"]["items"][-1]["parts"][0]["text"]
    assert '"encounters"' in text
    return text


async def delivered(p,now):
    async def get(table,**kwargs):
        if table=="ToolRecord":
            return {"tool_name":"reply","timestamp":now.isoformat(),"tool_data":json.dumps({"success":True,"metadata":{"sent_message_ids":["sent"]}})}
        return {"processed_plain_text":"原来你在这儿呀","timestamp":now.isoformat()}
    p.ctx.db.get.side_effect=get
    await p.living.receipts.refresh(now)


@pytest.mark.parametrize("mode",["自然交流","主动表达"])
async def test_group_opportunity_uses_planner_not_private_proactive(plugin,mode):
    p=plugin;now=setup(p);p.config.proactive.mode=mode
    text=await offer(p)
    assert "PRIVATE_SECRET" not in text and "未回复" not in text
    assert p.store.get("share_receipt","first")["elsewhere"]["status"]=="pending"
    await p.planner_receipt("group",reply())
    row=p.store.all("share_receipt")[-1]
    assert row["mode"]=="群聊接话" and "sent_at" not in row
    await delivered(p,now)
    row=p.store.get("share_receipt",row["id"])
    assert row["outcome"]=="实际已发送" and row["sent_text"]=="原来你在这儿呀"
    assert p.living.receipts.sent_times("group",now.tzinfo)==[]
    assert len(p.living.receipts.sent_times("private",now.tzinfo))==1
    p.ctx.maisaka.proactive.trigger.assert_not_awaited()
    p.ctx.send.text.assert_not_awaited()
    p.ctx.llm.generate.assert_not_awaited()


@pytest.mark.parametrize("items",[[],[{"item_type":"FunctionCallItem","tool_call":{"func_name":"stop","call_id":"stop"}}]])
async def test_silence_consumes_once_without_private_fallback(plugin,items):
    p=plugin;now=setup(p)
    await offer(p)
    await p.planner_receipt("group",items)
    await p.management_message(message(text="再说一句"))
    assert p.presence.encounter_context("group","u",now,True)==[]
    assert p.presence.encounter_context("private","u",now,False)==[]
    assert len(p.store.all("share_receipt"))==1
    assert p.store.get("share_receipt","first")["elsewhere"]["status"]=="silent"


@pytest.mark.parametrize("session",["group","private"])
async def test_user_chooses_where_to_continue(plugin,session):
    p=plugin;now=setup(p)
    await offer(p);await p.planner_receipt("group",reply());await delivered(p,now)
    later=now+timedelta(seconds=2);p.now=lambda:later
    await p.management_message(message(session,"被你发现了"))
    rows=p.presence.encounter_context(session,"u",later,session=="group")
    assert rows[0]["said"]=="原来你在这儿呀" and "换话题就聊新话题" in rows[0]["guide"]
    assert p.presence.encounter_context("other-group","u",later,True)==[]
    assert p.presence.encounter_context(session,"someone-else",later,session=="group")==[]
    assert p.presence.encounter_context(session,"u",now+timedelta(hours=3),session=="group")==[]


@pytest.mark.parametrize("public",[True,False])
async def test_sleep_statement_uses_same_group_flow(plugin,public):
    p=plugin;now=setup(p)
    with p.store.db:p.store.delete("share_receipt","first")
    p.presence.declare("group" if public else "private","u","准备睡觉",now-timedelta(minutes=5))
    text=await offer(p)
    assert ("先前在本群说去睡了" in text)==public
    if not public:assert "准备睡觉" not in text
    await p.planner_receipt("group",reply());await delivered(p,now)
    assert p.store.get("person_state","person-u")["elsewhere"]["status"]=="handled"
    assert p.presence.opportunities("group","u",now)==[]


@pytest.mark.parametrize("gate",["disabled","sleeping","replied","expired"])
async def test_unavailable_opportunity_not_offered(plugin,monkeypatch,gate):
    p=plugin;now=setup(p)
    if gate=="disabled":p.config.proactive.enabled=False
    if gate=="sleeping":monkeypatch.setattr(p.living,"sleeping",lambda _:True)
    if gate=="replied":p.living.receipts.receive("private","u","我看到了",now)
    await p.management_message(message())
    if gate=="expired":now+=timedelta(minutes=31)
    assert p.presence.encounter_context("group","u",now,True)==[]


@pytest.mark.parametrize("budget",[1200,2400,4800])
async def test_context_budget_and_only_included_opportunities_consumed(plugin,budget):
    p=plugin;now=setup(p);p.config.context.group_chars=budget
    await p.management_message(message())
    text=p.context_text("group")
    assert len(text)<=budget
    await p.planner_receipt("group",[])
    status=p.store.get("share_receipt","first")["elsewhere"]["status"]
    assert status==("silent" if '"encounters"' in text else "pending")


async def test_query_tool_round_keeps_opportunity_until_reply(plugin):
    p=plugin;now=setup(p);await offer(p)
    await p.planner_receipt("group",[{"item_type":"FunctionCallItem","tool_call":{"func_name":"maiweave_query","call_id":"query"}}])
    assert p.store.get("share_receipt","first")["elsewhere"]["status"]=="pending"
    await p.planner_context(session_id="group",items=[])
    await p.planner_receipt("group",reply())
    assert len(p.store.all("share_receipt"))==2


async def test_failed_reply_does_not_create_followup_context(plugin):
    p=plugin;now=setup(p);await offer(p);await p.planner_receipt("group",reply())
    p.ctx.db.get.return_value={"tool_name":"reply","timestamp":now.isoformat(),"tool_data":json.dumps({"success":False,"error_message":"发送失败"})}
    await p.living.receipts.refresh(now)
    assert p.store.all("share_receipt")[-1]["outcome"]=="发送失败"
    p.living.receipts.receive("private","u","你好",now+timedelta(seconds=2))
    assert p.presence.encounter_context("private","u",now+timedelta(seconds=2),False)==[]


async def test_existing_pending_observation_uses_native_identity(plugin):
    p=plugin;now=setup(p)
    row=p.store.get("share_receipt","first")
    row["elsewhere"]={"session_id":"group","user_id":"u","at":now.isoformat(),"status":"pending","text":"旧版已有的群发言"}
    with p.store.db:p.store.put("share_receipt",row)
    await offer(p)
    await p.planner_receipt("group",reply())
    assert p.store.all("share_receipt")[-1]["encounter_person"]=="person-u"

@pytest.mark.parametrize("text", ["我去睡了", "我去睡了，晚安", "@测试角色  我去睡了，晚安！"])
async def test_sleep_declaration_is_not_later_group_activity(plugin, text):
    p=plugin;now=setup(p)
    with p.store.db:p.store.delete("share_receipt","first")
    ticks=iter(now+timedelta(microseconds=n) for n in range(100))
    p.now=lambda:next(ticks)
    await p.management_message(message(text=text))
    state=p.store.get("person_state","person-u")
    assert state["status"]=="准备睡觉"
    assert "elsewhere" not in state
    await p.management_message(message(text="又回来看看"))
    assert p.store.get("person_state","person-u")["elsewhere"]["kind"]=="sleep"

@pytest.mark.parametrize("text", ["他说我去睡了，晚安", "我去睡了，晚安可以这么说吗", "我不去睡了，晚安"])
def test_sleep_statement_keeps_sentence_meaning(plugin,text):
    p=plugin;now=setup(p)
    p.presence.receive("group","u",text,now)
    assert p.store.find("person_state","person-u") is None
