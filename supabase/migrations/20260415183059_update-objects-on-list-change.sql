set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.log_list_membership_change()
 RETURNS trigger
 LANGUAGE plpgsql
 SECURITY DEFINER
AS $function$
DECLARE
    v_object_id INTEGER;
BEGIN
    IF TG_OP = 'INSERT' THEN
        v_object_id := NEW.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (NEW.list_id, NEW.object_id, auth.uid(), 'add', NEW.ra, NEW.dec);
    ELSIF TG_OP = 'DELETE' THEN
        v_object_id := OLD.object_id;
        INSERT INTO list_audit_log (list_id, object_id, user_id, action, ra, dec)
        VALUES (OLD.list_id, OLD.object_id, auth.uid(), 'remove', OLD.ra, OLD.dec);
    END IF;

    -- Bump objects.updated_at so incremental sync picks up tag changes
    UPDATE objects SET updated_at = NOW() WHERE id = v_object_id;

    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$function$
;


