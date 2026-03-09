"""
Supabase database operations.

Handles upserts for programs, objects, and spectra tables, plus
slit geometry deployment and filter cache refresh.
"""

from supabase import create_client, Client


def get_supabase_client(config: dict) -> Client:
    """Create a Supabase client from deploy config."""
    return create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key'],
    )


def check_existing_objects(client: Client, object_ids: list[str]) -> set[str]:
    """Return the subset of object_ids that already exist in the database."""
    if not object_ids:
        return set()

    existing = set()
    batch_size = 500
    for i in range(0, len(object_ids), batch_size):
        batch = object_ids[i:i + batch_size]
        resp = client.table('objects').select('object_id').in_('object_id', batch).execute()
        existing.update(row['object_id'] for row in resp.data)
    return existing


def upsert_programs(
    client: Client,
    program_ids: list[int],
    programs_config: dict[int, dict],
) -> None:
    """Upsert program records."""
    for pid in program_ids:
        info = programs_config.get(pid, {})
        data = {
            'program_id': pid,
            'program_name': info.get('program_name', f'Program {pid}'),
            'pi_name': info.get('pi_name', ''),
            'description': info.get('description', ''),
            'is_public': info.get('is_public', False),
        }
        client.table('programs').upsert(data, on_conflict='program_id').execute()
        print(f"  + {pid} ({data['program_name']})")


def batch_upsert_objects(
    client: Client,
    objects: list[dict],
    field: str,
    force_overwrite: bool,
    objects_with_sed: set[str] | None = None,
    batch_size: int = 500,
) -> int:
    """
    Upsert objects in batches.

    Three branches:
      - New objects: full insert with defaults
      - Existing (normal): update pipeline fields only, preserve inspection data
      - Existing (force_overwrite): reset all fields including inspection data

    Args:
        objects: List of dicts from summary.get_unique_objects()
        field: Field name
        force_overwrite: Whether to reset inspection data
        objects_with_sed: Set of object_ids that have SED plots
        batch_size: Records per batch

    Returns:
        Number of objects upserted
    """
    if not objects:
        return 0
    if objects_with_sed is None:
        objects_with_sed = set()

    object_ids = [o['object_id'] for o in objects]
    existing = check_existing_objects(client, object_ids)

    new_records = []
    update_records = []

    for obj in objects:
        oid = obj['object_id']
        is_existing = oid in existing
        has_sed = oid in objects_with_sed

        if is_existing and not force_overwrite:
            # Update pipeline fields only
            data = {
                'object_id': oid,
                'program_id': obj['program_id'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
            }
            update_records.append(data)
        elif is_existing and force_overwrite:
            # Reset everything
            data = {
                'object_id': oid,
                'program_id': obj['program_id'],
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
            # New object
            data = {
                'object_id': oid,
                'program_id': obj['program_id'],
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
        client.table('objects').insert(batch).execute()

    # Batch upsert updates
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('objects').upsert(batch, on_conflict='object_id').execute()

    return len(objects)


def batch_upsert_spectra(
    client: Client,
    spectra: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Upsert spectra in batches, keyed on (object_id, grating).

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
    object_ids = list(set(r['object_id'] for r in spectra))
    existing_map = {}  # (object_id, grating) -> id

    for i in range(0, len(object_ids), batch_size):
        batch_ids = object_ids[i:i + batch_size]
        resp = client.table('spectra').select('id,object_id,grating').in_('object_id', batch_ids).execute()
        for row in resp.data:
            existing_map[(row['object_id'], row['grating'])] = row['id']

    new_records = []
    update_records = []

    for record in spectra:
        key = (record['object_id'], record['grating'])
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


def update_has_sed_plot(
    client: Client,
    object_ids: set[str],
    batch_size: int = 500,
) -> int:
    """Set has_sed_plot = true for the given object IDs."""
    if not object_ids:
        return 0

    id_list = list(object_ids)
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i:i + batch_size]
        client.table('objects').update({'has_sed_plot': True}).in_('object_id', batch).execute()

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
