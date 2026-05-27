"""Tests for pipeline version string resolution."""

from unittest import mock

from campfire_pipeline.common import version as version_mod


class TestDescribeToPep440:
    """Pure parser tests — exercised independently of the runtime path."""

    def test_exact_tag_clean(self):
        assert version_mod._describe_to_pep440('pipeline-v0.4.0', dirty=False) == '0.4.0'

    def test_exact_tag_dirty(self):
        with mock.patch.object(version_mod, '_today_local_segment', return_value='d20260504'):
            assert (
                version_mod._describe_to_pep440('pipeline-v0.4.0', dirty=True)
                == '0.4.0+d20260504'
            )

    def test_post_tag_clean(self):
        assert (
            version_mod._describe_to_pep440('pipeline-v0.4.0-3-g7f4e2c1', dirty=False)
            == '0.4.1.dev3+g7f4e2c1'
        )

    def test_post_tag_dirty(self):
        with mock.patch.object(version_mod, '_today_local_segment', return_value='d20260504'):
            assert (
                version_mod._describe_to_pep440('pipeline-v0.4.0-3-g7f4e2c1', dirty=True)
                == '0.4.1.dev3+g7f4e2c1.d20260504'
            )

    def test_unparseable_returns_none(self):
        assert version_mod._describe_to_pep440('not-a-pipeline-tag', dirty=False) is None


def _fake_git(*, tag='pipeline-v0.4.0', pipeline_distance='0', sha='7f4e2c1', status=''):
    """Build a `_run_git` mock with controllable per-command return values."""
    captured: list[list[str]] = []

    def fake_run_git(args, cwd):
        captured.append(args)
        if args[0] == 'describe':
            return tag
        if args[0] == 'rev-list':
            return pipeline_distance
        if args[0] == 'rev-parse':
            return sha
        if args[0] == 'status':
            return status
        return None

    return fake_run_git, captured


class TestGitVersion:
    """Regression coverage for #135 (dirty-scope) and the pipeline-scoped
    distance fix that supersedes ``git describe --long``'s monorepo-wide
    count.
    """

    def test_describe_invocation_has_no_dirty_flag(self, tmp_path):
        """`git describe --dirty` must not be used (whole-tree dirty leak)."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, captured = _fake_git()

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            result = version_mod._git_version()

        describe_calls = [c for c in captured if c[0] == 'describe']
        assert describe_calls, 'expected at least one git describe call'
        for call in describe_calls:
            assert '--dirty' not in call, f'`--dirty` leaks whole-tree state: {call}'

        assert result == '0.4.0'

    def test_describe_uses_abbrev_zero_not_long(self, tmp_path):
        """`describe --long` count is monorepo-wide; we must use `--abbrev=0`."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, captured = _fake_git()

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            version_mod._git_version()

        describe_calls = [c for c in captured if c[0] == 'describe']
        assert describe_calls, 'expected a git describe call'
        for call in describe_calls:
            assert '--long' not in call, (
                f'`--long` would expose monorepo-wide distance count: {call}'
            )
            assert '--abbrev=0' in call, (
                f'expected `--abbrev=0` to suppress distance/sha suffix: {call}'
            )

    def test_dirty_check_is_pipeline_scoped(self, tmp_path):
        """Dirty check must pass `-- pipeline` to git status."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, captured = _fake_git()

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            version_mod._git_version()

        status_calls = [c for c in captured if c[0] == 'status']
        assert status_calls, 'expected a git status call to check dirtiness'
        for call in status_calls:
            assert '--' in call and 'pipeline' in call, (
                f'status call must be scoped to pipeline/: {call}'
            )

    def test_distance_is_pipeline_scoped(self, tmp_path):
        """`rev-list -- pipeline` must produce the distance, not `describe --long`."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, captured = _fake_git()

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            version_mod._git_version()

        rev_list_calls = [c for c in captured if c[0] == 'rev-list']
        assert rev_list_calls, 'expected a `git rev-list` call for pipeline-scoped distance'
        for call in rev_list_calls:
            assert '--count' in call, f'rev-list must request --count: {call}'
            assert '--' in call and 'pipeline' in call, (
                f'rev-list must be scoped to pipeline/: {call}'
            )
            assert 'pipeline-v0.4.0..HEAD' in call, (
                f'rev-list must use <tag>..HEAD range: {call}'
            )

    def test_zero_pipeline_commits_returns_clean_tag(self, tmp_path):
        """0 pipeline commits past the tag -> exact tag version, no .dev / +g."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, _ = _fake_git(pipeline_distance='0', status='')

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            assert version_mod._git_version() == '0.4.0'

    def test_zero_pipeline_commits_even_when_describe_long_would_count_them(self, tmp_path):
        """The core fix: web/python/scripts commits don't bump the dev counter.

        Simulates a HEAD that's many commits past the tag but where every commit
        landed outside pipeline/ (rev-list -- pipeline returns 0).
        """
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, _ = _fake_git(pipeline_distance='0', sha='ff936d6', status='')

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            result = version_mod._git_version()

        assert result == '0.4.0', (
            f'commits outside pipeline/ must not bump the version; got {result!r}'
        )

    def test_pipeline_commits_bump_dev_and_include_sha(self, tmp_path):
        """Non-zero pipeline-scoped distance bumps patch and appends .devN+gSHA."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, _ = _fake_git(pipeline_distance='3', sha='7f4e2c1', status='')

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            assert version_mod._git_version() == '0.4.1.dev3+g7f4e2c1'

    def test_pipeline_dirty_appends_local_segment(self, tmp_path):
        """When pipeline files are dirty, the local segment fires."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, _ = _fake_git(
            pipeline_distance='0',
            sha='7f4e2c1',
            status=' M pipeline/campfire_pipeline/common/version.py',
        )

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
            mock.patch.object(version_mod, '_today_local_segment', return_value='d20260504'),
        ):
            assert version_mod._git_version() == '0.4.0+d20260504'

    def test_pipeline_dirty_with_pipeline_commits(self, tmp_path):
        """Dirty + non-zero pipeline distance combines both local segments."""
        repo = tmp_path
        (repo / '.git').mkdir()

        fake_run_git, _ = _fake_git(
            pipeline_distance='3',
            sha='7f4e2c1',
            status=' M pipeline/foo.py',
        )

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
            mock.patch.object(version_mod, '_today_local_segment', return_value='d20260504'),
        ):
            assert version_mod._git_version() == '0.4.1.dev3+g7f4e2c1.d20260504'
