"""Dedicated Git state branch, ordinary pushes, no expiring cache fallback."""
import argparse
import base64
import os
from pathlib import Path
import subprocess

BRANCH = 'liquidity-bos-state'
FILENAME = 'liquidity_bot_state.json'


def git(root, *args, check=True):
    env = os.environ.copy()
    token = env.get('GITHUB_TOKEN')
    if token:
        auth = base64.b64encode(('x-access-token:' + token).encode()).decode()
        env.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='http.https://github.com/.extraheader',
                   GIT_CONFIG_VALUE_0='AUTHORIZATION: basic ' + auth)
    result = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', *args],
                            cwd=root, env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError('State Git operation failed: ' + args[0])
    return result


def restore(root, initialize=False, remote=None):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    remote = remote or f"https://github.com/{os.environ['GITHUB_REPOSITORY']}.git"
    git(root, 'init', '--quiet')
    git(root, 'remote', 'add', 'origin', remote)
    exists = git(root, 'ls-remote', '--exit-code', '--heads', 'origin',
                 'refs/heads/' + BRANCH, check=False)
    if exists.returncode == 0:
        git(root, 'fetch', '--quiet', 'origin', 'refs/heads/' + BRANCH)
        git(root, 'checkout', '--quiet', '--detach', 'FETCH_HEAD')
        if not (root / FILENAME).is_file():
            raise RuntimeError('State branch missing state file')
    elif exists.returncode == 2 and initialize:
        from liquidity_alerts import load_state
        (root / FILENAME).write_text(__import__('json').dumps(load_state()))
        persist(root)
    else:
        raise RuntimeError('Cannot restore state; initialize explicitly only on first run')


def persist(root):
    root = Path(root)
    git(root, 'config', 'user.name', 'github-actions[bot]')
    git(root, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git(root, 'add', '--', FILENAME)
    if git(root, 'diff', '--cached', '--quiet', check=False).returncode:
        git(root, 'commit', '--quiet', '-m', 'Persist Liquidity BOS state')
    git(root, 'push', '--quiet', 'origin', 'HEAD:refs/heads/' + BRANCH)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--initialize', action='store_true')
    args = parser.parse_args()
    restore(args.root, args.initialize)
