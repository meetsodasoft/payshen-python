# How work reaches main

The owner wants a GitHub history that shows real engineering: issues for planned work, pull requests for changes, reviews where they help. Nothing is created just because a session starts or to move a statistic.

## Issues

- Before a meaningful feature, fix or technical improvement, look for an existing issue (`gh issue list --search`). If there is none, open one: problem, proposed solution, acceptance criteria as a checklist, labels (`bug`, `enhancement`, `documentation`).
- Genuine future work that is not done now goes into the backlog as an issue.
- No issue for a typo, a one-line check or anything finished in the same session without a real decision.

## Pull requests

- Never push to `main`. One branch per change, `gh pr create`, with `Closes #N` (or `Refs #N`) when an issue exists.
- The description says what changed and why, and the tests run with their result. Say plainly what was not run.
- Batch related work into one pull request (CI minutes are limited). No pull request exists only to add to a count.
- Merge when CI is green, with a merge commit, and delete the branch. When a second reviewer is named (meetsodasoft/payshen#175), one approval becomes required.
- Commits: descriptive messages, the real author, no empty commits, no changed timestamps, no history rewrites. No Claude attribution lines in commits or pull requests.

## Releases

A release is published only after the owner says so ("objavi"); the Payshen main repository's news, changelog and docs are updated in the same batch.
