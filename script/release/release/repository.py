from __future__ import absolute_import
from __future__ import unicode_literals

import os

from git import GitCommandError
from git import Repo
from github import Github

from .const import NAME
from .const import REPO_ROOT
from .utils import branch_name
from .utils import read_release_notes_from_changelog
from .utils import ScriptError


class Repository(object):
    def __init__(self, root=None, gh_name=None):
        if root is None:
            root = REPO_ROOT
        if gh_name is None:
            gh_name = NAME
        self.git_repo = Repo(root)
        self.gh_client = Github(os.environ['GITHUB_TOKEN'])
        self.gh_repo = self.gh_client.get_repo(gh_name)

    def create_release_branch(self, version, base=None):
        print('Creating release branch {} based on {}...'.format(version, base or 'master'))
        remote = self.find_remote(self.gh_repo.full_name)
        remote.fetch()
        if self.branch_exists(branch_name(version)):
            raise ScriptError(
                "Branch {} already exists locally. "
                "Please remove it before running the release script.".format(branch_name(version))
            )
        if base is not None:
            base = self.git_repo.tag('refs/tags/{}'.format(base))
        else:
            base = 'refs/remotes/{}/automated-releases'.format(remote.name)
        release_branch = self.git_repo.create_head(branch_name(version), commit=base)
        release_branch.checkout()
        self.git_repo.git.merge('--strategy=ours', '--no-edit', '{}/release'.format(remote.name))
        with release_branch.config_writer() as cfg:
            cfg.set_value('release', version)
        return release_branch

    def find_remote(self, remote_name=None):
        if not remote_name:
            remote_name = self.gh_repo.full_name
        for remote in self.git_repo.remotes:
            for url in remote.urls:
                if remote_name in url:
                    return remote
        return None

    def create_bump_commit(self, bump_branch, version):
        print('Creating bump commit...')
        bump_branch.checkout()
        self.git_repo.git.commit('-a', '-s', '-m "Bump {}"'.format(version), '--no-verify')

    def diff(self):
        return self.git_repo.git.diff()

    def checkout_branch(self, name):
        return self.git_repo.branches[name].checkout()

    def push_branch_to_remote(self, branch, remote_name=None):
        print('Pushing branch {} to remote...'.format(branch.name))
        remote = self.find_remote(remote_name)
        remote.push(refspec=branch, force=True)

    def branch_exists(self, name):
        return name in [h.name for h in self.git_repo.heads]

    def create_release_pull_request(self, version):
        return self.gh_repo.create_pull(
            title='Bump {}'.format(version),
            body='Automated release for docker-compose {}\n\n{}'.format(
                version, read_release_notes_from_changelog()
            ),
            base='release',
            head=branch_name(version),
        )

    def create_release(self, version, release_notes, **kwargs):
        return self.gh_repo.create_git_release(
            tag=version, name=version, message=release_notes, **kwargs
        )

    def find_release(self, version):
        print('Retrieving release draft for {}'.format(version))
        releases = self.gh_repo.get_releases()
        for release in releases:
            if release.tag_name == version and release.title == version:
                return release
        return None

    def remove_release(self, version):
        print('Removing release draft for {}'.format(version))
        releases = self.gh_repo.get_releases()
        for release in releases:
            if release.tag_name == version and release.title == version:
                if not release.draft:
                    print(
                        'The release at {} is no longer a draft. If you TRULY intend '
                        'to remove it, please do so manually.'
                    )
                    continue
                release.delete_release()

    def remove_bump_branch(self, version, remote_name=None):
        name = branch_name(version)
        if not self.branch_exists(name):
            return False
        print('Removing local branch "{}"'.format(name))
        if self.git_repo.active_branch.name == name:
            print('Active branch is about to be deleted. Checking out to master...')
            try:
                self.checkout_branch('master')
            except GitCommandError:
                raise ScriptError(
                    'Unable to checkout master. Try stashing local changes before proceeding.'
                )
        self.git_repo.branches[name].delete(self.git_repo, name, force=True)
        print('Removing remote branch "{}"'.format(name))
        remote = self.find_remote(remote_name)
        try:
            remote.push(name, delete=True)
        except GitCommandError as e:
            if 'remote ref does not exist' in str(e):
                return False
            raise ScriptError(
                'Error trying to remove remote branch: {}'.format(e)
            )
        return True

    def find_release_pr(self, version):
        print('Retrieving release PR for {}'.format(version))
        name = branch_name(version)
        open_prs = self.gh_repo.get_pulls(state='open')
        for pr in open_prs:
            if pr.head.ref == name:
                print('Found matching PR #{}'.format(pr.number))
                return pr
        print('No open PR for this release branch.')
        return None

    def close_release_pr(self, version):
        print('Retrieving and closing release PR for {}'.format(version))
        name = branch_name(version)
        open_prs = self.gh_repo.get_pulls(state='open')
        count = 0
        for pr in open_prs:
            if pr.head.ref == name:
                print('Found matching PR #{}'.format(pr.number))
                pr.edit(state='closed')
                count += 1
        if count == 0:
            print('No open PR for this release branch.')
        return count


def get_contributors(pr_data):
    commits = pr_data.get_commits()
    authors = {}
    for commit in commits:
        author = commit.author.login
        authors[author] = authors.get(author, 0) + 1
    return [x[0] for x in sorted(list(authors.items()), key=lambda x: x[1])]


def upload_assets(gh_release, files):
    print('Uploading binaries and hash sums')
    for filename, filedata in files.items():
        print('Uploading {}...'.format(filename))
        gh_release.upload_asset(filedata[0], content_type='application/octet-stream')
        gh_release.upload_asset('{}.sha256'.format(filedata[0]), content_type='text/plain')
    gh_release.upload_asset(
        os.path.join(REPO_ROOT, 'script', 'run', 'run.sh'), content_type='text/plain'
    )
