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
                  {"no_leader": True} drops the dotted leader between label
                  and value for that one row — for a row like sunrise/sunset
                  that already reads as two short readings side by side; a
                  dotted line implies "here's the value for this label" the
                  way every other row means it, which misdescribes a row
                  whose right-hand side is a second, unrelated label+value
                  pair rather than the left-hand label's own figure.
        footnote: "Crowds are KT-estimated" or "" for none
        dateline: masthead period under the title ("December 2025") on a single-
                  frame dated card; "" when grouped (the date rides a subhead)

Raises CardRenderError on any failure so the poster can fall back to plaintext.
"""

import textwrap
import html
import math
import re
import subprocess
import tempfile
from pathlib import Path

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
SENTINEL = 'FF00FF'          # page background; cropped away. Never appears in art.
SENTINEL_RGB = (255, 0, 255)
MAP_CAPTION_WRAP = 100       # characters per caption line on the route map
CARD_WIDTH = 600             # CSS px; device-scale 2 renders at 1200 px.
RENDER_HEIGHT = 1000         # generous CSS height; cropped to content after.
# Escalating per-attempt budgets, not one fixed timeout repeated. A single
# 60s retry (added 3 September 2026) was itself hung twice in a row on 4
# September, losing the whole post again: whatever was starving the machine
# that morning (nas_backup.sh was still stuck materialising iCloud files at
# 05:25, unfinished when the Mac rebooted at 08:47) outlasted two 60s
# attempts back to back. Growing the budget on each retry assumes that if
# the first attempt was starved, the contention is more likely ongoing than
# a one-off blip.
RENDER_TIMEOUTS = (60, 90, 120)
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
    # The emoji+space sits in its own removable span, not glued as plain text
    # into .lab: see the wrap-detection script in _build_html, which strips
    # this span (rather than leaving the emoji to fend for itself) whenever
    # the row would otherwise wrap and orphan it alone on its own line.
    lead = f'<span class="ico">{_esc(emoji)} </span>' if emoji else ''
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
    led_class = 'led plain' if line.get('no_leader') else 'led'
    return (
        '<div class="r">'
        f'<span class="lab">{lead}{lab}</span>'
        f'<span class="{led_class}"></span>'
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
.r .led.plain{{border-bottom:none}}
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
</div>
<script>
// A row's emoji is glued directly before its label with only a space between
// them, so when the label barely doesn't fit (usually because .val is
// white-space:nowrap and has already claimed most of the row), the browser's
// only break opportunity is that space — stranding the emoji alone on its
// own line above the label it belongs to. Real Chrome layout, not a guessed
// character width, is what decides whether that happened, so this runs here,
// synchronously, before the screenshot: any .ico whose row's .lab grew past
// one line loses its icon, which is what the row is measured a second time
// (in the same pass) to allow for.
(function() {{
  document.querySelectorAll('.r').forEach(function(row) {{
    var lab = row.querySelector('.lab');
    var ico = row.querySelector('.ico');
    if (!lab || !ico) return;
    var oneLine = parseFloat(getComputedStyle(lab).lineHeight);
    if (lab.offsetHeight > oneLine * 1.5) ico.remove();
  }});
}})();
</script>
</body></html>"""


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


