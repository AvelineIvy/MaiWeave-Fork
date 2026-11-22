"""本地与实机共用的离线作息场景。"""
from datetime import timedelta

CASES = [
    ("整晚睡眠", 94), ("半小时睡眠", 94), ("尚无日程", 94),
    ("临时唤醒", 70), ("已有工作", 79), ("全天候", 5), ("取消当晚睡眠", 5),
]


async def restart_overnight(p, plan_input, case, expected):
    start = p.now().replace(hour=23, minute=0, second=0, microsecond=0)
    morning = start + timedelta(hours=10)
    p.now = lambda: start
    p.config.life.schedule_enabled = case != "全天候"
    p.config.life.autonomous = True
    p.config.life.sleep_hour, p.config.life.wake_hour = 23, 7
    with p.store.db:
        p.store.put("state", {**p.engine.state(), "energy": 35, "updated_at": start.isoformat()})
        if case in {"临时唤醒", "取消当晚睡眠"}:
            key = "awakening" if case == "临时唤醒" else "sleep_skip"
            hours = 3 if case == "临时唤醒" else 8
            p.store.put("runtime", {"id": key, "until": (start + timedelta(hours=hours)).isoformat()})
    if case in {"整晚睡眠", "半小时睡眠", "已有工作"}:
        work = case == "已有工作"
        hours = 8 if case == "整晚睡眠" else 2 if work else 0.5
        p.engine.create_plan(plan_input(title=case, kind="work" if work else "sleep",
            start=start.isoformat(), end=(start + timedelta(hours=hours)).isoformat()), "", start)
    await p.on_unload()
    p.now = lambda: morning
    await p.on_load()
    assert p.engine.state()["energy"] == expected, (case, p.engine.state()["energy"], expected)
    records = p.store.all('experience')
    state = p.engine.state()
    await p.on_unload()
    await p.on_load()
    assert p.engine.state() == state
    assert p.store.all("experience") == records
    return state["energy"]
