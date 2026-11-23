"""资料模型输出、饥饿与活动候选的联动。"""
import json
from datetime import timedelta
import pytest
from pydantic import ValidationError
from test_plugin import plugin
from test_activity_connections import set_time
from maiweave.interfaces.review import Assessment
from maiweave.core.models import PlanInput


@pytest.mark.parametrize("hunger,minutes", [(25, 168), (62, 37)])
async def test_rest_ends_at_recovery_or_hunger(plugin, hunger, minutes):
    now = plugin.now().replace(hour=12, minute=0, second=0, microsecond=0)
    set_time(plugin, now)
    plugin.config.life.autonomous = True
    with plugin.store.db:
        plugin.store.put("state", {**plugin.engine.state(), "energy": 16, "hunger": hunger})
    await plugin.living.choose_activity(now)
    rest = plugin.engine.current(now)
    assert rest["kind"] == "rest"
    for step in range(1, minutes):
        at = now + timedelta(minutes=step)
        plugin.engine.advance(at)
        assert plugin.engine.current(at)["id"] == rest["id"]
    end = now + timedelta(minutes=minutes)
    plugin.engine.advance(end)
    assert plugin.store.get("plan", rest["id"])["status"] == "completed"
    assert len([e for e in plugin.store.all("experience") if e.get("plan_id") == rest["id"]]) == 1
    if hunger == 62:
        assert plugin.engine.state()["hunger"] >= 65


@pytest.mark.parametrize("hours_since_meal,expected", [(2, "rest"), (6, "meal")])
async def test_tired_after_rest_considers_time_since_meal(plugin, hours_since_meal, expected):
    p = plugin
    now = p.now().replace(hour=18, minute=0, second=0, microsecond=0)
    last_meal = now - timedelta(hours=hours_since_meal)
    start = last_meal - timedelta(minutes=20)
    set_time(p, start)
    for title, kind, began, ended in [("午饭", "meal", start, last_meal),
                                      ("闭眼休息", "rest", now - timedelta(minutes=45), now)]:
        p.engine.create_plan(PlanInput(title=title, kind=kind, start=began.isoformat(),
                                       end=ended.isoformat(), location="住处"), "", began)
        p.engine.advance(ended)
    with p.store.db:
        p.store.put("state", {**p.engine.state(), "energy": 18, "hunger": 30})
    p.config.life.autonomous = True
    p.ctx.llm.generate.return_value = {"success": True, "response": json.dumps({
        "title": "煮碗面吃", "kind": "meal", "minutes": 20, "location": "住处", "motivation": "歇过了，想简单吃点"})}
    await p.living.choose_activity(now)
    assert p.engine.current(now)["kind"] == expected


@pytest.mark.parametrize('hunger', [64, 65, 100])
@pytest.mark.parametrize('energy', [20, 80])
@pytest.mark.parametrize('night_sleep', [False, True])
async def test_hunger_energy_and_night_schedule(plugin, hunger, energy, night_sleep):
    """饥饿阈值影响清醒时的自主选择，夜间仍按作息入睡。"""
    p = plugin
    now = p.now().replace(hour=1, minute=0, second=0, microsecond=0)
    set_time(p, now)
    p.config.life.autonomous = True
    p.config.life.schedule_enabled = night_sleep
    p.config.life.sleep_hour = 1
    p.config.life.wake_hour = 7
    with p.store.db:
        p.store.put('state', {**p.engine.state(), 'hunger': hunger, 'energy': energy})
    kind = 'meal' if hunger >= 65 else 'leisure'
    p.ctx.llm.generate.return_value = {'success': True, 'response': json.dumps({
        'title': '吃点东西' if kind == 'meal' else '整理桌面', 'kind': kind,
        'minutes': 20, 'location': '住处', 'motivation': '按当前状态安排'})}
    await p.living.choose_activity(now)
    expected = 'sleep' if night_sleep else 'rest' if hunger < 65 and energy <= 25 else kind
    assert p.engine.current(now)['kind'] == expected
    assert p.ctx.llm.generate.await_count == (0 if expected in {'sleep', 'rest'} else 1)


