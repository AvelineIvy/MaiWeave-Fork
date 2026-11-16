"""凌晨作息、日记收尾与聊天状态的耦合。"""
import json
from datetime import timedelta
import pytest
from test_plugin import plugin
from maiweave.core.models import PlanInput

@pytest.mark.parametrize('hour',[1,2])
async def test_midnight_bedtime_context_and_greeting(plugin,hour):
    p=plugin;p.config.life.sleep_hour=hour;p.config.life.wake_hour=9;p.config.life.autonomous=True
    p.config.proactive.natural_greetings=True
    bedtime=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)+timedelta(days=1)
    for minutes in (120,40,30,20,10):
        now=bedtime-timedelta(minutes=minutes);p.now=lambda:now
        assert p.living.bedtime(now)==bedtime
        data=json.loads(p.context_text('private').split('\n')[-1])
        assert data['sleep_policy']['minutes_until_bed']==minutes
        assert not data['sleep_policy']['quiet']
        window=p.living.greetings.window(now)
        assert bool(window)==(minutes<=20)

@pytest.mark.parametrize('hour',[1,2])
async def test_tired_rest_is_awake_before_late_bedtime(plugin,hour):
    p=plugin;p.config.life.sleep_hour=hour;p.config.life.autonomous=True
    now=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)-timedelta(hours=2)
    p.now=lambda:now
    with p.store.db:
        state=p.engine.state();state.update(energy=20,hunger=20,updated_at=now.isoformat());p.store.put('state',state)
    await p.living.choose_activity(now)
    assert p.engine.current(now)['kind']=='rest'
    assert not p.living.quiet_sleep(now)
    assert not p.living.greetings.window(now)

@pytest.mark.parametrize('hour',[1,2])
async def test_diary_defers_in_wind_down_and_sleep(plugin,hour):
    p=plugin;p.config.life.sleep_hour=hour;p.config.life.wake_hour=9
    bedtime=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)+timedelta(days=1)
    for now in (bedtime-timedelta(minutes=30),bedtime,bedtime+timedelta(minutes=30)):
        await p.diary.arrange(now,p.spatial)
    p.ctx.llm.generate.assert_not_awaited()
    assert p.store.all('plan')==[]

@pytest.mark.parametrize('hour',[1,2])
async def test_diary_finishes_before_wind_down(plugin,hour):
    p=plugin;p.config.life.sleep_hour=hour;p.config.life.wake_hour=9
    bedtime=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)+timedelta(days=1)
    now=bedtime-timedelta(hours=2)
    start=bedtime-timedelta(minutes=50)
    p.ctx.llm.generate.return_value={'success':True,'response':json.dumps({'start':start.isoformat(),'end':(start+timedelta(minutes=15)).isoformat(),'reason':'先完成日记，再洗漱'})}
    await p.diary.arrange(now,p.spatial)
    assert p.engine.time(p.store.all('plan')[0]['end'])<=bedtime-timedelta(minutes=30)

async def test_all_day_mode_keeps_diary_available(plugin):
    p=plugin;p.config.life.schedule_enabled=False
    now=p.now().replace(hour=1,minute=45)
    p.ctx.llm.generate.return_value={'success':True,'response':json.dumps({'start':now.isoformat(),'end':(now+timedelta(minutes=15)).isoformat(),'reason':'当前有空'})}
    await p.diary.arrange(now,p.spatial)
    assert p.store.all('plan')[0]['kind']=='diary'

@pytest.mark.parametrize('hour',[1,2])
async def test_diary_replans_slot_crossing_sleep_preparation(plugin,hour):
    p=plugin;p.config.life.sleep_hour=hour;p.config.life.wake_hour=9
    bedtime=p.now().replace(hour=hour,minute=0,second=0,microsecond=0)+timedelta(days=1)
    now=bedtime-timedelta(hours=2)
    def response(start):
        return {'success':True,'response':json.dumps({'start':start.isoformat(),'end':(start+timedelta(minutes=15)).isoformat(),'reason':'空闲时间'})}
    p.ctx.llm.generate.side_effect=[response(bedtime-timedelta(minutes=35)),response(bedtime-timedelta(minutes=60))]
    row=await p.diary.arrange(now,p.spatial)
    assert p.engine.time(row['end'])==bedtime-timedelta(minutes=45)
    assert p.ctx.llm.generate.await_count==2
