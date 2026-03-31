"""
Supabase database operations.

Handles upserts for programs, targets, and spectra tables, plus
slit geometry deployment and filter cache refresh.
"""

from supabase import create_client, Client


def get_supabase_client(config: dict) -> Client:
    """Create a Supabase client from deploy config."""
    return create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key'],
    )


REDSHIFT_DRIFT_THRESHOLD = 0.03


def check_existing_targets(client: Client, target_ids: list[str]) -> dict[str, dict]:
    """
    Return existing targets as a dict keyed by target_id.

    Each value contains inspection-relevant fields needed for the
    redshift drift check during upsert.
    """
    if not target_ids:
        return {}

    fields = 'target_id, redshift_auto, redshift_inspected, redshift_quality'
    existing = {}
    batch_size = 500
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = client.table('targets').select(fields).in_('target_id', batch).execute()
        for row in resp.data:
            existing[row['target_id']] = row
    return existing


def upsert_programs(
    client: Client,
    program_slugs: list[str],
    programs_config: dict[str, dict],
) -> None:
    """Upsert program records."""
    for slug in program_slugs:
        info = programs_config.get(slug, {})
        data = {
            'slug': slug,
            'program_name': info.get('program_name', slug),
            'pi_name': info.get('pi_name', ''),
            'description': info.get('description', ''),
            'is_public': info.get('is_public', False),
            'cycle': info.get('cycle'),
        }
        client.table('programs').upsert(data, on_conflict='slug').execute()
        print(f"  + {slug} ({data['program_name']})")


def upsert_observation(
    client: Client,
    obs_name: str,
    program_slug: str,
    jwst_program_id: int,
    field: str,
) -> None:
    """Upsert an observation record."""
    data = {
        'name': obs_name,
        'program_slug': program_slug,
        'jwst_program_id': jwst_program_id,
        'field': field,
    }
    client.table('observations').upsert(data, on_conflict='name').execute()


