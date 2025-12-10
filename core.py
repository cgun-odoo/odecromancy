import ast
import logging
from xml.etree import ElementTree
from typing import Dict, List, Set, Optional

from models import ModelValue, FieldValue, MethodValue
from visitors import FieldCollector
from utils import (
    find_modules,
    extract_init,
    extract_manifest,
    get_str_from_constant_or_name,
    get_decorator_name,
)


class OdooAnalyzer:
    def __init__(self):
        self.definitions_map: Dict[str, ModelValue] = {}
        self.python_file_paths: List[str] = []
        self.xml_file_paths: List[str] = []

    def scan_directory(self, path: str):
        modules = find_modules(path)
        for module_path in modules:
            self.python_file_paths.extend(extract_init(module_path))
            self.xml_file_paths.extend(extract_manifest(module_path))

        # Filter tests and migrations
        self.python_file_paths = [
            p
            for p in self.python_file_paths
            if "migration" not in p and "tests" not in p
        ]

    def analyze(self):
        # 1. Parse Python files to build models
        for file_path in self.python_file_paths:
            self._initialize_definitions_map(file_path)

        # 2. Link relationships
        self._fill_definitions_map()

        # 3. Parse XML files for usage
        for xml_path in self.xml_file_paths:
            try:
                self._fill_fields_from_xml_file(xml_path)
            except ElementTree.ParseError as pe:
                logging.warning(f"XML Parse Error in {xml_path}: {pe}")

        # 4. Parse Python Method Bodies for usage
        for file_path in self.python_file_paths:
            self._fill_field_usage_in_methods(file_path)

    def report(self):
        for model_name, model in self.definitions_map.items():
            if not model.fields and not model.methods:
                continue

            unused_fields = [
                (field.name, field.definition_paths)
                for field in model.fields.values()
                if field.unused_percentage >= 100
            ]
            unused_methods = [
                (method.name, method.definition_paths)
                for method in model.methods.values()
                if method.unused_percentage >= 100
            ]

            if not unused_fields or not unused_methods:
                continue
            logging.info(model_name)
            if unused_fields:
                logging.info("FIELDS: ")
                for field, definition_paths in unused_fields:
                    logging.info(f"\t{field}")
                    for path in definition_paths:
                        logging.info(f"\t\t{path}")
            if unused_methods:
                logging.info("METHODS: ")
                for method, definition_paths in unused_methods:
                    logging.info(f"\t{method}")
                    for path in definition_paths:
                        logging.info(f"\t\t{path}")
            logging.info("-" * 22)

    # --- Internal Logic Methods ---

    def _initialize_definitions_map(self, file_path):
        try:
            with open(file_path) as file:
                node = ast.parse(file.read())
                for class_ in [
                    elem for elem in node.body if isinstance(elem, ast.ClassDef)
                ]:
                    model = self._initialize_model(class_)
                    if not model:
                        continue

                    fields = self._find_fields(class_, file_path)
                    model.fields = {f.name: f for f in fields}

                    methods = self._find_methods(class_, file_path)
                    model.methods = {m.name: m for m in methods}

                    if self.definitions_map.get(model.name):
                        self.definitions_map[model.name] |= model
                    else:
                        self.definitions_map[model.name] = model
        except Exception as e:
            logging.error(f"Failed to process {file_path}: {e}")

    def _initialize_model(self, class_def: ast.ClassDef) -> Optional[ModelValue]:
        name_node = None
        inherit_node = None
        composite_inherits_node = None

        for assign in (e for e in class_def.body if isinstance(e, ast.Assign)):
            target_id = getattr(assign.targets[0], "id", None)
            if target_id == "_name":
                name_node = assign.value
            elif target_id == "_inherit":
                inherit_node = assign.value
            elif target_id == "_inherits":
                composite_inherits_node = assign.value

        inherited_model_names = set()
        model_name = None

        if name_node and isinstance(name_node, ast.Constant):
            model_name = name_node.value
        elif inherit_node and isinstance(inherit_node, ast.Constant):
            # _inherit with no _name implies extension of that model
            return ModelValue(inherit_node.value)
        elif (
            inherit_node
            and isinstance(inherit_node, ast.List)
            and len(inherit_node.elts) == 1
        ):
            return ModelValue(inherit_node.elts[0].value)
        else:
            return None  # Complex or dynamic names not supported yet

        # Collect inherited names
        if inherit_node and isinstance(inherit_node, ast.Constant):
            inherited_model_names.add(inherit_node.value)
        elif inherit_node and isinstance(inherit_node, ast.List):
            for elem in inherit_node.elts:
                if isinstance(elem, ast.Constant):
                    inherited_model_names.add(elem.value)

        if composite_inherits_node and isinstance(composite_inherits_node, ast.Dict):
            for key in composite_inherits_node.keys:
                if isinstance(key, ast.Constant):
                    inherited_model_names.add(key.value)

        # Resolve inherited models from existing map
        inherited_models = {}
        for inherited_name in inherited_model_names:
            if inherited_name == model_name:
                continue

            if inherited_name in self.definitions_map:
                inherited_models[inherited_name] = self.definitions_map[inherited_name]
            else:
                # Placeholder for now
                placeholder = ModelValue(inherited_name)
                self.definitions_map[inherited_name] = placeholder
                inherited_models[inherited_name] = placeholder

        return ModelValue(model_name, inherited_models=inherited_models)

    def _find_fields(self, class_def: ast.ClassDef, file_path: str) -> Set[FieldValue]:
        fields = set()
        for assign in (e for e in class_def.body if isinstance(e, ast.Assign)):
            if not (
                isinstance(assign.value, ast.Call)
                and isinstance(assign.value.func, ast.Attribute)
                and isinstance(assign.value.func.value, ast.Name)
                and assign.value.func.value.id == "fields"
            ):
                continue

            field_name = assign.targets[0].id
            definition_path = f"{file_path}:{assign.lineno}"
            field = FieldValue(field_name, definition_paths={definition_path})

            field_type = assign.value.func.attr
            if field_type in ["Many2one", "One2many", "Many2many"]:
                field.attributes["relational"] = True
                args = assign.value.args
                keywords = assign.value.keywords

                # Extract Comodel
                if args:
                    field.attributes["comodel_name"] = get_str_from_constant_or_name(
                        args[0]
                    )

                comodel_kw = next(
                    (k for k in keywords if k.arg == "comodel_name"), None
                )
                if comodel_kw:
                    field.attributes["comodel_name"] = get_str_from_constant_or_name(
                        comodel_kw.value
                    )

                # Extract Inverse
                if field_type == "One2many":
                    if len(args) > 1:
                        field.attributes["inverse_name"] = (
                            get_str_from_constant_or_name(args[1])
                        )
                    inv_kw = next(
                        (k for k in keywords if k.arg == "inverse_name"), None
                    )
                    if inv_kw:
                        field.attributes["inverse_name"] = (
                            get_str_from_constant_or_name(inv_kw.value)
                        )

                # Related
                related_kw = next((k for k in keywords if k.arg == "related"), None)
                if related_kw:
                    field.attributes["related"] = get_str_from_constant_or_name(
                        related_kw.value
                    )

            fields.add(field)
        return fields

    def _find_methods(
        self, class_def: ast.ClassDef, file_path: str
    ) -> Set[MethodValue]:
        methods = set()
        common_orm_decorators = {
            "depends",
            "constrains",
            "onchange",
            "ondelete",
            "model_create_multi",
        }
        ignored_methods = {"create", "write", "default_get", "unlink", "copy"}

        for func_def in (e for e in class_def.body if isinstance(e, ast.FunctionDef)):
            decorators = {get_decorator_name(d) for d in func_def.decorator_list}

            if not decorators.isdisjoint(common_orm_decorators):
                continue
            if func_def.name in ignored_methods:
                continue
            if func_def.name.startswith(("_compute", "_inverse", "_default")):
                continue

            methods.add(
                MethodValue(
                    func_def.name,
                    func_def,
                    definition_paths={f"{file_path}:{func_def.lineno}"},
                )
            )
        return methods

    def _fill_definitions_map(self):
        # 1. Resolve Children
        for model in self.definitions_map.values():
            for inherited_name in model.inherited_models.keys():
                model.inherited_models[inherited_name] = self.definitions_map.get(
                    inherited_name
                )

        for model_name, model in self.definitions_map.items():
            for parent_model in model.inherited_models.values():
                if parent_model:
                    parent_model.child_models[model_name] = model

        # 2. Process Relations & Related Fields
        for model in self.definitions_map.values():
            for field in model.fields.values():
                # Fix relational fields inheriting comodel
                if field.attributes.get("relational") and not field.attributes.get(
                    "comodel_name"
                ):
                    for parent in model.inherited_models.values():
                        parent_field = self._get_field_definition(
                            parent.name, field.name
                        )
                        if parent_field and parent_field.attributes.get("comodel_name"):
                            field.attributes["comodel_name"] = parent_field.attributes[
                                "comodel_name"
                            ]
                            break

                # Fix related fields
                if field.attributes.get("related") and not field.attributes.get(
                    "comodel_name"
                ):
                    field.attributes["comodel_name"] = (
                        self._get_comodel_from_related_path(
                            model, field.attributes["related"]
                        )
                    )

    def _get_field_definition(self, model_name, field_name) -> Optional[FieldValue]:
        model = self.definitions_map.get(model_name)
        if not model:
            return None
        if field_name in model.fields:
            return model.fields[field_name]
        for inherited in model.inherited_models.keys():
            res = self._get_field_definition(inherited, field_name)
            if res:
                return res
        return None

    def _get_method_definition(self, model_name, method_name) -> Optional[MethodValue]:
        model = self.definitions_map.get(model_name)
        if not model:
            return None
        if method_name in model.methods:
            return model.methods[method_name]
        for inherited in model.inherited_models.keys():
            res = self._get_method_definition(inherited, method_name)
            if res:
                return res
        return None

    def _get_comodel_from_related_path(self, model, related_path) -> Optional[str]:
        parts = related_path.split(".")
        current_model = model
        for part in parts:
            if not current_model:
                return None
            current_model.field_used(part, 100)  # Mark intermediate fields as used

            field_def = self._get_field_definition(current_model.name, part)
            if not field_def:
                return None

            if field_def.attributes.get("comodel_name"):
                current_model = self.definitions_map.get(
                    field_def.attributes["comodel_name"]
                )
            else:
                # Recurse if the field itself is related
                if field_def.attributes.get("related"):
                    sub_comodel_name = self._get_comodel_from_related_path(
                        current_model, field_def.attributes["related"]
                    )
                    current_model = self.definitions_map.get(sub_comodel_name)
                else:
                    return None
        return current_model.name if current_model else None

    def _fill_fields_from_xml_file(self, file_path):
        tree = ElementTree.parse(file_path)
        for record in tree.findall(".//record"):
            model_attr = record.get("model")
            if model_attr == "ir.ui.view":
                self._parse_view_arch(record)
            elif model_attr in ["ir.actions.server", "ir.cron"]:
                self._parse_xml_data_code(record)

    def _get_xml_record_model(self, record_node) -> str:
        # Check <field name="model">
        model_field = record_node.find(".//field[@name='model']")
        if model_field is not None and model_field.text:
            return model_field.text
        # Check <field name="model_id" ref="model_x_y"/>
        model_id = record_node.find(".//field[@name='model_id']")
        if model_id is not None and model_id.get("ref"):
            ref = model_id.get("ref")
            if "." in ref:
                ref = ref.split(".")[1]
            if ref.startswith("model_"):
                return ref[6:].replace("_", ".")
        return ""

    def _extract_fields_from_xml_attributes(self, model_name, element) -> Set[str]:
        used = set()
        for attr in ["invisible", "readonly", "required"]:
            val = element.get(attr)
            if val:
                try:
                    visitor = FieldCollector(model_name, self.definitions_map)
                    visitor.visit(ast.parse(val.lstrip()))
                    used.update(
                        visitor.get_results().get(model_name, {}).get("fields", [])
                    )
                except (SyntaxError, ValueError):
                    pass
        return used

    def _parse_view_arch(self, record_node):
        model_name = self._get_xml_record_model(record_node)
        model = self.definitions_map.get(model_name)
        if not model:
            return

        arch = record_node.find(".//field[@name='arch']")
        if arch is None:
            return

        # Parse fields
        processed_fields = set()
        for field in arch.findall(".//field[@name]"):
            fname = field.get("name")
            fdef = self._get_field_definition(model_name, fname)
            if fname in processed_fields:
                continue

            used = {fname}
            used |= self._extract_fields_from_xml_attributes(model_name, field)

            # Sub-views (one2many)
            if fdef and (field.find(".//tree") or field.find(".//form")):
                comodel = fdef.attributes.get("comodel_name")
                if comodel and comodel in self.definitions_map:
                    sub_root = field.find(".//tree") or field.find(".//form")
                    for sub_field in sub_root.findall(".//field[@name]"):
                        sub_fname = sub_field.get("name")
                        self.definitions_map[comodel].field_used_in_view(sub_fname)
                        # Inverse logic
                        if fdef.attributes.get("inverse_name"):
                            self.definitions_map[comodel].field_used_in_view(
                                fdef.attributes["inverse_name"]
                            )

            model.field_used_multi(list(used), 100)
            processed_fields.update(used)

        # Parse buttons
        for btn in arch.findall(".//button[@name]"):
            method_name = btn.get("name")
            if self._get_method_definition(model_name, method_name):
                model.method_used(method_name, 100)
                used_attrs = self._extract_fields_from_xml_attributes(model_name, btn)
                model.field_used_multi(list(used_attrs), 100)

    def _parse_xml_data_code(self, record_node):
        model_name = self._get_xml_record_model(record_node)
        model = self.definitions_map.get(model_name)
        if not model:
            return

        code_field = record_node.find(".//field[@name='code']")
        if code_field is not None and code_field.text:
            context = [
                (model_name, "records"),
                (model_name, "record"),
                (model_name, "model"),
            ]
            fc = FieldCollector(
                model_name, self.definitions_map, default_context_stack=context
            )
            fc.visit(ast.parse(code_field.text.lstrip()))
            self._mark_usage_from_collector(fc.get_results())

    def _fill_field_usage_in_methods(self, file_path):
        try:
            with open(file_path) as f:
                node = ast.parse(f.read())

            for class_ in [e for e in node.body if isinstance(e, ast.ClassDef)]:
                model = self._initialize_model(class_)
                if not model or model.name not in self.definitions_map:
                    continue

                real_model_name = model.name

                for func in [e for e in class_.body if isinstance(e, ast.FunctionDef)]:
                    # Process decorators (simplified)
                    for dec in func.decorator_list:
                        if isinstance(dec, ast.Call) and dec.args:
                            for arg in dec.args:
                                if isinstance(arg, ast.Constant):
                                    self.definitions_map[real_model_name].field_used(
                                        arg.value, 25
                                    )

                    # Process Body
                    fc = FieldCollector(real_model_name, self.definitions_map)
                    fc.visit(func)
                    self._mark_usage_from_collector(fc.get_results())

        except Exception as e:
            logging.error(f"Error parsing methods in {file_path}: {e}")

    def _mark_usage_from_collector(self, results):
        for model_name, usage in results.items():
            if model_name not in self.definitions_map:
                continue
            model = self.definitions_map[model_name]
            for f in usage["fields"]:
                model.field_used(f, 100)
            for m in usage["methods"]:
                model.method_used(m, 100)
