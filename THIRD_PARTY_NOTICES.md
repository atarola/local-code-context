# Third-Party Notices

## Python Tree-sitter tag query

`src/local_code_rag/syntax/queries/python-tags.scm`

Derived from the Python tag query distributed by Aider:

- Project: Aider
- Repository: Aider-AI/aider
- Path: aider/queries/tree-sitter-languages/python-tags.scm
- Commit: `e3d5eaf388ae8da925fbd0d3577adbc07fdae16d`
- License: MIT

The Aider query is itself derived from or associated with the
`tree-sitter-python` grammar:

- Project: tree-sitter-python
- Repository: tree-sitter/tree-sitter-python
- Commit or release: `0.25.0`
- License: MIT

Local modifications:

- normalized capture names for the generic extraction engine
- added module capture
- added explicit method capture
- added import captures
- added decorated-definition handling

Copyright and license notices are retained under the terms of the MIT License.
