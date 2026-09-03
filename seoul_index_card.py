#!/usr/bin/env python3
"""
Card renderer for Seoul Index (@seoul-index.bsky.social).

Renders one post's index as a monospace "markdown on cream" PNG card, matching
the account avatar (cream #f5f0e6, red #d70000). Headless Google Chrome does the
type/emoji/Hangul layout (so color emoji and Korean Just Work via system fonts);
Pillow crops the result to the content.

Design is fixed by seoul_index_post.compose(): a bold header (## + optional
opener emoji + title), then one row per line (optional emoji + label, a red
dotted leader, a bold right-aligned value), then an optional muted footnote for
a caveat on the numbers ("Crowds are KT-estimated"). Hashtags are NEVER on the
card, and Source usually is not either — the poster normally keeps it as real,
clickable text under the image, which a rendered PNG cannot be. ⚠️ The one
exception is compose()'s `credit_on_card` (added 28 Aug 2026, for the boxhist
"then and now" card, which has no dateline of its own): there the credit rides
the dateline slot below as plain red text, unlinked, and the poster drops the
source reply entirely rather than say it twice.

The card is rendered on a magenta sentinel background and cropped to content, so
3-line, 4-line, and wrapped-long-label posts all come out tight with no guessed
height. Corners are square on purpose: Bluesky rounds image corners itself.

Public API:
    render_card(opener, lines, out_path, korean=False, footnote="", dateline="")
        opener:   {"emoji": "🧾" or "", "text": "Spent last quarter in Seoul"}
        lines:    [{"emoji": "☕" or "", "label": "Coffee shops",
                    "value": "₩651.4bn"}, ...]
                  A line may instead be a group subhead — {"subhead": "Right now"}
                  — rendered red over the rows that follow it. A grouped cross-pair
                  card uses two (a date over the monthly group, "Right now" over
                  the live one); a then-and-now card uses one per METRIC, with
                  {"bold": True} on the period rows beneath it; plain cards pass
                  none.
                  {"emph": "Gangnam Station"} bolds that run inside the label,
                  for a card whose rows share their wording and differ in one
                  span of it.
                  {"value_lead": "🌙 Sunset "} prints that text in REGULAR
                  weight immediately before the (still bold) value — for a row
                  that packs a second emoji-led reading into the value slot
                  (sunrise/sunset, a forecast beside an observation) without
                  its own label/word turning bold along with the figure that
                  follows it.
        footnote: "Crowds are KT-estimated" or "" for none
        dateline: masthead period under the title ("December 2025") on a single-
                  frame dated card; "" when grouped (the date rides a subhead)

Raises CardRenderError on any failure so the poster can fall back to plaintext.
"""

import html
import re
import subprocess
import tempfile
from pathlib import Path

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
SENTINEL = 'FF00FF'          # page background; cropped away. Never appears in art.
SENTINEL_RGB = (255, 0, 255)
CARD_WIDTH = 600             # CSS px; device-scale 2 renders at 1200 px.
RENDER_HEIGHT = 1000         # generous CSS height; cropped to content after.
CREAM = '#f5f0e6'
RED = '#d70000'
INK = '#20242c'
BULLET = '#b0a487'
# Footnote ink: warm and quiet, but still 5.35:1 on the cream, so the caveat
# stays legible at 13px. (BULLET is only 2.17:1 — decoration, never text.)
MUTED = '#6b6152'

# Menlo covers latin + digits + the ₩ sign; Apple SD Gothic Neo covers Hangul;
# Apple Color Emoji is picked up automatically for emoji. Same stack both langs
# so the EN and KO siblings share one feel.
FONT_STACK = "Menlo, 'Apple SD Gothic Neo', monospace"


class CardRenderError(RuntimeError):
    """Rendering failed — caller should fall back to a plaintext post."""


def curly(s):
    """Typographer's quotes: straight ' and " become curly. Applied to every
    piece of card text via _esc (and to the alt/fallback bodies by the poster),
    so it holds no matter where the text came from — the selector, a fixed
    opener or the methodology prose."""
    if not s:
        return s or ''
    s = re.sub(r"(?<=\w)'(?=\w)", '’', s)   # contractions: Seoul's
    s = re.sub(r"'(?=\d\d)", '’', s)        # decade elision: '90s
    s = re.sub(r"(?<=\w)'", '’', s)         # trailing possessive: palaces'
    s = re.sub(r"'(?=\S)", '‘', s)          # opening single quote
    s = s.replace("'", '’')                 # anything left closes
    s = re.sub(r'(?<=\S)"(?=[\s.,;:!?)\]]|$)', '”', s)  # closing double
    s = re.sub(r'"(?=\S)', '“', s)          # opening double
    s = s.replace('"', '”')
    return s


def _esc(s):
    return html.escape(curly(s), quote=True)


# A label that opens with a year and a colon gets the year bolded: "2026: The
# Odyssey". The year is what the reader is scanning down a then-and-now card,
# and it is the one part of such a label that is not a name. Done here rather
# than in the label itself because labels are escaped, so markup cannot travel
# in them, and doing it in the renderer keeps the alt text plain.
_YEAR_LEAD = re.compile(r'^((?:19|20)\d\d):')


