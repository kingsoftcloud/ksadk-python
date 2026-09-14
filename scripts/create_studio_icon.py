from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter
import math
out=Path('dist/studio-app/.iconset'); out.mkdir(parents=True, exist_ok=True)
for size in (16,32,128,256,512):
    im=Image.new('RGBA',(size,size),(0,0,0,0)); d=ImageDraw.Draw(im)
    # dark glass gradient
    for y in range(size):
        t=y/max(1,size-1); c=(10+int(20*t),22+int(25*t),50+int(55*t),255)
        d.line((0,y,size,y),fill=c)
    d.rounded_rectangle((size*.05,size*.05,size*.95,size*.95),radius=size*.2,outline=(90,220,255,255),width=max(1,size//32))
    # neon K mark
    w=max(2,size//8); x=size*.28
    d.line((x,size*.22,x,size*.78),fill=(255,255,255,255),width=w)
    d.line((x,size*.5,size*.72,size*.22),fill=(40,220,255,255),width=w)
    d.line((x,size*.5,size*.78,size*.78),fill=(160,90,255,255),width=w)
    im=im.filter(ImageFilter.GaussianBlur(0.15*size/16))
    im.save(out/f'icon_{size}x{size}.png')
    if size <= 512: im.resize((size*2,size*2), Image.Resampling.LANCZOS).save(out/f'icon_{size}x{size}@2x.png')
