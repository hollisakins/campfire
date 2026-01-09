
from astropy.table import Table, vstack
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS, FITSFixedWarning
from astropy.nddata import Cutout2D
from astropy.nddata.utils import NoOverlapError
import astropy.units as u
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import toml, glob, os
import argparse
from scipy.optimize import curve_fit
import warnings
warnings.simplefilter('ignore')

# Fit a 2D polynomial to the offsets
def poly2d(coords, a0, a1, a2, b0, b1, c0):
    """2nd order polynomial: a0 + a1*x + a2*y + b0*x^2 + b1*y^2 + c0*x*y"""
    ra, dec = coords
    return a0 + a1*ra + a2*dec + b0*ra**2 + b1*dec**2 + c0*ra*dec

def get_tile(coords):
    """
    Get the tile for a given coordinate. Finds the closest match 
    to the center coordinates of the tiles. Returns None for 
    coordinates outside the COSMOS-Web footprint. 

    Parameters
    ----------

    coords : astropy.coordinates.SkyCoord or np.ndarray(N,2)
        Coordinates of input sources. Can be specified as either 
        a single SkyCoord object, a list of SkyCoord objects, or a 
        numpy array with 2 columns giving RA and Dec. 
    """
    
    single = False
    if type(coords)==np.ndarray:
        coords = SkyCoord(ra=coords[:,0], dec=coords[:,1], unit='deg')
    elif type(coords)==list:
        coords = SkyCoord(coords)
    elif type(coords)==SkyCoord:
        coords = SkyCoord([coords])
        single = True
        

    tiles = np.array(['A1','A2','A3','A4','A5','A6','A7','A8','A9','A10','B1','B2','B3','B4','B5','B6','B7','B8','B9','B10'])
    ra = np.array([149.83060849224526,149.96618456679428,150.10175329130632,150.23731326578584,150.37286289023743,149.7647850185184,149.9003454431693,150.03589926778474,150.17144509237895,150.30698131694916,149.96220693940455,150.09747486414722,150.23328673819674,150.36919886197347,150.50453028662352,149.89657956583684,150.03225868964083,150.16766126474636,150.30327548908838,150.4387048134958])
    dec = np.array([2.2105308808257207,2.1612435187006245,2.111943456573772,2.062631644446241,2.013309032319107,2.0296854374792526,1.9804026753817368,1.9311082632823562,1.8818031511845486,1.8324881890870612,2.5720372673973584,2.5230015052794137,2.473525843055812,2.423885930793452,2.374833668674315,2.3912872741259417,2.341569861870867,2.292723449827574,2.2433903376662596,2.1941086255207374])
    coords_cen = SkyCoord(ra, dec, unit='deg')
    idx, d2d, d3d = coords.match_to_catalog_sky(coords_cen)
    t = list(tiles[idx])
    if single:
        return t[0]
    return t


