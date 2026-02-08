import numpy as np
from astroquery.mast import Observations
from astroquery.exceptions import InvalidQueryError
from glob import glob
import os
import shutil
from astropy.io import fits

LOOP_OBS = True

def get_products(
    proposal_id,
    observation_id=None,
    instrument_name='NIRSpec/MSA',
    coordinates = None,
    radius = None,
):
    
    args = {'obs_collection':'JWST'}

    if not isinstance(proposal_id, list):
        args['proposal_id'] = [proposal_id]
    else:
        args['proposal_id'] = proposal_id

    if instrument_name is not None:
        args['instrument_name'] = instrument_name

    if coordinates is not None:
        args['coordinates'] = coordinates
    if radius is not None:
        args['radius'] = radius

    print(f"Querying observations for proposal_id = {args['proposal_id']}")

    obs = Observations.query_criteria(**args)

    print("Getting products")  
    # try:   
    #     if LOOP_OBS:
    #         from astropy.table import vstack
    #         product_list = [Observations.get_product_list(o) for o in obs]
    #         products = vstack(product_list)
    #     else:
    products = Observations.get_product_list(obs)
    # except InvalidQueryError:
    #     print("No products found")
    #     return None
    
    return products

def filter_products(
    products, 
    filters,
    obs=None,
    overwrite=False,
    data_dir='/n23data2/hakins/jwst/data/cosmos/',
    proposal_ids_to_skip = [],
):
    print("Filtering products")    
    calib_level=1,
    product_type='SCIENCE' 
    extension='uncal.fits'

    # get list of extensions for downloading only raw or cals or something
    exts = np.array([x.split('_')[-1] for x in products['productFilename']])

    if filters == '*':
        filters = np.unique(products['filters'])

    products_filters = []
    for f in products['filters']:
        if f == 'F150W2;F162M':
            products_filters.append('F162M')
        else:
            products_filters.append(f)
    products_filters = np.array(products_filters)

    if overwrite:
        w = np.where(
            np.logical_and.reduce((
                products['productType'] == product_type,
                products['calib_level'] == calib_level,
                exts == extension,
                np.isin(products_filters, np.array([f.upper() for f in filters])),
                ~np.isin(products['proposal_id'], proposal_ids_to_skip),
                products['dataRights'] == 'PUBLIC',
            ))
        )
    else:
        w = np.where(
            np.logical_and.reduce((
                products['productType'] == product_type,
                products['calib_level'] == calib_level,
                exts == extension,
                np.isin(products_filters, np.array([f.upper() for f in filters])),
                np.array([not os.path.exists(os.path.join(data_dir, filt.lower(), filename)) for filt, filename in zip(products_filters, products['productFilename'])], dtype=bool),
                ~np.isin(products['proposal_id'], proposal_ids_to_skip),
                products['dataRights'] == 'PUBLIC',
            ))
        )

    print("Before filtering...")
    print('Total products:', len(products))
    print("Unique proposal IDs:", np.unique(list(products['proposal_id'])))
    print("Unique filters:", np.unique(list(products['filters'])))
    print("")
    
    products = products[w]
    if obs is not None:
        obs_ids = np.array([int(i.split('_')[0][7:-3]) for i in products['obs_id']])
        products = products[np.isin(obs_ids, obs)]

    print("After filtering...")
    print('Total products:', len(products))
    print("Unique proposal IDs:", np.unique(list(products['proposal_id'])))
    print("Unique filters:", np.unique(list(products['filters'])))

    print("")
    print('Available data products:')
    products.pprint(max_lines=300)

    q = input('Do you wish to continue? [y/n] ')
    if not q in ['y','Y']:
        quit()

    return products



def download_data(
    proposal_id,
    filters=None, 
    instrument_name='NIRCAM*',
    obs=None,
    data_dir='/n23data2/hakins/jwst/data/cosmos/',
    download_dir = '/n23data2/hakins/jwst/temp/',
    coordinates = '150.118903 2.204694', 
    radius = '35 arcmin',
    overwrite = False,
):

    products = get_products(
        proposal_id, 
        filters=filters, 
        instrument_name=instrument_name,
        coordinates=coordinates,
        radius=radius,
    )

    products = filter_products(products, filters, obs=obs, overwrite=overwrite, data_dir=data_dir)

    # actually download the data - to download_dir
    manifest = Observations.download_products(products, download_dir=download_dir)

    # move the downloaded data to the data folder
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    files  = glob(os.path.join(download_dir,'mastDownload','JWST','jw*','*.fits'))[:]
    for file in files: 
        with fits.open(file) as hdul:
            # hdul.verify('fix')
            filt_name    = hdul[0].header['FILTER'].lower()
            final_dir = os.path.join(data_dir, filt_name)
            if not os.path.exists(final_dir):
                os.mkdir(final_dir)
            
        shutil.move(file, os.path.join(final_dir, file.split('/')[-1]))
    
    dirs = glob(os.path.join(download_dir,'mastDownload','JWST','jw*'))[:]
    for d in dirs:
        os.rmdir(d)
    os.rmdir(os.path.join(download_dir, 'mastDownload', 'JWST'))
    os.rmdir(os.path.join(download_dir, 'mastDownload'))

