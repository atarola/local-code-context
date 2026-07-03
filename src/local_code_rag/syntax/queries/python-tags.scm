; SPDX-License-Identifier: MIT
;
; Derived from:
;   Aider-AI/aider
;   aider/queries/tree-sitter-languages/python-tags.scm
;   commit: e3d5eaf388ae8da925fbd0d3577adbc07fdae16d
;
; Original query/grammar project:
;   tree-sitter/tree-sitter-python
;   commit or release: 0.25.0
;
; Local modifications:
;   - normalized definition-name captures to @name
;   - added module capture
;   - added explicit method captures
;   - added import captures
;   - added constant capture
;   - added decorated-definition handling
;
; This file remains available under the MIT License.

(module) @definition.module

(class_definition
  name: (identifier) @name) @definition.class

(function_definition
  name: (identifier) @name) @definition.function

(class_definition
  body: (block
    [
      (function_definition
        name: (identifier) @name) @definition.method
      (decorated_definition
        (function_definition
          name: (identifier) @name) @definition.method)
    ]))

(call
  function: [
    (identifier) @name
    (attribute attribute: (identifier) @name)
  ]) @reference.call

(import_statement) @reference.import
(import_from_statement) @reference.import

(assignment) @definition.constant
