# Working on uri-social-backend

## Branches and releases (read before changing anything)

- `aws/prod` is production and the source of truth. `aws/dev` is staging. Both deploy on push.
- Every change starts from prod: `git fetch origin && git switch -c feat/<name> origin/aws/prod`.
- Test on staging: open a PR from that branch into `aws/dev`, merge with "Create a merge commit".
- Release: open a PR from the **same branch** into `aws/prod` (needs a code-owner approval), merge with a merge commit, watch the deploy.
- Prod moved on and the release PR conflicts? `git merge origin/aws/prod` into the branch. Do not rebase a branch that has already been merged into `aws/dev`.
- Hotfix: branch from `aws/prod` → PR into `aws/prod` → same branch PR into `aws/dev` the same day.
- Never: push directly to `aws/dev` / `aws/prod`, merge `aws/dev` into anything, or force-push a shared branch.

## Environment and secrets

- Dev and prod are separate AWS accounts with their own credentials. Secrets live in AWS SSM Parameter Store, never in git.
- `.github/` deploy files (`deploy-aws-*.yml`, `scripts/ensure_task_secrets.py`, `codedeploy/`) differ between `aws/dev` and `aws/prod` on purpose. Do not edit them on a feature branch.
- Never commit data exports, `.env` files, or connection strings. Scripts read connection details from environment variables.
- Unfinished features are hidden with feature flags (`app/features`), not by deleting code on one branch.

## Checks

PRs run `.github/workflows/pr-checks.yml`: "Backend starts" (compile + import) and "Backend tests" (skips `tests/test_0*_*.py`, which need a running API).
