"""
trade_image.py - Pillow-based image renderer for the !trade command.
"""

import io
import os
import logging

logger = logging.getLogger("trade_eval.image")

_HERE = os.path.dirname(os.path.abspath(__file__))

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    _PIL_AVAILABLE = False
    logger.info("Pillow not installed")

BG          = (43,  45,  49)
SECTION_BG  = (35,  36,  40)
HEADER_BG   = (28,  29,  32)
SEP_COL     = (63,  65,  71)
TEXT        = (219, 222, 225)
MUTED       = (148, 155, 164)
WHITE       = (255, 255, 255)
RED_TEXT    = (237, 66,  69)
WIN_ROW_BG  = (48,  72,  58)
LOSE_ROW_BG = (70,  44,  46)
ALT_ROW_BG  = (39,  41,  45)



def _load_font(size):
    candidates = [
        "C:/Windows/Fonts/consola.ttf",
        "C:/Windows/Fonts/cour.ttf",
        "C:/Windows/Fonts/lucon.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        "/usr/share/fonts/truetype/ubuntu/UbuntuMono-R.ttf",
        "/System/Library/Fonts/Supplemental/Courier New.ttf",
        "/Library/Fonts/Courier New.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _load_custom_font(filename, size):
    path = os.path.join(_HERE, filename)
    if os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError) as e:
            logger.warning("Could not load custom font %s: %s", path, e)
    return _load_font(size)


# Supersampling scale — defined once here so font cache and _render stay in sync
_RENDER_SCALE = 2

# Fonts cached at module load — loaded once, reused for every render call.
# Guarded by _PIL_AVAILABLE so import failures don't crash the module.
if _PIL_AVAILABLE:
    _FC_TITLE    = _load_custom_font("freeshipping.ttf",               26 * _RENDER_SCALE)
    _FC_SECTION  = _load_font(15 * _RENDER_SCALE)
    _FC_COL_HDR  = _load_font(13 * _RENDER_SCALE)
    _FC_BODY     = _load_font(14 * _RENDER_SCALE)
    _FC_SMALL    = _load_font(12 * _RENDER_SCALE)
    _FC_SIDE_HDR = _load_custom_font("freeshipping.ttf", 20 * _RENDER_SCALE) #"Chicago Athletic Slab Serif 2.ttf" alternate
else:
    _FC_TITLE = _FC_SECTION = _FC_COL_HDR = _FC_BODY = _FC_SMALL = _FC_SIDE_HDR = None


def _tw(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0]


def _th(draw, text, font):
    bb = draw.textbbox((0, 0), text, font=font)
    return bb[3] - bb[1]


def _draw_centered(draw, text, font, x, y, row_h, color, align="left",
                   stroke_width=0, stroke_fill=None):
    bb = draw.textbbox((0, 0), text, font=font)
    glyph_h = bb[3] - bb[1]
    glyph_top_offset = bb[1]
    text_y = y + (row_h - glyph_h) // 2 - glyph_top_offset
    if align == "right":
        text_x = x - _tw(draw, text, font)
    else:
        text_x = x
    draw.text((text_x, text_y), text, fill=color, font=font,
              stroke_width=stroke_width, stroke_fill=stroke_fill)


def _strip_md(text):
    return text.replace("**", "")


def _draw_letterspaced(draw, text, font, cx, y, row_h, color,
                       stroke_width=0, stroke_fill=None, extra_px=0, align="center"):
    """Draw text with extra_px gap between each character.
    align='center': cx is the horizontal centre of the text.
    align='left':   cx is the left edge of the first character.
    Drawing letter-by-letter keeps each glyph's stroke halo isolated so
    adjacent characters' outlines don't bleed into each other's fill."""
    widths = [draw.textbbox((0, 0), ch, font=font) for ch in text]
    char_ws = [bb[2] - bb[0] for bb in widths]
    total_w = sum(char_ws) + extra_px * (len(text) - 1)
    x = cx if align == "left" else cx - total_w // 2
    for i, ch in enumerate(text):
        bb = draw.textbbox((0, 0), ch, font=font)
        glyph_h = bb[3] - bb[1]
        glyph_top = bb[1]
        text_y = y + (row_h - glyph_h) // 2 - glyph_top
        draw.text((x, text_y), ch, fill=color, font=font,
                  stroke_width=stroke_width, stroke_fill=stroke_fill)
        x += char_ws[i] + extra_px


def render(picks_a, picks_b,
           j_a, h_a, f_a, s_a,
           j_b, h_b, f_b, s_b,
           j_winner, h_winner, f_winner, s_winner,
           j_adv, h_adv, f_adv, s_adv,
           close):
    if not _PIL_AVAILABLE:
        return None
    try:
        return _render(picks_a, picks_b,
                       j_a, h_a, f_a, s_a,
                       j_b, h_b, f_b, s_b,
                       j_winner, h_winner, f_winner, s_winner,
                       j_adv, h_adv, f_adv, s_adv,
                       close)
    except Exception:
        logger.warning("Trade image render failed", exc_info=True)
        return None


