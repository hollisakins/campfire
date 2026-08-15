"""Tests for pipeline version string resolution."""

import re
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


def _fake_git(
    *,
    tag='pipeline-v0.4.0',
    pipeline_distance='0',
    sha='7f4e2c1',
    status='',
    unavailable=(),
):
    """Build a `_run_git` mock with controllable per-command return values.

    Commands listed in *unavailable* raise ``_GitUnavailable``, simulating a
    timeout (or missing git binary) for that specific invocation (#463).
    """
    captured: list[list[str]] = []

    def fake_run_git(args, cwd, deadline=None):
        captured.append(args)
        if args[0] in unavailable:
            raise version_mod._GitUnavailable(f'git {args[0]}: simulated timeout')
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


def _git_version_with(fake_run_git, repo):
    with (
        mock.patch.object(version_mod, '_repo_root', return_value=repo),
        mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
    ):
        return version_mod._git_version()


class TestGitUnavailable:
    """Regression coverage for #463: a git timeout must never masquerade as
    an answer, and no fallback path may emit a bare release string that
    passes deploy's `_is_release_version()` check.
    """

    RELEASE_RE = re.compile(r'^\d+\.\d+\.\d+$')  # mirrors deploy.py's _RELEASE_VERSION_RE

    def _repo(self, tmp_path):
        (tmp_path / '.git').mkdir()
        return tmp_path

    def test_distance_timeout_never_emits_bare_release(self, tmp_path):
        """The f460m case: tag resolves, rev-list times out. Previously the
        distance collapsed to 0 and a clean `0.4.0` was stamped."""
        fake_run_git, _ = _fake_git(unavailable={'rev-list'})
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.4.0+g7f4e2c1.unresolved'
        assert not self.RELEASE_RE.match(result)

    def test_distance_garbage_output_is_unresolved_not_zero(self, tmp_path):
        """rev-list answering non-numeric garbage is equally not a distance of 0."""
        fake_run_git, _ = _fake_git(pipeline_distance='fatal: bad revision')
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.4.0+g7f4e2c1.unresolved'

    def test_describe_timeout_does_not_fall_into_no_tag_branch(self, tmp_path):
        """The f182m..f444w case: describe times out. Previously this was
        indistinguishable from 'no tag exists' and stamped 0.0.0.dev0+gSHA;
        now the string is additionally marked unresolved."""
        fake_run_git, _ = _fake_git(unavailable={'describe'})
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.0.0.dev0+g7f4e2c1.unresolved'

    def test_dirty_check_timeout_is_not_stamped_clean(self, tmp_path):
        """A tree of unknown dirtiness must not produce a clean release string."""
        fake_run_git, _ = _fake_git(pipeline_distance='0', unavailable={'status'})
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.4.0+g7f4e2c1.unresolved'
        assert not self.RELEASE_RE.match(result)

    def test_dirty_check_timeout_with_distance_keeps_dev_segment(self, tmp_path):
        fake_run_git, _ = _fake_git(pipeline_distance='3', unavailable={'status'})
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.4.1.dev3+g7f4e2c1.unresolved'

    def test_everything_times_out(self, tmp_path):
        fake_run_git, _ = _fake_git(
            unavailable={'describe', 'rev-list', 'rev-parse', 'status'},
        )
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.0.0.dev0+unresolved'

    def test_genuinely_no_tag_is_unchanged(self, tmp_path):
        """describe answering 'no matching tag' (nonzero exit -> None) keeps
        the historical synthetic string, with no unresolved marker."""
        fake_run_git, _ = _fake_git(tag=None)
        result = _git_version_with(fake_run_git, self._repo(tmp_path))
        assert result == '0.0.0.dev0+g7f4e2c1'

    def test_run_git_timeout_raises_git_unavailable(self, tmp_path):
        """The real _run_git must map TimeoutExpired to _GitUnavailable, not None."""
        import subprocess

        with mock.patch.object(
            version_mod.subprocess,
            'run',
            side_effect=subprocess.TimeoutExpired(cmd=['git'], timeout=1),
        ):
            try:
                version_mod._run_git(['status'], tmp_path)
            except version_mod._GitUnavailable:
                pass
            else:
                raise AssertionError('TimeoutExpired must raise _GitUnavailable')

    def test_run_git_missing_binary_raises_git_unavailable(self, tmp_path):
        with mock.patch.object(
            version_mod.subprocess, 'run', side_effect=FileNotFoundError('git'),
        ):
            try:
                version_mod._run_git(['status'], tmp_path)
            except version_mod._GitUnavailable:
                pass
            else:
                raise AssertionError('FileNotFoundError must raise _GitUnavailable')

    def test_nonzero_exit_still_returns_none(self, tmp_path):
        import subprocess

        with mock.patch.object(
            version_mod.subprocess,
            'run',
            side_effect=subprocess.CalledProcessError(128, ['git']),
        ):
            assert version_mod._run_git(['describe'], tmp_path) is None


class TestSharedDeadline:
    """All git probes of one resolution share a single wall-clock budget, so a
    hung git stalls startup for at most _GIT_TIME_BUDGET total — not once per
    probe (PR #464 review)."""

    def test_git_version_passes_one_deadline_to_every_probe(self, tmp_path):
        (tmp_path / '.git').mkdir()
        deadlines = []

        def fake_run_git(args, cwd, deadline=None):
            deadlines.append(deadline)
            if args[0] == 'describe':
                return 'pipeline-v0.4.0'
            if args[0] == 'rev-list':
                return '3'
            if args[0] == 'rev-parse':
                return '7f4e2c1'
            if args[0] == 'status':
                return ''
            return None

        with (
            mock.patch.object(version_mod, '_repo_root', return_value=tmp_path),
            mock.patch.object(version_mod, '_run_git', side_effect=fake_run_git),
        ):
            version_mod._git_version()

        assert len(deadlines) >= 3, 'expected describe/rev-list/status/rev-parse probes'
        assert all(d is not None for d in deadlines), 'every probe must get the deadline'
        assert len(set(deadlines)) == 1, f'probes must share ONE deadline: {deadlines}'

    def test_exhausted_deadline_fails_instantly_without_running_git(self, tmp_path):
        with mock.patch.object(version_mod.subprocess, 'run') as run:
            try:
                version_mod._run_git(
                    ['status'], tmp_path, deadline=version_mod.time.monotonic() - 1,
                )
            except version_mod._GitUnavailable:
                pass
            else:
                raise AssertionError('expired deadline must raise _GitUnavailable')
        run.assert_not_called()

    def test_remaining_budget_becomes_the_subprocess_timeout(self, tmp_path):
        with mock.patch.object(version_mod.subprocess, 'run') as run:
            run.return_value = mock.Mock(stdout='ok\n')
            version_mod._run_git(
                ['status'], tmp_path, deadline=version_mod.time.monotonic() + 7.0,
            )
        timeout = run.call_args.kwargs['timeout']
        assert 0 < timeout <= 7.0, f'timeout must be the remaining budget, got {timeout}'
