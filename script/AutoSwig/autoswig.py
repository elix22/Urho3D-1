import argparse
import logging
import os
import re
import subprocess
import sys
from collections import OrderedDict
from contextlib import suppress

from clang.cindex import Config, CursorKind, AccessSpecifier, TypeKind

from walkcpp.generator import Generator
from walkcpp.module import Module
from walkcpp.passes import AstPass, AstAction
from walkcpp.utils import get_fully_qualified_name, is_builtin_type, desugar_type


builtin_to_cs = {
    TypeKind.VOID: 'void',
    TypeKind.BOOL: 'bool',
    TypeKind.CHAR_U: 'byte',
    TypeKind.UCHAR: 'byte',
    # TypeKind.CHAR16,
    # TypeKind.CHAR32,
    TypeKind.USHORT: 'ushort',
    TypeKind.UINT: 'uint',
    TypeKind.ULONG: 'uint',
    TypeKind.ULONGLONG: 'ulong',
    # TypeKind.UINT128,
    TypeKind.CHAR_S: 'char',
    TypeKind.SCHAR: 'char',
    # TypeKind.WCHAR,
    TypeKind.SHORT: 'short',
    TypeKind.INT: 'int',
    TypeKind.LONG: 'int',
    TypeKind.LONGLONG: 'long',
    # TypeKind.INT128,
    TypeKind.FLOAT: 'float',
    TypeKind.DOUBLE: 'double',
    # TypeKind.LONGDOUBLE,
    TypeKind.NULLPTR: 'null'
    # TypeKind.FLOAT128,
    # TypeKind.HALF,
}


def split_identifier(identifier):
    """Splits string at _ or between lower case and uppercase letters."""
    prev_split = 0
    parts = []

    if '_' in identifier:
        parts = [s.lower() for s in identifier.split('_')]
    else:
        for i in range(len(identifier) - 1):
            if identifier[i + 1].isupper():
                parts.append(identifier[prev_split:i + 1].lower())
                prev_split = i + 1
        last = identifier[prev_split:]
        if last:
            parts.append(last.lower())
    return parts


def camel_case(identifier):
    identifier = identifier.strip('_')
    return_string = False
    if isinstance(identifier, str):
        if identifier.isupper() and '_' not in identifier:
            identifier = identifier.lower()
        name_parts = split_identifier(identifier)
        return_string = True
    elif isinstance(identifier, (list, tuple)):
        name_parts = identifier
    else:
        raise ValueError('identifier must be a list, tuple or string.')

    for i in range(len(name_parts)):
        name_parts[i] = name_parts[i][0].upper() + name_parts[i][1:]

    if return_string:
        return ''.join(name_parts)
    return name_parts


def rename_identifier(name, parent_name, is_private):
    name_parts = split_identifier(name)
    parent_parts = split_identifier(parent_name)
    # Remove name prefix if it consists of first letters of parent name words
    try:
        for i, c in enumerate(name_parts[0]):
            if c != parent_parts[i][0]:
                break
        else:
            del name_parts[0]
    except IndexError:
        pass
    name_parts = camel_case(name_parts)
    if is_private:
        name_parts[0] = name_parts[0][0].tolower() + name_parts[0][1:]
        name_parts[0] = '_' + name_parts[0]
    return ''.join(name_parts)


def find_identifier_parent_name(node):
    while node.kind not in (CursorKind.ENUM_DECL, CursorKind.CLASS_DECL, CursorKind.STRUCT_DECL, CursorKind.TRANSLATION_UNIT):
        node = node.parent
    name = node.spelling
    if node.kind == CursorKind.TRANSLATION_UNIT:
        name = os.path.basename(node.spelling)
        name = re.sub('\.h(pp|xx)?$', '', name)
    return name


def read_raw_code(node):
    extent = node.extent
    with open(extent.start.file.name) as fp:
        fp.seek(node.extent.start.offset, os.SEEK_SET)
        return fp.read(node.extent.end_int_data - node.extent.start.offset)


