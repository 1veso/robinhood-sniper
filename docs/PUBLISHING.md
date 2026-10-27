# Publish the tested version

The archive's original release commit is
`0924cfaf63fcd900829eb5bb55ca4a66cfc9fe4a`. The v0.1.1 update is a descendant
containing the Windows fix, automatic research service and its tests. The source
folder can retain the name `sniper-v0.1.0` after the update; `python run.py --version`
reports the installed code version.

To restore the original bundled history into a new checkout:

```bash
git clone git-history/sniper-v0.1.0.bundle ../sniper-publish
cd ../sniper-publish
git remote set-url origin https://github.com/1veso/robinhood-sniper.git
```

For an existing tested checkout, publish its committed main branch with:

```bash
git push -u origin main
```

These are non-forced operations. The bundle continues to identify the original
v0.1.0 source; it does not silently include later fixes. Obtain the updated source
from the repository's main branch. All credentials, service state, wallet histories,
databases and generated reports stay under ignored runtime paths and are not
published. Review `git diff --cached` and run `python run_tests.py` before future
release commits.
