"""Publish the release, commit a publication receipt, then verify its final SHA.

No force pushes, history rewriting, credentials, or unrelated worktree changes.
Run after the initial large push has completed.
"""
from pathlib import Path
import datetime
import json
import os
import shutil
import subprocess

B = Path('/public/home/liusiqi/03.毕设')
O = B / 'BioMind_Production_20261020'
W = B / 'BioMind-worktrees/submission-production-20261020'
D = W / 'docs/implementation/submission-20261020'
BRANCH = 'release/submission-20261020'
URL = 'https://github.com/jebwvgsid/biomind_agent_47'
ENV = dict(os.environ, GIT_TERMINAL_PROMPT='0')

def git(*args, timeout=240):
    result = subprocess.run(['git', '-c', 'gc.auto=0', '-C', str(W), *args],
                            env=ENV, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Git operation failed: ' + result.stderr[-3000:])
    return result.stdout.strip()

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

def remote_head():
    text = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH)
    rows = [line.split() for line in text.splitlines() if line.strip()]
    assert len(rows) == 1 and rows[0][1] == 'refs/heads/' + BRANCH
    return rows[0][0]

def main():
    assert git('branch', '--show-current') == BRANCH
    assert git('remote', 'get-url', 'origin') == URL + '.git'
    assert git('status', '--porcelain') == '', 'Preserve concurrent changes'
    source_head = git('rev-parse', 'HEAD')
    assert source_head.startswith('2eaf18d'), 'Unexpected release head; review before publishing'
    first = remote_head()
    git('merge-base', '--is-ancestor', first, source_head)
    pushed = git('push', '-u', 'origin', BRANCH)
    (O / 'github/push-shared-repair.log').write_text(pushed + '\n')
    assert remote_head() == source_head
    at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    receipt = {'status': 'VERIFIED_BEFORE_RECEIPT_COMMIT', 'verified_at': at,
               'repository': URL, 'branch': BRANCH, 'verified_commit': source_head,
               'first_published_commit': first, 'authentication_login': 'jebwvgsid',
               'authentication_method': 'GitHub CLI browser device authorization',
               'credential_file_mode': '0600', 'credentials_committed': False,
               'note': 'This receipt is committed afterwards; final local GIT.json records the receipt commit and its remote verification.'}
    write(D / 'GIT_PUSH.json', receipt)
    for base in (O, D):
        p = base / 'DEPLOYMENT.json'
        data = json.loads(p.read_text())
        data['git_remote_push'] = receipt
        write(p, data)
    for p in (O / '部署说明.txt', O / 'shared-plugin/交付说明.txt'):
        text = p.read_text().replace('最终远端状态见 ../GIT.json', '最终远端状态见 ' + str(O / 'GIT.json'))
        text += f'\nGitHub 已验证：{URL}/tree/{BRANCH}\n已发布功能提交：{source_head}（{at}）。本回执随后单独提交；最终回执提交和远端复核见 {O}/GIT.json。\n'
        p.write_text(text)
    shutil.copy2(O / '部署说明.txt', D / '部署说明.txt')
    shutil.copy2(O / 'shared-plugin/交付说明.txt', D / 'shared-plugin/交付说明.txt')
    shutil.copy2(Path(__file__), D / 'shared-plugin/finish_git_delivery.py')
    paths = ['docs/implementation/submission-20261020/' + p for p in (
        'GIT_PUSH.json', 'DEPLOYMENT.json', '部署说明.txt', 'shared-plugin/交付说明.txt',
        'shared-plugin/finish_git_delivery.py')]
    git('diff', '--check')
    git('add', '--', *paths)
    git('commit', '-m', 'docs(release): record verified GitHub and shared-plugin delivery')
    final_head = git('rev-parse', 'HEAD')
    git('push', 'origin', BRANCH)
    assert remote_head() == final_head
    assert git('status', '--porcelain') == ''
    record = {'branch': BRANCH, 'commit': final_head, 'worktree': str(W),
              'working_tree_clean': True, 'remote_pushed': True, 'remote_commit': final_head,
              'repository': URL, 'branch_url': URL + '/tree/' + BRANCH,
              'functional_commit': source_head, 'verified_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'build_source_commit': '6321f6e6ccc389ccff6f7bc045aafaa51c46ffb6',
              'authentication_login': 'jebwvgsid'}
    write(O / 'GIT.json', record)
    data = json.loads((O / 'DEPLOYMENT.json').read_text())
    data['git_remote_push'] = {'status': 'PUSHED', 'commit': final_head, 'remote_commit': final_head,
                               'branch_url': record['branch_url'], 'verified_at': record['verified_at']}
    write(O / 'DEPLOYMENT.json', data)
    with (O / '部署说明.txt').open('a') as stream:
        stream.write(f'\n最终 Git 提交：{final_head}；远端同分支 SHA 完全一致，发布工作树干净。\n')
    print(json.dumps({'status': 'PUSHED_AND_VERIFIED', 'commit': final_head, 'branch_url': record['branch_url']}))

if __name__ == '__main__':
    main()