class DefineConstantsPass(AstPass):
    outputs = ['_constants.i']
    cs_code = []
    rx_deconst = re.compile(r'^const ')

    def on_begin(self):
        self.fp = open(os.path.join(self.module.args.output, '_constants.i'), 'w+')
        return True

    def on_end(self):
        self.fp.write('%pragma(csharp) modulecode=%{\n')
        self.fp.writelines(self.cs_code)
        self.fp.write('%}\n')
        self.fp.close()

    def get_constant_value(self, node):
        # Supports only single line constants so far
        ln = read_raw_code(node)
        if '=' in ln:
            ln = re.sub(rf'.*{node.spelling}\s*=\s*(.+);[\n\t\s]*', r'\1', ln)
        else:
            ln = ln.rstrip('\s\n;')
        if ' ' in ln:
            # Complex expression
            ln = None
        return ln

    @AstPass.once
    def visit(self, node, action: AstAction):
        if node.c.spelling.endswith('/MathDefs.h'):
            return False
        if node.kind == CursorKind.VAR_DECL and node.c.semantic_parent.kind == CursorKind.NAMESPACE:
            value = None
            fqn = get_fully_qualified_name(node)
            if fqn.startswith('Urho3D::IsFlagSet') or fqn.startswith('Urho3D::FlagSet') or \
               node.spelling.startswith('E_') or node.spelling.startswith('P_'):
                return False

            type_name = re.sub(self.rx_deconst, '', node.type.spelling)
            idiomatic_name = camel_case(node.spelling)
            if not node.type.is_const_qualified():
                # Non const variables are ignored
                return False

            if node.type.spelling in ('const ea::string', 'const char*'):
                node_expr = node.find_child(kind=CursorKind.UNEXPOSED_EXPR)
                if node_expr:
                    type_name = 'string'
                    value = self.get_constant_value(node_expr)

            elif is_builtin_type(node.type):
                literal = node.find_any_child(kind=CursorKind.INTEGER_LITERAL) or node.find_any_child(kind=CursorKind.FLOATING_LITERAL)
                if literal:
                    value = self.get_constant_value(literal)
                    if value:
                        type_kind = node.type.get_canonical().kind
                        if type_kind in (TypeKind.CHAR16, TypeKind.CHAR32, TypeKind.USHORT, TypeKind.UINT,
                                         TypeKind.ULONG, TypeKind.ULONGLONG, TypeKind.UINT128):
                            value = value.rstrip('Uu') + 'U'
                        elif type_kind == TypeKind.FLOAT:
                            value = value.rstrip('Ff') + 'f'

                        try:
                            type_name = builtin_to_cs[type_kind]
                        except KeyError:
                            value = None

            self.fp.write(f'%ignore {fqn};\n')
            if value:
                # A raw C# constant. Can not use %csconst because swig strings f from float values.
                self.cs_code.append(f'  public const {type_name} {idiomatic_name} = {value};\n')
            else:
                # Convert readonly variable
                if node.c.type.kind == TypeKind.ENUM:
                    enum = 'enum '
                else:
                    enum = ''
                self.fp.write(f'%constant {enum}{type_name} {idiomatic_name} = {fqn};\n')

        elif node.kind in (CursorKind.CXX_METHOD, CursorKind.FUNCTION_DECL, CursorKind.FIELD_DECL, CursorKind.ENUM_CONSTANT_DECL):
            if node.kind in (CursorKind.CXX_METHOD, CursorKind.FUNCTION_DECL):
                if re.match(r'^operator[^\w]+.*$', node.spelling) is not None:
                    return False
            fqn = get_fully_qualified_name(node)
            if '::' in fqn:
                _, name = fqn.rsplit('::', 1)
            else:
                name = fqn

        return True


