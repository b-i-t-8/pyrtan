# The catalog

Trees published here are served at <https://pyrtan.com/catalog/> beside
the repository's own examples (which are the `pyrtan` namespace). One
directory per namespace, one `<name>.toml` per tree, and an `OWNERS` file
naming the GitHub logins that may change the namespace:

    catalog/
      barry/
        OWNERS            # one login per line
        lan-printer.toml  # namespace = "barry", license = "MIT"

`pyrtan publish my-tree.toml --into <this checkout> --owner <your login>`
puts a tree in the right place and runs the gate; then open a pull
request. The `catalog` workflow runs the same gate on the pull request:
every tree loads clean, carries `license` and `namespace`, sits in its
namespace's directory under its own name, ids are unique, only owners
change a namespace, and a changed file bumps its `version`. A maintainer
merges; the `pages` workflow publishes on merge. There is no account and
no API: the pull request is the publishing. See
[`site/README.md`](../site/README.md).
