import os
import ast
import sys
import logging
from collections import namedtuple
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError
from ast_function_visitor import FieldCollector
from field_value import ModelValue, FieldValue, MethodValue

definitions_map = {}
python_file_paths = []
xml_file_paths = []


def _find_model_name(class_):
    model = None
    for assign in (elem for elem in class_.body if isinstance(elem, ast.Assign)):
        target_id = getattr(assign.targets[0], "id", None)
        if target_id not in ("_inherit", "_name"):
            continue
        if isinstance(assign.value, ast.Constant):
            model = assign.value.value
        elif isinstance(assign.value, ast.List):
            model = assign.value.elts[0].value
        if model:
            break
    return model


def _get_str_from_constant_or_name(element):
    if hasattr(element, "value"):
        return element.value
    elif hasattr(element, "id"):
        return element.id


def _process_node_for_chain(chain, node, is_method=False):
    ChainNode = namedtuple("ChainNode", ["value", "is_method"])
    if isinstance(node, ast.Call):
        return _process_node_for_chain(chain, node.func, is_method=True)
    elif isinstance(node, ast.Attribute):
        chain.append(ChainNode(node.attr, is_method))
        return _process_node_for_chain(chain, node.value)
    elif isinstance(node, ast.Name):
        return chain, node.id
    else:
        logging.info("Found a different kind in chain", node)
        return chain, ""


def extract_chain_from_call(call_node: ast.Call):
    chain = []
    chain, name = _process_node_for_chain(chain, call_node)

    return list(reversed(chain)), name


def _find_fields(class_, file_path):
    fields = set()
    for assign in (elem for elem in class_.body if isinstance(elem, ast.Assign)):
        if not isinstance(assign.value, ast.Call):
            continue
        # we need to check if `assign.value.func.value.id` is equal to `fields`
        if not hasattr(assign.value.func, "value"):
            continue
        if not hasattr(assign.value.func.value, "id"):
            continue
        if assign.value.func.value.id != "fields":
            continue

        definition_path = f"{file_path}:{assign.lineno}"
        field = FieldValue(assign.targets[0].id, definition_paths={definition_path})
        if assign.value.func.attr in ["Many2one", "One2many", "Many2many"]:
            field.attributes["relational"] = True
            if assign.value.args:
                comodel_arg = assign.value.args[0]
                field.attributes["comodel_name"] = _get_str_from_constant_or_name(
                    comodel_arg
                )
            if assign.value.func.attr == "One2many":
                if len(assign.value.args) > 1:
                    inverse_name_arg = assign.value.args[1]
                    field.attributes["inverse_name"] = _get_str_from_constant_or_name(
                        inverse_name_arg
                    )
            comodel_keyword = next(
                (kw for kw in assign.value.keywords if kw.arg == "comodel_name"), None
            )
            inverse_name_keyword = next(
                (kw for kw in assign.value.keywords if kw.arg == "inverse_name"), None
            )
            related_keyword = next(
                (kw for kw in assign.value.keywords if kw.arg == "related"), None
            )
            if comodel_keyword:
                field.attributes["comodel_name"] = _get_str_from_constant_or_name(
                    comodel_keyword.value
                )
            if inverse_name_keyword:
                field.attributes["inverse_name"] = _get_str_from_constant_or_name(
                    inverse_name_keyword.value
                )
            elif related_keyword:
                field.attributes["related"] = _get_str_from_constant_or_name(
                    related_keyword.value
                )
        fields.add(field)
    return fields


def get_decorator_name(decorator_node):
    if isinstance(decorator_node, ast.Name):
        return decorator_node.id
    elif isinstance(decorator_node, ast.Call):
        return get_decorator_name(decorator_node.func)
    elif isinstance(decorator_node, ast.Attribute):
        return decorator_node.attr
    return None