def _shoot(doc, out_path, attempt=0, size=None):
    """Render an HTML doc to a content-cropped PNG. Returns (out_path, (w, h)).

    `size` overrides the (width, height) CSS window Chrome renders at — the
    card's own (CARD_WIDTH, RENDER_HEIGHT) by default, but the bus-route map
    is a fixed square unrelated to a card's row-driven height.

    ⚠️ Escalating retries on a Chrome hang (RENDER_TIMEOUTS), not on a Chrome
    crash. A crash (Chrome exits but leaves no PNG) is not retried: that is a
    real fault in the HTML or Chrome itself, and trying again would just mask
    it. A hang, by contrast, has repeatedly meant nothing wrong with the HTML
    at all — see RENDER_TIMEOUTS' own comment."""
    if not Path(CHROME).exists():
        raise CardRenderError(f'Chrome not found at {CHROME}')
    out_path = str(out_path)
    win_w, win_h = size or (CARD_WIDTH, RENDER_HEIGHT)
    with tempfile.TemporaryDirectory() as td:
        html_path = Path(td) / 'card.html'
        raw_png = Path(td) / 'raw.png'
        html_path.write_text(doc, encoding='utf-8')
        cmd = [
            CHROME, '--headless=new', '--disable-gpu', '--hide-scrollbars',
            '--force-device-scale-factor=2',
            f'--window-size={win_w},{win_h}',
            f'--default-background-color={SENTINEL}FF',
            f'--screenshot={raw_png}', f'file://{html_path}',
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=RENDER_TIMEOUTS[attempt])
        except subprocess.TimeoutExpired:
            if attempt + 1 < len(RENDER_TIMEOUTS):
                return _shoot(doc, out_path, attempt=attempt + 1, size=size)
            budgets = ', '.join(f'{t}s' for t in RENDER_TIMEOUTS)
            raise CardRenderError(
                f'Chrome hung on all {len(RENDER_TIMEOUTS)} attempts ({budgets})')
        if not raw_png.exists():
            raise CardRenderError(
                f'Chrome produced no image (exit {r.returncode}): '
                f'{(r.stderr or r.stdout or "").strip()[:200]}')
        _, size_out = _crop_to_content(raw_png, out_path)
    return out_path, size_out


def render_card(opener, lines, out_path, korean=False, footnote='', dateline=''):
    """Render one index card (label→leader→value rows, optional footnote).
    An optional dateline sits under the title. Returns (path, (w, h))."""
    if not lines:
        raise CardRenderError('no lines to render')
    return _shoot(_build_html(opener, lines, footnote, dateline), out_path)


MAP_SIZE = 600  # CSS px; device-scale 2 renders at 1200 px, same width as a card.