def _render(picks_a, picks_b,
            j_a, h_a, f_a, s_a,
            j_b, h_b, f_b, s_b,
            j_winner, h_winner, f_winner, s_winner,
            j_adv, h_adv, f_adv, s_adv,
            close):

    # Supersampling scale and fonts — pulled from module-level cache (loaded once on import)
    SCALE      = _RENDER_SCALE
    F_TITLE    = _FC_TITLE
    F_SECTION  = _FC_SECTION
    F_COL_HDR  = _FC_COL_HDR
    F_BODY     = _FC_BODY
    F_SMALL    = _FC_SMALL
    F_SIDE_HDR = _FC_SIDE_HDR

    SIDE_A_COL = (0,   53,  148)   # NFC blue
    SIDE_B_COL = (213, 10,  10)    # AFC red

    IMG_W      = 650  * SCALE
    PAD        = 20   * SCALE
    INNER_L    = PAD  + 6 * SCALE
    TITLE_H    = 46   * SCALE
    SECTION_H  = 30   * SCALE
    COL_HEAD_H = 38   * SCALE
    ROW_H      = 27   * SCALE
    SEP_H      = 2    * SCALE
    ADV_LINE_H = 26   * SCALE
    FOOTER_H   = 28   * SCALE
    GAP        = 20   * SCALE

    CONTENT_W  = IMG_W - 2 * PAD
    PICK_COL_W = 188  * SCALE
    DATA_COL_W = (CONTENT_W - PICK_COL_W) // 4

    col_x = [
        PAD,
        PAD + PICK_COL_W,
        PAD + PICK_COL_W + DATA_COL_W,
        PAD + PICK_COL_W + DATA_COL_W * 2,
        PAD + PICK_COL_W + DATA_COL_W * 3,
    ]
    col_r = [
        col_x[0] + PICK_COL_W - 4 * SCALE,
        col_x[1] + DATA_COL_W - 6 * SCALE,
        col_x[2] + DATA_COL_W - 6 * SCALE,
        col_x[3] + DATA_COL_W - 6 * SCALE,
        IMG_W - PAD - 4 * SCALE,
    ]

    adv_plain   = [_strip_md(j_adv), _strip_md(h_adv), _strip_md(f_adv), _strip_md(s_adv)]
    adv_winners = [j_winner, h_winner, f_winner, s_winner]

    def side_block_h(n):
        return COL_HEAD_H + SEP_H + n * ROW_H + ROW_H

    img_h = (TITLE_H + GAP
             + side_block_h(len(picks_a)) + GAP
             + side_block_h(len(picks_b)) + GAP
             + SECTION_H + len(adv_plain) * ADV_LINE_H + GAP
             + FOOTER_H + PAD)

    img  = Image.new("RGB", (IMG_W, img_h), BG)
    draw = ImageDraw.Draw(img)
    y = 0

    # Title bar - football field
    FIELD_GREEN = (34, 90, 34)
    YARD_LINE_W = 4 * SCALE
    NUM_LINES   = 9

    draw.rectangle([0, 0, IMG_W, TITLE_H], fill=FIELD_GREEN)
    spacing = IMG_W / (NUM_LINES + 1)
    for i in range(1, NUM_LINES + 1):
        lx = round(i * spacing)
        draw.rectangle([lx, 0, lx + YARD_LINE_W - 1, TITLE_H], fill=WHITE)

    title = "NFL Trade Evaluator"
    _draw_centered(draw, title, F_TITLE,
                   (IMG_W - _tw(draw, title, F_TITLE)) // 2,
                   0, TITLE_H, WHITE, stroke_width=2 * SCALE, stroke_fill=(0, 0, 0))
    y = TITLE_H + GAP

    def draw_side(picks, j_tot, h_tot, f_tot, s_tot, side_key):
        nonlocal y

        side_text = "TEAM A SENDS" if side_key == "a" else "TEAM B SENDS"
        side_col  = SIDE_A_COL if side_key == "a" else SIDE_B_COL

        draw.rectangle([0, y, IMG_W, y + COL_HEAD_H], fill=HEADER_BG)
        _draw_letterspaced(draw, side_text, F_SIDE_HDR, INNER_L, y, COL_HEAD_H,
                           WHITE, stroke_width=2 * SCALE, stroke_fill=side_col,
                           extra_px=2 * SCALE, align="left")
        hdrs = ["Johnson", "Hill", "Fitz-Spiel", "Stuart"]
        for i, hdr in enumerate(hdrs):
            _draw_centered(draw, hdr, F_COL_HDR, col_r[i + 1], y, COL_HEAD_H, MUTED, align="right")
        y += COL_HEAD_H

        draw.rectangle([0, y, IMG_W, y + SEP_H], fill=SEP_COL)
        y += SEP_H

        for idx, p in enumerate(picks):
            row_bg = BG if idx % 2 == 0 else ALT_ROW_BG
            draw.rectangle([0, y, IMG_W, y + ROW_H], fill=row_bg)
            future = f" (-{p['years_out']}yr)" if p['years_out'] > 0 else ""
            _draw_centered(draw, f"{p['label']}{future}", F_BODY, INNER_L, y, ROW_H, TEXT)
            vals = [str(p["johnson"]), str(int(p["hill"])), str(p["fitz_spiel"]), f"{p['stuart']:.1f}"]
            for i, val in enumerate(vals):
                _draw_centered(draw, val, F_BODY, col_r[i + 1], y, ROW_H, TEXT, align="right")
            y += ROW_H

        def _tot_style(winner):
            """Return (fill, stroke) for a TOTAL cell.
            Winning side: white text with team-colored outline.
            Tie or losing side: plain white, no stroke."""
            if winner == "tie" or winner != side_key:
                return (WHITE, None)
            team_col = SIDE_A_COL if side_key == "a" else SIDE_B_COL
            return (WHITE, team_col)

        tot_styles = [
            _tot_style(j_winner),
            _tot_style(h_winner),
            _tot_style(f_winner),
            _tot_style(s_winner),
        ]

        draw.rectangle([0, y, IMG_W, y + ROW_H], fill=SECTION_BG)
        _draw_centered(draw, "TOTAL", F_BODY, INNER_L, y, ROW_H, WHITE)
        for i, (val, (fill, stroke)) in enumerate(zip(
            [str(j_tot), str(int(h_tot)), str(f_tot), f"{s_tot:.1f}"],
            tot_styles
        )):
            _draw_centered(draw, val, F_BODY, col_r[i + 1], y, ROW_H, fill,
                           align="right",
                           stroke_width=1 * SCALE if stroke else 0,
                           stroke_fill=stroke)
        y += ROW_H

    draw_side(picks_a, j_a, h_a, f_a, s_a, "a")
    y += GAP
    draw_side(picks_b, j_b, h_b, f_b, s_b, "b")
    y += GAP

    draw.rectangle([0, y, IMG_W, y + SECTION_H], fill=SECTION_BG)
    _draw_centered(draw, "Chart Advantage", F_SECTION, INNER_L, y, SECTION_H, WHITE)
    y += SECTION_H

    for adv_text in adv_plain:
        draw.rectangle([0, y, IMG_W, y + ADV_LINE_H], fill=BG)
        x = INNER_L + 4 * SCALE

        # Split at "Team A" or "Team B" and render each segment inline.
        # Team label always gets white fill + colored outline unless it's a tie
        # (ties produce "Even" with no team label, falling through to plain draw).
        rendered = False
        for side_str, side_col in (("Team A", SIDE_A_COL), ("Team B", SIDE_B_COL)):
            if side_str in adv_text:
                idx    = adv_text.index(side_str)
                before = adv_text[:idx]
                after  = adv_text[idx + len(side_str):]
                for seg, seg_color, seg_stroke in (
                    (before,   TEXT,  None),
                    (side_str, WHITE, side_col),
                    (after,    TEXT,  None),
                ):
                    if not seg:
                        continue
                    bb = draw.textbbox((0, 0), seg, font=F_BODY)
                    ty = y + (ADV_LINE_H - (bb[3] - bb[1])) // 2 - bb[1]
                    draw.text((x, ty), seg, fill=seg_color, font=F_BODY,
                              stroke_width=1 if seg_stroke else 0,
                              stroke_fill=seg_stroke)
                    x += bb[2] - bb[0]
                rendered = True
                break
        if not rendered:
            bb = draw.textbbox((0, 0), adv_text, font=F_BODY)
            ty = y + (ADV_LINE_H - (bb[3] - bb[1])) // 2 - bb[1]
            draw.text((x, ty), adv_text, fill=TEXT, font=F_BODY)

        y += ADV_LINE_H

    y += GAP
    footer = "Future picks: one round penalty per year  (e.g. 2027 R1 = 2026 R2 value)"
    fw = _tw(draw, footer, F_SMALL)
    _draw_centered(draw, footer, F_SMALL, (IMG_W - fw) // 2, y, FOOTER_H, MUTED)

    # Downscale to final output size with LANCZOS for sharp text
    out_w = IMG_W // SCALE
    out_h = img_h // SCALE
    img = img.resize((out_w, out_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf
