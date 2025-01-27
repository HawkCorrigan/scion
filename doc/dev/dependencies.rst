.. _external-dependencies:

Dependencies
============

Go
--
Go dependencies are managed as `Go modules <https://golang.org/ref/mod>`_.
Dependencies are controlled by the ``go.mod`` file, and cryptographic hashes of
the dependencies are stored in the ``go.sum`` file.

When building with Bazel, all external dependencies are managed as "remote
repositories" defined in the ``WORKSPACE``.
In our ``WORKSPACE`` file, we load the file ``go_deps.bzl`` which lists all
external dependencies (including transitive dependencies) with exact version
and hash.
This ``go_deps.bzl`` file is **generated** by gazelle from the ``go.mod`` file.

Workflow to modify dependencies
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

To add/remove or update dependencies:

1. Modify ``go.mod``, manually or using e.g. ``go get``.
2. ``make go-mod-tidy``
3. ``make go_deps.bzl``
4. ``make licenses``, to update the licenses with the new dependency
5. ``make gazelle``, to update the build files that depend on the newly added dependency

.. Warning::
  The Go rules for Bazel (rules_go) declares some internally used dependencies.
  These may **silently shadow** the dependency versions declared in
  ``go_deps.bzl``.

  To explicitly override such a dependency version, the corresponding
  ``go_repository`` rule can be moved from ``go_deps.bzl`` to the
  ``WORKSPACE`` file, *before* the call to ``go_rules_dependencies``.
  Refer to the `go_rules documentation on overriding dependencies <https://github.com/bazelbuild/rules_go/blob/master/go/dependencies.rst#overriding-dependencies>`_.


Python
^^^^^^

The Python dependencies are listed in ``requirements.txt`` files. They are generated with Bazel from the
corresponding ``requirements.in`` files.

The Python dependencies are listed in `tools/env/pip3/requirements.txt
<https://github.com/scionproto/scion/blob/master/tools/env/pip3/requirements.txt>`__
and `tools/lint/python/requirements.txt
<https://github.com/scionproto/scion/blob/master/tools/lint/python/requirements.txt>`__.
These files is generated from the corresponding ``requirements.in`` by Bazel. Only
direct dependencies need to be listed; the transitive dependencies are inferred.
The exact command to update ``requirements.txt`` is described in a comment in
the file's header.
