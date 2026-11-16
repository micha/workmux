"""Generic container discovery, cached checks, and flat workspace sidebar children."""

from concurrent.futures import Future
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from workmux.checkouts import (CheckoutCache, POLL_INTERVAL, checkout_root, contains,
                              discover_checkouts, worktree_paths)
from workmux.core import Tab, Workspace
from workmux.git_status import GitStatusCache
from workmux.sidebar import sidebar_rows


class CheckoutTests(unittest.TestCase):
    def test_porcelain_paths_are_opaque(self):
        output = (b'worktree /main space\0HEAD abc\0branch refs/heads/main\0\0'
                  b'worktree /linked\nbranch fake\0HEAD def\0detached\0\0')
        self.assertEqual(worktree_paths(output), ('/main space', '/linked\nbranch fake'))

    def test_containment_is_component_wise(self):
        self.assertTrue(contains('/job', '/job/worktrees/owner/repo'))
        self.assertTrue(contains('/job', '/job'))
        self.assertFalse(contains('/job', '/job', strict=True))
        self.assertFalse(contains('/job', '/job-other/repo'))

    def test_discovery_query_is_local_and_optional(self):
        with patch('workmux.checkouts.subprocess.run', return_value=Mock(returncode=0, stdout=b'/repo\n')) as run:
            self.assertEqual(checkout_root('/repo'), '/repo')
        self.assertEqual(run.call_args.args[0],
                         ['git', '--no-optional-locks', '-C', '/repo', 'rev-parse', '--show-toplevel'])
        for error in (FileNotFoundError(), subprocess.TimeoutExpired('git', 2)):
            with patch('workmux.checkouts.subprocess.run', side_effect=error):
                self.assertIsNone(checkout_root('/repo'))

    def test_cache_nonblocking_deduplicated_and_removed_paths_cancelled(self):
        first, second, third = Future(), Future(), Future()
        workspaces = [Workspace('$1', 'one', '', '/job'), Workspace('$2', 'two', '', '/job')]
        with patch('workmux.checkouts.ThreadPoolExecutor') as executor, \
                patch('workmux.checkouts.time.monotonic', return_value=0) as clock:
            executor.return_value.submit.side_effect = [first, second, third]
            cache = CheckoutCache()
            self.assertEqual(cache.snapshot(workspaces), {'$1': (), '$2': ()})
            cache.snapshot(workspaces)
            self.assertEqual(executor.return_value.submit.call_count, 1)
            first.set_result(('/job/repo',))
            self.assertEqual(cache.snapshot(workspaces)['$2'], ('/job/repo',))
            clock.return_value = POLL_INTERVAL
            self.assertEqual(cache.snapshot(workspaces)['$1'], ('/job/repo',))
            second.set_result(())
            self.assertEqual(cache.snapshot(workspaces)['$1'], ())
            cache.snapshot([Workspace('$1', 'renamed', '', '/other')])
            cache.snapshot([])
            self.assertTrue(third.cancelled())
            cache.close()
            executor.return_value.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_cache_depth_changes_cancel_old_scan_and_share_matching_settings(self):
        first, changed, other = Future(), Future(), Future()
        with patch('workmux.checkouts.ThreadPoolExecutor') as executor:
            executor.return_value.submit.side_effect = [first, changed, other]
            cache = CheckoutCache()
            workspaces = [Workspace('$1', 'one', '', '/job', 2),
                          Workspace('$2', 'two', '', '/job', 2)]
            cache.snapshot(workspaces)
            executor.return_value.submit.assert_called_once_with(discover_checkouts, '/job', 2)
            updated = [Workspace('$1', 'one', '', '/job', 3),
                       Workspace('$2', 'two', '', '/job', 3)]
            self.assertEqual(cache.snapshot(updated), {'$1': (), '$2': ()})
            self.assertTrue(first.cancelled())
            executor.return_value.submit.assert_called_with(discover_checkouts, '/job', 3)
            changed.set_result(('/job/repo',))
            self.assertEqual(cache.snapshot(updated), {'$1': ('/job/repo',), '$2': ('/job/repo',)})
            cache.snapshot([updated[0], workspaces[1]])
            self.assertEqual(executor.return_value.submit.call_count, 3)
            cache.close()

    def test_git_checks_are_shared_by_checkout_paths_and_workspace_roots(self):
        first, second = Future(), Future()
        with patch('workmux.git_status.ThreadPoolExecutor') as executor:
            executor.return_value.submit.side_effect = [first, second]
            cache = GitStatusCache()
            workspaces = [Workspace('$1', 'job', '', '/job/repo')]
            paths = ('/job/repo', '/job/other')
            snapshot = cache.snapshot(workspaces, paths)
            self.assertEqual(snapshot, {'$1': None, '/job/repo': None, '/job/other': None})
            self.assertEqual(executor.return_value.submit.call_count, 2)
            for entry in cache.entries.values():
                entry.future.set_result(('dirty', 'ahead'))
            snapshot = cache.snapshot(workspaces, paths)
            self.assertEqual(snapshot['$1'], snapshot['/job/repo'])
            self.assertEqual(snapshot['/job/other'], ('dirty', 'ahead'))
            cache.snapshot(workspaces)
            self.assertEqual(set(cache.entries), {'/job/repo'})
            cache.close()