def make_nircam_rgb_cutout(field, tile, coord, cutout_width=3*u.arcsec):
    match field:
        case 'cosmos':
            image_data = COSMOS_IMAGE_DATA
        case 'uds':
            image_data = UDS_IMAGE_DATA
        case 'egs':
            image_data = EGS_IMAGE_DATA
        case _:
            raise NotImplementedError

    cutouts = {}
    for band in ['f115w','f150w','f182m','f200w','f210m','f277w','f356w','f444w']:
        
        try:
            filepath = image_data[band]
        except KeyError:
            continue

        if '{tile}' in filepath:
            filepath = filepath.replace('{tile}', tile)
        if '{ext}' in filepath:
            filepath = filepath.replace('{ext}', 'sci')

        try:
            with fits.open(filepath) as hdul:
                sci = hdul[0].data
                wcs = WCS(hdul[0].header)
                cutout = Cutout2D(sci.data, coord, size=cutout_width*3, wcs=wcs)
            del sci
            if not np.all(np.isnan(cutout.data)) or np.sum(cutout.data) == 0: 
                cutouts[band] = cutout
        except (FileNotFoundError,NoOverlapError):
            pass

    from photutils.segmentation import make_2dgaussian_kernel
    from astropy.convolution import convolve
    
    # now decide what to do based on the cutouts available! 
    if np.all(np.isin(['f115w','f150w','f200w','f277w','f356w','f444w'], list(cutouts.keys()))):
        rgb_dict = {}
        rgb_dict['f115w'] = {'colors':np.array([0.0, 0.0, 1.0]), 'data':convolve(cutouts['f115w'].data, make_2dgaussian_kernel(2.0, size=9))}
        rgb_dict['f150w'] = {'colors':np.array([0.0, 0.2, 0.8]), 'data':convolve(cutouts['f150w'].data, make_2dgaussian_kernel(1.5, size=9))}
        rgb_dict['f200w'] = {'colors':np.array([0.0, 0.9, 0.1]), 'data':convolve(cutouts['f200w'].data, make_2dgaussian_kernel(1.0, size=9))}
        rgb_dict['f277w'] = {'colors':np.array([0.1, 0.9, 0.0]), 'data':cutouts['f277w'].data}
        rgb_dict['f356w'] = {'colors':np.array([8.0, 0.2, 0.0]), 'data':cutouts['f356w'].data}
        rgb_dict['f444w'] = {'colors':np.array([1.0, 0.0, 0.0]), 'data':cutouts['f444w'].data}

    elif np.all(np.isin(['f115w','f150w','f182m','f210m','f277w','f356w','f444w'], list(cutouts.keys()))):
        rgb_dict = {}
        rgb_dict['f115w'] = {'colors':np.array([0.0, 0.0, 1.0]), 'data':cutouts['f115w'].data}
        rgb_dict['f150w'] = {'colors':np.array([0.0, 0.2, 0.8]), 'data':cutouts['f150w'].data}
        rgb_dict['f200w'] = {'colors':np.array([0.0, 0.9, 0.1]), 'data':np.nanmean([cutouts['f182m'].data, cutouts['f210m'].data],axis=0)}
        rgb_dict['f277w'] = {'colors':np.array([0.1, 0.9, 0.0]), 'data':cutouts['f277w'].data}
        rgb_dict['f356w'] = {'colors':np.array([8.0, 0.2, 0.0]), 'data':cutouts['f356w'].data}
        rgb_dict['f444w'] = {'colors':np.array([1.0, 0.0, 0.0]), 'data':cutouts['f444w'].data}
    
    elif np.all(np.isin(['f115w','f150w','f277w','f444w'], list(cutouts.keys()))):
        rgb_dict = {}
        rgb_dict['f115w'] = {'colors':np.array([0.0, 0.0, 1.0]), 'data':cutouts['f115w'].data}
        rgb_dict['f150w'] = {'colors':np.array([0.0, 0.3, 0.8]), 'data':cutouts['f150w'].data}
        rgb_dict['f277w'] = {'colors':np.array([0.1, 0.9, 0.0]), 'data':cutouts['f277w'].data}
        rgb_dict['f444w'] = {'colors':np.array([1.0, 0.0, 0.0]), 'data':cutouts['f444w'].data}
    
    elif np.all(np.isin(['f115w','f150w','f200w','f444w'], list(cutouts.keys()))):
        rgb_dict = {}
        rgb_dict['f115w'] = {'colors':np.array([0.0, 0.0, 1.0]), 'data':cutouts['f115w'].data}
        rgb_dict['f150w'] = {'colors':np.array([0.0, 0.3, 0.8]), 'data':cutouts['f150w'].data}
        rgb_dict['f200w'] = {'colors':np.array([0.1, 0.9, 0.0]), 'data':cutouts['f200w'].data}
        rgb_dict['f444w'] = {'colors':np.array([1.0, 0.0, 0.0]), 'data':cutouts['f444w'].data}
    
    else:
        print(cutouts.keys())
        raise RuntimeError("Couldn't find necessary cutouts to make RGB!")

    from htools.utils.imaging import gen_rgb_image
    imrgb = gen_rgb_image(rgb_dict, noisesig=2.0, noiselum=0.12, satpercent=0.01)
    wcs = cutouts['f444w'].wcs
    ps = wcs.proj_plane_pixel_scales()[0].to(u.arcsec).value
    size = np.shape(cutout.data)[0]
    extent = [-size*ps/2, size*ps/2, -size*ps/2, size*ps/2]
    return imrgb, extent


