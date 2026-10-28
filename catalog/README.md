# The catalog

Trees published here are served at <https://pyrtan.com/catalog/> beside
the repository's own examples (which are the `pyrtan` namespace). One
directory per namespace, one `<name>.toml` per tree, and an `OWNERS` file
naming the GitHub accounts that may change the namespace:

    catalog/
      barry/
        OWNERS            # one per line: a login, then its numeric id (barry 1234567)
        lan-printer.toml  # namespace = "barry", license = "MIT"

`pyrtan publish my-tree.toml --into <this checkout> --owner <your login>`
puts a tree in the right place and runs the gate; then open a pull
request. The `catalog-gate` workflow runs the same gate on the pull request,
main's copy of it, so a pull request cannot change the gate that judges it:
every tree loads clean, carries `license` and `namespace`, sits in its
namespace's directory under its own name, ids are unique, only owners
change a namespace, and a changed file bumps its `version`. Give your
numeric GitHub id after your login in OWNERS (`https://api.github.com/users/<login>`
shows it): the gate then knows you by the id, so a login you give up and
someone else takes does not take your namespace with it. A new namespace
that a name at pyrtan.com claims (`site/names/`) is that name's account's.
The repository's own `examples/` have an OWNERS of their own, judged the
same way. A maintainer
merges; the `pages` workflow publishes on merge. There is no account and
no API: the pull request is the publishing. See
[`site/README.md`](../site/README.md).
