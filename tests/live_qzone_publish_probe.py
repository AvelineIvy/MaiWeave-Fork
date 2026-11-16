"""用现有登录实测网页图文发布、持久化记录和空间权限。"""
import asyncio
import json
import tomllib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw
from .live_reading_probe import Probe


class PublishProbe(Probe):
    async def run(self, case):
        from maiweave_probe_target.core.store import Store
        from maiweave_probe_target.config import MaiWeaveConfig
        from maiweave_probe_target.integrations.qzone import DiaryPublisher
        from maiweave_probe_target.integrations.qzone_web import QzoneWeb
        store = None
        try:
            root = Path(__file__).resolve().parent.parent / "ilworkcn_maiweave"
            config = MaiWeaveConfig.model_validate(tomllib.loads((root / "config.toml").read_text()))
            config.diary.publish_mode = "手动发布"
            config.diary.publish_visibility = "仅自己"
            now = datetime.now(ZoneInfo(config.life.timezone))
            record_id = "web-test-" + now.strftime("%Y%m%d-%H%M%S")
            folder = Path("/MaiMBot/data/plugins/ilworkcn.maiweave")
            native = self.ctx
            class Context:
                paths = SimpleNamespace(data_dir=str(folder))
                def __getattr__(self, name):
                    return getattr(native, name)
            store = Store(folder / "maiweave.sqlite3")
            assert not any(row["status"] in {"生成中", "发布中", "配图中"} for row in store.all("diary_publication")), "请等待现有空间任务结束"
            publisher = DiaryPublisher(SimpleNamespace(store=store), Context(), config)
            connection = await publisher.check_connection()
            account = connection["account"]
            actual_client = connection["client"]
            async def use_web():
                await original_check()
                publisher.connection["client"] = "NapCat"
                return publisher.connection
            original_check = publisher.check_connection
            publisher.check_connection = use_web
            image_path = "diary_images/" + record_id + ".jpg"
            (folder / image_path).parent.mkdir(exist_ok=True)
            image = Image.new("RGB", (640,480), (237,225,201))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((80,80,560,400),radius=30,fill=(184,209,187))
            draw.text((210,220),"MaiWeave Web Publish Test",fill=(35,65,47))
            image.save(folder/image_path)
            text = "【MaiWeave 网页发布测试】验证带图说说和仅自己可见权限。测试时间：" + now.strftime("%Y-%m-%d %H:%M:%S")
            with store.db:
                store.put("diary", {"id":record_id,"day":record_id,"status":"已生成","complete":False,"content":text,"material":{},"test_record":True})
            publisher.save(record_id, text, now)
            row=store.get("diary_publication",record_id)
            row.update(image_file=image_path,image_mime="image/jpeg",image_status="已生成",image_at=now.isoformat(),test_record=True)
            publisher.put(row)
            self.result.update(stage="发布仅自己可见的图文说说", record_id=record_id)
            result=await publisher.publish(record_id,now)
            result["client"] = actual_client + "（网页发布测试）"
            publisher.put(result)
            self.result.update(stage="读取空间实际记录和权限",tid=result["tid"],record_status=result["status"],record_visibility=result["visibility"])
            cookies=await self.ctx.call_capability("api.call",api_name="adapter.napcat.account.get_cookies",version="1",timeout_ms=15000,args={"params":{"domain":"qzone.qq.com"}})
            web=QzoneWeb(account,cookies["data"]["cookies"])
            def read():
                url="https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?"+urlencode({"uin":account,"g_tk":web.token,"num":5,"pos":0,"format":"json","ftype":0,"sort":0})
                req=Request(url,headers={"Cookie":"; ".join(f"{k}={v}" for k,v in web.cookies.items()),"Referer":f"https://user.qzone.qq.com/{account}","User-Agent":"Mozilla/5.0"})
                with urlopen(req,timeout=30) as response:body=response.read().decode()
                return json.loads(body[body.index("{"):body.rindex("}")+1])
            data=await asyncio.to_thread(read)
            assert data["code"]==0, "读取空间列表失败"
            item=next(row for row in data.get("msglist",[]) if row["tid"]==result["tid"])
            self.result.update(remote_keys=list(item),remote_right={key:value for key,value in item.items() if "right" in key or "priv" in key},image_count=len(item.get("pic",[])),content_matches=item["content"]==text)
            assert self.result["image_count"]==1 and self.result["content_matches"]
            assert item["ugc_right"] == 64, "空间实际可见范围不是仅自己"
            store.close()
            store=Store(folder/"maiweave.sqlite3")
            saved=store.get("diary_publication",record_id)
            assert saved["tid"]==result["tid"] and saved["status"]=="已发布" and saved["visibility"]=="仅自己"
            self.result.update(status="已完成",record_persisted=True,account=account)
        except Exception as error:
            self.result.update(status="失败",error_type=type(error).__name__,error=str(error)[:250])
        finally:
            if store:store.close()


def create_plugin():
    return PublishProbe()
