"""Tests for pipeline version string resolution."""

from unittest import mock

from campfire_pipeline.common import version as version_mod


class TestDescribeToPep440:
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


class TestGitVersion:
    """Regression tests for #135 — pipeline-scoped dirty check.

    The bug: `git describe --dirty` checks the entire working tree, so edits
    to web/, python/, supabase/ flipped the pipeline version's dirty flag
    even when no pipeline file changed.
    """

    def test_describe_invocation_has_no_dirty_flag(self, tmp_path):
        """`git describe --dirty` must not be used (whole-tree dirty leak)."""
        repo = tmp_path
        (repo / '.git').mkdir()

        captured: list[list[str]] = []

        def fake_run_git(args, cwd):
            captured.append(args)
            if args[0] == 'describe':
                return 'pipeline-v0.4.0-0-g7f4e2c1'
            if args[0] == 'status':
                return ''  # clean
            return None

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

    def test_dirty_check_is_pipeline_scoped(self, tmp_path):
        """Dirty check must pass `-- pipeline` to git status."""
        repo = tmp_path
        (repo / '.git').mkdir()

        captured: list[list[str]] = []

        def fake_run_git(args, cwd):
            captured.append(args)
            if args[0] == 'describe':
                return 'pipeline-v0.4.0-0-g7f4e2c1'
            if args[0] == 'status':
                return ''  # clean
            return None

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

    def test_pipeline_dirty_appends_local_segment(self, tmp_path):
        """When pipeline files are dirty, the local segment fires."""
        repo = tmp_path
        (repo / '.git').mkdir()

        def fake_run_git(args, cwd):
            if args[0] == 'describe':
                return 'pipeline-v0.4.0-0-g7f4e2c1'
            if args[0] == 'status':
                return ' M pipeline/campfire_pipeline/common/version.py'
            return None

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=repo),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
            mock.patch.object(version_mod, '_today_local_segment', return_value='d20260504'),
        ):
            assert version_mod._git_version() == '0.4.0+d20260504'