@unittest.skipUnless(shutil.which('git'), 'Git is optional')
class CheckoutRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='workmux-checkouts-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job = self.root / 'container space; $literal'
        self.job.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

    def git(self, directory, *args):
        return subprocess.run(['git', '-C', str(directory), *args], env=self.env, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def repo(self, directory):
        directory.mkdir(parents=True)
        self.git(directory, 'init', '-b', 'main')
        self.git(directory, 'config', 'user.name', 'Workmux Test')
        self.git(directory, 'config', 'user.email', 'test@example.invalid')
        self.git(directory, 'commit', '--allow-empty', '-m', 'initial')
        return directory

    def test_nested_independent_worktrees_in_plain_container(self):
        main = self.repo(self.root / 'external-source')
        linked = self.job / 'arbitrary' / 'owner' / "repo ' $; space"
        linked.parent.mkdir(parents=True)
        self.git(main, 'worktree', 'add', '-b', 'feature', str(linked))
        other = self.repo(self.job / 'services' / 'other')
        (self.job / 'tracking.md').write_text('job documents, not a repository')
        self.assertTrue((linked / '.git').is_file())
        self.assertEqual(discover_checkouts(str(self.job)), tuple(sorted((str(linked), str(other)))))
        self.assertEqual(checkout_root(str(linked)), str(linked))
        self.assertEqual(checkout_root(str(main)), str(main))
        self.git(main, 'worktree', 'remove', str(linked))
        self.assertEqual(discover_checkouts(str(self.job)), (str(other),))

    def test_repository_workspace_registered_nested_worktrees_and_subdirectory(self):
        main = self.repo(self.job / 'repo')
        linked = main / '.worktrees' / 'feature'
        linked.parent.mkdir()
        self.git(main, 'worktree', 'add', '-b', 'feature', str(linked))
        outside = self.root / 'outside'
        self.git(main, 'worktree', 'add', '-b', 'outside', str(outside))
        self.assertEqual(discover_checkouts(str(main)), (str(linked),))
        self.assertEqual(discover_checkouts(str(linked.parent)), (str(linked),))

    def test_symlinks_missing_roots_invalid_markers_and_scan_depth(self):
        actual = self.repo(self.root / 'outside')
        (self.job / 'linked-repo').symlink_to(actual, target_is_directory=True)
        (self.job / 'loop').symlink_to(self.job, target_is_directory=True)
        fake = self.job / 'fake'
        fake.mkdir()
        (fake / '.git').write_text('not a git marker')
        self.assertEqual(discover_checkouts(str(self.job)), ())
        self.assertEqual(discover_checkouts(str(self.root / 'missing')), ())
        nested = self.repo(self.job / 'nested' / 'repo')
        with patch('workmux.checkouts.MAX_DEPTH', 1):
            self.assertEqual(discover_checkouts(str(self.job)), ())
        alias = self.root / 'job-alias'
        alias.symlink_to(self.job, target_is_directory=True)
        self.assertEqual(discover_checkouts(str(alias)), (str(nested),))

    def test_nested_foreign_worktree_inside_repository_and_configured_depth(self):
        checkout = self.repo(self.job / 'repo')
        source = self.repo(self.root / 'external-source')
        linked = checkout / 'worktrees' / 'adzerk' / 'bifrost'
        linked.parent.mkdir(parents=True)
        self.git(source, 'worktree', 'add', '-b', 'feature', str(linked))
        self.assertEqual(discover_checkouts(str(checkout)), (str(linked),))
        self.assertEqual(discover_checkouts(str(checkout), 2), ())
        self.assertEqual(discover_checkouts(str(checkout), 3), (str(linked),))
        self.assertEqual(discover_checkouts(str(checkout), 0), ())
        nested = self.repo(linked / 'nested')
        self.assertEqual(discover_checkouts(str(checkout), 3), (str(linked),))
        self.assertEqual(discover_checkouts(str(checkout), 4), tuple(sorted((str(linked), str(nested)))))

    def test_registered_worktrees_obey_depth_limit(self):
        checkout = self.repo(self.job / 'repo')
        linked = checkout / 'trees' / 'feature'
        linked.parent.mkdir()
        self.git(checkout, 'worktree', 'add', '-b', 'feature', str(linked))
        self.assertEqual(discover_checkouts(str(checkout), 1), ())
        self.assertEqual(discover_checkouts(str(checkout), 2), (str(linked),))

    def test_directory_walk_skips_git_contents_even_when_git_is_unavailable(self):
        checkout = self.repo(self.job / 'repo')
        (checkout / 'content').mkdir()
        with patch('workmux.checkouts._git', return_value=None), \
                patch('workmux.checkouts.os.scandir', wraps=os.scandir) as scandir:
            self.assertEqual(discover_checkouts(str(checkout)), ())
        self.assertEqual({call.args[0] for call in scandir.call_args_list},
                         {str(checkout), str(checkout / 'content')})

    def test_directory_and_query_budgets(self):
        self.repo(self.job / 'repo')
        with patch('workmux.checkouts.MAX_DIRECTORIES', 0):
            self.assertEqual(discover_checkouts(str(self.job)), ())
        with patch('workmux.checkouts.MAX_GIT_QUERIES', 0):
            self.assertEqual(discover_checkouts(str(self.job)), ())
        with patch('workmux.checkouts.MAX_ENTRIES', 0):
            self.assertEqual(discover_checkouts(str(self.job)), ())


class CheckoutSidebarTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Workspace('$1', 'job', '', '/job')
        self.paths = ('/job/worktrees/owner/api', '/job/worktrees/owner/ui')
        self.checkouts = {'$1': self.paths}

    def test_checkouts_and_agents_are_siblings_with_basename_checkout_labels(self):
        api, ui = self.paths
        tabs = [Tab('$1', '@1', 1, 'coordinate', 'working', '%1', ('/job',)),
                Tab('$1', '@2', 2, 'implement', 'working', '%2', (api + '/src',)),
                Tab('$1', '@3', 3, 'review', 'attention', '%3', (ui,))]
        rows = sidebar_rows([self.workspace], tabs, {api: ('dirty', 'ahead'), ui: ('clean',)},
                            self.checkouts, {('$1', api): '@7'})
        self.assertEqual([row.text for row in rows], [
            '1:job', '├─ ⎇ api ● ↑', '├─ ⎇ ui ✓', '├─ ✦ 1:coordinate',
            '├─ ✦ 2:implement', '└─ ✦ 3:review'])
        self.assertEqual(rows[1].key, ('$1', 'checkout:' + api))
        self.assertEqual(rows[1].window, '@7')
        self.assertEqual(rows[4].key, ('$1', '%2'))
        changed = sidebar_rows([self.workspace], tabs, {}, self.checkouts, {('$1', api): '@8'})
        self.assertEqual(rows[1].key, changed[1].key)

    def test_mixed_unknown_and_external_roots_stay_at_workspace_level(self):
        api, ui = self.paths
        for roots in ((), (None,), (api, None), (api, ui), ('/outside',),
                      (api + '-other',), ('relative',)):
            with self.subTest(roots=roots):
                rows = sidebar_rows([self.workspace], [Tab('$1', '@2', 2, 'mixed', 'working', roots=roots)],
                                    {}, self.checkouts)
                self.assertEqual(rows[-1].text, '└─ ✦ 2:mixed')

    def test_nested_checkout_does_not_indent_its_agent(self):
        api, ui = self.paths
        nested = api + '/nested'
        tabs = [Tab('$1', '@2', 2, 'agent', 'working', roots=(nested, nested + '/src'))]
        rows = sidebar_rows([self.workspace], tabs, {}, {'$1': (api, nested)})
        self.assertEqual(rows[-1].text, '└─ ✦ 2:agent')
        self.assertEqual(rows[-2].directory, nested)

    def test_duplicate_checkout_basenames_keep_distinct_paths_and_keys(self):
        paths = ('/job/one/repo', '/job/two/repo')
        rows = sidebar_rows([self.workspace], [], checkouts={'$1': paths},
                            checkout_windows={('$1', paths[0]): '@7', ('$1', paths[1]): '@8'})
        self.assertEqual([row.text for row in rows[1:]], ['├─ ⎇ repo', '└─ ⎇ repo'])
        self.assertEqual([row.directory for row in rows[1:]], list(paths))
        self.assertEqual([row.window for row in rows[1:]], ['@7', '@8'])
        self.assertEqual(len({row.key for row in rows}), 3)

    def test_removed_checkout_does_not_hide_its_agent_tab(self):
        tab = Tab('$1', '@2', 2, 'agent', 'working', roots=(self.paths[0],))
        rows = sidebar_rows([self.workspace], [tab], {}, {})
        self.assertEqual(rows[-1].key, ('$1', '@2'))
        self.assertEqual(rows[-1].text, '└─ ✦ 2:agent')
