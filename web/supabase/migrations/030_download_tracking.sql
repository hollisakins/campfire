-- Download tracking for analytics and audit purposes
-- Tracks all downloads: individual FITS, batch FITS, ZIP exports, CSV exports, SED plots

-- Create download_log table
CREATE TABLE download_log (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  download_type TEXT NOT NULL CHECK (download_type IN ('fits_single', 'fits_object', 'fits_batch', 'fits_zip', 'csv', 'sed_plot')),
  object_count INTEGER,
  file_count INTEGER,
  object_ids TEXT[],            -- Array for "most downloaded" queries
  filter_snapshot JSONB,        -- Preserves filter context for reproducibility
  ip_address TEXT,
  user_agent TEXT,
  requested_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for common queries
CREATE INDEX idx_download_log_user_id ON download_log(user_id);
CREATE INDEX idx_download_log_requested_at ON download_log(requested_at DESC);
CREATE INDEX idx_download_log_download_type ON download_log(download_type);
CREATE INDEX idx_download_log_object_ids ON download_log USING GIN (object_ids);

-- Enable RLS
ALTER TABLE download_log ENABLE ROW LEVEL SECURITY;

-- Users can view their own download history
CREATE POLICY "Users can view own downloads"
  ON download_log FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- Only service role can insert (via server-side tracking)
-- No insert policy for authenticated users - inserts happen via service client

-- Admins can view all downloads (using user_profiles.is_admin)
CREATE POLICY "Admins can view all downloads"
  ON download_log FOR SELECT
  TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM user_profiles
      WHERE user_profiles.user_id = auth.uid()
      AND user_profiles.is_admin = true
    )
  );

-- RPC function for admin dashboard statistics
CREATE OR REPLACE FUNCTION get_download_stats(
  p_days INTEGER DEFAULT 30
)
RETURNS JSON
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
  result JSON;
  is_admin BOOLEAN;
BEGIN
  -- Check if user is admin
  SELECT COALESCE(up.is_admin, false) INTO is_admin
  FROM user_profiles up
  WHERE up.user_id = auth.uid();

  IF NOT is_admin THEN
    RAISE EXCEPTION 'Access denied: Admin privileges required';
  END IF;

  SELECT json_build_object(
    'total_downloads', (
      SELECT COUNT(*) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'unique_users', (
      SELECT COUNT(DISTINCT user_id) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'by_type', (
      SELECT json_object_agg(download_type, count)
      FROM (
        SELECT download_type, COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY download_type
      ) t
    ),
    'total_files', (
      SELECT COALESCE(SUM(file_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'total_objects', (
      SELECT COALESCE(SUM(object_count), 0) FROM download_log
      WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
    ),
    'recent_downloads', (
      SELECT json_agg(t)
      FROM (
        SELECT
          dl.id,
          dl.download_type,
          dl.object_count,
          dl.file_count,
          dl.requested_at,
          au.email,
          up.full_name
        FROM download_log dl
        LEFT JOIN auth.users au ON dl.user_id = au.id
        LEFT JOIN user_profiles up ON dl.user_id = up.user_id
        WHERE dl.requested_at >= NOW() - (p_days || ' days')::INTERVAL
        ORDER BY dl.requested_at DESC
        LIMIT 50
      ) t
    ),
    'most_downloaded_objects', (
      SELECT json_agg(t)
      FROM (
        SELECT
          object_id,
          COUNT(*) as download_count
        FROM download_log, unnest(object_ids) as object_id
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY object_id
        ORDER BY download_count DESC
        LIMIT 20
      ) t
    ),
    'downloads_by_day', (
      SELECT json_agg(t ORDER BY day)
      FROM (
        SELECT
          DATE(requested_at) as day,
          COUNT(*) as count
        FROM download_log
        WHERE requested_at >= NOW() - (p_days || ' days')::INTERVAL
        GROUP BY DATE(requested_at)
      ) t
    )
  ) INTO result;

  RETURN result;
END;
$$;

-- Grant execute permission on the function
GRANT EXECUTE ON FUNCTION get_download_stats(INTEGER) TO authenticated;
