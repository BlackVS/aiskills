# aimem process set: moved to the aimem repository

The aimem reference process set (manifest, handbook, READY/DONE checklists
and task templates) now lives in the aimem repository, at
[`processes/aimem/`](https://github.com/BlackVS/aimem/tree/master/processes/aimem).
It moved there because it is bound to aimem: the manifest format aimem reads,
templates in aimem's task shape and a handbook of aimem's Kanban states. The
set was added there by commit
[`4811a8800ec5f038e52bdf8c516f736e00c91036`](https://github.com/BlackVS/aimem/commit/4811a8800ec5f038e52bdf8c516f736e00c91036).

Select it from the aimem repository. The manifest path defaults to
`processes/aimem/manifest.json`, which is where the set lives there:

```sh
aimem process select https://github.com/BlackVS/aimem.git <release tag, v0.10.14 or later> -p <project>
```

v0.10.14 is the first aimem release that carries the set. A full 40-character
commit of the aimem repository that contains the set works as well.

Selections pinned to an ai-skills commit keep working. A hub stores the files
of the commit it selected, and forge tarballs are per commit, so moving the
set does not change what an existing selection serves. Replace such a
selection with one from the aimem repository when convenient.

This directory holds only this pointer. It will be removed in the release
after the one that ships it.
