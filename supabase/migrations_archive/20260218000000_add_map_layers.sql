-- Map tile layers for the NIRCam image map viewer
-- Stores metadata about available tile pyramids served from R2

-- 1. Create table
CREATE TABLE IF NOT EXISTS public.map_layers (
    id serial PRIMARY KEY,
    field text NOT NULL,
    filter text NOT NULL,

    -- Tile serving
    tile_base_url text NOT NULL,       -- e.g., "https://tiles.example.com/cosmos/f444w"
    min_zoom integer NOT NULL,
    max_zoom integer NOT NULL,
    tile_size integer NOT NULL DEFAULT 256,

    -- Spatial bounds (degrees)
    ra_min double precision NOT NULL,
    ra_max double precision NOT NULL,
    dec_min double precision NOT NULL,
    dec_max double precision NOT NULL,

    -- WCS parameters for pixel <-> sky coordinate conversion
    -- Output is always North-up so CD matrix is diagonal (CD1_2 = CD2_1 = 0)
    -- Keys: crpix1, crpix2, crval1, crval2, cd1_1, cd2_2, naxis1, naxis2
    wcs_params jsonb NOT NULL,

    -- Image dimensions at max zoom (pixels)
    image_width integer NOT NULL,
    image_height integer NOT NULL,

    -- Metadata
    total_tiles integer,
    total_size_bytes bigint,
    is_default boolean DEFAULT false,   -- default layer for this field
    created_at timestamptz DEFAULT now(),

    -- One layer per field/filter combination
    UNIQUE (field, filter)
);

-- 2. Indexes
CREATE INDEX idx_map_layers_field ON public.map_layers(field);

-- 3. RLS
ALTER TABLE public.map_layers ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Authenticated users can read map layers"
    ON public.map_layers
    FOR SELECT
    TO authenticated
    USING (true);

CREATE POLICY "Service role has full access to map layers"
    ON public.map_layers
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- 4. Grants
GRANT SELECT ON public.map_layers TO authenticated;
GRANT ALL ON public.map_layers TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.map_layers_id_seq TO service_role;

-- 5. Viewport object query for map markers
-- Returns lightweight data for rendering markers within a bounding box
CREATE OR REPLACE FUNCTION public.get_objects_in_viewport(
    p_ra_min double precision,
    p_ra_max double precision,
    p_dec_min double precision,
    p_dec_max double precision,
    p_field text DEFAULT NULL,
    p_limit integer DEFAULT 5000
)
RETURNS TABLE (
    "object_id" text,
    "ra" double precision,
    "dec" double precision,
    "redshift" double precision,
    "redshift_quality" integer,
    "field" text,
    "program_id" integer
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
AS $$
BEGIN
    RETURN QUERY
    SELECT
        o.object_id,
        o.ra,
        o.dec,
        o.redshift::double precision,
        o.redshift_quality,
        o.field,
        o.program_id
    FROM public.objects o
    WHERE
        o.ra BETWEEN p_ra_min AND p_ra_max
        AND o.dec BETWEEN p_dec_min AND p_dec_max
        AND (p_field IS NULL OR o.field = p_field)
    ORDER BY o.ra
    LIMIT p_limit;
END;
$$;

GRANT EXECUTE ON FUNCTION public.get_objects_in_viewport TO authenticated;
