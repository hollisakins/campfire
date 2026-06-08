from jwst import pipeline, associations
from astropy.io import fits
from astropy import table
from astropy.coordinates import SkyCoord
import numpy as np
from scipy.interpolate import interp1d
from scipy.optimize import curve_fit
import crds
import glob
import matplotlib.pyplot as plt
import os
import subprocess
from multiprocessing import Pool
import warnings; warnings.filterwarnings('ignore')
import shutil
import asdf
import json
from datetime import datetime
from tqdm import tqdm
from campfire import Campfire

os.environ['CRDS_PATH'] = os.environ.get('CAMPFIRE_ROOT')+'/cache/crds/'
os.environ['CRDS_SERVER_URL'] = 'https://jwst-crds.stsci.edu'
os.environ['CRDS_CONTEXT']='jwst_1464.pmap'

steps={'detector1':False,
       'extend_flats':True,
       'spec2':False,
       'spec3':False,
       'calibrate':True}

ncores=10

# Run detector1

if steps['detector1']:

    print('Downloading uncal and MSA data from MAST...')
    subprocess.run(['cfpipe','download','--program','9214'])
    for msa_meta_file in glob.glob(os.environ.get('CAMPFIRE_ROOT')+'/raw/9214/*msa.fits'):
        shutil.copy2(msa_meta_file, f'{os.getcwd()}/{msa_meta_file.split('/')[-1]}')
    for uncal_file in glob.glob(os.environ.get('CAMPFIRE_ROOT')+'/raw/9214/*uncal.fits'):
        os.symlink(uncal_file,f'{os.getcwd()}/{uncal_file.split('/')[-1]}')
    files=sorted(glob.glob('*uncal.fits'))

    def runS1(x): 
        print(x)
        try: 
            pipeline.Detector1Pipeline.call(x,save_results=True,steps={'clean_flicker_noise':{'skip':False,'mask_science_regions':True}})
            os.remove(x.replace('uncal.fits','rateints.fits'))
            os.remove(x)
        except: 
            print('No data on detector... skipping '+x)
        return

    with Pool(ncores) as pool:
        _=pool.map(runS1,sorted(glob.glob('*uncal.fits')))


# Extends Flats

maxwaves={'G140M':{'F100LP':1.87,'F070LP':1.27},'G235M':{'F170LP':3.17}}#'PRISM':{'CLEAR':5.3},}

