-- Query to determine approximate maximum character lengths for table columns
-- Run this in Supabase SQL Editor to get actual data-driven column widths

SELECT
  -- Object ID column
  MAX(LENGTH(object_id)) as max_object_id_length,

  -- Field column
  MAX(LENGTH(field)) as max_field_length,

  -- RA column (formatted with 6 decimals)
  MAX(LENGTH(ra::TEXT)) as max_ra_raw_length,
  MAX(LENGTH(ROUND(ra::numeric, 6)::TEXT)) as max_ra_formatted_length,

  -- Dec column (formatted with 6 decimals)
  MAX(LENGTH(dec::TEXT)) as max_dec_raw_length,
  MAX(LENGTH(ROUND(dec::numeric, 6)::TEXT)) as max_dec_formatted_length,

  -- Redshift column (formatted with 4 decimals, can be null)
  MAX(LENGTH(ROUND(redshift::numeric, 4)::TEXT)) FILTER (WHERE redshift IS NOT NULL) as max_redshift_formatted_length,

  -- Max S/N column (formatted with 1 decimal, can be null)
  MAX(LENGTH(ROUND(max_snr::numeric, 1)::TEXT)) FILTER (WHERE max_snr IS NOT NULL) as max_snr_formatted_length,

  -- Number of gratings (integer)
  MAX(LENGTH(num_gratings::TEXT)) as max_num_gratings_length,

  -- Additional stats
  COUNT(*) as total_objects,
  COUNT(DISTINCT field) as distinct_fields

FROM objects;

-- Also get some sample long values to understand the data better
SELECT
  'Sample longest object_id' as description,
  object_id,
  LENGTH(object_id) as length
FROM objects
ORDER BY LENGTH(object_id) DESC
LIMIT 5;

SELECT
  'Sample longest field' as description,
  field,
  LENGTH(field) as length
FROM objects
ORDER BY LENGTH(field) DESC
LIMIT 5;
