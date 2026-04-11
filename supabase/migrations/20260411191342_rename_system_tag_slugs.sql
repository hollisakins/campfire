-- Rename system tag slugs to shorter abbreviations
UPDATE public.object_lists SET slug = 'o3e'   WHERE slug = 'oiii-emitter' AND is_system = true;
UPDATE public.object_lists SET slug = 'bbg'   WHERE slug = 'balmer-break-galaxy' AND is_system = true;
UPDATE public.object_lists SET slug = 'blagn' WHERE slug = 'broad-line' AND is_system = true;
UPDATE public.object_lists SET slug = 'hae'   WHERE slug = 'ha-emitter' AND is_system = true;
UPDATE public.object_lists SET slug = 'lae'   WHERE slug = 'lya-emitter' AND is_system = true;
UPDATE public.object_lists SET slug = 'qg'    WHERE slug = 'passive' AND is_system = true;
