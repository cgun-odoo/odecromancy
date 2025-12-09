import ast
from collections import defaultdict


class FieldCollector(ast.NodeVisitor):
    """
    Collects field and method names used within a function definition,
    organizing them by the model they belong to.
    """

    def __init__(
        self, current_model_name, definitions_map, *, default_context_stack=[]
    ):
        self.definitions_map = definitions_map

        self.used_fields_by_model = defaultdict(set)
        self.used_methods_by_model = defaultdict(set)

        self.model_context_stack = default_context_stack + [
            (current_model_name, "self")
        ]
        self._current_model_name = current_model_name

    def get_results(self):
        results = defaultdict(lambda: {"fields": [], "methods": []})

        for model, fields in self.used_fields_by_model.items():
            results[model]["fields"] = sorted(list(fields))

        for model, methods in self.used_methods_by_model.items():
            results[model]["methods"] = sorted(list(methods))

        return dict(results)

    def _get_field_info(self, model_name, name):
        """Retrieves FieldValue for a given model and field name."""
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

    def _get_method_info(self, model_name, name):
        """Retrieves MethodValue for a given model and method name."""
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
        """Finds the model context associated with a variable name."""
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
        pushed_context_name = None

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
                        pushed_context_name = f"{obj_name}.{name}"
                        self.model_context_stack.append(
                            (
                                field_value.attributes["comodel_name"],
                                pushed_context_name,
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
                        pushed_context_name = f"{prev_obj_name}.{name}"
                        self.model_context_stack.append(
                            (
                                field_value.attributes["comodel_name"],
                                pushed_context_name,
                            )
                        )

                    if (
                        not field_value.attributes.get("comodel_name")
                        and prev_obj_name.startswith("self.")
                        and prev_obj_name not in ("mapped_call", "env_call")
                    ):
                        self.model_context_stack.pop()
                    elif (
                        not field_value.attributes.get("comodel_name")
                        and "." in prev_obj_name
                    ):
                        self.model_context_stack.pop()

                elif method_value:
                    self._track_method_usage(prev_model_name, name)
                    is_method = True

        self.generic_visit(node)

    def visit_Subscript(self, node):
        """
        Handle dictionary/list access, specifically targeting the self.env['model'] pattern
        to temporarily push the model context for the next call.
        """
        if (
            isinstance(node.value, ast.Attribute)
            and node.value.attr == "env"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
        ):
            if isinstance(node.slice, ast.Constant):
                model_name = node.slice.value

                if isinstance(model_name, str):
                    self.model_context_stack.append((model_name, "env_call"))

        self.generic_visit(node)

    def visit_Lambda(self, node):
        """
        Handle field/method usage inside a lambda expression.
        The context for the lambda arguments must be pushed before visiting the body.
        """
        model_name = self.model_context_stack[-1][0]

        if node.args.posonlyargs:
            lambda_arg_name = node.args.posonlyargs[0].arg
        elif node.args.args:
            lambda_arg_name = node.args.args[0].arg
        else:
            self.generic_visit(node)
            return

        self.model_context_stack.append((model_name, lambda_arg_name))
        self.visit(node.body)
        self.model_context_stack.pop()

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            self.visit(node.func.value)

            model_name, _ = self.model_context_stack[-1]

            if self._get_method_info(model_name, method_name):
                self._track_method_usage(model_name, method_name)

            if method_name in ("filtered", "filtered_domain", "mapped", "search"):
                self._handle_orm_context_change(node, model_name, method_name)

            if self.model_context_stack[-1][1] in ("mapped_call", "env_call"):
                self.model_context_stack.pop()
        self.generic_visit(node)

    def _handle_orm_context_change(self, node, model_name, method_name):
        """Helper to track argument usage in ORM methods."""
        if (
            method_name == "mapped"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            field_name_str = node.args[0].value
            self._track_field_usage(model_name, field_name_str)

            field_value = self._get_field_info(model_name, field_name_str)

            if field_value and field_value.attributes.get("comodel_name"):
                new_model = field_value.attributes["comodel_name"]
                self.model_context_stack.append((new_model, "mapped_call"))

    def visit_SetComp(self, node):
        self._visit_comprehension(node)

    def visit_ListComp(self, node):
        self._visit_comprehension(node)

    def visit_GeneratorExp(self, node):
        self._visit_comprehension(node)

    def _visit_comprehension(self, node):
        original_stack_size = len(self.model_context_stack)
        self.visit(node.elt)

        for generator in node.generators:
            self.visit(generator)

        while len(self.model_context_stack) > original_stack_size:
            self.model_context_stack.pop()

    def visit_comprehension(self, node):
        """
        Handles the 'for target in iter' part of the comprehension.
        This is essentially a simplified version of visit_For.
        """
        self.visit(node.iter)

        iter_model = self.model_context_stack[-1][0]

        if isinstance(node.target, ast.Name):
            target_name = node.target.id

            self.model_context_stack.append((iter_model, target_name))

            for if_clause in node.ifs:
                self.visit(if_clause)

    def visit_For(self, node):
        stack_size_before_iter = len(self.model_context_stack)

        self.visit(node.iter)

        iter_model = self.model_context_stack[-1][0]

        if isinstance(node.target, ast.Name):
            target_name = node.target.id

            self.model_context_stack.append((iter_model, target_name))

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
                model_accessor_node = call_node.func.value

                if (
                    isinstance(model_accessor_node, ast.Subscript)
                    and isinstance(model_accessor_node.value, ast.Attribute)
                    and model_accessor_node.value.attr == "env"
                    and isinstance(model_accessor_node.value.value, ast.Name)
                    and model_accessor_node.value.value.id == "self"
                    and isinstance(model_accessor_node.slice, ast.Constant)
                ):
                    new_model_name = model_accessor_node.slice.value

                    if isinstance(new_model_name, str):
                        self.model_context_stack.append((new_model_name, target_name))
