; Verilog/SystemVerilog tags query
;
; Node types from tree-sitter-verilog grammar.

; Module declarations
(module_declaration (module_header (simple_identifier) @name)) @definition.class

; Function declarations
(function_declaration (function_body_declaration (function_identifier (_ (simple_identifier) @name)))) @definition.function

; Task declarations
(task_declaration (task_body_declaration (task_identifier (_ (simple_identifier) @name)))) @definition.function

; Module instantiations (with parameter list)
(module_instantiation (simple_identifier) @name) @reference.call

; Checker instantiations (module/interface inst without params)
(checker_instantiation (checker_identifier (simple_identifier) @name)) @reference.call

; UDP instantiations
(udp_instantiation (simple_identifier) @name) @reference.call

; System task/function calls
(system_tf_call (system_tf_identifier) @name) @reference.call

; Include directives
(include_compiler_directive) @reference.import
