# Publish the tested version

The first GitHub publication attempt was blocked with HTTP 403,
`Resource not accessible by integration`, when creating README.md. The connection
could read the repository but was not authorized to write its contents. No
successful remote commit was created by that attempt.

The delivered source archive is complete and runnable. Its `git-history/`
directory contains a Git bundle of the tested source, including the initial
commit. From inside the extracted `sniper-v0.1.0` folder, use your own
authenticated Git terminal:

```bash
git clone git-history/sniper-v0.1.0.bundle ../sniper-publish
cd ../sniper-publish
git remote set-url origin https://github.com/1veso/robinhood-sniper.git
git push -u origin main
```

These are ordinary non-forced operations. They preserve the bundle's commit and
will reject an incompatible remote history. The source archive also works
without Git: use `python run.py demo` directly from its extracted folder.

Alternatively, restore repository-content write access for the ChatGPT GitHub
connection and publish the same committed tree through that connection. There
is no need to rebuild the application or disclose an access token in chat.
