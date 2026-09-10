-- Hand-authored (comment-only; column comments are not tracked by `supabase db diff`).
-- Copied verbatim from supabase/schemas/tables.sql, which remains the source of truth.
--
-- linefit LFITVER 2 (pipeline PR "doublet totals"): the `lines` jsonb now also
-- carries doublet totals (component = 'doublet', e.g. CIII1908 / OII3727 /
-- SII6725) with a `members` field and the new flag bit 2048 (resolved).
-- No data or structural change.

COMMENT ON COLUMN "public"."spectrum_line_fits"."lines" IS 'Per-line records keyed by catalog name (components such as Halpha / CIII1907, accepted broad components as <line>_broad, and doublet totals such as CIII1908 / OII3727 / SII6725): {label, component (narrow|broad|doublet), wave_rest [vacuum A], wave_obs [um], flux, flux_err [erg/s/cm2], snr, ew_rest, ew_rest_err [A], cont, cont_err [erg/s/cm2/A], dv, dv_err, sigma_v, sigma_v_err, sigma_lsf_kms [km/s], complex, chi2, dof, npix, flags (bitmask: 1 tied, 2 blended, 4 blend, 8 edge, 16 kin_global, 32 kin_default, 64 broad, 128 no_continuum, 256 fit_failed, 512 masked, 1024 sigma_unresolved, 2048 resolved), blend_into, tied_to, members (doublet totals only: the two component names)}. NaN is null; a `blended` line carries no flux of its own (its blend primary reports the total). A doublet total is the covariance-propagated sum of its components where the grating resolves them (`resolved` set) and the single blended measurement where it does not, so it means the same thing in every grating: select on totals, not components. fit_version >= 2.';
