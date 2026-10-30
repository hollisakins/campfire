import sys, numpy as np
from astropy.table import Table
d = sys.argv[1]
sh = Table.read(f'{d}/ember_egs_p1_shutters.ecsv'); ok = np.isfinite(sh['scale'])
def pct(x): return ' / '.join(f'{v:.3f}' for v in np.nanpercentile(x, [16, 50, 84]))
print(f'{d}: {np.sum(sh["clean"])}/{len(sh)} clean')
print('a_s percentiles 16/50/84 over ALL shutters, and rejected fraction:')
for nod in sorted(set(sh['nod'])):
    for ak in (1, 2):
        m = ok & (sh['nod'] == nod) & (np.abs(sh['k']) == ak)
        if m.sum() > 5:
            print(f'  nod {int(nod)} |k|={ak}: n={m.sum():3d}  {pct(sh["scale"][m])}   rejected {1-np.mean(sh["clean"][m]):.2f}   chi2 {np.nanmedian(sh["chi2_red"][m]):.2f}')
e = Table.read(f'{d}/ember_egs_p1_exposures.ecsv')
print('per exposure x detector vs pooled: scale (err), offset/sky')
for r in e:
    print(f"  nod {int(r['nod'])} {r['detector']}: {r['scale']:.4f} ({r['scale_err']:.4f})  {r['offset_rel']:+.4f}   n={r['n_shutters']}")