class DefineRefCountedPass(AstPass):
    outputs = ['_refcounted.i']
    parent_classes = {}

    def on_end(self):
        fp = open(os.path.join(self.module.args.output, '_refcounted.i'), 'w+')

        for cls_name in self.parent_classes.keys():
            if self.is_subclass_of(cls_name, 'Urho3D::RefCounted'):
                assert isinstance(cls_name, str)
                fp.write(f'URHO3D_REFCOUNTED({cls_name});\n')

        fp.close()

    def is_subclass_of(self, class_name, base_name):
        if class_name == base_name:
            return True
        subclasses = self.parent_classes[class_name]
        if base_name in subclasses:
            return True
        for subclass in subclasses:
            if subclass in self.parent_classes:
                if self.is_subclass_of(subclass, base_name):
                    return True
        return False

    @AstPass.once
    def visit(self, node, action: AstAction):
        if node.kind == CursorKind.CLASS_DECL or node.kind == CursorKind.STRUCT_DECL:
            for base in node.find_children(kind=CursorKind.CXX_BASE_SPECIFIER):
                base = base.type.get_declaration()
                super_fqn = get_fully_qualified_name(node)
                sub_fqn = get_fully_qualified_name(base)

                try:
                    bases_set = self.parent_classes[super_fqn]
                except KeyError:
                    bases_set = self.parent_classes[super_fqn] = set()
                # Create tree of base classes
                bases_set.add(sub_fqn)

        return True


