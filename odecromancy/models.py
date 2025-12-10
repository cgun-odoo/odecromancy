import ast
import logging
from typing import Dict, List, Optional, Set


class FieldValue:

    def __init__(self, field_name: str, definition_paths: Set[str] = None, **kwargs):
        self.name = field_name
        self.attributes = kwargs
        self.unused_percentage = 100
        self.definition_paths = definition_paths or set()

    def reduce_certainty(self):
        self.unused_percentage -= 25

    def __hash__(self):
        return hash(self.name)

    def __ior__(self, other: "FieldValue"):
        for key, value in other.attributes.items():
            if value:
                self.attributes[key] = other.attributes[key]
        self.definition_paths |= other.definition_paths
        return self


class MethodValue:

    def __init__(
        self,
        method_name: str,
        ast_function: ast.FunctionDef,
        definition_paths: Set[str] = None,
    ):
        self.name = method_name
        self.function_definitions = [ast_function]
        self.unused_percentage = 100
        self.dependencies: List[FieldValue] = []
        self.definition_paths = definition_paths or set()

    def reduce_certainty(self):
        self.unused_percentage -= 25

    def __ior__(self, other: "MethodValue"):
        if not isinstance(other, MethodValue):
            raise TypeError("MethodValue can only be IORed with MethodValue")
        self.function_definitions += other.function_definitions
        self.unused_percentage = min(self.unused_percentage, other.unused_percentage)
        self.definition_paths |= other.definition_paths
        return self

    def __hash__(self):
        return hash(self.name)

    def __repr__(self):
        return f"Method {self.name}"


class ModelValue:
    def __init__(
        self,
        model_name: str,
        inherited_models: Optional[Dict[str, "ModelValue"]] = None,
        child_models: Optional[Dict[str, "ModelValue"]] = None,
    ):
        self.name = model_name
        self.inherited_models: Dict[str, ModelValue] = inherited_models or {}
        self.child_models: Dict[str, ModelValue] = child_models or {}
        self.fields: Dict[str, FieldValue] = {}
        self.methods: Dict[str, MethodValue] = {}

    def __ior__(self, other: "ModelValue"):
        if not isinstance(other, ModelValue):
            raise TypeError("Only models can be IORed")

        for field_name, other_field in other.fields.items():
            if field_name in self.fields:
                self.fields[field_name] |= other_field
            else:
                self.fields[field_name] = other_field

        for method_name, other_method in other.methods.items():
            if method_name in self.methods:
                self.methods[method_name] |= other_method
            else:
                self.methods[method_name] = other_method

        self.inherited_models.update(other.inherited_models)
        self.child_models.update(other.child_models)
        return self

    def __repr__(self):
        return f"Model {self.name}: Fields: {list(self.fields.keys())} Methods: {list(self.methods.keys())}"

    def __hash__(self):
        return hash(self.name)

    def field_used_in_view(self, field_name: str):
        self.field_used(field_name, 100)

    def field_used_multi(self, fields: List[str], confidence: int):
        for field in fields:
            self.field_used(field, confidence)

    def field_used(self, field_name: str, confidence: int):
        if field_name in self.fields:
            field = self.fields[field_name]
            field.unused_percentage = max(field.unused_percentage - confidence, 0)
        else:
            logging.debug(
                "Field {%s} not found in model {%s}. Checking inherits.",
                field_name,
                self.name,
            )
            for name, inherited_model in self.inherited_models.items():
                if field_name in inherited_model.fields:
                    inherited_field = inherited_model.fields[field_name]
                    inherited_field.unused_percentage = max(
                        inherited_field.unused_percentage - confidence, 0
                    )
                    logging.debug(
                        "Field {%s} found in inherited model {%s}", field_name, name
                    )

    def method_used_child(self, method_name: str):
        for child in self.child_models.values():
            if method_name in child.methods:
                child.methods[method_name].unused_percentage = 0
            child.method_used_child(method_name)

    def method_used(self, method_name: str, confidence: int):
        if method_name in self.methods:
            method = self.methods[method_name]
            method.unused_percentage = max(method.unused_percentage - confidence, 0)
            self.method_used_child(method_name)
        else:
            logging.debug(
                "Method {%s} not found in model {%s}. Checking inherits.",
                method_name,
                self.name,
            )
            for name, inherited_model in self.inherited_models.items():
                if method_name in inherited_model.methods:
                    inherited_method = inherited_model.methods[method_name]
                    inherited_method.unused_percentage = max(
                        inherited_method.unused_percentage - confidence, 0
                    )
                    logging.debug(
                        "Method {%s} found in inherited model {%s}", method_name, name
                    )