def generate_extended_cals(rate_files,cwd):
    print('Fetching CRDS data...')
    allrefs=set()
    for rate_file in rate_files:
        hdr = fits.getheader(rate_file)
        det = hdr.get('DETECTOR', 'NRS1')
        grating = hdr.get('GRATING', 'UNKNOWN')
        filt = hdr.get('FILTER', 'UNKNOWN')
        config_key = (det, grating, filt)
        seen_configs = set()
        if config_key in seen_configs: continue
        seen_configs.add(config_key)
        reftypes = ['fflat','sflat','photom','wavelengthrange']
        params = {
            "INSTRUME": "NIRSPEC",
            "DETECTOR": det,
            "EXP_TYPE": hdr.get('EXP_TYPE', 'NRS_MSASPEC'),
            "FILTER": filt,
            "GRATING": grating,
            "DATE-OBS": hdr.get('DATE-OBS', '2023-01-01'),
            "TIME-OBS": hdr.get('TIME-OBS', '00:00:00'),
        }
        try: 
            refs = crds.getreferences(params, reftypes=reftypes, observatory='jwst')
            for i in refs: allrefs.add(refs[i])
        except Exception as e: print(f"CRDS prefetch warning for {config_key}: {e}")

    photom=[ref for ref in allrefs if 'photom' in ref][0]
    fflats=[ref for ref in allrefs if 'fflat' in ref]
    sflats=[ref for ref in allrefs if 'sflat' in ref]
    wavelengthrange_file=[ref for ref in allrefs if 'wavelengthrange' in ref][0]

    print('Extending Flat Fields...')

    for file in fflats:
        with fits.open(file) as fflat:
            grat=fflat[0].header['GRATING'].strip()
            filt=fflat[0].header['FILTER'].strip()
            if grat in maxwaves:
                if filt in maxwaves[grat]: pass
                else: 
                    print(f"Grating/filter: {grat}/{filt} not supported")
                    continue
            else: 
                print(f"Grating: {grat} not supported")
                continue
            newflat=fits.HDUList()
            for num,i in enumerate(fflat):
                if num in [5,10,15,20]:
                    tab=table.Table.read(file,hdu=num)
                    newwave=np.round(np.arange(np.round(np.min(tab['wavelength'][0]),3),5.301,0.001),3)
                    newflux=np.interp(newwave,tab['wavelength'][0],tab['data'][0])#
                    newflux[newwave>=maxwaves[grat][filt]]=newflux[newwave==maxwaves[grat][filt]] #flat after last real value
                    newflux[np.isnan(newflux)]=0
                    tab['newwavelength']=newwave[:,None].T.astype('float32')
                    tab['newdata']=newflux[:,None].T.astype('float32')
                    tab['newerror']=newflux[:,None].T.astype('float32')*np.nan
                    for col in ['wavelength','data','error']:
                        del tab[col]
                        tab['new'+col].name=col
                    tab['nelem']=len(newflux)
                    temp=fits.table_to_hdu(tab)
                    newflat.append(temp)
                else: newflat.append(fflat[num])
            newflat.writeto(f'{cwd}/extended_{file.split('/')[-1]}',overwrite=1)

    for file in sflats:
        with fits.open(file) as sflat:
            grat=sflat[0].header['GRATING'].strip()
            filt=sflat[0].header['FILTER'].strip()
            if grat in maxwaves:
                if filt in maxwaves[grat]: pass
                else: 
                    print(f"Grating/filter: {grat}/{filt} not supported")
                    continue
            else: 
                print(f"Grating: {grat} not supported")
                continue    
            tab=table.Table.read(file,hdu=5)
            newwave=np.round(np.arange(np.round(np.min(tab['wavelength'][0][tab['wavelength'][0]>0]),3),5.301,0.001),3)
            newflux=interp1d(tab['wavelength'][0],tab['data'][0],bounds_error=0,fill_value='extrapolate')(newwave)
            newflux[newwave>=maxwaves[grat][filt]]=newflux[newwave==maxwaves[grat][filt]] # flat after last real value
            tab['newwavelength']=newwave[:,None].T.astype('float32')
            tab['newdata']=newflux[:,None].T.astype('float32')
            for col in ['wavelength','data']:
                del tab[col]
                tab['new'+col].name=col
            tab['nelem']=len(newflux)
            sflat[5]=fits.table_to_hdu(tab)
            sflat[1].header["FLAT_05"]=5.3
            sflat[4].data[-1]=(5.3,)
            sflat.writeto(f'{cwd}/'+file.split('/')[-1].replace('jwst','extended_jwst'),overwrite=1)

    with fits.open(photom) as hdul: #create temporary extended photom file, to be updated later after extended calibration
        tab=table.Table.read(photom,hdu=1)
        newwave=np.arange(0.6,5.301,0.001)
        newflux=np.ones_like(newwave)
        tab['newwavelength']=newwave[:,None].T.astype('float32')
        tab['newrelresponse']=newflux[:,None].T.astype('float32')
        tab['newreluncertainty']=newflux[:,None].T.astype('float32')*0
        for col in ['wavelength','relresponse','reluncertainty']:
            del tab[col]
            tab['new'+col].name=col
        tab['nelem']=len(newflux)
        hdul[1]=fits.table_to_hdu(tab)
        hdul.writeto(f'{cwd}/uncalibrated_extended_'+photom.split('/')[-1],overwrite=1)

    wavelength_updates = {'F100LP_G140M': [9.7e-07, 5.3e-06],'F170LP_G235M': [1.66e-06, 5.3e-06]}
    with asdf.open(wavelengthrange_file) as af:
        selectors = af['waverange_selector']
        wavelength_ranges = af['wavelengthrange']
        for filter_grating, new_range in wavelength_updates.items():
            if filter_grating in selectors:
                idx = selectors.index(filter_grating)
                old_range = wavelength_ranges[idx].copy()
                wavelength_ranges[idx] = new_range
            else:
                print(f"Warning: {filter_grating} not found in waverange_selector")        
        new_history_entry = {
            'description': f"Updated wavelength ranges for {', '.join([filter_grating for filter_grating in wavelength_updates])}",
            'software': {
                'author': 'anthony.taylor@austin.utexas.edu',
                'name': 'Generate_Extended_Cals.py',
                'version': '0.0.0'
            },
            'time': datetime.now()
        }
        if 'history' in af.tree and 'entries' in af['history']:
            af['history']['entries'].append(new_history_entry)
        af['meta']['date'] = datetime.now().isoformat()
        af.write_to(f'{cwd}/extended_'+wavelengthrange_file.split('/')[-1])