class DefinePropertiesPass(AstPass):
    outputs = ['_properties.i']
    renames = {
        'Urho3D::Variant': {
            'GetType': 'GetVariantType'
        }
    }
    interfaces = [
        'Urho3D::Octant',
        'Urho3D::GPUObject',
    ]
    pod_types = [
        'ea::string',
        'Urho3D::Color',
        'Urho3D::Rect',
        'Urho3D::IntRect',
        'Urho3D::Vector2',
        'Urho3D::IntVector2',
        'Urho3D::Vector3',
        'Urho3D::IntVector3',
        'Urho3D::Vector4',
        'Urho3D::Matrix3',
        'Urho3D::Matrix3x4',
        'Urho3D::Matrix4',
        'Urho3D::Quaternion',
        'Urho3D::Plane',
        'Urho3D::BoundingBox',
        'Urho3D::Sphere',
        'Urho3D::Ray',
        'Urho3D::StringHash',
    ]

    class Property:
        name = None
        access = 'public'
        getter = None
        setter = None
        getter_access = 'private'
        setter_access = 'private'
        type = ''

        def __init__(self, name):
            self.name = name

    def access_to_str(self, m):
        return 'public' if m.access == AccessSpecifier.PUBLIC else 'protected'

    def on_file_begin(self, file_path):
        if os.path.abspath(file_path).startswith(self.module.args.input):
            try:
                dir_name = os.path.abspath(file_path)[len(self.module.args.input):].strip('/').split('/')[0].lower()
            except:
                return False
            self.fp = open(os.path.join(self.module.args.output, f'_properties_{dir_name}.i'), 'a+')
            return True
        else:
            return False

    def on_file_end(self, file_path):
        file_name = self.fp.name
        self.fp.close()
        if os.stat(file_name).st_size == 0:
            os.unlink(file_name)

    def sort_getters_and_setters(self, methods):
        sorted_methods = OrderedDict()

        for m in methods:
            if m.is_virtual_method() or m.is_pure_virtual_method() or m.is_static_method() or m.access == AccessSpecifier.PRIVATE or m.parent.is_abstract_record() or get_fully_qualified_name(m.parent) in self.interfaces:
                continue

            basename = re.sub('^Get|Set', '', m.spelling, flags=re.IGNORECASE)
            if not basename:
                continue

            if any([m.spelling == m2.spelling and m != m2 for m2 in methods]):
                continue

            if not basename.startswith('Is') and len(list(m.parent.find_children(spelling=basename))):
                continue

            try:
                prop = sorted_methods[basename]
            except KeyError:
                prop = self.Property(basename)

            num_children = len(list(m.find_children(kind=CursorKind.PARM_DECL)))
            if m.spelling.startswith('Set') and num_children == 1:
                argument_type = m.find_child(kind=CursorKind.PARM_DECL).type
                if prop.getter:
                    if prop.getter.type.get_result() != argument_type:
                        del sorted_methods[basename]
                        continue

                prop.setter = m
                prop.setter_access = self.access_to_str(m)
                prop.type = argument_type

            elif (m.spelling.startswith('Get') or m.spelling.startswith('Is')) and num_children == 0:
                if prop.setter:
                    if prop.setter.find_child(kind=CursorKind.PARM_DECL).type != m.type.get_result():
                        del sorted_methods[basename]
                        continue

                prop.getter = m
                prop.getter_access = self.access_to_str(m)
                prop.type = m.type.get_result()

            if prop.getter or prop.setter:
                sorted_methods[basename] = prop

        for k, prop in list(sorted_methods.items()):
            if prop.getter is None or prop.getter_access == 'private':
                # No properties without getter
                del sorted_methods[k]
            else:
                if prop.getter_access == prop.access:
                    prop.getter_access = ''
                if prop.setter:
                    if prop.setter_access == prop.access:
                        prop.setter_access = ''
                else:
                    prop.access = prop.getter_access
                    prop.getter_access = ''

        return sorted_methods

    def insert_rename(self, cls_fqn, name):
        try:
            return self.renames[cls_fqn][name]
        except KeyError:
            return name

    def visit(self, getter, action: AstAction):
        if action == AstAction.ENTER:
            if getter.kind == CursorKind.CLASS_DECL:
                self.properties = []
                self.method_attribs = []
        else:
            if getter.kind == CursorKind.CLASS_DECL and len(self.properties):
                self.fp.write(f'%typemap(cscode) {getter.fully_qualified_name} %{{\n')
                self.fp.write('\n'.join(self.properties))
                self.fp.write('\n%}\n')
                self.fp.write('\n'.join(self.method_attribs))
                self.fp.write('\n')
            return

        # Getter must be a method
        if getter.kind != CursorKind.CXX_METHOD:
            return True

        # Getter must be public
        if getter.access != AccessSpecifier.PUBLIC:
            return True

        # Getter must not be static
        if getter.c.is_static_method():
            return True

        # Getter must start with 'Get' or 'Is'
        if getter.spelling.startswith('Get'):
            attribute_name = getter.spelling[3:]
        elif getter.spelling.startswith('Is'):
            attribute_name = getter.spelling
        else:
            return True

        # Getter must not be virtual
        if getter.c.is_virtual_method():
            print(f'Ignore {getter.fully_qualified_name}: virtual')
            return False

        # Getter must have no parameters and a return type
        if len(list(getter.c.get_arguments())) != 0:
            print(f'Ignore {getter.fully_qualified_name}: parameters')
            return True

        # Getter must have no parameters and a return type
        if getter.c.result_type is None or getter.c.result_type.spelling == 'void':
            print(f'Ignore {getter.fully_qualified_name}: return type')
            return True

        # Something with the same name already exists
        for c in getter.parent.find_children():
            if c.access == AccessSpecifier.PUBLIC and camel_case(c.spelling) == attribute_name:
                print(f'Ignore {getter.fully_qualified_name}: attr name taken')
                return False

        # Multiple overloads exist
        if len(list(getter.parent.find_children(spelling=getter.spelling))) > 1:
            print(f'Ignore {getter.fully_qualified_name}: overloads')
            return False

        # We have a getter method now. Find a setter.
        def find_setter(n):
            nonlocal getter
            # Setter must be public
            if n.access != AccessSpecifier.PUBLIC or n.c.kind != CursorKind.CXX_METHOD:
                return False
            if not n.c.is_virtual_method() and not n.c.is_static_method() and n.spelling == f'Set{attribute_name}':
                setter_parameters = list(n.c.get_arguments())
                if len(setter_parameters) != 1:
                    return False
                # Compare parameter type of setter and return type of getter
                return setter_parameters[0].type.spelling == getter.result_type.spelling
            return False
        try:
            setter = next(filter(find_setter, getter.parent.children))
        except StopIteration:
            setter = None
        cstype = getter.c.result_type.get_canonical().spelling
        # Clean up type we will be using to look up typemaps with. SWIG does not use canonical types but rather is
        # matching what it gets directly. These replacements are incomplete.
        cstype = cstype.replace('eastl::basic_string<char, eastl::allocator>', 'eastl::string')
        cstype = cstype.replace(', eastl::allocator>', '>')
        cstype = re.sub(r'eastl::hash_map<(.*), eastl::hash<.*>, eastl::allocator, false>', r'eastl::unordered_map<\1>', cstype)
        cstype = re.sub(r'eastl::hash_map<(.*), eastl::hash<.*>, eastl::allocator, true>', r'eastl::map<\1>', cstype)
        cstype = re.sub(r'Urho3D::FlagSet<(.*), .*>', r'\1', cstype)

        self.properties.append(f'  public $typemap(cstype, {cstype}) {attribute_name} {{')
        self.properties.append(f'    get {{ return {getter.spelling}(); }}')
        self.method_attribs.append(f'%csmethodmodifiers {getter.fully_qualified_name} "private";')
        if setter is not None:
            self.properties.append(f'    set {{ {setter.spelling}(value); }}')
            self.method_attribs.append(f'%csmethodmodifiers {setter.fully_qualified_name} "private";')
        self.properties.append('  }')

        # attribute = '%attribute'
        # # attribute for T (primitive)
        # # attribute2 for const T&
        # # attributeref for T&
        # # attributestring for ea::string, const char*
        # # attributeval for T
        # result = getter.c.result_type.spelling
        # is_flagset = False
        # try:
        #     base_type = desugar_type(getter.c.result_type)
        #     if 'FlagSet' in base_type.spelling:
        #         result = base_type.spelling[16:-7]
        #         is_flagset = True
        # except AttributeError:
        #     pass
        #
        # pointee = getter.c.result_type.get_pointee()
        # if pointee.kind != TypeKind.INVALID:
        #     if pointee.is_const_qualified():
        #         assert pointee.spelling.startswith('const ')
        #         result = pointee.spelling[6:]
        #     elif setter is None and getter.c.result_type.kind not in (TypeKind.LVALUEREFERENCE, TypeKind.POINTER):
        #         attribute = '%attributeref'
        # elif not is_flagset and not getter.c.result_type.is_pod() and not getter.c.is_scoped_enum() and \
        #     not result.startswith('SharedPtr<') and not result.startswith('WeakPtr<') and \
        #     not result.startswith('ea::shared_array') and result not in self.pod_types:
        #     attribute = '%attributeval'
        #
        # def arg(a):
        #     if ',' in a:
        #         return f'%arg({a})'
        #     else:
        #         return a
        #
        # self.fp.write(f'{attribute}({arg(getter.parent.fully_qualified_name)}, {arg(result)}, {arg(attribute_name)}, {arg(getter.spelling)}')
        # if setter is not None:
        #     self.fp.write(f', {arg(setter.spelling)}')
        # self.fp.write(');\n')

        # if node.kind == CursorKind.CLASS_DECL or node.kind == CursorKind.STRUCT_DECL:
        #     methods = list(node.find_children(kind=CursorKind.CXX_METHOD))
        #     methods = list(filter(lambda m: m.spelling.startswith('Get') or
        #                                     m.spelling.startswith('Set') or
        #                                     m.spelling.startswith('Is'), methods))
        #     if not len(methods):
        #         return True
        #
        #     properties = self.sort_getters_and_setters(methods)
        #     if not len(properties):
        #         return True
        #
        #     for basename, prop in properties.items():
        #         if not prop.getter:
        #             continue
        #         # base_type = desugar_type(prop.type)
        #         # if base_type.kind in builtin_to_cs:
        #         #     base_type = builtin_to_cs[base_type.kind]
        #         # else:
        #         #     if 'FlagSet' in base_type.spelling:
        #         #         base_type = base_type.spelling[16:-7]
        #         #     else:
        #         #         base_type = prop.type.spelling
        #         #     base_type = base_type.replace('Urho3D::', 'Urho3DNet.')
        #
        #         if prop.access == prop.getter.access:
        #             prop.getter.access = ''
        #
        #         self.fp.write(f"""
        #         %csmethodmodifiers {get_fully_qualified_name(prop.getter)} "
        #         {prop.access} $typemap(cstype, {prop.type.spelling}) {prop.name} {{
        #             {prop.getter_access} get {{ return __{prop.getter.spelling}(); }}
        #         """)
        #         if prop.setter:
        #             if prop.access == prop.setter.access:
        #                 prop.setter.access = ''
        #             self.fp.write(f"""
        #             {prop.setter_access} set {{ __{prop.setter.spelling}(value); }}
        #             """)
        #         self.fp.write("""
        #         }
        #         private"
        #         """)
        #         self.fp.write(f'%rename(__{prop.getter.spelling}) {get_fully_qualified_name(prop.getter)};\n')
        #         if prop.setter:
        #             self.fp.write(f'%rename(__{prop.setter.spelling}) {get_fully_qualified_name(prop.setter)};\n')
        #             self.fp.write(f'%csmethodmodifiers {get_fully_qualified_name(prop.setter)} "private"\n')

        return True