def _row_html(line):
    emoji = line.get('emoji') or ''
    lead = f'{_esc(emoji)} ' if emoji else ''
    lab = _esc(line['label'])
    m = _YEAR_LEAD.match(lab)
    if m:
        lab = f'<b>{m.group(1)}:</b>{lab[m.end():]}'
    elif line.get('bold'):
        # A row under a metric subhead: the whole label IS the discriminator
        # ("Summer 2026" against "Summer 1976"), so all of it bolds, not just a
        # leading year. Flagged per line rather than inferred from the subhead,
        # because a live+dated cross pair also uses subheads and its labels are
        # ordinary metric labels that must stay regular weight.
        lab = f'<b>{lab}</b>'
    elif line.get('emph'):
        # The same idea one level down: the rows share a metric and differ in a
        # RUN INSIDE the label rather than in the whole of it, so that run bolds
        # and the shared wording stays regular. On a crowd card that run is the
        # place; the rest ("Estimated crowd,") is identical down every row and
        # is what the eye should be able to skip.
        #
        # ⚠️ Matched as a plain substring on the ESCAPED label, and a miss is
        # silently fine: compose() takes the run from the harvester's own data
        # while the label can be the selector's rewrite, so the two need not
        # agree. Nothing bolds in that case and the card is as it was.
        # replace(count=1) so a place that also appears in the shared wording
        # bolds where it varies rather than everywhere it occurs.
        # No "is it present" guard: str.replace on a run that is not there is
        # already a no-op, and a condition with no behavior behind it is a
        # branch no mutation can catch and no test can pin.
        run = _esc(line['emph'])
        if run:
            lab = lab.replace(run, f'<b>{run}</b>', 1)
    val_lead = line.get('value_lead') or ''
    val_lead_html = f'<span class="valreg">{_esc(val_lead)}</span>' if val_lead else ''
    return (
        '<div class="r">'
        f'<span class="lab">{lead}{lab}</span>'
        '<span class="led"></span>'
        f'<span class="val">{val_lead_html}{_esc(line["value"])}</span>'
        '</div>'
    )


def _item_html(item):
    """One card element. An item carrying a 'subhead' key renders as a red group
    subhead — the same red as the masthead dateline, but sitting mid-card over
    the rows that follow it. A cross-pair card uses a date over its monthly group
    and "Right now" over its live group; a then-and-now card uses the METRIC
    ("Nights never below 25°C (77°F)") over its two period rows. Anything else is
    a normal row."""
    if 'subhead' in item:
        return f'<div class="sub">{_esc(item["subhead"])}</div>'
    return _row_html(item)


HANDLE_WATERMARK = '@seoul-index.bsky.social'


def _build_html(opener, lines, footnote='', dateline=''):
    op_emoji = opener.get('emoji') or ''
    op_lead = f'{_esc(op_emoji)} ' if op_emoji else ''
    rows = ''.join(_item_html(l) for l in lines)
    foot = f'<div class="fn">{_esc(footnote)}</div>' if footnote else ''
    # A dateline sits just under the title, above the rows: the period the
    # figures cover, lifted out of the muted footnote so it reads as a masthead
    # date. When present the title tightens up (.hasdl) so the two group. A
    # grouped cross pair carries its date as a .sub group subhead instead, and
    # passes no dateline (see seoul_index_post._card_payload).
    dl = f'<div class="dl">{_esc(dateline)}</div>' if dateline else ''
    h_class = 'h hasdl' if dateline else 'h'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:4px solid {RED};padding:28px 30px;font-family:{FONT_STACK}}}
.h{{font-size:17px;font-weight:700;margin-bottom:22px;line-height:1.35}}
.h.hasdl{{margin-bottom:5px}}
.h .md{{color:{RED}}}
.dl{{font-size:14px;font-weight:700;letter-spacing:.02em;color:{RED};
  margin-bottom:22px;line-height:1.3}}
.sub{{font-size:14px;font-weight:700;letter-spacing:.02em;color:{RED};
  line-height:1;margin:24px 0 0}}
.sub+.r{{margin-top:7px}}
.r{{display:flex;align-items:flex-end;margin:13px 0;font-size:16px}}
.r .lab{{line-height:1.3;min-width:0;overflow-wrap:anywhere}}
.r .led{{flex:1 0 34px;border-bottom:2px dotted {RED};margin:0 9px}}
.r .val{{font-weight:700;line-height:1;white-space:nowrap}}
.r .val .valreg{{font-weight:400}}
.fn{{margin-top:20px;font-size:13px;line-height:1.4;color:{MUTED}}}
.handle{{margin-top:14px;padding-top:12px;
  font-size:12px;letter-spacing:.04em;color:{MUTED}}}
