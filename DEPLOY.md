# Personal fork maintenance

This is your personal fork of [garrytan/gbrain](https://github.com/garrytan/gbrain).
It lives at [dracozombie19/gbrain](https://github.com/dracozombie19/gbrain) and
contains your deployment files (Dockerfile + setup scripts) on a long-lived
branch called `personal-deploy`.

## Remotes

```
origin    https://github.com/dracozombie19/gbrain.git   (your fork)
upstream  https://github.com/garrytan/gbrain.git        (the source)
```

If these ever drift, fix with:

```bash
git remote set-url origin   https://github.com/dracozombie19/gbrain.git
git remote set-url upstream https://github.com/garrytan/gbrain.git
```

## Branch layout

- **`master`** — clean mirror of `upstream/master`. Never commit directly to
  this branch. It exists only so you can fast-forward from upstream cleanly.
- **`personal-deploy`** — your working branch. Contains everything on `master`
  PLUS your `Dockerfile` and `scripts/` deployment helpers. Day-to-day edits
  go here.

## Pulling in new upstream releases

When the upstream repo ships a new version (e.g. v0.34, v1.0), bring it down
in three steps:

```bash
# 1. Fast-forward your local master to upstream
git checkout master
git pull upstream master --ff-only

# 2. Mirror master to your fork on GitHub
git push origin master

# 3. Merge the new upstream changes into your deploy branch
git checkout personal-deploy
git merge master
git push origin personal-deploy
```

Conflicts during step 3 are rare because your changes are isolated (Dockerfile
and `scripts/setup-*`, `scripts/grant-*`, `scripts/gen-*` are filenames upstream
doesn't touch). If upstream ever adds its own Dockerfile, you'll need to resolve
that one file by hand.

If you prefer rebase over merge, use `git rebase master` instead of `git merge
master`, but be aware it rewrites `personal-deploy`'s history and forces a
`--force-with-lease` push afterward. For a personal fork, merge is simpler.

## Day-to-day deploy edits

```bash
git checkout personal-deploy
# edit Dockerfile, scripts/, etc.
git add Dockerfile scripts/whatever.sh
git commit -m "Tweak deploy step X"
git push
```

That's it. No PR ceremony needed since this is your own fork.

## Cloning fresh on a new machine

```bash
git clone https://github.com/dracozombie19/gbrain.git
cd gbrain
git remote add upstream https://github.com/garrytan/gbrain.git
git fetch upstream
git checkout personal-deploy
```

## Windows line-ending gotcha

Git on Windows auto-converts LF -> CRLF on checkout. For shell scripts and
Dockerfiles that get copied into Linux containers, this can cause `exec format
error` or shebang-line failures at runtime.

If you hit that, add a `.gitattributes` file at the repo root with:

```
Dockerfile      text eol=lf
*.sh            text eol=lf
scripts/*.sh    text eol=lf
```

Then re-checkout the affected files:

```bash
git rm --cached -r .
git reset --hard
```

Not urgent — only do this if you actually see breakage during a container build
or run. Your `.ps1` and `.bat` scripts run on Windows, so they should stay CRLF.

## Things to NEVER commit

- `secret_iam.json` (service account credentials)
- `gbrain_service.json` (if it contains creds)
- `.env`, `.env.local`, or anything with API keys
- Anything matching `*.pem`, `*.key`, `*-credentials.json`

If you need to track which secret files exist without committing them, add
them to `.gitignore` and document their purpose in this file instead.