COSMOS_IMAGE_DATA = {
        'f115w': '/V/maurice/mosaics/cosmos/f115w/mosaic_nircam_f115w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f150w': '/V/maurice/mosaics/cosmos/f150w/mosaic_nircam_f150w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f200w': '/V/maurice/mosaics/cosmos/f200w/mosaic_nircam_f200w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f182m': '/V/maurice/mosaics/cosmos/f182m/mosaic_nircam_f182m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f210m': '/V/maurice/mosaics/cosmos/f210m/mosaic_nircam_f210m_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f277w': '/V/maurice/mosaics/cosmos/f277w/mosaic_nircam_f277w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f356w': '/V/maurice/mosaics/cosmos/f356w/mosaic_nircam_f356w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
        'f444w': '/V/maurice/mosaics/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_v0p7_{tile}_{ext}.fits',
    }

UDS_IMAGE_DATA = {
        'f115w': '/V/maurice/mosaics/uds/f115w/mosaic_nircam_f115w_uds_30mas_v0p5_primer_{ext}.fits',
        'f150w': '/V/maurice/mosaics/uds/f150w/mosaic_nircam_f150w_uds_30mas_v0p5_primer_{ext}.fits',
        'f277w': '/V/maurice/mosaics/uds/f277w/mosaic_nircam_f277w_uds_30mas_v0p5_primer_{ext}.fits',
        'f182m': '/V/maurice/mosaics/uds/f182m/mosaic_nircam_f182m_uds_30mas_v0p5_primer_{ext}.fits',
        'f200w': '/V/maurice/mosaics/uds/f200w/mosaic_nircam_f200w_uds_30mas_v0p5_primer_{ext}.fits',
        'f210m': '/V/maurice/mosaics/uds/f210m/mosaic_nircam_f210m_uds_30mas_v0p5_primer_{ext}.fits',
        'f356w': '/V/maurice/mosaics/uds/f356w/mosaic_nircam_f356w_uds_30mas_v0p5_primer_{ext}.fits',
        'f444w': '/V/maurice/mosaics/uds/f444w/mosaic_nircam_f444w_uds_30mas_v0p5_primer_{ext}.fits',
    }

EGS_IMAGE_DATA = {
        'f115w': '/V/maurice/mosaics/egs/ceers_nrc_f115w_{ext}.fits',
        'f150w': '/V/maurice/mosaics/egs/ceers_nrc_f150w_{ext}.fits',
        'f200w': '/V/maurice/mosaics/egs/ceers_nrc_f200w_{ext}.fits',
        'f277w': '/V/maurice/mosaics/egs/ceers_nrc_f277w_{ext}.fits',
        'f356w': '/V/maurice/mosaics/egs/ceers_nrc_f356w_{ext}.fits',
        'f444w': '/V/maurice/mosaics/egs/ceers_nrc_f444w_{ext}.fits',
    }

def get_source_pos(spec_file):
    exp = Table.read(spec_file, hdu=7)
    return np.median(exp['source_ra'][exp['source_ra']!=0]), np.median(exp['source_dec'][exp['source_dec']!=0])