if steps['extend_flats']:
    files=sorted(glob.glob('*rate.fits'))
    generate_extended_cals(files,os.getcwd())
    
# generate association files and run spec2
if steps['spec2']:

    print('Running spec2...')

    files=sorted(glob.glob('*rate.fits'))

    def runS2(x):
        scifile=''
        with open(x,'r') as file:
            assn=json.load(file)
            for ratefile in assn['products'][0]['members']:
                if ratefile['exptype']=='science': scifile=ratefile['expname']
        if scifile=='': raise Exception(f'Science file not found in association file {x}')
        with fits.open(scifile) as hdul:
            detector=hdul[0].header['DETECTOR'].strip()
            filt=hdul[0].header['FILTER'].strip()
            grat=hdul[0].header['GRATING'].strip()
        alt_fflat=None
        for fflat in glob.glob('extended_jwst_nirspec_fflat_????.fits'):
            with fits.open(fflat) as hdul:
                flatfilt=hdul[0].header['FILTER'].strip()
                flatgrat=hdul[0].header['GRATING'].strip()
                if flatfilt==filt and flatgrat==grat: 
                    alt_fflat=fflat
                    break
        # alt_dflat=None
        # for dflat in glob.glob('extended_jwst_nirspec_dflat_????.fits'):
        #     with fits.open(dflat) as hdul:
        #         flatdetector=hdul[0].header['DETECTOR'].strip()
        #         if flatdetector==detector: 
        #             alt_dflat=dflat
        #             break
        alt_sflat=None
        for sflat in glob.glob('extended_jwst_nirspec_sflat_????.fits'):
            with fits.open(sflat) as hdul:
                flatdetector=hdul[0].header['DETECTOR'].strip()
                flatfilt=hdul[0].header['FILTER'].strip()
                flatgrat=hdul[0].header['GRATING'].strip()
                if flatdetector==detector and flatfilt==filt and flatgrat==grat: 
                    alt_sflat=sflat
                    break
        alt_photom = 'uncalibrated_extended_jwst_nirspec_photom_0015.fits'
        alt_waverange = 'extended_jwst_nirspec_wavelengthrange_0008.asdf'
        steps={'extract_1d': {'skip': True},
            'flat_field': {'override_fflat': alt_fflat,'override_sflat': alt_sflat},# 'override_dflat': alt_dflat},
            'nsclean': {'skip': True},
            'assign_wcs': {'override_wavelengthrange': alt_waverange},
            'photom': {'override_photom': alt_photom},
            }
        # steps={'extract_1d': {'skip': True},'flat_field': {'override_fflat': alt_fflat},'nsclean': {'skip': True}}
        try: 
            _=pipeline.Spec2Pipeline.call(x, save_results=True, steps=steps)
            os.remove(x)
            os.remove(x.replace('spec2.json','s2d.fits'))
        except: os.remove(x)
        return

    files=table.Table()
    files['filename']=sorted(glob.glob('jw09214*_*_rate.fits'))
    files['nrs']=[file.split('nrs')[-1][0] for file in files['filename']]
    files['obs']=[file['filename'].split('_')[0] for file in files]
    files['config']=[file['filename'].split('_')[1] for file in files]
    files['nod']=[file['filename'].split('_')[2] for file in files]

    for nrs in np.unique(files['nrs']):
        for obs in np.unique(files['obs']):
            for config in np.unique(files['config']):
                targetfiles=files[(files['nrs']==nrs) & (files['obs']==obs) & (files['config']==config)]
                if len(targetfiles)==0: continue
                for file in targetfiles:
                    bkg_ims=targetfiles['filename'][targetfiles['nod']!=file['nod']]
                    if len(bkg_ims)%2!=0 or len(bkg_ims)==0: 
                        print('Missing nods for',file['filename'])
                        continue
                    association=[(file['filename'], 'science')]
                    for bkg in bkg_ims: association.append((bkg,'background'))
                    prod_name = file['filename'].replace('_rate.fits', '')
                    asn = associations.asn_from_list.asn_from_list(association,with_exptype=True,product_name=prod_name)
                    suggested_name, serialization = asn.dump()
                    if os.path.exists(f'{prod_name}_cal.fits'):
                        print(f'{prod_name} already exists')
                    else:
                        with open(f'{prod_name}_spec2.json','w') as new_file: new_file.write(serialization)
                        print(prod_name)
    assns=glob.glob('*_spec2.json')

    with Pool(ncores) as pool: _=pool.map(runS2,assns)

