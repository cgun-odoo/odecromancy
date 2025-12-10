import ast
import logging
from typing import List

class FieldValue:

    def __init__(self, field_name, definition_path="", **kwargs):
        self.name = field_name
        self.attributes = kwargs
        self.unused_percentage = 100
        self.definition_path: str = definition_path

    def reduce_certainty(self):
        self.unused_percentage -= 25

    def __hash__(self):
        return hash(self.name)

    def __ior__(self, other):
        for key, value in other.attributes.items():
            if value:
                self.attributes[key] = other.attributes[key]
        return self

class MethodValue:

    def __init__(
        self, method_name: str, ast_function: ast.FunctionDef, definition_path=""
    ):
        self.name = method_name
        self.function_definitions = [ast_function]
        self.unused_percentage = 100
        self.dependencies: List[FieldValue] = []
        self.definition_path: str = definition_path

    def reduce_certainty(self):
        self.unused_percentage -= 25

    def __ior__(self, other):
        if not isinstance(other, MethodValue):
            raise TypeError("MethodValue can only be IORed with MethodValue")
        # A function can be defined multiple times. If it's used all of them are used. We don't care about overriding here.
        self.function_definitions += other.function_definitions
        self.unused_percentage = min(self.unused_percentage, other.unused_percentage)
        return self

    def __hash__(self):
        return hash(self.name)

    def __repr__(self):
        return f"Method {self.name}"

class ModelValue:
    def __init__(self, model_name, inherited_models=None, child_models=None):
        if inherited_models is None:
            inherited_models = {}
        self.name = model_name
        self.fields: dict[str, FieldValue] = {}
        self.methods: dict[str, MethodValue] = {}
        self.inherited_models: dict[str, ModelValue] = inherited_models
        self.child_models: dict[str, ModelValue] = {}

    def __ior__(self, other):
        if not isinstance(other, ModelValue):
            raise TypeError("Only models")

        for field_name, other_field in other.fields.items():
            if field_name in self.fields:
                self.fields[field_name] |= other_field
            else:
                self.fields[field_name] = other_field
        for method_name, other_method in other.methods.items():
            if method_name in self.fields:
                self.methods[method_name] |= other_method
            else:
                self.methods[method_name] = other_method
        self.inherited_models |= other.inherited_models
        self.child_models |= other.child_models
        return self

    def __repr__(self):
        return f"Model {self.name}: Fields: {self.fields} Methods: {self.methods}"

    def __hash__(self):
        return hash(self.name)

    def field_used_in_view(self, field_name):
        self.field_used(field_name, 100)

    def field_used_multi(self, fields: list, confidence):
        for field in fields:
            self.field_used(field, confidence)

    def field_used(self, field_name: str, confidence):
        if field_name in self.fields:
            self.fields.get(field_name).unused_percentage = max(self.fields.get(field_name).unused_percentage - confidence, 0)
        else:
            logging.info('Field {%s} not found in model {%s} maybe because it\'s a default field. Checking inherits', field_name, self.name)
            for name, inherited_model in self.inherited_models.items():
                if field_name in inherited_model.fields:
                    inherited_model.fields.get(field_name).unused_percentage = max(inherited_model.fields.get(field_name).unused_percentage - confidence, 0)
                    logging.info("Field {%s} found in inherited model {%s}", field_name, name)

    def method_used_child(self, method_name):
        for child_name, child in self.child_models.items():
            if method_name in child.methods:
                child.methods.get(method_name).unused_percentage = 0
            child.method_used_child(method_name)

    def method_used(self, method_name: str, confidence):
        if method_name in self.methods:
            self.methods.get(method_name).unused_percentage = max(
                self.methods.get(method_name).unused_percentage - confidence, 0)
            self.method_used_child(method_name)
        else:
            logging.info('Method {%s} not found in model {%s} maybe because it\'s a default field. Checking inherits',
                         method_name, self.name)
            for name, inherited_model in self.inherited_models.items():
                if method_name in inherited_model.methods:
                    inherited_model.methods.get(method_name).unused_percentage = max(
                        inherited_model.methods.get(method_name).unused_percentage - confidence, 0)
                    logging.info("Method {%s} found in inherited model {%s}", method_name, name)
