# Names at pyrtan.com

A name here is `<your GitHub login>@pyrtan.com`, pointing at your
pyrtan identity (a key: `pyr:` and 56 characters). Anyone resolves it with

    pyrtan id resolve alice@pyrtan.com

which fetches `https://pyrtan.com/.well-known/pyrtan/name/alice.json`,
checks that your key signed it, and pins the answer: if this file ever
names a different key, everyone who resolved it before is told rather
than switched. pyrtan.com is one name provider among any number; your
identity is the key, and works without a name.

## Getting one

**In a browser:** open the console's Identity tab
(<https://pyrtan.com/console/>), *Make my identity*, save your backup,
then *Claim a name* with your GitHub login. GitHub's own new-file page
opens with `site/names/<login>.json` filled in; choose *Propose changes*
and *Create pull request*, from that GitHub account.

**From a shell:**

    pip install 'pyrtan[identity]'
    pyrtan id new                                  # once; back up the key it names
    pyrtan id name <login>@pyrtan.com --into <a checkout of this repository>

That writes `site/names/<login>.json` (the claim, signed by your key,
with your numeric GitHub user id, which it looks up from your login; pass
`--github ID` to give it yourself) and `site/names/<login>.id.json` (your
identity document: routes, devices, revocations). Commit both and open a
pull request **from the GitHub account the name is**. When your identity
document changes (a device added or revoked), copy the new one over
with `pyrtan id show --json > site/names/<login>.id.json` and open
another.

## What the gate checks

The `catalog-gate` workflow runs main's `site/gate.py` on the pull request
(a pull request that edits the gate is still judged by main's):

- every claim is for `<its file's name>@pyrtan.com` and signed by the key
  it names; every identity document is signed by that same key;
- a name is a GitHub login, and each GitHub account holds at most one;
- a **new** name is the pull request author's own login, and the claim's
  `github` is the author's numeric id;
- a name is **changed or removed** only by the account whose numeric id it
  carried before. GitHub frees a login when its owner renames, so the id,
  not the login, is what owns a name. To follow a rename, remove the old
  name and add the new one in the same pull request.

A maintainer merges; the `pages` workflow publishes within the hour. A
maintainer may merge over the gate to remove a name on its owner's
request or for abuse. The design is in
[`_meta/identity.md`](../../_meta/identity.md) §6.
