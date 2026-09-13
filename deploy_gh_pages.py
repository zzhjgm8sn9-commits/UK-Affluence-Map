"""Publish dist/ to a gh-pages branch.

    python deploy_gh_pages.py https://github.com/YOU/uk-affluence-map.git

`dist/` is turned into its own throwaway git repository with a single commit and
force-pushed to `gh-pages`. That is deliberate: the built site is 117 MB, and
committing it to the main repository would add that much to the history on every
rebuild, for files that are entirely reproducible from the pipeline. A
force-pushed single commit keeps the deployed branch at one copy forever.

The main repository is untouched -- no worktrees, no branch switching, nothing
to clean up if this goes wrong.

Pushing needs your GitHub credentials, so git will prompt (or open a browser)
the first time. That part cannot be automated from here.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

# GitHub rejects files over 100 MB outright and warns above 50 MB.
HARD_LIMIT = 100 * 1024 * 1024
WARN_LIMIT = 50 * 1024 * 1024


def run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True)


def check_sizes() -> bool:
    ok = True
    for f in sorted(DIST.rglob("*")):
        if not f.is_file():
            continue
        size = f.stat().st_size
        if size > HARD_LIMIT:
            print(f"  TOO LARGE  {f.relative_to(DIST)}  {size / 1e6:.0f} MB "
                  f"-- GitHub rejects files over 100 MB")
            ok = False
        elif size > WARN_LIMIT:
            print(f"  large      {f.relative_to(DIST)}  {size / 1e6:.0f} MB "
                  f"-- GitHub warns above 50 MB but will accept it")
    return ok


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        print("error: pass the repository URL as the first argument")
        sys.exit(1)
    remote = sys.argv[1]

    if not (DIST / "index.html").exists():
        print("dist/ not built -- running build_dist.py first")
        result = subprocess.run([sys.executable, str(ROOT / "build_dist.py")])
        if result.returncode != 0:
            sys.exit(result.returncode)

    print("checking file sizes against GitHub's limits:")
    if not check_sizes():
        sys.exit(1)
    print("  all files within limits")

    # A fresh repository each time, so the deployed branch never accumulates
    # history. dist/ is rebuilt from scratch anyway.
    git_dir = DIST / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)

    steps = [
        (["git", "init", "-q"], "init"),
        (["git", "checkout", "-q", "-b", "gh-pages"], "branch"),
        (["git", "add", "-A"], "stage"),
        (["git", "-c", "user.name=deploy", "-c", "user.email=deploy@localhost",
          "commit", "-q", "-m", "Deploy UK Affluence Map"], "commit"),
        (["git", "remote", "add", "origin", remote], "remote"),
    ]
    for args, label in steps:
        result = run(args, DIST)
        if result.returncode != 0:
            print(f"failed at {label}:\n{result.stderr}")
            sys.exit(1)

    files = sum(1 for f in DIST.rglob("*") if f.is_file() and ".git" not in f.parts)
    print(f"\ndist/ staged as a gh-pages commit ({files:,} files)")
    print("\npushing -- git may prompt for credentials or open a browser...")

    push = subprocess.run(["git", "push", "-f", "origin", "gh-pages"], cwd=DIST, text=True)
    if push.returncode != 0:
        print("\npush failed. Once you have sorted credentials, run:")
        print(f"  cd dist && git push -f origin gh-pages")
        sys.exit(1)

    owner_repo = remote.rstrip("/").removesuffix(".git").split("github.com")[-1].strip(":/")
    owner = owner_repo.split("/")[0] if "/" in owner_repo else "YOU"
    repo = owner_repo.split("/")[-1]
    print("\npushed.")
    print(f"Now enable Pages: https://github.com/{owner_repo}/settings/pages")
    print("  Source: 'Deploy from a branch'   Branch: gh-pages   Folder: / (root)")
    print(f"\nThe site will appear at https://{owner}.github.io/{repo}/")
    print("First build takes a couple of minutes.")


if __name__ == "__main__":
    main()
