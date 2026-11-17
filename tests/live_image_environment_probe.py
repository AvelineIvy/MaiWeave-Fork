"""在实机用当前天气、角色参考图和图片服务核验穿着。"""
import asyncio,json,shutil,sqlite3,tomllib
from pathlib import Path
from types import SimpleNamespace
from contextlib import suppress
from uuid import uuid4
from .live_reading_probe import Probe


class EnvironmentProbe(Probe):
    async def run(self,case):
        from maiweave_probe_target.core.models import ExperienceInput
        reference_image = json.loads(case)["reference_image"] if case != "images" else ""
        native=self.ctx
        folder=Path(native.paths.data_dir)/uuid4().hex
        folder.mkdir()
        class Context:
            paths=SimpleNamespace(data_dir=str(folder))
            def __getattr__(self,name):return getattr(native,name)
        p=self.create()
        cfg=tomllib.loads((Path(__file__).resolve().parent.parent/'ilworkcn_maiweave'/'config.toml').read_text())
        cfg['life'].update(enabled=False,autonomous=False)
        cfg['diary'].update(image_service_enabled=True)
        p.set_plugin_config(cfg);p._set_context(Context())
        self.result.update(checks=[],folder=str(folder))
        try:
            await p.on_load();p.worker.cancel()
            with suppress(asyncio.CancelledError):await p.worker
            production=Path(native.paths.data_dir).parent/'ilworkcn.maiweave'
            with sqlite3.connect(f'file:{production / "maiweave.sqlite3"}?mode=ro',uri=True) as db:
                for category,key in [('image_settings','character'),('world','weather_place')]:
                    raw=db.execute('SELECT data FROM records WHERE category=? AND id=?',(category,key)).fetchone()
                    if raw:
                        row=json.loads(raw[0])
                        if category=='image_settings':p.life_images.images.save_character({k:v for k,v in row.items() if k!='id'})
                        else:
                            with p.store.db:p.store.put(category,row)
            reference=production/'character_reference.jpg'
            if reference.exists():shutil.copyfile(reference,folder/'character_reference.jpg')
            if reference_image:
                from PIL import Image
                with Image.open(reference_image) as picture:
                    picture.thumbnail((768,768))
                    picture.convert('RGB').save(folder/'character_reference.jpg',format='JPEG',quality=85)
            p.config.world.weather_enabled=True
            self.result['stage']='读取真实天气'
            weather=await p.world.weather(p.now())
            self.result['temperature']=weather['current']['temperature_2m']
            p.config.diary.image_character_enabled=True
            p.config.diary.image_character_reference_enabled=(folder/'character_reference.jpg').exists()
            self.result['reference_used']=p.config.diary.image_character_reference_enabled
            images=p.life_images.images
            original=images.request
            prompts=[]
            def request(*args):
                prompts.append(args[0])
                return original(*args)
            images.request=request
            for location,scene,details in [
                ('公园','在公园步道散步时拍一张前置自拍',{}),
                ('厨房','在温暖的厨房举起刚做好的小兔子拍前置自拍',{'穿着':'米白色针织毛衣，居家长裤','环境':'供暖的温暖厨房'})]:
                if reference_image and location=='公园':continue
                self.result['stage']=location+'真实配图'
                now=p.now()
                event=p.engine.record_experience(ExperienceInput(summary=scene,source='simulated',occurred_at=now.isoformat(),evidence='用户授权的穿着配图测试',details={'地点':location,**details}),'test-image-session',now)
                row={'id':event['id'],'status':'草稿','content':p.life_images.content(event),'image_request':scene}
                images.put('experience_image',row)
                row=await images.illustrate('experience_image',event['id'],now)
                assert row['image_status']=='已生成'
                assert '气温℃' in prompts[-1] and location in prompts[-1]
                assert row['image_character_visible'] and row['image_selfie']
                if location=='厨房':assert '米白色' in row['image_prompt'],row['image_prompt']
                self.result['checks'].append({'scene':location,'image':row['image_file'],'prompt':row['image_prompt'],'environment':row['image_environment'],'attempts':row['image_attempts']})
            if reference_image:
                self.result.update(status='已完成',stage='完成',sent_images=0)
                return
            self.result['stage']='空间配图环境'
            day=p.now().date().isoformat()
            row={'id':day,'day':day,'status':'草稿','content':'在供暖的厨房喝茶，穿着米白色针织毛衣。'}
            images.put('diary_publication',row)
            row=await images.illustrate('diary_publication',day,p.now())
            assert row['image_status']=='已生成' and '气温℃' in row['image_environment']
            self.result['checks'].append({'scene':'空间','image':row['image_file'],'prompt':row['image_prompt'],'environment':row['image_environment']})
            assert not p.store.all('image_delivery')
            self.result.update(status='已完成',stage='完成',sent_images=0)
        except Exception as error:
            self.result.update(status='失败',error=str(error))
            self.ctx.logger.exception('环境穿着实测失败')
        finally:await p.on_unload()


def create_plugin():return EnvironmentProbe()
