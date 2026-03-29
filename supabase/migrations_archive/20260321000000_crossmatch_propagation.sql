-- Cross-match inspection propagation (Issue #21)
--
-- When an object is marked Secure (quality=4), propagate that quality to
-- nearby uninspected objects (within 0.1") with matching redshift.
-- Also handles the reverse: new uninspected objects can inherit quality
-- from nearby Secure cross-matches at deploy time.

-- 1. Allow NULL user_id in audit log for system/automated actions
--    (the track_flag_changes trigger uses auth.uid() which is NULL in
--    service_role / deploy context)
ALTER TABLE flag_audit_log ALTER COLUMN user_id DROP NOT NULL;

-- 2. Cross-match propagation RPC function
CREATE OR REPLACE FUNCTION propagate_crossmatch_inspection(
    p_object_id INTEGER,
    p_radius_arcsec DOUBLE PRECISION DEFAULT 0.1,
    p_redshift_tolerance DOUBLE PRECISION DEFAULT 0.01
)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_source RECORD;
    v_radius_deg DOUBLE PRECISION;
    v_updated_count INTEGER := 0;
BEGIN
    -- Fetch the source object
    SELECT id, ra, dec, redshift, redshift_auto, redshift_quality
    INTO v_source
    FROM objects
    WHERE id = p_object_id;

    IF NOT FOUND THEN
        RETURN 0;
    END IF;

    v_radius_deg := p_radius_arcsec / 3600.0;

    IF v_source.redshift_quality = 4 AND v_source.redshift IS NOT NULL THEN
        -- Forward propagation: source is Secure, propagate to nearby uninspected.
        -- Compare source's effective redshift (inspector-corrected or auto-fit)
        -- against target's auto-fit redshift.
        WITH matches AS (
            SELECT o.id
            FROM objects o
            WHERE o.id != v_source.id
              AND o.redshift_quality = 0
              AND o.redshift_auto IS NOT NULL
              AND ABS(o.redshift_auto - v_source.redshift) < p_redshift_tolerance
              -- Bounding box pre-filter
              AND o.ra BETWEEN (v_source.ra - v_radius_deg) AND (v_source.ra + v_radius_deg)
              AND o.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
              -- Haversine post-filter
              AND (2 * DEGREES(ASIN(SQRT(
                  POWER(SIN(RADIANS(o.dec - v_source.dec) / 2), 2) +
                  COS(RADIANS(v_source.dec)) * COS(RADIANS(o.dec)) *
                  POWER(SIN(RADIANS(o.ra - v_source.ra) / 2), 2)
              )))) <= v_radius_deg
        )
        UPDATE objects
        SET redshift_quality = 4,
            last_inspected_at = NOW()
        FROM matches
        WHERE objects.id = matches.id;

        GET DIAGNOSTICS v_updated_count = ROW_COUNT;

    ELSIF v_source.redshift_quality = 0 AND v_source.redshift_auto IS NOT NULL THEN
        -- Reverse propagation: source is uninspected, check if a nearby
        -- Secure object validates it. Compare source's auto-fit against
        -- nearby object's effective (inspected) redshift.
        PERFORM 1
        FROM objects o
        WHERE o.id != v_source.id
          AND o.redshift_quality = 4
          AND o.redshift IS NOT NULL
          AND ABS(v_source.redshift_auto - o.redshift) < p_redshift_tolerance
          -- Bounding box pre-filter
          AND o.ra BETWEEN (v_source.ra - v_radius_deg) AND (v_source.ra + v_radius_deg)
          AND o.dec BETWEEN (v_source.dec - v_radius_deg) AND (v_source.dec + v_radius_deg)
          -- Haversine post-filter
          AND (2 * DEGREES(ASIN(SQRT(
              POWER(SIN(RADIANS(o.dec - v_source.dec) / 2), 2) +
              COS(RADIANS(v_source.dec)) * COS(RADIANS(o.dec)) *
              POWER(SIN(RADIANS(o.ra - v_source.ra) / 2), 2)
          )))) <= v_radius_deg
        LIMIT 1;

        IF FOUND THEN
            UPDATE objects
            SET redshift_quality = 4,
                last_inspected_at = NOW()
            WHERE id = v_source.id;

            v_updated_count := 1;
        END IF;
    END IF;

    RETURN v_updated_count;
END;
$$;

-- Grant execute to roles that need it
GRANT EXECUTE ON FUNCTION propagate_crossmatch_inspection(INTEGER, DOUBLE PRECISION, DOUBLE PRECISION) TO authenticated;
GRANT EXECUTE ON FUNCTION propagate_crossmatch_inspection(INTEGER, DOUBLE PRECISION, DOUBLE PRECISION) TO service_role;
