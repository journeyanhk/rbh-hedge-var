# CI workflow

`github-actions-ci.yml` is the GitHub Actions pipeline (pytest + ruff + a guard
against committing `.env`). It lives here instead of `.github/workflows/`
because the delivery token lacks the `workflow` OAuth scope.

To activate it, move the file into place with a token that has `workflow` scope
(or via the GitHub web UI → Actions → new workflow):

    mkdir -p .github/workflows
    git mv ci/github-actions-ci.yml .github/workflows/ci.yml
    git commit -m "Enable CI workflow"
