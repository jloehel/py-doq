import re
from typing import Dict, List

import parso
from parso.python.tree import Class, ExprStmt


def sort_by_lineno(key):
    if len(key["defs"]):
        return key["defs"][0]["start_lineno"]

    return key["start_lineno"]


def get_return_type(line):
    matched = re.search(r"\)(.*)->(.*):", line)
    if matched:
        return_type = matched.group()[1:]
        return_type = re.sub(r":$", "", return_type).strip()
        return_type = return_type.replace("->", "").strip()
        return return_type

    return None


def get_start_col(line, start_col):
    lines = line.split("\n")
    for v in lines:
        if v == "":
            # Maybe line break only row
            continue

        index = v.find("def")
        if index == start_col:
            return start_col

    # Maybe async keyword exists.
    # parso does not parse by parso and it's known issue.
    return start_col - 6  # 'async '


def parse_return_type(code, start_lineno, end_lineno):
    lines = code.strip().split("\n")
    lineno = end_lineno - start_lineno
    if lineno == 0:
        # Signature end
        return get_return_type(lines[0])

    for line in lines[0 : lineno + 1]:
        return_type = get_return_type(line)
        if return_type:
            return return_type

    return None


def parse_defs(
    module,
    omissions=None,
    ignore_exception=False,
    ignore_yield=False,
    ignore_init=False,
):  # noqa C901
    if omissions is None:
        omissions = []

    results = []
    for d in module.iter_funcdefs():
        is_doc_exists = True if d.get_doc_node() else False

        (start_lineno, start_col) = d.start_pos
        (end_lineno, end_col) = d.end_pos

        code = d.get_code()
        start_col = get_start_col(code, start_col)

        name = d.name.value
        params = []
        is_classmethod = any("@classmethod" in d.get_code() for d in d.get_decorators())
        is_overload = any("@overload" in d.get_code() for d in d.get_decorators())

        if is_overload:
            # TODO: Save overloads in a dictionary before and use the
            #       type information from the overloads for the actual
            #       implemented function.
            continue

        for i, p in enumerate(d.get_params()):
            if is_classmethod and i == 0:
                # Ignore first argument if method is `@classmethod`.
                continue

            if p.name.value in omissions and i == 0:
                # Method's first variable is maybe `self`.
                continue

            arguments = {"argument": None, "annotation": None, "default": None}
            arguments["argument"] = p.name.value
            if p.annotation:
                arguments["annotation"] = p.annotation.get_code().strip()
            if p.default:
                arguments["default"] = p.default.get_code().strip()

            params.append(arguments)

        return_type = None
        if d.children[3].value == "->":
            return_type = d.children[4].get_code().strip()

        yields = []
        if ignore_yield is False:
            for y in d.iter_yield_exprs():
                yields.append(y.children[1].get_first_leaf().value)

        exceptions = []
        if ignore_exception is False:
            for e in d.iter_raise_stmts():
                if hasattr(e, "children"):
                    exceptions.append(e.children[1].get_first_leaf().get_code().strip())
                else:  # bare raise
                    exceptions.append("")

        results.append(
            {
                "name": name,
                "params": params,
                "return_type": return_type,
                "start_lineno": start_lineno,
                "start_col": start_col,
                "end_lineno": end_lineno,
                "end_col": end_col,
                "is_doc_exists": is_doc_exists,
                "exceptions": exceptions,
                "yields": yields,
            },
        )

        nested = parse_defs(
            d,
            ignore_exception=ignore_exception,
            ignore_yield=ignore_yield,
            ignore_init=ignore_init,
        )
        if len(nested):
            results += nested

        nested = parse_classdefs(d)
        if len(nested):
            results += nested

    return results