@pytest.mark.parametrize('accept', [False, True])
async def test_classified_conflict_confirmation_preserves_manual_relationship(plugin, accept):
    """分类冲突经确认后写入资料，手动关系沿用用户选择。"""
    p = plugin
    profile = p.anniversaries.save_profile('小明', 'private', 'user')
    p.ctx.llm.generate.return_value = {'success': True, 'response': json.dumps({
        'conflicts': [{'type': '关系', 'text': '从普通关系改为好友'}],
        'relationship': '朋友', 'level': 1, 'reason': '已有互动'})}
    record = await p.reviews.submit('profile', {'profile_id': profile['id'], 'name': '小明',
        'session_id': 'private', 'user_id': 'user', 'intimacy_mode': 'manual',
        'relationship': '好友', 'intimacy': 3}, 'private')
    assert record['status'] == '待确认'
    assert record['conflicts'] == ['关系：从普通关系改为好友']
    await p.reviews.decide(record['id'], accept)
    saved = p.store.get('profile', profile['id'])
    if accept:
        assert saved['relationship'] == '好友' and saved['intimacy'] == 3
    else:
        assert saved == profile


def test_assessment_preserves_classified_conflicts():
    row=Assessment.model_validate_json(json.dumps({'conflicts':[{'type':'生日','text':'日期不同'},'称呼不同'],'relationship':'朋友','level':3}))
    assert row.conflicts==['生日：日期不同','称呼不同']
    assert row.relationship=='朋友' and row.level==3
    with pytest.raises(ValidationError):Assessment(conflicts=[],relationship='亲密')

async def test_profile_rejects_intimacy_as_relationship(plugin):
    with pytest.raises(ValidationError):
        await plugin.ui_save_profile('朋友','private',relationship='亲密')
    assert plugin.store.all('profile')==[]

@pytest.mark.parametrize('energy',[20,80])
async def test_hungry_planning_corrects_rest_to_meal(plugin,energy):
    p=plugin;now=p.now().replace(hour=12,minute=0,second=0,microsecond=0);set_time(p,now)
    p.config.life.autonomous=True;p.config.life.schedule_enabled=False
    with p.store.db:p.store.put('state',{**p.engine.state(),'hunger':100,'energy':energy})
    def response(kind):
        return {'success':True,'response':json.dumps({'title':'吃午饭' if kind=='meal' else '听音乐','kind':kind,'minutes':20,'location':'住处','motivation':'当前身体需要'})}
    p.ctx.llm.generate.side_effect=[response('rest'),response('meal')]
    await p.living.choose_activity(now)
    assert p.engine.current(now)['kind']=='meal'
    assert p.ctx.llm.generate.await_count==2
    assert '100表示非常饿' in p.ctx.llm.generate.call_args_list[0].args[0]

async def test_hidden_continuation_is_corrected(plugin):
    p=plugin;now=p.now().replace(hour=12,minute=0,second=0,microsecond=0);set_time(p,now)
    p.config.life.autonomous=True;p.config.life.schedule_enabled=False
    with p.store.db:
        for i in range(13):p.store.put('activity_memory',{'id':f'idea-{i:02}','session_id':'','kind':'leisure','next_action':'整理桌面','updated_at':now.isoformat()})
    def response(cid):return {'success':True,'response':json.dumps({'title':'整理桌面','kind':'leisure','minutes':10,'location':'住处','motivation':'整理手边的东西','continuation_id':cid})}
    p.ctx.llm.generate.side_effect=[response('idea-00'),response('')]
    await p.living.choose_activity(now)
    prompt=p.ctx.llm.generate.call_args_list[0].args[0]
    candidates=json.loads(prompt.split('可接续的具体念头：')[1].split('。选择接续')[0])
    assert len(candidates)==12 and 'idea-00' not in {r['id'] for r in candidates}
    assert p.engine.current(now)['continuation_id']==''
    assert p.ctx.llm.generate.await_count==2

async def test_recently_updated_old_continuation_stays_available(plugin):
    from datetime import timedelta
    p=plugin;now=p.now().replace(hour=12,minute=0,second=0,microsecond=0);set_time(p,now)
    p.config.life.autonomous=True;p.config.life.schedule_enabled=False
    with p.store.db:
        for i in range(13):p.store.put('activity_memory',{'id':f'idea-{i:02}','session_id':'','kind':'leisure','next_action':'整理桌面','updated_at':(now-timedelta(days=1)).isoformat()})
        p.store.put('activity_memory',{**p.store.get('activity_memory','idea-00'),'updated_at':now.isoformat()})
    p.ctx.llm.generate.return_value={'success':True,'response':json.dumps({'title':'整理桌面','kind':'leisure','minutes':10,'location':'住处','motivation':'接着整理','continuation_id':''})}
    await p.living.choose_activity(now)
    prompt=p.ctx.llm.generate.call_args.args[0]
    candidates=json.loads(prompt.split('可接续的具体念头：')[1].split('。选择接续')[0])
    assert len(candidates)==12 and candidates[-1]['id']=='idea-00'
    assert 'idea-01' not in {r['id'] for r in candidates}