</style></head><body>
<div class="card">
<div class="{h_class}"><span class="md">##</span> {op_lead}{_esc(opener['text'])}</div>
{dl}
{rows}
{foot}
<div class="handle">{_esc(HANDLE_WATERMARK)}</div>
</div></body></html>"""


def _crop_to_content(raw_path, out_path):
    try:
        from PIL import Image, ImageChops
    except ImportError as e:
        raise CardRenderError(f'Pillow not available: {e}')
    with Image.open(raw_path) as im:
        im = im.convert('RGB')
        bg = Image.new('RGB', im.size, SENTINEL_RGB)
        bbox = ImageChops.difference(im, bg).getbbox()
        if not bbox:
            raise CardRenderError('rendered image was entirely background')
        cropped = im.crop(bbox)
        cropped.save(out_path)
        size = cropped.size
    return out_path, size


def _shoot(doc, out_path, retry=True):
    """Render an HTML doc to a content-cropped PNG. Returns (out_path, (w, h)).

    ⚠️ One retry on a Chrome hang, not on a Chrome crash. The 3 September 2026
    weather run lost its whole post to headless Chrome simply never returning
    within the 60s timeout, with nothing wrong in the HTML — indistinguishable
    from data.seoul.go.kr's own transient timeouts elsewhere in this codebase,
    which get the same one-retry treatment. A crash (Chrome exits but leaves no
    PNG) is not retried: that is a real fault in the HTML or Chrome itself, and
    trying again would just mask it."""
    if not Path(CHROME).exists():
        raise CardRenderError(f'Chrome not found at {CHROME}')
    out_path = str(out_path)
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'card.html'
        raw_png = Path(td) / 'raw.png'
        html_path.write_text(doc, encoding='utf-8')
        cmd = [
            CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--force-device-scale-factor=2',
            f'--window-size={CARD_WIDTH},{RENDER_HEIGHT}',
            f'--default-background-color={SENTINEL}FF',
            f'--screenshot={raw_png}', f'file://{html_path}',
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            if retry:
                return _shoot(doc, out_path, retry=False)
            raise CardRenderError('Chrome hung twice in a row (60s each)')
        if not raw_png.exists():
            raise CardRenderError(
                f'Chrome produced no image (exit {r.returncode}): '
                f'{(r.stderr or r.stdout or "").strip()[:200]}')
        _, size = _crop_to_content(raw_png, out_path)
    return out_path, size


def render_card(opener, lines, out_path, korean=False, footnote='', dateline=''):
    """Render one index card (label→leader→value rows, optional footnote).
    An optional dateline sits under the title. Returns (path, (w, h))."""
    if not lines:
        raise CardRenderError('no lines to render')
    return _shoot(_build_html(opener, lines, footnote, dateline), out_path)


# Source domains get bolded wherever they appear in prose body text.
PROSE_BOLD_TERMS = ('data.seoul.go.kr', 'kosis.kr')


def _prose_paragraph(p):
    s = _esc(p)
    for term in PROSE_BOLD_TERMS:
        s = s.replace(term, f'<b>{term}</b>')
    return f'<p>{s}</p>'


def _build_prose_html(heading, paragraphs, emoji=''):
    lead = f'{_esc(emoji)} ' if emoji else ''
    body = ''.join(_prose_paragraph(p) for p in paragraphs)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;background:#{SENTINEL}}}
.card{{width:{CARD_WIDTH}px;box-sizing:border-box;background:{CREAM};color:{INK};
  border-top:4px solid {RED};padding:28px 30px;font-family:{FONT_STACK}}}
.h{{font-size:17px;font-weight:700;margin-bottom:18px;line-height:1.35}}
.h .md{{color:{RED}}}
.body{{font-size:15px;line-height:1.65}}
.body p{{margin:0 0 12px}}
.body p:last-child{{margin-bottom:0}}
</style></head><body>
<div class="card">
<div class="h"><span class="md">##</span> {lead}{_esc(heading)}</div>
<div class="body">{body}</div>
</div></body></html>"""


def render_prose_card(heading, paragraphs, out_path, korean=False, emoji=''):
    """Render one prose card (heading + wrapped body paragraphs) in the same
    cream/red identity as the index cards. `paragraphs` is a list of strings.
    Returns (path, (w, h))."""
    if not paragraphs:
        raise CardRenderError('no body text to render')
    return _shoot(_build_prose_html(heading, paragraphs, emoji), out_path)


if __name__ == '__main__':
    # Manual smoke test: two posts, short and long-label.
    en = render_card(
        {'emoji': '🧾', 'text': 'Spent last quarter in Seoul'},
        [{'emoji': '☕', 'label': 'Coffee shops', 'value': '₩651.4bn'},
         {'emoji': '📚', 'label': 'Bookshops', 'value': '₩77.8bn'},
         {'emoji': '🍗', 'label': 'Fried-chicken shops', 'value': '₩77.7bn'},
         {'emoji': '🐾', 'label': 'Pet shops', 'value': '₩7.9bn'}],
        'card_en.png')
    national = render_card(
        {'emoji': '🇰🇷', 'text': 'Seoul and the nation'},
        [{'emoji': '', 'label': 'People who live in South Korea', 'value': '51,117,378'},
         {'emoji': '', 'label': 'People who live in Seoul', 'value': '9,299,548'},
         {'emoji': '', 'label': 'Share of all South Koreans who live in Seoul', 'value': '18.2%'}],
        'card_national.png')
    print('wrote card_en.png, card_national.png:', en, national)
