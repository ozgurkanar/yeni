from __future__ import annotations
from pathlib import Path
import base64, io, tempfile, time

def wait_visual(client,step,stop_event,log):
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError as exc:
        raise RuntimeError('Görsel akış adımları için Pillow gerekli: pip install Pillow') from exc
    template=Image.open(io.BytesIO(base64.b64decode(step.template_png_base64))).convert('RGB')
    deadline=time.monotonic()+max(.1,step.timeout_s); best=0.0
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp)/'screen.png'
        while time.monotonic()<deadline:
            if stop_event.is_set(): raise RuntimeError('Durduruldu')
            client.screenshot(path); current=Image.open(path).convert('RGB')
            rx,ry,rw,rh=map(int,(step.region_x,step.region_y,step.region_w,step.region_h))
            region=current.crop((rx,ry,rx+rw,ry+rh))
            if region.size!=template.size: region=region.resize(template.size,Image.Resampling.BILINEAR)
            mean=sum(ImageStat.Stat(ImageChops.difference(template,region)).mean)/3
            score=max(0,min(1,1-mean/255)); best=max(best,score)
            if score>=step.similarity:
                log(f'Görsel bulundu: {step.name}, {score:.3f}'); return int(step.x),int(step.y)
            time.sleep(max(.25,step.poll_interval))
    raise RuntimeError(f'Görsel bulunamadı: {step.name}; en iyi {best:.3f}')