def render_bus_route_map(routes, seoul_stops, out_path, title='', caption=''):
    """Draw named bus routes over a faint backdrop of every Seoul bus stop —
    the threaded reply for the busroutes card, built entirely from data
    already in hand (no external map, no fabricated URL; see the busroutes
    SELECT_PROMPT rule and the design discussion that produced this).

    `routes`: [(label, colour, [(lon, lat), ...]), ...] — one entry per
    route, already ordered stop-to-stop (transport_facts()' ranking order,
    so the legend reads Busiest/2nd-busiest/Quietest top to bottom).
    `seoul_stops`: [(lon, lat), ...] for every Seoul-prefixed stop, drawn as
    the background silhouette. `title` is the bold red masthead line;
    `caption` is the small muted line under the legend — this account's
    house style requires it read as a measurement, not the route's official
    path (the stops are the ones the day's feed lists for the route, one
    direction, boarded or not; see the caption text the callers build).

    Returns (path, (w, h)), or raises CardRenderError — the caller (main())
    treats a failed map the same way a failed card render is already
    treated: the rest of the thread still posts, just without this reply.
    """
    if not routes or not seoul_stops:
        raise CardRenderError('no route or stop data to draw')
    size = MAP_SIZE
    pad = size * 0.05
    lo0 = min(p[0] for p in seoul_stops)
    lo1 = max(p[0] for p in seoul_stops)
    la0 = min(p[1] for p in seoul_stops)
    la1 = max(p[1] for p in seoul_stops)
    k = math.cos(math.radians((la0 + la1) / 2))
    scale = min((size - 2 * pad) / ((lo1 - lo0) * k), (size - 2 * pad) / (la1 - la0))
    ox = (size - (lo1 - lo0) * k * scale) / 2
    oy = (size - (la1 - la0) * scale) / 2

    def xy(lon, lat):
        return (ox + (lon - lo0) * k * scale, size - oy - (lat - la0) * scale)

    def smooth(pts):
        if len(pts) < 3:
            return 'M' + ' L'.join(f'{x:.1f},{y:.1f}' for x, y in pts)
        d = [f'M{pts[0][0]:.1f},{pts[0][1]:.1f}']
        for i in range(1, len(pts) - 1):
            mx, my = (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2
            d.append(f'Q{pts[i][0]:.1f},{pts[i][1]:.1f} {mx:.1f},{my:.1f}')
        d.append(f'L{pts[-1][0]:.1f},{pts[-1][1]:.1f}')
        return ' '.join(d)

    body = []
    # A blurred layer first, so the dense cloud of dots merges into a soft
    # landmass silhouette (real stop density, not a drawn coastline), then
    # crisp dots on top for texture close up — the same two-pass treatment
    # the design preview settled on before this was ever wired into main().
    soft, crisp = [], []
    for lon, lat in seoul_stops:
        x, y = xy(lon, lat)
        soft.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.05"/>')
        crisp.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="0.5"/>')
    body.append(f'<g fill="{MUTED}" opacity="0.5" filter="url(#soften)">{"".join(soft)}</g>')
    body.append(f'<g fill="{INK}" opacity="0.16">{"".join(crisp)}</g>')

    legend = []
    # The caption wraps at MAP_CAPTION_WRAP characters (Menlo 9px on a
    # 600px canvas holds about a hundred): with his bus-stops sentence on it
    # since 12 September 2026 it runs to ~150 characters, and drawn as one
    # line it was cut at the right edge ("...gray dots that rep"). Each
    # extra line lifts the legend by a line height, as extra legend rows do.
    caption_lines = textwrap.wrap(caption, MAP_CAPTION_WRAP) if caption else []
    # The legend grows upward with its row count so the caption under it
    # stays on the canvas: three rows start at size-84, four at size-103.
    # (A four-route map clipped its caption on 10 September 2026.)
    ly = size - 84 - 19 * (len(routes) - 3) - 12 * max(0, len(caption_lines) - 1)
    legend_top = ly - 20
    for route in routes:
        # (label, colour, path) or, since 12 September 2026, (label, colour,
        # path, served): the served stops are drawn as small dots on the
        # line, cream-ringed so they read on it, and their count is the
        # card footnote's "stops served" rather than the line's own stop
        # count, which the reader was otherwise left to reconcile.
        label, colour, pts = route[:3]
        served = route[3] if len(route) > 3 else []
        line_pts = [xy(lon, lat) for lon, lat in pts]
        if len(line_pts) >= 2:
            body.append(f'<path d="{smooth(line_pts)}" stroke="{colour}" '
                         f'stroke-width="2.5" fill="none" stroke-linecap="round" '
                         f'opacity="0.92"/>')
            for x, y in (line_pts[0], line_pts[-1]):
                body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{colour}"/>')
        for lon, lat in served:
            x, y = xy(lon, lat)
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{colour}" '
                         f'stroke="{CREAM}" stroke-width="0.8" class="served"/>')
        legend.append(f'<rect x="30" y="{ly}" width="14" height="5" rx="2.5" fill="{colour}"/>')
        legend.append(f'<text x="50" y="{ly + 5}" font-family="Menlo,monospace" '
                       f'font-size="13" fill="{INK}">{_esc(label)}</text>')
        ly += 19
    # Extra breathing room below the last legend line before the caption,
    # same fix as the card's own legend/caption spacing.
    caption_y = ly + 12
    legend_bg = (f'<rect x="0" y="{legend_top}" width="{size}" '
                 f'height="{caption_y - legend_top + 10 + 12 * max(0, len(caption_lines) - 1)}" '
                 f'fill="{CREAM}" opacity="0.94"/>')
    title_html = (f'<text x="30" y="25" font-family="Menlo,monospace" font-size="14" '
                  f'font-weight="bold" fill="{RED}">{_esc(title)}</text>' if title else '')
    caption_html = ''.join(
        f'<text x="30" y="{caption_y + 12 * i}" font-family="Menlo,monospace" '
        f'font-size="9" fill="{MUTED}">{_esc(line)}</text>'
        for i, line in enumerate(caption_lines))

    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
           f'<defs><filter id="soften" x="-20%" y="-20%" width="140%" height="140%">'
           f'<feGaussianBlur stdDeviation="2.2"/></filter></defs>'
           f'<rect width="{size}" height="{size}" fill="{CREAM}"/>'
           f'{"".join(body)}{legend_bg}{"".join(legend)}{title_html}{caption_html}'
           f'</svg>')
    doc = f'<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">{svg}</body></html>'
    return _shoot(doc, out_path, size=(size, size))



