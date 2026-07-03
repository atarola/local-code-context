; SPDX-License-Identifier: MIT
; Derived from Aider-AI/aider/aider/queries/tree-sitter-languages/python-tags.scm
; and the tree-sitter-python grammar (MIT).
; Upstream reference:
; https://github.com/Aider-AI/aider/blob/main/aider/queries/tree-sitter-languages/python-tags.scm

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