def _find_methods(
    class_,
    file_path,
):  # Methods that might not be used. (action methods helped functions)
    methods = set()
    for functionDef in (
        elem for elem in class_.body if isinstance(elem, ast.FunctionDef)
    ):
        common_orm_decorators = [
            "depends",
            "constrains",
            "onchange",
            "ondelete",
            "model_create_multi",
        ]
        decorators = list(
            map(
                lambda decorator: get_decorator_name(decorator),
                functionDef.decorator_list,
            )
        )
        if any(decorator in decorators for decorator in common_orm_decorators):
            continue
        if functionDef.name in ["create", "write", "default_get", "unlink", "copy"]:
            continue
        if (
            functionDef.name.startswith("_compute")
            or functionDef.name.startswith("_inverse")
            or functionDef.name.startswith("_default")
        ):
            continue
        methods.add(
            MethodValue(
                functionDef.name,
                functionDef,
                definition_paths={f"{file_path}:{functionDef.lineno}"},
            )
        )
    return methods


def initialize_model(class_):
    name_node = None
    inherit_node = None
    composite_inherits_node = None
    for assign in (elem for elem in class_.body if isinstance(elem, ast.Assign)):
        target_id = getattr(assign.targets[0], "id", None)
        if target_id == "_name":
            name_node = assign.value
        elif target_id == "_inherit":
            inherit_node = assign.value
        elif target_id == "_inherits":
            composite_inherits_node = assign.value

    inherited_model_names = set()

    # TODO: Things like _name = model('char') are not supported
    if name_node and not isinstance(name_node, ast.Constant):
        return False

    if name_node:
        model_name = name_node.value
    elif inherit_node and isinstance(inherit_node, ast.Constant):
        return ModelValue(inherit_node.value)
    elif (
        inherit_node
        and isinstance(inherit_node, ast.List)
        and len(inherit_node.elts) == 1
    ):
        return ModelValue(inherit_node.elts[0].value)
    else:
        return False
    if inherit_node and isinstance(inherit_node, ast.Constant):
        inherited_model_names.add(inherit_node.value)
    elif inherit_node and isinstance(inherit_node, ast.List):
        for elem in inherit_node.elts:
            inherited_model_names.add(elem.value)

    if composite_inherits_node and isinstance(composite_inherits_node, ast.Dict):
        for key in composite_inherits_node.keys:
            inherited_model_names.add(key.value)

    inherited_models = {}
    for inherited_model_name in inherited_model_names:
        if inherited_model_name == model_name:
            continue
        if inherited_model_name in definitions_map:
            inherited_models[inherited_model_name] = definitions_map[
                inherited_model_name
            ]
        else:
            inherited_model = ModelValue(inherited_model_name)
            inherited_models[inherited_model_name] = inherited_model
            definitions_map[inherited_model_name] = inherited_model
    return ModelValue(model_name, inherited_models=inherited_models)


def initialize_definitions_map(file_path):
    with open(file_path) as file:
        node = ast.parse(file.read())
        for class_ in [elem for elem in node.body if isinstance(elem, ast.ClassDef)]:
            model = initialize_model(class_)
            if not model:
                logging.warning("Class does not correspond to model %s", file_path)
                continue
            model.fields = {
                field.name: field for field in _find_fields(class_, file_path)
            }
            model.methods = {
                method.name: method for method in _find_methods(class_, file_path)
            }
            if definitions_map.get(model.name):
                definitions_map[model.name] |= model
            else:
                definitions_map[model.name] = model


def _process_dot_notation(model_name, dotted_str):
    field_names = dotted_str.split(".")
    current_model = model_name
    model_field_map = {}
    for field_name in field_names:
        if not current_model:
            return model_field_map
        if not model_field_map.get(current_model):
            model_field_map[current_model] = []
        if current_model not in definitions_map:
            return model_field_map
        field_def = definitions_map[current_model].fields.get(field_name)
        if not field_def:
            logging.info("Field cannot be found in model", field_name, current_model)
            continue
        model_field_map[current_model].append(field_name)
        current_model = field_def.attributes.get("comodel_name")
    return model_field_map


