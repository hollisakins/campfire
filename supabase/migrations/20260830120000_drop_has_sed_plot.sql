-- Drop targets.has_sed_plot (A2 cleanup, issue #371).
--
-- The column flagged the presence of a static SED-plot PDF in R2 so the web
-- could skip a runtime HeadObject. RGB/SED static cutouts were fully
-- deprecated in #364 (superseded by the on-the-fly /api/v1/cutout API):
-- nothing on the web renders the flag, deploy no longer generates or uploads
-- SED plots, and the deploy-side writers (update_has_sed_plot, the
-- batch_upsert_objects objects_with_sed path) are deleted alongside this
-- migration. Hand-authored: a simple column drop, same shape migra would emit.

DROP INDEX IF EXISTS "public"."idx_targets_has_sed_plot";

ALTER TABLE "public"."targets" DROP COLUMN IF EXISTS "has_sed_plot";
