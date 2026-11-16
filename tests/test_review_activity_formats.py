"""资料模型输出、饥饿与活动候选的联动。"""
import json
import pytest
from pydantic import ValidationError
from test_plugin import plugin
from test_activity_connections import set_time
from maiweave.interfaces.review import Assessment


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