def _get_model_method_from_call(model_name, ast_call: ast.Call):
    """Given an ast.Call node return the last model and method name and fields that are used from each model
    For call node: self.intervention_ids.recovery_id._get_retro_move() Returns:
    {
        'guarantee.guarantee':['intervention_ids'],
        'guarantee.intervention': ['recovery_id'],
        '': ['account_move_line_ids']
    } in

    """
    AttributeUsedInCall = namedtuple(
        "AttributeUsedInCall", ["name", "confidence", "is_method"]
    )
    chain, name = extract_chain_from_call(ast_call)
    current_model = model_name
    used_attributes = {current_model: []}
    for item in chain:
        used_attributes[current_model] = used_attributes.get(current_model) or []
        used_attributes.get(current_model).append(
            AttributeUsedInCall(item.value, 100, item.is_method)
        )
        if field_definition := _get_field_definition(current_model, item.value):
            comodel_name = field_definition.attributes.get("comodel_name")
            current_model = comodel_name or ""
        # TODO: Methods it might be possible to extract the return value
        # elif method_definition := _get_method_definition(current_model, item.value):
        #     current_model = ''
        else:
            current_model = ""

    return used_attributes


def _get_model_method_from_constant(model_name, constant_node: ast.Constant):
    field_name = constant_node.value
    if "." in field_name:
        return _process_dot_notation(model_name, field_name)
    else:
        return {model_name: [field_name]}


def _get_fields_from_method_return(model_name, method_name):
    method = definitions_map[model_name].methods.get(method_name)
    fields = {model_name: []}
    if not method or not method.function_definitions:
        return []
    for function_definition in method.function_definitions:
        for return_node in (
            node
            for node in ast.walk(function_definition)
            if isinstance(node, ast.Return)
        ):
            if not return_node.value:
                return []
            returned_value = return_node.value
            if isinstance(returned_value, ast.List):
                for element in returned_value.elts:
                    used_fields = {}
                    if isinstance(element, ast.Constant):
                        used_fields = _get_model_method_from_constant(
                            model_name, element
                        )
                    elif isinstance(element, ast.Call):
                        used_fields = _get_model_method_from_call(model_name, element)
                    elif isinstance(element, ast.Starred):
                        used_fields = _get_model_method_from_call(
                            model_name, element.value
                        )
                    for key, value in used_fields.items():
                        fields[key] = fields.get(key, []) + value

            elif isinstance(returned_value, ast.Constant):
                return _get_model_method_from_constant(model_name, element)
            elif isinstance(returned_value, ast.Call):
                return _get_model_method_from_call(model_name, returned_value)
            elif isinstance(returned_value, ast.Starred):
                return _get_model_method_from_call(model_name, returned_value.value)
    return fields


def _process_field_in_decorator(model_name, field_name):
    if "." in field_name:
        model_field_map = _process_dot_notation(model_name, field_name)
        for model, fields in model_field_map.items():
            definitions_map[model].field_used_multi(fields, 25)
    else:
        definitions_map[model_name].field_used(field_name, 25)


def _process_decorators(model_name: str, function_definition: ast.FunctionDef):
    decorator_list = function_definition.decorator_list
    if not decorator_list:
        return
    for decorator in decorator_list:
        attributes_in_decorator = {}
        if not isinstance(decorator, ast.Call) or not decorator.args:
            continue
        for arg in decorator.args:
            if isinstance(arg, ast.Constant):  # It's a string representing a field
                attributes_in_decorator = _get_model_method_from_constant(
                    model_name, arg
                )

            elif isinstance(arg, ast.Name):  # A method name is used in the decorator
                method_name = arg.id
                definitions_map[model_name].method_used(method_name, 100)
                attributes_in_decorator = _get_fields_from_method_return(
                    model_name, method_name
                )

        for attribute_model, attribute_list in attributes_in_decorator.items():
            if not attribute_model:
                # TODO: Sometimes we can't get the model, in this case we should look for these attributes in relevant models. And reduce usage with low confidence.
                continue
            for attribute in attribute_list:
                if hasattr(attribute, "is_method") and attribute.is_method:
                    definitions_map[attribute_model].method_used(
                        attribute.name, attribute.confidence
                    )
                else:
                    field_name = (
                        attribute.name if hasattr(attribute, "name") else attribute
                    )
                    usage_confidence = (
                        attribute.confidence
                        if hasattr(attribute, "confidence")
                        else 100
                    )
                    definitions_map[attribute_model].field_used(
                        field_name, usage_confidence
                    )


