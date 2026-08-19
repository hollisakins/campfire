import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

C_STOCK = '#d8dee6'   # stock jwst (possibly retuned)
C_CF    = '#f5c46b'   # CAMPFIRE custom
C_EXT   = '#aecde8'   # third-party model/package + CAMPFIRE fitting
C_DATA  = '#f2f2ee'   # data products
EDGE    = '#4a4a4a'

fig, ax = plt.subplots(figsize=(12.5, 10.5))
ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis('off')

def box(x, y, w, h, text, sub, color, dashed=False, data=False):
    style = 'round,pad=0.004,rounding_size=0.012' if not data else 'round,pad=0.004,rounding_size=0.002'
    p = FancyBboxPatch((x, y), w, h, boxstyle=style,
                       fc=color, ec=EDGE, lw=1.0,
                       ls=(0, (4, 2.5)) if dashed else '-')
    ax.add_patch(p)
    cy = y + h/2
    if sub:
        ax.text(x + w/2, cy + 0.011, text, ha='center', va='center',
                fontsize=10.5, weight='bold' if not data else 'normal',
                style='italic' if data else 'normal')
        ax.text(x + w/2, cy - 0.0125, sub, ha='center', va='center',
                fontsize=8.3, color='#333333')
    else:
        ax.text(x + w/2, cy, text, ha='center', va='center', fontsize=10.5,
                weight='bold' if not data else 'normal',
                style='italic' if data else 'normal')

def arrow(x, y0, y1):
    ax.add_patch(FancyArrowPatch((x, y0), (x, y1), arrowstyle='-|>',
                                 mutation_scale=13, color=EDGE, lw=1.1,
                                 shrinkA=0, shrinkB=0))

# ---- left column: per-exposure processing ----
LX, LW = 0.055, 0.36
left = [
    ('raw exposure (_uncal) from MAST', None, C_DATA, False, True),
    ('Detector processing', 'ramp fitting, aggressive snowball flagging — stock, retuned', C_STOCK, False, False),
    ('Jackknife correction', 'removes ramp-fit zero-point bias ("snowball circles")', C_CF, False, False),
    ('Persistence flagging', 'snowblind — charge persistence from earlier exposures', C_EXT, False, False),
    ('Wisp subtraction', 'Wu et al. NMF model + CAMPFIRE fitting (4 SW detectors)', C_EXT, False, False),
    ('Flat field & flux calibration', 'WCS, flat, photometric calibration to MJy/sr — stock', C_STOCK, False, False),
    ('Edge flagging', 'masks noisy detector border rows/columns', C_CF, False, False),
    ('Background & 1/f subtraction', 'sky + amp pedestals + GP 1/f, one shared source mask', C_CF, False, False),
    ('Diagonal striping removal', 'angled scattered-light stripes — opt-in per field', C_CF, True, False),
    ('SNR previews', 'quick-look renders for visual inspection', C_CF, False, False),
    ('Astrometric alignment', 'per-exposure WCS vs. Gaia-tied reference catalog', C_CF, False, False),
    ('calibrated, aligned exposure', None, C_DATA, False, True),
]
n = len(left)
top, bot = 0.955, 0.03
bh = 0.052
gap = (top - bot - n*bh) / (n - 1)
ys = [top - (i+1)*bh - i*gap for i in range(n)]
for (t, s, c, d, data), y in zip(left, ys):
    box(LX, y, LW, bh, t, s, c, d, data)
for i in range(n-1):
    arrow(LX + LW/2, ys[i], ys[i+1] + bh)

# ---- right column: mosaic combination ----
RX, RW = 0.585, 0.36
right = [
    ('all exposures of a filter', None, C_DATA, False, True),
    ('Manual masks', 'reviewer-drawn artifact regions, applied non-destructively', C_CF, False, False),
    ('Bad-pixel rejection', 'ensemble consistently-bad pixels — opt-in', C_CF, True, False),
    ('Outlier detection', 'CR rejection across dithers — stock, per visit\n+ opt-in artifact-region growth', C_STOCK, False, False),
    ('Drizzle to mosaic tiles', '30 mas, inverse-variance weighting — stock', C_STOCK, False, False),
    ('Mosaic background subtraction', 'tiered masking, depth-aware, non-negativity guard', C_CF, False, False),
    ('mosaic tiles (_i2d) per field / filter / tile', None, C_DATA, False, True),
]
m = len(right)
rtop = 0.72
rbh = 0.058
rgap = (rtop - bot - m*rbh) / (m - 1)
rys = [rtop - (i+1)*rbh - i*rgap for i in range(m)]
for (t, s, c, d, data), y in zip(right, rys):
    box(RX, y, RW, rbh, t, s, c, d, data)
for i in range(m-1):
    arrow(RX + RW/2, rys[i], rys[i+1] + rbh)

# connector: left bottom -> right top
ax.add_patch(FancyArrowPatch((LX + LW + 0.005, ys[-1] + bh/2),
                             (RX - 0.007, rys[0] + rbh/2),
                             connectionstyle='arc3,rad=0.18',
                             arrowstyle='-|>', mutation_scale=13,
                             color=EDGE, lw=1.1))

# phase headers
ax.text(LX + LW/2, 0.985, 'Per-exposure processing', ha='center', fontsize=13, weight='bold')
ax.text(RX + RW/2, 0.75, 'Mosaic combination', ha='center', fontsize=13, weight='bold')

# legend
lx, ly = 0.585, 0.955
items = [(C_STOCK, 'stock JWST pipeline (retuned)'),
         (C_CF, 'CAMPFIRE custom step'),
         (C_EXT, 'external model + CAMPFIRE fitting')]
for i, (c, lab) in enumerate(items):
    y = ly - i*0.034
    ax.add_patch(FancyBboxPatch((lx, y - 0.011), 0.030, 0.022,
                 boxstyle='round,pad=0.003,rounding_size=0.006', fc=c, ec=EDGE, lw=0.9))
    ax.text(lx + 0.042, y, lab, va='center', fontsize=9.5)
y = ly - 3*0.034
ax.add_patch(FancyBboxPatch((lx, y - 0.011), 0.030, 0.022,
             boxstyle='round,pad=0.003,rounding_size=0.006', fc='white', ec=EDGE, lw=0.9,
             ls=(0, (4, 2.5))))
ax.text(lx + 0.042, y, 'opt-in (enabled per field)', va='center', fontsize=9.5)

fig.savefig('nircam_flow.png', dpi=200, bbox_inches='tight', facecolor='white')
print('saved')
