# Implementing NIRCam images into CAMPFIRE

Plan to implement NIRCam image downloading on CAMPFIRE. 

## NIRCam Supabase table

Already exists 

Table: nircam_images

Columns: 
- field (text)
- tile (text)
- filter (text)
- pixel_scale (float)
- version (text)
- extension (text)
- file_path (text)

## Script to upsert entries to NIRCam images table

NIRCam data is stored on the CANDIDE cluster, so I want a deployment script there that upserts to supabase directly. We'll need to discover files and parse field, tile, filter, etc from the file names. 

```
╰─❯ ls data/nircam/*/

data/nircam/cosmos/:
 mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_err.fits.gz
 mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_rms.fits.gz
 mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_sci.fits.gz
 mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_srcmask.fits.gz
```

e.g., 
data/nircam/{field}/mosaic_nircam_{filter}_{field}_{pixel_scale}_{version}_{tile}_{extension}.fits.gz


File paths should be prepended with the base URL for NIRCam data access, which is through BunnyCDN and points to the CANDIDE webserver. 

https://hollisakins-candide.b-cdn.net/data/nircam/

## Frontend

The NIRCam data page needs to have a similar table as the NIRCam table, with filterable/sortable columns. However, since theres a lot fewer rows, we don't need to have any fancy server-side sorting or anything. 

The important new feature we'll need is a button to generate a curl script to download the filtered files. 
Take a look at @web/old/nircam_cosmos.html for an example. 
This should pull from the CDN and use the following username/password: "ember" / "ember!jwst"