def get_exposure_table(spec_file):
    exp = Table.read(spec_file, hdu=7)
    if exp['nod_type'][0] != '3-SHUTTER-SLITLET':
        raise NotImplementedError

    exp = Table.read(spec_file, hdu=7)
    roots = ['_'.join(f.split('_')[:2]) for f in exp['filename']]
    nrs1_files = [f for f in exp['filename'] if 'nrs1' in f]
    nrs2_files = [f for f in exp['filename'] if 'nrs2' in f]
    if len(nrs1_files) == len(exp) or len(nrs2_files) == len(exp):
        # all NRS1 or all NRS2
        pass
    else:
        # assert len(nrs1_files)==len(nrs2_files)
        # assert len(nrs1_files)==len(exp)/2
        nrs2 = np.array(['nrs2' in f for f in exp['filename']],dtype=bool)
        exp = exp[nrs2]

    # exp = exp[exp['shutter_state']=='1x1']
    return exp

def plot_single_object(spec_files, field, output, cutout_width=3*u.arcsec, override_pos=None):
    
    exp = get_exposure_table(spec_files[0])
    if len(spec_files)>1:
        for spec_file in spec_files[1:]:
            exp = vstack([exp, get_exposure_table(spec_file)])
    # print(exp)
        
    if override_pos is not None:
        source_ra = override_pos[0]
        source_dec = override_pos[1]
    else:
        source_ra, source_dec = get_source_pos(spec_files[0])
    source_c = SkyCoord(source_ra, source_dec, unit='deg')
    if field=='cosmos':
        tile = get_tile(source_c)
    else:
        tile = ''
    
    imrgb, extent = make_nircam_rgb_cutout(field, tile, source_c, cutout_width=cutout_width)
    if imrgb is None:
        raise ValueError

    fig, ax = plt.subplots(figsize=(4,4))

    ax.imshow(imrgb, origin='lower', extent=extent)

    for t in exp:
        pa = (t['v3pa']-360+138.5)*u.deg
        if field=='cosmos':
            pa -= 20*u.deg
        elif field=='egs':
            pa -= 130.3*u.deg
            # pa = 45*u.deg

        if ~np.isfinite(t['source_xpos']) or t['source_xpos']==0:
            dx = np.nanmedian(exp['source_xpos'])*0.27*u.arcsec
        else:
            dx = t['source_xpos']*0.27*u.arcsec
        if ~np.isfinite(t['source_ypos']) or t['source_ypos']==0:
            dy = np.nanmedian(exp['source_ypos'])*0.53*u.arcsec
        else:
            dy = t['source_ypos']*0.53*u.arcsec

        # print(source_c.dec.value)
        # print(np.cos(np.radians(source_c.dec.value)))
        # dy *= np.cos(np.radians(source_c.dec.value))
        # dx *= np.cos(np.radians(source_c.dec.value))

        shutter_c = source_c.directional_offset_by(-pa, dy).directional_offset_by(90*u.deg-pa, dx)
        # separation = shutter_c.separation(source_c) * np.cos(np.radians(source_c.dec.value))
        # position_angle = shutter_c.position_angle(source_c)
        # shutter_c = source_c.directional_offset_by(position_angle, separation)
        match t['shutter_state']:
            case '1x1':
                pass
            case '11x':
                shutter_c = shutter_c.directional_offset_by(-pa, 0.53*u.arcsec)
            case 'x11':
                shutter_c = shutter_c.directional_offset_by(-pa, -0.53*u.arcsec)

        match len(exp):
            case 1: 
                alpha = 1.0
            case 2: 
                alpha = 0.8
            case 3: 
                alpha = 0.6
            case _:
                alpha = 0.5

        for i in [-1,0,1]:
            c = shutter_c.directional_offset_by(-pa, i*0.53*u.arcsec)
            p = mpl.patches.Rectangle(
                ((c.ra.value-source_c.ra.value-0.22/3600/2)*3600*np.cos(np.radians(source_c.dec.value)), (c.dec.value-source_c.dec.value-0.46/3600/2)*3600), 
                width=0.22, height=0.46,
                facecolor='none', 
                edgecolor='lime',
                angle=pa.to('deg').value,
                rotation_point='center',
                zorder=10000,
                alpha=alpha,
                linewidth=1.3,
            )
            ax.add_patch(p)

    ax.set_xlim(-1.5, 1.5)
    ax.set_ylim(-1.5, 1.5)

    # Remove axes
    ax.axis('off')

    # Remove all whitespace/padding
    plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

    plt.savefig(output, dpi=200)

    plt.close()

