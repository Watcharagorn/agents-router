# Delegation rules implementation notes

The template is package data, like the existing review policy.
The command reads it with `importlib.resources.files` so it can also work from an installed package.
Python 3.11 documents this API and its support for packaged resources:
[package resources](https://docs.python.org/3.11/library/importlib.resources.html).
The [resource loader source](https://github.com/python/cpython/blob/3.11/Lib/importlib/resources/_common.py)
shows how it obtains the package's resource reader.

The installer edits bytes between one marker pair.
Reading and writing bytes avoids text newline conversion, which would change unrelated CRLF text.
The existing `fsutil.write_private` helper writes the result through a temporary file and a rename.
Backups use exclusive creation so a later install cannot overwrite the first copy.
Python documents the byte APIs in [pathlib](https://docs.python.org/3.11/library/pathlib.html).

No external implementation or code was copied.
The tests cover both newline styles, mixed outside bytes, marker errors and repeat installs.
