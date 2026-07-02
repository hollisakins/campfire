-- epic #261, N3 / D6 — retire CANDIDE: hide the dead CANDIDE-pointing mosaics.
--
-- Before this epic, nircam_images.file_path held a CANDIDE-relative path
-- (<field>/<filename>); the public page prepended a hardcoded CANDIDE base URL +
-- shipped plaintext credentials. NIRCam now serves from OSN, where file_path is
-- the canonical storage key (starts 'data/'). CANDIDE is retired, so the legacy
-- rows point at a dead host — revoke them (D6): they go dark per (field, filter)
-- until that field is re-reduced and re-deployed fresh, which republishes the slot
-- with its OSN key (the mosaic deploy upserts on the same
-- (field,tile,filter,pixel_scale,extension) slot).
--
-- Revoke rather than delete so the change is reversible and a re-deploy simply
-- flips the slot back to published. No-op on a fresh DB (the seed has no
-- nircam_images rows) and on any DB already on OSN keys.

UPDATE public.nircam_images
   SET deploy_status = 'revoked'
 WHERE file_path NOT LIKE 'data/%'
   AND deploy_status <> 'revoked';
