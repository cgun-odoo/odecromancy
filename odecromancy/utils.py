import ast
import os
import logging
from collections import namedtuple
from typing import List, Tuple, Any, Optional

ChainNode = namedtuple("ChainNode", ["value", "is_method"])


def get_str_from_constant_or_name(element: Any) -> Optional[str]:
    if isinstance(element, ast.Constant):
        return element.value
    elif isinstance(element, ast.Name):
        return element.id
    # Fallback for older python versions or specific node types if attributes exist
    if hasattr(element, "value") and not isinstance(element, ast.AST):
        return element.value
    if hasattr(element, "id"):
        return element.id
    return None


def get_decorator_name(decorator_node: ast.AST) -> Optional[str]:
    if isinstance(decorator_node, ast.Name):
        return decorator_node.id
    elif isinstance(decorator_node, ast.Call):
        return get_decorator_name(decorator_node.func)
    elif isinstance(decorator_node, ast.Attribute):
        return decorator_node.attr
    return None


def _process_node_for_chain(
    chain: List[ChainNode], node: ast.AST, is_method: bool = False
) -> Tuple[List[ChainNode], str]:
    if isinstance(node, ast.Call):
        return _process_node_for_chain(chain, node.func, is_method=True)
    elif isinstance(node, ast.Attribute):
        chain.append(ChainNode(node.attr, is_method))
        return _process_node_for_chain(chain, node.value)
    elif isinstance(node, ast.Name):
        return chain, node.id
    else:
        logging.debug(f"Found a different kind in chain: {type(node)}")
        return chain, ""


def extract_chain_from_call(call_node: ast.Call) -> Tuple[List[ChainNode], str]:
    chain: List[ChainNode] = []
    chain, name = _process_node_for_chain(chain, call_node)
    return list(reversed(chain)), name


def find_modules(main_path: str) -> List[str]:
    modules = []
    for root, dirs, files in os.walk(main_path):
        if "__manifest__.py" in files:
            modules.append(root)
    return modules


def extract_init(module_path: str) -> List[str]:
    python_files = []
    init_path = os.path.join(module_path, "__init__.py")
    if os.path.isfile(init_path):
        try:
            with open(init_path, "r") as init_file:
                init_node = ast.parse(init_file.read())
                for elem in init_node.body:
                    if isinstance(elem, ast.ImportFrom):
                        for alias in elem.names:
                            possible_file = os.path.join(
                                module_path, f"{alias.name}.py"
                            )
                            possible_dir = os.path.join(module_path, alias.name)

                            if os.path.isfile(possible_file):
                                python_files.append(possible_file)
                            elif os.path.isdir(possible_dir):
                                python_files.extend(extract_init(possible_dir))
        except Exception as e:
            logging.error(f"Error parsing init file {init_path}: {e}")
    return python_files


def extract_manifest(module_path: str) -> List[str]:
    xml_files = []
    manifest_path = os.path.join(module_path, "__manifest__.py")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r") as manifest_file:
                manifest = ast.literal_eval(manifest_file.read())
                for data in manifest.get("data", []):
                    if data.endswith(".xml"):
                        xml_files.append(os.path.join(module_path, data))
        except Exception as e:
            logging.error(f"Error parsing manifest {manifest_path}: {e}")
    return xml_files
