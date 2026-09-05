-- Perf T2-D2 (#508): chi2_min / confidence (added by 20260904050000) join the
-- column list bump_spectra_updated_at_trigger watches, so a deploy or
-- scripts/backfill_zfit_scalars.py writing only the scalars moves
-- spectra.updated_at and incremental sync (p_updated_since) picks the row up.
--
-- Hand-authored: no local Docker for `supabase db diff`. Trigger definition
-- copied verbatim from supabase/schemas/triggers.sql (the source of truth).

DROP TRIGGER IF EXISTS bump_spectra_updated_at_trigger ON public.spectra;
CREATE TRIGGER bump_spectra_updated_at_trigger
  BEFORE UPDATE OF
    dq_flags,
    redshift_auto,
    signal_to_noise,
    thumbnail_svg_fnu,
    thumbnail_svg_flambda,
    fits_path,
    file_hash,
    -- zfit scalars on the row (perf T2-D2, #508): written by deploy and the
    -- backfill; a sync client showing the fit summary must see them change.
    chi2_min,
    confidence
  ON public.spectra
  FOR EACH ROW EXECUTE FUNCTION public.bump_spectra_updated_at();