def fit_offsets(msa_coords, ref_coords, method='poly2d',smoothing=1.0, plot=True):

    # Match MSA catalog to reference catalog (derived from whatever images we're overplotting slits on)
    idx, d2d, d3d = msa_coords.match_to_catalog_sky(ref_coords)
    xmatch = d2d < 0.3*u.arcsec
    dra = (ref_coords.ra[idx[xmatch]] - msa_coords.ra[xmatch]).to('arcsec').value
    ddec = (ref_coords.dec[idx[xmatch]] - msa_coords.dec[xmatch]).to('arcsec').value
    ra,dec = msa_coords[xmatch].ra.value,  msa_coords[xmatch].dec.value

    if method=='poly2d':
        params_ra, _ = curve_fit(poly2d, (ra, dec), dra)
        params_dec, _ = curve_fit(poly2d, (ra, dec), ddec)
        dra_fit = poly2d((ra,dec),*params_ra)
        ddec_fit = poly2d((ra,dec),*params_dec)
        lra = lambda r,d: poly2d((r,d),*params_ra)
        ldec = lambda r,d: poly2d((r,d),*params_dec)

    elif method=='rbf':
        from scipy.interpolate import RBFInterpolator
        interp_ra = RBFInterpolator(
            np.column_stack([ra, dec]),
            dra,
            smoothing=smoothing,  # Higher = smoother, tune to your data
            kernel='thin_plate_spline'
        )
        interp_dec = RBFInterpolator(
            np.column_stack([ra, dec]),
            ddec,
            smoothing=smoothing,
            kernel='thin_plate_spline'
        )
        dra_fit = interp_ra(np.column_stack([ra,dec]))
        ddec_fit = interp_dec(np.column_stack([ra,dec]))
        lra = lambda r,d: interp_ra(np.column_stack([r,d]))
        ldec = lambda r,d: interp_dec(np.column_stack([r,d]))


    if plot:    

        # Important: use same scale and scale_units for all
        scale = np.nanmax(np.sqrt(dra**2 + ddec**2)) /20 # Adjust multiplier to taste
        scale_units = 'xy'

        fig, ax = plt.subplots(1,3,figsize=(14,4),dpi=200, sharex=True, sharey=True)
        ax[0].quiver(ra, dec, dra/3600, ddec/3600, scale=scale, scale_units=scale_units)
        ax[0].set_title('Data')
        ax[0].set_aspect('equal')
        ax[1].quiver(ra, dec, dra_fit/3600, ddec_fit/3600, scale=scale, scale_units=scale_units)
        ax[1].set_title('Fit')
        ax[1].set_aspect('equal')
        ax[2].quiver(ra, dec, (dra_fit-dra)/3600, (ddec_fit-ddec)/3600, scale=scale, scale_units=scale_units)
        ax[2].set_title('Residuals')
        ax[2].set_aspect('equal')
        plt.tight_layout()
        plt.show()

        from time import sleep
        sleep(0.5)


    return lra,ldec


