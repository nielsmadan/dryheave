# Packaged operator resources and ownership

`skills.py` discovers `dryheave-collect`, `dryheave-case` and `dryheave-results`
through `importlib.resources` in the installed `dryheave` package. The wheel and
sdist ship their agent-neutral SKILL.md bytes and the standalone public examples.
No checkout path, global installation or dynamic network fetch is needed.
`example write --target FRESH_DIRECTORY` copies the offline Python demo and real
workflow from those installed resources. It refuses existing destinations.

`skills list` lists the bundle. `skills list --target PATH` and
`skills doctor --target PATH` inspect the explicit target read-only, reporting
`missing`, `current`, `outdated` or `conflict`. `install`, `update` and `uninstall`
require `--target`; optional positional skill names select a distinct subset,
otherwise all three are selected. Targets are never inferred from the operator's
agent, HOME or config. Listing/doctor do not create an absent target or store.

Each installed directory contains SKILL.md and `.dryheave-owned.json`, a strict
schema-version-1 manifest with owner, skill name, package version and SHA-256 of
the exact installed file. Version/hash differences from the current bundle mark
an intact older installation as outdated. The stored hashes establish ownership
of bytes for maintenance, not cryptographic authenticity or access control.

Every selected directory is checked before any skill content changes. Install
refuses any name collision, even byte-identical content without ownership.
Update and uninstall require matching ownership names, exact expected entries,
regular files and bytes matching the old owned hashes. Foreign extra files,
missing files, changed bytes and malformed manifests cause refusal; the operator
must preserve/review the conflict or select another target. There is no force or
adopt mode. Unselected sibling directories remain untouched.

Filesystem helpers reject symlinks in directory components and file reads. Targets
cannot be filesystem roots or contain parent traversal. A target directory flock
serializes cooperating lifecycle writers without creating a lock file alongside
foreign skills. File publication is atomic. Uninstall removes only the verified
SKILL.md and ownership file, then the empty owned directory; it never recursively
deletes a target. An interrupted I/O operation can leave a partial installation
or a manifest/content mismatch; a later operation reports the conflict and
preserves those bytes for review. Multi-skill operations are prevalidated, not a
transactional filesystem rollback. Concurrent external edits are outside this
cooperative ownership model.

These operator skills help select evidence and explain actual public JSON fields.
They are not runtime controllers or trusted graders, and are disabled for subjects
by the native profile defaults. Skill installation changes neither those defaults
nor agent global configuration. Use the target directory actually recognized by
your operator agent and reload its discovery according to that agent's behavior.

Runtime `doctor` is separate from ownership inspection: it reads package/runtime
information, finds executable paths and probes only tui-test's pinned version.
It does not download tools, launch agents/models, inspect credentials, discover
configuration or imply a successful authenticated native trial. Diagnostic success
is represented by `data.ready`; the command can successfully report missing tools.
