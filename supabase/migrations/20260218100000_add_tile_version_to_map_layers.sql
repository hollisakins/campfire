-- Add tile_version column for cache busting when tiles are re-uploaded
ALTER TABLE public.map_layers
    ADD COLUMN tile_version integer NOT NULL DEFAULT 1;

-- RPC to bump tile_version after re-uploading tiles
CREATE OR REPLACE FUNCTION public.increment_tile_version(
    p_field text,
    p_filter text
)
RETURNS void
LANGUAGE sql
AS $$
    UPDATE public.map_layers
    SET tile_version = tile_version + 1
    WHERE field = p_field AND filter = p_filter;
$$;

GRANT EXECUTE ON FUNCTION public.increment_tile_version TO service_role;