def main():
    # Parse arguments 
    parser = argparse.ArgumentParser(description='NIRSpec Data Reduction Pipeline')
    parser.add_argument('--obs', type=str, required=True, 
                        help='Observation name from observations.toml')
    parser.add_argument('--overwrite', action='store_true',
                        help='Overwrite existing products')
    parser.add_argument('--skip-shifts', action='store_true',
                        help='Skip fitting astrometric shifts')
    parser.add_argument('--approve-shifts', action='store_true',
                        help='Auto approve astrometric shifts')
    
    args = parser.parse_args()
    skip_shifts = args.skip_shifts


    obs = toml.load('observations.toml')[args.obs]
    field = obs.get('field')
    obs = args.obs

    if not skip_shifts:
        # Search for MSA catalog file
        msacat_file = f'products/{obs}/{obs}_msacat.csv'
        if not os.path.exists(msacat_file):
            raise FileNotFoundError("No MSA catalog file found! Probably need to retrieve from APT")

        msa_cat = Table.read(msacat_file)[1:]
        msa_coords = SkyCoord(msa_cat['RA'], msa_cat['DEC'], unit='deg')

        match field:
            case 'uds':
                ref_cat = fits.open('/V/maurice/primeruds_photom_v0.89.fits')
                ref_coords = SkyCoord(ref_cat[1].data['RA'], ref_cat[1].data['DEC'], unit='deg')
            case 'egs':
                ref_cat = fits.open('/V/maurice/ceers_photom_v0.9.fits')
                ref_coords = SkyCoord(ref_cat[1].data['RA'], ref_cat[1].data['DEC'], unit='deg')
            case 'cosmos':
                ref_cat = fits.open('/research/COSMOS-3D/catalog_cosmos_v1.1_merged.fits')
                ref_coords = SkyCoord(ref_cat[1].data['ra'], ref_cat[1].data['dec'], unit='deg')
            case _:
                raise NotImplementedError

        # Discover *_spec.fits files 
        srcids = sorted(list(set([int(f.split('_')[-2]) for f in glob.glob(f'products/{obs}/{obs}_*_spec.fits')])))
        object_ids = [f'{obs}_{i}' for i in srcids]
        obs_ra, obs_dec = [], []
        for i in range(len(object_ids)):
            srcid = srcids[i]
            spec_files = glob.glob(f'products/{obs}/{obs}_*_{srcid}_spec.fits')
            ra, dec = get_source_pos(spec_files[0])
            obs_ra.append(ra)
            obs_dec.append(dec)
        obs_coords = SkyCoord(obs_ra, obs_dec, unit='deg')

        # Filter MSA catalog coordinates to <1.5 arcmin from nearest observed object
        idx, d2d, d3d = msa_coords.match_to_catalog_sky(obs_coords)
        msa_coords = msa_coords[d2d < 1.5*u.arcmin]


        # dra_interp, ddec_interp = fit_offsets(msa_coords, ref_coords, method='rbf', smoothing=0.01, plot=True)
        if not args.approve_shifts:
            dra_interp, ddec_interp = fit_offsets(msa_coords, ref_coords, method='poly2d', smoothing=0.01, plot=True)
            cont = input('Satisfied with offset fits [Y/n]? ')
            if not cont == 'Y':
                return
        else:
            dra_interp, ddec_interp = fit_offsets(msa_coords, ref_coords, method='poly2d', smoothing=0.01, plot=False)
    else:
        srcids = sorted(list(set([int(f.split('_')[-2]) for f in glob.glob(f'products/{obs}/{obs}_*_spec.fits')])))
        object_ids = [f'{obs}_{i}' for i in srcids]
    

    import tqdm
    for i in tqdm.tqdm(range(len(object_ids))):
        object_id = object_ids[i]
        srcid = srcids[i]
        # print(object_id)
        # if object_id not in ['capers_egs_p5_16994']:
            # continue
        
        spec_files = glob.glob(f'products/{obs}/{obs}_*_{srcid}_spec.fits')
        
        ra, dec = get_source_pos(spec_files[0])
        print(object_id)
        
        if not skip_shifts:
            drai = dra_interp(ra,dec)/3600 
            ddeci = ddec_interp(ra,dec)/3600
            if drai > 8e-5 or ddeci > 8e-5:
                drai, ddeci = 0,0
            
            ra += drai
            dec += ddeci
        out = f'products/{obs}/{obs}_{srcid}_rgb.png'
        if os.path.exists(out) and not args.overwrite: 
            continue
        plot_single_object(spec_files, field, output=out, override_pos=(ra,dec))

    #     obs_ra.append(ra)
    #     obs_dec.append(dec)
    
    # obs_coords = SkyCoord(obs_ra, obs_dec, unit='deg')




if __name__ == "__main__":
    main()