def download_all_data_for_program(
    proposal_id,
    filters=None, 
    instrument_name='NIRCAM*',
    obs=None,
    data_dir='/n23data2/hakins/jwst/data/cosmos/',
    download_dir = '/n23data2/hakins/jwst/temp/',
    overwrite = False,
):

    products = get_products(
        proposal_id, 
        filters=filters, 
        instrument_name=instrument_name,
        coordinates=None,
        radius=None,
    )

    products = filter_products(products, filters, obs=obs, overwrite=overwrite, data_dir=data_dir)

    # actually download the data - to download_dir
    manifest = Observations.download_products(products, download_dir=download_dir)

    # move the downloaded data to the data folder
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    files  = glob(os.path.join(download_dir,'mastDownload','JWST','jw*','*.fits'))[:]
    for file in files: 
        with fits.open(file) as hdul:
            # hdul.verify('fix')
            filt_name    = hdul[0].header['FILTER'].lower()
            final_dir = os.path.join(data_dir, filt_name)
            if not os.path.exists(final_dir):
                os.mkdir(final_dir)
            
        shutil.move(file, os.path.join(final_dir, file.split('/')[-1]))
    
    dirs = glob(os.path.join(download_dir,'mastDownload','JWST','jw*'))[:]
    for d in dirs:
        os.rmdir(d)
    os.rmdir(os.path.join(download_dir, 'mastDownload', 'JWST'))
    os.rmdir(os.path.join(download_dir, 'mastDownload'))

def download_data_cosmos(
    proposal_id,
    filters, 
    instrument_name='NIRCAM*',
    data_dir='/n23data2/hakins/jwst/data/cosmos/',
    download_dir = '/n23data2/hakins/jwst/temp/',
    proposal_ids_to_skip = [],
    overwrite = False,
):

    from astropy.table import Table, vstack, unique


    positions = [
        ['150.1165123 2.2094715','20.5 arcmin'],
        ['150.4082754 2.3731163','7 arcmin'],
        ['150.0005011 2.5225641','7 arcmin'],
        ['150.2336640 1.8910217','7 arcmin'],
        ['149.8302241 2.0442104','7 arcmin'],
        ['150.3224947 2.4854362','2 arcmin'],
        ['150.1403104 2.5545695','2 arcmin'],
        ['150.0953522 1.8564664','2 arcmin'],
        ['149.9120839 1.9209629','2 arcmin'],
        ['150.2544670 2.5200039','1.3 arcmin'],
        ['150.2198750 2.5338306','1.3 arcmin'],
        ['150.0273492 1.8714393','1.3 arcmin'],
        ['149.9754808 1.8921707','1.3 arcmin'],
        ['149.8820271 2.4566296','1.3 arcmin'],
        ['149.7760179 2.1686125','1.3 arcmin'],
        ['150.4584999 2.2469502','1.3 arcmin'],
        ['150.3535440 1.9555194','1.3 arcmin'],
        ['149.7080556 1.9888899','1.3 arcmin'],
        ['150.2889749 1.7689125','1.3 arcmin'],
        ['150.5288733 2.4277914','1.3 arcmin'],
        ['149.9465747 2.6409537','1.3 arcmin'],
    ]

    products = None
    for i,pos in enumerate(positions):
        print(f'Querying position {i}')
        products_i = get_products(proposal_id, instrument_name=instrument_name, filters=filters, coordinates = pos[0], radius = pos[1])
        if products_i == None:
            print(f'No products found for position {i}')
            continue
        
        print(f'Found {len(products_i)} products for position {i}')

        if products == None:
            products = products_i
        else:
            products = vstack([products, products_i])

    products = unique(products, keys='productFilename')

    products = filter_products(products, filters, 
        overwrite=overwrite, data_dir=data_dir, 
        proposal_ids_to_skip=proposal_ids_to_skip)
    
    # actually download the data - to download_dir
    manifest = Observations.download_products(products, download_dir=download_dir)

    # move the downloaded data to the data folder
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
    
    files  = glob(os.path.join(download_dir,'mastDownload','JWST','jw*','*.fits'))[:]
    for file in files: 
        with fits.open(file) as hdul:
            # hdul.verify('fix')
            filt_name    = hdul[0].header['FILTER'].lower()
            final_dir = os.path.join(data_dir, filt_name)
            if not os.path.exists(final_dir):
                os.mkdir(final_dir)
            
        shutil.move(file, os.path.join(final_dir, file.split('/')[-1]))
    
    dirs = glob(os.path.join(download_dir,'mastDownload','JWST','jw*'))[:]
    for d in dirs:
        os.rmdir(d)
    os.rmdir(os.path.join(download_dir, 'mastDownload', 'JWST'))
    os.rmdir(os.path.join(download_dir, 'mastDownload'))


if __name__ == "__main__":
    proposal_id = 1213
    products = get_products(
        proposal_id,
        observation_id=None,
        instrument_name='NIRSpec/MSA',
        coordinates = None,
        radius = None,
    )
    print(products.colnames)

    products.pprint()