def get_attributes(
    expr: ExprStmt, scope: Dict[str, Dict[str, str]]
) -> List[Dict[str, str]]:
    """Extracts attribute information from an expression statement.

    It is a list because something like that is also possible:

    .. code-block:: python

        class Test:
            attr1 = attr2 = 1

    Args:
        expr (ExprStmt): The python expression
        scope (Dict[str, Dict[str, str]]): The current scope.

    Returns:
        List[Dict[str, str]]: A list of attributes
    """
    attributes = []
    # TODO: If the value of the stmt is a function
    #       we should treat the attribute like a
    #       method. Right now this is not possible
    #       because with the current we can not
    #       resolve variables. It would be necessary
    #       to add a basic interpreter for this.
    for attr_name in expr.get_defined_names():
        attr_name = attr_name.value
        if expr.children[1].type == "annassign":
            attr_annotation = expr.children[1].children[1].get_code().strip()
        else:
            attr_annotation = None
        if attr_name in scope:
            if scope[attr_name]["annotation"] is None and attr_annotation is not None:
                scope[attr_name]["annotation"] = attr_annotation
            if (
                scope[attr_name]["annotation"] is not None
                and attr_annotation is not None
            ):
                scope[attr_name]["annotation"] += f" | {attr_annotation}"
        else:
            scope[attr_name] = {
                "name": attr_name,
                "annotation": attr_annotation,
            }
            attributes.append(scope[attr_name])
    return attributes


def get_instance_attributes(
    _class: Class, scope: Dict[str, Dict[str, str]]
) -> List[Dict[str, str]]:
    """Get all instance attributes from a class

    Args:
        _class (Class): The python class
        scope (Dict[str, Dict[str, str]]): The current scope

    Returns:
        List[Dict[str, str]]: Returns all instance attributes.
    """
    attributes = []
    for constructor in [
        method for method in _class.iter_funcdefs() if method.name.value == "__init__"
    ]:
        if any(
            [
                "@overload" in decorator.get_code()
                for decorator in constructor.get_decorators()
            ]
        ):
            continue
        for stmt in constructor._search_in_scope("expr_stmt"):
            if "self" in stmt.children[0].get_code():
                attributes += get_attributes(stmt, scope)
    return attributes


def get_class_attributes(
    _class: Class, scope: Dict[str, Dict[str, str]]
) -> List[Dict[str, str]]:
    """Get all class attributes.

    Args:
        _class (Class): The python class
        scope (Dict[str, Dict[str, str]]): The current scope

    Returns:
        List[Dict[str, str]]: Returns all class attributes.
    """
    attributes = []
    for stmt in _class._search_in_scope("expr_stmt"):
        attributes += get_attributes(stmt, scope)
    return attributes


def parse_classdefs(
    module, ignore_exception=False, ignore_yield=False, ignore_init=False
):
    results = []

    for _class in module.iter_classdefs():
        is_doc_exists = True if _class.get_doc_node() else False

        (start_lineno, start_col) = _class.start_pos
        (end_lineno, end_col) = _class.end_pos

        name = _class.name.value
        class_scope = {}
        defs = parse_defs(
            _class,
            omissions=["self"],
            ignore_exception=ignore_exception,
            ignore_yield=ignore_yield,
            ignore_init=ignore_init,
        )
        results.append(
            {
                "name": name,
                "defs": defs,
                "attributes": {
                    "class": get_class_attributes(_class, class_scope),
                    "instance": get_instance_attributes(_class, class_scope),
                },
                "start_lineno": start_lineno,
                "start_col": start_col,
                "end_lineno": end_lineno,
                "end_col": end_col,
                "is_doc_exists": is_doc_exists,
            },
        )

        nested = parse_classdefs(
            _class, ignore_exception=ignore_exception, ignore_yield=ignore_yield
        )
        if len(nested):
            results += nested

    results.sort(key=sort_by_lineno)

    return results


def parse(
    code, omissions=None, ignore_exception=False, ignore_yield=False, ignore_init=False
):
    m = parso.parse(code)
    results = []
    if "class" in code:
        results = parse_classdefs(
            m,
            ignore_exception=ignore_exception,
            ignore_yield=ignore_yield,
            ignore_init=ignore_init,
        )

    results += parse_defs(
        m,
        omissions=omissions,
        ignore_exception=ignore_exception,
        ignore_yield=ignore_yield,
        ignore_init=ignore_init,
    )

    return results
