import json
from pathlib import Path
from PIL import Image, ImageDraw

work = Path(__file__).parent
media = Path('C:/Users/Lenovo/AppData/Roaming/Anki2/账户 1/collection.media')
headings = json.loads((work / 'heading-candidates.json').read_text(encoding='utf8'))
headings = [h for h in headings if 39 <= h['h'] <= 43 and len(h['text']) < 55]
for batch in range(0, len(headings), 24):
    current = headings[batch:batch+24]
    sheet = Image.new('RGB', (1370, 80*len(current)), 'white')
    draw = ImageDraw.Draw(sheet)
    for index, h in enumerate(current):
        im = Image.open(media / f'图{h["page"]}_第{h["page"]}页_UNIX环境高级编程（第三版）.jpg')
        strip = im.crop((h['x']-5, h['y']-10, h['x']+1245, h['y']+65))
        sheet.paste(strip, (100,index*80))
        draw.text((10,index*80+20), str(h['page']), fill='black',font_size=28)
    sheet.save(work / f'headings-{batch//24+1}.png')