def _mark_usage_in_methods(results):
    for model_name, definitions in results.items():
        model = definitions_map.get(model_name)
        if not model:
            continue
        for field_name in definitions["fields"]:
            model.field_used(field_name, 100)
        for method_name in definitions["methods"]:
            model.method_used(method_name, 100)
            # IF a method is used is it used in all inheriting classes? (it is)


def fill_field_usage_in_methods(file_path):
    with open(file_path) as file:
        node = ast.parse(file.read())
        for class_ in [elem for elem in node.body if isinstance(elem, ast.ClassDef)]:
            model_name = _find_model_name(class_)
            if not model_name:
                continue
            for functionDef in [
                elem for elem in class_.body if isinstance(elem, ast.FunctionDef)
            ]:
                # _process_decorators(model_name, functionDef) TODO: Should be revisited with FieldCollector
                field_collector = FieldCollector(model_name, definitions_map)
                field_collector.visit(functionDef)
                _mark_usage_in_methods(field_collector.get_results())


def _get_field_definition(model_name, field_name):
    model_definition = definitions_map.get(model_name)
    if not model_definition:
        return None
    if model_definition.fields.get(field_name):
        return model_definition.fields.get(field_name)
    else:
        for inherited_model in model_definition.inherited_models.keys():
            field = _get_field_definition(inherited_model, field_name)
            if field:
                return field
    return None


def _get_method_definition(model_name, method_name):
    model_definition = definitions_map.get(model_name)
    if not model_definition:
        return None
    if model_definition.methods.get(method_name):
        return model_definition.methods.get(method_name)
    else:
        for inherited_model in model_definition.inherited_models.values():
            if inherited_model.methods.get(method_name):
                return inherited_model.methods.get(method_name)
    return None


def _extract_fields_from_xml_attributes(model_name, xml_field):
    attribute_names = ["invisible", "readonly", "required"]
    attribute_fields = set()
    for attribute in attribute_names:
        if not (attribute_val := xml_field.get(attribute)):
            continue
        else:
            try:
                attribute_parsed = ast.parse(attribute_val.lstrip())
                function_visitor = FieldCollector(model_name, definitions_map)
                function_visitor.visit(attribute_parsed)
                attribute_fields |= set(
                    function_visitor.get_results().get(model_name, [])
                )
            except SyntaxError:  # TODO: country_id != %(base.br)d
                continue

    return attribute_fields


def parse_fields_in_view_arch(model_def, arch_field, processed_fields):
    model_name = model_def.name
    if model_name not in definitions_map:
        logging.warning("Model: %s not in definitions", model_name)
    for field in arch_field.findall(".//field[@name]"):
        field_name = field.get("name")
        field_definition = _get_field_definition(model_name, field_name)
        if field_name in processed_fields or not field_definition:
            continue
        used_fields = {field_name}
        used_fields |= _extract_fields_from_xml_attributes(model_name, field)
        if field_name != "arch" and field.find(".//tree") or field.find(".//form"):
            comodel_name = field_definition.attributes.get("comodel_name")
            sub_root = field.find(".//tree") or field.find(".//form")
            for sub_field in sub_root.findall(".//field[@name]"):
                if comodel_name not in definitions_map:
                    logging.warning("Comodel %s not defined yet", comodel_name)
                definitions_map[comodel_name].field_used_in_view(sub_field.get("name"))
                sub_field_attributes = _extract_fields_from_xml_attributes(
                    comodel_name, sub_field
                )
                for attribute_field in sub_field_attributes:
                    definitions_map[comodel_name].field_used_in_view(attribute_field)
                if field_definition.attributes.get("inverse_name"):
                    definitions_map[comodel_name].field_used_in_view(
                        field_definition.attributes.get("inverse_name")
                    )
                processed_fields.add(sub_field)
        model_def.field_used_multi(used_fields, 100)
        processed_fields |= used_fields


