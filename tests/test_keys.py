"""Where keys come from, without reading any real key."""
from riley import keys


def test_a_worktree_also_reads_the_main_checkouts_env(tmp_path):
    main, tree = tmp_path / 'main', tmp_path / 'tree'
    (main / '.git' / 'worktrees' / 'tree').mkdir(parents=True)
    tree.mkdir()
    (tree / '.git').write_text(f"gitdir: {main / '.git' / 'worktrees' / 'tree'}\n")
    assert keys.checkout_envs(tree) == [tree / '.env', main / '.env']
    assert keys.checkout_envs(main) == [main / '.env']