# generate association files and run spec3
if steps['spec3']:

    print('Running spec3...')

    files=table.Table()
    files['filename']=sorted(glob.glob('*nrs?_cal.fits'))
    files['grating']=[fits.getheader(file)['GRATING'] for file in files['filename']]
    files['obs']=[int(file['filename'].split('_')[0][7:10]) for file in files]

    for obs in np.unique(files['obs']):
        for grating in np.unique(files['grating']):
            targetfiles=files[(files['grating']==grating) & (files['obs']==obs)]
            if len(targetfiles)>0:
                association=[(file['filename'], 'science') for file in targetfiles]
                prod_name = f"jw09214{targetfiles['obs'][0]:03}_"+targetfiles['grating'][0]
                asn = associations.asn_from_list.asn_from_list(association,with_exptype=True,product_name=prod_name)
                suggested_name, serialization = asn.dump()
                with open(f'{prod_name}_spec3.json', 'w') as new_file: new_file.write(serialization)
                print(prod_name)

    def runS3(x):
        print(x)
        _ = pipeline.Spec3Pipeline.call(x,save_results=True)
        os.remove(x)
        return 

    with Pool(ncores) as pool: pool.map(runS3,glob.glob('*spec3.json'))

# Read in all spectra

if steps['calibrate']:
    print('Fitting emission lines and calibrating extended wavelength ranges...')
    allobs=table.Table()
    allobs['filename']=glob.glob('*_s?????????_x1d.fits')
    temp=[]
    temp2=[]
    temp3=[]
    temp4=[]
    temp5=[]
    temp6=[]
    print('Loading Spectra...')
    for file in allobs['filename']:
        with fits.open(file) as hdu:
            temp.append(hdu[1].header['SRCNAME'])
            temp2.append(float(hdu[1].header['SRCRA']))
            temp3.append(float(hdu[1].header['SRCDEC']))
            temp4.append(float(hdu[1].header['SRCXPOS']))
            temp5.append(float(hdu[1].header['SRCYPOS']))
            temp6.append(float(hdu[0].header['EFFEXPTM']))
    allobs['SRCNAME']=temp
    allobs['RA']=temp2
    allobs['DEC']=temp3
    allobs['SRCXPOS']=temp4
    allobs['SRCYPOS']=temp5
    allobs['EFFEXPTM']=temp6
    allobs=allobs[allobs['RA']>0]
    allobs['ID']=[int(i.split('_s')[1].split('_')[0]) for i in allobs['filename']]
    allobs['OBS']=[int(i[7:10]) for i in allobs['filename']]

    # combine rows of the table with differnet gratings

    b=allobs[['G140M' in i for i in allobs['filename']]]
    g=allobs[['G235M' in i for i in allobs['filename']]]
    r=allobs[['G395M' in i for i in allobs['filename']]]
    for col in r.columns.copy():
        if col not in ['ID','RA','DEC','OBS']: r[col].name=col+'_r'
    for col in g.columns.copy():
        if col not in ['ID','RA','DEC','OBS']: g[col].name=col+'_g'
    for col in b.columns.copy():
        if col not in ['ID','RA','DEC','OBS']: b[col].name=col+'_b'

    allobs=table.join(b,g)
    allobs=table.join(allobs,r)

    # Load the actual spectra and store in the table

    temp={'b':[],'g':[],'r':[]}
    for i in allobs:
        for grat in ['b','g','r']:
            tab=table.Table.read(i[f'filename_{grat}'])
            for col in tab.columns.copy():
                try: tab[col]=tab[col].filled(np.nan)
                except: pass
            temp[grat].append([tab['WAVELENGTH'].value,tab['FLUX'].value,tab['FLUX_ERROR'].value])
    for grat in ['b','g','r']:
        maxlength=np.max([len(i[0]) for i in temp[grat]])
        for num in range(len(temp[grat])):
            if len(temp[grat][num][0])<maxlength:
                for i in range(3):
                    temp[grat][num][i]=np.concatenate([temp[grat][num][i],np.ones(maxlength-len(temp[grat][num][i]))*np.nan])
            # temp['grat'][num]=temp['grat'][num][:3]
        temp[grat]=np.array(temp[grat])
        allobs[f'wave_{grat}']=temp[grat][:,0,:]
        allobs[f'flux_{grat}']=temp[grat][:,1,:]
        allobs[f'err_{grat}']=temp[grat][:,2,:]

    # Fetch known redshifts from CAMPFIRE database
    cf=Campfire()
    cf.sync()

    zs=table.Table.read(f"{os.environ.get('CAMPFIRE_ROOT')}/meta/objects.csv",format='ascii.csv')
    zs=zs[['spurs' in i for i in zs['programs']]]
    idx,sep,_=SkyCoord(allobs['RA'],allobs['DEC'],unit='deg').match_to_catalog_sky(SkyCoord(zs['ra'],zs['dec'],unit='deg'))
    spurs=table.hstack([allobs,zs[idx]])
    spurs=spurs[sep.arcsec<0.2]
    spurs=spurs[spurs['redshift_quality']==4]

    # Fit emission lines in the data

    def linemodel(wave,amp,fwhm,center,cont,slope):
        sigma=fwhm/3e5*center/2.335
        return amp*np.exp(-0.5*((wave-center)/sigma)**2)+slope*(wave-center)+cont

    lines=np.array([3727,4861,4959,5007,6563,9069,9531,10830,10938,12820])

    def fit_line(row,line,grat,iters=1000,plot=False):
        maxfwhm=1500
        oneplusz=1+row['redshift']
        waverange=np.abs(line/1e4*oneplusz-row[f'wave_{grat}'])/(line/1e4*oneplusz)*3e5<maxfwhm
        wave,flux,err=row[f'wave_{grat}'][waverange],row[f'flux_{grat}'][waverange],row[f'err_{grat}'][waverange]
        flux,err=flux*3e18*1e-23/(wave*1e4)**2,err*3e18*1e-23/(wave*1e4)**2
        finite=np.isfinite(wave) & np.isfinite(flux) & np.isfinite(err)
        wave,flux0,err=wave[finite],flux[finite],err[finite]
        if np.max(flux0/err)<5: return np.nan
        out=[]
        for i in range(iters):
            flux=flux0+np.random.normal(0,err)
            p0=[np.max(flux)-np.median(flux),200.0,line/1e4*oneplusz,np.median(flux),1e-30]
            bounds=[[0,50,np.min(wave),np.min(flux),-1e-17],
                    [np.max(flux)+np.abs(np.median(flux)),maxfwhm,np.max(wave),np.max(flux),1e-17]]
            try:
                popt,pcov=curve_fit(linemodel,wave,flux,sigma=err,p0=p0,bounds=bounds)
                amp,fwhm,center,cont,slope=popt
                if plot:
                    plt.step(wave/oneplusz,flux,where='mid')
                    xrange=np.linspace(np.min(wave),np.max(wave),1000)
                    plt.plot(xrange/oneplusz,linemodel(xrange,amp,fwhm,center,cont,slope))
                    plt.show()
                out.append(amp*np.sqrt(2*np.pi)*(fwhm/3e5*center*1e4/2.335))
            except RuntimeError: out.append(np.nan)
        if np.nanmedian(out)*2/(np.nanquantile(out,0.84)-np.nanquantile(out,0.16))<10: return np.nan
        else: return np.nanmedian(out)

    def fit_lines(row,plot=False):
        grats=['b','g','r']
        out={i:[] for i in grats}
        out['z']=row['redshift']
        oneplusz=1+row['redshift']
        for grat in grats:
            for line in lines:
                if np.isfinite(row[f'flux_{grat}'][np.nanargmin(np.abs(row[f'wave_{grat}']-line/1e4*oneplusz))]) and np.sum([np.isfinite(row[f'flux_{grati}'][np.nanargmin(np.abs(row[f'wave_{grati}']-line/1e4*oneplusz))]) for grati in grats])>=2: #check if line is present in spectrum and is presnet in at least two gratings
                    out[grat].append(fit_line(row,line,grat,plot=plot,iters=500))
                else: out[grat].append(np.nan)
            out[grat]=np.array(out[grat])
        return out
    
    print('Fitting emission lines...')
    with Pool(ncores,maxtasksperchild=1) as pool: tests=list(tqdm(pool.imap(fit_lines,spurs,chunksize=1),total=len(spurs)))

    # Assemble results and derive calibration curves

    print('Computing calibration curves...')

    bcal=table.Table([np.array([lines*(1+test['z'])/1e4 for test in tests]).flatten(),np.array([test['b']/test['g'] for test in tests]).flatten()],names=['wave','ratio'])
    gcal=table.Table([np.array([lines*(1+test['z'])/1e4 for test in tests]).flatten(),np.array([test['g']/test['r'] for test in tests]).flatten()],names=['wave','ratio'])
    bcal=bcal[np.isfinite(bcal['ratio'])]# & (bcal['wave']>1.9)]
    gcal=gcal[np.isfinite(gcal['ratio'])]# & (gcal['wave']>3.2)]
    # bcal['ratio'][bcal['ratio']>1]=1
    # gcal['ratio'][gcal['ratio']>1]=1

    bpfit=np.polyfit(bcal['wave'][bcal['wave']>1.9],bcal['ratio'][bcal['wave']>1.9],2)
    gpfit=np.polyfit(gcal['wave'][gcal['wave']>3.2],gcal['ratio'][gcal['wave']>3.2],2)

    print('G140M/F100LP fit: ',bpfit)
    print('G235M/F170LP fit: ',gpfit)

    # Plot results

    plt.figure(dpi=200)
    plt.scatter(bcal['wave'],bcal['ratio'],c='g')
    plt.scatter(gcal['wave'],gcal['ratio'],c='r')
    xrange=np.linspace(1,3.2,1000)
    plt.plot(xrange,np.dot(bpfit,np.array([xrange**i for i in np.arange(len(bpfit))[::-1]])))
    xrange=np.linspace(1,5.3,1000)
    plt.plot(xrange,np.dot(gpfit,np.array([xrange**i for i in np.arange(len(gpfit))[::-1]])))
    plt.ylabel('G140M/G235M or G235M/G395M')
    plt.xlabel('Observed Wavelength [A]')
    plt.ylim(0,6)
    plt.savefig('Line_Ratios.pdf')
    plt.close()

    waverange=np.arange(0.6,5.301,0.001)
    bcorr=np.dot(bpfit,np.array([waverange**i for i in np.arange(len(bpfit))[::-1]]))
    bcorr[waverange<1.87]=1
    bcorr[waverange>3.17]=0
    gcorr=np.dot(gpfit,np.array([waverange**i for i in np.arange(len(gpfit))[::-1]]))
    gcorr[waverange<3.17]=1
    corrs=[gcorr,bcorr]

    sflats=[]
    fflats=[]
    for i in sorted(glob.glob('extended_jwst_nirspec_sflat_????.fits')):
        tab=table.Table.read(i,hdu=5)
        subtab=table.Table([tab['wavelength'][0],tab['data'][0]],names=['wavelength','data'])
        subtab.sort('wavelength')
        subtab=subtab[subtab['data']>0]
        sflats.append(np.interp(waverange,subtab['wavelength'],subtab['data']))
    for i in sorted(glob.glob('extended_jwst_nirspec_fflat_????.fits')):
        tab=table.Table.read(i,hdu=5)
        subtab=table.Table([tab['wavelength'][0],tab['data'][0]],names=['wavelength','data'])
        subtab.sort('wavelength')
        subtab=subtab[subtab['data']>0]
        fflats.append(np.interp(waverange,subtab['wavelength'],subtab['data']))

    plt.figure(dpi=150)
    for num,f in enumerate(fflats):
        for s in sflats[num::2]:
            plt.plot(waverange,s*f*corrs[num])
            if num==1: plt.scatter(bcal['wave'],bcal['ratio']*np.interp(bcal['wave'],waverange,s*f),c='grey',marker='.',lw=0)
            else: plt.scatter(gcal['wave'],gcal['ratio']*np.interp(gcal['wave'],waverange,s*f),c='grey',marker='.',lw=0)
    plt.ylabel('Throughput')
    plt.xlabel('Wavelength [um]')
    plt.ylim(0,1.3e11)
    plt.savefig('Final_Calibration.pdf')
    plt.close()

    photom_file=sorted(glob.glob('uncalibrated_extended_jwst_nirspec_photom_0015.fits'))[0]
    with fits.open(photom_file) as hdul: #create final extended photom file
        tab=table.Table.read(photom_file,hdu=1)
        b_idx=np.where((tab['filter']=='F100LP') & (tab['grating']=='G140M'))[0]
        g_idx=np.where((tab['filter']=='F170LP') & (tab['grating']=='G235M'))[0]
        newwave=np.round(np.arange(0.6,5.301,0.001),3)
        newflux=np.ones_like(newwave)
        tab['newwavelength']=newwave[:,None].T.astype('float32')
        tab['newrelresponse']=newflux[:,None].T.astype('float32')
        tab['newreluncertainty']=newflux[:,None].T.astype('float32')*0
        tab['newrelresponse'][b_idx]=1/bcorr
        tab['newwavelength'][b_idx][np.isnan(tab['newrelresponse'][b_idx])]=0
        tab['newrelresponse'][b_idx][np.isnan(tab['newrelresponse'][b_idx])]=0
        tab['newrelresponse'][g_idx]=1/gcorr
        tab['newwavelength'][g_idx][np.isnan(tab['newrelresponse'][g_idx])]=0
        tab['newrelresponse'][g_idx][np.isnan(tab['newrelresponse'][g_idx])]=0
        for col in ['wavelength','relresponse','reluncertainty']:
            del tab[col]
            tab['new'+col].name=col
        tab['nelem']=tab['nelem'].astype('int32')
        for num in range(len(tab)): tab['nelem'][num]=len(tab['relresponse'][num][tab['relresponse'][num]>0])
        hdul[1]=fits.table_to_hdu(tab)
        hdul.writeto(photom_file.replace('uncalibrated_extended_','extended_'),overwrite=1)