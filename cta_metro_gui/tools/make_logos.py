"""Draws the payment badges shown on the payment screen (assets/logos/*.png, 300x100).

These are simple look-alike badges drawn with Pillow, NOT the official brand artwork. To use the real logos, save
your own PNG files (about 300x100, transparent or white background) over the files in assets/logos with the same
names: card.png, mada.png, stcpay.png, paypal.png, tabby.png, tamara.png, cash.png. The app shows whatever is there.

Run:  python tools/make_logos.py
"""
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / 'assets' / 'logos'
W, H, S = 300, 100, 3                      # drawn 3x bigger, then shrunk for smooth edges
FONT_DIRS = ['/usr/share/fonts/truetype/dejavu', 'C:/Windows/Fonts', '/System/Library/Fonts/Supplemental']
BOLD = ['DejaVuSans-Bold.ttf', 'arialbd.ttf', 'Arial Bold.ttf']
ITALIC = ['DejaVuSans-BoldOblique.ttf', 'arialbi.ttf', 'Arial Bold Italic.ttf']


def font(names, size):
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.is_file():
                return ImageFont.truetype(str(p), size * S)
    return ImageFont.load_default()


def canvas(bg, border=None):
    im = Image.new('RGBA', (W * S, H * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((2 * S, 2 * S, (W - 2) * S, (H - 2) * S), radius=16 * S, fill=bg,
                        outline=border, width=2 * S if border else 0)
    return im, d


def centered(d, text, f, cx, cy, fill):
    box = d.textbbox((0, 0), text, font=f)
    d.text((cx * S - (box[0] + box[2]) / 2, cy * S - (box[1] + box[3]) / 2), text, font=f, fill=fill)


def save(im, name):
    OUT.mkdir(parents=True, exist_ok=True)
    im.resize((W, H), Image.LANCZOS).save(OUT / f'{name}.png')


def card():
    im, d = canvas('white', '#CBD5E1')
    centered(d, 'VISA', font(ITALIC, 38), 82, 50, '#1A1F71')
    r = 25
    for cx, col in ((196, '#EB001B'), (232, '#F79E1B')):
        d.ellipse(((cx - r) * S, (50 - r) * S, (cx + r) * S, (50 + r) * S), fill=col)
    a = Image.new('L', im.size, 0); b = Image.new('L', im.size, 0)
    ImageDraw.Draw(a).ellipse(((196 - r) * S, (50 - r) * S, (196 + r) * S, (50 + r) * S), fill=255)
    ImageDraw.Draw(b).ellipse(((232 - r) * S, (50 - r) * S, (232 + r) * S, (50 + r) * S), fill=255)
    overlap = ImageChops.multiply(a, b)
    im.paste(Image.new('RGBA', im.size, '#FF5F00'), (0, 0), overlap)
    save(im, 'card')


def mada():
    im, d = canvas('white', '#CBD5E1')
    centered(d, 'mada', font(BOLD, 46), 150, 44, '#259BD6')
    d.rounded_rectangle((92 * S, 70 * S, 208 * S, 78 * S), radius=4 * S, fill='#84BD00')
    save(im, 'mada')


def stcpay():
    im, d = canvas('#4F008C')
    f = font(BOLD, 44)
    wa = d.textlength('stc', font=f) / S
    wb = d.textlength(' pay', font=f) / S
    x = 150 - (wa + wb) / 2
    box = d.textbbox((0, 0), 'stc', font=f)
    y = 50 * S - (box[1] + box[3]) / 2
    d.text((x * S, y), 'stc', font=f, fill='white')
    d.text(((x + wa) * S, y), ' pay', font=f, fill='#FF375E')
    save(im, 'stcpay')


def paypal():
    im, d = canvas('white', '#CBD5E1')
    f = font(ITALIC, 44)
    wa, wb = d.textlength('Pay', font=f) / S, d.textlength('Pal', font=f) / S
    x = 150 - (wa + wb) / 2
    box = d.textbbox((0, 0), 'Pay', font=f)
    y = 50 * S - (box[1] + box[3]) / 2
    d.text((x * S, y), 'Pay', font=f, fill='#003087')
    d.text(((x + wa) * S, y), 'Pal', font=f, fill='#009CDE')
    save(im, 'paypal')


def tabby():
    im, d = canvas('#3CFFD0')
    centered(d, 'tabby', font(BOLD, 46), 150, 50, '#111111')
    save(im, 'tabby')


def tamara():
    im, d = canvas('#FFE4D6')
    centered(d, 'tamara', font(BOLD, 44), 140, 50, '#1B1B1B')
    d.ellipse((246 * S, 40 * S, 262 * S, 56 * S), fill='#FF7A45')
    save(im, 'tamara')


def cash():
    im, d = canvas('#E8F7EE', '#9AD3B0')
    d.rounded_rectangle((26 * S, 28 * S, 100 * S, 72 * S), radius=7 * S, fill='#1F9D55')
    d.ellipse((50 * S, 38 * S, 76 * S, 62 * S), outline='white', width=3 * S)
    for x in (34, 92):
        d.ellipse((x * S - 3 * S, 50 * S - 3 * S, x * S + 3 * S, 50 * S + 3 * S), fill='white')
    centered(d, 'Cash', font(BOLD, 36), 188, 50, '#14532D')
    save(im, 'cash')


if __name__ == '__main__':
    for fn in (card, mada, stcpay, paypal, tabby, tamara, cash):
        fn()
    print('wrote', len(list(OUT.glob('*.png'))), 'logos to', OUT)