def parse_methods_in_buttons(model_def, arch_field):
    model_name = model_def.name
    for button in arch_field.findall(".//button[@name]"):
        method_name = button.get("name")
        method_definition = _get_method_definition(model_name, method_name)
        if not method_definition:
            continue
        used_fields = _extract_fields_from_xml_attributes(model_name, button)
        model_def.method_used(method_name, 100)
        model_def.field_used_multi(used_fields, 100)


def parse_view_arch(model_def, record_root):
    processed_fields = set()
    for arch_field in record_root.find(".//field[@name='arch']") or []:
        parse_fields_in_view_arch(model_def, arch_field, processed_fields)
        parse_methods_in_buttons(model_def, arch_field)


def _get_technical_model_name_from_xml_id(xml_model_string: str):
    model_part = (
        xml_model_string.split(".")[1] if "." in xml_model_string else xml_model_string
    )
    return model_part[6:].replace("_", ".")  # Remove model_ , replace _ with .


def find_model_name_from_xml_record(record_root):
    model_field_element = record_root.find(".//field[@name='model']")
    if getattr(model_field_element, "text", None):
        return model_field_element.text

    model_field_element = record_root.find(".//field[@name='model_id']")
    if "ref" in getattr(model_field_element, "attrib", {}):
        ref = model_field_element.attrib.get("ref", "")
        return _get_technical_model_name_from_xml_id(ref)
    # TODO: Problem with sriw_reporting.value_reduction
    return ""


def _parse_code_in_data(model_definition, record_root):
    code_field = record_root.find(".//field[@name='code']")
    code = getattr(code_field, "text", "")
    default_context_stack = [
        (model_definition.name, "records"),
        (model_definition.name, "record"),
        (model_definition.name, "model"),
    ]
    if not code:
        return
    fc = FieldCollector(
        model_definition.name,
        definitions_map,
        default_context_stack=default_context_stack,
    )
    fc.visit(ast.parse(code.lstrip()))
    _mark_usage_in_methods(fc.get_results())


def parse_xml_data(model_definition, record_root):
    _parse_code_in_data(model_definition, record_root)
    # TODO: Parse record rules


def fill_fields_from_xml_file(file_path):
    tree = ElementTree.parse(file_path)
    tree_root = tree.getroot()
    for record_root in tree_root.findall(".//record"):
        record_model = record_root.attrib.get("model")
        if record_model not in ["ir.ui.view", "ir.actions.server", "ir.cron"]:
            continue
        model_name = find_model_name_from_xml_record(record_root)
        model_definition = definitions_map.get(model_name)
        if not model_definition or not isinstance(model_definition, ModelValue):
            logging.warning(
                "Somehow definitions map has a wrong value for model %s", model_name
            )
            continue
        if record_model == "ir.ui.view":
            parse_view_arch(model_definition, record_root)
        elif record_model in ["ir.actions.server", "ir.cron"]:
            parse_xml_data(model_definition, record_root)


def find_modules(main_path):
    modules = []
    for root, dirs, files in os.walk(main_path):
        if "__manifest__.py" not in files:
            continue
        modules.append(root)

    return modules


def extract_init(module_path):
    init_path = f"{module_path}/__init__.py"
    if os.path.isfile(init_path):
        with open(init_path, "r") as init_file:
            init_node = ast.parse(init_file.read())
            for import_from in [
                elem for elem in init_node.body if isinstance(elem, ast.ImportFrom)
            ]:
                for alias in [
                    alias for alias in import_from.names if isinstance(alias, ast.alias)
                ]:
                    if os.path.isfile(f"{module_path}/{alias.name}.py"):
                        python_file_paths.append(f"{module_path}/{alias.name}.py")
                    elif os.path.isdir(f"{module_path}/{alias.name}"):
                        extract_init(f"{module_path}/{alias.name}")


