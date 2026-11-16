"""实机验证适配器取 Cookie、空间只读请求和图片上传。"""
import asyncio
import base64
import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from .live_reading_probe import Probe


class WebProbe(Probe):
    async def run(self, case):
        from maiweave_probe_target.integrations.qzone_web import QzoneWeb
        try:
            self.result.update(stage="读取适配器登录信息")
            account = await self.ctx.call_capability("api.call", api_name="adapter.napcat.system.get_login_info", version="1", timeout_ms=15000, args={})
            result = await self.ctx.call_capability("api.call", api_name="adapter.napcat.account.get_cookies", version="1", timeout_ms=15000, args={"params":{"domain":"qzone.qq.com"}})
            assert result["status"] == "ok", "适配器取 Cookie 失败"
            web = QzoneWeb(str(account["user_id"]), result["data"]["cookies"])
            self.result.update(cookie_received=True, stage="请求 QQ 空间只读列表")
            def read():
                url="https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6?"+urlencode({"uin":web.account,"g_tk":web.token,"num":1,"pos":0,"format":"json","ftype":0,"sort":0})
                request=Request(url, headers={"Cookie":"; ".join(f"{k}={v}" for k,v in web.cookies.items()),"Referer":f"https://user.qzone.qq.com/{web.account}","User-Agent":"Mozilla/5.0"})
                with urlopen(request,timeout=30) as response:
                    text = response.read().decode("utf-8")
                    self.result.update(read_bytes=len(text), read_type=response.headers.get("Content-Type", ""))
                    return json.loads(text[text.index("{"):text.rindex("}")+1])
            response=await asyncio.to_thread(read)
            self.result.update(read_code=response.get("code"))
            assert response.get("code")==0, "空间只读请求失败"
            self.result.update(stage="上传测试图片，不发布说说")
            import io
            from PIL import Image
            data=io.BytesIO()
            Image.new("RGB",(32,24),(235,220,200)).save(data,format="JPEG")
            fields=await asyncio.to_thread(web.prepare,"空间图片上传测试",64,base64.b64encode(data.getvalue()).decode())
            assert fields.get("pic_bo") and fields.get("richval")
            self.result.update(status="已完成",upload_ok=True,published=False)
        except Exception as error:
            self.result.update(status="失败",error_type=type(error).__name__,error=str(error)[:200])


def create_plugin():
    return WebProbe()