class FindFlagEnums(AstPass):
    flag_enums = []

    def visit(self, node, action: AstAction):
        if node.kind == CursorKind.STRUCT_DECL:
            if node.spelling == 'IsFlagSet':
                enum = node.children[0].type.spelling
                self.flag_enums.append(enum)

        return True


class CleanEnumValues(AstPass):
    outputs = ['_enums.i']

    def on_begin(self):
        self.fp = open(os.path.join(self.module.args.output, '_enums.i'), 'w+')
        return True

    def on_end(self):
        self.fp.close()

    @AstPass.once
    def visit(self, node, action: AstAction):
        if node.kind == CursorKind.ENUM_DECL:
            if node.type.spelling in FindFlagEnums.flag_enums:
                self.fp.write(f'%typemap(csattributes) {node.type.spelling} "[global::System.Flags]";\n')

            if node.type.spelling.startswith('Urho3D::'):
                for child in node.children:
                    code = read_raw_code(child)
                    if ' = SDL' in code:
                        # Enum uses symbols from SDL. Redefine it with raw enum values.
                        self.fp.write(f'%csconstvalue("{child.enum_value}") {child.spelling};\n')

        elif node.kind == CursorKind.TYPE_ALIAS_DECL:
            underlying_type = node.type.get_canonical()
            if underlying_type.spelling.startswith('Urho3D::FlagSet<'):
                enum_name = node.children[1].type.spelling
                target_name = node.spelling.strip("'")
                self.fp.write(f'using {target_name} = {enum_name};\n')
                self.fp.write(f'%typemap(ctype) {target_name} "size_t";\n')
                self.fp.write(f'%typemap(out) {target_name} "$result = (size_t)$1.AsInteger();"\n')

        return True


