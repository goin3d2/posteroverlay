from PIL import Image, ImageDraw

from app.render import renderer, spec as specs
from app.settings import Style

DATA = {"resolution": "res_4k", "hdr": "hdr_dv", "imdb": 7.1, "rt_critic": 88}


def poster(title_at_top: bool):
    img = Image.new("RGB", (1000, 1500), (40, 70, 110))
    d = ImageDraw.Draw(img)
    d.ellipse((300, 600, 700, 1000), fill=(220, 150, 70))
    if title_at_top:
        f = renderer.font(170, True)
        d.text((20, 60), "SISTERS", font=f, fill=(240, 20, 140))
    return img


def badge_box(img_before, img_after):
    from PIL import ImageChops
    return ImageChops.difference(img_before.convert("RGB"), img_after.convert("RGB")).getbbox()


def draw(img, position):
    spec = specs.build(DATA, "movie", Style(badge_position=position))
    return renderer.render(img, spec, lambda k: None)


def test_fixed_positions():
    img = poster(False)
    tl = badge_box(img, draw(img, "TopLeft"))
    tr = badge_box(img, draw(img, "TopRight"))
    ml = badge_box(img, draw(img, "MiddleLeft"))
    br = badge_box(img, draw(img, "BottomRight"))
    assert tl[0] < 60 and tl[1] < 60
    assert tr[2] > 940 and tr[1] == tl[1]
    assert abs((ml[1] + ml[3]) / 2 - 750) < 10
    assert br[3] > 1440 and br[2] > 940
    assert tr[2] - tr[0] == tl[2] - tl[0]


def test_auto_moves_off_a_title_and_stays_when_clear():
    clear = poster(False)
    assert badge_box(clear, draw(clear, "Auto")) == badge_box(clear, draw(clear, "TopLeft"))
    titled = poster(True)
    auto = badge_box(titled, draw(titled, "Auto"))
    title_right = 20 + renderer.font(170, True).getlength("SISTERS")
    assert auto[0] > title_right - 60 or auto[1] > 300, auto     # beside or below the title, not on it
