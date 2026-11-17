"""使用原生画像和实机模型验证关系与分享、问候、日程的联动。"""
import asyncio,json,tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from .live_reading_probe import Probe

class ReviewProbe(Probe):
    async def run(self,case):
        native=self.ctx;folder=Path(native.paths.data_dir)/uuid4().hex;folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=True,schedule_enabled=False)
        cfg['world'].update(weather_enabled=False,places_enabled=False,explore_enabled=False)
        cfg['memory']['enabled']=True
        p.set_plugin_config(cfg);p._set_context(Context())
        self.result.update(checks=[])
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            database_path=Path(p.store.db.execute("PRAGMA database_list").fetchone()[2]).resolve()
            assert database_path.parent == folder.resolve()
            self.result["test_database"]=str(database_path)
            p.config.life.enabled=True
            from datetime import timedelta
            payload=json.loads(case)
            session=payload["session_id"]
            user=str(await native.person.get_value(person_id=payload["person_id"],field_name="user_id"))
            old=p.anniversaries.save_profile(payload["name"],"coupling-old-chat",user,person_id=payload["person_id"])
            with p.store.db:p.store.put("profile",{**old,"intimacy_mode":"auto","intimacy":0,"relationship":"普通关系"})
            settings=p.config.proactive.model_dump()
            now=p.now()
            if payload.pop("replay",False):
                messages=await native.db.get("Messages",filters={"session_id":session,"is_command":False},order_by="-timestamp",limit=30)
                replay_now=max(p.engine.time(r["timestamp"]) for r in messages if r["user_id"]==user)+timedelta(minutes=1)
                self.result["replay_at"]=replay_now.isoformat()
                for mode,level in (("auto",3),("manual",0),("manual",3)):
                    row=p.anniversaries.save_profile(payload["name"],session,user,person_id=payload["person_id"],profile_id=payload.get("profile_id",""))
                    with p.store.db:
                        p.store.put("profile",{**row,"intimacy_mode":mode,"intimacy":level,"relationship":"恋人" if level==3 else "朋友"})
                        p.store.put("profile",{**old,"intimacy_mode":"auto","intimacy":3,"relationship":"恋人"})
                    payload["profile_id"]=row["id"]
                    for expression in ("自然交流","主动表达"):
                        p.config.proactive.mode=expression
                        for kind in ("早安","晚安"):
                            window={"kind":kind,"until":replay_now+timedelta(minutes=20),"reason":"真实聊天历史回放"}
                            candidate=await p.living.greetings.candidate(session,replay_now,window)
                            assert bool(candidate)==(level>=2),(mode,level,expression,kind)
                            self.result["checks"].append({"mode":mode,"level":level,"expression":expression,"kind":kind,"candidate":bool(candidate)})
                self.result.update(status="已完成",stage="真实历史回放完成")
                return
            for mode,level in (("auto",3),("manual",0),("manual",3),("auto",3)):
                self.result["stage"]=f"{mode}等级{level}"
                args={**payload,"intimacy_mode":mode,"intimacy":level,"relationship":"恋人" if level==3 else "朋友"}
                row=await p.reviews.submit("profile",args)
                if row["status"]=="待确认":row=await p.reviews.decide(row["id"],True)
                assert row["status"]=="已保存",row["status"]
                payload["profile_id"]=row["result"]["id"]
                assert p.living.intimacy(session,user)==level
                assert p.store.get("profile",old["id"])["intimacy"]==3
                recipient=await p.living.share_recipient(session,"")
                assert recipient["有效亲密度"]==level
                kinds={}
                for kind in ("早安","晚安"):
                    window={"kind":kind,"until":now+timedelta(minutes=20),"reason":"关系联动测试"}
                    for expression in ("自然交流","主动表达"):
                        p.config.proactive.mode=expression
                        candidate=await p.living.greetings.candidate(session,now,window)
                        if level<2:assert candidate is None
                        kinds[kind+expression]=bool(candidate)
                decisions=[]
                for expression in ("自然交流","主动表达"):
                    p.config.proactive.mode=expression
                    answer=await p.living.receipts.decide(session,["刚折好一个小兔子书签，耳朵稍微歪着，看起来很可爱，想分享这个小进展。"],now,p.config,recipient)
                    decisions.append({"mode":expression,"send":answer.send,"reason":answer.reason})
                for kind,minimum in (("leisure",1),("study",2),("sleep",3)):
                    try:p.living.check_influence(session,user,[{"autonomous":True,"kind":kind}])
                    except ValueError:assert level<minimum
                    else:assert level>=minimum
                p.config.proactive.mode=settings["mode"]
                assert p.config.proactive.model_dump()==settings
                assert p.living.receipts.sent_times(session,now.tzinfo)==[]
                self.result["checks"].append({"mode":mode,"level":level,"saved":row["result"]["intimacy"],"greetings":kinds,"sharing":decisions,"schedule":"通过","quota":"未改变"})
            self.result.update(status="已完成",stage="已完成")
        except Exception as error:
            self.result.update(status="失败",error=repr(error));self.ctx.logger.exception("关系身份实测失败")
        finally:await p.on_unload()

def create_plugin():return ReviewProbe()