def extract_manifest(module_path):
    manifest_path = f"{module_path}/__manifest__.py"
    with open(manifest_path, "r") as manifest_file:
        manifest = ast.literal_eval(manifest_file.read())
        for data in manifest.get("data", []):
            if data.endswith(".xml"):
                xml_file_paths.append(f"{module_path}/{data}")


def fill_definitions_map_with_children():
    for model_name, model in definitions_map.items():
        for inherited_model_name in model.inherited_models.keys():
            model.inherited_models[inherited_model_name] = definitions_map.get(
                inherited_model_name
            )

    for model_name, model in definitions_map.items():
        for parent_name, parent in model.inherited_models.items():
            if parent:
                parent.child_models[model_name] = model


def get_comodel_from_related_field(model, field):
    if field.attributes.get("related") and not field.attributes.get("comodel_name"):
        related_path = field.attributes["related"]
        related_nodes = related_path.split(".")
        current_model_name = model.name
        for related_node in related_nodes:
            current_model = definitions_map.get(current_model_name)
            if not current_model:
                return
            current_model.field_used(related_node, 100)
            related_field = _get_field_definition(current_model_name, related_node)
            if not related_field:
                return
            if related_field.attributes.get("comodel_name"):
                current_model_name = related_field.attributes["comodel_name"]
            else:
                current_model_name = get_comodel_from_related_field(
                    current_model, related_field
                )
        return current_model_name


def process_related_fields():
    for model_name, model in definitions_map.items():
        for field_name, field in model.fields.items():
            field.attributes["comodel_name"] = field.attributes.get(
                "comodel_name"
            ) or get_comodel_from_related_field(model, field)


def process_relational_fields():
    for model_name, model in definitions_map.items():
        for field_name, field in model.fields.items():
            if field.attributes.get("relational") and not field.attributes.get(
                "comodel_name"
            ):
                inherit_field = None
                for inherit_model_name, inherit_model in model.inherited_models.items():
                    inherit_field = _get_field_definition(
                        inherit_model_name, field_name
                    )
                    if inherit_field and inherit_field.attributes.get("comodel_name"):
                        break
                if not inherit_field or not inherit_field.attributes.get(
                    "comodel_name"
                ):
                    continue
                field.attributes["comodel_name"] = inherit_field.attributes[
                    "comodel_name"
                ]


def fill_definitions_map():
    fill_definitions_map_with_children()
    process_relational_fields()
    process_related_fields()


def main():
    if len(sys.argv) != 2:
        print("Usage: python find_dead_code.py <path_to_odoo_project>")
        sys.exit(1)

    file_path = sys.argv[1]

    modules = find_modules(file_path)
    for module_path in modules:
        extract_init(module_path)
        extract_manifest(module_path)

    for python_file_path in python_file_paths:
        if "migration" in python_file_path or "tests" in python_file_path:
            continue
        initialize_definitions_map(python_file_path)
    fill_definitions_map()

    for xml_file_path in xml_file_paths:
        try:
            fill_fields_from_xml_file(xml_file_path)
        except ParseError as pe:
            logging.exception(pe, xml_file_path)

    for python_file_path in python_file_paths:
        if "migration" in python_file_path or "tests" in python_file_path:
            continue
        fill_field_usage_in_methods(python_file_path)

    for model_name, model in definitions_map.items():
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
        if not unused_fields and not unused_methods:
            continue
        print(model_name)
        if unused_fields:
            print("FIELDS: ")
            for field, definition_paths in unused_fields:
                print(f"\t{field}")
                for definition_path in definition_paths:
                    print(f"\t\t{definition_path}")
        if unused_methods:
            print("METHODS: ")
            for method, definition_paths in unused_methods:
                print(f"\t{method}")
                for definition_path in definition_paths:
                    print(f"\t\t{definition_path}")
        print("----------------------")


if __name__ == "__main__":
    main()