def batch_upsert_targets(
    client: Client,
    objects: list[dict],
    field: str,
    force_overwrite: bool,
    objects_with_sed: set[str] | None = None,
    batch_size: int = 500,
) -> int:
    """
    Upsert targets in batches.

    Three branches:
      - New targets: full insert with defaults
      - Existing (normal): update pipeline fields only, preserve inspection data
      - Existing (force_overwrite): reset all fields including inspection data

    Args:
        objects: List of dicts from summary.get_unique_targets()
        field: Field name
        force_overwrite: Whether to reset inspection data
        objects_with_sed: Set of target_ids that have SED plots
        batch_size: Records per batch

    Returns:
        Tuple of (n_upserted, new_target_ids, n_quality_reset)
    """
    if not objects:
        return 0, []
    if objects_with_sed is None:
        objects_with_sed = set()

    target_ids = [o['object_id'] for o in objects]
    existing = check_existing_targets(client, target_ids)

    new_records = []
    update_records = []
    n_quality_reset = 0

    for obj in objects:
        oid = obj['object_id']
        is_existing = oid in existing
        has_sed = oid in objects_with_sed

        if is_existing and not force_overwrite:
            # Update pipeline fields only
            data = {
                'target_id': oid,
                'program_slug': obj['program_slug'],
                'observation': obj['observation'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
            }

            # Reset Secure quality if redshift_auto drifted and there's
            # no manual override anchoring the redshift
            old = existing[oid]
            if (
                old['redshift_quality'] == 4
                and old['redshift_inspected'] is None
                and old['redshift_auto'] is not None
                and obj['redshift_best'] is not None
                and abs(float(old['redshift_auto']) - float(obj['redshift_best'])) > REDSHIFT_DRIFT_THRESHOLD
            ):
                data['redshift_quality'] = 0
                data['last_inspected_at'] = None
                data['last_inspected_by'] = None
                n_quality_reset += 1
                print(f"    quality reset: {oid} z_auto {old['redshift_auto']:.4f} → {obj['redshift_best']:.4f}")

            update_records.append(data)
        elif is_existing and force_overwrite:
            # Reset everything
            data = {
                'target_id': oid,
                'program_slug': obj['program_slug'],
                'observation': obj['observation'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,
                'object_flags': 0,
                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
            update_records.append(data)
        else:
            # New target
            data = {
                'target_id': oid,
                'program_slug': obj['program_slug'],
                'observation': obj['observation'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,
                'object_flags': 0,
                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
            new_records.append(data)

    # Batch insert new records
    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('targets').insert(batch).execute()

    # Batch upsert updates
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('targets').upsert(batch, on_conflict='target_id').execute()

    new_ids = [r['target_id'] for r in new_records]
    return len(objects), new_ids, n_quality_reset


def batch_upsert_spectra(
    client: Client,
    spectra: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Upsert spectra in batches, keyed on (target_id, grating).

    Args:
        spectra: List of dicts from summary.get_spectra_records(), optionally
                 enriched with thumbnail_svg_fnu / thumbnail_svg_flambda.
        batch_size: Records per batch

    Returns:
        Number of spectra upserted
    """
    if not spectra:
        return 0

    # Fetch existing spectra to split insert vs update
    target_ids = list(set(r['target_id'] for r in spectra))
    existing_map = {}  # (target_id, grating) -> id

    for i in range(0, len(target_ids), batch_size):
        batch_ids = target_ids[i:i + batch_size]
        resp = client.table('spectra').select('id,target_id,grating').in_('target_id', batch_ids).execute()
        for row in resp.data:
            existing_map[(row['target_id'], row['grating'])] = row['id']

    new_records = []
    update_records = []

    for record in spectra:
        key = (record['target_id'], record['grating'])
        if key in existing_map:
            update_records.append({**record, 'id': existing_map[key]})
        else:
            new_records.append(record)

    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('spectra').insert(batch).execute()

    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('spectra').upsert(batch, on_conflict='id').execute()

    return len(spectra)


def propagate_crossmatches(
    client: Client,
    target_ids: list[str],
    batch_size: int = 500,
) -> int:
    """
    Check new targets against existing inspected cross-matches.

    For each new target (quality=0), calls the DB function to check if
    a nearby Secure (quality=4) target with matching redshift exists.
    If so, the new target is automatically marked Secure.

    Args:
        target_ids: String target_ids of newly inserted targets
        batch_size: Records per batch for ID lookups

    Returns:
        Number of targets auto-secured
    """
    if not target_ids:
        return 0

    # Batch-fetch integer IDs for the new targets
    id_map: dict[str, int] = {}
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = client.table('targets').select('id, target_id').in_('target_id', batch).execute()
        for row in resp.data:
            id_map[row['target_id']] = row['id']

    total = 0
    for oid in target_ids:
        db_id = id_map.get(oid)
        if db_id is None:
            continue
        try:
            result = client.rpc('propagate_crossmatch_inspection', {
                'p_target_id': db_id,
            }).execute()
            total += result.data or 0
        except Exception as e:
            print(f"  Warning: cross-match check failed for {oid}: {e}")

    return total


def update_has_sed_plot(
    client: Client,
    target_ids: set[str],
    batch_size: int = 500,
) -> int:
    """Set has_sed_plot = true for the given target IDs."""
    if not target_ids:
        return 0

    id_list = list(target_ids)
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i:i + batch_size]
        client.table('targets').update({'has_sed_plot': True}).in_('target_id', batch).execute()

    return len(id_list)


def deploy_slits(
    client: Client,
    obs_name: str,
    slits_data: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Deploy slit geometry: delete existing rows for the observation,
    then bulk insert new rows.
    """
    client.table('slit_regions').delete().eq('observation', obs_name).execute()

    total = 0
    for i in range(0, len(slits_data), batch_size):
        batch = slits_data[i:i + batch_size]
        client.table('slit_regions').insert(batch).execute()
        total += len(batch)

    return total


def deploy_shutters(
    client: Client,
    obs_name: str,
    shutters_data: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Deploy shutters: delete existing rows for the observation,
    then bulk insert new rows.
    """
    client.table('shutters').delete().eq('observation', obs_name).execute()

    total = 0
    for i in range(0, len(shutters_data), batch_size):
        batch = shutters_data[i:i + batch_size]
        client.table('shutters').insert(batch).execute()
        total += len(batch)

    return total


def refresh_filter_options(client: Client) -> None:
    """Refresh the filter options materialized view."""
    print("  Refreshing filter options cache...")
    try:
        client.rpc('refresh_filter_options').execute()
        print("  Done")
    except Exception as e:
        print(f"  Warning: Failed to refresh filter options: {e}")
        print("  Run manually: SELECT refresh_filter_options();")


def refresh_programs_overview(client: Client) -> None:
    """Refresh the programs overview materialized view."""
    print("  Refreshing programs overview cache...")
    try:
        client.rpc('refresh_programs_overview').execute()
        print("  Done")
    except Exception as e:
        print(f"  Warning: Failed to refresh programs overview: {e}")
        print("  Run manually: SELECT refresh_programs_overview();")