class DefineEventsPass(AstPass):
    outputs = ['_events.i']
    re_param_name = re.compile(r'URHO3D_PARAM\(([^,]+),\s*([a-z0-9_]+)\);\s*', re.IGNORECASE)

    def on_begin(self):
        self.fp = open(os.path.join(self.module.args.output, '_events.i'), 'w+')
        self.fp.write(
            '%pragma(csharp) moduleimports=%{\n' +
            'public static class E\n' +
            '{\n'
        )
        return True

    def on_end(self):
        self.fp.write('}\n%}\n')
        self.fp.close()

    def visit(self, node, action: AstAction):
        if node.kind == CursorKind.VAR_DECL and node.type.spelling == 'const Urho3D::StringHash' and node.spelling.startswith('E_'):
            siblings: list = node.parent.children
            try:
                next_node = siblings[siblings.index(node) + 1]
            except IndexError:
                return False

            if next_node.kind == CursorKind.NAMESPACE:
                if next_node.spelling.lower() == node.spelling[2:].replace('_', '').lower():
                    self.fp.write(
                        f'    public class {next_node.spelling}Event {{\n' +
                        f'        private StringHash _event = new StringHash("{next_node.spelling}");\n\n')

                    for param in next_node.children:
                        param_name = read_raw_code(param)
                        param_name = self.re_param_name.match(param_name).group(2)
                        var_name = camel_case(param_name)
                        self.fp.write(f'        public StringHash {var_name} = new StringHash("{param_name}");\n')

                    self.fp.write(
                        f'        public {next_node.spelling}Event() {{ }}\n' +
                        f'        public static implicit operator StringHash({next_node.spelling}Event e) {{ return e._event; }}\n' +
                        '    }\n' +
                        f'    public static {next_node.spelling}Event {next_node.spelling} = new {next_node.spelling}Event();\n\n'
                    )
        return True