def render_station_map(stations, seoul_stops, out_path, title='', caption=''):
    """Place named subway stations over the same faint backdrop of every Seoul
    bus stop that render_bus_route_map() draws — the threaded reply for the
    stations card. Added 10 September 2026, his call ("including a map with
    subway card"). Same reasoning as the route map: built from data already
    in hand, no external map, no fabricated URL.

    `stations`: [(label, colour, (lon, lat)), ...] — one entry per station,
    in ranking order (Busiest / 2nd-busiest / Quietest), so the legend reads
    top to bottom the way the card does. `seoul_stops`: [(lon, lat), ...]
    for every Seoul-prefixed bus stop, drawn as the background silhouette —
    the bus-stop cloud rather than the ~330 stations, which are too sparse
    to read as a landmass. Each station is a filled dot with a white ring so
    it reads against the dots beneath it, and its label sits beside it on
    the map as well as in the legend, since three dots with no names on a
    grey field say nothing until the reader's eye drops to the legend.

    Returns (path, (w, h)), or raises CardRenderError — main() treats a
    failed map exactly as it treats a failed route map: the thread above it
    has already posted and stays.
    """
    if not stations or not seoul_stops:
        raise CardRenderError('no station or stop data to draw')
    size = MAP_SIZE
    pad = size * 0.05
    lo0 = min(p[0] for p in seoul_stops)
    lo1 = max(p[0] for p in seoul_stops)
    la0 = min(p[1] for p in seoul_stops)
    la1 = max(p[1] for p in seoul_stops)
    k = math.cos(math.radians((la0 + la1) / 2))
    scale = min((size - 2 * pad) / ((lo1 - lo0) * k), (size - 2 * pad) / (la1 - la0))
    ox = (size - (lo1 - lo0) * k * scale) / 2
    oy = (size - (la1 - la0) * scale) / 2

    def xy(lon, lat):
        return (ox + (lon - lo0) * k * scale, size - oy - (lat - la0) * scale)

    body = []
    soft, crisp = [], []
    for lon, lat in seoul_stops:
        x, y = xy(lon, lat)
        soft.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="1.05"/>')
        crisp.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="0.5"/>')
    body.append(f'<g fill="{MUTED}" opacity="0.5" filter="url(#soften)">{"".join(soft)}</g>')
    body.append(f'<g fill="{INK}" opacity="0.16">{"".join(crisp)}</g>')

    legend = []
    ly = size - 84 - 19 * (len(stations) - 3)   # see render_bus_route_map
    legend_top = ly - 20
    for label, colour, (lon, lat) in stations:
        x, y = xy(lon, lat)
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="{colour}" '
                     f'stroke="{CREAM}" stroke-width="2.5"/>')
        # The map label is the station name alone (after the rank word and
        # colon in the legend label), placed to the right of the dot, or to
        # the left when the dot sits in the eastern fifth so it cannot run
        # off the frame.
        name = label.split(': ', 1)[-1]
        anchor, tx = ('end', x - 11) if x > size * 0.8 else ('start', x + 11)
        body.append(f'<text x="{tx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor}" '
                     f'font-family="Menlo,monospace" font-size="12" font-weight="bold" '
                     f'fill="{colour}" stroke="{CREAM}" stroke-width="3" '
                     f'paint-order="stroke">{_esc(name)}</text>')
        legend.append(f'<circle cx="37" cy="{ly + 2.5}" r="5" fill="{colour}"/>')
        legend.append(f'<text x="50" y="{ly + 5}" font-family="Menlo,monospace" '
                       f'font-size="13" fill="{INK}">{_esc(label)}</text>')
        ly += 19
    caption_y = ly + 12
    legend_bg = (f'<rect x="0" y="{legend_top}" width="{size}" '
                 f'height="{caption_y - legend_top + 10}" fill="{CREAM}" opacity="0.94"/>')
    title_html = (f'<text x="30" y="25" font-family="Menlo,monospace" font-size="14" '
                  f'font-weight="bold" fill="{RED}">{_esc(title)}</text>' if title else '')
    caption_html = (f'<text x="30" y="{caption_y}" font-family="Menlo,monospace" '
                    f'font-size="9" fill="{MUTED}">{_esc(caption)}</text>' if caption else '')
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}">'
           f'<defs><filter id="soften" x="-20%" y="-20%" width="140%" height="140%">'
           f'<feGaussianBlur stdDeviation="2.2"/></filter></defs>'
           f'<rect width="{size}" height="{size}" fill="{CREAM}"/>'
           f'{"".join(body)}{legend_bg}{"".join(legend)}{title_html}{caption_html}'
           f'</svg>')
    doc = f'<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0">{svg}</body></html>'
    return _shoot(doc, out_path, size=(size, size))

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
