-- Check if the RPC function has the coordinate search parameters
SELECT
    p.proname as function_name,
    pg_get_function_arguments(p.oid) as arguments
FROM pg_proc p
JOIN pg_namespace n ON p.pronamespace = n.oid
WHERE p.proname = 'get_filtered_objects_paginated'
AND n.nspname = 'public';
