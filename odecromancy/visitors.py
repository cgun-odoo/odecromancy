import ast
from collections import defaultdict
from typing import List, Tuple, Dict, Any, Optional
from .models import FieldValue, MethodValue


class FieldCollector(ast.NodeVisitor):
    """
    Collects field and method names used within a function definition,
    organizing them by the model they belong to.
    """

    def __init__(
        self,
        current_model_name: str,
        definitions_map: Dict[str, Any],
        *,
        default_context_stack: Optional[List[Tuple[str, str]]] = None,
    ):
        self.definitions_map = definitions_map
        self.used_fields_by_model = defaultdict(set)
        self.used_methods_by_model = defaultdict(set)

        initial_stack = (
            default_context_stack if default_context_stack is not None else []
        )
        self.model_context_stack = initial_stack + [(current_model_name, "self")]
        self._current_model_name = current_model_name

    def get_results(self):
        results = defaultdict(lambda: {"fields": [], "methods": []})
        for model, fields in self.used_fields_by_model.items():
            results[model]["fields"] = sorted(list(fields))
        for model, methods in self.used_methods_by_model.items():
            results[model]["methods"] = sorted(list(methods))
        return dict(results)

    def _get_field_info(self, model_name, name) -> Optional[FieldValue]:
        model_value = self.definitions_map.get(model_name)
        if not model_value:
            return None
        if name in model_value.fields:
            return model_value.fields[name]
        for inherited_model_name in model_value.inherited_models.keys():
            field = self._get_field_info(inherited_model_name, name)
            if field:
                return field
        return None

    def _get_method_info(self, model_name, name) -> Optional[MethodValue]:
        model_value = self.definitions_map.get(model_name)
        if not model_value:
            return None
        if name in model_value.methods:
            return model_value.methods[name]
        for inherited_model_name in model_value.inherited_models.keys():
            method = self._get_method_info(inherited_model_name, name)
            if method:
                return method
        return None

    def _get_context_for_name(self, name):
        for model_name, obj_name in reversed(self.model_context_stack):
            if obj_name == name:
                return model_name
        return None

    def _track_field_usage(self, model_name, field_name):
        self.used_fields_by_model[model_name].add(field_name)

    def _track_method_usage(self, model_name, method_name):
        self.used_methods_by_model[model_name].add(method_name)

    def visit_Attribute(self, node):
        is_method = False
        if isinstance(node.value, ast.Name):
            obj_name = node.value.id
            name = node.attr
            model_name = self._get_context_for_name(obj_name)
            if model_name:
                field_value = self._get_field_info(model_name, name)
                method_value = self._get_method_info(model_name, name)

                if field_value:
                    self._track_field_usage(model_name, name)
                    if field_value.attributes.get("comodel_name"):
                        self.model_context_stack.append(
                            (
                                field_value.attributes["comodel_name"],
                                f"{obj_name}.{name}",
                            )
                        )
                elif method_value:
                    self._track_method_usage(model_name, name)
                    is_method = True

        elif isinstance(node.value, (ast.Attribute, ast.Call)):
            self.visit(node.value)
            if len(self.model_context_stack) > 1:
                prev_model_name, prev_obj_name = self.model_context_stack[-1]
                name = node.attr
                field_value = self._get_field_info(prev_model_name, name)
                method_value = self._get_method_info(prev_model_name, name)

                if field_value:
                    self._track_field_usage(prev_model_name, name)
                    if field_value.attributes.get("comodel_name"):
                        self.model_context_stack.append(
                            (
                                field_value.attributes["comodel_name"],
                                f"{prev_obj_name}.{name}",
                            )
                        )
                    # Pop context if we traversed past a non-relational field or a finished chain
                    should_pop = (
                        not field_value.attributes.get("comodel_name")
                        and (prev_obj_name.startswith("self.") or "." in prev_obj_name)
                        and prev_obj_name not in ("mapped_call", "env_call")
                    )
                    if should_pop:
                        self.model_context_stack.pop()

                elif method_value:
                    self._track_method_usage(prev_model_name, name)
                    is_method = True

        self.generic_visit(node)

    def visit_Subscript(self, node):
        # Handle self.env['model']
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "env"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            self.model_context_stack.append((node.slice.value, "env_call"))
        self.generic_visit(node)

    def visit_Lambda(self, node):
        if not self.model_context_stack:
            self.generic_visit(node)
            return

        model_name = self.model_context_stack[-1][0]
        lambda_arg_name = None
        if node.args.posonlyargs:
            lambda_arg_name = node.args.posonlyargs[0].arg
        elif node.args.args:
            lambda_arg_name = node.args.args[0].arg

        if lambda_arg_name:
            self.model_context_stack.append((model_name, lambda_arg_name))
            self.visit(node.body)
            self.model_context_stack.pop()
        else:
            self.generic_visit(node)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            self.visit(node.func.value)

            if self.model_context_stack:
                model_name, _ = self.model_context_stack[-1]
                if self._get_method_info(model_name, method_name):
                    self._track_method_usage(model_name, method_name)
                if method_name in ("filtered", "filtered_domain", "mapped", "search"):
                    self._handle_orm_context_change(node, model_name, method_name)

                if self.model_context_stack and self.model_context_stack[-1][1] in (
                    "mapped_call",
                    "env_call",
                ):
                    self.model_context_stack.pop()
        self.generic_visit(node)

    def _handle_orm_context_change(self, node, model_name, method_name):
        if (
            method_name in ("mapped", "filtered", "filtered_domain")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            field_name_str = node.args[0].value
            self._track_field_usage(model_name, field_name_str)

            if method_name == "mapped":
                field_value = self._get_field_info(model_name, field_name_str)
                if field_value and field_value.attributes.get("comodel_name"):
                    new_model = field_value.attributes["comodel_name"]
                    self.model_context_stack.append((new_model, "mapped_call"))

    def _visit_comprehension(self, node):
        original_stack_size = len(self.model_context_stack)
        self.visit(node.elt)
        for generator in node.generators:
            self.visit(generator)
        while len(self.model_context_stack) > original_stack_size:
            self.model_context_stack.pop()

    def visit_SetComp(self, node):
        self._visit_comprehension(node)

    def visit_ListComp(self, node):
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node):
        self._visit_comprehension(node)

    def visit_comprehension(self, node):
        self.visit(node.iter)
        if self.model_context_stack:
            iter_model = self.model_context_stack[-1][0]
            if isinstance(node.target, ast.Name):
                target_name = node.target.id
                self.model_context_stack.append((iter_model, target_name))
                for if_clause in node.ifs:
                    self.visit(if_clause)

    def visit_For(self, node):
        stack_size_before_iter = len(self.model_context_stack)
        self.visit(node.iter)

        if self.model_context_stack:
            iter_model = self.model_context_stack[-1][0]
            if isinstance(node.target, ast.Name):
                self.model_context_stack.append((iter_model, node.target.id))
                for stmt in node.body:
                    self.visit(stmt)
                self.model_context_stack.pop()

        while len(self.model_context_stack) > stack_size_before_iter:
            if self.model_context_stack[-1][1] == "self":
                break
            self.model_context_stack.pop()

        if node.orelse:
            for stmt in node.orelse:
                self.visit(stmt)

    def visit_Assign(self, node):
        self.visit(node.value)
        target_name = None
        for target in node.targets:
            if isinstance(target, ast.Name):
                target_name = target.id

        if target_name and isinstance(node.value, ast.Call):
            call_node = node.value
            if isinstance(call_node.func, ast.Attribute) and call_node.func.attr in (
                "search",
                "browse",
                "create",
            ):
                model_accessor = call_node.func.value
                if (
                    isinstance(model_accessor, ast.Subscript)
                    and isinstance(model_accessor.value, ast.Attribute)
                    and model_accessor.value.attr == "env"
                    and isinstance(model_accessor.slice, ast.Constant)
                    and isinstance(model_accessor.slice.value, str)
                ):
                    self.model_context_stack.append(
                        (model_accessor.slice.value, target_name)
                    )
