"""使用适配器提供的空间登录信息上传图片与发布随笔。"""

import json
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen

UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"


class QzoneWeb:
    def __init__(self, account, cookie):
        self.account = account
        self.cookies = dict(part.strip().split("=", 1) for part in cookie.split(";") if "=" in part)
        if not self.cookies.get("p_skey"):
            raise ValueError("空间登录信息缺少 p_skey，请在 QQ 客户端重新登录")
        for key in ("uin", "p_uin"):
            if self.cookies.get(key) and self.cookies[key].removeprefix("o").lstrip("0") != account:
                raise ValueError("空间登录账号与当前 QQ 账号不一致")
        value = 5381
        for char in self.cookies["p_skey"]:
            value = (value * 33 + ord(char)) & 0x7fffffff
        self.token = value

    def request(self, url, fields):
        request = Request(url, data=urlencode(fields).encode(), headers={
            "Cookie": "; ".join(f"{key}={value}" for key, value in self.cookies.items()),
            "Referer": f"https://user.qzone.qq.com/{self.account}",
            "Origin": "https://user.qzone.qq.com",
            "User-Agent": "Mozilla/5.0",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        })
        with urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8")
        # 上传接口使用回调包裹 JSON，发布接口直接返回 JSON。
        return json.loads(text[text.index("{"):text.rindex("}") + 1])

    def prepare(self, content, visibility, image=""):
        fields = {"con": content, "ugc_right": visibility, "hostuin": self.account,
                  "syn_tweet_verson": 1, "paramstr": 1, "who": 1, "feedversion": 1,
                  "ver": 1, "to_sign": 0, "code_version": 1, "format": "json",
                  "qzreferrer": f"https://user.qzone.qq.com/{self.account}"}
        if image:
            result = self.request(UPLOAD_URL, {
                "filename": "maiweave.jpg", "uploadtype": 1, "albumtype": 7, "exttype": 0,
                "uin": self.account, "p_uin": self.account, "zzpaneluin": self.account,
                "skey": self.cookies.get("skey", ""), "p_skey": self.cookies["p_skey"],
                "output_type": "json", "refer": "shuoshuo", "charset": "utf-8", "output_charset": "utf-8",
                "upload_hd": 1, "hd_width": 2048, "hd_height": 10000, "hd_quality": 96,
                "url": UPLOAD_URL + "?" + urlencode({"g_tk": self.token}), "base64": 1, "picfile": image,
            })
            if result["ret"] != 0:
                raise RuntimeError("空间图片上传失败：" + str(result.get("msg") or result.get("message") or result["ret"]))
            data = result["data"]
            fields["pic_bo"] = parse_qs(urlsplit(data["url"]).query)["bo"][0]
            fields["richtype"] = 1
            fields["richval"] = ",".join(str(value) for value in (
                "", data["albumid"], data["lloc"], data["sloc"], data["type"],
                data["height"], data["width"], "", data["height"], data["width"]))
        return fields

    def publish(self, fields):
        return self.request(PUBLISH_URL + "?" + urlencode({"g_tk": self.token, "uin": self.account}), fields)