def find_program(name, paths=None):
    if paths is None:
        paths = []
    if isinstance(name, str):
        name = [name]
    paths = paths + os.environ['PATH'].split(os.pathsep)
    for path in paths:
        for n in name:
            full_path = os.path.join(path, n)
            if os.path.isfile(full_path):
                return full_path


class Urho3DModule(Module):
    exclude_headers = [
        r'/Urho3D/Precompiled.h',
        r'/Urho3D/Container/.+$',
        r'/Urho3D/Graphics/[^/]+/.+\.h$',
    ]

    def __init__(self, args):
        super().__init__(args)
        self.name = 'Urho3D'
        self.compiler_parameters += ['-std=c++17']
        if os.name == 'nt':
            self.compiler_parameters += ['-cc1', '-x', 'c++', '-fms-extensions']
        else:
            llvm_config = find_program('llvm-config', ['/usr/local/opt/llvm/bin'])
            self.compiler_parameters += \
                filter(lambda s: len(s), subprocess.check_output([llvm_config, '--cppflags']).decode().strip().split(' '))

            if sys.platform == 'linux':
                version = subprocess.check_output([llvm_config, '--version']).decode().strip()
                self.include_directories += [f'/usr/lib/clang/{version}/include']

        self.include_directories += [
            os.path.dirname(self.args.input),
            os.path.join(os.path.dirname(self.args.input), 'ThirdParty')
        ]
        self.exclude_headers = [re.compile(pattern, re.IGNORECASE) for pattern in self.exclude_headers]

    def register_passes(self, passes: list):
        passes += [DefineConstantsPass, DefineRefCountedPass, FindFlagEnums, CleanEnumValues, DefineEventsPass]

    def gather_files(self):
        yield os.path.join(self.args.input, '../ThirdParty/SDL/include/SDL/SDL_joystick.h')
        yield os.path.join(self.args.input, '../ThirdParty/SDL/include/SDL/SDL_gamecontroller.h')
        yield os.path.join(self.args.input, '../ThirdParty/SDL/include/SDL/SDL_keycode.h')
        for root, dirs, files in os.walk(self.args.input):
            for file in files:
                if not file.endswith('.h'):
                    continue
                file_path = os.path.join(root, file)
                if not any([ex.search(file_path) is not None for ex in self.exclude_headers]):
                    yield file_path


def main():
    logging.basicConfig(level=logging.DEBUG)

    bind = argparse.ArgumentParser()
    bind.add_argument('-I', action='append', dest='includes', default=[])
    bind.add_argument('-D', action='append', dest='defines', default=[])
    bind.add_argument('-O', action='append', dest='parameters', default=[])
    bind.add_argument('input')
    bind.add_argument('output')
    if len(sys.argv) == 2 and os.path.isfile(sys.argv[1]):
        # Load options from a file
        program_args = open(sys.argv[1]).read().split('\n')
        while '' in program_args:
            program_args.remove('')
    else:
        program_args = sys.argv[1:]
    args = bind.parse_args(program_args)

    # Filter out CMake's generators.
    args.parameters = filter(lambda p: '$<' not in p, args.parameters)

    # Clean up properties files because passes append to them
    for file in os.listdir(args.output):
        if file.startswith('_properties_') and file.endswith('.i'):
            os.unlink(f'{args.output}/{file}')

    if os.name == 'nt':
        try:
            Config.library_file = os.environ['URHO3D_LIBCLANG_PATH']
        except KeyError:
            Config.library_file = r'C:\Program Files\LLVM\bin\libclang.dll'
    else:
        with suppress(KeyError):
            Config.library_file = os.environ['URHO3D_LIBCLANG_PATH']

    generator = Generator()
    module = Urho3DModule(args)
    generator.process(module, args)


if __name__ == '__main__':
    main()
