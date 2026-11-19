"""Fetch the raw (uncal) file for every row of download_list.ecsv into the
pipeline's raw layout, $CAMPFIRE_ROOT/raw/nircam/<PID>/<filter>/, so the
calibration exposures can go through the same processing as science data.
Resumable; stops before the total would pass LIMIT_GB."""
import os, requests
from concurrent.futures import ThreadPoolExecutor
from astropy.table import Table
D = os.path.dirname(os.path.abspath(__file__))
RAW = '/Users/hollis/campfire-data/raw/nircam'
LIMIT_GB = 55.0
t = Table.read(D + '/download_list.ecsv')
URL = 'https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:JWST/product/'


def fetch(row):
    name = row['root'] + '_uncal.fits'
    d = os.path.join(RAW, str(int(row['program'])), str(row['filter']).lower())
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, name)
    err = ''
    for attempt in range(3):
        try:
            with requests.get(URL + name, stream=True, timeout=120) as r:
                r.raise_for_status()
                size = int(r.headers.get('content-length', 0))
                if os.path.exists(dst) and os.path.getsize(dst) == size:
                    return name, size, 'have'
                with open(dst + '.part', 'wb') as fh:
                    for chunk in r.iter_content(1 << 20):
                        fh.write(chunk)
            if size and os.path.getsize(dst + '.part') != size:
                raise IOError('short read')
            os.replace(dst + '.part', dst)
            return name, size, 'ok'
        except Exception as e:
            err = str(e)[:100]
    return name, 0, 'FAILED ' + err


total = 0
with ThreadPoolExecutor(4) as ex:
    for name, size, status in ex.map(fetch, list(t)):
        total += size
        print(f'{status:8s} {name} {size / 1e6:.0f} MB  (total {total / 1e9:.1f} GB)', flush=True)
        if total / 1e9 > LIMIT_GB:
            print('size limit reached; stopping'); os._exit(1)
print('done', total / 1e9, 'GB')
